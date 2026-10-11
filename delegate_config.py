# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Optional, local presentation settings shared by the CLI and feed."""
import copy
import json
import math
import re
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "delegate" / "config.json"
DEFAULTS = dict(path_shortening=[], watched_processes=[], feed_port=8765,
                stall_seconds=480, loop_count=6, polling_seconds=30,
                job_path_roots=[], activity_ignore_paths=[])


def _strings(value):
    return isinstance(value, list) and all(isinstance(s, str) and s for s in value)


def _valid(key, value):
    if key in ("feed_port", "loop_count"):
        return type(value) is int and (1 <= value <= 65535 if key == "feed_port" else 2 <= value <= 10)
    if key in ("stall_seconds", "polling_seconds"):
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    if key in ("job_path_roots", "activity_ignore_paths"):
        return _strings(value)
    if key == "path_shortening":
        return isinstance(value, list) and all(isinstance(p, list) and len(p) == 2 and
                all(isinstance(s, str) and s for s in p) for p in value)
    if key == "watched_processes":
        return isinstance(value, list) and all(isinstance(p, dict) and
                isinstance(p.get("name"), str) and p["name"] and
                isinstance(p.get("owner", "unknown"), str) and
                _strings(p.get("job_cwd_patterns", [])) and
                isinstance(p.get("owners", []), list) and all(
                    isinstance(r, dict) and _strings(r.get("path_patterns", [])) and
                    bool(r.get("path_patterns")) and isinstance(r.get("owner"), str)
                    for r in p.get("owners", [])) for p in value)
    return False


def load_config(path=None):
    """Missing config is normal; invalid fields warn and retain their defaults."""
    config = copy.deepcopy(DEFAULTS)
    path = Path(path) if path is not None else CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring config {path}: {exc}", file=sys.stderr)
        return config
    for key, value in data.items():
        if key in DEFAULTS and _valid(key, value):
            config[key] = value
        else:
            print(f"warning: ignoring config field {key!r}", file=sys.stderr)
    return config


CONFIG = load_config()


def normalize_path(path):
    return str(path).replace("\\", "/").rstrip("/")


def ignored_activity_path(folder):
    norm = normalize_path(folder)
    return any(norm.casefold() == normalize_path(p).casefold()
               for p in CONFIG["activity_ignore_paths"])


def shorten_paths(text, home=None, pairs=None):
    pairs = list(CONFIG["path_shortening"] if pairs is None else pairs)
    pairs.append([str(Path.home() if home is None else home), "~"])
    # Longest prefix wins. Match separators in either style, preserving everything else.
    for source, target in sorted(pairs, key=lambda p: -len(p[0])):
        source = normalize_path(source)
        pattern = re.escape(source).replace("/", r"[\\/]")
        flags = re.I if re.match(r"^[a-z]:", source, re.I) else 0
        text = re.sub(pattern + r"(?=[\\/\s'\";]|$)", lambda m: target.rstrip("/\\"), text, flags=flags)
    return text


def repetition(recent, count=None, polling_seconds=None):
    """Return (possible_loop, steady_polling) for up to ten (command, seconds) pairs."""
    if not recent:
        return False, False
    times = [t for cmd, t in recent[-10:] if cmd == recent[-1][0]]
    interval = CONFIG["polling_seconds"] if polling_seconds is None else polling_seconds
    polling = len(times) > 1 and ((times[-1] - times[0]) % 86400) / (len(times) - 1) >= interval
    threshold = CONFIG["loop_count"] if count is None else count
    return len(times) >= threshold and not polling, polling
