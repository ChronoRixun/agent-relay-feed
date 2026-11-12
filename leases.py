# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Slots for things on one machine that agents must not use at the same time: a build, a copy of
an application under test, an emulator. The `resources` config key names them:

    "resources": {
      "build":     {"slots": 1, "rule": "any compiler or full test-suite run"},
      "test-copy": {"slots": 2, "values": ["/work/copy-1", "/work/copy-2"],
                    "yield_to": ["TheApp.exe"], "rule": "launching the application under test"}
    }

`slots` is how many holders at once; `values` (optional, one per slot) is what a holder is handed,
such as its own folder; `yield_to` (optional) names programs of the machine's owner: while one of
them runs and is not an agent's own, nobody is granted a slot. A slot belongs to a holder: a job id
(freed when that job ends) or a free-form name (freed when its time-to-live runs out).

The state is one JSON file under a lock, so any process on the machine can take part.
"""
import contextlib
import json
import os
import time
from pathlib import Path

STATE = "leases.json"
DEFAULT_TTL_S = 3 * 3600        # for holders that are not jobs: renew by leasing again


@contextlib.contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "leases.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise OSError("lease lock timed out") from None
                    time.sleep(.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load(root):
    try:
        data = json.loads((Path(root) / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root, state):
    path = Path(root) / STATE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _live(entries, is_job_alive, now):
    """Drop slots whose holder is gone: a job that ended, or a named holder past its time-to-live."""
    kept = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        if e.get("job"):
            if is_job_alive(e.get("holder")):
                kept.append(e)
        elif now - e.get("t", 0) < e.get("ttl", DEFAULT_TTL_S):
            kept.append(e)
    return kept


def held(root, resource, holder, is_job_alive=lambda job: True, now=None):
    """This holder's slot on the resource, if it has one."""
    now = time.time() if now is None else now
    return next((e for e in _live(load(root).get(resource), is_job_alive, now) if e.get("holder") == holder), None)


def acquire(root, resources, resource, holder, *, job=True, is_job_alive=lambda job: True, blocked=None,
            ttl=None, now=None):
    """Take a slot. Returns dict(ok, slot, value, reason); asking again for a slot already held
    returns the same slot (and renews a named holder's time-to-live)."""
    now = time.time() if now is None else now
    spec = resources.get(resource)
    if spec is None:
        known = ", ".join(sorted(resources)) or "none are configured"
        return dict(ok=False, slot=None, value=None, unknown=True,
                    reason=f"there is no resource named {resource!r} (known: {known})")
    values = spec.get("values") or []
    with locked(root):
        state = load(root)
        entries = _live(state.get(resource), is_job_alive, now)
        mine = next((e for e in entries if e.get("holder") == holder), None)
        if mine is not None:
            mine["t"] = now if not mine.get("job") else mine.get("t", now)
            state[resource] = entries
            _save(root, state)
            return dict(ok=True, slot=mine["slot"], value=mine.get("value"), reason=None)
        reason = None
        # only now, with the file known to be writable and no slot already held, look at what
        # the owner is running (it can be slow)
        busy = blocked() if callable(blocked) else blocked
        if busy:
            reason = "the owner of this machine is using it: " + ", ".join(busy)
        else:
            free = [n for n in range(1, int(spec.get("slots", 1)) + 1) if n not in {e.get("slot") for e in entries}]
            if not free:
                reason = "every slot is taken (" + ", ".join(
                    f"slot {e.get('slot')}: {e.get('holder')}" for e in sorted(entries, key=lambda e: e.get("slot", 0))) + ")"
        if reason:
            if state.get(resource) != entries:      # still record that dead holders were cleared
                state[resource] = entries
                _save(root, state)
            return dict(ok=False, slot=None, value=None, reason=reason)
        slot = free[0]
        entry = dict(slot=slot, holder=holder, job=bool(job), t=now,
                     value=values[slot - 1] if slot <= len(values) else None)
        if not job:
            entry["ttl"] = ttl or DEFAULT_TTL_S
        state[resource] = entries + [entry]
        _save(root, state)
        return dict(ok=True, slot=slot, value=entry["value"], reason=None)


def release(root, holder, resource=None):
    """Give back this holder's slot on one resource, or on all of them. Returns what was freed."""
    freed = []
    with locked(root):
        state = load(root)
        for name in ([resource] if resource else list(state)):
            entries = state.get(name) if isinstance(state.get(name), list) else []
            keep = [e for e in entries if not (isinstance(e, dict) and e.get("holder") == holder)]
            if len(keep) != len(entries):
                freed.append(name)
                state[name] = keep
        if freed:
            _save(root, state)
    return freed


def snapshot(root, resources, is_job_alive=lambda job: True, now=None):
    """For display: every configured resource with its slot count and current holders."""
    now = time.time() if now is None else now
    state = load(root)
    return [dict(name=name, slots=int(spec.get("slots", 1)), rule=spec.get("rule"),
                 held=[dict(slot=e.get("slot"), holder=e.get("holder"), value=e.get("value"), since=e.get("t"))
                       for e in sorted(_live(state.get(name), is_job_alive, now), key=lambda e: e.get("slot", 0))])
            for name, spec in resources.items()]
