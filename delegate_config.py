# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Optional, local presentation settings shared by the CLI and feed."""
import copy
import json
import math
import re
import sys
from pathlib import Path

import baseline

CONFIG_PATH = Path.home() / ".claude" / "delegate" / "config.json"
DEFAULTS = dict(path_shortening=[], watched_processes=[], feed_port=8765,
                # Address the feed listens on. Loopback by default; "0.0.0.0" shows the (read-only,
                # unauthenticated) feed to every device on the local network.
                feed_host="127.0.0.1",
                # How long one `ask` command waits for an answer before telling the worker to run
                # it again. Keep it under the longest time the agent's shell tool lets a command run.
                ask_wait_seconds=240,
                # Nous Portal models (see nous.py): key file, a monthly budget that stops new jobs once
                # reached (0 = none), and the default spend cap of one job (run --max-usd overrides it).
                nous_api_key_file="~/.claude/delegate/nous-api-key.txt", nous_monthly_usd=0, nous_max_usd_per_job=3.0,
                # Windows: when the caller's job object refuses breakaway, start runners through WMI so
                # they live outside it (and survive the caller's process tree being killed).
                detach_via_wmi=True,
                # After a job, flag commands that usually need the owner's say-so (see results.guard).
                # Extra rules: [{"pattern": "<regex>", "text": "<what it means>"}].
                guard_enabled=True, guard_patterns=[],
                # Folders any unsandboxed job may write besides its own (see results.outside_writes).
                guard_allow_dirs=[],
                # Monthly budget for Claude API jobs in USD, shown beside the month's spend (0 = no budget).
                claude_api_monthly_usd=0,
                # Minutes before a job's time limit at which the worker is told to wrap up (0 = no warning).
                checkpoint_minutes=10,
                # Queued jobs (run --after) start only while fewer than this many relay jobs run.
                queue_max_running=3,
                # How many times a job that died of a connection error is resumed on its own (0 = never).
                auto_resume=1,
                # Alerts when a job needs an answer or ends (see notify.py); off while `notify` is empty.
                notify=[], notify_on=["question", "failed", "flagged", "done"], notify_detail=True,
                notify_link="",
                # PIN that lets the feed page answer a worker's question or send it a message.
                # Empty = the feed stays read-only.
                feed_pin="",
                # Things on this machine that agents must not use at the same time (see leases.py):
                # name -> {"slots": N, "values": [one per slot], "yield_to": [process names],
                #          "rule": "what a worker needs the slot for"}.
                resources={},
                stall_seconds=480, loop_count=6, polling_seconds=30,
                job_path_roots=[], activity_ignore_paths=[],
                # Relay jobs load only these MCP servers and plugins from the user's Codex
                # config; everything else (desktop control, messaging, account tools) is
                # switched off so a background agent cannot drive the real mouse and
                # keyboard or act outside its brief.
                codex_mcp_allow=[], codex_plugins_allow=[],
                # Milliseconds a Codex script may run before it hands back to the model unasked
                # (Codex's own default is 30000). 0 leaves Codex's default alone. A larger value
                # saves model requests while a worker waits on `ask` or on a long command.
                codex_exec_yield_ms=0,
                claude_api_key_file="~/.claude/delegate/anthropic-api-key.txt",
                claude_api_mcp_allow=[], claude_api_mcp_servers={},
                # Native Claude Code subagents (Agent tool) shown beside relay jobs: transcripts under
                # the projects root changed within the window; an optional list of substrings keeps
                # only matching project folders or working directories.
                claude_agents_enabled=True, claude_projects_root="~/.claude/projects",
                claude_agents_window_hours=24, claude_agents_projects=[])


def _strings(value):
    return isinstance(value, list) and all(isinstance(s, str) and s for s in value)


def _valid(key, value):
    if key in ("feed_port", "loop_count"):
        return type(value) is int and (1 <= value <= 65535 if key == "feed_port" else 2 <= value <= 10)
    if key == "notify":
        return isinstance(value, list) and all(
            isinstance(t, dict) and t.get("kind") in ("ntfy", "discord", "json")
            and isinstance(t.get("url"), str) and re.match(r"https?://", t["url"]) for t in value)
    if key == "notify_on":
        return _strings(value) and set(value) <= {"question", "failed", "flagged", "done"}
    if key == "notify_detail":
        return type(value) is bool
    if key == "notify_link":
        return isinstance(value, str)
    if key == "feed_pin":
        return isinstance(value, str) and (value == "" or len(value) >= 4)
    if key in ("queue_max_running", "auto_resume"):
        return type(value) is int and (1 <= value <= 16 if key == "queue_max_running" else 0 <= value <= 3)
    if key == "codex_exec_yield_ms":
        return type(value) is int and 0 <= value <= 3600000
    if key == "claude_agents_enabled":
        return type(value) is bool
    if key in ("nous_monthly_usd", "nous_max_usd_per_job"):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    if key in ("claude_projects_root", "claude_api_key_file", "feed_host", "nous_api_key_file"):
        return isinstance(value, str) and bool(value.strip())
    if key == "claude_api_mcp_servers":
        return isinstance(value, dict) and all(isinstance(k, str) and k and isinstance(v, dict)
                                              for k, v in value.items())
    if key == "detach_via_wmi":
        return type(value) is bool
    if key == "guard_enabled":
        return type(value) is bool
    if key == "guard_patterns":
        return isinstance(value, list) and all(isinstance(g, dict) and isinstance(g.get("pattern"), str) and g["pattern"]
                                               and isinstance(g.get("text"), str) and g["text"] for g in value)
    if key == "claude_api_monthly_usd":
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    if key == "checkpoint_minutes":
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 120
    if key in ("stall_seconds", "polling_seconds", "claude_agents_window_hours", "ask_wait_seconds"):
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    if key in ("job_path_roots", "activity_ignore_paths", "codex_mcp_allow", "codex_plugins_allow", "guard_allow_dirs",
               "claude_agents_projects", "claude_api_mcp_allow"):
        return _strings(value)
    if key == "resources":
        return isinstance(value, dict) and all(
            isinstance(name, str) and re.fullmatch(r"[\w.-]+", name) and not name.startswith("_")
            and isinstance(spec, dict)
            and type(spec.get("slots", 1)) is int and 1 <= spec.get("slots", 1) <= 64
            and (spec.get("baseline") is None or baseline.valid(spec["baseline"]))
            and (spec.get("values") is None or (_strings(spec["values"]) and len(spec["values"]) == spec.get("slots", 1)))
            and (spec.get("yield_to") is None or _strings(spec["yield_to"]))
            and isinstance(spec.get("rule", ""), str)
            for name, spec in value.items())
    if key == "path_shortening":
        return isinstance(value, list) and all(isinstance(p, list) and len(p) == 2 and
                all(isinstance(s, str) and s for s in p) for p in value)
    if key == "watched_processes":
        return isinstance(value, list) and all(isinstance(p, dict) and
                isinstance(p.get("name"), str) and p["name"] and
                isinstance(p.get("owner", "unknown"), str) and
                _strings(p.get("job_cwd_patterns", [])) and
                isinstance(p.get("owners", []), list) and all(
                    isinstance(r, dict) and _strings(r.get("path_patterns", [])) and
                    bool(r.get("path_patterns")) and isinstance(r.get("owner"), str)
                    for r in p.get("owners", [])) for p in value)
    return False


def load_config(path=None):
    """Missing config is normal; invalid fields warn and retain their defaults."""
    config = copy.deepcopy(DEFAULTS)
    path = Path(path) if path is not None else CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring config {path}: {exc}", file=sys.stderr)
        return config
    for key, value in data.items():
        if key in DEFAULTS and _valid(key, value):
            config[key] = value
        else:
            print(f"warning: ignoring config field {key!r}", file=sys.stderr)
    return config


CONFIG = load_config()


_WORKER = re.compile(r"""delegate\.py\\?["']?\s+(phase|ask|check|lease|release)\b(.*)$""", re.S)
_WORKER_OPT = re.compile(r"""--(?:job|wait|as|count)[=\s]+\\?["']?[\w.-]+\\?["']?""")
_WORKER_COUNT = re.compile(r"""--count[=\s]+\\?["']?(\d{1,2})\b""")
QUESTION_CHARS = 600


def worker_command(text):
    """(verb, text) for a worker's `delegate.py phase|ask|check ...` command line, else (None, None).

    Workers talk to the bridge by running those commands; the bridge reads them back out of the
    agent's own event stream, so it works in a read-only sandbox and even if the command fails.
    The text may still carry a shell wrapper or JSON escaping around it."""
    m = _WORKER.search(str(text or ""))
    if not m:
        return None, None
    verb = m.group(1)
    rest = _WORKER_OPT.sub(" ", m.group(2)).replace('\\"', '"').strip()
    quote = rest[:1]
    if quote in ("'", '"'):
        # a stage name ends at its first closing quote (other commands may follow); a question may
        # contain quotes of its own, so it runs to the last one
        end = rest.find(quote, 1) if verb == "phase" else rest.rfind(quote)
        rest = rest[1:end] if end > 0 else rest[1:]
        rest = rest.rstrip(quote)
    else:
        rest = re.split(r"[;|&\"'}]", rest)[0]
    rest = " ".join(rest.split())
    if verb in ("lease", "release"):        # the resource name: one bare word
        word = re.match(r"[\w.-]+", rest)
        return verb, word.group(0) if word else None
    return verb, rest[:120 if verb == "phase" else QUESTION_CHARS] or None


def lease_count(text):
    """How many slots a worker's `delegate.py lease ... --count N` command line asks for (1 if unsaid)."""
    m = _WORKER.search(str(text or ""))
    n = _WORKER_COUNT.search(m.group(2)) if m else None
    return max(1, int(n.group(1))) if n else 1


def phase_from_command(text):
    """The stage name from a worker's `delegate.py phase "<name>"` command line, else None."""
    verb, name = worker_command(text)
    return name if verb == "phase" else None


# Errors that mean the connection to the model failed, not the work: worth one automatic resume.
TRANSIENT = re.compile(r"connection (?:error|reset|refused|closed|aborted)|unable to connect|ssl|certificate|"
                       r"econnreset|etimedout|socket hang up|stream disconnected|network (?:error|is unreachable)|"
                       r"getaddrinfo|\b50[234]\b|overloaded|temporarily unavailable", re.I)


def normalize_path(path):
    return str(path).replace("\\", "/").rstrip("/")


def ignored_activity_path(folder):
    norm = normalize_path(folder)
    return any(norm.casefold() == normalize_path(p).casefold()
               for p in CONFIG["activity_ignore_paths"])


def shorten_paths(text, home=None, pairs=None):
    pairs = list(CONFIG["path_shortening"] if pairs is None else pairs)
    pairs.append([str(Path.home() if home is None else home), "~"])
    # Longest prefix wins. Match separators in either style, preserving everything else.
    for source, target in sorted(pairs, key=lambda p: -len(p[0])):
        source = normalize_path(source)
        pattern = re.escape(source).replace("/", r"[\\/]")
        flags = re.I if re.match(r"^[a-z]:", source, re.I) else 0
        text = re.sub(pattern + r"(?=[\\/\s'\";]|$)", lambda m: target.rstrip("/\\"), text, flags=flags)
    return text


def repetition(recent, count=None, polling_seconds=None):
    """Return (possible_loop, steady_polling) for up to ten (command, seconds) pairs."""
    if not recent:
        return False, False
    times = [t for cmd, t in recent[-10:] if cmd == recent[-1][0]]
    interval = CONFIG["polling_seconds"] if polling_seconds is None else polling_seconds
    polling = len(times) > 1 and ((times[-1] - times[0]) % 86400) / (len(times) - 1) >= interval
    threshold = CONFIG["loop_count"] if count is None else count
    return len(times) >= threshold and not polling, polling
