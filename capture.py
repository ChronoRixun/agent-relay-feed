# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Screenshots that say what they are of.

`delegate.py capture out.png --process TheApp.exe` saves the client area of that program's window
and, beside it, `out.png.capture.json`: the window's title, its program, its real pixel size,
whether it had a border, the size of the monitor it was on, whether it was in front, and a hash
of the image. The result checks read that record, so a screenshot of the wrong window, a cropped
one or a blank one is flagged without anyone opening the picture.

Windows only (ctypes, no extra packages). Other systems report that the capture is not supported.
"""
import hashlib
import json
import os
import struct
import time
import zlib

RECORD_SUFFIX = ".capture.json"
TOOL = "delegate.py capture"


def record_path(image):
    return str(image) + RECORD_SUFFIX


def load_record(image):
    """The capture record written beside an image, or None."""
    try:
        with open(record_path(image), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("tool") == TOOL else None


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def describe(rec):
    """One line for people: what the capture was of."""
    bits = [f"{rec.get('width')}x{rec.get('height')}", "borderless" if rec.get("borderless") else "with a window border"]
    mon = rec.get("monitor") or []
    if len(mon) == 2:
        bits.append(f"{'fills' if rec.get('fills_monitor') else 'does not fill'} the {mon[0]}x{mon[1]} monitor")
    shown = rec.get("displayed") or []
    if rec.get("scaled") and len(shown) == 2:
        bits.append(f"stretched to {shown[0]}x{shown[1]} on screen by Windows display scaling")
    if not rec.get("foreground"):
        bits.append("not in front")
    return f"{rec.get('process') or 'unknown program'} \"{rec.get('title') or ''}\" ({', '.join(bits)})"


def write_png(path, width, height, bgra):
    """A PNG from top-down 32-bit BGRA pixels (as Windows hands them over); alpha is dropped."""
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def is_blank(bgra, samples=5003):
    """True if every sampled pixel has the same colour (a black or empty capture)."""
    step = 4 * max(1, len(bgra) // 4 // samples)
    first = bgra[:3]
    return all(bgra[i:i + 3] == first for i in range(0, len(bgra) - 3, step))


def pick(windows, process=None, title=None, pid=None, foreground=False):
    """The windows that match what was asked for, best first: in front, then largest."""
    def exe(name):
        name = str(name or "").casefold()
        return name[:-4] if name.endswith(".exe") else name

    found = []
    for w in windows:
        if foreground and not w["foreground"]:
            continue
        if process and exe(w["process"]) != exe(os.path.basename(process)):
            continue
        if pid and w["pid"] != pid:
            continue
        if title and title.casefold() not in w["title"].casefold():
            continue
        found.append(w)
    return sorted(found, key=lambda w: (not w["foreground"], -(w["client"][0] * w["client"][1])))


# ---------------------------------------------------------------- Windows

def _win():
    import ctypes
    from ctypes import wintypes
    u, g, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True), \
        ctypes.WinDLL("kernel32", use_last_error=True)
    H, B, I, U, P = ctypes.c_void_p, wintypes.BOOL, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p
    for fn, res, args in (
            (u.IsWindowVisible, B, [H]), (u.IsIconic, B, [H]), (u.GetWindowTextLengthW, I, [H]),
            (u.GetWindowTextW, I, [H, ctypes.c_wchar_p, I]), (u.GetWindowThreadProcessId, wintypes.DWORD, [H, P]),
            (u.GetForegroundWindow, H, []), (u.GetClientRect, B, [H, P]), (u.ClientToScreen, B, [H, P]),
            (u.GetWindowLongW, ctypes.c_long, [H, I]), (u.MonitorFromWindow, H, [H, wintypes.DWORD]),
            (u.GetMonitorInfoW, B, [H, P]), (u.GetDC, H, [H]), (u.ReleaseDC, I, [H, H]), (u.PrintWindow, B, [H, H, U]),
            (g.CreateCompatibleDC, H, [H]), (g.CreateCompatibleBitmap, H, [H, I, I]), (g.SelectObject, H, [H, H]),
            (g.BitBlt, B, [H, I, I, I, I, H, I, I, wintypes.DWORD]), (g.GetDIBits, I, [H, H, U, U, P, P, U]),
            (g.DeleteObject, B, [H]), (g.DeleteDC, B, [H]),
            (k.OpenProcess, H, [wintypes.DWORD, B, wintypes.DWORD]), (k.CloseHandle, B, [H]),
            (k.QueryFullProcessImageNameW, B, [H, wintypes.DWORD, ctypes.c_wchar_p, P])):
        fn.restype, fn.argtypes = res, args
    try:    # real pixels, not sizes scaled by the display setting
        u.SetProcessDpiAwarenessContext.restype, u.SetProcessDpiAwarenessContext.argtypes = B, [H]
        u.SetProcessDpiAwarenessContext(H(-4))
    except (AttributeError, OSError):
        pass
    return ctypes, wintypes, u, g, k


def list_windows():
    """Every visible, titled, not minimised top-level window, with its program and geometry."""
    ctypes, wintypes, u, g, k = _win()
    try:
        dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        dwm = None
    front = u.GetForegroundWindow()
    handles = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)(lambda h, _l: handles.append(h) or True)
    u.EnumWindows(proc, 0)
    out = []
    for h in handles:
        if not u.IsWindowVisible(h) or u.IsIconic(h):
            continue
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            continue
        if dwm is not None:
            cloaked = wintypes.DWORD(0)
            dwm.DwmGetWindowAttribute(ctypes.c_void_p(h), 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                continue        # a store app parked in the background: listed but not on screen
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        rect = wintypes.RECT()
        u.GetClientRect(h, ctypes.byref(rect))
        if rect.right <= 0 or rect.bottom <= 0:
            continue
        origin = wintypes.POINT(0, 0)
        u.ClientToScreen(h, ctypes.byref(origin))
        pid = wintypes.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        path = ""
        handle = k.OpenProcess(0x1000, False, pid.value)
        if handle:
            name, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                path = name.value
            k.CloseHandle(handle)
        style = u.GetWindowLongW(h, -16)
        monitor = [0, 0, 0, 0]
        mon = u.MonitorFromWindow(h, 2)
        if mon:
            info = (ctypes.c_long * 10)()
            info[0] = ctypes.sizeof(info)
            if u.GetMonitorInfoW(mon, ctypes.byref(info)):
                monitor = [info[1], info[2], info[3], info[4]]
        out.append(dict(hwnd=h, title=buf.value, pid=pid.value, exe=path, process=os.path.basename(path),
                        client=[rect.right, rect.bottom], origin=[origin.x, origin.y],
                        borderless=not (style & 0x00C00000 == 0x00C00000 or style & 0x00040000),
                        monitor=[monitor[2] - monitor[0], monitor[3] - monitor[1]], monitor_origin=monitor[:2],
                        foreground=h == front))
    return out


def grab(window, method):
    """(width, height, BGRA pixels) of a window's client area: copied off the screen, or drawn by
    the window itself (works when it is covered, but not for every program)."""
    ctypes, wintypes, u, g, k = _win()
    w, h = window["client"]
    hwnd = ctypes.c_void_p(window["hwnd"])
    restore = None
    if method != "screen":
        # A program that ignores display scaling draws at its own, smaller size and Windows stretches
        # it. Ask in that program's terms, so the picture is what it drew and not a corner of it.
        try:
            u.GetWindowDpiAwarenessContext.restype, u.GetWindowDpiAwarenessContext.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
            u.SetThreadDpiAwarenessContext.restype, u.SetThreadDpiAwarenessContext.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
            restore = u.SetThreadDpiAwarenessContext(u.GetWindowDpiAwarenessContext(hwnd))
            rect = wintypes.RECT()
            if u.GetClientRect(hwnd, ctypes.byref(rect)) and rect.right > 0 and rect.bottom > 0:
                w, h = rect.right, rect.bottom
        except (AttributeError, OSError):
            restore = None
    src = u.GetDC(None if method == "screen" else hwnd)
    mem = g.CreateCompatibleDC(src)
    bmp = g.CreateCompatibleBitmap(src, w, h)
    old = g.SelectObject(mem, bmp)
    try:
        if method == "screen":
            ok = g.BitBlt(mem, 0, 0, w, h, src, window["origin"][0], window["origin"][1], 0x00CC0020 | 0x40000000)
        else:
            ok = u.PrintWindow(hwnd, mem, 1 | 2)
        if not ok:
            raise OSError(f"Windows refused the {method} capture")
        header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        info = ctypes.create_string_buffer(header + b"\0" * 16)
        pixels = ctypes.create_string_buffer(w * h * 4)
        g.SelectObject(mem, old)
        if g.GetDIBits(mem, bmp, 0, h, pixels, info, 0) != h:
            raise OSError("could not read the captured pixels")
        return w, h, pixels.raw
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None if method == "screen" else hwnd, src)
        if restore:
            u.SetThreadDpiAwarenessContext(restore)


def capture(out, process=None, title=None, pid=None, foreground=False, method="auto", windows=None, grabber=grab):
    """Capture the matching window to `out` (PNG) and write its record. Returns dict(ok, ...):
    on success the record; otherwise `reason` and the visible windows, so the caller can pick."""
    if windows is None:
        if os.name != "nt":
            return dict(ok=False, reason="window capture is only supported on Windows", windows=[])
        windows = list_windows()
    if not (process or title or pid or foreground):
        return dict(ok=False, reason="say which window: --process, --window, --pid or --foreground", windows=windows)
    found = pick(windows, process, title, pid, foreground)
    if not found:
        return dict(ok=False, reason="no visible window matches (it may be minimised or not running)", windows=windows)
    win = found[0]
    # copying the screen shows what was really displayed, but only if the window was in front
    used = method if method != "auto" else ("screen" if win["foreground"] else "window")
    w, h, pixels = grabber(win, used)
    blank = is_blank(pixels)
    if blank and method == "auto":      # some programs draw nothing one way and fine the other
        other = "window" if used == "screen" else "screen"
        try:
            w2, h2, pixels2 = grabber(win, other)
            if not is_blank(pixels2) and (other == "window" or win["foreground"]):
                w, h, pixels, used, blank = w2, h2, pixels2, other, False
        except OSError:
            pass
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_png(out, w, h, pixels)
    mon = win["monitor"]
    rec = dict(tool=TOOL, t=time.time(), path=out, width=w, height=h, title=win["title"], process=win["process"],
               exe=win["exe"], pid=win["pid"], borderless=bool(win["borderless"]), foreground=bool(win["foreground"]),
               # the size on the monitor; differs from the picture when Windows stretches the program
               displayed=list(win["client"]), scaled=[w, h] != list(win["client"]),
               monitor=mon, fills_monitor=bool(win["borderless"] and list(win["client"]) == mon
                                               and win["origin"] == win.get("monitor_origin", win["origin"])),
               method=used, blank=blank, other_matches=len(found) - 1, sha256=file_hash(out))
    with open(record_path(out), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    return dict(ok=True, **rec)


def check(entry, image, rec):
    """Findings (ok, text) comparing an evidence entry's claims with the image's capture record."""
    found = []
    try:
        same = file_hash(image) == rec.get("sha256")
    except OSError:
        same = False
    if not same:
        found.append((False, f"image was changed after it was captured (cropped or edited); the capture was "
                             f"{describe(rec)}: {image}"))
        return found
    if rec.get("blank"):
        found.append((False, f"capture is one flat colour (blank), so it shows nothing: {image}"))
    problems = []
    claimed = entry.get("process")
    if claimed:
        strip = lambda n: str(n).casefold().removesuffix(".exe")
        if strip(os.path.basename(str(claimed))) != strip(rec.get("process")):
            problems.append(f"it is of {rec.get('process') or 'an unknown program'}, NOT the claimed {claimed}")
    if entry.get("window") and str(entry["window"]).casefold() not in str(rec.get("title", "")).casefold():
        problems.append(f"its window title is \"{rec.get('title')}\", which does not contain the claimed \"{entry['window']}\"")
    shows = str(entry.get("shows") or "").casefold()
    says_borderless = entry.get("borderless") is True or (
        entry.get("borderless") is None and "borderless" in shows
        and not any(no in shows for no in ("not borderless", "no borderless", "non-borderless", "instead of borderless")))
    if says_borderless and not rec.get("borderless"):
        problems.append("the window had a title bar or resize border, so it was NOT borderless")
    if entry.get("borderless") is False and rec.get("borderless"):
        problems.append("the window was borderless, though the claim says it was not")
    if entry.get("fullscreen") is True and not rec.get("fills_monitor"):
        problems.append("the window did not fill its monitor")
    for p in problems:
        found.append((False, f"capture record disagrees: {p} ({describe(rec)}): {image}"))
    if not problems and not rec.get("blank"):
        found.append((True, f"captured from {describe(rec)}: {image}"))
    return found
