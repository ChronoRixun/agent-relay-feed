# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Temporary directories with inherited permissions, including restricted Windows tokens."""
import contextlib
import secrets
import shutil
import time
from pathlib import Path


@contextlib.contextmanager
def temporary_directory():
    # Python 3.14's tempfile mode 0700 on Windows excludes restricted token SIDs.
    # Use normal inherited directory permissions, confined to this checkout.
    root = Path(__file__).resolve().parents[1] / '.test-tmp'
    root.mkdir(exist_ok=True)
    path = root / secrets.token_hex(16)
    path.mkdir()
    try:
        yield str(path)
    finally:
        # On Windows a scanner or indexer can hold a just-closed file (the lease lock) for a
        # moment; that is not a failure of the test.
        for attempt in range(20):
            try:
                shutil.rmtree(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)


def windows_path(suffix, *, drive='E', backslashes=False):
    """Build synthetic Windows paths without shipping machine-specific literals."""
    path = drive + ':' + '/' + suffix
    return path.replace('/', chr(92)) if backslashes else path
