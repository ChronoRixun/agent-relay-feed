# Backends and profiles

[Back to the README](../README.md)

A *profile* names a backend, a model, an effort and a label. `python delegate.py profiles` lists them and shows whether
each backend's CLI was found. You only need the backends you use.

- [Codex](#codex) · [Kimi](#kimi) · [Nous Portal](#models-from-nous-portal) ·
  [Claude API credits](#claude-via-prepaid-api-credits) · [Access modes](#access-modes) ·
  [The relay agents](#the-relay-agents) · [Adding a profile](#adding-a-profile)

## Codex

Install the [Codex CLI](https://developers.openai.com/codex/cli) and sign in. Codex is discovered from the Windows
desktop install or `codex` on PATH; set `DELEGATE_CODEX` to the executable path to override discovery. Sandbox support
depends on your platform and CLI version; see [Codex security](https://developers.openai.com/codex/security).

Codex relay jobs do not inherit your Codex tools: every MCP server and plugin in your Codex config
(`$CODEX_HOME/config.toml`, default `~/.codex/config.toml`) is switched off for the job with
`-c <kind>.<name>.enabled=false`, and ChatGPT app connectors with `-c features.apps=false`, unless the name is listed
in `codex_mcp_allow` / `codex_plugins_allow`. This keeps a background agent away from desktop control, messaging and
account tools you use interactively. Names containing a dot cannot be addressed by `codex -c` and stay as configured.
Kimi jobs are unaffected.

`codex_exec_yield_ms` sets how long a Codex script may run before it hands back to the model unasked (Codex's own
default is 30000 ms). A larger value saves model requests while a worker waits on `ask`, `lease` or a long command.

A sandboxed Codex worker cannot write the lease file, so its runner takes and returns slots for it
([SLOTS](SLOTS.md)). On Windows, sandboxed Codex jobs remove WindowsApps entries from their child PATH to avoid Store
shell alias failures.

## Kimi

Install the Kimi CLI and sign in, following your distribution's instructions. Kimi is discovered from `kimi` on PATH;
on Windows the bridge prefers the npm package's `main.mjs` through Node to preserve multiline prompts, and
`DELEGATE_KIMI_MAIN` overrides that module path. CLI authentication and other settings remain those of your installed
tools.

## Models from Nous Portal

[Nous Portal](https://portal.nousresearch.com/models) sells access to many model families through one
OpenAI-compatible API, billed to Nous credits. Six of them are profiles here, each run by the Kimi CLI:

| Profile | Model | Suited to |
|---|---|---|
| `nous-deepseek-pro` | DeepSeek V4 Pro | implementation, scripts, second opinions |
| `nous-deepseek-flash` | DeepSeek V4.1 Flash | cheap bulk and routine work |
| `nous-mimo-pro` | Xiaomi MiMo V2.6 Pro | long investigations (cached input is nearly free) |
| `nous-glm` | Z.ai GLM 5.3 | long-horizon coding, a different-family opinion |
| `nous-glm-flash` | Z.ai GLM 5.3 Flash | cheap routine coding and chores |
| `nous-gemini-argon` | Google Gemini 4 Argon | reviews of plans, diffs and conclusions (the expensive tier) |

Put your Nous API key alone in `~/.claude/delegate/nous-api-key.txt` (`nous_api_key_file`). Each job gets its own Kimi
home folder inside its job folder whose `config.toml` names Nous Portal as a provider and reads the key from the
`NOUS_API_KEY` environment variable; the runner sets that variable for the CLI process only, so the key is never
written into the CLI's config, the job folder or a log, and your own Kimi setup is not touched. Follow-ups (`--resume`)
continue in the same Kimi home.

Spend is measured, not guessed: the Kimi CLI records the tokens of every model call in its session files, and the
runner prices them with the Portal's live per-token prices (read from its public model list when the job starts,
including the cheaper cached-input price). It checks every 5 seconds and stops the job when it passes its cap, which is
`nous_max_usd_per_job` (default $3) unless `--max-usd` sets another; a job can end slightly over the cap by the cost of
its last model call. Finished jobs go into `nous-ledger.jsonl`, `board`, the feed and the digest show the month's
total, and with `nous_monthly_usd` set, `run` refuses new Nous jobs once the month's spend reaches it. Like the other
Kimi profiles, these run without a sandbox: `read` and `write` are instructions, not enforced limits.

## Claude via prepaid API credits

Install Claude Code, create an API key in the [Claude Console](https://platform.claude.com/settings/keys), and set a
workspace spend limit there. Save only the key on one line to `~/.claude/delegate/anthropic-api-key.txt` (Windows:
`%USERPROFILE%/.claude/delegate/anthropic-api-key.txt`). Restrict access to the file. The relay never creates it;
`claude_api_key_file` in local config overrides its path. `DELEGATE_CLAUDE` selects the CLI executable.

The owner must approve **each specific run, including follow-ups**. Relay agents copy that approval note into
`--approved`; they must never invent approval.

```sh
python delegate.py run claude-api-sonnet --prompt-file brief.md --cwd /path/to/project --mode write --approved "Sam / 2026-10-13 / implement parser" --max-usd 2
python delegate.py spend -n 5
```

Profiles use `claude-opus-5-5`, `claude-sonnet-5-5`, and `claude-haiku-5-5`. Model availability and effort support
depend on the installed CLI and account. Effort uses `CLAUDE_CODE_EFFORT_LEVEL`; older CLIs may ignore it.

The key is read from the file and put only in the child environment. Missing or empty files refuse the run with no
subscription fallback. The runner strips alternative credentials, cloud-provider routing and custom API endpoints, uses
`~/.claude/delegate/claude-api-home` for isolated settings and session history, disables user/project/local settings
and hooks, and requires the init event's `apiKeySource` to equal `ANTHROPIC_API_KEY`. A different or missing source
stops the child. Never sign in to that isolated home or place secrets there. Managed machine policies can still apply.
See [authentication](https://code.claude.com/docs/en/authentication) and the
[CLI reference](https://code.claude.com/docs/en/cli-reference).

MCP is empty by default with `--strict-mcp-config`. To load a server named `docs`, set `claude_api_mcp_allow` to
`["docs"]` and provide its Claude MCP definition in `claude_api_mcp_servers.docs`. No MCP servers or credentials are
copied from the owner's Claude settings.

`--max-usd` passes Claude's `--max-budget-usd` and stops the child when a reported `total_cost_usd` exceeds the cap.
Cost is generally reported at the final result; checks cannot prevent an in-flight request from overshooting.
Interrupted jobs may have unknown or partial spend. Console billing is authoritative. Feed cards, status and reports
show reported cost. Each finished job appends one line to `~/.claude/delegate/claude-api-ledger.jsonl`: job, approval,
model, start/end, status, cost and token usage. `spend` totals known costs and counts unknown costs; `board` and the
pages show the month's total against `claude_api_monthly_usd`. Child stdout/stderr is redacted before relay storage.
Tests make no real API calls. Claude API jobs are never resumed automatically and keep the plain time limit.

## Access modes

| Backend | `read` | `write` (default) | `full` |
|---|---|---|---|
| Codex | read-only sandbox | workspace-write sandbox for the working directory, `--add-dir` folders and the progress folder, with network | bypasses sandbox and approval prompts; only when explicitly authorised |
| Kimi and Nous | instruction only | instruction only | instruction only |
| Claude API | plan mode | acceptEdits for the working directory and authorised `--add-dir` folders | bypassPermissions |

- Kimi's prompt mode runs without approvals or sandbox enforcement. Its `read` and `write` modes are instructions in
  the brief, not filesystem protection; writes outside the job's folders are flagged afterwards
  ([CLI](CLI.md#checked-results-and-guard-flags)).
- Claude API modes are tool permissions, not an operating-system sandbox; headless shell commands needing approval may
  be denied.
- In Codex read mode, progress-file writes may be blocked; activity is still logged.
- Every brief instructs the worker not to commit, push, publish, send, launch games or drive the desktop unless the
  brief explicitly requests it. These instructions are not a network security boundary. Always state your constraints
  and review the returned work.
- Relays return reports without checking the worker's claims beyond the automatic checks. CLI calls may consume
  provider quota or incur costs under your account.

## The relay agents

All twenty relay variants come from one template:

| Agent tool name | Delegate profile | Effort |
|---|---|---|
| `gpt-astra-med` | `gpt-astra-med` | medium |
| `gpt-astra-high` | `gpt-astra-high` | high |
| `gpt-astra-xhigh` | `gpt-astra-xhigh` | xhigh |
| `gpt-sol-med` | `gpt-sol-med` | medium |
| `gpt-sol-high` | `gpt-sol-med` | high override |
| `gpt-sol-xhigh` | `gpt-sol-med` | xhigh override |
| `gpt-terra-med` | `gpt-terra-med` | medium |
| `gpt-terra-high` | `gpt-terra-high` | high |
| `kimi-k28` | `kimi-k28` | max |
| `kimi-k3` | `kimi-k3` | high |
| `kimi-k31-pro` | `kimi-k31-pro` | max |
| `claude-api-opus` | `claude-api-opus` | high |
| `claude-api-sonnet` | `claude-api-sonnet` | high |
| `claude-api-haiku` | `claude-api-haiku` | medium |
| `nous-deepseek-pro` | `nous-deepseek-pro` | high |
| `nous-deepseek-flash` | `nous-deepseek-flash` | high |
| `nous-mimo-pro` | `nous-mimo-pro` | default |
| `nous-glm` | `nous-glm` | high |
| `nous-glm-flash` | `nous-glm-flash` | high |
| `nous-gemini-argon` | `nous-gemini-argon` | high |

Plugin installs prefix them with `agent-relay-feed:`. These labels are configured profiles, not promises of model
availability; check `profiles` before the first delegation. How to install them is in [INSTALL](INSTALL.md).

## Adding a profile

Edit `PROFILES` near the top of `delegate.py` to change provider model IDs or add a direct-CLI profile. Each entry needs
`backend` (`codex`, `kimi` or `claude-api`), `model`, `effort` and a display `label`. For example:

```python
"reviewer": dict(backend="codex", model="your-model-id", effort="high", label="Reviewer (high)")
```

To expose it as a relay in Claude Code, add `dict(name="reviewer", profile="reviewer")` to `install_agents.py`'s
`SPECS`, then rerun the installer. For the plugin, run `python build_plugin.py` and include the regenerated `agents/`
files. A spec may set `effort` to use one profile with a different effort, as `gpt-sol-high` does. Model IDs and
supported effort values depend on your backend and account; the bridge passes them through.
