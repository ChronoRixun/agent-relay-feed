---
name: delegate
description: Use when asked to delegate coding, review, or investigation to an external Codex, Kimi or owner-approved Claude API agent, or monitor and follow up on a delegated background task.
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
| `agent-relay-feed:gpt-sol-xhigh` | Codex | xhigh |
| `agent-relay-feed:gpt-terra-med` | Codex | medium |
| `agent-relay-feed:gpt-terra-high` | Codex | high |
| `agent-relay-feed:kimi-k28` | Kimi | max |
| `agent-relay-feed:kimi-k3` | Kimi | high |
| `agent-relay-feed:kimi-k31-pro` | Kimi | max |
| `agent-relay-feed:claude-api-opus` | Claude API credits | high |
| `agent-relay-feed:claude-api-sonnet` | Claude API credits | high |
| `agent-relay-feed:claude-api-haiku` | Claude API credits | medium |
| `agent-relay-feed:nous-deepseek-pro` | Nous Portal credits (Kimi CLI) | high |
| `agent-relay-feed:nous-deepseek-flash` | Nous Portal credits (Kimi CLI) | high |
| `agent-relay-feed:nous-mimo-pro` | Nous Portal credits (Kimi CLI) | default |
| `agent-relay-feed:nous-glm` | Nous Portal credits (Kimi CLI) | high |
| `agent-relay-feed:nous-glm-flash` | Nous Portal credits (Kimi CLI) | high |
| `agent-relay-feed:nous-gemini-argon` | Nous Portal credits (Kimi CLI) | high |

Nous Portal relays bill the owner's Nous credits and need the owner's key file;
they run through the Kimi CLI, so they are unsandboxed like Kimi. Each job has a
spend cap and the month has a budget; `delegate.py spend` shows both.

Use Claude API relays ONLY when the owner approved this specific run. Include
the approval note so the relay passes `--approved "<who/when/why>"`, and copy any
`--max-usd` limit. Each follow-up needs fresh approval. Never invent an approval
or read/create an API key. `delegate.py spend` shows recorded API costs; caps are
checked when cost is reported and can overshoot during an API request. Claude
read/write modes use plan/acceptEdits permissions, not an OS sandbox.

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

When several jobs run, supervise them with the bridge instead of reading logs
(the README has the details of each):

- `delegate.py board` prints one line per running job and Claude Code subagent:
  stage, time, open question and slots held. `delegate.py digest` sums up what
  finished and what still needs a look.
- `delegate.py wait --any` in one background call wakes you once per outcome,
  question or subagent cut off by a connection error. Do not poll.
- `delegate.py tell <job> "<text>"` sends a running job a message;
  `delegate.py answer <job> "<text>"` answers its open question.
- `delegate.py run ... --after <job>` queues a job behind another.
- `delegate.py bg -- <command>` runs a long command as its own job.
- Finished jobs end with a result block whose claims are checked automatically;
  FLAG lines mark claims that did not hold. Checks are a first pass, not proof.

Use `/agent-relay-feed:feed` when the user asks to see background task activity.
The feed reads local logs that may include sensitive prompts and project data.
