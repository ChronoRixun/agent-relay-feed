# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Screenshots that say what they are of.

`delegate.py capture out.png --process TheApp.exe` saves the client area of that program's window
and, beside it, `out.png.capture.json`: the window's title, its program, its real pixel size,
whether it had a border, the size of the monitor it was on, whether it was in front, and a hash
of the image. The result checks read that record, so a screenshot of the wrong window, a cropped
one or a blank one is flagged without anyone opening the picture.

On Windows it uses ctypes (no extra packages). On Linux, and for a program running under Wine or
Proton, it goes through X instead: the window is found with xwininfo/xprop on the agent's own
private display and copied with xwd, pixel for pixel. It never uses the WSLg desktop display.
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
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
    bits = [f"{rec.get('width')}x{rec.get('height')}"]
    if rec.get("borderless") is not None:
        bits.append("borderless" if rec["borderless"] else "with a window border")
    mon = rec.get("monitor") or []
    if len(mon) == 2:
        bits.append(f"{'fills' if rec.get('fills_monitor') else 'does not fill'} the {mon[0]}x{mon[1]} monitor")
    shown = rec.get("displayed") or []
    if rec.get("scaled") and len(shown) == 2:
        bits.append(f"stretched to {shown[0]}x{shown[1]} on screen by Windows display scaling")
    if rec.get("display"):
        bits.append(f"X display {rec['display']}, copied with xwd")
    elif not rec.get("foreground"):
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


# ---------------------------------------------------------------- X11 (Linux, Wine, Proton)
#
# A program under Wine or Proton draws into an X window, and Windows' own capture calls made
# from inside Wine return a flat colour. So on Linux, and when this runs under Wine, the window is
# found with xwininfo/xprop and copied with xwd from the X display the agent was given.

X_TOOLS_HINT = "the X tools xwininfo, xprop and xwd are needed (packages x11-utils and x11-apps)"
_TREE = re.compile(r'^(\s*)(0x[0-9a-fA-F]+) (?:"(.*)"|\(has no name\)): \((.*?)\)\s+'
                   r'(\d+)x(\d+)[+-]-?\d+[+-]-?\d+\s+([+-]-?\d+)([+-]-?\d+)\s*$')


def under_wine():
    """True when this Python is a Windows program running under Wine or Proton."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return hasattr(ctypes.WinDLL("ntdll"), "wine_get_version")
    except OSError:
        return False


def in_wsl(env=None):
    env = os.environ if env is None else env
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def x_display(display=None, env=None, wsl=None):
    """(display, None), or (None, why not). Only a private display will do: under WSL, `:0` is the
    desktop the machine's owner is using."""
    env = os.environ if env is None else env
    display = (display or env.get("DISPLAY") or "").strip()
    if not display:
        return None, ("no X display is set (DISPLAY is empty); run the program on a private display and pass "
                      "that display, for example DISPLAY=:73 or --display :73")
    if (in_wsl(env) if wsl is None else wsl) and re.fullmatch(r"(?:localhost|unix)?:0(?:\.0)?", display):
        return None, (f"DISPLAY is {display}, the WSLg desktop the machine's owner uses; this only captures from a "
                      "private display (for example DISPLAY=:73 or --display :73)")
    return display, None


def run_x(argv, display, timeout=30):
    """Run one X tool on the display and return its output."""
    try:
        p = subprocess.run(argv, env=dict(os.environ, DISPLAY=display), capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise OSError(f"{argv[0]} is not installed; {X_TOOLS_HINT}") from None
    except subprocess.TimeoutExpired:
        raise OSError(f"{argv[0]} did not answer on display {display}") from None
    if p.returncode:
        raise OSError(f"{argv[0]} failed on display {display}: "
                      f"{' '.join(p.stderr.decode('utf-8', 'replace').split())[:200] or 'no message'}")
    return p.stdout.decode("utf-8", "replace")


def _field(text, label, cast=int, default=None):
    m = re.search(r"^\s*" + re.escape(label) + r":?\s*(\S+)", text, re.M)
    try:
        return cast(m.group(1)) if m else default
    except ValueError:
        return default


def process_of(pid):
    """(program file name, its path) for a process id on this machine, as far as /proc tells."""
    for name in (f"/proc/{pid}/cmdline", f"/proc/{pid}/comm"):
        try:
            with open(name, "rb") as f:
                first = f.read(4096).split(b"\0")[0].decode("utf-8", "replace").strip()
        except OSError:
            continue
        if first:
            return re.split(r"[\\/]", first)[-1], first
    return "", ""


def parse_tree(text):
    """The named windows in `xwininfo -root -tree` output: id, title, class, size, position and
    whether the window sits directly on the root (no window-manager frame around it)."""
    found = []
    for line in text.splitlines():
        m = _TREE.match(line)
        if not m or m.group(3) is None:
            continue
        indent, wid, title, klass, w, h, x, y = m.groups()
        names = re.findall(r'"([^"]*)"', klass)
        found.append(dict(hwnd=int(wid, 16), title=title, klass=names[0] if names else "", client=[int(w), int(h)],
                          origin=[int(x.replace("+", "", 1)), int(y.replace("+", "", 1))], top=len(indent) <= 5))
    return found


def x_list_windows(display, run=None, process_of=None, limit=60):
    """Every named, viewable window on the X display, in the same shape as list_windows()."""
    run, process_of = run or run_x, process_of or globals()["process_of"]
    root = run(["xwininfo", "-root"], display)
    screen = [_field(root, "Width", default=0), _field(root, "Height", default=0)]
    try:
        active = re.search(r"window id # (0x[0-9a-fA-F]+)", run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], display))
        front = int(active.group(1), 16) if active else None
    except OSError:
        front = None
    out = []
    candidates = [w for w in parse_tree(run(["xwininfo", "-root", "-tree"], display)) if w["client"][0] * w["client"][1] >= 64]
    for w in sorted(candidates, key=lambda w: -(w["client"][0] * w["client"][1]))[:limit]:
        wid = hex(w["hwnd"])
        try:
            info = run(["xwininfo", "-id", wid], display)
            props = run(["xprop", "-id", wid, "_NET_WM_PID", "_MOTIF_WM_HINTS"], display)
        except OSError:
            continue                    # the window went away while we looked
        if _field(info, "Map State", str) != "IsViewable":
            continue
        pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", props)
        pid = int(pid.group(1)) if pid else 0
        name, path = process_of(pid) if pid else ("", "")
        hints = re.search(r"_MOTIF_WM_HINTS\(_MOTIF_WM_HINTS\) = (.+)", props)
        hints = [int(v, 16) for v in re.findall(r"0x[0-9a-fA-F]+", hints.group(1))] if hints else []
        undecorated = len(hints) >= 3 and bool(hints[0] & 2) and hints[2] == 0
        # A Wine program can draw its own title bar inside its X window, so X cannot tell whether
        # it has a border: that stays unknown instead of being guessed.
        wine = (name or w["klass"]).lower().endswith(".exe")
        frameless = bool(w["top"] or undecorated or _field(info, "Override Redirect State", str) == "yes")
        out.append(dict(hwnd=w["hwnd"], title=w["title"], pid=pid, exe=path, process=name or w["klass"],
                        client=[_field(info, "Width", default=w["client"][0]), _field(info, "Height", default=w["client"][1])],
                        origin=[_field(info, "Absolute upper-left X", default=w["origin"][0]),
                                _field(info, "Absolute upper-left Y", default=w["origin"][1])],
                        borderless=None if wine else frameless,
                        monitor=screen, monitor_origin=[0, 0],
                        foreground=None if front is None else w["hwnd"] == front, display=display))
    return out


def parse_xwd(data):
    """(width, height, top-down BGRA pixels) from an xwd dump, pixel for pixel: nothing is cropped or scaled."""
    if len(data) < 100:
        raise OSError("xwd wrote no image")
    (header, version, fmt, _depth, width, height, xoffset, byte_order, _unit, _bit_order, _pad, bpp, stride, _visual,
     red, green, blue, _bits, _entries, ncolors) = struct.unpack(">20I", data[:80])
    n = bpp // 8
    if version != 7 or fmt != 2 or bpp not in (24, 32):
        raise OSError(f"this xwd image is not supported (version {version}, format {fmt}, {bpp} bits per pixel)")
    start = header + 12 * ncolors
    if width <= 0 or height <= 0 or len(data) < start + (height - 1) * stride + (xoffset + width) * n:
        raise OSError("the xwd image is cut short")

    def byte_of(mask):      # which byte of a pixel holds this colour, if it is a plain 8-bit channel
        shift = (mask & -mask).bit_length() - 1
        if mask == 0 or shift % 8 or mask >> shift != 0xFF:
            raise OSError(f"this xwd image has an unusual colour layout (mask {mask:#x})")
        return shift // 8 if byte_order == 0 else n - 1 - shift // 8

    r, g, b = byte_of(red), byte_of(green), byte_of(blue)
    rows = b"".join(data[start + y * stride + xoffset * n: start + y * stride + (xoffset + width) * n] for y in range(height))
    bgra = bytearray(width * height * 4)
    bgra[0::4], bgra[1::4], bgra[2::4] = rows[b::n], rows[g::n], rows[r::n]
    return width, height, bytes(bgra)


def grab_x(window, _method="xwd", run=None):
    """The window's own pixels, copied by the X server. Nothing is raised, moved or activated."""
    run = run or run_x
    handle, tmp = tempfile.mkstemp(suffix=".xwd")
    os.close(handle)
    try:
        run(["xwd", "-silent", "-nobdrs", "-id", hex(window["hwnd"]), "-out", tmp], window["display"])
        with open(tmp, "rb") as f:
            return parse_xwd(f.read())
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def host_path(path):
    """The Windows spelling of a WSL path under /mnt/<drive>/ (what the machine's other programs
    and the result checks open); any other path unchanged."""
    m = re.fullmatch(r"/mnt/([a-zA-Z])(/.*)?", str(path))
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if m else str(path)


def from_wine(argv, display=None, timeout=90, sleep=time.sleep):
    """Called under Wine: run `delegate.py capture` again with the machine's own python3, which
    captures through X, and return (exit code, its output). Wine starts a Unix program but hands
    back no handle to wait on, so the script leaves its exit code in a file."""
    import ctypes

    def winepath(flag, path):
        p = subprocess.run(["winepath", flag, path], capture_output=True, text=True, timeout=30)
        text = p.stdout.strip()
        if p.returncode or not text:
            raise OSError(f"winepath could not translate {path}")
        return text

    display, reason = x_display(display)
    if display is None:
        return 1, f"NOT CAPTURED: {reason}."
    quote = lambda s: "'" + str(s).replace("'", "'\\''") + "'"
    base = f"/tmp/relay-capture-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    script, log, done = (winepath("-w", base + ext) for ext in (".sh", ".log", ".rc"))
    tool = winepath("-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegate.py"))
    args = []
    for i, a in enumerate(argv):        # the PNG path is the one argument that names a file
        args.append(winepath("-u", os.path.abspath(a)) if i == 0 and not a.startswith("-") else a)
    with open(script, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\n"
                f"DISPLAY={quote(display)} python3 {quote(tool)} capture {' '.join(quote(a) for a in args)} "
                f"> {quote(base + '.log')} 2>&1\n"
                f"echo $? > {quote(base + '.rc')}\n")
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        startup = ctypes.create_string_buffer(104 if wide else 68)      # STARTUPINFOW, zeroed but for its size
        struct.pack_into("<I", startup, 0, len(startup))
        made = ctypes.create_string_buffer(24 if wide else 16)          # PROCESS_INFORMATION
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([winepath("-w", "/bin/sh"), base + ".sh"]))
        if not k.CreateProcessW(None, command, None, None, False, 0x08000000, None, None, startup, made):
            raise OSError(f"Wine could not start /bin/sh (error {ctypes.get_last_error()})")
        deadline = time.time() + timeout
        while not os.path.exists(done) and time.time() < deadline:
            sleep(0.2)
        if not os.path.exists(done):
            raise OSError("the capture started from Wine did not finish")
        sleep(0.1)
        with open(done, encoding="utf-8") as f:
            code = int(f.read().strip() or 1)
        with open(log, encoding="utf-8", errors="replace") as f:
            return code, f.read().rstrip()
    finally:
        for name in (script, log, done):
            try:
                os.remove(name)
            except OSError:
                pass


def capture(out, process=None, title=None, pid=None, foreground=False, method="auto", windows=None, grabber=None,
            display=None):
    """Capture the matching window to `out` (PNG) and write its record. Returns dict(ok, ...):
    on success the record; otherwise `reason` and the visible windows, so the caller can pick."""
    if windows is None:
        if os.name == "nt":
            windows = list_windows()
        else:
            display, reason = x_display(display)
            if display is None:
                return dict(ok=False, reason=reason, windows=[])
            windows = x_list_windows(display)
    if grabber is None:
        grabber = grab_x if any(w.get("display") for w in windows) else grab
    if not (process or title or pid or foreground):
        return dict(ok=False, reason="say which window: --process, --window, --pid or --foreground", windows=windows)
    found = pick(windows, process, title, pid, foreground)
    if not found:
        return dict(ok=False, reason="no visible window matches (it may be minimised or not running)", windows=windows)
    win = found[0]
    on_x = bool(win.get("display"))
    # copying the screen shows what was really displayed, but only if the window was in front
    used = "xwd" if on_x else method if method != "auto" else ("screen" if win["foreground"] else "window")
    w, h, pixels = grabber(win, used)
    blank = is_blank(pixels)
    if blank and method == "auto" and not on_x:     # some programs draw nothing one way and fine the other
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
               exe=win["exe"], pid=win["pid"], foreground=bool(win["foreground"]),
               borderless=None if win["borderless"] is None else bool(win["borderless"]),
               # the size on the monitor; differs from the picture when Windows stretches the program
               displayed=list(win["client"]), scaled=[w, h] != list(win["client"]),
               monitor=mon, fills_monitor=bool(win["borderless"] is not False and list(win["client"]) == mon
                                               and win["origin"] == win.get("monitor_origin", win["origin"])),
               method=used, blank=blank, other_matches=len(found) - 1, sha256=file_hash(out))
    if on_x:
        rec.update(display=win["display"], platform="x11", host_path=host_path(out))
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
    unknown = ""
    if rec.get("borderless") is None and (says_borderless or entry.get("borderless") is False):
        unknown = "; whether it had a border cannot be read from an X capture of a Wine program, so that claim is unchecked"
    elif says_borderless and not rec.get("borderless"):
        problems.append("the window had a title bar or resize border, so it was NOT borderless")
    if entry.get("borderless") is False and rec.get("borderless"):
        problems.append("the window was borderless, though the claim says it was not")
    if entry.get("fullscreen") is True and not rec.get("fills_monitor"):
        problems.append("the window did not fill its monitor")
    for p in problems:
        found.append((False, f"capture record disagrees: {p} ({describe(rec)}): {image}"))
    if not problems and not rec.get("blank"):
        found.append((True, f"captured from {describe(rec)}{unknown}: {image}"))
    return found
