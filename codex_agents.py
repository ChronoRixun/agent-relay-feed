# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Read-only incremental native Codex session summaries.

Only allowlisted metadata and synthetic activity descriptions leave this reader. Prompts,
reasoning, shell arguments, tool results and assistant bodies are deliberately not retained.
Both old response-item and new item-completed streams are understood; the latter is preferred.
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from delegate_config import CONFIG, TRANSIENT, normalize_path

UUID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
TERMINAL = {'done', 'failed', 'cancelled', 'lost'}


def epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def name(value, limit=100):
    # Metadata and filenames can contain control characters; never let them control a terminal.
    return ' '.join(str(value or '').split())[:limit]


def basename(value):
    return name(str(value or '').replace('\\', '/').rsplit('/', 1)[-1])


def kind(value):
    return str(value or '').replace('_', '').lower()


class Transcript:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.file_identity = None
        self.sid = None
        self.hidden = False
        self.parent_id = None
        self.depth = 0
        self.role = 'session'
        self.nickname = ''
        self.cwd = None
        self.model = ''
        self.effort = ''
        self.started = None
        self.last = 0
        self.finished = None
        self.state = 'idle'
        self.turn = None
        self.died = False
        self.modern = False
        self.events = []
        self.legacy = []
        self.recent = []
        self.legacy_recent = []
        self.phase_at = None
        self.seen = set()
        self.steps = self.failed = self.edits = self.messages = 0
        self.legacy_steps = 0
        self.tokens = {}
        self.context = None
        self.lines = 0

    def pump(self):
        try:
            st = self.path.stat()
            identity = (st.st_dev, st.st_ino, st.st_ctime_ns if os.name == 'nt' else None)
            if self.file_identity is not None and (identity != self.file_identity or st.st_size < self.offset):
                self.__init__(self.path)
            self.file_identity = identity
            with self.path.open('rb') as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        break  # do not consume a partially written UTF-8/JSON line
                    self.offset = f.tell()
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        self.lines += 1
                        self.feed(value)
        except OSError:
            pass

    def activity(self, at, verb, icon='proc', detail='', failed=False, edit=False, ident=None, legacy=False, signature=None):
        key = ('legacy' if legacy else 'item', ident)
        if ident and key in self.seen:
            return
        if ident:
            self.seen.add(key)
        ev = dict(t=time.strftime('%H:%M:%S', time.localtime(at)), kind='command',
                  icon=icon, verb=verb, detail=detail, more=0, raw=verb + (' ' + detail if detail else ''), rc=1 if failed else 0)
        fingerprint = hashlib.sha256(str(signature if signature is not None else (verb, detail)).encode('utf8')).hexdigest()
        self.phase_at = at
        if legacy:
            self.legacy_recent = (self.legacy_recent + [(fingerprint, at)])[-10:]
            self.legacy_steps += 1
            self.legacy = (self.legacy + [ev])[-60:]
        else:
            self.recent = (self.recent + [(fingerprint, at)])[-10:]
            self.steps += 1
            self.failed += int(failed)
            self.edits += int(edit)
            self.events = (self.events + [ev])[-60:]
        self.last = max(self.last, at)

    def usage(self, values):
        if not isinstance(values, dict):
            return
        for field, key in [('input_tokens', 'input'), ('cached_input_tokens', 'cached'),
                           ('output_tokens', 'output'), ('reasoning_output_tokens', 'reasoning')]:
            n = values.get(field)
            if type(n) is int and n >= 0:
                self.tokens[key] = max(self.tokens.get(key, 0), n)

    def feed(self, row):
        p = row.get('payload')
        if not isinstance(p, dict):
            return
        at = epoch(row.get('timestamp')) or self.last or 0
        typ = row.get('type')
        if typ in ('response_item', 'token_usage_record') or (typ == 'event_msg' and p.get('type') in ('token_count', 'item_completed')):
            # Fresh work is evidence of liveness even when its private body is never retained.
            self.last = max(self.last, at)
        if typ == 'session_meta':
            sid = p.get('id')
            if not isinstance(sid, str) or not UUID.fullmatch(sid):
                return
            self.sid = sid.lower()
            source = p.get('source')
            sub = source.get('subagent') if isinstance(source, dict) else None
            spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
            self.hidden = isinstance(source, dict) and not isinstance(spawn, dict)
            if isinstance(spawn, dict):
                parent = spawn.get('parent_thread_id')
                self.parent_id = 'codex-' + parent.lower() if isinstance(parent, str) and UUID.fullmatch(parent) else None
                self.depth = spawn.get('depth') if type(spawn.get('depth')) is int else 1
                self.nickname = name(p.get('agent_nickname') or spawn.get('agent_nickname') or basename(spawn.get('agent_path')))
                self.role = name(p.get('agent_role') or spawn.get('agent_role') or 'subagent', 50)
            self.cwd = name(p.get('cwd'), 1000) or None
            self.started = at
            self.last = max(self.last, at)
        elif typ == 'turn_context':
            self.model = name(p.get('model'), 80) or self.model
            self.effort = name(p.get('effort'), 30)
            self.cwd = name(p.get('cwd'), 1000) or self.cwd
        elif typ == 'token_usage_record':
            self.usage(p.get('thread_token_usage'))
            usage = p.get('usage') or {}
            if isinstance(usage, dict) and type(usage.get('input_tokens')) is int:
                self.context = usage['input_tokens']
        elif typ == 'event_msg':
            event = p.get('type')
            if event == 'task_started':
                if p.get('turn_id') != self.turn or self.state != 'running':
                    # Freeze prior-turn legacy history before preferring modern items in this turn.
                    self.steps += self.legacy_steps
                    self.events = (self.events + self.legacy)[-60:]
                    self.recent = (self.recent + self.legacy_recent)[-10:]
                    self.legacy_steps, self.legacy, self.legacy_recent = 0, [], []
                    self.modern = False
                    self.started = at
                self.turn = p.get('turn_id')
                self.state, self.finished, self.died = 'running', None, False
                self.last = max(self.last, at)
                # Context usage comes from the last request, not model window capacity.
            elif event in ('task_complete', 'turn_aborted', 'task_aborted'):
                if p.get('turn_id') and self.turn and p['turn_id'] != self.turn:
                    return
                self.state = 'done' if event == 'task_complete' else 'cancelled'
                self.finished = at
                self.last = max(self.last, at)
            elif event == 'error' and not p.get('will_retry'):
                # Never retain the error body: it may contain request data or credentials.
                self.died = bool(TRANSIENT.search(str(p.get('message') or p.get('error') or '')))
                self.state, self.finished = 'failed', at
                self.last = max(self.last, at)
            elif event == 'token_count':
                info = p.get('info') or {}
                if isinstance(info, dict):
                    self.usage(info.get('total_token_usage'))
                    last = info.get('last_token_usage') or {}
                    if isinstance(last, dict) and type(last.get('input_tokens')) is int:
                        self.context = last['input_tokens']
            elif event == 'item_completed' and isinstance(p.get('item'), dict):
                if not self.modern:
                    self.legacy_steps, self.legacy, self.legacy_recent = 0, [], []
                self.modern = True
                self.item(p['item'], at)
        elif typ == 'response_item' and not self.modern:
            # Legacy calls lack parsed command summaries. Never display their argument/input body.
            if p.get('type') in ('function_call', 'custom_tool_call'):
                tool = name(p.get('name'), 70)
                self.activity(at, 'Tool', 'run', tool, ident=p.get('call_id') or p.get('id'), legacy=True, signature=p.get('arguments') or p.get('input') or tool)

    def item(self, item, at):
        typ, ident = kind(item.get('type')), item.get('id')
        failed = item.get('status') == 'failed' or bool(item.get('exit_code'))
        if typ == 'commandexecution':
            # parsed_cmd is metadata supplied by Codex; do not echo the command or its arguments.
            parsed = item.get('parsed_cmd')
            verbs, files = [], []
            for part in parsed if isinstance(parsed, list) else []:
                if not isinstance(part, dict):
                    continue
                action = {'read':'Read', 'search':'Search', 'listfiles':'List files'}.get(kind(part.get('type')))
                if action:
                    verbs.append(action)
                    path = part.get('path') or part.get('name')
                    if path:
                        files.append(basename(path))
            self.activity(at, '/'.join(dict.fromkeys(verbs)) or 'Run command', 'run', ', '.join(files[:3]), failed, ident=ident, signature=item.get('command'))
        elif typ == 'filechange':
            changes = item.get('changes') or {}
            files = list(changes) if isinstance(changes, dict) else [c.get('path') for c in changes if isinstance(c, dict) and isinstance(c.get('path'), str)] if isinstance(changes, list) else []
            self.activity(at, 'Edit files', 'write', ', '.join(basename(x) for x in files[:3]), failed, True, ident)
        elif typ == 'mcptoolcall':
            self.activity(at, 'MCP tool', 'mcp', name(item.get('server'), 40) + ': ' + name(item.get('tool'), 60), failed, ident=ident)
        elif typ in ('collabagenttoolcall', 'subagentactivity'):
            self.activity(at, 'Agent activity', 'agent', name(item.get('tool') or item.get('kind'), 60), failed, ident=ident)
        elif typ == 'imageview':
            self.activity(at, 'View image', 'read', basename(item.get('path')), ident=ident)
        elif typ in ('websearch', 'websearchtoolcall'):
            self.activity(at, 'Web search', 'search', ident=ident)
        elif typ == 'agentmessage':
            key = ('message', ident)
            if ident and key in self.seen:
                return
            if ident:
                self.seen.add(key)
            self.messages += 1
            self.last = max(self.last, at)
            # Only a synthetic status, even if the original message contains code or file content.
            self.events = (self.events + [dict(t=time.strftime('%H:%M:%S', time.localtime(at)), kind='message', text='Agent reported progress')])[-60:]
        elif typ == 'contextcompaction':
            self.activity(at, 'Compact context', ident=ident)

    def record(self, now, stall):
        last = self.last or self.started or now
        quiet = max(0, now - last)
        state = 'lost' if self.state == 'running' and quiet > max(3600, 4 * stall) else self.state
        events = (self.events + self.legacy)[-60:]
        commands = [e for e in events if e['kind'] == 'command']
        model = self.model + (f' ({self.effort})' if self.model and self.effort else '')
        result = {'done':'Turn completed; this session can resume.', 'cancelled':'Turn interrupted.',
                  'failed':'Turn failed; inspect it in the originating Codex session.',
                  'lost':'No activity within the native-session timeout; completion is unknown.'}.get(state)
        return dict(id='codex-' + self.sid, agent_id=self.sid, source='codex', path=str(self.path),
                    project=basename(self.cwd), session=self.sid, label=self.nickname or f'Codex session #{self.sid[-8:]}',
                    agent_type=self.role, depth=self.depth, parent_id=self.parent_id, parent=None,
                    model_id=self.model, model=model, cwd=self.cwd, workdir=self.cwd,
                    started=self.started or last, last_activity=last, finished=(self.finished or last) if state in TERMINAL else None,
                    state=state, quiet=quiet, stalled=state == 'running' and quiet > stall,
                    tokens=dict(self.tokens, context=self.context) if self.tokens else None,
                    phase=commands[-1]['verb'] if commands else None, phase_since=self.phase_at if commands else None,
                    phases=0, stages=[], steps=self.steps + self.legacy_steps,
                    failed=self.failed, edits=self.edits, messages=self.messages,
                    events=list(events), recent=(self.recent + self.legacy_recent)[-10:], result=result,
                    died=state == 'failed' and self.died,
                    resume='Resume from the originating Codex session; the feed cannot resume native sessions.' if self.died else None,
                    final=None, checked=None, verdict=None, flags=0,
                    errors=[result] if state in ('failed', 'cancelled', 'lost') else [])


class CodexAgents:
    def __init__(self, root=None, config=None, relay_sessions=None, stall_seconds=None):
        cfg = dict(CONFIG)
        cfg.update(config or {})
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.root = Path(os.path.expandvars(os.path.expanduser(str(root or cfg.get('codex_sessions_root') or home / 'sessions'))))
        self.enabled = cfg.get('codex_agents_enabled', True)
        self.window = cfg.get('codex_agents_window_hours', 24) * 3600
        self.projects = [normalize_path(p).casefold() for p in cfg.get('codex_agents_projects', [])]
        self.stall = stall_seconds if stall_seconds is not None else cfg['stall_seconds']
        self.relay_sessions = relay_sessions or (lambda: {})
        self.files = {}
        self.lock = threading.Lock()

    def discover(self, now):
        if not self.root.is_dir():
            return []
        paths = []
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not Path(folder, d).is_symlink()]
            for fn in files:
                p = Path(folder, fn)
                try:
                    if fn.endswith('.jsonl') and not p.is_symlink() and now - p.stat().st_mtime <= self.window:
                        paths.append(p)
                except OSError:
                    pass
        return paths

    def scan(self, now=None):
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        with self.lock:
            paths = set(self.discover(now))
            for gone in self.files.keys() - paths:
                del self.files[gone]
            best = {}
            for p in paths:
                tr = self.files.setdefault(p, Transcript(p))
                tr.pump()
                if not tr.sid or tr.hidden:
                    continue
                rank = (tr.last, tr.lines, tr.offset)
                if tr.sid not in best or rank > best[tr.sid][0]:
                    best[tr.sid] = (rank, tr)
            relay = self.relay_sessions()
            records = [tr.record(now, self.stall) for _, tr in best.values() if tr.sid not in relay]
            labels = {r['id']: r['label'] for r in records}
            for r in records:
                parent = r['parent_id']
                if parent:
                    sid = parent.removeprefix('codex-')
                    jobs = relay.get(sid) or []
                    r['parent'] = ('Relay session #' + sid[-8:] + ' (' + ', '.join(name(j.get('id'), 100) for j in jobs) + ')') if jobs else labels.get(parent, 'Codex session #' + sid[-8:])
            return sorted((r for r in records if not self.projects or any(p in normalize_path(r['workdir'] or '').casefold() for p in self.projects)), key=lambda r: (-r['last_activity'], r['id']))
