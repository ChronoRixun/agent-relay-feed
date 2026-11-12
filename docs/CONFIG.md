# Optional local config

[Back to the README](../README.md)

Create `~/.claude/delegate/config.json` yourself. No config is needed. The bridge never creates or overwrites it.
Settings are loaded when each CLI or feed process starts; restart the feed after editing. Invalid JSON or invalid
fields produce a warning and retain defaults.

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
  "activity_ignore_paths": ["/work/tasks"],
  "claude_agents_enabled": true,
  "claude_agents_window_hours": 24,
  "claude_agents_projects": []
}
```

## Feed and display

| Key | Default | Meaning |
|---|---|---|
| `feed_port` | `8765` | HTTP port (1–65535); `--port` overrides it. |
| `feed_host` | `"127.0.0.1"` | Address the feed listens on; `--host` overrides it. `"0.0.0.0"` shows the feed, with no login, to every device on the local network. |
| `feed_pin` | `""` | PIN that lets the pages answer questions, message jobs and mark them reviewed; empty keeps the feed read-only. |
| `path_shortening` | `[]` | Pairs of path prefix and display replacement; longest prefix wins, either slash style is accepted. Home always shortens to `~`. |
| `watched_processes` | `[]` | Executable names matched without case sensitivity; an empty list hides the panel. |
| `stall_seconds` | `480` | Activity silence threshold; watch's `--stall` overrides it in minutes. |
| `loop_count` | `6` | Repetition threshold in the last ten commands (2–10); watch's `--loop` overrides it. |
| `polling_seconds` | `30` | Minimum average interval that treats repetitions as polling. |
| `job_path_roots` | `[]` | Optional shared work roots: the first folder below a root is matched to a job's working-directory components. |
| `activity_ignore_paths` | `[]` | Exact shared directories to exclude from activity scans and broad process ownership matches. |

Each watched-process entry has `name`, optional fallback `owner`, and optional `owners`: an ordered list of
`{path_patterns: [...], owner: "label"}` rules. Path patterns are literal substrings, with case and slash style
ignored. A process inside a running job's working directory is attributed to that job first (longest directory wins),
then shared-root matching applies. Optional `job_cwd_patterns: [...]` matches literal substrings in running job
directories when an executable lives elsewhere; use only when that association is intentional. Then the first matching
owner rule wins, followed by the fallback owner (`unknown` by default).

## Alerts

| Key | Default | Meaning |
|---|---|---|
| `notify` | `[]` | Where to send alerts: a list of `{"kind": "ntfy" \| "discord" \| "json", "url": "..."}`. Empty = no alerts. |
| `notify_on` | all four | Which events alert: `question`, `failed`, `flagged`, `done`. |
| `notify_detail` | `true` | `false` sends only the job's name and the kind of event. |
| `notify_link` | `""` | Address an alert opens, such as your feed's `/mission` page. |

See [FEED](FEED.md#alerts).

## Jobs

| Key | Default | Meaning |
|---|---|---|
| `resources` | `{}` | Shared resources and their slots (`slots`, `values`, `yield_to`, `rule`, `baseline`); see [SLOTS](SLOTS.md). |
| `checkpoint_minutes` | `10` | Minutes before a job's time limit at which the worker is told to wrap up; `0` = no warning. |
| `ask_wait_seconds` | `240` | How long one worker `ask` command waits for an answer before telling the worker to run it again. Keep it under the longest time the agent's shell tool lets one command run. |
| `queue_max_running` | `3` | Queued jobs start only while fewer relay jobs than this are running. |
| `auto_resume` | `1` | How many times a job that died of a connection error is resumed automatically; `0` = never. |
| `guard_enabled` | `true` | Flag installs, publishing commands and read-only violations when a job ends. |
| `guard_patterns` | `[]` | Extra guard rules: `[{"pattern": "<regex>", "text": "<what it means>"}]`. |
| `guard_allow_dirs` | `[]` | Folders every unsandboxed (Kimi or Nous) job may write besides its own, without a flag. |
| `detach_via_wmi` | `true` | Windows: start runners through WMI when the caller's job object refuses breakaway; see [INTERNALS](INTERNALS.md#the-detached-runner). |

## Backends

| Key | Default | Meaning |
|---|---|---|
| `codex_exec_yield_ms` | `0` | Milliseconds a Codex script may run before handing back to the model unasked (Codex's default is 30000); `0` leaves it alone. A larger value saves model requests while a worker waits. |
| `codex_mcp_allow` | `[]` | MCP server names from your Codex config that relay jobs may load; every other server is switched off. Add `codex_apps` to keep ChatGPT app connectors. |
| `codex_plugins_allow` | `[]` | Codex plugin names (as written in your Codex config, for example `name@source`) that relay jobs may load; every other plugin is switched off. |
| `claude_api_key_file` | `~/.claude/delegate/anthropic-api-key.txt` | API key file; missing or empty refuses the run. |
| `claude_api_mcp_allow` | `[]` | Explicit Claude API MCP server names, e.g. `["docs"]`. |
| `claude_api_mcp_servers` | `{}` | Claude MCP definitions by name; only allowlisted entries load. |
| `claude_api_monthly_usd` | `0` | Monthly Claude API budget shown beside the month's spend; `0` = no budget. |
| `nous_api_key_file` | `~/.claude/delegate/nous-api-key.txt` | File holding only your Nous Portal API key. |
| `nous_max_usd_per_job` | `3.0` | Spend cap of one Nous job unless `run --max-usd` sets another; `0` = no cap. |
| `nous_monthly_usd` | `0` | Nous Portal budget for the calendar month; new Nous jobs are refused once it is reached. `0` = no budget. |

## Claude Code subagents

| Key | Default | Meaning |
|---|---|---|
| `claude_agents_enabled` | `true` | Show native Claude Code subagents in the feed, `board` and `watch`. |
| `claude_projects_root` | `~/.claude/projects` | Where Claude Code keeps session transcripts. |
| `claude_agents_window_hours` | `24` | Only subagent transcripts changed within this many hours are read. |
| `claude_agents_projects` | `[]` | Optional substrings; when set, only subagents whose project folder or working directory contains one are shown (case and slash style ignored). |

## Environment variables

| Variable | Meaning |
|---|---|
| `DELEGATE_JOBS_DIR`, `DELEGATE_AGENTS_DIR` | Override the two storage roots ([INTERNALS](INTERNALS.md#where-jobs-are-kept)). |
| `DELEGATE_CODEX` | Path to the Codex executable. |
| `DELEGATE_KIMI_MAIN` | Path to the Kimi npm package's `main.mjs` (Windows). |
| `DELEGATE_CLAUDE` | Path to the Claude Code executable for Claude API jobs. |
