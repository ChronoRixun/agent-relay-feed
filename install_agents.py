#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Agent Relay Feed: install, list or remove user-scoped relay definitions."""
import argparse
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path

from delegate import PROFILES

AGENTS = Path.home() / ".claude" / "agents"
DELEGATE = Path(__file__).resolve().parent / "delegate.py"
MARKER = re.compile(r"^<!-- generated-by: (?:agent-relay-feed|claude-delegate) sha256:([0-9a-f]{64}) -->\n", re.M)

# Add a profile in delegate.py, then a relay spec here if it needs an Agent tool name.
SPECS = [
    dict(name="gpt-astra-med", profile="gpt-astra-med"),
    dict(name="gpt-astra-high", profile="gpt-astra-high"),
    dict(name="gpt-astra-xhigh", profile="gpt-astra-xhigh"),
    dict(name="gpt-sol-med", profile="gpt-sol-med"),
    dict(name="gpt-sol-high", profile="gpt-sol-med", effort="high", relay_model="sonnet"),
    dict(name="kimi-k28", profile="kimi-k28"),
    dict(name="kimi-k3", profile="kimi-k3"),
]

SANDBOX_NOTE = {
    "codex": ("`read` uses Codex's read-only sandbox, and `write` allows writes inside `--cwd`, "
              "`--add-dir` folders and the progress folder (network allowed). `full` turns the sandbox off."),
    "kimi": ("Kimi has no sandbox in this mode: commands and edits run without approval prompts. "
             "`read` and `write` are instructions, not enforced limits. Copy the caller's limits exactly."),
}

BODY = """---
name: {name}
description: {description_yaml}
model: {relay_model}
tools: Bash, Read, Write
---

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 ChronoRixun -->

You are a relay to an external coding agent: **{model_line}**. You do not do the task yourself, and you do not plan, research, check or edit anything on your own. You pass the caller's brief to that model, wait for it to finish, and return its answer unchanged.

## 1. Write the brief

{brief_setup}

Use the Write tool to save the caller's request as a Markdown file at `{inbox}/<short-slug>-<4 random letters>.md`.

- Copy the caller's instructions **verbatim**: goal, paths, constraints, things not to touch, and the report they want. Do not summarize, shorten, reinterpret or add your own plan.
- If the caller gave context such as file contents, findings or error messages, include it too.
- If the caller names a project brief or other file to put in front of the task, do not copy it into your brief file; pass it with `--preamble` instead.

## 2. Start the job

Run this with the Bash tool and **always set the Bash timeout to 600000**:

```
{command} run {profile}{effort_flag} --prompt-file "<brief path>" --cwd "<working directory>" --mode <mode>
```

- `--cwd`: the project directory the caller named. If none was named, use your own current working directory.
- `--mode`: `read` if the caller asked for analysis, review or investigation only, or said not to change files. `write` for normal work (the default). `full` only if the caller explicitly asks for unrestricted access. {sandbox_note}
- `--preamble "<file>"`: once for each project brief or role file the caller wants placed before the task.
- `--add-dir "<dir>"`: once for each extra directory the caller says may be written.
- `--effort <effort>`: {effort_note} Only change effort if the caller explicitly asks.
- **Follow-ups:** if the caller sends you more instructions after an earlier job, write the new message to a new brief file and add `--resume <job id>` using the JOB id from your previous run. This continues the same conversation with the model.

## 3. Wait until it finishes

The output starts with `STARTED: <job id>` and then a `STATUS:` line.

- If `STATUS: running`, run `{command} wait <job id>` (Bash timeout 600000). Repeat until the status is `done`, `failed`, `timeout`, `cancelled` or `lost`. Long jobs can take an hour or more; keep waiting.
- Never start a second copy of the same job. If a Bash call times out or its output is cut off, run `{command} list` to find the job id, then `wait` on it.
- Only run `{command} cancel <job id>` if the caller asks you to stop.

## 4. Return the result

Reply with:

1. One line: `{name} | job <job id> | <status> | <duration> | session <session id or none>`
2. The text between `----- RESULT -----` and `----- END RESULT -----`, **verbatim**. Do not paraphrase, trim, reformat or add commentary, and do not check the model's claims; the caller will judge them.
3. If the status is not `done`, include the ERRORS and STDERR lines exactly as printed.
4. The `REPORT FILE`, `PROGRESS NOTES` and `ACTIVITY LOG` paths exactly as printed, so the caller can find them later.
"""

def generate(spec, home=None, delegate=None, python=None, *, plugin=False):
    """Render one definition, including a checksum of all generated content."""
    home = Path.home() if home is None else Path(home)
    delegate = DELEGATE if delegate is None else Path(delegate)
    python = Path(sys.executable) if python is None else Path(python)
    prof = PROFILES[spec["profile"]]
    effort = spec.get("effort", prof["effort"])
    model_line = f"{prof['model']} at {effort} effort, running in the {prof['backend']} CLI"
    description = (f"External worker: {model_line}. Give it a self-contained brief with goal, "
                   "working directory, paths, evidence, constraints, permitted edits and the report you want. "
                   "Say 'read-only' for analysis. Send follow-ups to the same agent to continue its session.")
    if prof["backend"] == "kimi":
        description += " Kimi runs unsandboxed without approval prompts."
    command = ('python "${CLAUDE_PLUGIN_ROOT}/delegate.py"' if plugin else
               f"{shlex.quote(python.as_posix())} {shlex.quote(delegate.resolve().as_posix())}")
    brief_setup = ("First use Bash to resolve and create the inbox:\n\n```sh\n"
                   "python -c \"from pathlib import Path; p = Path.home() / '.claude' / 'delegate' / 'inbox'; "
                   "p.mkdir(parents=True, exist_ok=True); print(p.as_posix())\"\n```\n\n"
                   "Use the printed absolute path below as `<resolved inbox>`. If Python 3 is `python3` "
                   "on this machine, use `python3` in every command. The plugin root in these commands "
                   "is substituted inline by Claude Code; do not read it from the Bash environment."
                   if plugin else "")
    text = BODY.format(
        name=spec["name"], profile=spec["profile"], model_line=model_line,
        description_yaml=json.dumps(description), relay_model=spec.get("relay_model", "haiku"),
        inbox="<resolved inbox>" if plugin else (home / ".claude" / "delegate" / "inbox").as_posix(),
        command=command, brief_setup=brief_setup,
        effort_flag=f" --effort {effort}" if "effort" in spec else "",
        effort_note=f"Always pass `--effort {effort}` for this variant." if "effort" in spec
                    else "Use the profile default unless the caller requests an override.",
        sandbox_note=SANDBOX_NOTE[prof["backend"]])
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Keep YAML frontmatter first; the checksum covers the whole definition except its marker.
    end = text.index("\n---\n", 4) + len("\n---\n")
    return text[:end] + f"<!-- generated-by: agent-relay-feed sha256:{digest} -->\n" + text[end:]


def is_generated(text):
    matches = list(MARKER.finditer(text))
    if len(matches) != 1:
        return False
    match = matches[0]
    body = text[:match.start()] + text[match.end():]
    return hashlib.sha256(body.encode("utf-8")).hexdigest() == match.group(1)


def main(argv=None, *, home=None, delegate=None):
    home = Path.home() if home is None else Path(home)
    agents = home / ".claude" / "agents"
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list available relays and installed state")
    mode.add_argument("--uninstall", action="store_true", help="remove generated relay files; keep job data")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="select names (space or comma separated)")
    ap.add_argument("--force", action="store_true", help="replace/remove even user-modified relay files")
    args = ap.parse_args(argv)
    names = {n for group in (args.only or []) for n in group.split(",")}
    unknown = names - {s["name"] for s in SPECS}
    if unknown:
        ap.error("unknown relay(s): " + ", ".join(sorted(unknown)))
    failed = False
    for spec in SPECS:
        if names and spec["name"] not in names:
            continue
        path = agents / f"{spec['name']}.md"
        # Symlinks must never redirect writes/removals into another location.
        if path.is_symlink():
            print(f"refused symlink: {path}")
            failed = True
            continue
        exists = path.exists()
        old = path.read_text(encoding="utf-8") if exists else ""
        state = "generated" if exists and is_generated(old) else "modified/unmanaged" if exists else "not installed"
        if args.list:
            effort = spec.get("effort", PROFILES[spec["profile"]]["effort"])
            print(f"{spec['name']:16} profile={spec['profile']} effort={effort} [{state}]")
            continue
        if exists and not is_generated(old) and not args.force:
            print(f"kept user-modified/unmanaged file: {path} (use --force to override)")
            failed = True
            continue
        if args.uninstall:
            if exists:
                path.unlink()
                if path.exists():
                    raise OSError(f"removal verification failed: {path}")
                print("removed", path)
            continue
        text = generate(spec, home=home, delegate=delegate)
        if old == text:
            print("unchanged", path)
            continue
        agents.mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "delegate" / "inbox").mkdir(parents=True, exist_ok=True)
        with path.open("w" if exists else "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if path.read_text(encoding="utf-8") != text:
            raise OSError(f"write verification failed: {path}")
        print("wrote", path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
