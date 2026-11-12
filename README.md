# Agent Relay Feed

Mission control for the AI coding agents you run on one machine. Hand work to Codex, Kimi, Nous Portal models or Claude
(on API credits) from Claude Code or a terminal, then watch every agent at once: what stage it is in, whether it is
stuck, which shared resources it holds, and what it is asking you. Answer its questions while it runs, get one alert
per outcome on your phone, and have its claims checked before you read "PASS".

![Orbit: running agents, shared slots and finished jobs on one dial](docs/orbit.png)

*Orbit, one of three live views, showing invented demo jobs.*

## What you need

- Python 3.10 or newer. Nothing else from PyPI.
- The backends you want to use, installed and signed in: [Codex CLI](https://developers.openai.com/codex/cli),
  Kimi CLI (also used for Nous Portal models), or Claude Code with your own API key file for Claude on API credits.
- Claude Code, if you want to delegate from a Claude Code session. Plain terminal use works without it.

Windows, macOS and Linux are supported. Model names in the profiles are configuration, not a promise of availability.

## Install

As a Claude Code plugin:

```sh
claude plugin marketplace add ChronoRixun/agent-relay-feed
claude plugin install agent-relay-feed@agent-relay-feed
```

Start a new Claude Code session afterwards. Or clone the repository and run `python install_agents.py` to install the
relay agents by hand. Both routes, updating and uninstalling are in [docs/INSTALL.md](docs/INSTALL.md).

## Quick start

```sh
python delegate.py run gpt-sol-med --prompt-file brief.md --cwd ~/code/app --mode write --wait 0
python delegate.py wait --any --wait 43200     # one background call: wakes you once per outcome or question
python delegate.py board                       # every running agent on one line
python delegate.py answer <job_id> "Yes, keep the old format behind a flag"
python delegate.py tell <job_id> "The staging database is read-only today"
python feed.py --open                          # the feed, mission control, Orbit and review pages
```

From Claude Code, pick a relay agent by name, for example
`Agent(subagent_type="agent-relay-feed:gpt-sol-high", prompt="Review the parser. Read-only. ...")`, or ask Claude to
run the commands above. [docs/SUPERVISING.md](docs/SUPERVISING.md) describes how one session can run many agents this
way.

## What you get

- **One status line per agent.** Workers name their stages; `board` and the feed show stage, time, last activity, quiet
  and looping agents, for relay jobs and Claude Code subagents alike. [CLI](docs/CLI.md#stages-and-the-board)
- **One wake-up per outcome.** `wait --any` reports each finished job, question and cut-off subagent exactly once.
  [CLI](docs/CLI.md#one-wake-up-per-outcome)
- **Two-way messages.** Workers ask and wait; you answer or send news mid-run, from a terminal or the feed page.
  [CLI](docs/CLI.md#talking-to-a-running-job)
- **Checked results.** Every job ends with a result block; commits, branches, files, image sizes and screenshots are
  checked, and installs, publishing and writes outside the job's folders are flagged.
  [CLI](docs/CLI.md#checked-results-and-guard-flags)
- **Shared slots.** Builds, test copies and emulators are leased so agents do not collide, and shared settings are put
  back after the last holder leaves. Agents give way when you start your own program. [SLOTS](docs/SLOTS.md)
- **Queues, resume, long commands.** `run --after` waits for other jobs, dropped connections are resumed once, and
  `bg` runs a long command as a job of its own. [CLI](docs/CLI.md#queues-and-automatic-resume)
- **Live pages.** A feed, a mission-control view, the Orbit dial and a review page for evidence, readable from a phone,
  with a PIN for answering. [FEED](docs/FEED.md)
- **Alerts and budgets.** ntfy, Discord or JSON alerts; month spend and budgets for Claude API and Nous credits.
  [FEED](docs/FEED.md#alerts) · [BACKENDS](docs/BACKENDS.md)

All documentation:

| Page | What is in it |
|---|---|
| [INSTALL](docs/INSTALL.md) | Plugin and manual install, the relay agents, update and uninstall |
| [CLI](docs/CLI.md) | Every command and option |
| [SUPERVISING](docs/SUPERVISING.md) | Running many agents from one session |
| [FEED](docs/FEED.md) | Feed, mission control, Orbit, review page, PIN, alerts, Claude Code subagents |
| [BACKENDS](docs/BACKENDS.md) | Codex, Kimi, Nous Portal, Claude API credits, profiles |
| [SLOTS](docs/SLOTS.md) | Shared resources, shared state, giving way to the owner |
| [CONFIG](docs/CONFIG.md) | The optional config file, key by key |
| [INTERNALS](docs/INTERNALS.md) | Job storage, the detached runner, how stages are read |
| [DEVELOPMENT](docs/DEVELOPMENT.md) | Tests, plugin checks, adding a profile |

## Safety, in brief

- Codex `read` and `write` are enforced by its sandbox; `full` bypasses it and is for explicitly authorised work only.
- Kimi (and so every Nous model) has no sandbox: its `read` and `write` modes are instructions in the brief. Writes
  outside the job's folders are flagged afterwards, not prevented.
- Claude API jobs need the owner's approval for each run and use tool permissions, not an operating-system sandbox.
- Every brief tells the worker not to commit, push, publish, send messages, launch games or drive the desktop unless
  asked. That is an instruction, not a security boundary: state your limits and review the work.
- Workers' reports are claims. The automatic checks are a first pass, not proof.
- Job logs and briefs can hold sensitive data. The feed listens on `127.0.0.1` unless you choose otherwise and has no
  login, so anyone who can reach its port can read it. Never expose it publicly or commit job logs.
- Provider calls run under your accounts and can cost money.

Details for each backend are in [docs/BACKENDS.md](docs/BACKENDS.md#access-modes).

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. Copyright 2026 ChronoRixun.
