# Agent Relay Feed

Spawn agents and keep an eye on your background tasks in Claude Code.

Run external coding agents from any Claude Code session, or directly from a terminal. A thin relay passes your brief to Codex CLI or Kimi CLI, waits for the job, and returns its report verbatim. Jobs run in a separate runner process with progress notes, activity logs, resumable sessions, cancellation, and a local read-only feed.

## Requirements

- Python 3.10 or newer; no third-party Python dependencies.
- Claude Code for the relay agents. Direct CLI use does not require Claude Code.
- Codex CLI and/or Kimi CLI installed and logged in, with access to the model IDs you configure. You only need the backend you use. See the [Codex CLI documentation](https://developers.openai.com/codex/cli) for its setup; follow your Kimi CLI distribution's installation and login instructions.

Windows, macOS and Linux are supported by the bridge. Codex sandbox support depends on your platform and CLI version; see [Codex security](https://developers.openai.com/codex/security). This checkout includes model/profile names chosen by its maintainer; they are configuration, not a guarantee of provider availability.

## Install as a plugin

Add this repository as a plugin marketplace, then install the plugin:

```sh
claude plugin marketplace add ChronoRixun/agent-relay-feed
claude plugin install agent-relay-feed@agent-relay-feed
```

From a local checkout instead, run the first command from the folder that contains the checkout and give it a
relative path (absolute Windows paths are rejected by the marketplace command):

```sh
claude plugin marketplace add ./agent-relay-feed
```

For a session-only preview, run `claude --plugin-dir ./agent-relay-feed`.

Start a new Claude Code session after installing. Plugin agents are namespaced,
for example `agent-relay-feed:gpt-sol-high`; manual agents have the unprefixed
names in the table below. You do not need to run the manual installer for the
plugin. Use `/agent-relay-feed:feed` to start the local feed and open its URL, or
ask it to start without opening a browser. `/agent-relay-feed:delegate` describes
profile selection, briefs, waiting, follow-ups and access limits.

The seven plugin agents are generated from the same template as the manual
installer. They use the documented `${CLAUDE_PLUGIN_ROOT}` substitution in their
Markdown bodies, which resolves to the installed plugin's absolute path. They
resolve your home directory at runtime and store job data outside the plugin
cache, in the existing `~/.claude/delegate/` layout. Plugin updates therefore do
not invalidate hardcoded paths or delete job data. Use a current Claude Code
release with plugin skills and plugin-root substitution in agent content. Older
releases may validate manifests without supporting all runtime behavior; the
manual installer is the fallback. No exact minimum version has been established.

Official references: [plugins overview](https://code.claude.com/docs/en/plugins),
[components](https://code.claude.com/docs/en/plugins/components),
[plugin manifest and path variables](https://code.claude.com/docs/en/plugins-reference#environment-variables),
and [marketplaces](https://code.claude.com/docs/en/plugin-marketplaces).

## Install manually

```sh
git clone <repository-url> agent-relay-feed
cd agent-relay-feed
python install_agents.py
python install_agents.py --list
```

Use `python3` instead of `python` if that is your Python 3 command. The installer records the current Python interpreter and this checkout's absolute location, and writes user-scoped definitions to `~/.claude/agents/`. Keep the checkout in place. Restart Claude Code after installation; user agents are available across projects ([Claude Code subagents](https://code.claude.com/docs/en/sub-agents)).

```sh
python install_agents.py --only gpt-sol-med gpt-sol-high
python install_agents.py --only kimi-k28,kimi-k3
```

All seven relay variants come from one template:

| Agent tool name | Delegate profile | Effort |
|---|---|---|
| `gpt-astra-med` | `gpt-astra-med` | medium |
| `gpt-astra-high` | `gpt-astra-high` | high |
| `gpt-astra-xhigh` | `gpt-astra-xhigh` | xhigh |
| `gpt-sol-med` | `gpt-sol-med` | medium |
| `gpt-sol-high` | `gpt-sol-med` | high override |
| `kimi-k28` | `kimi-k28` | max |
| `kimi-k3` | `kimi-k3` | high |

Generated files carry a marker and SHA-256 checksum. Reinstalling updates intact generated files, but preserves modified or unmarked files and returns exit code 1. Review those files before using `--force`. This also means definitions made by an older installer need `--force` to migrate. After moving the checkout or Python interpreter, rerun the installer.

## Usage in Claude Code

Ask Claude to use the Agent tool with the installed name, for example:

```text
Agent(subagent_type="agent-relay-feed:gpt-sol-high", prompt="Review the parser in the current working directory. Read-only. Return findings with file paths and line numbers.")
```

For a manual install use `gpt-sol-high` instead of the plugin-prefixed name.

Give each relay a self-contained brief: goal, working directory, relevant paths and evidence, permitted changes, constraints, and the final report you want. The relay writes your instructions verbatim into `~/.claude/delegate/inbox/`, starts the job, waits until it ends, and returns the result unchanged. Send follow-ups to the same relay to resume its external session.

## Direct CLI

From the checkout (or use an absolute path to `delegate.py` from another directory):

```sh
python delegate.py profiles
python delegate.py run gpt-astra-high --prompt-file brief.md --cwd /path/to/project --mode read
python delegate.py run gpt-sol-med --effort high --prompt-file brief.md --cwd /path/to/project --mode write
python delegate.py wait <job_id>
python delegate.py status <job_id>
python delegate.py run gpt-sol-med --resume <job_id> --prompt-file followup.md
python delegate.py list
python delegate.py cancel <job_id>
```

On Windows, pass your project directory as `--cwd`; forward slashes are accepted. `run` and `wait` return after at most 540 seconds by default (`--wait SECONDS`). A `STATUS: running` job continues in the background: keep calling `wait`, rather than starting a duplicate. When calling through Claude's Bash tool, give the tool a timeout of 600000 milliseconds. Jobs have a default time limit of 180 minutes (`--max-minutes N`).

`--preamble FILE` prepends a brief or role file and may be repeated. `--add-dir DIR` adds permitted write directories and may be repeated. `--prompt-file -` reads stdin. `--effort VALUE` overrides the profile default. Resume requires a previous job with a session ID and the same backend; it inherits the previous working directory unless `--cwd` overrides it.

Codex is discovered from the Windows desktop install or `codex` on PATH. Set `DELEGATE_CODEX` to the executable path to override discovery. Kimi is discovered from `kimi` on PATH; on Windows the bridge prefers the npm package's `main.mjs` through Node to preserve multiline prompts. `DELEGATE_KIMI_MAIN` overrides that module path. CLI authentication and other settings remain those of your installed tools.

On Windows, sandboxed Codex jobs remove WindowsApps entries from their child PATH to avoid Store shell alias failures. The runner tries to leave the caller's Windows job object; if that fails, the report warns that killing the caller's process tree can stop the job. On macOS/Linux the runner starts a new session. In Codex read mode, progress-file writes may be blocked; activity is still logged.

## Feed and watch

```sh
python feed.py
python feed.py --open
python feed.py --port 9000 --hours 24
python feed.py --dump --hours 1
python delegate.py watch
python delegate.py watch <job_id> --stall 10 --loop 6 --no-color
```

The feed binds to `127.0.0.1:8765` by default. Open that URL in your browser, or use `--open` to open your default browser. It shows running cards and jobs finished in the last 12 hours (`--hours N`). Hover steps for the recorded command; expand cards for the timeline, progress notes and result. `--dump` prints the same snapshot as JSON without starting a server or opening a browser, even with `--open`. The terminal watch follows running and newly started jobs; supply job IDs to follow only those jobs.

Badges and indicators:

- Green dot: a running job.
- Model chip: model label with the effort actually used; Kimi keeps its profile label.
- Read-only / full-access chip: selected execution mode; write is the default.
- Quiet: no activity or recent working-directory file writes for the stall threshold. A long operation may still be healthy.
- Possible loop: the same recorded command repeated at least six times in the last ten commands. Repeats averaging 30 seconds apart or more count as polling and suppress this warning.
- Reconnects: connection retry messages observed in the log.
- Errors / non-zero exits: recorded errors or failed command exits, which may be recoverable.
- Finished icon: done, failed, timeout, cancelled or lost (runner missing without a final status).
- Optional process pills: watched executable, matching job or configured owner; unknown means attribution failed. Green means no watched process was found, not that the whole computer is idle.

The process panel is hidden without configuration. Process discovery uses PowerShell/CIM on Windows and `ps` on macOS/Linux, with `/proc` executable paths when available on Linux. Missing tools, permission restrictions and unavailable paths make discovery or ownership best effort. It never launches or stops watched processes.

> Screenshot placeholder: add a screenshot using synthetic demo jobs before publishing. Do not include private work.

## Optional local config

Create `~/.claude/delegate/config.json` yourself. No config is needed. The bridge never creates or overwrites it. Settings are loaded when each CLI/feed process starts; restart the feed after editing. Invalid JSON or invalid fields produce a warning and retain defaults.

Generic example (adapt the paths and executable names):

```json
{
  "path_shortening": [["/work/tasks", "tasks/"], ["/work", "W/"]],
  "watched_processes": [
    {
      "name": "editor.exe",
      "owner": "unknown",
      "owners": [{"path_patterns": ["/personal/editor"], "owner": "you"}]
    }
  ],
  "feed_port": 8765,
  "stall_seconds": 480,
  "loop_count": 6,
  "polling_seconds": 30,
  "job_path_roots": ["/work/tasks"],
  "activity_ignore_paths": ["/work/tasks"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `path_shortening` | `[]` | Pairs of path prefix and display replacement; longest prefix wins, either slash style is accepted. Home always shortens to `~`. |
| `watched_processes` | `[]` | Executable names matched without case sensitivity; an empty list hides the panel. |
| `feed_port` | `8765` | Loopback HTTP port (1–65535); `--port` overrides it. |
| `stall_seconds` | `480` | Activity silence threshold; watch's `--stall` overrides it in minutes. |
| `loop_count` | `6` | Repetition threshold in the last ten commands (2–10); watch's `--loop` overrides it. |
| `polling_seconds` | `30` | Minimum average interval that treats repetitions as polling. |
| `job_path_roots` | `[]` | Optional shared work roots: the first folder below a root is matched to a job's working-directory components. |
| `activity_ignore_paths` | `[]` | Exact shared directories to exclude from activity scans and broad process ownership matches. |

Each watched-process entry has `name`, optional fallback `owner`, and optional `owners`: an ordered list of `{path_patterns: [...], owner: "label"}` rules. Path patterns are literal substrings, with case and slash style ignored. A process inside a running job's working directory is attributed to that job first (longest directory wins), then shared-root matching applies. Optional `job_cwd_patterns: [...]` matches literal substrings in running job directories when an executable lives elsewhere; use only when that association is intentional. Then the first matching owner rule wins, followed by the fallback owner (`unknown` by default).

## Profiles

Edit `PROFILES` near the top of `delegate.py` to change provider model IDs or add a direct-CLI profile. Each entry needs `backend` (`codex` or `kimi`), `model`, `effort` and a display `label`. For example:

```python
"reviewer": dict(backend="codex", model="your-model-id", effort="high", label="Reviewer (high)")
```

To expose it as a relay in Claude Code, add `dict(name="reviewer", profile="reviewer")` to `install_agents.py`'s `SPECS`, then rerun the installer. For the plugin, run `python build_plugin.py` and include the regenerated `agents/` files. A spec may set `effort` to use one profile with a different effort, as `gpt-sol-high` does. Model IDs and supported effort values depend on your backend/account; the bridge passes them through.

## Safety

`~/.claude/delegate/agents/<profile>/` contains `progress/<job_id>.md`, `activity/<job_id>.log`, `reports/<job_id>.md` and `latest-report.md`. `~/.claude/delegate/jobs/<job_id>/` contains the prompt, metadata, command, events, stderr, result and status. The high-effort Sol relay uses the `gpt-sol-med` folder because that is its delegate profile. `DELEGATE_JOBS_DIR` and `DELEGATE_AGENTS_DIR` can override the two storage roots.

- Codex `read`: read-only sandbox. Codex `write`: workspace-write sandbox for the working directory, extra directories and progress folder, with network enabled. Codex `full`: bypasses sandbox and approval prompts; use only when explicitly authorized.
- Kimi's prompt mode runs without approvals or sandbox enforcement. Its `read` and `write` modes are instructions in the brief, not filesystem protection.
- Every brief instructs the worker not to commit, push, publish, send, launch games or drive the desktop unless the brief explicitly requests it. These instructions are not a network security boundary. Always state your constraints and review the returned work.
- Relays return reports without checking the worker's claims. CLI calls may consume provider quota or incur costs under your account.
- Logs and briefs may contain sensitive project data. The feed is local and read-only, but anyone able to access its loopback port can read the displayed data. Do not expose it publicly or commit job logs.

## Uninstall

For a plugin install:

```sh
claude plugin uninstall agent-relay-feed@agent-relay-feed
claude plugin marketplace remove agent-relay-feed
```

Stop any running feed background task separately. Plugin uninstall leaves this
tool's job history and local config in `~/.claude/delegate/`; archive or remove
those yourself when you no longer need them. It does not remove manually
installed relays or external CLIs. For a manual install:

```sh
python install_agents.py --uninstall
python install_agents.py --uninstall --only kimi-k28 kimi-k3
```

Uninstall removes intact generated relay definitions only. Modified/unmarked files require explicit `--force`. Job history, config and the checkout remain; archive or remove them yourself after cancelling any running jobs. The installer does not uninstall external CLIs.

## Development and validation

```sh
python -m unittest
python build_plugin.py --check
claude plugin validate .claude-plugin/plugin.json
claude plugin validate .claude-plugin/marketplace.json
```

`build_plugin.py --check` detects missing, stale and unexpected generated agent
files. Manifest validation checks packaging, not provider login/model access or
runtime path expansion. Before publishing, load the plugin in a current Claude
Code session and smoke-test one authorized read-only task and the feed command.

Tests use only the standard library and temporary directories inside the checkout. The source filenames and `~/.claude/delegate/` storage layout remain unchanged for compatibility.

## License

Licensed under [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. Copyright 2026 ChronoRixun.
