#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: bridge that lets Claude Code sessions and subagents hand work to external coding
agents (OpenAI Codex CLI and Moonshot Kimi Code CLI) and collect their final report.

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
  delegate.py status <job_id>
  delegate.py cancel <job_id>
  delegate.py list [-n N]
  delegate.py profiles
"""
import argparse
import ctypes
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from delegate_config import CONFIG, ignored_activity_path, repetition, shorten_paths

PROFILES = {
    "gpt-astra-med": dict(backend="codex", model="gpt-6-astra", effort="medium",
                          label="GPT-6-Astra (medium)"),
    "gpt-astra-high": dict(backend="codex", model="gpt-6-astra", effort="high",
                           label="GPT-6-Astra (high)"),
    "gpt-astra-xhigh": dict(backend="codex", model="gpt-6-astra", effort="xhigh",
                            label="GPT-6-Astra (extra high)"),
    "gpt-sol-med": dict(backend="codex", model="gpt-6.1-sol", effort="medium",
                        label="GPT-6.1-Sol (medium)"),
    "kimi-k28": dict(backend="kimi", model="kimi-code/kimi-for-coding", effort="max",
                     label="Kimi K2.8 Preview"),
    "kimi-k3": dict(backend="kimi", model="kimi-code/k3", effort="high",
                    label="Kimi K3"),
}

HOME = Path.home()
DELEGATE_ROOT = HOME / ".claude" / "delegate"
JOBS_ROOT = Path(os.environ.get("DELEGATE_JOBS_DIR", DELEGATE_ROOT / "jobs")).resolve()
AGENTS_ROOT = Path(os.environ.get("DELEGATE_AGENTS_DIR", DELEGATE_ROOT / "agents")).resolve()
DEFAULT_WAIT_S = 540           # stays under the Bash tool's 600 s ceiling
DEFAULT_MAX_MINUTES = 180      # hard cap on one job
CMDLINE_LIMIT = 30000          # Windows allows 32767 UTF-16 units; keep a margin
POLL_S = 2
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
PROGRESS_RULES = ("Progress notes file: {path}\n"
                  "Append short timestamped notes there as you work: what you tried, what you "
                  "learned, what is next. Write one when you start, after each significant "
                  "finding, and at least every 45-60 minutes on long tasks.{read_note}")
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


def build_codex_cmd(meta, jd):
    exe = find_codex()
    mode = meta["mode"]
    cfg = ["-c", f'model_reasoning_effort="{meta["effort"]}"',
           "-c", 'approval_policy="never"']
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
        self.src = jd / "events.jsonl"
        self.dst = Path(meta["activity_file"])
        self.offset = 0
        self.session = None

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
            summary = summarize_event(self.backend, ev)
            if summary:
                lines.append(f"{stamp}  {summary}")
        if lines:
            with open(self.dst, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


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
    errors = list(extra_errors) + errors
    status.update(state=state, exit_code=rc, finished=time.time(),
                  session_id=session or status.get("session_id") or meta.get("resume_session"),
                  errors=errors[-5:])
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
        if errors:
            lines += ["## Errors", ""] + [f"    {one_line(e, 1500)}" for e in errors[-5:]] + [""]
        lines += ["## Result", "", result or "(no final message)", ""]
        text = "\n".join(lines)
        ap["report"].write_text(text, encoding="utf-8")
        ap["latest"].write_text(text, encoding="utf-8")
    except Exception as e:
        status.setdefault("errors", []).append(f"could not write agent report: {e!r}")
    save_json(jd / "status.json", status)


def cmd_runner(args):
    jd = Path(args.jobdir).resolve()
    meta = load_json(jd / "meta.json")
    status = {"state": "running", "started": time.time(), "runner_pid": os.getpid()}
    if cancel_requested(jd):
        finalize(jd, meta, status, "cancelled", None)
        return
    save_json(jd / "status.json", status)
    state, rc, extra = None, None, []
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
            deadline = time.time() + meta["max_minutes"] * 60
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
                if time.time() > deadline:
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
    if st.get("state") == "running" and not pid_alive(st.get("runner_pid")):
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
    if st.get("session_id"):
        out(f"SESSION: {st['session_id']}  (follow up with: run {meta.get('profile')} --resume {jd.name} --prompt-file ...)")
    out(f"PROGRESS NOTES: {meta.get('progress_file')}")
    out(f"ACTIVITY LOG: {meta.get('activity_file')}")
    out(f"FILES: {jd}")
    if read_text(jd / "detach.txt").strip() == "in-job":
        out("WARNING: the runner could not leave the caller's job object; it may stop if the caller's process tree is killed.")
    if state not in TERMINAL:
        out(f"LAST ACTIVITY: {last_line(meta.get('activity_file', '')) or 'none yet'}")
        out(f"NEXT: python \"{Path(__file__).as_posix()}\" wait {jd.name}")
        return
    out(f"REPORT FILE: {agent_paths(meta.get('profile', ''), jd.name)['report']}")
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
                        PROGRESS_RULES.format(path=meta["progress_file"], read_note=read_note)])
    parts = [header]
    for p in preambles:
        parts.append(read_text(p).strip())
    parts.append(brief)
    if not meta.get("resume_session"):
        parts.append(REPORT_RULES)
    return "\n\n---\n\n".join(parts) + "\n"


def cmd_run(args):
    prof = PROFILES.get(args.profile)
    if not prof:
        sys.exit(f"error: unknown agent {args.profile!r}; choose from {', '.join(PROFILES)}")
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

    resume_session = None
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

    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{args.profile}-{secrets.token_hex(2)}"
    jd = JOBS_ROOT / job_id
    jd.mkdir(parents=True)
    ap = agent_paths(args.profile, job_id)
    for key in ("progress", "activity", "report"):
        ap[key].parent.mkdir(parents=True, exist_ok=True)
    meta = dict(profile=args.profile, **prof, mode=args.mode, cwd=cwd, add_dirs=add_dirs,
                resume_job=args.resume, resume_session=resume_session,
                created=time.time(), max_minutes=args.max_minutes, prompt_source=source,
                preambles=[str(Path(p).resolve()) for p in args.preamble],
                progress_file=str(ap["progress"]), activity_file=str(ap["activity"]))
    if args.effort:
        meta["effort"] = args.effort
    (jd / "prompt.md").write_text(build_prompt(args, meta, brief, args.preamble), encoding="utf-8")
    ap["progress"].write_text(f"# {args.profile} progress notes: {job_id}\n\n", encoding="utf-8")
    ap["activity"].write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  job {job_id} started "
                              f"({meta['label']}, effort {meta['effort']}, mode {args.mode}, cwd {cwd})\n",
                              encoding="utf-8")

    save_json(jd / "meta.json", meta)  # must exist before the runner starts

    runner = [sys.executable, str(Path(__file__).resolve()), "_runner", str(jd)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=cwd)
    if os.name == "nt":
        base = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            subprocess.Popen(runner, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
            detach = "breakaway"
        except OSError:
            subprocess.Popen(runner, creationflags=base, **kw)
            detach = "in-job"
    else:
        subprocess.Popen(runner, start_new_session=True, **kw)
        detach = "new-session"
    (jd / "detach.txt").write_text(detach, encoding="utf-8")
    out(f"STARTED: {job_id} ({args.profile} = {meta['label']}, effort={meta['effort']}, mode={args.mode})")
    wait_for(jd, args.wait)


def cmd_wait(args):
    wait_for(job_dir(args.job), args.wait)


def cmd_status(args):
    report(job_dir(args.job))


def cmd_cancel(args):
    jd = job_dir(args.job)
    st = effective_status(jd)
    if st.get("state") in TERMINAL:
        out(f"{jd.name} already finished with status {st.get('state')}; nothing to cancel")
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
        kill_tree(st.get("child_pid"))
        kill_tree(st.get("runner_pid"))
        st.update(state="cancelled", finished=time.time())
        st.setdefault("errors", []).append("runner did not respond to cancel; processes were killed")
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

    out(f"{c['bold']}watching delegate jobs (Ctrl+C to stop; stall warning after {args.stall} min){c['off']}")
    try:
        while True:
            now = time.time()
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a job on an agent and wait for it")
    r.add_argument("profile", metavar="agent")
    r.add_argument("--prompt-file", required=True, help="task brief file, or - for stdin")
    r.add_argument("--cwd")
    r.add_argument("--mode", choices=["read", "write", "full"], default="write")
    r.add_argument("--preamble", action="append", default=[],
                   help="file to put before the brief, such as a project brief (repeatable)")
    r.add_argument("--effort")
    r.add_argument("--add-dir", action="append", default=[])
    r.add_argument("--resume", metavar="JOB_ID")
    r.add_argument("--wait", type=int, default=DEFAULT_WAIT_S)
    r.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES)
    r.set_defaults(fn=cmd_run)

    w = sub.add_parser("wait")
    w.add_argument("job")
    w.add_argument("--wait", type=int, default=DEFAULT_WAIT_S)
    w.set_defaults(fn=cmd_wait)

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

    rn = sub.add_parser("_runner")
    rn.add_argument("jobdir")
    rn.set_defaults(fn=cmd_runner)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
