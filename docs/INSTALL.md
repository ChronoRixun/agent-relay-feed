# Installing Agent Relay Feed

[Back to the README](../README.md)

## As a Claude Code plugin

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

Start a new Claude Code session after installing. Plugin agents are namespaced, for example
`agent-relay-feed:gpt-sol-high`; manual agents have the unprefixed names in the table in
[BACKENDS](BACKENDS.md#the-relay-agents). You do not need to run the manual installer for the plugin.
`/agent-relay-feed:feed` starts the local feed and opens its URL (or ask it to start without opening a browser).
`/agent-relay-feed:delegate` describes profile selection, briefs, waiting, follow-ups and access limits.

The twenty plugin agents are generated from the same template as the manual installer. They use the documented
`${CLAUDE_PLUGIN_ROOT}` substitution in their Markdown bodies, which resolves to the installed plugin's absolute path.
They resolve your home directory at runtime and store job data outside the plugin cache, in the `~/.claude/delegate/`
layout, so plugin updates do not invalidate hardcoded paths or delete job data. Use a current Claude Code release with
plugin skills and plugin-root substitution in agent content. Older releases may validate manifests without supporting
all runtime behaviour; the manual installer is the fallback. No exact minimum version has been established.

Official references: [plugins overview](https://code.claude.com/docs/en/plugins),
[components](https://code.claude.com/docs/en/plugins/components),
[plugin manifest and path variables](https://code.claude.com/docs/en/plugins-reference#environment-variables),
and [marketplaces](https://code.claude.com/docs/en/plugin-marketplaces).

## Manually

```sh
git clone <repository-url> agent-relay-feed
cd agent-relay-feed
python install_agents.py
python install_agents.py --list
```

Use `python3` instead of `python` if that is your Python 3 command. The installer records the current Python
interpreter and this checkout's absolute location, and writes user-scoped definitions to `~/.claude/agents/`. Keep the
checkout in place. Restart Claude Code after installation; user agents are available across projects
([Claude Code subagents](https://code.claude.com/docs/en/sub-agents)).

```sh
python install_agents.py --only gpt-sol-med gpt-sol-high
python install_agents.py --only kimi-k28,kimi-k3
```

Generated files carry a marker and SHA-256 checksum. Reinstalling updates intact generated files, but preserves
modified or unmarked files and returns exit code 1. Review those files before using `--force`. This also means
definitions made by an older installer need `--force` to migrate. If you rewrote a relay's `description` (the text
Claude Code uses to choose an agent) or its relay `model`, add `--keep-description`: the installer saves those two
lines to `~/.claude/delegate/agent-overrides.json` before replacing the file, and this and every later install uses
them. After moving the checkout or Python interpreter, rerun the installer.

## Using the relay agents in Claude Code

Ask Claude to use the Agent tool with the installed name, for example:

```text
Agent(subagent_type="agent-relay-feed:gpt-sol-high", prompt="Review the parser in the current working directory. Read-only. Return findings with file paths and line numbers.")
```

For a manual install use `gpt-sol-high` instead of the plugin-prefixed name.

Give each relay a self-contained brief: goal, working directory, relevant paths and evidence, permitted changes,
constraints, and the final report you want. The relay writes your instructions verbatim into
`~/.claude/delegate/inbox/`, starts the job, waits until it ends, and returns the result unchanged. Send follow-ups to
the same relay to resume its external session. A session that supervises many agents can also skip the relay and run
the commands directly; see [SUPERVISING](SUPERVISING.md).

## Uninstalling

For a plugin install:

```sh
claude plugin uninstall agent-relay-feed@agent-relay-feed
claude plugin marketplace remove agent-relay-feed
```

Stop any running feed background task separately. Plugin uninstall leaves this tool's job history and local config in
`~/.claude/delegate/`; archive or remove those yourself when you no longer need them. It does not remove manually
installed relays or external CLIs. For a manual install:

```sh
python install_agents.py --uninstall
python install_agents.py --uninstall --only kimi-k28 kimi-k3
```

Uninstall removes intact generated relay definitions only. Modified or unmarked files need an explicit `--force`.
Job history, config and the checkout remain; archive or remove them yourself after cancelling any running jobs. The
installer does not uninstall external CLIs.
