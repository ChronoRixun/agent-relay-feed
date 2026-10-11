---
name: feed
description: Start or open the local Agent Relay Feed for background task progress.
disable-model-invocation: true
allowed-tools: Bash
---
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 ChronoRixun -->

Start the local read-only feed and open its URL. Use the Bash tool's background
execution (`run_in_background: true`) so the server stays running after the call:

```sh
python "${CLAUDE_PLUGIN_ROOT}/feed.py" --open
```

Use `python3` if that is the machine's Python 3 command. The plugin root is
substituted inline by Claude Code; it is not a Bash environment variable.
Read the startup output and return the actual loopback URL, background task ID,
and how to stop the server (stop that background task or Ctrl+C in its terminal).
Do not start a second server if the configured port is already serving Agent
Relay Feed: open that existing URL instead, after checking its page title.
If the port belongs to another service, report the conflict and offer a different
`--port`; do not stop the other service. If browser opening fails, return the URL
for the user to open manually. Respect a request to start without opening by
omitting `--open`. Accept port/hours preferences by supplying `--port N` and
`--hours N`; do not insert arbitrary user text into a shell command.
The server binds only to 127.0.0.1; never expose it publicly.
