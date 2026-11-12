# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Standard-library regression tests; run with python -m unittest."""
import copy

import delegate_config

# The tests must not depend on whatever the person running them has in their own config file.
# This runs before any test module imports the code under test, so every module sees defaults.
delegate_config.CONFIG.clear()
delegate_config.CONFIG.update(copy.deepcopy(delegate_config.DEFAULTS))
# nor on the Claude Code subagents running on this machine: tests that need transcripts make their own
delegate_config.CONFIG["claude_projects_root"] = "~/.claude/no-such-folder-for-tests"
