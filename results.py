# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""A fixed shape for a worker's result, and cheap checks of what it claims.

A worker ends its final report with one fenced block:

    ```result
    {"verdict": "pass", "summary": "...", "commits": [{"repo": "/abs/repo", "branch": "fix/x", "head": "<sha>"}],
     "files": ["/abs/path"], "checks": [{"name": "unit tests", "result": "pass", "detail": "214 passed"}],
     "evidence": [{"path": "/abs/shot.png", "shows": "4K borderless menu", "width": 3840, "height": 2160}],
     "open": ["..."], "cleanup": ["..."], "owner": ["..."]}
    ```

The checks here only catch what a machine can see without judgement: a commit that does not
exist, a branch that points elsewhere, uncommitted work, a missing or stale evidence file, an image
that is not the size it is said to be, a "pass" beside a failed check. They cannot tell whether a
screenshot shows the right window or whether a test was honest; a clean check list is not proof.
"""
import json
import os
import re
import struct
import subprocess

import capture

# what an evidence entry may say about a screenshot beyond its size; checked against its capture record
CAPTURE_CLAIMS = ("process", "window", "borderless", "fullscreen")
VERDICTS = ("pass", "fail", "partial", "blocked")
CHECK_RESULTS = ("pass", "fail", "skip", "expected-fail")
# A check that is meant to fail and did, such as a new test run against the old code to prove it
# catches the bug (a red test). Workers write it in several ways; all mean the same.
_EXPECTED_FAIL = {"expected-fail", "expected_fail", "expected fail", "xfail", "red", "red-test", "red test"}


def check_result(c):
    """A check's result, with the spellings of an expected failure folded into one."""
    r = str(c.get("result", "")).strip().lower() if isinstance(c, dict) else ""
    return "expected-fail" if r in _EXPECTED_FAIL else r
LISTS = ("commits", "files", "checks", "evidence", "open", "cleanup", "owner")
_BLOCK = re.compile(r"```(?:result|json)[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)

RULES = ("End your final report with one fenced block exactly like this (a line with ```result, the JSON, "
         "a line with ```), filled in truthfully. It is read by a program and its claims are checked, so "
         "use absolute paths and leave out any key you have nothing for:\n"
         "```result\n"
         '{"verdict": "pass | fail | partial | blocked",\n'
         ' "summary": "one sentence",\n'
         ' "commits": [{"repo": "<absolute repo path>", "branch": "<branch>", "head": "<full commit hash>"}],\n'
         ' "files": ["<absolute path of each file you created or changed and did not commit>"],\n'
         ' "checks": [{"name": "<gate or test>", "result": "pass | fail | skip | expected-fail", "detail": "<numbers>"}],\n'
         ' "evidence": [{"path": "<absolute path of a file, not a folder>", "shows": "<what it proves>", "width": 0, "height": 0}],\n'
         ' "open": ["<what is not done or not verified>"],\n'
         ' "cleanup": ["<what you removed, stopped or restored>"],\n'
         ' "owner": ["<what only the owner can do or decide>"]}\n'
         "```\n"
         "Use expected-fail for a check that is meant to fail and did, such as a new test run against the "
         "old code to prove it catches the bug, and say in detail what the failure proves. "
         "verdict is pass only if everything asked for is done and every other check passed. Give width and "
         "height only for images, as the real pixel size of the file. For a screenshot of a program's window, "
         'also give "process": "<its .exe name>" (and "borderless": true if that is the claim); those are '
         "compared with the record written by the capture command, so take such screenshots with it and do "
         "not crop or edit them afterwards.")


def extract(text):
    """(data, error) from a final report: the last fenced result block that has a verdict."""
    blocks = _BLOCK.findall(text or "")
    if not blocks:
        return None, "no result block"
    error = "no result block with a verdict"
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except ValueError as e:
            error = f"result block is not valid JSON ({e})"
            continue
        if isinstance(data, dict) and "verdict" in data:
            return data, None
    return None, error


def image_size(path):
    """(width, height) of a PNG, JPEG, GIF or BMP file from its header, else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return w, abs(h)
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(4)
                    if len(marker) < 4 or marker[0] != 0xFF:
                        return None
                    kind, length = marker[1], struct.unpack(">H", marker[2:4])[0]
                    if 0xC0 <= kind <= 0xCF and kind not in (0xC4, 0xC8, 0xCC):
                        h, w = struct.unpack(">HH", f.read(5)[1:5])
                        return w, h
                    f.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error):
        pass
    return None


def _git(repo, *args):
    """(exit code, output) of a read-only git command; (None, reason) if it could not run."""
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def _items(data, key):
    value = data.get(key)
    return value if isinstance(value, list) else []


def verify(data, started=None, git=_git):
    """Findings for a result block: a list of dict(ok, text). Reads files and runs read-only git."""
    found = []

    def note(ok, text):
        found.append(dict(ok=bool(ok), text=text))

    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        note(False, f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    for key in LISTS:
        if key in data and not isinstance(data[key], list):
            note(False, f"{key} is not a list")
    failed = [c.get("name", "?") for c in _items(data, "checks") if check_result(c) == "fail"]
    odd = [c for c in _items(data, "checks") if check_result(c) not in CHECK_RESULTS]
    if odd:
        note(False, f"{len(odd)} check(s) have no pass/fail/skip result")
    if verdict == "pass" and failed:
        note(False, "verdict is pass but these checks failed: " + ", ".join(map(str, failed)))
    if verdict == "pass" and _items(data, "open"):
        note(False, f"verdict is pass but {len(_items(data, 'open'))} item(s) are listed as open")

    for c in _items(data, "commits"):
        if not isinstance(c, dict) or not c.get("repo") or not c.get("head"):
            note(False, "a commit entry lacks repo or head")
            continue
        repo, head, branch = str(c["repo"]), str(c["head"]), c.get("branch")
        if not os.path.isdir(repo):
            note(False, f"repo not found: {repo}")
            continue
        rc, _ = git(repo, "cat-file", "-e", head + "^{commit}")
        if rc is None:
            note(False, f"could not run git in {repo}")
            continue
        note(rc == 0, f"commit {head[:10]} {'exists' if rc == 0 else 'does NOT exist'} in {repo}")
        if rc == 0 and branch:
            rc2, tip = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            if rc2 != 0:
                note(False, f"branch {branch} does not exist in {repo}")
            else:
                same = tip.startswith(head) or head.startswith(tip)
                note(same, f"branch {branch} {'is at' if same else 'is NOT at'} {head[:10]}"
                           + ("" if same else f" (it is at {tip[:10]})"))
        rc3, dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
        if rc3 == 0:
            n = len([l for l in dirty.splitlines() if l.strip()])
            note(n == 0, f"{repo} has {n} uncommitted change(s)" if n else f"{repo} has no uncommitted changes")

    def check_file(path, label):
        path = str(path)
        try:
            st = os.stat(path)
        except OSError:
            note(False, f"{label} not found: {path}")
            return False
        if os.path.isdir(path):
            # a folder proves nothing by itself; say so without calling it a problem
            note(True, f"{label} is a folder, so only its existence was checked: {path}")
            return False
        if os.path.isfile(path) and st.st_size == 0:
            note(False, f"{label} is empty: {path}")
            return False
        if started and st.st_mtime < started - 60:
            note(False, f"{label} is older than this job: {path}")
            return True
        note(True, f"{label} exists: {path}")
        return True

    for path in _items(data, "files"):
        check_file(path, "file")
    for e in _items(data, "evidence"):
        if not isinstance(e, dict) or not e.get("path"):
            note(False, "an evidence entry has no path")
            continue
        if not check_file(e["path"], "evidence"):
            continue
        w, h = e.get("width"), e.get("height")
        size = image_size(str(e["path"]))
        if w and h:
            if size is None:
                note(False, f"evidence is not a readable image: {e['path']}")
            else:
                note(tuple(size) == (w, h), f"image is {size[0]}x{size[1]}"
                     + ("" if tuple(size) == (w, h) else f", NOT the claimed {w}x{h}") + f": {e['path']}")
        if size is None:
            continue
        # a screenshot taken with `delegate.py capture` carries a record of what it was of
        rec = capture.load_record(str(e["path"]))
        claims = [k for k in CAPTURE_CLAIMS if e.get(k) is not None]
        if rec is not None:
            for ok, text in capture.check(e, str(e["path"]), rec):
                found.append(dict(ok=ok, text=text, capture=True))
        elif claims:
            note(False, f"no capture record, so the claimed {', '.join(claims)} cannot be checked "
                        f"(take the screenshot with `delegate.py capture`): {e['path']}")
    return found


GUARD_RULES = [
    dict(pattern=r"\b(?:pip3?|python3? -m pip|uv pip) install\b|\bnpm (?:install|i|add) (?:-g|--global)\b|"
                 r"\b(?:winget|choco|scoop|apt|apt-get|brew|dnf|pacman) (?:install|-S)\b",
         text="installed software"),
    # `git stash push` only shelves local changes, so a "push" after "stash" in the same command does not count
    dict(pattern=r"\bgit\b(?:(?!\bstash\b)[^;|&\n])*\bpush\b|\bgh (?:release|pr|issue|repo) (?:create|comment|merge|edit|delete)\b",
         text="ran a command that publishes outside this machine"),
]
_GUARD_COMMAND = re.compile(r"^\S+\s+(?:command \(exit -?\d+\):|tool \w+:)\s*(.*)$")
_GUARD_FILES = re.compile(r"^\S+\s+files changed:\s*(.*)$")


def guard(mode, activity_lines, extra_rules=()):
    """Findings for things in a job's activity log that usually need the owner's say-so: software
    installs, commands that publish, file changes in a read-only job. It reads what the worker
    ran after the fact and cannot stop anything; a brief may have allowed it, so each finding says
    to check."""
    rules = []
    for rule in list(GUARD_RULES) + list(extra_rules):
        try:
            rules.append((re.compile(rule["pattern"], re.I), rule["text"]))
        except re.error:
            continue
    hits, changed = {}, []
    for line in activity_lines:
        m = _GUARD_COMMAND.match(line)
        if m:
            for pattern, text in rules:
                if pattern.search(m.group(1)):
                    count, example = hits.get(text, (0, m.group(1)))
                    hits[text] = (count + 1, example)
            continue
        m = _GUARD_FILES.match(line)
        if m and mode == "read":
            changed.append(m.group(1))
    found = [dict(ok=False, text=f"GUARD: {text} ({n}x), e.g. `{' '.join(example.split())[:140]}`; check the brief allowed it")
             for text, (n, example) in hits.items()]
    if changed:
        found.append(dict(ok=False, text=f"GUARD: changed files in a read-only job ({len(changed)}x), e.g. {changed[0][:140]}"))
    return found


def digest(text, started=None, git=_git):
    """Everything the bridge keeps about a final report's result block."""
    data, error = extract(text)
    if data is None:
        return dict(data=None, error=error, findings=[], verdict=None, flags=0)
    findings = verify(data, started, git)
    return dict(data=data, error=None, findings=findings, verdict=data.get("verdict"),
                flags=sum(1 for f in findings if not f["ok"]))


def summary_lines(d):
    """The compact view a supervisor reads before (or instead of) the full report."""
    if not d or d.get("data") is None:
        return ([f"VERDICT: none given ({(d or {}).get('error') or 'no result block'}); read the report below"]
                + [f"  FLAG: {f['text']}" for f in (d or {}).get("findings") or [] if not f["ok"]])
    data, findings = d["data"], d["findings"]
    ok = sum(1 for f in findings if f["ok"])
    lines = [f"VERDICT: {data.get('verdict')}  |  CLAIMS CHECKED: {ok} ok, {d['flags']} FLAGGED"]
    if data.get("summary"):
        lines.append(f"SUMMARY: {data['summary']}")
    lines += [f"  FLAG: {f['text']}" for f in findings if not f["ok"]]
    lines += [f"  SCREENSHOT: {f['text']}" for f in findings if f["ok"] and f.get("capture")]
    for c in _items(data, "commits"):
        if isinstance(c, dict):
            lines.append(f"  COMMIT: {str(c.get('head'))[:10]} on {c.get('branch') or '?'} in {c.get('repo')}")
    checks = [c for c in _items(data, "checks") if isinstance(c, dict)]
    if checks:
        lines.append("  CHECKS: " + "; ".join(f"{c.get('name')}={check_result(c)}"
                                             + (f" ({c['detail']})" if c.get("detail") else "") for c in checks))
    for key, label in (("open", "OPEN"), ("owner", "YOUR TURN"), ("cleanup", "CLEANUP")):
        for item in _items(data, key):
            lines.append(f"  {label}: {item}")
    if d["flags"] == 0 and findings:
        lines.append("  (checks cover existence, sizes, commits and consistency only, not whether the evidence shows what it says)")
    return lines
