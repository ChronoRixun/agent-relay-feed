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
    dict(name="gpt-sol-xhigh", profile="gpt-sol-med", effort="xhigh", relay_model="sonnet"),
    dict(name="gpt-terra-med", profile="gpt-terra-med"),
    dict(name="gpt-terra-high", profile="gpt-terra-high"),
    dict(name="kimi-k28", profile="kimi-k28"),
    dict(name="kimi-k3", profile="kimi-k3"),
    dict(name="kimi-k31-pro", profile="kimi-k31-pro"),
    dict(name="claude-api-opus", profile="claude-api-opus"),
    dict(name="claude-api-sonnet", profile="claude-api-sonnet"),
    dict(name="claude-api-haiku", profile="claude-api-haiku"),
    # Nous Portal models: other model families, billed to Nous credits
    dict(name="nous-deepseek-pro", profile="nous-deepseek-pro",
         about="DeepSeek V4 Pro: strong, cheap coding model from another family; can drift on very long tool "
               "chains and write plausible-but-wrong code, so ask for tests. Good for implementation, scripts "
               "and second opinions; not the hardest reverse engineering."),
    dict(name="nous-deepseek-flash", profile="nous-deepseek-flash",
         about="DeepSeek V4.1 Flash: very cheap workhorse for bulk and routine work (gate runs, scripted chores, "
               "log triage, simple fixes)."),
    dict(name="nous-mimo-pro", profile="nous-mimo-pro",
         about="Xiaomi MiMo V2.6 Pro: long agentic investigations at very low cost (cached input is almost free); "
               "slow, and prone to repeating searches, so give it clear stop points."),
    dict(name="nous-glm", profile="nous-glm",
         about="Z.ai GLM 5.3: capable long-horizon coding model; a different-family second opinion on "
               "implementation work."),
    dict(name="nous-glm-flash", profile="nous-glm-flash",
         about="Z.ai GLM 5.3 Flash: the cheap GLM, roughly a seventh of GLM 5.3's price; routine coding, chores "
               "and quick second looks where full GLM 5.3 would be overkill."),
    dict(name="nous-gemini-argon", profile="nous-gemini-argon",
         about="Google Gemini 4 Argon: frontier reasoning from a lab not otherwise used; the expensive tier, so "
               "use it for reviews of plans, diffs and conclusions, not long builds."),
]

SANDBOX_NOTE = {
    "claude-api": ("`read` uses Claude Code plan mode; `write` uses acceptEdits for the working directory "
                   "and authorized --add-dir folders. Shell commands can require permission and fail in headless mode. "
                   "These are tool permission rules, not an OS sandbox. `full` bypasses permissions. "
                   "Use ONLY when the owner has approved this specific run; pass --approved with the approval note. "
                   "Copy any owner-specified --max-usd cap. Every follow-up needs its own approval. "
                   "Never invent an approval or read, create or copy API keys; the runner loads the configured key file."),
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
{command} run {profile}{effort_flag} --name "<3-6 word title of the task>" --prompt-file "<brief path>" --cwd "<working directory>" --mode <mode>
```

- `--name`: a short title for the task in your own words (it is what the owner sees on the status board and in alerts).
- `--cwd`: the project directory the caller named. If none was named, use your own current working directory.
- `--mode`: `read` if the caller asked for analysis, review or investigation only, or said not to change files. `write` for normal work (the default). `full` only if the caller explicitly asks for unrestricted access. {sandbox_note}
- `--preamble "<file>"`: once for each project brief or role file the caller wants placed before the task.
- `--add-dir "<dir>"`: once for each extra directory the caller says may be written.
- `--effort <effort>`: {effort_note} Only change effort if the caller explicitly asks.
- **Follow-ups:** if the caller sends you more instructions after an earlier job, write the new message to a new brief file and add `--resume <job id>` using the JOB id from your previous run. This continues the same conversation with the model.

## 3. Wait until it finishes

The output starts with `STARTED: <job id>` and then a `STATUS:` line.

- If `STATUS: running`, make ONE call: `{command} wait <job id> --wait 43200` with the Bash tool's `run_in_background: true` (no timeout needed). Then stop and say nothing more; you are woken once when that command exits. On waking, read its output; if the status is still `running`, make the same single background call again. Never poll with short foreground waits: each wake-up costs the caller tokens and posts noise in their chat.
- Never start a second copy of the same job. If a Bash call times out or its output is cut off, run `{command} list` to find the job id, then `wait` on it.
- Only run `{command} cancel <job id>` if the caller asks you to stop.

## 4. Return the result

Reply with:

1. One line: `{name} | job <job id> | <status> | <duration> | session <session id or none>`
2. The text between `----- RESULT -----` and `----- END RESULT -----`, **verbatim**. Do not paraphrase, trim, reformat or add commentary, and do not check the model's claims; the caller will judge them.
3. If the status is not `done`, include the ERRORS and STDERR lines exactly as printed.
4. The `REPORT FILE`, `PROGRESS NOTES` and `ACTIVITY LOG` paths exactly as printed, so the caller can find them later.
"""

OVERRIDES = "agent-overrides.json"
FRONT = re.compile(r"^(description|model):[ \t]*(.*)$", re.M)


def overrides_path(home):
    return Path(home) / ".claude" / "delegate" / OVERRIDES


def load_overrides(home):
    """The owner's own description / relay model per relay name; they survive reinstalls."""
    try:
        data = json.loads(overrides_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def front_matter(text):
    """The description and model lines of an installed definition's YAML header."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    head = text[4:text.index("\n---\n", 4)]
    out = {}
    for key, value in FRONT.findall(head):
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, str) and value:
            out[key] = value
    if "model" in out and not re.fullmatch(r"[\w.\[\]-]+", out["model"]):
        del out["model"]
    return out


def generate(spec, home=None, delegate=None, python=None, *, plugin=False):
    """Render one definition, including a checksum of all generated content."""
    home = Path.home() if home is None else Path(home)
    own = {} if plugin else load_overrides(home).get(spec["name"], {})
    delegate = DELEGATE if delegate is None else Path(delegate)
    python = Path(sys.executable) if python is None else Path(python)
    prof = PROFILES[spec["profile"]]
    effort = spec.get("effort", prof["effort"])
    model_line = (f"{prof['label']} from Nous Portal, running in the Kimi CLI" if prof.get("provider") == "nous"
                  else f"{prof['model']} at {effort} effort, running in the {prof['backend']} CLI")
    description = (f"External worker: {model_line}. Give it a self-contained brief with goal, "
                   "working directory, paths, evidence, constraints, permitted edits and the report you want. "
                   "Say 'read-only' for analysis. Send follow-ups to the same agent to continue its session.")
    if spec.get("about"):
        description = spec["about"] + " " + description
    if prof["backend"] == "kimi":
        description += " Kimi runs unsandboxed without approval prompts."
    if prof.get("provider") == "nous":
        description += (" Billed to Nous Portal credits (small monthly budget) with a per-job spend cap; "
                        "pass --max-usd only if the caller sets one.")
    if prof["backend"] == "claude-api":
        description += " Use ONLY when the owner has approved this specific run; pass --approved with the approval note. Billed to Anthropic API credits."
    if isinstance(own.get("description"), str) and own["description"].strip():
        description = " ".join(own["description"].split())
    relay_model = own.get("model") if isinstance(own.get("model"), str) else None
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
        description_yaml=json.dumps(description),
        relay_model=relay_model or spec.get("relay_model", "haiku"),
        inbox="<resolved inbox>" if plugin else (home / ".claude" / "delegate" / "inbox").as_posix(),
        command=command, brief_setup=brief_setup,
        effort_flag=((f" --effort {effort}" if "effort" in spec else "") +
                     (' --approved "<owner approval: who/when/why>"' if prof["backend"] == "claude-api" else "")),
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
    ap.add_argument("--keep-description", action="store_true",
                    help="before replacing a file, save its own description and relay model to "
                         f"~/.claude/delegate/{OVERRIDES}, so this and later installs keep them")
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
        if exists and args.keep_description:
            theirs = front_matter(old)
            ours = front_matter(generate(spec, home=home, delegate=delegate))
            kept = {k: v for k, v in theirs.items() if v != ours.get(k)}
            if kept:
                saved = load_overrides(home)
                saved[spec["name"]] = {**saved.get(spec["name"], {}), **kept}
                target = overrides_path(home)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                print(f"kept {' and '.join(sorted(kept))} of {spec['name']} in {target}")
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
