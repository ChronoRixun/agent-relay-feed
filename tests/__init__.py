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

# And they must never read or change this machine's real jobs, slots or saved shared state: a test
# that forgot to point the bridge at its own folder once put a live session's state back. Every
# test starts from a throwaway folder; tests that need their own still patch these.
import atexit      # noqa: E402
import shutil      # noqa: E402
import tempfile    # noqa: E402
from pathlib import Path    # noqa: E402

import delegate    # noqa: E402

_ROOT = Path(tempfile.mkdtemp(prefix="relay-tests-"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
delegate.DELEGATE_ROOT = _ROOT
delegate.JOBS_ROOT = _ROOT / "jobs"
delegate.AGENTS_ROOT = _ROOT / "agents"
