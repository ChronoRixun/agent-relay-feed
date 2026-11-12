# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Slots for things on one machine that agents must not use at the same time: a build, a copy of
an application under test, an emulator. The `resources` config key names them:

    "resources": {
      "build":     {"slots": 1, "rule": "any compiler or full test-suite run"},
      "test-copy": {"slots": 2, "values": ["/work/copy-1", "/work/copy-2"],
                    "yield_to": ["TheApp.exe"], "rule": "launching the application under test",
                    "baseline": {"registry": ["HKCU\\Software\\Vendor\\TheApp"]}}
    }

`slots` is how many holders at once; `values` (optional, one per slot) is what a holder is handed,
such as its own folder; `yield_to` (optional) names programs of the machine's owner: while one of
them runs and is not an agent's own, nobody is granted a slot; `baseline` (optional, see
baseline.py) is state all holders share: saved when the first holder arrives, put back when the
last one leaves. A slot belongs to a holder: a job id (freed when that job ends) or a free-form
name (freed when its time-to-live runs out). A holder may ask for several slots at once.

The state is one JSON file under a lock, so any process on the machine can take part.
"""
import contextlib
import json
import os
import time
from pathlib import Path

import baseline

STATE = "leases.json"
DEFAULT_TTL_S = 3 * 3600        # for holders that are not jobs: renew by leasing again
WAITING = "_waiting"            # state key: who asked for a slot and was refused
WAITING_FRESH_S = 15 * 60       # a refused holder counts as waiting this long after it last asked
WAITING_WRITE_S = 20            # how often a repeated refusal is written down again


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


def _waiters(state, resource, is_job_alive, now):
    """{holder: record} of those refused a slot on the resource and still likely to want it."""
    found = (state.get(WAITING) or {}).get(resource)
    return {h: w for h, w in (found.items() if isinstance(found, dict) else [])
            if isinstance(w, dict) and now - w.get("seen", 0) < WAITING_FRESH_S
            and (not w.get("job") or is_job_alive(h))}


def _note_waiting(state, resource, holder, job, is_job_alive, now):
    """Record a refusal. Returns whether the state file needs writing (not on every retry)."""
    waiting = _waiters(state, resource, is_job_alive, now)
    mine = waiting.get(holder)
    fresh = mine is not None and now - mine.get("seen", 0) < WAITING_WRITE_S
    if not fresh:
        waiting[holder] = dict(since=(mine or {}).get("since", now), seen=now, job=bool(job))
        state.setdefault(WAITING, {})[resource] = waiting
    return not fresh


def _stop_waiting(state, resource, holder):
    found = (state.get(WAITING) or {}).get(resource)
    if isinstance(found, dict):
        found.pop(holder, None)


def holding(root, resource, holder, is_job_alive=lambda job: True, now=None):
    """Every slot this holder has on the resource, lowest first."""
    now = time.time() if now is None else now
    return sorted((e for e in _live(load(root).get(resource), is_job_alive, now) if e.get("holder") == holder),
                  key=lambda e: e.get("slot", 0))


def held(root, resource, holder, is_job_alive=lambda job: True, now=None):
    """This holder's (first) slot on the resource, if it has one."""
    return next(iter(holding(root, resource, holder, is_job_alive, now)), None)


def _granted(mine):
    mine = sorted(mine, key=lambda e: e.get("slot", 0))
    return dict(ok=True, slot=mine[0]["slot"], value=mine[0].get("value"), reason=None,
                slots=[e["slot"] for e in mine], values=[e.get("value") for e in mine])


def acquire(root, resources, resource, holder, *, job=True, is_job_alive=lambda job: True, blocked=None,
            ttl=None, now=None, count=1, registry=None):
    """Take `count` slots (all of them or none). Returns dict(ok, slot, value, slots, values,
    reason); asking again for slots already held returns the same ones (and renews a named
    holder's time-to-live). The first holder of a resource with a `baseline` has that state saved
    before it is granted anything."""
    now = time.time() if now is None else now
    spec = resources.get(resource)
    if spec is None:
        known = ", ".join(sorted(resources)) or "none are configured"
        return dict(ok=False, slot=None, value=None, unknown=True,
                    reason=f"there is no resource named {resource!r} (known: {known})")
    total = int(spec.get("slots", 1))
    if not 1 <= count <= total:
        return dict(ok=False, slot=None, value=None, unknown=True,
                    reason=f"{resource} has {total} slot{'s' if total != 1 else ''}, so {count} can never be granted")
    values = spec.get("values") or []
    with locked(root):
        state = load(root)
        entries = _live(state.get(resource), is_job_alive, now)
        mine = [e for e in entries if e.get("holder") == holder]
        if len(mine) >= count:
            for e in mine:
                e["t"] = now if not e.get("job") else e.get("t", now)
            state[resource] = entries
            _stop_waiting(state, resource, holder)
            _save(root, state)
            return _granted(mine)
        reason = None
        # only now, with the file known to be writable and too few slots held, look at what
        # the owner is running (it can be slow)
        busy = blocked() if callable(blocked) else blocked
        if busy:
            reason = "the owner of this machine is using it: " + ", ".join(busy)
        else:
            free = [n for n in range(1, total + 1) if n not in {e.get("slot") for e in entries}]
            need = count - len(mine)
            if len(free) < need:
                others = ", ".join(f"slot {e.get('slot')}: {e.get('holder')}"
                                   for e in sorted(entries, key=lambda e: e.get("slot", 0)))
                reason = (f"every slot is taken ({others})" if not free
                          else f"{need} free slots are needed and {len(free)} {'is' if len(free) == 1 else 'are'} "
                               f"free ({others})")
            elif spec.get("baseline") and not baseline.is_open(root, resource):
                if entries:
                    # holders from before `baseline` was configured are mid-run, so what is there
                    # now may already be changed: wait until they have gone rather than save that
                    reason = ("its shared state is saved only once the holders that started before that was "
                              "set up have finished (" + ", ".join(sorted({str(e.get("holder")) for e in entries})) + ")")
                else:
                    # nobody is using it, so what is there now is the state to put back later
                    error = baseline.save(root, resource, spec["baseline"], now, registry)
                    if error:
                        reason = f"the shared state could not be saved first ({error})"
        if reason:
            wrote = _note_waiting(state, resource, holder, job, is_job_alive, now)
            if wrote or state.get(resource) != entries:     # also records that dead holders were cleared
                state[resource] = entries
                _save(root, state)
            return dict(ok=False, slot=None, value=None, reason=reason)
        for slot in free[:need]:
            entry = dict(slot=slot, holder=holder, job=bool(job), t=now,
                         value=values[slot - 1] if slot <= len(values) else None)
            if not job:
                entry["ttl"] = ttl or DEFAULT_TTL_S
            mine.append(entry)
            entries.append(entry)
        state[resource] = entries
        _stop_waiting(state, resource, holder)
        _save(root, state)
        return _granted(mine)


def release(root, holder, resource=None):
    """Give back this holder's slots on one resource, or on all of them. Returns what was freed.
    Call settle() afterwards so shared state is put back once the last holder has left."""
    freed = []
    with locked(root):
        state = load(root)
        for name in ([resource] if resource else [n for n in state if n != WAITING]):
            entries = state.get(name) if isinstance(state.get(name), list) else []
            keep = [e for e in entries if not (isinstance(e, dict) and e.get("holder") == holder)]
            if len(keep) != len(entries):
                freed.append(name)
                state[name] = keep
        if freed:
            _save(root, state)
    return freed


def settle(root, is_job_alive=lambda job: True, now=None, registry=None):
    """Put saved shared state back for every resource that no longer has a holder. Returns one
    record per restore tried: dict(resource, ok, changed, error, ...). Cheap when nothing is saved."""
    names = baseline.open_names(root)
    if not names:
        return []
    now = time.time() if now is None else now
    done = []
    with locked(root):
        state = load(root)
        for name in names:
            if not _live(state.get(name), is_job_alive, now):
                done.append(dict(baseline.restore(root, name, now, registry), resource=name))
    return done


def snapshot(root, resources, is_job_alive=lambda job: True, now=None):
    """For display: every configured resource with its slot count, holders, who is waiting, and
    its shared state (when it was saved, or how the last restore went)."""
    now = time.time() if now is None else now
    state = load(root)
    rows = []
    for name, spec in resources.items():
        saved, last = baseline.info(root, name) if spec.get("baseline") or baseline.is_open(root, name) else (None, None)
        rows.append(dict(
            name=name, slots=int(spec.get("slots", 1)), rule=spec.get("rule"),
            held=[dict(slot=e.get("slot"), holder=e.get("holder"), value=e.get("value"), since=e.get("t"))
                  for e in sorted(_live(state.get(name), is_job_alive, now), key=lambda e: e.get("slot", 0))],
            waiting=[dict(holder=h, since=w.get("since"))
                     for h, w in sorted(_waiters(state, name, is_job_alive, now).items(),
                                        key=lambda kv: kv[1].get("since", 0))],
            baseline=saved, restored=last))
    return rows
