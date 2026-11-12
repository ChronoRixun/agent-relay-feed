#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: a small local web page over the delegate jobs.

Reads the same files as `delegate.py watch` (jobs/<id>/meta.json + status.json, the per-agent
activity log, progress notes and result) and turns each raw activity line into something a person can
read at a glance - "Read task-020g.md", "Run tests", "git commit" - with stall / loop warnings and
optional watched processes. Native Claude Code subagents (Agent tool) appear beside the relay jobs,
read from their transcripts by claude_agents.py.

    python feed.py [--port 8765] [--hours 12]      then open http://127.0.0.1:8765

Read-only: it never writes to a job, an agent folder or anything else.
"""
import argparse
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import claude_agents as CA
import delegate as D
import results as R
from delegate_config import CONFIG, ignored_activity_path, normalize_path, repetition

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'feed.html'
MISSION_PAGE = HERE / 'mission.html'
EVIDENCE_PAGE = HERE / 'evidence.html'
ORBIT_PAGE = HERE / 'orbit.html'
STALL_S = CONFIG["stall_seconds"]
LOOP_N = CONFIG["loop_count"]

_LINE = re.compile(r'^(?:\d{4}-\d\d-\d\d )?(\d\d:\d\d:\d\d)\s+(.*)$')
_CMD = re.compile(r'^command \(exit (-?\d+)\):\s*(.*)$')
_KIND = re.compile(r'^(message|files changed|web search|mcp tool|error|ERROR|session|turn completed|job \S+ started)\b:?\s*(.*)$')
_KIMI_TOOL = re.compile(r'tool (\w+): (\{.*?)(?=; tool \w+: \{|$)')
_RETRY = re.compile(r'Reconnecting\.\.\.|Falling back from WebSockets|stream disconnected', re.I)
_INBOX_SUFFIX = re.compile(r'-[a-z0-9]{4}$')


# ------------------------------------------------------------------ turning commands into words

def _base(p):
    p = p.strip().strip("'\"").rstrip("/\\")
    return re.split(r'[\\/]', p)[-1] or p


def _quoted(text):
    return re.findall(r"'([^']+)'|\"([^\"]+)\"", text)


def _first_paths(text, n=3):
    found = []
    for a, b in _quoted(text):
        s = a or b
        if (re.search(r'[\\/]|\.\w{1,5}$', s) and not s.startswith('-') and len(s) < 160
                and re.match(r'^[\w~.:/\\-]', s) and not re.search(r'\s(import|=)|[;(),"]', s)):
            found.append(_base(s))
    return found[:n]


def _split_commands(text):
    """Top-level pieces of a shell line (; && || newlines), ignoring separators inside quotes or here-strings."""
    if re.search(r"@'|<<\s*'?\w+'?", text):            # inline script: treat as one piece
        return [text]
    parts, cur, q = [], [], None
    i = 0
    while i < len(text):
        ch = text[i]
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == ';' or text.startswith('&&', i) or text.startswith('||', i) or ch == '\n':
            parts.append(''.join(cur))
            cur = []
            i += 2 if text.startswith(('&&', '||'), i) else 1
            continue
        cur.append(ch)
        i += 1
    parts.append(''.join(cur))
    parts = [_SHELL_KEYWORD.sub('', p.strip().removesuffix('...').strip()).rstrip(')}').strip() for p in parts]
    return [p for p in parts if p and not re.match(r'^\$env:|^cd\b|^Set-Location\b|^echo\b|^Write-(Host|Output)\b|'
                                                   r'^(?:for|select)\s+\w+\s+in\b|^(?:done|fi|esac)$|^\[\[?\s|^set\s+[-+]', p.strip(), re.I)]


# bash control words, subshells and `timeout N` in front of the command that does the work
_SHELL_KEYWORD = re.compile(r'^(?:(?:do|then|else|if|while|until|time)\s+|!\s*|[({]\s*|timeout\s+-?[\w.]+\s+)+')


_STDLIB = set('sys os re json math time copy types struct pathlib collections itertools functools subprocess shutil glob '
              'hashlib zlib io csv argparse datetime random statistics textwrap typing dataclasses importlib traceback'.split())


def _modules(code):
    mods = re.findall(r'\bfrom\s+([\w.]+)\s+import\b', code)
    rest = re.sub(r'\bfrom\s+[\w.]+\s+import\s+[\w.]+(?:\s*,\s*[\w.]+)*', ' ', code)
    for group in re.findall(r'\bimport\s+([\w.]+(?:\s*,\s*[\w.]+)*)', rest):
        mods += group.split(',')
    out = []
    for m in mods:
        m = m.strip().split('.')[0]
        if m and m not in _STDLIB and not m[0].isupper() and m not in out:
            out.append(m)
    return out


def _python_detail(code):
    reads = [_base(a or b) for a, b in re.findall(r"Path\(['\"]([^'\"]+)['\"]\)\.read_(?:text|bytes)|open\(['\"]([^'\"]+)['\"]", code)]
    if reads and 'write' not in code:
        return ('read', 'Read', ', '.join(dict.fromkeys(reads[:3])))
    mods = _modules(code)
    return ('code', 'Python snippet', ('uses ' + ', '.join(mods[:3])) if mods else ', '.join(_first_paths(code, 2)))


def humanize_piece(cmd):
    """(icon, verb, detail) for one shell command."""
    c = cmd.strip()
    low = c.lower()
    if c.startswith("@'") or re.search(r"<<\s*'?\w+'?|python3?\s+-\s*$|\|\s*python3?\s+-", c):
        if re.search(r'\bimport\b|\bprint\(|\bdef\b', c):
            return _python_detail(c)
        return ('code', 'PowerShell script', ', '.join(_first_paths(c, 2)))
    m = re.match(r'^(?:&\s*)?(?:python3?|py)(?:\.exe)?(?:\s+-[uIBX]\S*)*\s+-c\b\s*(.*)$', c, re.I)
    if m:
        return _python_detail(m.group(1))
    if re.match(r'^\(?(get-content|cat|type|gc|head|tail|sed -n|less|more)\b', low):
        files = _first_paths(c) or [_base(x) for x in re.findall(r'(?:^|\s)([\w./\\:-]+\.\w{1,6})\b', c)[:3]]
        tail = ' (end)' if re.search(r'-tail\b|^tail\b', low) else ' (start)' if re.search(r'-head\b|^head\b', low) else ''
        return ('read', 'Read', ', '.join(dict.fromkeys(files)) + tail)
    if re.match(r'^(rg --files|ls|dir|get-childitem|gci|get-item|find|tree)\b', low):
        target = _first_paths(c, 1) or [w for w in c.split()[1:] if not w.startswith('-')][:1]
        return ('list', 'List', _base(target[0]) if target else '')
    if re.match(r'^(rg|grep|select-string|findstr|git grep)\b', low):
        pats = re.findall(r"'([^']+)'|\"([^\"]+)\"", c)
        pat = next((a or b for a, b in pats), '') or (c.split()[1] if len(c.split()) > 1 else '')
        return ('search', 'Search', f'“{pat[:60]}”')
    m = re.match(r'^git\s+(?:-C\s+("[^"]+"|\'[^\']+\'|\S+)\s+)?(\S+)(.*)$', c)
    if m:
        repo, sub, rest = m.groups()
        sub = sub.lower()
        detail = ''
        if sub == 'commit':
            msg = re.search(r'-m\s+["\']([^"\']+)', rest)
            detail = msg.group(1)[:70] if msg else ''
        elif sub in ('merge', 'checkout', 'switch', 'fetch', 'cherry-pick', 'rebase', 'branch', 'worktree', 'clone', 'diff', 'log'):
            detail = ' '.join(w for w in rest.split() if not w.startswith('-') and w != 'FETCH_HEAD')[:70]
        return ('git', f'git {sub}', (f'[{_base(repo)}] ' if repo else '') + detail)
    if re.search(r'pytest|unittest|tests[\\/]unit[\\/]run\.py|run_tests|ctest|_test\.exe|parity|selftest|self_test', low):
        return ('test', 'Run tests', ', '.join(_base(x) for x in re.findall(r'[\w./\\-]+\.(?:py|exe)', c)[:2]))
    if re.search(r'cmake --build|msbuild|\bninja\b|npm run build|\btsc\b', low):
        return ('build', 'Build', '')
    if re.match(r'^(start-process|nohup)\b', low):
        return ('proc', 'Start process', ', '.join(_first_paths(c)))
    if re.match(r'^(stop-process|taskkill|kill|pkill)\b', low):
        return ('proc', 'Stop process', '')
    if re.match(r'^(new-item|set-content|out-file|add-content|tee)\b', low) or '[io.file]::write' in low:
        return ('write', 'Write', ', '.join(_first_paths(c)))
    if re.match(r'^(copy-item|cp|robocopy|xcopy|move-item|mv)\b', low):
        return ('copy', 'Copy', ' → '.join(_first_paths(c, 2)))
    if re.match(r'^(remove-item|rm|del|rmdir)\b', low):
        return ('delete', 'Delete', ', '.join(_first_paths(c)))
    if re.match(r'^(sleep|start-sleep)\b', low):
        return ('proc', 'Wait', ' '.join(w for w in c.split()[1:3] if not w.startswith('-')))
    if re.match(r'^(get-process|tasklist|get-ciminstance)\b', low):
        return ('proc', 'Check processes', '')
    if re.match(r'^(get-filehash|certutil|sha1sum|sha256sum)\b', low):
        return ('hash', 'Hash', ', '.join(_first_paths(c)))
    m = re.match(r'^(?:&\s*)?(?:["\']?[^"\'\s]*python3?(?:\.exe)?["\']?|py)(?:\s+-[uIBX]\S*)*\s+("[^"]+"|\'[^\']+\'|\S+)(.*)$', c, re.I)
    if m:
        args = ' '.join(w.strip("'\"") for w in m.group(2).split()[:3] if not w.startswith(('>', '*>', '2>')))
        return ('run', f'Run {_base(m.group(1))}', args[:60])
    m = re.match(r'^(?:&\s*)?("[^"]+"|\'[^\']+\'|\S+)', c)
    head = _base(m.group(1)) if m else c[:20]
    if re.match(r'^(get|select|format|measure|test|write|sort|where|foreach|out|join|split|resolve)-\w+$', head, re.I):
        return ('misc', head, ' '.join(c.split()[1:4])[:60])
    return ('run', head, ' '.join(c.split()[1:4])[:60])


def humanize_command(raw):
    text = D._condense_command(raw)
    pieces = _split_commands(text) or [text]
    icon, verb, detail = humanize_piece(pieces[0])
    # prefer the most telling piece: tests / build / game / git commit outrank reads
    rank = dict(misc=-1, list=0, proc=0, hash=0, game=6, test=5, build=5, git=4, write=3, run=3, copy=2, delete=2, code=2, search=1, read=1)
    for p in pieces[1:]:
        h = humanize_piece(p)
        if rank.get(h[0], 0) > rank.get(icon, 0):
            icon, verb, detail = h
    more = len(pieces) - 1
    return dict(icon=icon, verb=verb, detail=detail.strip(), more=more, raw=text[:600])


def humanize_kimi(text):
    out = []
    for name, js in _KIMI_TOOL.findall(text):
        try:
            a = json.loads(js)
        except (ValueError, TypeError):
            # Activity logs truncate long tool JSON. Recover visible string arguments.
            a = {}
            for key, value in re.findall(r'"(command|path|pattern|query)":\s*"((?:\\.|[^"\\])*)', js):
                try:
                    a[key] = json.loads('"' + value + '"')
                except ValueError:
                    a[key] = value
        if not isinstance(a, dict):
            a = {}
        if name == 'Bash':
            h = humanize_command(a.get('command', js))
        elif name in ('Read', 'ReadFile'):
            h = dict(icon='read', verb='Read', detail=_base(a.get('path', '')), more=0, raw=js[:600])
        elif name in ('Write', 'WriteFile', 'Edit', 'StrReplaceFile'):
            h = dict(icon='write', verb='Write' if 'Write' in name else 'Edit', detail=_base(a.get('path', '')), more=0, raw=js[:600])
        elif name in ('Grep', 'Glob', 'SearchWeb'):
            h = dict(icon='search', verb='Search', detail=f"“{(a.get('pattern') or a.get('query') or '')[:60]}”", more=0, raw=js[:600])
        else:
            h = dict(icon='run', verb=name, detail='', more=0, raw=js[:600])
        out.append(h)
    return out


def humanize_claude(name, inp):
    """One Claude Code tool call (Bash, Edit, Grep, Agent, mcp__server__tool, ...) as a feed step."""
    return CA.humanize_tool(name, inp, humanize_command, _base)


# ------------------------------------------------------------------ jobs

def task_name(meta, job_id):
    if meta.get('name'):
        return meta['name']
    src = Path(meta.get('prompt_source') or '')
    if src.name and src.parent.name == 'inbox':
        return _INBOX_SUFFIX.sub('', src.stem)
    return Path(meta.get('cwd') or '').name or job_id


_activity_cache = {}


def parse_activity(path):
    """Events and retry count for one activity log; re-parsed only when the file's size or mtime changes.

    The page polls every few seconds and most of the 60 jobs shown are finished, so without this every poll
    re-humanised thousands of unchanged commands (about 0.7 s of CPU per poll). A log written in the last two
    seconds is not cached: a rewrite inside one timestamp tick could keep both its size and its mtime."""
    try:
        st = os.stat(path)
    except OSError:
        _activity_cache.pop(str(path), None)
        return _parse_activity(path)
    key = (st.st_size, st.st_mtime_ns)
    hit = _activity_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1], hit[2]
    events, retries = _parse_activity(path)
    if time.time() - st.st_mtime > 2:
        if len(_activity_cache) >= 240:     # only the newest 60 jobs are shown; drop logs that scrolled away
            _activity_cache.clear()
        _activity_cache[str(path)] = (key, events, retries)
    else:
        _activity_cache.pop(str(path), None)
    return events, retries


def _parse_activity(path):
    events, retries = [], 0
    for line in D.read_text(path).splitlines():
        m = _LINE.match(line)
        if not m:
            if events and line.strip():
                events[-1]['text'] = (events[-1].get('text', '') + ' ' + line.strip())[:2000]
            continue
        t, rest = m.groups()
        if _RETRY.search(rest):
            retries += 1
            continue
        c = _CMD.match(rest)
        if c:
            ev = dict(t=t, kind='command', rc=int(c.group(1)), **humanize_command(c.group(2)))
            events.append(ev)
            continue
        if rest.startswith('tool '):
            for h in humanize_kimi(rest):
                events.append(dict(t=t, kind='command', rc=0, **h))
            continue
        k = _KIND.match(rest)
        kind, text = (k.group(1), k.group(2)) if k else ('other', rest)
        if kind.startswith('job '):
            kind, text = 'started', rest
        elif kind == 'files changed':
            kind = 'files'
            text = ', '.join(_base(p.split(' ', 1)[-1]) for p in text.split(', '))
        elif kind == 'ERROR':
            kind = 'error'
        events.append(dict(t=t, kind=kind, text=text[:2000]))
    return events, retries


def _secs(hms):
    h, m, s = map(int, hms.split(':'))
    return h * 3600 + m * 60 + s


def tail_lines(path, n):
    lines = [l.rstrip() for l in D.read_text(path).splitlines() if l.strip()]
    return lines[-n:]


_newest_cache = {}


def newest_write(folder, depth=2, limit=4000):
    """Newest file mtime in an agent's working folder (two levels, bounded); cached for 20 s per folder."""
    if not folder or not os.path.isdir(folder) or ignored_activity_path(folder):
        return 0
    hit = _newest_cache.get(folder)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    newest, seen, stack = 0, 0, [(folder, 0)]
    while stack and seen < limit:
        d, n = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if n < depth and not e.name.startswith('.'):
                                stack.append((e.path, n + 1))
                        else:
                            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        pass
        except OSError:
            pass
    _newest_cache[folder] = (time.time(), newest)
    return newest


_EFFORT_NAMES = {'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'extra high', 'max': 'max'}


def effective_label(meta, profile):
    """Model name with the effort the job actually ran at (relays pass --effort over the profile default)."""
    label = meta.get('label', profile)
    effort = meta.get('effort')
    if not effort or meta.get('backend') == 'kimi':
        return label
    base = label.split(' (')[0]
    return f"{base} ({_EFFORT_NAMES.get(effort, effort)})"


def job_summary(jd, now):
    meta = D.load_json(jd / 'meta.json', {}) or {}
    st = D.effective_status(jd) or {}
    profile = meta.get('profile', '?')
    ap = D.agent_paths(profile, jd.name)
    act = Path(meta.get('activity_file') or ap['activity'])
    events, retries = parse_activity(act)
    started = st.get('started') or meta.get('created') or now
    finished = st.get('finished')
    try:
        last_touch = act.stat().st_mtime
    except OSError:
        last_touch = started
    # an agent busy inside one long step (e.g. Kimi's parallel sub-tasks) logs nothing until the step ends, but its
    # files keep changing: count recent writes in its own working folder as activity too
    workdir_touch = newest_write(meta.get('cwd')) if st.get('state') in ('running', 'starting') else 0
    last_touch = max(last_touch, workdir_touch)
    cmds = [e for e in events if e['kind'] == 'command']
    loop, _polling = repetition([(e['raw'], _secs(e['t'])) for e in cmds[-10:]], count=LOOP_N)
    msgs = [e for e in events if e['kind'] == 'message']
    state = st.get('state', '?')
    running = state in ('running', 'starting')
    idle = now - last_touch
    info = D.phase_info(jd)
    stages = [dict(t=e.get('t'), kind=e['kind'], text=e.get('text')) for e in D.read_journal(jd)
              if e.get('kind') in ('phase', 'asked', 'answered')][-200:]
    if not running:     # a question dies with its job
        info.update(question=None, question_since=None, queued=0)
    return dict(
        id=jd.name, name=task_name(meta, jd.name), profile=profile, label=effective_label(meta, profile),
        backend=meta.get('backend'), mode=meta.get('mode'), cwd=meta.get('cwd'), state=state,
        started=started, finished=finished, elapsed=(finished or now) - started, idle=idle if running else None,
        # a worker waiting for an answer is quiet on purpose
        stalled=running and idle > STALL_S and not info['question'], loop=running and loop, retries=retries,
        now_msg=msgs[-1]['text'] if msgs else None, now_msg_t=msgs[-1]['t'] if msgs else None,
        current=cmds[-1] if cmds else None,
        counts=dict(commands=len(cmds), failed=sum(1 for e in cmds if e.get('rc')),
                    files=sum(1 for e in events if e['kind'] == 'files'), messages=len(msgs),
                    errors=sum(1 for e in events if e['kind'] == 'error')),
        events=events[-60:],
        progress=tail_lines(meta.get('progress_file') or ap['progress'], 6),
        result=(D.read_text(jd / 'result.md').strip()[:4000] if not running else None),
        errors=st.get('errors') or [],
        source='relay', agent_type=None, depth=None, parent=None, tokens=st.get('usage'),
        total_cost_usd=st.get('total_cost_usd'), verdict=st.get('verdict'), flags=st.get('flags') or 0, stages=stages, after=meta.get('after') or [],
        provider=meta.get('provider'), max_minutes=meta.get('max_minutes'),
        resumed_by=st.get('resumed_by'), died=False, resume=None, **info,
    )


# ------------------------------------------------------------------ Claude Code subagents

_claude = dict(reader=None)


def claude_reader():
    """One incremental transcript reader per feed process (None when switched off in the config)."""
    if not CONFIG['claude_agents_enabled']:
        return None
    if _claude['reader'] is None:
        _claude['reader'] = CA.ClaudeAgents(humanize=humanize_claude)
    return _claude['reader']


def claude_job_summary(rec, now):
    """A subagent record in the same shape as a relay job, with the same stall and loop rules."""
    running = rec['state'] == 'running'
    last_touch = rec['last_activity']
    if running:     # inside one long tool call the transcript is silent; recent writes in its folder count
        last_touch = max(last_touch, newest_write(rec['workdir']))
    idle = now - last_touch
    loop, _polling = repetition(rec['recent'], count=LOOP_N)
    events = rec['events']
    cmds = [e for e in events if e['kind'] == 'command']
    msgs = [e for e in events if e['kind'] == 'message']
    finished = rec['finished']
    return dict(
        id=rec['id'], name=rec['label'], profile=rec['agent_type'], label=rec['model'] or 'Claude',
        backend='claude', mode=None, cwd=rec['workdir'], state=rec['state'], total_cost_usd=None, max_minutes=None,
        started=rec['started'], finished=finished, elapsed=(finished or now) - rec['started'],
        idle=idle if running else None, stalled=running and idle > STALL_S, loop=running and loop, retries=0,
        now_msg=msgs[-1]['text'] if msgs else None, now_msg_t=msgs[-1]['t'] if msgs else None,
        current=cmds[-1] if cmds else None,
        counts=dict(commands=rec['steps'], failed=rec['failed'], files=rec['edits'], messages=rec['messages'],
                    errors=len(rec['errors'])),
        events=events, progress=[], result=rec['result'], errors=rec['errors'],
        source='claude', agent_type=rec['agent_type'], depth=rec['depth'], parent=rec['parent'], tokens=rec['tokens'],
        phase=rec.get('phase'), phase_since=rec.get('phase_since'), phases=rec.get('phases', 0),
        question=None, question_since=None, queued=0, verdict=rec.get('verdict'), flags=rec.get('flags') or 0, stages=rec.get('stages') or [], after=[], resumed_by=None, provider=None,
        died=bool(rec.get('died')), resume=rec.get('resume'),
    )


def claude_jobs(now):
    reader = claude_reader()
    if reader is None:
        return []
    return [claude_job_summary(r, now) for r in reader.scan(now)]


# ------------------------------------------------------------------ optional watched processes

_processes = dict(at=0, list=[])
_processes_lock = threading.Lock()


def read_processes():
    """Best effort executable paths; unavailable process data yields an empty list."""
    try:
        if os.name == 'nt':
            ps = ('Get-CimInstance Win32_Process | '
                  'ForEach-Object { "$($_.ProcessId)|$($_.Name)|$($_.ExecutablePath)" }')
            out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            return [l.split('|', 2) for l in out.splitlines() if l.count('|') == 2]
        out = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True,
                             text=True, timeout=15).stdout
        found = []
        for line in out.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2:
                pid, executable = fields
                # Linux comm may be a short name; /proc exposes the real executable.
                try:
                    executable = os.readlink(f'/proc/{pid}/exe')
                except OSError:
                    pass
                found.append([pid, _base(executable), executable])
        return found
    except (OSError, subprocess.SubprocessError):
        return []


def process_owner(name, path, job_cwds, spec):
    norm = normalize_path(path).casefold()
    cwds = sorted(((j, c) for j, c in job_cwds if c and not ignored_activity_path(c)),
                  key=lambda x: -len(x[1]))
    owner = next((j for j, c in cwds if norm.startswith(normalize_path(c).casefold() + '/')), None)
    for root in CONFIG['job_path_roots']:
        prefix = normalize_path(root).casefold() + '/'
        if owner is None and norm.startswith(prefix):
            folder = norm[len(prefix):].split('/', 1)[0]
            owner = next((j for j, c in cwds if folder in normalize_path(c).casefold().split('/')),
                         f'agent ({folder})')
    if owner is None:
        owner = next((j for j, c in cwds if any(p.casefold() in normalize_path(c).casefold()
                     for p in spec.get('job_cwd_patterns', []))), None)
    if owner is not None:
        return owner, 'agent'
    for rule in spec.get('owners', []):
        if any(normalize_path(p).casefold() in norm for p in rule['path_patterns']):
            return rule['owner'], 'owner'
    owner = spec.get('owner', 'unknown')
    return owner, 'unknown' if owner == 'unknown' else 'owner'


def watched_processes(job_cwds):
    specs = CONFIG['watched_processes']
    if not specs:
        return []
    with _processes_lock:
        if time.time() - _processes['at'] > 8:
            _processes['list'] = read_processes()
            _processes['at'] = time.time()
        found = []
        for pid, name, path in _processes['list']:
            spec = next((s for s in specs if s['name'].casefold() == name.casefold()), None)
            if spec is None:
                continue
            who, owner_kind = process_owner(name, path, job_cwds, spec)
            found.append(dict(pid=pid, name=name, path=path, who=who, owner_kind=owner_kind))
        return found


def snapshot(hours, demo=False):
    if demo:        # invented jobs; nothing is read from this machine's job data
        from demo_data import demo_snapshot
        return demo_snapshot(hours)
    now = time.time()
    jobs = []
    if D.JOBS_ROOT.is_dir():
        with os.scandir(D.JOBS_ROOT) as it:     # DirEntry.is_dir needs no extra stat per job folder on Windows
            folders = [Path(e.path) for e in it if e.is_dir()]
        for jd in sorted(folders, reverse=True)[:60]:
            try:
                s = job_summary(jd, now)
            except Exception as e:      # one broken job must not blank the page
                s = dict(id=jd.name, name=jd.name, state='?', error=repr(e), started=now, elapsed=0, events=[])
            if s['state'] in ('running', 'starting', 'queued') or now - (s.get('finished') or s['started']) < hours * 3600:
                jobs.append(s)
    try:
        jobs += [s for s in claude_jobs(now)
                 if s['state'] == 'running' or now - (s['finished'] or s['started']) < hours * 3600]
    except Exception as e:      # an unreadable transcript must not blank the page either
        jobs.append(dict(id='claude-agents', name='Claude subagents', state='?', error=repr(e), started=now,
                         elapsed=0, events=[], source='claude'))
    reviewed = D.load_reviewed()
    for j in jobs:
        j['reviewed'] = j.get('id') in reviewed
    jobs.sort(key=lambda j: -(j.get('started') or 0))
    # a subagent usually shares its whole session's folder, so watched processes are attributed to relay jobs only
    cwds = [(j['name'], j.get('cwd')) for j in jobs if j['state'] in ('running', 'starting') and j.get('source') != 'claude']
    try:
        slots = D.leases.snapshot(D.DELEGATE_ROOT, CONFIG['resources'], D.job_running, now) if CONFIG['resources'] else []
        for row in slots:   # programs of the machine's owner that keep new holders out for now
            names = (CONFIG['resources'].get(row['name']) or {}).get('yield_to')
            row['closed'] = D.owner_programs(names) if names else []
    except Exception:       # an unreadable lease file must not blank the page
        slots = []
    return dict(now=now, jobs=jobs, processes=watched_processes(cwds), slots=slots, stall=STALL_S,
                can_answer=bool(CONFIG['feed_pin']), api_spend=D.api_spend(now),
                nous_spend=dict(month=D.nous.month_spend(D.DELEGATE_ROOT, now), budget=CONFIG['nous_monthly_usd'] or None),
                process_panel=bool(CONFIG['watched_processes']), checkpoint_minutes=CONFIG['checkpoint_minutes'])


# ------------------------------------------------------------------ evidence gallery

IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
               '.bmp': 'image/bmp', '.webp': 'image/webp'}
EVIDENCE_MAX_BYTES = 40 * 1024 * 1024


def _job_evidence(jd, checked=None):
    """The evidence entries of a job's result block (only entries with a path)."""
    checked = (D.load_json(jd / 'result.json') or {}) if checked is None else checked
    data = checked.get('data') if isinstance(checked.get('data'), dict) else {}
    entries = data.get('evidence') if isinstance(data.get('evidence'), list) else []
    return checked, data, [e for e in entries if isinstance(e, dict) and isinstance(e.get('path'), str) and e['path']]


def _claude_finished_records():
    reader = claude_reader()
    if reader is None:
        return []
    try:
        return [r for r in reader.scan() if r['state'] in D.TERMINAL]
    except Exception:
        return []


def _claude_records_with_results():
    return [r for r in _claude_finished_records() if r.get('checked')]


def _evidence_items(job, entries, started):
    items = []
    for i, e in enumerate(entries):
        path = e['path']
        try:
            stat = os.stat(path)
            exists, size, mtime = os.path.isfile(path), stat.st_size, stat.st_mtime
        except OSError:
            exists, size, mtime = False, 0, None
        dims = R.image_size(path) if exists else None
        image = exists and Path(path).suffix.lower() in IMAGE_TYPES and dims is not None
        rec = R.capture.load_record(path) if image else None
        items.append(dict(job=job, i=i, path=path, shows=e.get('shows'), exists=exists, size=size, mtime=mtime,
                          captured=R.capture.describe(rec) if rec else None, claimed_process=e.get('process') or None,
                          image=image, width=dims[0] if dims else None, height=dims[1] if dims else None,
                          claimed_width=e.get('width') or None, claimed_height=e.get('height') or None,
                          stale=bool(exists and started and mtime < started - 60)))
    return items


def evidence(hours):
    """Every evidence file cited by recently finished jobs, with what can be read off the file itself."""
    now, jobs, reviewed = time.time(), [], D.load_reviewed()

    def add(job, name, label, state, checked, data, entries, finished, started, errors):
        flags = checked.get('flags') or 0
        look = D.needs_look(state, checked.get('verdict'), flags)
        if not entries and not look:
            return          # nothing to look at: a clean job that cites no evidence
        problems = [f['text'] for f in checked.get('findings') or [] if not f.get('ok')]
        if state != 'done':
            problems += [str(e) for e in errors or []][:3]
        jobs.append(dict(id=job, name=name, label=label, state=state, verdict=checked.get('verdict'), flags=flags,
                         finished=finished, summary=data.get('summary'), needs_look=look, problems=problems[:12],
                         reviewed=job in reviewed, items=_evidence_items(job, entries, started)))

    if D.JOBS_ROOT.is_dir():
        for jd in sorted((p for p in D.JOBS_ROOT.iterdir() if p.is_dir()), reverse=True)[:120]:
            st = D.load_json(jd / 'status.json', {}) or {}
            if st.get('state') not in D.TERMINAL or st.get('resumed_by'):
                continue        # still going, or superseded by its automatic resume
            meta = D.load_json(jd / 'meta.json', {}) or {}
            finished = st.get('finished') or meta.get('created') or now
            if now - finished > hours * 3600:
                continue
            checked, data, entries = _job_evidence(jd)
            add(jd.name, task_name(meta, jd.name), meta.get('label'), st.get('state'), checked, data, entries,
                finished, st.get('started'), st.get('errors'))
    for rec in _claude_finished_records():
        finished = rec.get('finished') or now
        if now - finished <= hours * 3600:
            checked, data, entries = _job_evidence(None, rec.get('checked') or {})
            add(rec['id'], rec['label'], rec.get('model') or 'Claude', rec['state'], checked, data, entries,
                finished, rec.get('started'), rec.get('errors'))
    jobs.sort(key=lambda j: -j['finished'])
    return dict(now=now, jobs=jobs, can_review=bool(CONFIG['feed_pin']))


def evidence_file(job, index):
    """(content type, bytes) for one cited evidence file, or None. Files are addressed only by job
    and position in that job's result block, never by a path from the request."""
    if not re.fullmatch(r'[\w.-]+', job or ''):
        return None
    if (D.JOBS_ROOT / job).is_dir():
        _checked, _data, entries = _job_evidence(D.JOBS_ROOT / job)
    else:       # a Claude Code subagent that ended with a result block
        rec = next((r for r in _claude_records_with_results() if r['id'] == job), None)
        if rec is None:
            return None
        _checked, _data, entries = _job_evidence(None, rec['checked'])
    if not 0 <= index < len(entries):
        return None
    path = entries[index]['path']
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > EVIDENCE_MAX_BYTES:
            return None
        kind = IMAGE_TYPES.get(Path(path).suffix.lower())
        if kind and R.image_size(path) is not None:
            return kind, Path(path).read_bytes()
        with open(path, 'rb') as f:         # anything else is shown as the start of a text file
            return 'text/plain; charset=utf-8', f.read(20000).decode('utf-8', 'replace').encode('utf-8')
    except OSError:
        return None


# ------------------------------------------------------------------ background watcher

class Watcher(threading.Thread):
    """While the feed runs it also keeps two things moving that otherwise need a waiting
    session: queued jobs are released when what they wait for ends, and Claude Code subagents,
    which have no runner of their own, get an alert when they finish."""

    def __init__(self, interval=5):
        super().__init__(daemon=True)
        self.interval = interval
        self.running = set()        # subagents seen running by this process

    def step(self):
        try:
            D.start_ready()
        except Exception:
            pass
        D.settle_slots()        # shared state goes back once the last holder of a resource is gone
        reader = claude_reader()
        if reader is None or not CONFIG['notify']:
            return
        for rec in reader.scan():
            if rec['state'] == 'running':
                self.running.add(rec['id'])
            elif rec['id'] in self.running:
                self.running.discard(rec['id'])
                checked = rec.get('checked') or {}
                summary = ((checked.get('data') or {}).get('summary') or '')
                if rec.get('died'):
                    summary = f"Died of a connection error, not finished. {rec['id']}: resume it once from its session."
                D.notify.outcome(rec['label'], rec.get('model') or 'Claude', rec['state'], rec.get('verdict'),
                                 rec.get('flags') or 0, summary,
                                 D.fmt_secs((rec.get('finished') or time.time()) - rec['started']))

    def run(self):
        while True:
            try:
                self.step()
            except Exception:
                pass
            time.sleep(self.interval)


_pin_failures = {}      # client address -> (wrong PINs in a row, locked until)


class Handler(BaseHTTPRequestHandler):
    hours = 12
    demo = False

    def _send(self, code, body, ctype):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass    # the browser went away mid-reply (refresh, closed tab, overlapping poll): nothing to do

    def do_GET(self):
        if self.path.startswith('/api/jobs'):
            self._send(200, json.dumps(snapshot(self.hours, demo=getattr(self, 'demo', False))), 'application/json')
        elif getattr(self, 'demo', False) and self.path.split('?')[0].startswith('/api/evidence'):
            if self.path.split('?')[0] == '/api/evidence':     # the demo has no files to show
                self._send(200, json.dumps(dict(now=time.time(), jobs=[], can_review=False)), 'application/json')
            else:
                self._send(404, 'not found', 'text/plain')
        elif self.path in ('/', '/index.html'):
            self._send(200, PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif self.path.split('?')[0] in ('/mission', '/mission.html'):
            self._send(200, MISSION_PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif self.path.split('?')[0] in ('/orbit', '/orbit.html'):
            self._send(200, ORBIT_PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif self.path.split('?')[0] in ('/evidence', '/evidence.html'):
            self._send(200, EVIDENCE_PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif self.path.split('?')[0] == '/api/evidence':
            self._send(200, json.dumps(evidence(self.hours)), 'application/json')
        elif self.path.split('?')[0] == '/api/evidence/file':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                found = evidence_file(query.get('job', [''])[0], int(query.get('i', ['-1'])[0]))
            except ValueError:
                found = None
            if found is None:
                self._send(404, 'not found', 'text/plain')
            else:
                self._send(200, found[1], found[0])
        else:
            self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        """Answer a worker's question, send it a message, or mark a finished job as reviewed. Needs the PIN."""
        path = self.path.split('?')[0]
        if path not in ('/api/answer', '/api/tell', '/api/review'):
            return self._send(404, 'not found', 'text/plain')

        def reply(code, **body):
            self._send(code, json.dumps(body), 'application/json')

        if getattr(self, 'demo', False):
            return reply(403, error='This is the demo: there are no real jobs to answer.')
        pin = CONFIG['feed_pin']
        if not pin:
            return reply(403, error='Answering from the feed is off. Set feed_pin in the config to turn it on.')
        who, now = self.client_address[0], time.time()
        wrong, locked_until = _pin_failures.get(who, (0, 0))
        if locked_until > now:
            return reply(429, error='Too many wrong PINs. Try again in a few minutes.')
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= 20000:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, OSError):
            return reply(400, error='Bad request.')
        if not hmac.compare_digest(str(body.get('pin', '')).encode('utf-8'), pin.encode('utf-8')):
            wrong += 1
            _pin_failures[who] = (0, now + 300) if wrong >= 5 else (wrong, 0)
            return reply(401, error='Wrong PIN.')
        _pin_failures.pop(who, None)
        job, text = str(body.get('job', '')), str(body.get('text', ''))[:4000]
        if path == '/api/review':
            if not re.fullmatch(r'[\w.-]+', job) or not ((D.JOBS_ROOT / job).is_dir() or job.startswith('agent-')):
                return reply(404, error='No such job.')
            on = D.set_reviewed(job, not body.get('undo'))
            return reply(200, ok=True, reviewed=on)
        jd = D.JOBS_ROOT / job
        if not re.fullmatch(r'[\w.-]+', job) or not jd.is_dir():
            return reply(404, error='No such job.')
        try:
            message = D.answer_job(jd, text) if path == '/api/answer' else D.tell_job(jd, text)
        except ValueError as e:
            return reply(409, error=str(e))
        reply(200, ok=True, message=message)

    def log_message(self, *a):
        pass


def _quiet_handle_error(server, request, client_address):
    """A client disconnecting mid-reply is not an error worth a traceback."""
    import sys
    if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        return
    type(server).handle_error(server, request, client_address)


def lan_address():
    """This machine's address on the local network, for the URL to open from another device (None if unknown)."""
    import socket
    try:
        addr = socket.gethostbyname(socket.gethostname())
    except OSError:
        return None
    return None if addr.startswith('127.') else addr


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default=CONFIG['feed_host'],
                    help='address to listen on (default 127.0.0.1; 0.0.0.0 shows the read-only feed, '
                         'with no login, to every device on your local network)')
    ap.add_argument('--port', type=int, default=CONFIG['feed_port'])
    ap.add_argument('--hours', type=float, default=12, help='show finished jobs from the last N hours (default 12)')
    ap.add_argument('--dump', action='store_true', help='print one snapshot as JSON and exit (for testing)')
    ap.add_argument('--open', action='store_true', help='open the loopback feed in your default browser')
    ap.add_argument('--demo', action='store_true', help='show invented jobs and processes without reading job data')
    a = ap.parse_args(argv)
    if a.dump:
        print(json.dumps(snapshot(a.hours, demo=a.demo), indent=1))
        return
    Handler.hours = a.hours
    Handler.demo = a.demo
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.handle_error = _quiet_handle_error.__get__(srv)
    everywhere = a.host in ('0.0.0.0', '::', '')
    local = '127.0.0.1' if everywhere else a.host
    url = f'http://{local}:{srv.server_port}'
    print(f'Agent Relay Feed on {url}  (Ctrl+C to stop)', flush=True)
    if everywhere:
        lan = lan_address() or "<this computer's address>"
        print(f'Other devices on this network: http://{lan}:{srv.server_port}  '
              '(read-only, no login)', flush=True)
    if a.open:
        try:
            if not webbrowser.open(url):
                print(f'Browser did not open; open {url} manually.', flush=True)
        except webbrowser.Error as exc:
            print(f'Browser did not open: {exc}; open {url} manually.', flush=True)
    if not a.demo:      # the demo leaves real queued jobs and alerts alone
        Watcher().start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
