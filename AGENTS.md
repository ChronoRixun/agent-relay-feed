# Agent guidance for Agent Relay Feed

These instructions apply throughout this repository, including automated pull-request reviews.

This is the Python relay/plugin and local read-only task feed for external coding agents. It is tooling, not a game runtime. Read `README.md`; the bridge is `delegate.py`, configuration is `delegate_config.py`, the feed is `feed.py` / `feed.html`, and Claude-session parsing is `claude_agents.py`. Python 3.10+ and the standard library are the development baseline.

- Keep the distinction between access modes explicit. Codex sandbox flags and tool-disable settings are enforcement; Kimi read/write modes are instructions, not filesystem isolation. Never silently widen access or launch paid/authenticated provider jobs as a test.
- Preserve job identity, resume/backend checks, cancellation, process ownership, timeouts and atomic state/report updates. A running job's wait timeout is not completion. Do not kill unrelated processes or start a duplicate job to poll status.
- Keep the feed read-only and bound to loopback. Treat displayed logs and metadata as untrusted and potentially sensitive; preserve escaping and avoid exposing prompts, file contents, raw tool output or credentials. Do not publish real job history, private paths/IPs/emails or provider credentials.
- Relay reports are unverified worker claims. Keep access/model labels factual; configured model IDs are not proof of current provider availability.
- Plugin `agents/` definitions are generated from `install_agents.py`'s `SPECS` and template via `build_plugin.py`. Edit the source and regenerate together rather than hand-editing checksummed output. Preserve modified/unmanaged user definitions; `--force` must remain explicit. Keep `.claude-plugin/` manifests and `skills/` consistent.
- Ship original code/docs and synthetic demos, preserving `LICENSE` and `NOTICE`; never add game assets, official game logos, decoded game data or secrets.

There is no committed CI workflow. The README's local validation commands, from the root, are:

```
python -m unittest
python build_plugin.py --check
claude plugin validate .claude-plugin/plugin.json
claude plugin validate .claude-plugin/marketplace.json
```

The last two need Claude Code installed; they validate manifests, not provider access or runtime plugin substitution. Use temporary test storage and synthetic jobs. Do not run the user installer, alter client config, open a browser/feed, or call a real agent provider unless the task authorizes that integration check. Report unavailable checks honestly.

Review final diffs and callers for concrete correctness, access-control, data-loss and privacy regressions. Give file/line, trigger and consequence; distinguish hypotheses. Keep work scoped and preserve unrelated edits. Reviews do not authorize committing, pushing, merging, installing, publishing, deployment or sending messages.
