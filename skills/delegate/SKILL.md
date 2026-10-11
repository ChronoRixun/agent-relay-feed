---
name: delegate
description: Use when asked to delegate coding, review, or investigation to an external Codex or Kimi agent, or monitor and follow up on a delegated background task.
---
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 ChronoRixun -->

Use Agent Relay Feed when the user wants external coding work, an independent
review, or a background investigation. The user must authorize the task and its
scope. Do not delegate merely to consume an available model.

Select one of the plugin agents using its full namespaced name:

| Agent | Backend | Effort |
|---|---|---|
| `agent-relay-feed:gpt-astra-med` | Codex | medium |
| `agent-relay-feed:gpt-astra-high` | Codex | high |
| `agent-relay-feed:gpt-astra-xhigh` | Codex | xhigh |
| `agent-relay-feed:gpt-sol-med` | Codex | medium |
| `agent-relay-feed:gpt-sol-high` | Codex | high |
| `agent-relay-feed:kimi-k28` | Kimi | max |
| `agent-relay-feed:kimi-k3` | Kimi | high |

These labels are configured profiles, not promises of model availability. Check
installed/authenticated CLIs and profile IDs before the first delegation:

```sh
python "${CLAUDE_PLUGIN_ROOT}/delegate.py" profiles
```

Use `python3` when necessary. Read `${CLAUDE_PLUGIN_ROOT}/README.md` for setup,
profile configuration, CLI overrides, mode semantics, feed, and uninstall.
Plugin-root references are substituted inline, not Bash environment variables.

Give the Agent tool a self-contained brief: goal, working directory, relevant
paths and findings, allowed changes, files not to touch, desired checks, and the
final report. Preserve the user's constraints verbatim. Use read mode for a
review or investigation, write for authorized edits, and full only if explicitly
authorized. Kimi is unsandboxed in every mode; read/write are instructions only.
Never imply filesystem protection for Kimi. Do not authorize commits, pushes,
publishing, messages, games, or desktop interaction unless the user requested them.
When splitting authorized work, assign distinct file ownership and tell workers
they share the codebase and must accommodate others' edits.

A running job keeps going after a wait call returns. Keep waiting on its job ID;
never start a duplicate. Send follow-ups to the same relay so it can resume the
external session. Cancel only when requested. Return the relay report and its
paths without claiming its results have been independently verified. Review
claims and changes before presenting them as verified work.

Use `/agent-relay-feed:feed` when the user asks to see background task activity.
The feed reads local logs that may include sensitive prompts and project data.
