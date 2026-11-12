# Development and validation

[Back to the README](../README.md)

From the repository root:

```sh
python -m unittest
python build_plugin.py --check
claude plugin validate .claude-plugin/plugin.json
claude plugin validate .claude-plugin/marketplace.json
```

`build_plugin.py --check` detects missing, stale and unexpected generated agent files. Manifest validation checks
packaging, not provider login or model access or runtime path expansion. Before publishing, load the plugin in a
current Claude Code session and smoke-test one authorised read-only task and the feed command.

Tests use only the standard library and temporary directories inside the checkout. They start from a throwaway folder,
so they never read or change your real jobs, slots or saved shared state, and they make no provider calls.

Plugin `agents/` definitions are generated from `install_agents.py`'s `SPECS` and template by `build_plugin.py`: edit
the source and regenerate together rather than hand-editing checksummed output. Adding a profile is described in
[BACKENDS](BACKENDS.md#adding-a-profile). Contributor rules for agents working on this repository are in
[AGENTS.md](../AGENTS.md).

The pages can be checked without real data: `python feed.py --demo`, or `/mission?demo` and `/orbit?demo` on any
running feed ([FEED](FEED.md#demo-data)).
