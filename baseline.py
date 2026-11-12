# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Shared state that every holder of a resource changes: saved when the first holder arrives, put
back and checked when the last one leaves.

Some programs keep settings in one place for the whole machine (a registry key, a file in the
user's profile). Two copies under test then overwrite each other's settings, and a holder that
saves and restores them itself can be broken by any other holder. A resource's `baseline` moves
that job to the slots:

    "test-copy": {"slots": 4, "baseline": {"registry": ["HKCU\\Software\\Vendor\\TheApp"],
                                           "files": ["%LOCALAPPDATA%/TheApp/last-session.txt"]}}

While anyone holds a slot, the saved copy is <root>/baselines/<resource>.json. Once nobody does,
the state is put back (only what differs is written), read again and compared; the outcome is kept
in <resource>.last.json. Holders never save, restore or compare that state themselves.
"""
import base64
import json
import os
import re
from pathlib import Path

DIR = "baselines"
MAX_KEYS = 2000                 # registry keys under one saved key
MAX_FILE = 8 * 1024 * 1024
SHOWN = 30                      # changes listed in the record of a restore
_ROOT = re.compile(r"(HKCU|HKLM|HKEY_CURRENT_USER|HKEY_LOCAL_MACHINE)\\[^\\]+\\[^\\]+", re.I)


def valid(spec):
    """A `baseline` config value: registry keys at least two levels below a hive (never a whole
    hive or all of Software), and files."""
    if not isinstance(spec, dict) or set(spec) - {"registry", "files"}:
        return False
    keys, files = spec.get("registry", []), spec.get("files", [])
    return (isinstance(keys, list) and all(isinstance(k, str) and _ROOT.match(k.replace("/", "\\")) for k in keys)
            and isinstance(files, list) and all(isinstance(f, str) and f.strip() for f in files)
            and bool(keys or files))


def _path(root, resource, kind=""):
    return Path(root) / DIR / f"{resource}{kind}.json"


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def is_open(root, resource):
    return _path(root, resource).is_file()


def open_names(root):
    """Resources whose shared state is saved and still owed a restore."""
    try:
        return sorted(p.stem for p in (Path(root) / DIR).glob("*.json") if not p.stem.endswith(".last"))
    except OSError:
        return []


def info(root, resource):
    """For display: (when the open baseline was saved or None, the record of the last restore or None)."""
    saved = _load(_path(root, resource))
    return (saved or {}).get("t"), _load(_path(root, resource, ".last"))


# ---------------------------------------------------------------- registry

class WinRegistry:
    """The Windows registry through the standard library. Paths are `HKCU\\...`; `rel` is a
    subkey path below the saved key ("" for the key itself)."""

    def __init__(self):
        import winreg
        self.w = winreg
        self.hives = dict(HKCU=winreg.HKEY_CURRENT_USER, HKEY_CURRENT_USER=winreg.HKEY_CURRENT_USER,
                          HKLM=winreg.HKEY_LOCAL_MACHINE, HKEY_LOCAL_MACHINE=winreg.HKEY_LOCAL_MACHINE)

    def _at(self, path, rel=""):
        hive, _, sub = path.replace("/", "\\").partition("\\")
        return self.hives[hive.upper()], sub + ("\\" + rel if rel else "")

    def read(self, path):
        """{rel: {value name: (type, data)}} for the key and every key under it; None if it is absent."""
        w, tree = self.w, {}

        def walk(rel):
            with w.OpenKey(*self._at(path, rel), 0, w.KEY_READ) as key:
                subkeys, values, _ = w.QueryInfoKey(key)
                found = {}
                for i in range(values):
                    name, data, kind = w.EnumValue(key, i)
                    found[name] = (kind, data)
                names = [w.EnumKey(key, i) for i in range(subkeys)]
            tree[rel] = found
            if len(tree) > MAX_KEYS:
                raise ValueError(f"{path} has more than {MAX_KEYS} keys")
            for name in names:
                walk(rel + "\\" + name if rel else name)

        try:
            walk("")
        except FileNotFoundError:
            if not tree:
                return None
            raise
        return tree

    def ensure(self, path, rel):
        self.w.CreateKeyEx(*self._at(path, rel), 0, self.w.KEY_WRITE).Close()

    def set(self, path, rel, name, kind, data):
        with self.w.CreateKeyEx(*self._at(path, rel), 0, self.w.KEY_SET_VALUE) as key:
            self.w.SetValueEx(key, name, 0, kind, data)

    def delete_value(self, path, rel, name):
        with self.w.OpenKey(*self._at(path, rel), 0, self.w.KEY_SET_VALUE) as key:
            self.w.DeleteValue(key, name)

    def delete_key(self, path, rel):
        self.w.DeleteKey(*self._at(path, rel))


def _registry():
    if os.name != "nt":
        raise OSError("a registry baseline needs Windows")
    return WinRegistry()


def _enc(data):
    return {"hex": data.hex()} if isinstance(data, (bytes, bytearray)) else data


def _dec(data):
    return bytes.fromhex(data["hex"]) if isinstance(data, dict) else data


def _tree(reg, path):
    """The key as plain JSON data, or None if it does not exist."""
    raw = reg.read(path)
    if raw is None:
        return None
    return {rel: {name: [kind, _enc(data)] for name, (kind, data) in values.items()} for rel, values in raw.items()}


def _depth(rel):
    return rel.count("\\") + 1 if rel else 0


def _restore_key(reg, path, saved):
    """Make the key equal to `saved`, writing only what differs. Returns what was changed back."""
    current = _tree(reg, path)
    if current == saved:
        return []
    changed = []
    label = lambda rel: path + ("\\" + rel if rel else "")
    current = current or {}
    for rel in sorted(set(current) - set(saved or {}), key=_depth, reverse=True):     # deepest first
        reg.delete_key(path, rel)
        changed.append(f"{label(rel)}: key removed (it was not there before)")
    for rel in sorted(saved or {}, key=_depth):                                       # parents first
        have, want = current.get(rel), saved[rel]
        if have is None:
            reg.ensure(path, rel)
            changed.append(f"{label(rel)}: key put back")
        for name in sorted(set(have or {}) - set(want)):
            reg.delete_value(path, rel, name)
            changed.append(f"{label(rel)}: value {name or '(default)'} removed")
        for name, (kind, data) in want.items():
            if (have or {}).get(name) != [kind, data]:
                reg.set(path, rel, name, kind, _dec(data))
                changed.append(f"{label(rel)}: value {name or '(default)'} put back")
    return changed


# ---------------------------------------------------------------- files

def _file(path):
    """The file's bytes, or None if it does not exist."""
    try:
        if os.path.getsize(path) > MAX_FILE:
            raise ValueError(f"{path} is larger than {MAX_FILE} bytes")
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _restore_file(path, saved):
    if _file(path) == saved:
        return []
    if saved is None:
        os.remove(path)
        return [f"{path}: removed (it was not there before)"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(saved)
    return [f"{path}: put back"]


# ---------------------------------------------------------------- save and restore

def save(root, resource, spec, now, registry=None):
    """Save the state named by `spec` unless a saved copy is already waiting. Returns an error
    text, or None when the baseline is in place."""
    if is_open(root, resource):
        return None
    try:
        keys = [k.replace("/", "\\") for k in spec.get("registry") or []]
        reg = (registry or _registry()) if keys else None
        data = dict(t=now,
                    registry=[dict(key=k, tree=_tree(reg, k)) for k in keys],
                    files=[])
        for name in spec.get("files") or []:
            path = os.path.abspath(os.path.expandvars(os.path.expanduser(name)))
            raw = _file(path)
            data["files"].append(dict(path=path, data=None if raw is None else base64.b64encode(raw).decode("ascii")))
        _write(_path(root, resource), data)
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def restore(root, resource, now, registry=None):
    """Put the saved state back and check it. Returns the record also kept in <resource>.last.json:
    dict(saved, restored, ok, changed=[what holders had changed], error). On failure the saved copy
    stays, so the next call tries again."""
    saved = _load(_path(root, resource))
    if saved is None:
        return dict(saved=None, restored=now, ok=False, changed=[], error="the saved copy is missing or unreadable")
    changed, error = [], None
    try:
        keys = saved.get("registry") or []
        reg = (registry or _registry()) if keys else None
        for item in keys:
            changed += _restore_key(reg, item["key"], item.get("tree"))
            if _tree(reg, item["key"]) != item.get("tree"):
                raise ValueError(f"{item['key']} still differs from the saved copy")
        for item in saved.get("files") or []:
            want = None if item.get("data") is None else base64.b64decode(item["data"])
            changed += _restore_file(item["path"], want)
            if _file(item["path"]) != want:
                raise ValueError(f"{item['path']} still differs from the saved copy")
    except (OSError, ValueError, KeyError, TypeError) as e:
        error = f"{type(e).__name__}: {e}"
    record = dict(saved=saved.get("t"), restored=now, ok=error is None, changed=changed[:SHOWN],
                  changes=len(changed), error=error)
    try:
        _write(_path(root, resource, ".last"), record)
        if error is None:
            os.remove(_path(root, resource))
    except OSError as e:
        record.update(ok=False, error=f"{type(e).__name__}: {e}")
    return record
