# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Isolated Claude Code API runner. Secrets stay in memory and the child's env."""
import json
import contextlib
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


def key_path(config):
    return Path(os.path.expandvars(config['claude_api_key_file'])).expanduser()


def read_key(config):
    try:
        key = key_path(config).read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        raise RuntimeError('Claude API key file missing or unreadable; configure claude_api_key_file. No subscription fallback.') from None
    if not key or any(c.isspace() for c in key):
        raise RuntimeError('Claude API key file is empty or invalid. No subscription fallback.')
    return key


def redact(text, key):
    # JSON escaping must also be covered before output is parsed or persisted.
    for secret in sorted({key, json.dumps(key)[1:-1]}, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return text


def child_env(key, home, effort):
    # Do not even retrieve inherited credential values. Ignore settings injected by env,
    # FD credentials, provider routing, remote/gateway modes and custom API endpoints.
    env = {name: os.environ[name] for name in os.environ
           if not name.upper().startswith(('ANTHROPIC_', 'CLAUDE_CODE_'))
           and name.upper() not in {'CLAUDE_CONFIG_DIR', 'CLAUDE_CONFIG', 'CLAUDE_SETTINGS',
                                    'CLAUDECODE', 'CLAUDE_AGENT_SDK_OAUTH_TOKEN'}}
    env.update(ANTHROPIC_API_KEY=key, CLAUDE_CONFIG_DIR=str(home),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               DISABLE_AUTOUPDATER='1', CLAUDE_CODE_EFFORT_LEVEL=effort)
    return env


def find_claude():
    override = os.environ.get('DELEGATE_CLAUDE')
    exe = override or shutil.which('claude')
    if not exe or not Path(exe).is_file():
        raise RuntimeError('claude CLI not found (set DELEGATE_CLAUDE to its executable)')
    # Invoke npm's JS entrypoint directly to preserve stdin/argv through Windows shims.
    if Path(exe).suffix.lower() in {'.cmd', '.ps1'}:
        main = Path(exe).parent / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'cli.js'
        node = shutil.which('node')
        if main.is_file() and node:
            return [node, str(main)]
        raise RuntimeError('Claude npm shim has no cli.js/node; set DELEGATE_CLAUDE to a native executable')
    # Useful for a portable, entirely fake CLI in tests.
    if Path(exe).suffix.lower() == '.py':
        import sys
        return [sys.executable, exe]
    return [exe]


def build_cmd(meta, jd, config):
    selected = {name: config['claude_api_mcp_servers'][name]
                for name in config['claude_api_mcp_allow']
                if name in config['claude_api_mcp_servers']}
    missing = set(config['claude_api_mcp_allow']) - set(selected)
    if missing:
        raise RuntimeError('Missing claude_api_mcp_servers definitions: ' + ', '.join(sorted(missing)))
    mcp = jd / 'claude-mcp.json'
    mcp.write_text(json.dumps({'mcpServers': selected}), encoding='utf-8')
    mode = {'read': 'plan', 'write': 'acceptEdits', 'full': 'bypassPermissions'}[meta['mode']]
    cmd = [*find_claude(), '-p', '--model', meta['model'], '--output-format', 'stream-json',
           '--verbose', '--permission-mode', mode, '--setting-sources', '',
           '--settings', '{"apiKeyHelper":"","disableAllHooks":true}',
           '--strict-mcp-config', '--mcp-config', str(mcp),
           # Billed per token: a spawned subagent re-reads everything with a cold cache (the first review spent
           # most of its $6 on two duplicate Plan subagents). Prompt caching itself is on by default in Claude Code.
           '--disallowedTools', 'Agent', 'Task']
    for folder in meta['add_dirs']:
        cmd += ['--add-dir', folder]
    if meta.get('resume_session'):
        cmd += ['--resume', meta['resume_session']]
    if meta.get('max_usd') is not None:
        cmd += ['--max-budget-usd', str(meta['max_usd'])]
    return cmd


def cost_value(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def run(meta, jd, config, root, status, save_status, kill_tree, cancel_requested):
    """Read pipes in memory, guard init/cost, then persist only redacted output."""
    key = read_key(config)
    home = root / 'claude-api-home'
    home.mkdir(parents=True, exist_ok=True)
    state, errors, rc = None, [], None
    proc = None
    stopped_at = None
    try:
        cmd = build_cmd(meta, jd, config)
        # A resumed session reports total_cost_usd cumulatively (it includes the earlier jobs' spend); bill this job
        # only for its own part. session_cost_usd keeps the raw cumulative figure for the next resume.
        prior = 0.0
        if meta.get('resume_job'):
            try:
                ps = json.loads((jd.parent / meta['resume_job'] / 'status.json').read_text(encoding='utf-8'))
                if ps.get('session_id') == meta.get('resume_session'):
                    prior = float(ps.get('session_cost_usd', ps.get('total_cost_usd')) or 0.0)
            except (OSError, ValueError, TypeError):
                prior = 0.0
        status['session_prior_cost_usd'] = prior
        seen_assistant = False
        (jd / 'command.json').write_text(redact(json.dumps(cmd, indent=2), key), encoding='utf-8')
        env = child_env(key, home, meta['effort'])
        kw = dict(creationflags=0x08000000) if os.name == 'nt' else dict(start_new_session=True)
        with open(jd / 'prompt.md', 'rb') as prompt:
            proc = subprocess.Popen(cmd, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=meta['cwd'], env=env, **kw)
        del env

        def stop():
            nonlocal stopped_at
            if stopped_at is not None:
                return
            stopped_at = time.monotonic()
            kill_tree(proc.pid)
            # Restricted Windows tokens may make taskkill fail with Access denied,
            # even though Popen retains a handle with permission to stop its child.
            if proc.poll() is None:
                proc.kill()

        status['child_pid'] = proc.pid
        save_status()
        messages = queue.Queue(maxsize=256)

        def reader(pipe, kind):
            try:
                for raw in iter(pipe.readline, b''):
                    messages.put((kind, redact(raw.decode('utf-8', errors='replace'), key)))
            finally:
                pipe.close()
                messages.put((kind, None))

        threads = [threading.Thread(target=reader, args=(pipe, kind), daemon=True)
                   for pipe, kind in ((proc.stdout, 'out'), (proc.stderr, 'err'))]
        for thread in threads:
            thread.start()
        ends, initialized, result_seen = 0, False, False
        deadline = time.monotonic() + meta['max_minutes'] * 60
        init_deadline = time.monotonic() + 30
        with (jd / 'events.jsonl').open('w', encoding='utf-8') as events, \
                (jd / 'stderr.log').open('w', encoding='utf-8') as stderr, \
                Path(meta['activity_file']).open('a', encoding='utf-8') as activity:
            while ends < 2:
                if stopped_at is not None and time.monotonic() - stopped_at > 5:
                    # A surviving descendant can hold the pipe open after its parent
                    # exits. Do not let that prevent the runner publishing a status.
                    errors.append('stopped child output did not close; descendant termination may be incomplete')
                    break
                reason = None
                if state is None:
                    if cancel_requested(jd):
                        state = 'cancelled'
                    elif time.monotonic() > deadline:
                        state = 'timeout'
                    elif not initialized and time.monotonic() > init_deadline:
                        reason = 'not billed to API credits: no verified API-key init event'
                    if reason:
                        state, errors = 'failed', errors + [reason]
                    if state:
                        stop()
                try:
                    kind, line = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    ends += 1
                    continue
                if kind == 'err':
                    stderr.write(line)
                    stderr.flush()
                    continue
                events.write(line)
                events.flush()
                try:
                    ev = json.loads(line)
                    if not isinstance(ev, dict):
                        continue
                except ValueError:
                    continue
                if ev.get('type') == 'system' and ev.get('subtype') == 'init':
                    if ev.get('apiKeySource') != 'ANTHROPIC_API_KEY':
                        reason = 'not billed to API credits: init credential source is not ANTHROPIC_API_KEY'
                    else:
                        initialized = True
                        status['billing_verified'] = True
                        status['session_id'] = ev.get('session_id')
                elif not initialized and ev.get('type') != 'system':
                    # Newer CLIs emit housekeeping system events (e.g. ui_invalidate) before init; no model call has
                    # happened yet. Anything else before a verified init is refused.
                    reason = 'not billed to API credits: stream event arrived before verified init'
                if ev.get('type') == 'result':
                    result_seen = True
                    status['usage'] = ev.get('usage') or {}
                    status['session_id'] = ev.get('session_id') or status.get('session_id')
                    if ev.get('is_error') or ev.get('subtype') != 'success':
                        reason = 'Claude API result: ' + str(ev.get('subtype'))
                if ev.get('type') == 'assistant':
                    seen_assistant = True
                cost = cost_value(ev.get('total_cost_usd'))
                if cost is not None and not seen_assistant and meta.get('resume_job'):
                    # On resume the CLI first replays the session's last persisted result; that figure is the true
                    # base (a budget-killed run never persisted its own cost).
                    prior = cost
                    status['session_prior_cost_usd'] = prior
                if cost is not None:
                    status['session_cost_usd'] = max(cost, status.get('session_cost_usd', 0))
                    cost = max(0.0, cost - prior)
                    status['total_cost_usd'] = max(cost, status.get('total_cost_usd', 0))
                    if meta.get('max_usd') is not None and cost > meta['max_usd']:
                        reason = f"max-usd exceeded: ${cost:.6f} > ${meta['max_usd']:.6f}; charges may exceed the cap between cost events"
                save_status()
                summary = summarize(ev)
                if summary:
                    activity.write(time.strftime('%H:%M:%S') + '  ' + summary + '\n')
                    activity.flush()
                if reason:
                    errors.append(reason)
                    state = 'failed'
                    stop()
        for thread in threads:
            thread.join(timeout=1)
        rc = proc.wait(timeout=10)
        if not initialized:
            state = state or 'failed'
            errors.append('not billed to API credits: no verified API-key init event')
        elif not result_seen and state is None:
            state = 'failed'
            errors.append('Claude API exited without a final result; spend is unknown')
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            kill_tree(proc.pid)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        state, errors = 'failed', errors + [redact(f'Claude API runner error: {exc!r}', key)]
    return state, rc, errors


def summarize(ev):
    if ev.get('type') == 'system' and ev.get('subtype') == 'init':
        return 'session ' + str(ev.get('session_id'))
    if ev.get('type') == 'assistant':
        summaries = []
        for item in (ev.get('message') or {}).get('content') or []:
            if item.get('type') == 'text':
                summaries.append('message: ' + ' '.join(item.get('text', '').split())[:400])
            elif item.get('type') == 'tool_use':
                summaries.append('tool ' + str(item.get('name')) + ': ' + json.dumps(item.get('input') or {})[:250])
        return '; '.join(summaries) or None
    if ev.get('type') == 'result':
        return 'result ' + str(ev.get('subtype')) + ' (USD ' + str(ev.get('total_cost_usd', 'unknown')) + ')'
    return None


def append_ledger(root, jd, meta, status):
    root.mkdir(parents=True, exist_ok=True)
    entry = dict(job_id=jd.name, agent=meta['profile'], model=meta['model'],
                 approved=meta.get('approved'), start=status.get('started', meta['created']),
                 end=status.get('finished'), total_cost_usd=status.get('total_cost_usd'),
                 tokens=status.get('usage'), status=status['state'])
    data = (json.dumps(entry, ensure_ascii=True) + '\n').encode('utf-8')
    # Windows append uses seek+write; lock across processes as well as using O_APPEND.
    with ledger_lock(root):
        fd = os.open(root / 'claude-api-ledger.jsonl', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError('incomplete spend ledger write')
        finally:
            os.close(fd)


@contextlib.contextmanager
def ledger_lock(root):
    with (root / 'claude-api-ledger.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise OSError('spend ledger lock timed out') from None
                    time.sleep(.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
