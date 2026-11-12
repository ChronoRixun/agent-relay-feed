#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: bridge that lets Claude Code sessions and subagents hand work to external coding
agents (Codex, Kimi and Claude Code via API credits) and collect their final report.

Jobs run in a detached runner process, so a job outlives any single Bash call.
`run` waits up to --wait seconds; if the job is still going it reports
STATUS: running and the caller polls with `wait <job_id>`.

Every agent has a fixed folder, ~/.claude/delegate/agents/<agent>/:
  progress/<job_id>.md   progress notes written by the model while it works
  activity/<job_id>.log  timestamped activity log written by the bridge
  reports/<job_id>.md    final report of each job
  latest-report.md       copy of the most recent final report

Usage:
  delegate.py run <agent> --prompt-file F|- [--cwd DIR] [--mode read|write|full]
                  [--preamble FILE]... [--effort E] [--add-dir D]...
                  [--resume JOB_ID] [--wait SECS] [--max-minutes N]
  delegate.py wait <job_id> [--wait SECS]
  delegate.py wait --any [--wait SECS]     return when any job ends; each outcome is reported once
  delegate.py board [--hours N]            one line per running agent: phase, time, last activity
  delegate.py tell <job_id> "<message>"    queue a message; the worker reads it at its next stage
  delegate.py answer <job_id> "<reply>"    answer a worker's open question so it continues
  delegate.py phase|ask|check --job <id>   run by a worker: name its stage (and read messages),
                                           ask a question and wait, or just read messages
  delegate.py status <job_id>
  delegate.py cancel <job_id>
  delegate.py list [-n N]
  delegate.py profiles
"""
import argparse
import ctypes
import glob
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import claude_api
import nous
import leases
import notify
import results
from delegate_config import CONFIG, TRANSIENT, ignored_activity_path, repetition, shorten_paths, worker_command

PROFILES = {
    "claude-api-opus": dict(backend="claude-api", model="claude-opus-5-5", effort="high",
                            label="Claude Opus 5.5 (high)"),
    "claude-api-sonnet": dict(backend="claude-api", model="claude-sonnet-5-5", effort="high",
                              label="Claude Sonnet 5.5 (high)"),
    "claude-api-haiku": dict(backend="claude-api", model="claude-haiku-5-5", effort="medium",
                             label="Claude Haiku 5.5 (medium)"),
    "gpt-astra-med": dict(backend="codex", model="gpt-6-astra", effort="medium",
                          label="GPT-6-Astra (medium)"),
    "gpt-astra-high": dict(backend="codex", model="gpt-6-astra", effort="high",
                           label="GPT-6-Astra (high)"),
    "gpt-astra-xhigh": dict(backend="codex", model="gpt-6-astra", effort="xhigh",
                            label="GPT-6-Astra (extra high)"),
    "gpt-sol-med": dict(backend="codex", model="gpt-6.1-sol", effort="medium",
                        label="GPT-6.1-Sol (medium)"),
    "gpt-terra-med": dict(backend="codex", model="gpt-6.1-terra", effort="medium",
                          label="GPT-6.1-Terra (medium)"),
    "gpt-terra-high": dict(backend="codex", model="gpt-6.1-terra", effort="high",
                           label="GPT-6.1-Terra (high)"),
    "kimi-k28": dict(backend="kimi", model="kimi-code/kimi-for-coding", effort="max",
                     label="Kimi K2.8 Preview"),
    "kimi-k3": dict(backend="kimi", model="kimi-code/k3", effort="high",
                    label="Kimi K3"),
    "kimi-k31-pro": dict(backend="kimi", model="kimi-code/k3.1-pro", effort="max",
                         label="Kimi K3.1 Pro"),
    # Nous Portal models, run by the Kimi CLI and billed to Nous credits (see nous.py)
    "nous-deepseek-pro": dict(backend="kimi", provider="nous", model="nous/deepseek-v4-pro", effort="high",
                              label="DeepSeek V4 Pro (Nous)"),
    "nous-deepseek-flash": dict(backend="kimi", provider="nous", model="nous/deepseek-v4.1-flash", effort="high",
                                label="DeepSeek V4.1 Flash (Nous)"),
    "nous-mimo-pro": dict(backend="kimi", provider="nous", model="nous/mimo-v2.6-pro", effort="default",
                          label="MiMo V2.6 Pro (Nous)"),
    "nous-glm": dict(backend="kimi", provider="nous", model="nous/glm-5.3", effort="high",
                     label="GLM 5.3 (Nous)"),
    "nous-glm-flash": dict(backend="kimi", provider="nous", model="nous/glm-5.3-flash", effort="high",
                           label="GLM 5.3 Flash (Nous)"),
    "nous-gemini-argon": dict(backend="kimi", provider="nous", model="nous/gemini-4-argon", effort="high",
                              label="Gemini 4 Argon (Nous)"),
}

HOME = Path.home()
DELEGATE_ROOT = HOME / ".claude" / "delegate"
JOBS_ROOT = Path(os.environ.get("DELEGATE_JOBS_DIR", DELEGATE_ROOT / "jobs")).resolve()
AGENTS_ROOT = Path(os.environ.get("DELEGATE_AGENTS_DIR", DELEGATE_ROOT / "agents")).resolve()
DEFAULT_WAIT_S = 540           # stays under the Bash tool's 600 s ceiling
DEFAULT_MAX_MINUTES = 180      # hard cap on one job
CMDLINE_LIMIT = 30000          # Windows allows 32767 UTF-16 units; keep a margin
POLL_S = 2
WAIT_ANY_POLL_S = 3
NOUS_SPEND_CHECK_S = 5
ASK_POLL_S = 2
TERMINAL = {"done", "failed", "timeout", "cancelled", "lost"}

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

MODE_RULES = {
    "read": ("MODE: READ-ONLY. Do not create, modify or delete any files (except your progress "
             "notes file, if you are able to write it) and do not run commands that change "
             "system state. Investigate and report only."),
    "write": ("MODE: WORKSPACE-WRITE. You may create and edit files only inside the working "
              "directory{extra} and your progress notes file. Do not modify anything else, and "
              "do not install software system-wide unless the brief says so."),
    "full": "MODE: FULL ACCESS. Use it carefully and only for what the brief asks.",
}
COMMON_RULES = ("Do not commit, push, publish or send anything unless the brief explicitly "
                "asks you to. Do not launch games or drive the desktop unless asked.")
PHASE_RULES = ("Supervision: people are supervising this job and can talk to you while you work.\n"
               "Whenever you start a new stage of the work, first run this command:\n"
               "  {command} phase --job {job} \"<3-8 words naming the stage>\"\n"
               "It names your stage on their status board (for example \"live test 2 of 5\") and "
               "prints any messages they have sent you. Read them and act on them; where a message "
               "conflicts with the brief, the message wins. Run it when you begin, at every change "
               "of stage, and at least every 30-45 minutes on a long stage.\n"
               "If you reach a decision that is not yours to make (an approval, an instruction that "
               "can be read two ways, a risk the brief did not foresee, a brief that looks wrong), "
               "do not end the job and do not guess. Ask:\n"
               "  {command} ask --job {job} \"<your question, with the options you see>\"\n"
               "It waits for the answer and prints it, so give that command the longest timeout "
               "your shell tool allows. If it prints NO ANSWER YET, first do any work that does not "
               "depend on the answer, then run the same ask command again.\n"
               "If one of these commands fails with an error, carry on without it; do not "
               "investigate.")
# Codex runs shell commands through a script tool that hands control back to the model every
# 30-60 seconds while a command is still running, and every hand-back is a full model request.
# Told how, the model keeps one script waiting instead (found by reading Codex 0.167 session logs).
CODEX_WAIT_RULES = ("How to run the commands above that wait (ask, and lease if you were given one): each "
                    "blocks for up to {minutes} minutes. This is an expected long wait and an exception to "
                    "the general advice to avoid waits over 60 seconds. Run it inside a single `exec` call "
                    "whose first line is `// @exec: {{\"yield_time_ms\": 600000}}`, start it with "
                    "`tools.exec_command({{cmd, yield_time_ms: 30000}})`, and while the result has a "
                    "`session_id` and no `exit_code`, keep calling "
                    "`await tools.write_stdin({{session_id, chars: \"\", yield_time_ms: 300000}})` in a loop "
                    "inside that same script. Do not return to check on it, call `wait`, or write status "
                    "messages while it runs. If your tools are not shaped like this, just give the command "
                    "the longest timeout they allow.")
PROGRESS_RULES = ("Progress notes file: {path}\n"
                  "Append a short timestamped note there after each significant finding or "
                  "decision: what you learned and what is next.{read_note}")
REPORT_RULES = ("When you finish, reply with a concise final report: what you did or found "
                "(with evidence such as file paths, offsets, command output), the exact files "
                "you created or changed, how to verify it, and what remains uncertain.")


# ---------------------------------------------------------------- helpers

def out(text=""):
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except OSError:  # reader went away (e.g. piped into `head`); Windows reports EINVAL
        sys.stdout = open(os.devnull, "w")


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(20):  # a reader may hold the target open for a moment on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def one_line(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def job_dir(job_id):
    d = JOBS_ROOT / job_id
    if not d.is_dir():
        sys.exit(f"error: no such job: {job_id} (look in {JOBS_ROOT})")
    return d


def agent_paths(profile, job_id):
    base = AGENTS_ROOT / profile
    return dict(base=base,
                progress=base / "progress" / f"{job_id}.md",
                activity=base / "activity" / f"{job_id}.log",
                report=base / "reports" / f"{job_id}.md",
                latest=base / "latest-report.md")


def job_title(meta, job_id):
    """What people call the job: the name it was started with, else its brief's file name, else its folder."""
    if meta.get("name"):
        return meta["name"]
    src = Path(meta.get("prompt_source") or "")
    if src.name and src.parent.name == "inbox":
        return re.sub(r"-[a-z0-9]{4}$", "", src.stem)
    return Path(meta.get("cwd") or "").name or job_id


def journal(jd, kind, text=None):
    """Append one event to the job's journal: started, phase, then the final state. Never raises."""
    try:
        with open(Path(jd) / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(t=time.time(), kind=kind, text=text)) + "\n")
    except OSError:
        pass


def read_journal(jd):
    return [e for e in iter_events(Path(jd) / "journal.jsonl") if isinstance(e, dict)]


def read_inbox(jd):
    """Messages for the worker: `tell` (queued until its next stage change) and `answer` entries."""
    return [e for e in iter_events(Path(jd) / "inbox.jsonl") if isinstance(e, dict)]


def send(jd, kind, text, **extra):
    with open(Path(jd) / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(t=time.time(), kind=kind, text=text, **extra)) + "\n")


def open_question(jd, events=None):
    """The worker's latest question, if nobody has answered it yet."""
    events = read_journal(jd) if events is None else events
    asked = [e for e in events if e.get("kind") == "asked"]
    if not asked:
        return None
    last = asked[-1]
    if any(e.get("kind") == "answered" and e.get("t", 0) >= last.get("t", 0) for e in events):
        return None
    return last


def phase_info(jd):
    """What the worker last told the bridge: its stage (and when it started, and how many it has
    reported), its open question, and how many queued messages it has not picked up yet."""
    events = read_journal(jd)
    phases = [e for e in events if e.get("kind") == "phase"]
    last = phases[-1] if phases else {}
    question = open_question(jd, events) or {}
    # the worker reads its messages whenever it names a stage or runs `check`
    read_at = max((e.get("t", 0) for e in events if e.get("kind") in ("phase", "checked")), default=0)
    queued = sum(1 for m in read_inbox(jd) if m.get("kind") == "tell" and m.get("t", 0) > read_at)
    return dict(phase=last.get("text"), phase_since=last.get("t"), phases=len(phases),
                question=question.get("text"), question_since=question.get("t"), queued=queued)


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def process_start_time(pid):
    """Creation time of a live process as a Unix timestamp, or None if unknown."""
    if not pid:
        return None
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        ft = [ctypes.c_ulonglong() for _ in range(4)]
        ok = k32.GetProcessTimes(h, *[ctypes.byref(f) for f in ft])
        k32.CloseHandle(h)
        if not ok:
            return None
        return ft[0].value / 1e7 - 11644473600  # FILETIME (100 ns since 1601) -> Unix seconds
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def runner_alive(st):
    """The job's runner is still the process that wrote this status (a reused PID is not)."""
    pid = st.get("runner_pid")
    if not pid_alive(pid):
        return False
    born = process_start_time(pid)
    started = st.get("started")
    return born is None or started is None or born <= started + 5


def kill_tree(pid):
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(os.getpgid(int(pid)), 9)
        except OSError:
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass


def fmt_secs(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def cmdline_units(cmd):
    return len(subprocess.list2cmdline(cmd).encode("utf-16-le")) // 2 + 1


def find_codex():
    env = os.environ.get("DELEGATE_CODEX")
    if env and Path(env).exists():
        return env
    local = os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local"))
    cands = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
    if cands:
        return max(cands, key=os.path.getmtime)
    found = shutil.which("codex")
    if found:
        return found
    raise RuntimeError("codex CLI not found (set DELEGATE_CODEX to codex.exe)")


def find_kimi():
    """Return argv prefix for the Kimi CLI. Calls node directly so multi-line
    prompts are not mangled by the npm .cmd shim."""
    env = os.environ.get("DELEGATE_KIMI_MAIN")
    appdata = os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming"))
    main = Path(env) if env else Path(appdata) / "npm" / "node_modules" / "@moonshot-ai" / "kimi-code" / "dist" / "main.mjs"
    node = shutil.which("node")
    if main.exists() and node:
        return [node, str(main)]
    found = shutil.which("kimi")
    if found:
        return [found]
    raise RuntimeError("kimi CLI not found (npm install -g @moonshot-ai/kimi-code)")


# ---------------------------------------------------------------- command building


def codex_tool_overrides(path=None):
    """-c overrides that switch off every MCP server and plugin in the user's Codex config
    except the allowlisted ones (config keys codex_mcp_allow / codex_plugins_allow)."""
    path = Path(path) if path else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError, ImportError):
        return []
    # ChatGPT account connectors (GitHub, sites, ...) arrive as the built-in codex_apps server.
    out = [] if "codex_apps" in CONFIG["codex_mcp_allow"] else ["-c", "features.apps=false"]
    for kind, allow in (("mcp_servers", CONFIG["codex_mcp_allow"]), ("plugins", CONFIG["codex_plugins_allow"])):
        for name in sorted((data.get(kind) or {})):
            # codex -c splits the key path on dots and does not unquote, so names are
            # passed bare; a name containing a dot cannot be addressed this way.
            if name not in allow and "." not in name:
                out += ["-c", f"{kind}.{name}.enabled=false"]
    return out


def build_codex_cmd(meta, jd):
    exe = find_codex()
    mode = meta["mode"]
    cfg = ["-c", f'model_reasoning_effort="{meta["effort"]}"',
           "-c", 'approval_policy="never"']
    if CONFIG["codex_exec_yield_ms"]:
        # how long one Codex script may run before it hands back to the model on its own
        cfg += ["-c", f'features.code_mode.default_exec_yield_time_ms={CONFIG["codex_exec_yield_ms"]}']
    cfg += codex_tool_overrides()
    flags = []
    if mode == "full":
        flags.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cfg += ["-c", f'sandbox_mode="{"read-only" if mode == "read" else "workspace-write"}"']
        if mode == "write":
            roots = meta["add_dirs"] + [str(Path(meta["progress_file"]).parent)]
            cfg += ["-c", "sandbox_workspace_write.network_access=true",
                    "-c", f"sandbox_workspace_write.writable_roots=[{', '.join(json.dumps(r) for r in roots)}]"]
    common = ["-m", meta["model"], *cfg, *flags, "--skip-git-repo-check", "--json",
              "-o", str(jd / "result.md")]
    if meta.get("resume_session"):
        return [exe, "exec", "resume", meta["resume_session"], *common, "-"]
    return [exe, "exec", *common, "-C", meta["cwd"], "-"]


def build_kimi_cmd(meta, jd, prompt_text):
    argv = find_kimi()
    tail = []
    for d in meta["add_dirs"]:
        tail += ["--add-dir", d]
    if meta.get("resume_session"):
        tail += ["-S", meta["resume_session"]]
    head = [*argv, "-m", meta["model"], "--output-format", "stream-json", "-p"]
    cmd = [*head, prompt_text, *tail]
    if cmdline_units(cmd) > CMDLINE_LIMIT:
        pointer = (f"Your full task brief is in the file {jd / 'prompt.md'}. Read the whole "
                   "file first, then carry out the task exactly as it describes.")
        cmd = [*head, pointer, *tail]
    return cmd


# ---------------------------------------------------------------- event parsing

def _kimi_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type", "text") == "text")
    return ""


KIMI_HOOK_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionResult",
    "UserPromptSubmit", "UserPromptQueued", "TurnStarted", "Stop", "StopFailure", "Interrupt",
    "SessionStart", "SessionEnd", "SessionHeartbeat", "SubagentStart", "SubagentStop",
    "TaskStarted", "PreCompact", "PostCompact", "Notification",
}


def _is_kimi_hook_message(text):
    # Kimi 2.1.1 prints hook results as assistant messages titled "<Event> hook[ blocked]".
    first, sep, _ = text.partition("\n\n")
    return bool(sep) and first.strip() in {f"{e} hook{b}" for e in KIMI_HOOK_EVENTS for b in ("", " blocked")}


def iter_events(path):
    for line in read_text(path).splitlines():
        try:
            yield json.loads(line)
        except Exception:
            continue


def parse_events(backend, path):
    session, last_msg, errors = None, None, []
    for ev in iter_events(path):
        if backend == "codex":
            if ev.get("type") == "thread.started":
                session = ev.get("thread_id") or session
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = item["text"]
            elif item.get("type") == "error" and "unrecognized configuration" not in item.get("message", ""):
                errors.append(item.get("message", ""))
            elif ev.get("type") in ("turn.failed", "error"):
                errors.append(json.dumps(ev.get("error") or ev)[:2000])
        elif backend == "claude-api":
            session = ev.get("session_id") or session
            if ev.get("type") == "result":
                last_msg = ev.get("result") or last_msg
                errors.extend(str(e) for e in ev.get("errors") or [])
        else:
            if ev.get("type") == "session.resume_hint":
                session = ev.get("session_id") or session
            elif ev.get("role") == "assistant" and not ev.get("tool_calls"):
                text = _kimi_text(ev.get("content"))
                if text.strip() and not _is_kimi_hook_message(text):
                    last_msg = text
            elif ev.get("role") == "meta" and "error" in str(ev.get("type", "")):
                errors.append(json.dumps(ev)[:2000])
    return session, last_msg, errors


def summarize_event(backend, ev):
    if backend == "claude-api":
        return claude_api.summarize(ev)
    if backend == "codex":
        t = ev.get("type")
        if t == "thread.started":
            return f"session {ev.get('thread_id')}"
        if t == "turn.completed":
            u = ev.get("usage") or {}
            return f"turn completed (input {u.get('input_tokens')} / output {u.get('output_tokens')} tokens)"
        if t in ("turn.failed", "error"):
            return f"ERROR {one_line(json.dumps(ev.get('error') or ev), 400)}"
        if t != "item.completed":
            return None
        item = ev.get("item") or {}
        it = item.get("type")
        if it == "command_execution":
            return f"command (exit {item.get('exit_code')}): {one_line(item.get('command'), 300)}"
        if it == "agent_message":
            return f"message: {one_line(item.get('text'), 400)}"
        if it == "file_change":
            return "files changed: " + ", ".join(f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or [])
        if it == "web_search":
            return f"web search: {one_line(item.get('query'), 200)}"
        if it == "mcp_tool_call":
            return f"mcp tool: {item.get('server')}.{item.get('tool')}"
        if it == "error":
            msg = item.get("message", "")
            return None if "unrecognized configuration" in msg else f"error: {one_line(msg, 300)}"
        if it in ("reasoning", None):
            return None
        return it
    if ev.get("tool_calls"):
        return "; ".join(f"tool {tc.get('function', {}).get('name')}: "
                         f"{one_line(tc.get('function', {}).get('arguments'), 250)}" for tc in ev["tool_calls"])
    role = ev.get("role")
    if role == "assistant":
        text = _kimi_text(ev.get("content"))
        if not text.strip():
            return None
        return f"{'hook' if _is_kimi_hook_message(text) else 'message'}: {one_line(text, 400)}"
    if role == "meta":
        if ev.get("type") == "session.resume_hint":
            return f"session {ev.get('session_id')}"
        return None if ev.get("type") == "system.version" else f"meta: {ev.get('type')}"
    return None


class ActivityTail:
    """Turns new lines of events.jsonl into timestamped lines in the activity log."""

    def __init__(self, meta, jd):
        self.backend = meta["backend"]
        self.jd = jd
        self.src = jd / "events.jsonl"
        self.dst = Path(meta["activity_file"])
        self.offset = 0
        self.session = None
        self.wanted = set()     # resources this (sandboxed) worker is waiting to be granted

    def worker_said(self, text, starting=False, finished=False):
        verb, body = worker_command(text)
        if verb == "ask":
            # the worker repeats the same command while it waits: that is still one question
            current = open_question(self.jd) or {}
            if starting and body and current.get("text") != body:
                journal(self.jd, "asked", body)
                meta = load_json(self.jd / "meta.json", {}) or {}
                notify.question(job_title(meta, self.jd.name), meta.get("label", "?"), body)
        elif verb == "phase" and finished and body:
            journal(self.jd, "phase", body)
        elif verb == "check" and finished:
            journal(self.jd, "checked")
        elif self.backend == "codex" and body and verb in ("lease", "release"):
            # A sandboxed worker cannot write the lease file, so its runner takes and returns
            # slots for it; the worker's own command just waits to see the grant. Unsandboxed
            # workers do both themselves.
            if verb == "lease" and starting:
                self.wanted.add(body)
                self.grant()
            elif verb == "lease" and finished:
                self.wanted.discard(body)       # it stopped waiting; it asks again if it still wants it
            elif verb == "release" and finished:
                self.wanted.discard(body)
                leases.release(DELEGATE_ROOT, self.jd.name, body)

    def grant(self):
        """Try to take every slot the worker is waiting for; leave the reason where its command can read it."""
        waiting = {}
        for resource in sorted(self.wanted):
            try:
                got = take_slot(resource, self.jd.name)
            except OSError as e:
                got = dict(ok=False, reason=f"lease file unavailable: {e}")
            if got["ok"] or got.get("unknown"):
                self.wanted.discard(resource)
            if not got["ok"]:
                waiting[resource] = got["reason"]
        save_json(self.jd / "lease.wait", waiting)

    def pump(self):
        try:
            with open(self.src, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        lines = []
        stamp = time.strftime("%H:%M:%S")
        for raw in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if self.backend == "codex" and ev.get("type") == "thread.started":
                self.session = ev.get("thread_id")
            # A worker talks to the bridge by running `delegate.py phase|ask|check ...`. Only its own
            # commands count, never text it merely wrote. Codex announces a command when it starts
            # (a question must be seen while the worker is still waiting on it) and again when it
            # ends; the other backends log each tool call once, as it starts.
            item = ev.get("item") or {}
            if (self.backend == "codex" and ev.get("type") == "item.started"
                    and item.get("type") == "command_execution"):
                self.worker_said(item.get("command"), starting=True)
            summary = summarize_event(self.backend, ev)
            if summary:
                lines.append(f"{stamp}  {summary}")
                if summary.startswith(("command (", "tool ")):
                    self.worker_said(summary, starting=self.backend != "codex", finished=True)
        if lines:
            with open(self.dst, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


CHECKPOINT_TEXT = ("TIME LIMIT: this job will be stopped in about {minutes} minutes. Do not start new work. "
                   "Bring what you have to a safe state now (save files; commit if the brief allows commits), "
                   "release any slots, and write your final report with the result block, listing what is "
                   "unfinished under open. A clean partial result is worth more than being cut off mid-step.")
YIELD_TEXT = ("YIELD NOW: the owner of this machine has started {programs}. Stop using {resource} at once: close "
              "what you launched with it, then release it. Do other work, and lease it again later.")


class Minder:
    """What a runner does for its job besides reading its events: the time limit, with a warning
    to wrap up before it, and telling the worker to give a slot back when the owner starts one of
    that resource's `yield_to` programs."""

    def __init__(self, jd, meta, now=None):
        now = time.time() if now is None else now
        limit = meta["max_minutes"] * 60
        self.jd = jd
        self.deadline = now + limit
        self.window = min(CONFIG["checkpoint_minutes"] * 60, limit / 4)
        self.warned = None          # when the wrap-up warning was sent
        self.extended = False
        self.yielded = set()
        self.next_yield_check = 0

    def _read_since(self, t):
        return any(e.get("kind") in ("phase", "checked") and e.get("t", 0) >= t for e in read_journal(self.jd))

    def tick(self, waiting=False, now=None):
        """Call every POLL_S seconds. Returns "timeout" when the job must be stopped."""
        now = time.time() if now is None else now
        if waiting:
            self.deadline += POLL_S     # time spent waiting for an answer or a slot is not the worker's
        if self.window and self.warned is None and now >= self.deadline - self.window:
            self.warned = now
            send(self.jd, "tell", CHECKPOINT_TEXT.format(minutes=max(1, round((self.deadline - now) / 60))))
            journal(self.jd, "checkpoint")
        if now > self.deadline:
            # a worker reads messages when it changes stage; if the warning has not reached it,
            # give it one more window instead of cutting it off unwarned
            if self.warned is not None and not self.extended and not self._read_since(self.warned):
                self.extended = True
                self.deadline = now + self.window
                journal(self.jd, "extended")
                return None
            return "timeout"
        if now >= self.next_yield_check:
            self.next_yield_check = now + 10
            self.check_yield()
        return None

    def check_yield(self):
        for resource, spec in CONFIG["resources"].items():
            if not spec.get("yield_to"):
                continue
            try:
                busy = owner_programs(spec["yield_to"]) if leases.held(DELEGATE_ROOT, resource, self.jd.name) else []
            except OSError:
                continue
            if not busy:
                self.yielded.discard(resource)
            elif resource not in self.yielded:
                self.yielded.add(resource)
                send(self.jd, "tell", YIELD_TEXT.format(programs=", ".join(busy), resource=resource))
                journal(self.jd, "yield", resource)


# ---------------------------------------------------------------- runner (detached)

def cancel_requested(jd):
    return (jd / "cancel.flag").exists()


def wait_bounded(proc, secs=30):
    try:
        return proc.wait(timeout=secs)
    except subprocess.TimeoutExpired:
        return None


def finalize(jd, meta, status, state, rc, extra_errors=()):
    """Extract result and session, write the per-agent report, publish the final status."""
    session, last_msg, errors = parse_events(meta["backend"], jd / "events.jsonl")
    result_path = jd / "result.md"
    if meta["backend"] == "kimi" or not read_text(result_path).strip():
        if last_msg:
            result_path.write_text(last_msg, encoding="utf-8")
    result = read_text(result_path).strip()
    if state is None:
        state = "done" if rc == 0 and result else "failed"
    # the worker's result block, with what could be checked of its claims
    try:
        checked = results.digest(result, status.get("started"))
    except Exception as e:
        checked = dict(data=None, error=f"result check failed: {e!r}", findings=[], verdict=None, flags=0)
    if CONFIG["guard_enabled"]:
        guarded = results.guard(meta.get("mode"), read_text(meta.get("activity_file", "")).splitlines(), CONFIG["guard_patterns"])
        checked["findings"] = list(checked["findings"]) + guarded
        checked["flags"] += len(guarded)
    save_json(jd / "result.json", checked)
    status.update(verdict=checked["verdict"], flags=checked["flags"])
    errors = list(extra_errors) + errors
    status.update(state=state, exit_code=rc, finished=time.time(),
                  session_id=session or status.get("session_id") or meta.get("resume_session"),
                  errors=errors[-5:])
    if meta.get("provider") == "nous" and meta.get("kimi_home"):
        try:
            nous_spend(meta, status)
            nous.append_ledger(DELEGATE_ROOT, jd, meta, status)
        except Exception as e:
            errors.append(f"could not record Nous Portal spend: {e!r}")
            status["errors"] = errors[-5:]
    if meta["backend"] == "claude-api":
        try:
            claude_api.append_ledger(DELEGATE_ROOT, jd, meta, status)
        except Exception:
            status["state"] = state = "failed"
            errors.append("could not append Claude API spend ledger; job status retains usage/cost")
            status["errors"] = errors[-5:]
    try:
        ap = agent_paths(meta["profile"], jd.name)
        elapsed = status["finished"] - status.get("started", meta["created"])
        lines = [f"# {meta['profile']} report: {jd.name}", "",
                 f"- Model: {meta['label']} ({meta['backend']}:{meta['model']}, effort {meta['effort']})",
                 f"- Status: {state} (exit {rc}) after {fmt_secs(elapsed)}",
                 f"- Mode: {meta['mode']}",
                 f"- Working directory: {meta['cwd']}",
                 f"- Session: {status.get('session_id') or 'none'}",
                 f"- Progress notes: {meta['progress_file']}",
                 f"- Activity log: {meta['activity_file']}",
                 f"- Job folder: {jd}", ""]
        if meta["backend"] == "claude-api":
            lines += [f"- Approval: {meta.get('approved')}",
                      f"- API cost (USD): {status.get('total_cost_usd', 'unknown')}", ""]
        if errors:
            lines += ["## Errors", ""] + [f"    {one_line(e, 1500)}" for e in errors[-5:]] + [""]
        lines += ["## Result", "", result or "(no final message)", ""]
        text = "\n".join(lines)
        ap["report"].write_text(text, encoding="utf-8")
        ap["latest"].write_text(text, encoding="utf-8")
    except Exception as e:
        status.setdefault("errors", []).append(f"could not write agent report: {e!r}")
    try:
        leases.release(DELEGATE_ROOT, jd.name)     # a finished job holds nothing
    except OSError:
        pass
    try:
        resumed = auto_resume(jd, meta, status, errors)
    except Exception as e:
        resumed = None
        status.setdefault("errors", []).append(f"could not auto-resume: {e!r}")
    journal(jd, status["state"])
    if resumed:
        status["resumed_by"] = resumed
        save_json(jd / "status.json", status)
        return
    notify.outcome(job_title(meta, jd.name), meta.get("label", "?"), status["state"], status.get("verdict"),
                   status.get("flags") or 0, ((checked.get("data") or {}).get("summary") or ""),
                   fmt_secs(status["finished"] - status.get("started", meta.get("created", status["finished"]))))
    save_json(jd / "status.json", status)
    try:
        start_ready()       # a job that ended may be what a queued job was waiting for
    except Exception:
        pass


def nous_spend(meta, status, jd=None, meter=None):
    """Measure a Nous job's spend so far from its Kimi session files; record it in the status."""
    seen = meter.read() if meter is not None else nous.usage(nous.wire_files(meta["kimi_home"]))
    tokens = nous.minus(seen, status.get("nous_baseline"))
    usd = nous.cost(tokens, meta.get("prices") or nous.MODELS[meta["model"]]["prices"])
    status.update(total_cost_usd=usd, usage=dict(input_tokens=tokens["input"] + tokens["cache_write"],
                                                 cached_input_tokens=tokens["cached"], output_tokens=tokens["output"],
                                                 calls=tokens["calls"]))
    if jd is not None:
        save_json(Path(jd) / "status.json", status)
    return usd


BG_TAIL_LINES = 40
BG_LINES_PER_TICK = 3


def _slot_word(name):
    return re.sub(r"\W", "_", name).upper()


def run_shell(jd, meta, status):
    """A `bg` job: one long command run outside the caller's session. It takes the slots it was
    given first (and keeps them for the whole run), writes all output to output.log, shows the
    latest output lines as activity, and ends with the exit code and the tail of the log as its
    result. Returns (state, exit code, errors)."""
    env = os.environ.copy()
    env["DELEGATE_JOB"] = jd.name
    command = meta["command"]
    for res in sorted(meta.get("leases") or []):
        journal(jd, "phase", f"waiting for slot: {res}")
        while True:
            got = take_slot(res, jd.name)
            if got["ok"]:
                break
            if got.get("unknown"):
                return "failed", None, [got["reason"]]
            if cancel_requested(jd):
                return "cancelled", None, []
            if time.time() - status["started"] > meta.get("max_minutes", DEFAULT_MAX_MINUTES) * 60:
                return "timeout", None, [f"never got a slot on {res}: {got['reason']}"]
            time.sleep(POLL_S)
        value = str(got.get("value") or got["slot"])
        env["DELEGATE_SLOT_" + _slot_word(res)] = value
        fill = lambda s: s.replace("{" + res + "}", value)
        command = fill(command) if isinstance(command, str) else [fill(c) for c in command]
    (jd / "command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
    journal(jd, "phase", "running the command")
    log_path = jd / "output.log"
    started = time.time()
    deadline = started + meta.get("max_minutes", DEFAULT_MAX_MINUTES) * 60
    popen_kw = dict(creationflags=CREATE_NO_WINDOW) if os.name == "nt" else dict(start_new_session=True)
    state, rc, extra, offset = None, None, [], 0

    def pump():
        """Copy the newest complete output lines into the activity log, a few per tick."""
        nonlocal offset
        try:
            with open(log_path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return
        cut = data.rfind(b"\n") + 1
        if not cut:
            return
        offset += cut
        lines = [l.strip() for l in data[:cut].decode("utf-8", errors="replace").splitlines() if l.strip()]
        try:
            with open(meta["activity_file"], "a", encoding="utf-8") as act:
                for line in lines[-BG_LINES_PER_TICK:]:
                    act.write(f"{time.strftime('%H:%M:%S')}  message: {one_line(line, 400)}\n")
        except OSError:
            pass

    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(command, cwd=meta["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, env=env, **popen_kw)
        except OSError as e:
            return "failed", None, [f"could not start the command: {e}"]
        status["child_pid"] = proc.pid
        save_json(jd / "status.json", status)
        while True:
            try:
                rc = proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            pump()
            if cancel_requested(jd):
                kill_tree(proc.pid)
                rc, state = wait_bounded(proc), "cancelled"
                break
            if time.time() > deadline:
                kill_tree(proc.pid)
                rc, state = wait_bounded(proc), "timeout"
                extra.append(f"stopped at the {meta.get('max_minutes')}-minute limit; raise it with --max-minutes")
                break
    pump()
    lines = read_text(log_path).splitlines()
    shown = command if isinstance(command, str) else subprocess.list2cmdline(command)
    result = [f"Command: `{one_line(shown, 600)}`",
              f"Exit code: {rc} after {fmt_secs(time.time() - started)}" + (f" ({state})" if state else ""),
              f"Output log: {log_path} ({len(lines)} lines)", ""]
    if lines:
        result += [f"Last {min(len(lines), BG_TAIL_LINES)} lines of output:", "```text"] + \
                  [l.replace("```", "'''")[:400] for l in lines[-BG_TAIL_LINES:]] + ["```"]
    else:
        result.append("The command printed nothing.")
    (jd / "result.md").write_text("\n".join(result) + "\n", encoding="utf-8")
    if state is None:
        state = "done" if rc == 0 else "failed"
        if rc != 0:
            extra.append(f"the command exited with code {rc}")
    return state, rc, extra


def cmd_runner(args):
    jd = Path(args.jobdir).resolve()
    meta = load_json(jd / "meta.json")
    status = {"state": "running", "started": time.time(), "runner_pid": os.getpid()}
    if cancel_requested(jd):
        finalize(jd, meta, status, "cancelled", None)
        return
    save_json(jd / "status.json", status)
    journal(jd, "started")
    state, rc, extra = None, None, []
    if meta["backend"] == "claude-api":
        try:
            if not str(meta.get("approved") or "").strip():
                raise RuntimeError("Claude API run requires --approved with the owner's specific approval note")
            state, rc, extra = claude_api.run(
                meta, jd, CONFIG, DELEGATE_ROOT, status,
                lambda: save_json(jd / "status.json", status), kill_tree, cancel_requested)
        except Exception as e:
            state, extra = "failed", [str(e)]
        finalize(jd, meta, status, state, rc, extra)
        return
    if meta["backend"] == "shell":
        try:
            state, rc, extra = run_shell(jd, meta, status)
        except Exception as e:
            state, extra = "failed", [f"runner error: {e!r}"]
        finalize(jd, meta, status, state, rc, extra)
        return
    try:
        prompt_text = read_text(jd / "prompt.md")
        env = os.environ.copy()
        if meta["backend"] == "codex":
            cmd = build_codex_cmd(meta, jd)
            stdin = open(jd / "prompt.md", "rb")
            # The Codex Windows sandbox runs shells under a restricted token, which cannot
            # launch Store app-execution aliases (WindowsApps\pwsh.exe fails with
            # CreateProcessAsUserW error 5). Without them Codex falls back to the real
            # Windows PowerShell in System32.
            if os.name == "nt" and meta["mode"] != "full":
                env["PATH"] = os.pathsep.join(
                    p for p in env.get("PATH", "").split(os.pathsep) if "windowsapps" not in p.lower())
        else:
            cmd = build_kimi_cmd(meta, jd, prompt_text)
            if meta.get("provider") == "nous":
                env["KIMI_CODE_HOME"] = str(nous.prepare_home(meta["kimi_home"]))
                env[nous.KEY_ENV] = nous.read_key(CONFIG)       # for the CLI process only
                if meta["effort"] in nous.MODELS.get(meta["model"], {}).get("efforts", []):
                    env["KIMI_MODEL_THINKING_EFFORT"] = meta["effort"]
                status["nous_baseline"] = nous.usage(nous.wire_files(meta["kimi_home"]))
            else:
                env["KIMI_MODEL_THINKING_EFFORT"] = meta["effort"]
            env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
            stdin = subprocess.DEVNULL
        (jd / "command.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
        popen_kw = dict(creationflags=CREATE_NO_WINDOW) if os.name == "nt" else dict(start_new_session=True)
        tail = ActivityTail(meta, jd)
        with open(jd / "events.jsonl", "wb") as ev, open(jd / "stderr.log", "wb") as er:
            proc = subprocess.Popen(cmd, cwd=meta["cwd"], stdin=stdin, stdout=ev, stderr=er,
                                    env=env, **popen_kw)
            status["child_pid"] = proc.pid
            save_json(jd / "status.json", status)
            minder = Minder(jd, meta)
            meter = nous.Meter(meta["kimi_home"]) if meta.get("provider") == "nous" else None
            next_spend = 0
            while True:
                try:
                    rc = proc.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                tail.pump()
                if tail.session and status.get("session_id") != tail.session:
                    status["session_id"] = tail.session
                    save_json(jd / "status.json", status)
                if cancel_requested(jd):
                    kill_tree(proc.pid)
                    rc, state = wait_bounded(proc), "cancelled"
                    break
                if tail.wanted:
                    tail.grant()
                if meter is not None and time.time() >= next_spend:
                    next_spend = time.time() + NOUS_SPEND_CHECK_S
                    if nous_spend(meta, status, jd, meter) > (meta.get("max_usd") or float("inf")):
                        kill_tree(proc.pid)
                        rc, state = wait_bounded(proc), "failed"
                        extra.append(f"stopped at the ${meta['max_usd']:g} spend cap (Nous Portal); "
                                     "raise it with --max-usd if the job needs more")
                        break
                if minder.tick(waiting=bool(open_question(jd) or tail.wanted)) == "timeout":
                    kill_tree(proc.pid)
                    rc, state = wait_bounded(proc), "timeout"
                    break
        tail.pump()
        if stdin is not subprocess.DEVNULL:
            stdin.close()
    except Exception as e:  # always leave a final status behind
        state, extra = "failed", [f"runner error: {e!r}"]
    finalize(jd, meta, status, state, rc, extra)


# ---------------------------------------------------------------- reporting

def effective_status(jd):
    st = load_json(jd / "status.json")
    if st is None:
        meta = load_json(jd / "meta.json", {})
        if time.time() - meta.get("created", 0) < 60:
            return {"state": "starting"}
        return {"state": "lost", "errors": ["runner never started"]}
    if st.get("state") == "running" and not runner_alive(st):
        time.sleep(0.5)
        st = load_json(jd / "status.json", st)  # it may have just finished
        if st.get("state") == "running":
            st["state"] = "lost"
            st.setdefault("errors", []).append("runner process exited without a final status")
    return st


def last_line(path):
    lines = [l for l in read_text(path).splitlines() if l.strip()]
    return lines[-1] if lines else ""


def report(jd):
    meta = load_json(jd / "meta.json", {})
    st = effective_status(jd)
    state = st.get("state", "?")
    started = st.get("started", meta.get("created", time.time()))
    elapsed = (st.get("finished") or time.time()) - started
    out(f"JOB: {jd.name}")
    out(f"AGENT: {meta.get('profile')} = {meta.get('label')} [{meta.get('backend')}:{meta.get('model')} effort={meta.get('effort')}] mode={meta.get('mode')}")
    out(f"CWD: {meta.get('cwd')}")
    out(f"STATUS: {state}" + (f" (exit {st['exit_code']})" if st.get("exit_code") is not None else "") + f" after {fmt_secs(elapsed)}")
    if meta.get("provider") == "nous":
        cap = f" (cap ${meta['max_usd']:g})" if meta.get("max_usd") else ""
        out(f"NOUS COST (USD): {st.get('total_cost_usd', 'unknown')}{cap}")
    if meta.get("backend") == "claude-api":
        out(f"API COST (USD): {st.get('total_cost_usd', 'unknown')}")
        out(f"APPROVED: {meta.get('approved')}")
    if meta.get("backend") == "shell":
        command = meta.get("command")
        out(f"COMMAND: {one_line(command if isinstance(command, str) else subprocess.list2cmdline(command or []), 600)}")
        out(f"OUTPUT LOG: {jd / 'output.log'}")
        if meta.get("leases"):
            out(f"HOLDS WHILE RUNNING: {', '.join(meta['leases'])}")
    if st.get("session_id"):
        out(f"SESSION: {st['session_id']}  (follow up with: run {meta.get('profile')} --resume {jd.name} --prompt-file ...)")
    out(f"PROGRESS NOTES: {meta.get('progress_file')}")
    out(f"ACTIVITY LOG: {meta.get('activity_file')}")
    out(f"FILES: {jd}")
    if read_text(jd / "detach.txt").strip() == "in-job":
        out("WARNING: the runner could not leave the caller's job object (breakaway and WMI were both refused); "
            "it may stop if the caller's process tree is killed.")
    if state == "queued":
        out(f"QUEUED BEHIND: {', '.join(meta.get('after') or [])}"
            + ("  (starts only if they end successfully)" if meta.get("after_ok") else ""))
        out(f"CANCEL WITH: python \"{Path(__file__).as_posix()}\" cancel {jd.name}")
        return
    if state not in TERMINAL:
        info = phase_info(jd)
        if info["phase"]:
            out(f"PHASE: {info['phase']} (for {fmt_secs(time.time() - info['phase_since'])})")
        if info["question"]:
            out(f"QUESTION (waiting {fmt_secs(time.time() - info['question_since'])}): {info['question']}")
            out(f"ANSWER WITH: python \"{Path(__file__).as_posix()}\" answer {jd.name} \"<reply>\"")
        if info["queued"]:
            out(f"QUEUED MESSAGES: {info['queued']} not yet read by the worker")
        out(f"LAST ACTIVITY: {last_line(meta.get('activity_file', '')) or 'none yet'}")
        out(f"NEXT: python \"{Path(__file__).as_posix()}\" wait {jd.name}")
        return
    try:    # this outcome has now been shown to a caller: `wait --any` will not report it again
        (jd / "seen.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError:
        pass
    out(f"REPORT FILE: {agent_paths(meta.get('profile', ''), jd.name)['report']}")
    checked = load_json(jd / "result.json")
    if checked is not None and not (meta.get("backend") == "shell" and checked.get("data") is None):
        for line in results.summary_lines(checked):
            out(line)
    if st.get("resumed_by"):
        out(f"AUTO-RESUMED AS: {st['resumed_by']}  (it died of a connection error; do not resume it yourself)")
    if st.get("errors"):
        out("ERRORS:")
        for e in st["errors"]:
            out(f"  {e.strip()[:1500]}")
    result = read_text(jd / "result.md").rstrip()
    if result.strip():
        out("----- RESULT -----")
        out(result)
        out("----- END RESULT -----")
    else:
        out("No final message was produced.")
        tail = read_text(jd / "stderr.log")[-3000:]
        if tail.strip():
            out("----- STDERR (tail) -----")
            out(tail.rstrip())


def wait_for(jd, wait_s):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if effective_status(jd).get("state") in TERMINAL:
            break
        time.sleep(3)
    report(jd)


# ---------------------------------------------------------------- commands

def build_prompt(args, meta, brief, preambles):
    extra = (" and these extra directories: " + ", ".join(meta["add_dirs"])) if meta["add_dirs"] else ""
    read_note = ""
    if meta["mode"] == "read" and meta["backend"] == "codex":
        read_note = (" Your sandbox is read-only, so if you cannot write that file, put your notes "
                     "in the final report instead; the bridge also logs your activity.")
    header = "\n".join([MODE_RULES[meta["mode"]].format(extra=extra), COMMON_RULES,
                        f"Working directory: {meta['cwd']}",
                        PHASE_RULES.format(command=f'python "{Path(__file__).resolve().as_posix()}"',
                                           job=meta.get("job_id", "unknown")),
                        PROGRESS_RULES.format(path=meta["progress_file"], read_note=read_note)])
    if meta["backend"] == "codex":
        header += "\n" + CODEX_WAIT_RULES.format(minutes=max(1, round(CONFIG["ask_wait_seconds"] / 60)))
    if CONFIG["resources"]:
        header += "\n" + resource_rules(meta.get("job_id", "unknown"))
    parts = [header]
    for p in preambles:
        parts.append(read_text(p).strip())
    parts.append(brief)
    if not meta.get("resume_session"):
        parts.append(REPORT_RULES + "\n\n" + results.RULES + ("\n" + capture_rules() if os.name == "nt" else ""))
    return "\n\n---\n\n".join(parts) + "\n"


def cmd_run(args):
    prof = PROFILES.get(args.profile)
    if not prof:
        sys.exit(f"error: unknown agent {args.profile!r}; choose from {', '.join(PROFILES)}")
    if prof["backend"] == "claude-api":
        if not (args.approved or "").strip():
            sys.exit("error: Claude API runs require --approved \"<who/when/why>\" for this specific run")
        try:
            claude_api.read_key(CONFIG)  # refuse before creating a job; runner rereads in its own process
        except RuntimeError as e:
            sys.exit(f"error: {e}")
    elif prof.get("provider") == "nous":
        if args.approved:
            sys.exit("error: --approved applies only to claude-api profiles")
        try:
            nous.read_key(CONFIG)       # refuse before creating a job; the runner rereads it
        except RuntimeError as e:
            sys.exit(f"error: {e}")
        budget, spent = CONFIG["nous_monthly_usd"], nous.month_spend(DELEGATE_ROOT)
        if budget and spent >= budget:
            sys.exit(f"error: Nous Portal spend this month is ${spent:.2f}, at or over the ${budget:g} budget "
                     "(config key nous_monthly_usd); not starting")
    elif args.approved or args.max_usd is not None:
        sys.exit("error: --approved and --max-usd apply only to claude-api and Nous profiles")
    if args.max_usd is not None and (not math.isfinite(args.max_usd) or args.max_usd <= 0):
        sys.exit("error: --max-usd must be a finite positive number")
    if args.prompt_file == "-":
        brief, source = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip(), "stdin"
    else:
        brief, source = read_text(args.prompt_file).strip(), str(Path(args.prompt_file).resolve())
    if not brief:
        sys.exit(f"error: prompt is missing or empty: {args.prompt_file}")
    for p in args.preamble:
        if not Path(p).is_file():
            sys.exit(f"error: preamble file not found: {p}")
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    if not Path(cwd).is_dir():
        sys.exit(f"error: working directory does not exist: {cwd}")
    add_dirs = [str(Path(d).resolve()) for d in args.add_dir]

    resume_session, pmeta_for_resume = None, None
    if args.resume:
        pjd = job_dir(args.resume)
        pmeta = load_json(pjd / "meta.json", {})
        if pmeta.get("backend") != prof["backend"]:
            sys.exit(f"error: job {args.resume} used backend {pmeta.get('backend')}, not {prof['backend']}")
        resume_session = (load_json(pjd / "status.json", {}) or {}).get("session_id")
        if not resume_session:
            sys.exit(f"error: job {args.resume} has no session id to resume")
        if not args.cwd:
            cwd = pmeta.get("cwd", cwd)
        pmeta_for_resume = pmeta

    after = [d.strip() for part in (getattr(args, "after", None) or []) for d in part.split(",") if d.strip()]
    try:
        after = [resolve_ref(d) for d in after]
    except ValueError as e:
        sys.exit(f"error: --after {e}")
    for dep in after:
        if dep_state(dep) is None:
            sys.exit(f"error: --after {dep}: no such job or Claude subagent (use a job id, or agent-<id> from the feed)")
    jd, meta = create_job(args.profile, prof, brief, source, cwd=cwd, mode=args.mode, add_dirs=add_dirs,
                          preambles=args.preamble, resume_job=args.resume, resume_session=resume_session,
                          max_minutes=args.max_minutes, name=getattr(args, "name", None), effort=args.effort,
                          approved=args.approved, max_usd=args.max_usd, after=after, resume_meta=pmeta_for_resume,
                          after_ok=bool(getattr(args, "after_ok", False)))
    if after:
        save_json(jd / "status.json", dict(state="queued", queued=time.time()))
        journal(jd, "queued", ", ".join(after))
        out(f"QUEUED: {jd.name} ({args.profile} = {meta['label']}) starts when "
            f"{', '.join(after)} {'has' if len(after) == 1 else 'have'} ended"
            + (" successfully" if meta["after_ok"] else ""))
        start_ready()
        if (load_json(jd / "status.json", {}) or {}).get("state") == "queued":
            report(jd)
            return
    else:
        spawn_runner(jd, meta)
        out(f"STARTED: {jd.name} ({args.profile} = {meta['label']}, effort={meta['effort']}, mode={args.mode})")
    wait_for(jd, args.wait)


def create_job(profile, prof, brief, source, *, cwd, mode, add_dirs=(), preambles=(), resume_job=None,
               resume_session=None, max_minutes=DEFAULT_MAX_MINUTES, name=None, effort=None, approved=None,
               max_usd=None, after=(), after_ok=False, auto_resumes=0, resume_meta=None, extra=None):
    """Write a job's folder (meta, prompt, empty notes) without starting it. Returns (job folder, meta)."""
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{profile}-{secrets.token_hex(2)}"
    jd = JOBS_ROOT / job_id
    jd.mkdir(parents=True)
    ap = agent_paths(profile, job_id)
    for key in ("progress", "activity", "report"):
        ap[key].parent.mkdir(parents=True, exist_ok=True)
    meta = dict(job_id=job_id, name=(name or "").strip() or None, profile=profile, **prof, mode=mode, cwd=cwd,
                add_dirs=list(add_dirs), resume_job=resume_job, resume_session=resume_session,
                created=time.time(), max_minutes=max_minutes, prompt_source=source,
                preambles=[str(Path(p).resolve()) for p in preambles],
                progress_file=str(ap["progress"]), activity_file=str(ap["activity"]),
                after=list(after), after_ok=bool(after_ok), auto_resumes=auto_resumes)
    if effort:
        meta["effort"] = effort
    if prof["backend"] == "claude-api":
        meta.update(approved=(approved or "").strip(), max_usd=max_usd)
    if prof.get("provider") == "nous":
        # a follow-up continues in the same Kimi home, where its session lives
        home = (resume_meta or {}).get("kimi_home") or str(jd / "kimi-home")
        meta.update(kimi_home=home, prices=nous.prices_for(meta["model"]),
                    max_usd=max_usd if max_usd is not None else (CONFIG["nous_max_usd_per_job"] or None))
    if extra:
        meta.update(extra)
    (jd / "prompt.md").write_text(brief + "\n" if prof["backend"] == "shell" else build_prompt(None, meta, brief, preambles),
                                  encoding="utf-8")
    ap["progress"].write_text(f"# {profile} progress notes: {job_id}\n\n", encoding="utf-8")
    ap["activity"].write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  job {job_id} started "
                              f"({meta['label']}, effort {meta['effort']}, mode {mode}, cwd {cwd})\n",
                              encoding="utf-8")
    save_json(jd / "meta.json", meta)  # must exist before the runner starts
    return jd, meta


BG_PROFILE = dict(backend="shell", model="command", effort="", label="Background command")
BG_MAX_MINUTES = 600


def _find_bash():
    """Git Bash on Windows (not the WSL launcher in System32), else whatever `bash` is."""
    found = shutil.which("bash")
    if os.name == "nt":
        if found and "system32" not in found.lower():
            return found
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LocalAppData")):
            for rel in ("Git/bin/bash.exe", "Programs/Git/bin/bash.exe"):
                if base and (Path(base) / rel).is_file():
                    return str(Path(base) / rel)
    return found


def bg_command(words, shell):
    """What the runner executes for `bg`: the words as given, or one command line handed to a shell."""
    words = list(words)
    if words[:1] == ["--"]:
        words = words[1:]
    if not words or not " ".join(words).strip():
        raise ValueError("give the command to run after --")
    if shell == "none":
        # one quoted string is a whole command line; Windows parses it, elsewhere it is split here
        if len(words) == 1 and os.name != "nt":
            import shlex
            return shlex.split(words[0])
        return words[0] if len(words) == 1 else words
    line = words[0] if len(words) == 1 else subprocess.list2cmdline(words)
    if shell == "powershell":
        return [shutil.which("pwsh") or "powershell", "-NoProfile", "-NonInteractive", "-Command", line]
    if shell == "cmd":
        return [os.environ.get("ComSpec") or "cmd.exe", "/d", "/c", line]
    bash = _find_bash()
    if not bash:
        raise ValueError("bash was not found; use --shell powershell or give the command as separate words")
    return [bash, "-lc", line]


def cmd_bg(args):
    """Hand a long command to the bridge: it runs outside the caller's session as a job of its own
    (board, feed, `wait --any`, `cancel`), so an agent need not keep a background shell and wake
    up for it."""
    try:
        command = bg_command(args.command, args.shell)
    except ValueError as e:
        sys.exit(f"error: {e}")
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    if not Path(cwd).is_dir():
        sys.exit(f"error: working directory does not exist: {cwd}")
    wanted = [r.strip() for part in args.lease or [] for r in part.split(",") if r.strip()]
    unknown = [r for r in wanted if r not in CONFIG["resources"]]
    if unknown:
        sys.exit(f"error: no resource named {', '.join(unknown)} (known: {', '.join(sorted(CONFIG['resources'])) or 'none are configured'})")
    shown = command if isinstance(command, str) else subprocess.list2cmdline(command)
    jd, meta = create_job("bg", BG_PROFILE, shown, "bg", cwd=cwd, mode="full", max_minutes=args.max_minutes,
                          name=args.name or one_line(shown, 50), extra=dict(command=command, leases=wanted))
    spawn_runner(jd, meta)
    me = f'python "{Path(__file__).as_posix()}"'
    out(f"STARTED: {jd.name} (background command" + (f", holds {', '.join(wanted)}" if wanted else "") + ")")
    out(f"OUTPUT LOG: {jd / 'output.log'}")
    out("It runs outside your session and survives it. Do NOT start a timer, sleep loop or background shell to "
        "watch it. Either:")
    out(f"  - you need its result to continue: {me} wait {jd.name} --wait 580   (repeat while it says running)")
    out(f"  - you are done otherwise: end your turn and name this job id in your report; whoever runs "
        f"`wait --any` gets one notice when it ends.")
    out(f"STOP IT WITH: {me} cancel {jd.name}")
    if args.wait:
        wait_for(jd, args.wait)


_WMI_CREATE = (
    "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
    "-Property @{ShowWindow=[uint16]0; CreateFlags=[uint32]512};"
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
    "@{CommandLine=$env:DELEGATE_SPAWN_CMD; CurrentDirectory=$env:DELEGATE_SPAWN_CWD; ProcessStartupInformation=$si};"
    "'{0} {1}' -f $r.ReturnValue, $r.ProcessId")
DETACH_TIMEOUT_S = 20


def _wmi_create(cmd, cwd):
    """Win32_Process.Create: WMI creates the process for us, hidden, in our desktop session but
    outside our job object. Returns its pid, or None."""
    env = dict(os.environ, DELEGATE_SPAWN_CMD=subprocess.list2cmdline(cmd), DELEGATE_SPAWN_CWD=str(cwd))
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WMI_CREATE], env=env,
                           capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        code, pid = p.stdout.split()[-2:]
        return int(pid) if code == "0" and int(pid) > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def spawn_via_wmi(cmd, cwd):
    """Start cmd outside our job object with our full environment. WMI starts detach.py (outside
    the job, but with the user's registered environment, not ours); it connects back over an
    authenticated named pipe, receives cmd, cwd and our environment in memory, and starts cmd as
    its own child. Returns the new pid, or None (and leaves nothing running) if that failed."""
    import threading
    from multiprocessing.connection import Listener
    key = secrets.token_bytes(32)
    address = rf"\\.\pipe\agent-relay-{secrets.token_hex(8)}"
    try:
        listener = Listener(address, family="AF_PIPE", authkey=key)
    except OSError:
        return None
    got = {}

    def accept():
        try:
            got["conn"] = listener.accept()
        except Exception:
            pass

    waiter = threading.Thread(target=accept, daemon=True)
    waiter.start()
    helper = _wmi_create([sys.executable, str(Path(__file__).resolve().with_name("detach.py")), address, key.hex()], cwd)
    if helper:
        waiter.join(DETACH_TIMEOUT_S)
    conn = got.get("conn")
    try:
        if conn is None:
            return None
        conn.send(dict(cmd=list(cmd), cwd=str(cwd), env=dict(os.environ)))
        if not conn.poll(DETACH_TIMEOUT_S):
            return None
        answer = conn.recv()
        return answer.get("pid") if isinstance(answer, dict) else None
    except (OSError, EOFError):
        return None
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        if conn is None and helper:
            kill_tree(helper)       # it never connected: do not leave it waiting


def spawn_runner(jd, meta):
    """Start the detached runner process for a job folder. On Windows it must not stay inside the
    caller's job object (Claude Code's tool processes run in one that forbids breakaway): it tries
    breakaway, then WMI, and only then starts inside the job and says so in the report."""
    runner = [sys.executable, str(Path(__file__).resolve()), "_runner", str(jd)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=meta["cwd"])
    if os.name == "nt":
        base = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            subprocess.Popen(runner, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
            detach = "breakaway"
        except OSError:
            detach = "wmi" if CONFIG["detach_via_wmi"] and spawn_via_wmi(runner, meta["cwd"]) else None
            if detach is None:
                subprocess.Popen(runner, creationflags=base, **kw)
                detach = "in-job"
    else:
        subprocess.Popen(runner, start_new_session=True, **kw)
        detach = "new-session"
    (jd / "detach.txt").write_text(detach, encoding="utf-8")


# ---------------------------------------------------------------- queue and auto-resume

_claude_states = dict(at=0, states={})


def resolve_ref(ref, records=None):
    """The id `reviewed` and `--after` work with, from what a caller copied off the board or feed:
    a relay job id, `agent-<id>`, the bare subagent id or a prefix of it, or the short label
    `claude:<type>#<last 4>` / `<type>#<last 4>` / `#<last 4>`. ValueError if it names nothing, or
    more than one subagent."""
    ref = str(ref or "").strip().strip("\"'")
    if re.fullmatch(r"[\w.-]+", ref) and (JOBS_ROOT / ref).is_dir():
        return ref
    records = _claude_records() if records is None else records
    ids = [r["id"] for r in records]
    short = re.fullmatch(r"(?:claude:)?([\w.:-]*)#(\w{2,})", ref)
    if short:
        kind, tail = short.group(1).rsplit(":", 1)[-1].casefold(), short.group(2).casefold()
        hits = [r["id"] for r in records if r["id"].casefold().endswith(tail)
                and (not kind or r["agent_type"].rsplit(":", 1)[-1].casefold() == kind)]
    elif re.fullmatch(r"(?:agent-)?[0-9a-fA-F]{4,}", ref):
        bare = ref.casefold().removeprefix("agent-")
        hits = [i for i in ids if i.casefold() == "agent-" + bare] or \
               [i for i in ids if i.casefold().startswith("agent-" + bare)]
        if not hits and ref.startswith("agent-"):
            return ref          # a full id whose transcript has aged out of the window
    elif ref.startswith("agent-") and re.fullmatch(r"[\w.-]+", ref):
        hits = [i for i in ids if i == ref] or [ref]
    else:
        hits = []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(f"{ref} matches {len(hits)} Claude subagents ({', '.join(hits[:5])}); use the full agent-<id>")
    raise ValueError(f"no such job or Claude subagent: {ref} (a relay job id, agent-<id>, or the board's <type>#<id> label)")


def dep_state(dep):
    """State of something a queued job waits for: a relay job id, or `agent-<id>` for a Claude Code
    subagent. None if there is no such thing."""
    if not re.fullmatch(r"[\w.-]+", dep or ""):
        return None
    if (JOBS_ROOT / dep).is_dir():
        return effective_status(_last_attempt(dep)).get("state")
    if dep.startswith("agent-"):
        if time.time() - _claude_states["at"] > 15:
            try:
                _claude_states.update(at=time.time(), states={r["id"]: r["state"] for r in _claude_records()})
            except Exception:
                _claude_states.update(at=time.time(), states={})
        return _claude_states["states"].get(dep)
    return None


def _last_attempt(job):
    """The job folder that speaks for this job: itself, or the job that automatically resumed it."""
    jd = JOBS_ROOT / job
    for _ in range(5):
        nxt = (load_json(jd / "status.json", {}) or {}).get("resumed_by")
        if not nxt or not (JOBS_ROOT / nxt).is_dir():
            break
        jd = JOBS_ROOT / nxt
    return jd


def dep_failed(dep):
    if (JOBS_ROOT / dep).is_dir():
        st = effective_status(_last_attempt(dep))
        return st.get("state") != "done" or st.get("verdict") in ("fail", "blocked")
    return dep_state(dep) != "done"


def start_ready():
    """Start every queued job whose dependencies have ended (oldest first, while fewer than
    queue_max_running relay jobs run). Called when a job ends, by `wait --any` and by `run`; safe to
    call at any time from any process. Returns the ids it started."""
    dirs = _recent_job_dirs(200)
    queued = [jd for jd in dirs if (load_json(jd / "status.json", {}) or {}).get("state") == "queued"]
    if not queued:
        return []
    started = []
    with leases.locked(DELEGATE_ROOT):          # one starter at a time
        running = sum(1 for jd in dirs if (load_json(jd / "status.json", {}) or {}).get("state") in ("running", "starting")
                      and effective_status(jd).get("state") not in TERMINAL)
        for jd in sorted(queued):
            st = load_json(jd / "status.json", {}) or {}
            if st.get("state") != "queued":
                continue
            meta = load_json(jd / "meta.json", {}) or {}
            deps = meta.get("after") or []
            # a dependency that has vanished (its transcript aged out) counts as ended
            if any(dep_state(d) not in TERMINAL and dep_state(d) is not None for d in deps):
                continue
            failed = [d for d in deps if dep_failed(d)] if meta.get("after_ok") else []
            if failed:
                st.update(state="cancelled", finished=time.time(),
                          errors=[f"not started: {', '.join(failed)} did not end successfully"])
                save_json(jd / "status.json", st)
                journal(jd, "cancelled", st["errors"][0])
                notify.outcome(job_title(meta, jd.name), meta.get("label", "?"), "cancelled", summary=st["errors"][0])
                continue
            if running >= CONFIG["queue_max_running"]:
                continue
            save_json(jd / "status.json", dict(state="starting", started=time.time(), queued=st.get("queued")))
            journal(jd, "released", ", ".join(deps))
            spawn_runner(jd, meta)
            running += 1
            started.append(jd.name)
    return started


_TRANSIENT = TRANSIENT
AUTO_RESUME_BRIEF = ("Your previous run of this task was cut off by a connection error, not by anything you did. "
                     "Continue the task from where you stopped. First check the state your work is in (files, "
                     "commits, running processes you started), do not redo finished work, and finish with the "
                     "final report the original brief asked for.")


def auto_resume(jd, meta, status, errors):
    """Resume a job that died of a connection error, once, as a new job on the same session.
    Returns the new job's id, or None. Claude API jobs are never resumed this way: each of those
    runs needs the owner's own approval."""
    if (status.get("state") != "failed" or meta.get("backend") == "claude-api" or not status.get("session_id")
            or meta.get("auto_resumes", 0) >= CONFIG["auto_resume"] or cancel_requested(jd)):
        return None
    evidence = " ".join(errors) + " " + read_text(jd / "stderr.log")[-4000:]
    if not _TRANSIENT.search(evidence):
        return None
    prof = {k: meta[k] for k in ("backend", "model", "effort", "label", "provider") if k in meta}
    new, new_meta = create_job(meta["profile"], prof, AUTO_RESUME_BRIEF, "auto-resume", cwd=meta["cwd"], mode=meta["mode"],
                               add_dirs=meta.get("add_dirs") or [], resume_job=jd.name, resume_session=status["session_id"],
                               max_minutes=meta.get("max_minutes", DEFAULT_MAX_MINUTES),
                               name=job_title(meta, jd.name), auto_resumes=meta.get("auto_resumes", 0) + 1,
                               resume_meta=meta)
    spawn_runner(new, new_meta)
    journal(jd, "resumed", new.name)
    return new.name


def _recent_job_dirs(limit=60):
    if not JOBS_ROOT.is_dir():
        return []
    return sorted((p for p in JOBS_ROOT.iterdir() if p.is_dir()), reverse=True)[:limit]


NATIVE_SEEN = "native-seen.json"
NATIVE_SCAN_S = 15


def native_deaths(records, now=None):
    """Claude Code subagents that died of a connection error and that `wait --any` has not
    reported yet. The first time this runs on a machine, earlier deaths count as history."""
    now = time.time() if now is None else now
    path = DELEGATE_ROOT / NATIVE_SEEN
    died = [r for r in records if r.get("died") and now - (r.get("finished") or now) <= 12 * 3600]
    seen = load_json(path)
    if not isinstance(seen, dict):
        mark_native_seen(died)
        return []
    reviewed = load_reviewed()
    return [r for r in died if (r.get("finished") or 0) > (seen.get(r["id"]) or 0) and r["id"] not in reviewed]


def mark_native_seen(records):
    path = DELEGATE_ROOT / NATIVE_SEEN
    with leases.locked(DELEGATE_ROOT):
        seen = load_json(path)
        seen = seen if isinstance(seen, dict) else {}
        for r in records:
            seen[r["id"]] = r.get("finished") or time.time()
        if len(seen) > 500:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1] or 0)[-500:])
        save_json(path, seen)


def wait_any(wait_s):
    """Return when any job has news nobody has been shown yet: an outcome (done, failed, timeout,
    cancelled, lost), a question from a worker, or a Claude Code subagent that died of a
    connection error; or when nothing is left running. One background call of this is the
    caller's only wake-up: each piece of news is reported exactly once."""
    deadline = time.time() + wait_s
    watched = set()
    reader, natives, died, next_scan = None, [], [], 0
    if CONFIG["claude_agents_enabled"]:
        import claude_agents
        reader = claude_agents.ClaudeAgents()
    while True:
        outcomes, questions, running = [], [], []
        if reader is not None and time.time() >= next_scan:
            next_scan = time.time() + NATIVE_SCAN_S
            try:
                records = reader.scan()
                natives = [r for r in records if r["state"] == "running"]
                died = native_deaths(records)
            except Exception:
                natives, died = [], []
        for jd in _recent_job_dirs():
            if (jd / "seen.flag").exists():
                continue
            if effective_status(jd).get("state") in TERMINAL:
                # jobs from before the journal existed are history, not news
                if jd.name in watched or (jd / "journal.jsonl").exists():
                    outcomes.append(jd)
            else:
                running.append(jd)
                watched.add(jd.name)
                q = open_question(jd)
                if q and read_text(jd / "asked.seen").strip() != repr(q.get("t")):
                    questions.append((jd, q))
        if outcomes or questions or died or not (running or natives) or time.time() >= deadline:
            break
        try:
            start_ready()
        except OSError:
            pass
        time.sleep(WAIT_ANY_POLL_S)
    for rec in died:
        out(f"DIED: {rec['id']} ({rec['agent_type'].rsplit(':', 1)[-1]}) \"{rec['label']}\" was cut off by a "
            f"connection error after {fmt_secs((rec.get('finished') or time.time()) - rec['started'])}; its task is not finished.")
        out(f"  {rec['resume']}")
        out(f"  If it dies again, do not resume it a second time: tell the owner. Once handled: "
            f"python \"{Path(__file__).as_posix()}\" reviewed {rec['id']}")
        out()
    if died:
        mark_native_seen(died)
    for jd in sorted(outcomes):
        out(f"OUTCOME: {jd.name} {effective_status(jd).get('state')}")
        report(jd)
        out()
    for jd, q in questions:
        meta = load_json(jd / "meta.json", {}) or {}
        out(f"QUESTION: {jd.name} ({meta.get('label', '?')}) is waiting for an answer:")
        out(f"  {q.get('text')}")
        out(f"ANSWER WITH: python \"{Path(__file__).as_posix()}\" answer {jd.name} \"<reply>\"")
        out()
        try:
            (jd / "asked.seen").write_text(repr(q.get("t")), encoding="utf-8")
        except OSError:
            pass
    if running or natives:
        names = [jd.name for jd in sorted(running)] + [r["id"] for r in natives]
        out(f"STILL RUNNING: {len(names)} ({', '.join(names)})")
        out(f"NEXT: python \"{Path(__file__).as_posix()}\" wait --any --wait 43200   (one background call)")
    else:
        out("NOTHING RUNNING")


def cmd_wait(args):
    if getattr(args, "any", False):
        wait_any(args.wait)
    elif not args.job:
        sys.exit("error: give a job id, or --any to wait for whichever job ends first")
    else:
        wait_for(job_dir(args.job), args.wait)


def _worker_job(job):
    """The job folder for a worker-side command, or None. These commands must never fail the
    worker's task, so an unknown job id is not an error."""
    jd = JOBS_ROOT / job if job and re.fullmatch(r"[\w.-]+", job) else None
    return jd if jd is not None and jd.is_dir() else None


def _message_line(m):
    stamp = time.strftime("%H:%M", time.localtime(m.get("t", 0)))
    if m.get("kind") == "answer":
        return f"  [{stamp}] ANSWER to your question \"{one_line(m.get('question'), 80)}\": {m.get('text')}"
    return f"  [{stamp}] {m.get('text')}"


def deliver(jd):
    """Print the supervisor's messages for a worker. Where the worker can write beside its
    progress notes, only the new ones are shown; in a read-only sandbox it gets all of them."""
    messages = read_inbox(jd)
    if not messages:
        out("No messages from your supervisor.")
        return
    meta = load_json(jd / "meta.json", {}) or {}
    cursor = Path(meta.get("progress_file") or jd / "progress.md").with_suffix(".cursor")
    try:
        shown = int(read_text(cursor).strip() or 0)
    except ValueError:
        shown = 0
    try:
        cursor.write_text(str(len(messages)), encoding="utf-8")
    except OSError:
        shown = None
    if shown is None:
        out("MESSAGES FROM YOUR SUPERVISOR (all so far, oldest first; you may have seen the earlier "
            "ones). Act on them; where one conflicts with the brief, the message wins:")
    elif shown >= len(messages):
        out("No new messages from your supervisor.")
        return
    else:
        out("NEW MESSAGES FROM YOUR SUPERVISOR. Act on them; where one conflicts with the brief, "
            "the message wins:")
    for m in messages[shown or 0:]:
        out(_message_line(m))


def cmd_phase(args):
    # The stage name itself needs no handling here: the runner (or the feed, for Claude Code
    # subagents) reads it out of the worker's own command stream.
    out("ok" if " ".join(args.text).strip() else "usage: delegate.py phase \"<stage name>\"")
    jd = _worker_job(getattr(args, "job", None))
    if jd is not None:
        deliver(jd)


def cmd_check(args):
    jd = _worker_job(args.job)
    if jd is None:
        out("No messages from your supervisor.")
    else:
        deliver(jd)


def _question_key(text):
    return re.sub(r"\W+", "", str(text or "")).casefold()[:60]


def answer_for(jd, question, since):
    """The supervisor's answer to this question: one that arrived while this command waited, or
    a recent one to the same question from an earlier wait that ran out of time."""
    for m in reversed(read_inbox(jd)):
        if m.get("kind") != "answer":
            continue
        if m.get("t", 0) >= since - 1:
            return m
        if m.get("t", 0) >= since - 3600 and _question_key(m.get("question")) == _question_key(question):
            return m
        return None
    return None


def cmd_ask(args):
    """Worker side of a question: wait for the supervisor's answer and print it. The runner sees
    this command start in the worker's event stream and puts the question on the board."""
    question = " ".join(" ".join(args.text).split())
    jd = _worker_job(args.job)
    if jd is None or not question:
        out("ask: unknown job or empty question, so nobody can answer. Use your best judgement, "
            "say in your final report what you assumed, and carry on.")
        return
    since = time.time()
    wait_s = CONFIG["ask_wait_seconds"] if args.wait is None else args.wait
    while True:
        answer = answer_for(jd, question, since)
        if answer:
            out(f"ANSWER FROM YOUR SUPERVISOR: {answer.get('text')}")
            return
        if time.time() - since >= wait_s:
            break
        time.sleep(min(ASK_POLL_S, max(0.05, wait_s)))
    out(f"NO ANSWER YET after {fmt_secs(time.time() - since)}. Your question is on the supervisor's "
        "board. Do any work that does not depend on the answer, then run the exact same ask command "
        "again. Do not guess and do not end the job over this.")


def job_running(job_id):
    jd = JOBS_ROOT / str(job_id)
    return jd.is_dir() and effective_status(jd).get("state") not in TERMINAL


_owner_programs = dict(at=0, list=[])


def owner_programs(names):
    """Running programs from a resource's `yield_to` list that are not an agent's own, as text."""
    if not names:
        return []
    import feed
    if time.time() - _owner_programs["at"] > 10:
        _owner_programs.update(at=time.time(), list=feed.read_processes())
    cwds = []
    for jd in _recent_job_dirs():
        if (jd / "seen.flag").exists():
            continue
        meta = load_json(jd / "meta.json", {}) or {}
        if (load_json(jd / "status.json", {}) or {}).get("state") == "running":
            cwds.append((jd.name, meta.get("cwd")))
    specs = {s["name"].casefold(): s for s in CONFIG["watched_processes"]}
    wanted = {n.casefold() for n in names}
    found = []
    for _pid, name, path in _owner_programs["list"]:
        if name.casefold() in wanted:
            who, kind = feed.process_owner(name, path, cwds, specs.get(name.casefold(), {}))
            if kind != "agent":
                found.append(name if who == "unknown" else f"{name} ({who})")
    return sorted(set(found))


def take_slot(resource, holder, job=True):
    spec = CONFIG["resources"].get(resource) or {}
    return leases.acquire(DELEGATE_ROOT, CONFIG["resources"], resource, holder, job=job,
                          is_job_alive=job_running, blocked=lambda: owner_programs(spec.get("yield_to")))


def resource_rules(job):
    command = f'python "{Path(__file__).resolve().as_posix()}"'
    lines = ["Shared machine: the things below are limited, and other agents and the machine's owner use "
             "them too. Before you do what a line describes, take its slot and wait until it is GRANTED. "
             "Never go ahead without the slot, and use exactly the value a grant gives you (for example a folder)."]
    for name, spec in CONFIG["resources"].items():
        n = spec.get("slots", 1)
        lines.append(f"  - {name} ({n} slot{'s' if n != 1 else ''}): {spec.get('rule') or 'any use of it'}")
    lines += [f"  {command} lease --job {job} <name>",
              f"  {command} release --job {job} <name>",
              "lease waits for a free slot, so give it the longest timeout your shell tool allows; if it "
              "prints NOT YET, do work that does not need the slot, then run it again. Release a slot as "
              "soon as you are done with it; anything you still hold is released when the job ends."]
    return "\n".join(lines)


def _holder(args):
    """(holder, is_job) for a lease command: a job id, or a free-form name for anything else."""
    if getattr(args, "job", None):
        return args.job, True
    return getattr(args, "holder", None), False


def cmd_lease(args):
    """Take a slot on a shared resource, waiting for one. A sandboxed worker cannot write the
    lease file: its runner sees this command start and takes the slot for it."""
    holder, is_job = _holder(args)
    resource = args.resource
    if not holder or not re.fullmatch(r"[\w.-]+", holder) or (is_job and _worker_job(holder) is None):
        out(f"NOT GRANTED: give --job <your job id> (or --as <a name> outside a job). Do not use {resource} "
            "without a slot; say so in your report.")
        return
    wait_s = CONFIG["ask_wait_seconds"] if args.wait is None else args.wait
    since, direct, reason = time.time(), True, None
    while True:
        got = None
        if direct:
            try:
                got = take_slot(resource, holder, job=is_job)
            except OSError:
                direct = False
        if got is None:
            mine = leases.held(DELEGATE_ROOT, resource, holder, job_running)
            waiting = load_json(JOBS_ROOT / holder / "lease.wait", {}) or {}
            got = (dict(ok=True, slot=mine.get("slot"), value=mine.get("value")) if mine
                   else dict(ok=False, reason=waiting.get(resource) or "waiting for a free slot"))
        if got["ok"]:
            value = f" Use: {got['value']}" if got.get("value") else ""
            out(f"GRANTED: {resource} slot {got['slot']}.{value} Release it as soon as you are done.")
            return
        reason = got["reason"]
        if got.get("unknown") or time.time() - since >= wait_s:
            break
        time.sleep(min(ASK_POLL_S, max(0.05, wait_s)))
    out(f"NOT YET: {reason}. Do not use {resource} now. Do work that does not need it, then run the same "
        "lease command again.")


def cmd_release(args):
    holder, _is_job = _holder(args)
    if not holder:
        out("release: give --job <your job id> or --as <name>")
        return
    try:
        freed = leases.release(DELEGATE_ROOT, holder, args.resource)
        out(f"RELEASED: {', '.join(freed)}" if freed else "Nothing to release.")
    except OSError:
        out("ok: the slot is released for you.")       # sandboxed worker: its runner does it


def slot_lines(now=None):
    now = time.time() if now is None else now
    lines = []
    for r in leases.snapshot(DELEGATE_ROOT, CONFIG["resources"], job_running, now):
        holders = ", ".join(f"{h['holder']} ({fmt_secs(now - (h['since'] or now))})" for h in r["held"]) or "free"
        lines.append(f"  {r['name']} {len(r['held'])}/{r['slots']}: {holders}")
    return lines


def cmd_slots(_args):
    if not CONFIG["resources"]:
        out("No resources are configured (config key `resources`).")
        return
    out("SLOTS:")
    for line in slot_lines():
        out(line)
    for name, spec in CONFIG["resources"].items():
        busy = owner_programs(spec.get("yield_to"))
        if busy:
            out(f"  {name} is closed while the owner runs: {', '.join(busy)}")


def tell_job(jd, text):
    """Queue a message for a running job. Returns what to tell the sender; ValueError if it cannot land."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("empty message")
    if effective_status(jd).get("state") in TERMINAL:
        raise ValueError(f"{jd.name} has finished; continue it with: run <agent> --resume {jd.name} --prompt-file ...")
    send(jd, "tell", text)
    note = f"QUEUED for {jd.name}: the worker reads it when it next names a stage or checks its messages."
    if open_question(jd):
        note += "\nNOTE: this worker is waiting on a question; `answer` is what lets it continue."
    return note


def answer_job(jd, text):
    """Answer a running job's open question. Returns what to tell the sender; ValueError if there is none."""
    text = str(text or "").strip()
    question = open_question(jd)
    if not text:
        raise ValueError("empty answer")
    if not question:
        raise ValueError(f"{jd.name} has no open question; use `tell` to send it a message")
    send(jd, "answer", text, question=question.get("text"))
    journal(jd, "answered", text)
    return f"ANSWERED {jd.name} after {fmt_secs(time.time() - question.get('t', time.time()))}: the worker continues."


def cmd_tell(args):
    try:
        out(tell_job(job_dir(args.job), " ".join(args.text)))
    except ValueError as e:
        sys.exit(f"error: {e}")


def cmd_answer(args):
    try:
        out(answer_job(job_dir(args.job), " ".join(args.text)))
    except ValueError as e:
        sys.exit(f"error: {e}")


def _claude_records():
    if not CONFIG["claude_agents_enabled"]:
        return []
    import claude_agents
    return claude_agents.ClaudeAgents().scan()


def _phase_text(phase, since, now):
    return f"phase: {phase} ({fmt_secs(now - since)})" if phase else "no phase reported"


def cmd_board(args):
    """Every running agent on one line each, then recent outcomes: cheap enough to answer 'how is it going?'."""
    now = time.time()
    stall = CONFIG["stall_seconds"]
    running, finished, queued = [], [], []
    for jd in _recent_job_dirs():
        meta = load_json(jd / "meta.json", {}) or {}
        st = effective_status(jd)
        state = st.get("state", "?")
        started = st.get("started") or meta.get("created") or now
        if state in TERMINAL:
            ended = st.get("finished") or started
            if now - ended <= args.hours * 3600:
                first = next((l.strip("# *") for l in read_text(jd / "result.md").splitlines() if l.strip()), "")
                if st.get("verdict"):
                    state += f" / {st['verdict']}" + (f", {st['flags']} FLAGGED" if st.get("flags") else "")
                finished.append((ended, f"  {jd.name}  \"{one_line(job_title(meta, jd.name), 60)}\"  {state}  {fmt_secs(now - ended)} ago"
                                        + (f"  » {one_line(first, 100)}" if first else "")))
            continue
        if state == "queued":
            queued.append(f"  {jd.name}  {job_title(meta, jd.name)}  ({meta.get('label', '?')})  behind {', '.join(meta.get('after') or [])}")
            continue
        info = phase_info(jd)
        try:
            touched = os.stat(meta.get("activity_file", "")).st_mtime
        except OSError:
            touched = started
        quiet = now - max(touched, _newest_write(meta.get("cwd")))
        line = (f"  {jd.name}  \"{one_line(job_title(meta, jd.name), 60)}\"  {meta.get('label', '?')}  {fmt_secs(now - started)}  "
                f"{_phase_text(info['phase'], info['phase_since'], now)}  last activity {fmt_secs(quiet)} ago")
        if not info["phase"]:
            line += f"  | {one_line(last_line(meta.get('activity_file', '')), 90)}"
        if info["queued"]:
            line += f"  [{info['queued']} queued message(s) unread]"
        if info["question"]:
            line += (f"\n      ASKING (waiting {fmt_secs(now - info['question_since'])}): "
                     f"{one_line(info['question'], 300)}")
        running.append(line + ("  QUIET" if quiet > stall and not info["question"] else ""))
    try:
        records = _claude_records()
    except Exception as e:
        records = []
        out(f"(Claude subagents unreadable: {e!r})")
    died = []
    for rec in records:
        if rec.get("died") and now - (rec.get("finished") or now) <= args.hours * 3600:
            died.append(f"  {rec['id']} ({rec['agent_type'].rsplit(':', 1)[-1]})  \"{one_line(rec['label'], 60)}\"  "
                        f"died {fmt_secs(now - (rec.get('finished') or now))} ago\n      {rec['resume']}")
        if rec["state"] != "running":
            continue
        quiet = now - max(rec["last_activity"], _newest_write(rec.get("workdir")))
        running.append(f"  {rec['id']} ({rec['agent_type'].rsplit(':', 1)[-1]})  \"{one_line(rec['label'], 60)}\"  "
                       f"{rec.get('model') or 'Claude'}  {fmt_secs(now - rec['started'])}  "
                       f"{_phase_text(rec.get('phase'), rec.get('phase_since'), now)}  "
                       f"last activity {fmt_secs(quiet)} ago" + ("  QUIET" if quiet > stall else ""))
    out(f"RUNNING: {len(running)} | finished in the last {args.hours:g}h: {len(finished)}")
    for line in running:
        out(line)
    reviewed = load_reviewed()
    died = [line for line in died if line.split()[0] not in reviewed]
    if died:
        out("DIED OF A CONNECTION ERROR (not finished; resume each once, or mark it reviewed):")
        for line in died:
            out(line)
    spend = api_spend(now)
    if spend["month"] or spend["budget"]:
        out(spend_line(spend))
    if nous_line(now):
        out(nous_line(now))
    if queued:
        out("QUEUED:")
        for line in reversed(queued):
            out(line)
    if CONFIG["resources"]:
        out("SLOTS:")
        for line in slot_lines(now):
            out(line)
    if finished:
        out("FINISHED:")
        for _ended, line in sorted(finished, reverse=True)[:8]:
            out(line)


def cmd_status(args):
    report(job_dir(args.job))


def cmd_cancel(args):
    jd = job_dir(args.job)
    st = effective_status(jd)
    if st.get("state") in TERMINAL:
        out(f"{jd.name} already finished with status {st.get('state')}; nothing to cancel")
        return
    if st.get("state") == "queued":
        st.update(state="cancelled", finished=time.time(), errors=["cancelled while queued"])
        save_json(jd / "status.json", st)
        journal(jd, "cancelled")
        out(f"{jd.name} was queued and is now cancelled; it never started")
        return
    (jd / "cancel.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    deadline = time.time() + 45
    while time.time() < deadline:
        if effective_status(jd).get("state") in TERMINAL:
            report(jd)
            return
        time.sleep(1)
    # The runner did not respond: stop it ourselves.
    st = load_json(jd / "status.json", {}) or {}
    if st.get("state") not in TERMINAL:
        started = st.get("started") or 0
        child_born = process_start_time(st.get("child_pid"))
        if child_born is not None and child_born >= started - 5:   # never kill a process that reused the PID
            kill_tree(st.get("child_pid"))
        if runner_alive(st):
            kill_tree(st.get("runner_pid"))
        st.update(state="cancelled", finished=time.time())
        st.setdefault("errors", []).append("runner did not respond to cancel; processes were killed")
        journal(jd, "cancelled")
        save_json(jd / "status.json", st)
    report(jd)


def cmd_list(args):
    if not JOBS_ROOT.is_dir():
        out("no jobs yet")
        return
    for jd in sorted((p for p in JOBS_ROOT.iterdir() if p.is_dir()), reverse=True)[:args.n]:
        meta = load_json(jd / "meta.json", {})
        st = effective_status(jd)
        out(f"{jd.name}  {st.get('state', '?'):9}  {meta.get('mode', ''):5}  {meta.get('cwd', '')}")


_SHELL_PREFIX = re.compile(
    r'^(?:&\s*)?(?:"[^"\n]*(?:pwsh|powershell|cmd)(?:\.exe)?"|[^\s"]*(?:pwsh|powershell|cmd)(?:\.exe)?)'
    r'\s+(?:(?:-NoProfile|-NoLogo|-NonInteractive|/d|/s)\s+|-ExecutionPolicy\s+\S+\s+)*'
    r'(?:-Command|-c|/c)\s+', re.I)

_COMMAND_LINE = re.compile(r'^(\d\d:\d\d:\d\d)\s+command \(exit (-?\d+)\):\s*(.*)$')
_MESSAGE_LINE = re.compile(r'^(\d\d:\d\d:\d\d)\s+message:\s*(.*)$')
_COLORS = dict(dim="\x1b[2m", cyan="\x1b[36m", red="\x1b[31m", yellow="\x1b[33m", green="\x1b[32m",
               bold="\x1b[1m", off="\x1b[0m")


def _condense_command(text):
    """A shell command as a person would read it: no interpreter prefix or outer quotes, short paths."""
    text = text.strip()
    wrapper = _SHELL_PREFIX.match(text)
    if wrapper:
        text = text[wrapper.end():]
    if wrapper and text[:1] in ("\"", "'"):
        text = text[1:]
        if text[-1:] in ("\"", "'"):
            text = text[:-1]
    text = text.replace('\\"', '"')
    text = shorten_paths(text, home=HOME)
    return " ".join(text.split())


def _newest_write(folder, depth=2, limit=4000):
    """Newest file mtime in a job's working folder (two levels deep, bounded); configured shared roots are ignored."""
    if not folder or not os.path.isdir(folder) or ignored_activity_path(folder):
        return 0
    newest, seen, stack = 0, 0, [(folder, 0)]
    while stack and seen < limit:
        d, n = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if n < depth and not e.name.startswith("."):
                                stack.append((e.path, n + 1))
                        else:
                            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        pass
        except OSError:
            pass
    return newest


def _job_tag(meta):
    folder = Path(meta.get("cwd") or "").name or "?"
    return f"{folder}:{meta.get('profile', '?').replace('gpt-', '')}"


def _claude_tag(rec):
    return f"claude/{rec['agent_type'].rsplit(':', 1)[-1][:14]}#{rec['agent_id'][-4:]}"


def _watch_claude(reader, followed, now, args, emit):
    """Start / finish / stall / loop lines for native Claude Code subagents (no per-step lines)."""
    for rec in reader.scan(now):
        tag = _claude_tag(rec)
        job = followed.get(rec["id"])
        if job is None:
            if rec["state"] != "running":
                continue
            job = followed[rec["id"]] = dict(steps=rec["steps"], last_event=rec["last_activity"], warned=False,
                                             recent=[], done=False)
            under = f"  under {rec['parent']}" if rec.get("parent") else ""
            emit(tag, "green", f"▶ {rec['label']}  ({rec['model'] or 'Claude'}, {rec['agent_type']}){under}")
        if job["done"]:
            if rec["state"] != "running":
                continue
            job["done"] = False      # resumed with a follow-up message
            emit(tag, "green", f"▶ resumed: {rec['label']}")
        if rec["last_activity"] > job["last_event"]:
            job["last_event"], job["warned"] = rec["last_activity"], False
        new = rec["steps"] - job["steps"]
        if new > 0:
            job["steps"] = rec["steps"]
            job["recent"] = (job["recent"] + rec["recent"][-min(new, len(rec["recent"])):])[-10:]
            loop, _polling = repetition(job["recent"], count=args.loop)
            if loop:
                same = sum(step == job["recent"][-1][0] for step, _t in job["recent"])
                emit(tag, "yellow", f"⚠ the same step ran {same} times in the last 10 - possible loop")
                job["recent"] = []
        if rec["state"] in TERMINAL:
            job["done"] = True
            first = next((l for l in (rec.get("result") or "").splitlines() if l.strip()), "")
            colour = "green" if rec["state"] == "done" else "red"
            emit(tag, colour, f"■ {rec['state']}  after {fmt_secs(rec['last_activity'] - rec['started'])}"
                 + (f"  » {first.strip('# *')}" if first else ""))
        elif not job["warned"] and now - job["last_event"] > args.stall * 60:
            wrote = _newest_write(rec["workdir"])
            if now - wrote <= args.stall * 60:
                job["last_event"] = wrote
                continue
            job["warned"] = True
            emit(tag, "yellow", f"⚠ no activity for {args.stall} min (still running)")


def cmd_watch(args):
    """Follow the activity of running jobs live: one line per command / message, plus stall and loop warnings."""
    if os.name == "nt":
        os.system("")   # turns on ANSI colours in the Windows console
    c = _COLORS if not args.no_color else {k: "" for k in _COLORS}
    width = args.width
    followed = {}       # job_id -> dict(meta, path, offset, last_event, warned, recent, done)

    def emit(tag, colour, text):
        out(f"{c[colour]}{tag:28}{c['off']} {one_line(text, width)}")

    def candidates():
        if args.job:
            return [job_dir(j) for j in args.job]
        if not JOBS_ROOT.is_dir():
            return []
        return sorted((p for p in JOBS_ROOT.iterdir() if p.is_dir()), reverse=True)[:40]

    # native Claude Code subagents too, unless only named jobs are followed or the config switches them off
    claude, claude_followed = None, {}
    if not args.job and CONFIG["claude_agents_enabled"]:
        import claude_agents
        claude = claude_agents.ClaudeAgents(stall_seconds=args.stall * 60)
    what = "delegate jobs and Claude subagents" if claude else "delegate jobs"
    out(f"{c['bold']}watching {what} (Ctrl+C to stop; stall warning after {args.stall} min){c['off']}")
    try:
        while True:
            now = time.time()
            if claude is not None:
                try:
                    _watch_claude(claude, claude_followed, now, args, emit)
                except Exception as e:      # an unreadable transcript must not end the watch; say so once
                    if claude_followed.get("_error") != repr(e):
                        claude_followed["_error"] = repr(e)
                        emit("claude", "red", f"could not read Claude subagents: {e!r}")
            for jd in candidates():
                job_id = jd.name
                if job_id not in followed:
                    st = effective_status(jd)
                    if not args.job and st.get("state") not in ("running", "starting"):
                        continue
                    meta = load_json(jd / "meta.json", {}) or {}
                    path = Path(meta.get("activity_file") or agent_paths(meta.get("profile", ""), job_id)["activity"])
                    lines = read_text(path).splitlines()
                    followed[job_id] = dict(meta=meta, path=path, seen=max(0, len(lines) - args.backlog),
                                            last_event=now, warned=False, recent=[], done=False)
                    emit(_job_tag(meta), "green", f"▶ {job_id}  {meta.get('label', '')}  mode={meta.get('mode', '')}")
                job = followed[job_id]
                if job["done"]:
                    continue
                tag = _job_tag(job["meta"])
                lines = read_text(job["path"]).splitlines()
                for line in lines[job["seen"]:]:
                    job["last_event"], job["warned"] = now, False
                    m = _COMMAND_LINE.match(line)
                    if m:
                        stamp, rc, cmd = m.groups()
                        cmd = _condense_command(cmd)
                        h, mi, s = map(int, stamp.split(":"))
                        job["recent"] = (job["recent"] + [(cmd, h * 3600 + mi * 60 + s)])[-10:]
                        emit(tag, "red" if rc != "0" else "dim", f"{stamp} $ {cmd}" + (f"  [exit {rc}]" if rc != "0" else ""))
                        loop, _polling = repetition(job["recent"], count=args.loop)
                        same = sum(cmd == c for c, _t in job["recent"])
                        if loop:
                            emit(tag, "yellow", f"⚠ the same command ran {same} times in the last 10 - possible loop")
                            job["recent"] = []
                        continue
                    m = _MESSAGE_LINE.match(line)
                    if m:
                        emit(tag, "cyan", f"{m.group(1)} » {m.group(2)}")
                    elif line.strip():
                        emit(tag, "dim", line)
                job["seen"] = len(lines)
                st = effective_status(jd)
                if st.get("state") in TERMINAL:
                    job["done"] = True
                    colour = "green" if st["state"] == "done" else "red"
                    emit(tag, colour, f"■ {st['state']}  after {fmt_secs(now - job['meta'].get('created', now))}")
                elif not job["warned"] and now - job["last_event"] > args.stall * 60:
                    # an agent inside one long step logs nothing until it ends; recent writes in its folder count too
                    wrote = _newest_write(job["meta"].get("cwd"))
                    if now - wrote <= args.stall * 60:
                        job["last_event"] = wrote
                        continue
                    job["warned"] = True
                    emit(tag, "yellow", f"⚠ no activity for {args.stall} min (still {st.get('state')})")
            if args.job and all(j["done"] for j in followed.values()):
                return
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass


def cmd_profiles(_args):
    for name, p in PROFILES.items():
        out(f"{name:15} {p['backend']:6} {p['model']:28} effort={p['effort']:7} {p['label']}")
    out(f"\nPer-agent folders: {AGENTS_ROOT}/<agent>/(progress|activity|reports)/<job_id>, latest-report.md")


def needs_look(state, verdict=None, flags=0):
    """A finished job someone should look at: it did not end as done, its own verdict is not a
    clean pass, or claims in its result did not check out."""
    return state != "done" or verdict in ("fail", "blocked", "partial") or bool(flags)


def load_reviewed():
    """Job ids (relay jobs and `agent-<id>` subagents) someone has marked as reviewed -> when."""
    data = load_json(DELEGATE_ROOT / "reviewed.json", {})
    return data if isinstance(data, dict) else {}


def set_reviewed(job, on=True):
    """Mark a finished job as reviewed (or not), for every page and device. Returns the new mark."""
    with leases.locked(DELEGATE_ROOT):
        data = load_reviewed()
        if on:
            data[job] = dict(t=time.time())
        else:
            data.pop(job, None)
        if len(data) > 2000:            # keep the newest marks only
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("t", 0))[-2000:])
        save_json(DELEGATE_ROOT / "reviewed.json", data)
    return bool(on)


CAPTURE_RULES = ("Capture command (Windows), for any screenshot of a program's window you cite as evidence:\n"
                 "  {command} capture <absolute out.png> --process <name.exe>\n"
                 "(or --window \"<part of the title>\", or --foreground; `capture --list` shows the open windows). "
                 "It never activates or moves the window. It saves that window's client area and a record of what "
                 "it was of: title, program, real size, border, monitor. With several copies of one program "
                 "running, name yours with --pid. If it prints NOT CAPTURED or warns that the picture is blank, "
                 "say so in your report instead of citing another picture.")


def capture_rules():
    return CAPTURE_RULES.format(command=f'python "{Path(__file__).resolve().as_posix()}"')


def cmd_capture(args):
    """Screenshot one program's window, with a record of what it was of beside the image."""
    import capture
    if args.list:
        if os.name != "nt":
            sys.exit("error: window capture is only supported on Windows")
        for w in capture.pick(capture.list_windows()):
            out(f"  {w['process'] or '?':24} {w['client'][0]}x{w['client'][1]:<6} {'front ' if w['foreground'] else '      '}"
                f"{'borderless ' if w['borderless'] else ''}\"{one_line(w['title'], 80)}\"")
        return
    if not args.out:
        sys.exit("error: give the PNG file to write, or --list")
    target = Path(args.out).expanduser()
    if target.suffix.lower() != ".png":
        sys.exit("error: the output file must end in .png")
    try:
        got = capture.capture(str(target), process=args.process, title=args.window, pid=args.pid,
                              foreground=args.foreground, method=args.method)
    except OSError as e:
        got = dict(ok=False, reason=str(e), windows=[])
    if not got["ok"]:
        out(f"NOT CAPTURED: {got['reason']}.")
        shown = capture.pick(got.get("windows") or [])[:12]
        if shown:
            out("Visible windows: " + "; ".join(f"{w['process']} \"{one_line(w['title'], 40)}\"" for w in shown))
        sys.exit(1)
    out(f"CAPTURED: {got['path']}  {got['width']}x{got['height']}  from {capture.describe(got)}")
    if got["blank"]:
        out("WARNING: the picture is one flat colour (blank): this program does not draw into a background "
            "capture. Do not cite it as proof; say the capture was blank. (--method screen copies what is "
            "displayed, but only shows this window if it is in front, which the helper never arranges.)")
    if got["other_matches"]:
        out(f"NOTE: {got['other_matches']} other window(s) also matched; the one in front (else the largest) was taken.")
    cite = dict(path=got["path"], shows="<what it proves>", width=got["width"], height=got["height"], process=got["process"])
    out(f"RECORD: {capture.record_path(got['path'])}")
    out(f"CITE IT AS: {json.dumps(cite)}")


def cmd_save(args):
    """Write text to a file without the Write tool or a heredoc: from standard input, or the
    final message of a finished job or Claude subagent (which needs no quoting at all)."""
    path = Path(args.path).expanduser()
    if args.source:
        try:
            ref = resolve_ref(args.source)
        except ValueError as e:
            sys.exit(f"error: {e}")
        if (JOBS_ROOT / ref).is_dir():
            text = read_text(JOBS_ROOT / ref / "result.md")
        else:
            text = next((r.get("final") or "" for r in _claude_records() if r["id"] == ref), "")
        if not text.strip():
            sys.exit(f"error: {ref} has no final message yet")
        data = text.encode("utf-8")
    else:
        data = sys.stdin.buffer.read()
        if not data.strip():
            sys.exit("error: nothing arrived on standard input; pipe the text in, e.g.  cat <<'EOF' | ... save <path>")
    if path.is_dir():
        sys.exit(f"error: {path} is a folder")
    if path.exists() and path.stat().st_size and not (args.append or args.force):
        sys.exit(f"error: {path} already exists; add --force to replace it or --append to add to it")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if args.append else "wb") as f:
            f.write(data)
    except OSError as e:
        sys.exit(f"error: could not write {path}: {e}")
    out(f"SAVED: {path.resolve()} ({len(data)} bytes{', appended' if args.append else ''})")


def cmd_reviewed(args):
    try:
        job = resolve_ref(args.job)
    except ValueError as e:
        sys.exit(f"error: {e}")
    set_reviewed(job, not args.undo)
    out(f"{job}: {'no longer marked' if args.undo else 'marked'} as reviewed")


def digest_lines(hours, now=None):
    """What happened in the last N hours, for someone who was away: what needs a look, what is
    theirs to do, what is still open, and what is running, asking or queued now."""
    now = time.time() if now is None else now
    look, turn, opened, fine, running, asking, queued = [], [], [], 0, 0, [], 0
    finished = reviewed_n = 0
    reviewed = load_reviewed()

    def take(job, name, state, checked, ended):
        nonlocal finished, fine, reviewed_n
        finished += 1
        checked = checked or {}
        data = checked.get("data") or {}
        flags = [f["text"] for f in checked.get("findings") or [] if not f.get("ok")]
        verdict = checked.get("verdict")
        if needs_look(state, verdict, flags) and job in reviewed:
            reviewed_n += 1
        elif needs_look(state, verdict, flags):
            look.append(f"  \"{name}\": {state}" + (f" / {verdict}" if verdict else "") + f", {fmt_secs(now - ended)} ago")
            look.extend(f"      FLAG: {one_line(t, 200)}" for t in flags[:4])
        else:
            fine += 1
        turn.extend(f"  \"{name}\": {one_line(item, 200)}" for item in data.get("owner") or [] if isinstance(item, str))
        opened.extend(f"  \"{name}\": {one_line(item, 200)}" for item in data.get("open") or [] if isinstance(item, str))

    for jd in _recent_job_dirs(200):
        meta = load_json(jd / "meta.json", {}) or {}
        st = effective_status(jd)
        state, name = st.get("state", "?"), job_title(meta, jd.name)
        if state == "queued":
            queued += 1
        elif state not in TERMINAL:
            running += 1
            q = open_question(jd)
            if q:
                asking.append(f"  \"{name}\" ({jd.name}): {one_line(q.get('text'), 200)}")
        elif not st.get("resumed_by") and now - (st.get("finished") or 0) <= hours * 3600:
            take(jd.name, name, state, load_json(jd / "result.json"), st.get("finished") or now)
    try:
        records = _claude_records()
    except Exception:
        records = []
    for rec in records:
        if rec["state"] == "running":
            running += 1
        elif now - (rec.get("finished") or 0) <= hours * 3600:
            take(rec["id"], rec["label"], rec["state"], rec.get("checked"), rec.get("finished") or now)
    lines = [f"DIGEST: last {hours:g}h | finished {finished} ({fine} clean, {finished - fine - reviewed_n} need a look"
             + (f", {reviewed_n} reviewed" if reviewed_n else "") + ") | "
             f"running {running} | asking {len(asking)} | queued {queued}"]
    for title, block in (("WAITING FOR AN ANSWER:", asking), ("NEEDS A LOOK:", look), ("YOUR TURN:", turn), ("STILL OPEN:", opened)):
        if block:
            lines += [title] + block
    spend = api_spend(now)
    if spend["month"] or spend["budget"]:
        lines.append(spend_line(spend))
    if nous_line(now):
        lines.append(nous_line(now))
    return lines


def cmd_digest(args):
    lines = digest_lines(args.hours)
    for line in lines:
        out(line)
    if args.send:
        errors = notify.send("digest", lines[0], "\n".join(lines[1:]))
        out("SENT" if not errors and CONFIG["notify"] else "NOT SENT: " + ("; ".join(errors) or "no `notify` target is configured"))


def api_spend(now=None):
    """Claude API spend recorded this calendar month, and the monthly budget if one is configured."""
    now = time.time() if now is None else now
    month = time.strftime("%Y-%m", time.localtime(now))
    total = 0.0
    for e in iter_events(DELEGATE_ROOT / "claude-api-ledger.jsonl"):
        when = e.get("end") or e.get("start") if isinstance(e, dict) else None
        cost = claude_api.cost_value(e.get("total_cost_usd")) if isinstance(e, dict) else None
        if when and cost is not None and time.strftime("%Y-%m", time.localtime(when)) == month:
            total += cost
    return dict(month=round(total, 4), budget=CONFIG["claude_api_monthly_usd"] or None)


def nous_line(now=None):
    spent, budget = nous.month_spend(DELEGATE_ROOT, now), CONFIG["nous_monthly_usd"]
    if not (spent or budget):
        return ""
    shown = f"{spent:.2f}" if spent >= 0.1 or not spent else f"{spent:.3f}"     # a few cents still shows
    return f"NOUS PORTAL: ${shown} this month" +(f" of ${budget:g} ({100 * spent / budget:.0f}%)" if budget else "")


def spend_line(spend):
    text = f"CLAUDE API: ${spend['month']:.2f} this month"
    if spend["budget"]:
        text += f" of ${spend['budget']:g} ({100 * spend['month'] / spend['budget']:.0f}%)"
    return text


def cmd_spend(args):
    entries = list(iter_events(DELEGATE_ROOT / "claude-api-ledger.jsonl"))
    costs = [claude_api.cost_value(e.get("total_cost_usd")) for e in entries]
    total = sum(c for c in costs if c is not None)
    out(f"CLAUDE API SPEND (USD): {total:.6f} | jobs: {len(entries)} | unknown cost: {costs.count(None)}")
    for entry in entries[-args.n:]:
        cost = claude_api.cost_value(entry.get("total_cost_usd"))
        display = "unknown" if cost is None else f"${cost:.6f}"
        out(f"{entry.get('job_id')} {entry.get('agent')} {entry.get('status')} {display} approved={entry.get('approved')}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a job on an agent and wait for it")
    r.add_argument("profile", metavar="agent")
    r.add_argument("--prompt-file", required=True, help="task brief file, or - for stdin")
    r.add_argument("--cwd")
    r.add_argument("--name", help="short title for the board, feed and alerts (default: the working folder's name)")
    r.add_argument("--mode", choices=["read", "write", "full"], default="write")
    r.add_argument("--preamble", action="append", default=[],
                   help="file to put before the brief, such as a project brief (repeatable)")
    r.add_argument("--effort")
    r.add_argument("--add-dir", action="append", default=[])
    r.add_argument("--resume", metavar="JOB_ID")
    r.add_argument("--after", action="append", metavar="JOB_ID",
                   help="queue this job until that job (or agent-<id> Claude subagent) has ended; repeatable")
    r.add_argument("--after-ok", action="store_true",
                   help="with --after: start only if those ended successfully, otherwise cancel this job")
    r.add_argument("--wait", type=int, default=DEFAULT_WAIT_S)
    r.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES)
    r.add_argument("--approved", help="owner's approval note for this specific Claude API run (who/when/why)")
    r.add_argument("--max-usd", type=float, help="Claude API spend cap; checked at reported cost events")
    r.set_defaults(fn=cmd_run)

    w = sub.add_parser("wait")
    w.add_argument("job", nargs="?")
    w.add_argument("--any", action="store_true",
                   help="return when any job ends; each outcome is reported exactly once")
    w.add_argument("--wait", type=int, default=DEFAULT_WAIT_S)
    w.set_defaults(fn=cmd_wait)

    b = sub.add_parser("board", help="one line per running agent: phase, time, last activity")
    b.add_argument("--hours", type=float, default=12, help="also list jobs finished in the last N hours")
    b.set_defaults(fn=cmd_board)

    ph = sub.add_parser("phase", help="run by a worker to name the stage it is starting")
    ph.add_argument("--job")
    ph.add_argument("text", nargs="*")
    ph.set_defaults(fn=cmd_phase)

    ck = sub.add_parser("check", help="run by a worker to read messages from its supervisor")
    ck.add_argument("--job")
    ck.set_defaults(fn=cmd_check)

    ak = sub.add_parser("ask", help="run by a worker to put a question to its supervisor and wait for the answer")
    ak.add_argument("--job")
    ak.add_argument("--wait", type=float, help="seconds to wait before asking the worker to run it again")
    ak.add_argument("text", nargs="*")
    ak.set_defaults(fn=cmd_ask)

    tl = sub.add_parser("tell", help="queue a message for a running job; it is read at the next stage change")
    tl.add_argument("job")
    tl.add_argument("text", nargs="+")
    tl.set_defaults(fn=cmd_tell)

    an = sub.add_parser("answer", help="answer a running job's open question so it can continue")
    an.add_argument("job")
    an.add_argument("text", nargs="+")
    an.set_defaults(fn=cmd_answer)

    le = sub.add_parser("lease", help="take a slot on a shared resource, waiting for one")
    le.add_argument("--job")
    le.add_argument("--as", dest="holder", help="holder name when not running inside a job")
    le.add_argument("--wait", type=float)
    le.add_argument("resource")
    le.set_defaults(fn=cmd_lease)

    rl = sub.add_parser("release", help="give a slot back (all of the holder's slots if no resource is named)")
    rl.add_argument("--job")
    rl.add_argument("--as", dest="holder")
    rl.add_argument("resource", nargs="?")
    rl.set_defaults(fn=cmd_release)

    bg = sub.add_parser("bg", help="run a long command outside your session; one notice when it ends",
                        description="Run a long command (a boot loop, a bulk delete, a soak test) as a job of its own. "
                                    "Put the command after --. A {name} in it is replaced by the value of a leased slot.")
    bg.add_argument("--name", help="short title for the board, feed and alerts")
    bg.add_argument("--cwd")
    bg.add_argument("--lease", action="append", metavar="RESOURCE",
                    help="take this slot before starting and hold it until the command ends; repeatable")
    bg.add_argument("--shell", choices=["none", "bash", "powershell", "cmd"], default="none",
                    help="hand the command line to a shell (needed for pipes and redirects)")
    bg.add_argument("--max-minutes", type=int, default=BG_MAX_MINUTES)
    bg.add_argument("--wait", type=int, default=0, help="also wait this many seconds for it here")
    bg.add_argument("command", nargs=argparse.REMAINDER)
    bg.set_defaults(fn=cmd_bg)

    cp = sub.add_parser("capture", help="screenshot a program's window, with a record of what it was of",
                        description="Saves the window's client area as a PNG and writes <png>.capture.json beside it "
                                    "(title, program, real size, border, monitor). The result checks compare an "
                                    "evidence entry's claims with that record.")
    cp.add_argument("out", nargs="?", help="PNG file to write")
    cp.add_argument("--process", help="the program's file name, such as TheApp.exe")
    cp.add_argument("--window", help="text the window title contains")
    cp.add_argument("--pid", type=int)
    cp.add_argument("--foreground", action="store_true", help="the window that is in front right now")
    cp.add_argument("--method", choices=["auto", "screen", "window"], default="auto",
                    help="screen: copy what is displayed (window must be in front); window: let the window draw itself")
    cp.add_argument("--list", action="store_true", help="list the windows that can be captured")
    cp.set_defaults(fn=cmd_capture)

    sv = sub.add_parser("save", help="write piped text to a file, for agents whose Write tool is refused",
                        description="Writes standard input to PATH, or with --from the final message of a finished "
                                    "job or Claude subagent.")
    sv.add_argument("path")
    sv.add_argument("--from", dest="source", metavar="JOB", help="save that job's or subagent's final message instead of stdin")
    sv.add_argument("--append", action="store_true")
    sv.add_argument("--force", action="store_true", help="replace a file that already exists")
    sv.set_defaults(fn=cmd_save)

    sub.add_parser("slots", help="show shared resources and who holds them").set_defaults(fn=cmd_slots)
    rv = sub.add_parser("reviewed", help="mark a finished job as reviewed, so it no longer counts as needing a look")
    rv.add_argument("job")
    rv.add_argument("--undo", action="store_true")
    rv.set_defaults(fn=cmd_reviewed)
    dg = sub.add_parser("digest", help="what finished, what needs a look and what is yours to do, for the last N hours")
    dg.add_argument("--hours", type=float, default=12)
    dg.add_argument("--send", action="store_true", help="also send it through the configured alerts")
    dg.set_defaults(fn=cmd_digest)
    sub.add_parser("result-format", help="print the result block every final report should end with"
                   ).set_defaults(fn=lambda _a: out(results.RULES + "\n" + capture_rules()))

    s = sub.add_parser("status")
    s.add_argument("job")
    s.set_defaults(fn=cmd_status)

    c = sub.add_parser("cancel")
    c.add_argument("job")
    c.set_defaults(fn=cmd_cancel)

    ls = sub.add_parser("list")
    ls.add_argument("-n", type=int, default=15)
    ls.set_defaults(fn=cmd_list)

    wa = sub.add_parser("watch", help="follow running jobs' activity live (Ctrl+C to stop)")
    wa.add_argument("job", nargs="*", help="job ids to follow (default: every running job, and new ones as they start)")
    wa.add_argument("--stall", type=float, default=CONFIG["stall_seconds"] / 60, help="minutes without activity before a warning")
    wa.add_argument("--loop", type=int, default=CONFIG["loop_count"], help="same command this many times in 10 = loop warning")
    wa.add_argument("--backlog", type=int, default=5, help="earlier lines to show when a job is first seen (default 5)")
    wa.add_argument("--width", type=int, default=170, help="characters per line (default 170)")
    wa.add_argument("--no-color", action="store_true")
    wa.set_defaults(fn=cmd_watch)

    sub.add_parser("profiles").set_defaults(fn=cmd_profiles)
    spend = sub.add_parser("spend", help="show recorded Claude API spend and recent jobs")
    spend.add_argument("-n", type=int, default=15)
    spend.set_defaults(fn=cmd_spend)

    rn = sub.add_parser("_runner")
    rn.add_argument("jobdir")
    rn.set_defaults(fn=cmd_runner)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
