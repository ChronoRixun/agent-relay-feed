#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: read native Claude Code subagents (Agent tool) as job-like records.

Claude Code writes every subagent's transcript to
  <projects root>/<project>/<session id>/subagents/agent-<id>.jsonl
with a sibling agent-<id>.meta.json ({"agentType", "description", "spawnDepth", "parentAgentId", ...}).
The reader discovers transcripts changed within a window, tails each one by byte offset (transcripts reach
several MB, and the last line may still be half written) and keeps a small running summary per agent:
model, working folder, latest steps, token usage and whether the agent's turn is still open.

Read-only: it never writes to a transcript or anything else. Only the agent's own short text lines and
one-line tool steps are kept; prompts and tool results are never stored.
"""
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from delegate_config import CONFIG, normalize_path, shorten_paths

DONE_GRACE_S = 5        # a closed turn must stay quiet this long before the agent counts as done
EVENTS_KEPT = 60
RECENT_KEPT = 10
MESSAGE_CHARS = 300
RESULT_LINES = 8
RESULT_CHARS = 1500
TERMINAL = {"done", "failed", "cancelled", "lost"}

# Exact model ids first; anything else of the form claude-<family>-<major>[-<minor>][-<date>] is spelled out.
MODEL_LABELS = {
    "claude-fable-5-1": "Fable 5.1",
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
}
_MODEL = re.compile(r"^claude-([a-z]+)-(\d{1,2})(?:-(\d{1,2}))?(?:-\d{8})?(?:\[\w+\])?$")
_OLD_MODEL = re.compile(r"^claude-(\d{1,2})(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?(?:\[\w+\])?$")
_TURN_CLOSERS = {"end_turn", "stop_sequence", "refusal"}


def model_label(model_id):
    """claude-fable-5-1 -> Fable 5.1; unknown ids come back unchanged."""
    if not model_id:
        return ""
    if model_id in MODEL_LABELS:
        return MODEL_LABELS[model_id]
    m = _MODEL.match(model_id)
    if m:
        family, major, minor = m.groups()
    else:
        m = _OLD_MODEL.match(model_id)
        if not m:
            return model_id
        major, minor, family = m.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def relay_agent_names():
    """Agent types that are this tool's own relays; their relay job already has a card of its own."""
    try:
        from install_agents import SPECS
        return {s["name"] for s in SPECS}
    except Exception:
        return set()


def _epoch(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _hms(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _one_line(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _identity(name, inp):
    """Same tool with the same input = the same step (for loop detection); hashed, so no content is kept."""
    try:
        blob = json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(inp)
    if name in ("Bash", "PowerShell", "Monitor") and isinstance(inp, dict):
        return " ".join(str(inp.get("command", "")).split())[:600]
    return f"{name} {hashlib.sha1(blob.encode('utf-8', 'replace')).hexdigest()[:16]}"


_FILE_KEYS = ("file_path", "notebook_path", "path")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:)?[\\/]")


def _tool_path(inp):
    if not isinstance(inp, dict):
        return ""
    return next((inp[k] for k in _FILE_KEYS if isinstance(inp.get(k), str) and inp[k]), "")


def basic_step(name, inp):
    """Fallback step when no humanizer is supplied (the terminal watch only needs identities)."""
    return dict(icon="run", verb=name, detail="", more=0, raw=name)


class Transcript:
    """Running summary of one subagent transcript, fed line by line."""

    def __init__(self, path, humanize=None):
        self.path = Path(path)
        self.humanize = humanize or basic_step
        self.offset = 0
        self.meta, self.meta_mtime = {}, None
        self.agent_id = self.path.stem.removeprefix("agent-")
        self.killed_at = None       # set from the parent session's task notifications
        self.session = self.path.parent.parent.name
        self.project = self.path.parent.parent.parent.name
        self.reset()

    def reset(self):
        self.offset = 0
        self.started = self.last_line_at = None
        self.cwds, self.folders = Counter(), Counter()
        self.model_id = None
        self.events, self.recent = [], []
        self.steps = self.failed = self.edits = self.messages = 0
        self.pending = {}           # tool_use id -> its event (to mark a failed result)
        self.turn_open = True
        self.api_error = None
        self.last_text, self.last_text_msg = [], None
        self.cur_msg, self.cur_usage = None, {}
        self.usage = Counter()      # finished messages' usage
        self.context = 0            # prompt size of the latest message

    # ------------------------------------------------------------ reading

    def load_meta(self):
        meta_path = self.path.with_name(self.path.stem + ".meta.json")
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            return
        if mtime != self.meta_mtime:
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                self.meta = data if isinstance(data, dict) else {}
                self.meta_mtime = mtime
            except (OSError, ValueError):
                pass

    def pump(self):
        """Read complete lines added since the last call; a half-written last line waits for the next call."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:         # replaced or truncated: start over
            self.reset()
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.offset += len(line)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        self.feed(ev)
        except OSError:
            pass

    def feed(self, ev):
        t = _epoch(ev.get("timestamp"))
        if t is not None:
            self.started = self.started or t
            self.last_line_at = t
        if isinstance(ev.get("cwd"), str) and ev["cwd"]:
            self.cwds[ev["cwd"]] += 1
        kind = ev.get("type")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if kind == "assistant":
            self._assistant(ev, msg, t)
        elif kind == "user":
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        step = self.pending.pop(b.get("tool_use_id"), None)
                        if step is not None and b.get("is_error"):
                            step["rc"] = 1
                            self.failed += 1
            else:
                self.turn_open = True      # a new prompt, follow-up or notification: the agent works again
                self.api_error = None
        elif kind == "attachment" and (ev.get("attachment") or {}).get("type") == "queued_command":
            self.turn_open = True

    def _assistant(self, ev, msg, t):
        mid = msg.get("id")
        if mid != self.cur_msg:        # one API message is written as one line per content block
            self.usage.update({k: v for k, v in self.cur_usage.items()})
            self.cur_msg, self.cur_usage = mid, {}
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if usage:
            self.cur_usage = {k: usage.get(k) or 0 for k in keys if isinstance(usage.get(k) or 0, int)}
            self.context = sum(self.cur_usage.get(k, 0) for k in keys if k != "output_tokens")
        model = msg.get("model")
        if model and not str(model).startswith("<"):
            self.model_id = model
        stamp = _hms(t) if t is not None else ""
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text", "")).strip():
                if self.last_text_msg != mid:
                    self.last_text, self.last_text_msg = [], mid
                self.last_text.append(str(b["text"]))
                self.messages += 1
                self._event(dict(t=stamp, kind="message", text=_one_line(b["text"], MESSAGE_CHARS)))
            elif b.get("type") == "tool_use":
                name, inp = str(b.get("name") or "tool"), b.get("input") if isinstance(b.get("input"), dict) else {}
                step = dict(t=stamp, kind="command", rc=0, **self.humanize(name, inp))
                self.steps += 1
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    self.edits += 1
                path = _tool_path(inp)
                if _ABSOLUTE.match(path):
                    self.folders[normalize_path(os.path.dirname(path))] += 1
                self.pending[b.get("id")] = step
                if len(self.pending) > 200:
                    self.pending.pop(next(iter(self.pending)))
                self.recent = (self.recent + [(_identity(name, inp), t or 0)])[-RECENT_KEPT:]
                self._event(step)
        stop = msg.get("stop_reason")
        if stop in _TURN_CLOSERS:
            self.turn_open = False
            self.pending.clear()       # every tool call is answered before the model can end its turn
            self.api_error = (str(ev.get("error") or "API error")) if (ev.get("isApiErrorMessage") or stop == "refusal") else None
        elif stop == "tool_use":
            self.turn_open = True

    def _event(self, event):
        self.events.append(event)
        if len(self.events) > EVENTS_KEPT * 2:
            del self.events[:-EVENTS_KEPT]

    # ------------------------------------------------------------ summary

    def workdir(self, cwd):
        """The transcript cwd, unless the agent's file tools mostly work in one folder outside it."""
        if not self.folders:
            return cwd
        folder, n = self.folders.most_common(1)[0]
        base = normalize_path(cwd or "").casefold()
        if n >= 3 and not (base and (folder.casefold() + "/").startswith(base + "/")):
            return folder
        return cwd

    def record(self, now, stall_s, lost_s):
        mtime = self.mtime()
        if self.last_line_at and mtime and mtime - self.last_line_at > lost_s:
            mtime = None               # touched long after its last line: a copied or restored file, not activity
        last = max(self.last_line_at or 0, mtime or 0) or now
        quiet = now - last
        pending = bool(self.pending)
        meta = self.meta
        killed = self.killed_at is not None and self.killed_at >= last - DONE_GRACE_S
        if (meta.get("stoppedByUser") or killed) and quiet >= DONE_GRACE_S:
            state = "cancelled"
        elif not self.turn_open and not pending and quiet >= DONE_GRACE_S:
            state = "failed" if self.api_error else "done"
        elif quiet >= lost_s:
            state = "lost"             # the session ended under it (closed window, crash) without a final turn
        else:
            state = "running"
        cwd = self.cwds.most_common(1)[0][0] if self.cwds else None
        tokens = Counter(self.usage)
        tokens.update(self.cur_usage)
        result = None
        if state in ("done", "failed") and self.last_text:
            lines = [l.rstrip() for l in "\n".join(self.last_text).strip().splitlines()]
            result = "\n".join(lines[:RESULT_LINES])[:RESULT_CHARS]
            if len(lines) > RESULT_LINES:
                result += "\n…"
        return dict(
            id="agent-" + self.agent_id, agent_id=self.agent_id, path=str(self.path),
            project=self.project, session=self.session,
            label=_one_line(meta.get("description"), 120) or f"Claude subagent {self.agent_id[:8]}",
            agent_type=meta.get("agentType") or "subagent", depth=meta.get("spawnDepth"),
            parent_id=meta.get("parentAgentId"), model_id=self.model_id, model=model_label(self.model_id),
            cwd=cwd, workdir=self.workdir(cwd), started=self.started or last, last_activity=last,
            finished=last if state in TERMINAL else None, state=state, quiet=quiet,
            stalled=state == "running" and quiet > stall_s,
            tokens=dict(output=tokens["output_tokens"], context=self.context) if tokens else None,
            steps=self.steps, failed=self.failed, edits=self.edits, messages=self.messages,
            events=self.events[-EVENTS_KEPT:], recent=list(self.recent), result=result,
            errors=[f"API error: {_one_line(self.api_error, 200)}"] if state == "failed" else [],
        )

    def mtime(self):
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None


_NOTICE = re.compile(rb"<task-id>([0-9a-zA-Z]+)</task-id>.*?<status>(\w+)</status>")


class ParentNotices:
    """Tails a session transcript for task notifications; remembers when each agent was stopped (killed)."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.killed = {}            # agent id -> epoch of the stop

    def pump(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:
            self.offset, self.killed = 0, {}
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.offset += len(line)
                    if b"task-notification" not in line or b"killed" not in line:
                        continue
                    try:
                        at = _epoch(json.loads(line).get("timestamp"))
                    except (ValueError, AttributeError):
                        at = None
                    for task, status in _NOTICE.findall(line):
                        if status == b"killed" and at:
                            self.killed[task.decode("ascii", "replace")] = at
        except OSError:
            pass


class ClaudeAgents:
    """Discovers recently changed subagent transcripts and keeps one Transcript per file."""

    def __init__(self, root=None, window_hours=None, projects=None, humanize=None, hide_types=None,
                 stall_seconds=None, config=None):
        cfg = CONFIG if config is None else config
        self.root = Path(os.path.expanduser(root or cfg["claude_projects_root"]))
        self.window = (window_hours if window_hours is not None else cfg["claude_agents_window_hours"]) * 3600
        self.projects = [normalize_path(p).casefold() for p in (cfg["claude_agents_projects"] if projects is None else projects)]
        self.humanize = humanize
        self.hide = relay_agent_names() if hide_types is None else set(hide_types)
        self.stall_s = stall_seconds if stall_seconds is not None else cfg["stall_seconds"]
        self.lost_s = max(3600, 4 * self.stall_s)
        self.files = {}
        self.parents = {}           # (session folder, session id) -> ParentNotices
        self.lock = threading.Lock()

    def discover(self, now):
        found = []
        try:
            projects = list(os.scandir(self.root))
        except OSError:
            return found
        for proj in projects:
            if not proj.is_dir(follow_symlinks=False):
                continue
            try:
                sessions = list(os.scandir(proj.path))
            except OSError:
                continue
            for sess in sessions:
                sub = os.path.join(sess.path, "subagents")
                if not sess.is_dir(follow_symlinks=False) or not os.path.isdir(sub):
                    continue
                try:
                    with os.scandir(sub) as it:
                        for e in it:
                            if e.name.startswith("agent-") and e.name.endswith(".jsonl"):
                                try:
                                    if now - e.stat().st_mtime <= self.window:
                                        found.append(e.path)
                                except OSError:
                                    pass
                except OSError:
                    pass
        return found

    def _wanted(self, rec):
        if (rec["agent_type"].rsplit(":", 1)[-1]) in self.hide:
            return False
        if not self.projects:
            return True
        hay = [rec["project"].casefold(), normalize_path(rec.get("cwd") or "").casefold()]
        return any(p in h for p in self.projects for h in hay)

    def scan(self, now=None):
        """Records for every subagent changed within the window (newest first)."""
        now = time.time() if now is None else now
        with self.lock:
            paths = set(self.discover(now))
            for gone in set(self.files) - paths:
                del self.files[gone]
            for p in paths:
                tr = self.files.get(p)
                if tr is None:
                    tr = self.files[p] = Transcript(p, self.humanize)
                tr.load_meta()
                tr.pump()
            for key in {(tr.path.parent.parent, tr.session) for tr in self.files.values()}:
                notes = self.parents.get(key)
                if notes is None:
                    notes = self.parents[key] = ParentNotices(key[0].with_suffix(".jsonl"))
                notes.pump()
            for tr in self.files.values():
                notes = self.parents.get((tr.path.parent.parent, tr.session))
                tr.killed_at = notes.killed.get(tr.agent_id) if notes else None
            # A forked or restored session copies its subagent transcripts: show each agent once, preferring the
            # copy with the newest activity, then the oldest file (the original).
            best = {}
            for tr in self.files.values():
                rec = tr.record(now, self.stall_s, self.lost_s)
                rank = (rec["last_activity"], -(tr.mtime() or 0))
                if rec["agent_id"] not in best or rank > best[rec["agent_id"]][0]:
                    best[rec["agent_id"]] = (rank, rec)
            records = [rec for _, rec in best.values()]
        labels = {r["agent_id"]: r["label"] for r in records}
        out = []
        for r in records:
            r["parent"] = labels.get(r["parent_id"]) if r["parent_id"] else None
            if self._wanted(r):
                out.append(r)
        return sorted(out, key=lambda r: -r["started"])


def humanize_tool(name, inp, humanize_command, base):
    """(icon, verb, detail) plus raw hover text for one Claude Code tool call; never includes file contents."""
    inp = inp if isinstance(inp, dict) else {}

    def step(icon, verb, detail="", raw=None):
        return dict(icon=icon, verb=verb, detail=detail, more=0, raw=(raw if raw is not None else name)[:600])

    if name in ("Bash", "PowerShell") and inp.get("command"):
        return humanize_command(str(inp["command"]))
    path = _tool_path(inp)
    short = shorten_paths(normalize_path(path)) if path else ""
    if name == "Read":
        return step("read", "Read", base(path), short)
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return step("write", "Edit", base(path), short)
    if name == "Write":
        return step("write", "Write", base(path), short)
    if name == "Grep":
        return step("search", "Search", f"“{str(inp.get('pattern', ''))[:60]}”", f"Grep {inp.get('pattern', '')}")
    if name == "Glob":
        return step("list", "Find files", str(inp.get("pattern", ""))[:60], f"Glob {inp.get('pattern', '')}")
    if name == "WebSearch":
        return step("search", "Web search", f"“{str(inp.get('query', ''))[:60]}”")
    if name == "WebFetch":
        host = re.sub(r"^\w+://", "", str(inp.get("url", ""))).split("/")[0]
        return step("web", "Fetch", host[:60])
    if name in ("Agent", "Task"):
        kind = str(inp.get("subagent_type") or "agent")
        return step("agent", "Start agent", f"{kind}: {_one_line(inp.get('description'), 60)}")
    if name == "SendMessage":
        return step("agent", "Message agent", str(inp.get("to") or inp.get("recipient") or "")[:40])
    if name == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp.get("todos"), list) else []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return step("todo", "Update todos", f"{done}/{len(todos)} done" if todos else "")
    if name == "Monitor":
        return step("proc", "Monitor", _one_line(inp.get("description"), 60))
    if name == "TaskStop":
        return step("proc", "Stop task")
    if name == "ToolSearch":
        return step("search", "Load tools", _one_line(inp.get("query"), 60))
    if name == "Skill":
        return step("run", "Skill", str(inp.get("skill", ""))[:60])
    if name == "SubagentHandback":
        return step("message", "Hand back to caller")
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        server, tool = (parts[1], parts[2]) if len(parts) == 3 else ("mcp", name)
        return step("mcp", f"{server}: {tool.replace('_', ' ')}")
    return step("run", name)
