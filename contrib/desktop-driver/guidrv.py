"""Desktop screenshot / click / type driver for ONE application window (Windows, DRAFT).

Meant to be called by a model agent through a fenced Bash allow-rule (see README.md).
It is a probe tool, not a product. A sent action is not proof of anything: take a
screenshot after every action and look at it.

Target (required; set by environment variables or by the same flags before the command):
  GUIDRV_EXE    process image name, e.g. notepad.exe
  GUIDRV_TITLE  window-title substring (case-sensitive), e.g. Notepad
  --exe NAME / --title TEXT   override the environment for one call
At least one of the two must be set. When both are set, a window must match both.

Usage (python guidrv.py [--exe NAME] [--title TEXT] <command> ...):
  windows                         list visible top-level windows of the target (hwnd, title, rect)
  shot <out.png> [hwnd|-] [maxw]  screenshot the target window, downscaled to maxw (default 1920).
                                  Also writes <out>_full.png at full resolution.
  shotrect <out.png> L T R B      screenshot a physical-pixel screen rectangle (tight crops)
  focus [hwnd|-]                  bring the target window to the foreground
  click X Y [hwnd|-] [right]      click at window-relative PHYSICAL pixel X,Y
  dclick X Y [hwnd|-]             double-click at window-relative X,Y
  type "text"                     type unicode text into the focused control
  key NAME [NAME ...]             press a chord, e.g. key ctrl o | key enter | key esc
                                  alt+f4 is refused.

Every command prints one JSON object. ok is false, and the exit status is 1, on any failure.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

# user32 exists only on Windows. The module still imports elsewhere, so main() can fail with JSON.
user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None

if user32 is not None:
    try:
        # PER_MONITOR_AWARE_V2 (-4), set before any coordinate call, so window rectangles and
        # cursor positions are physical pixels on mixed-DPI displays.
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass  # older Windows has no such API
    user32.GetForegroundWindow.restype = wt.HWND

CFG = {"exe": None, "title": None}


def out(**kw):
    print(json.dumps(kw))


def fail(**kw):
    """Print a JSON failure object and exit with status 1."""
    kw["ok"] = False
    out(**kw)
    sys.exit(1)


def _pid_of(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _image_pids(exe):
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, errors="replace")
    pids = set()
    for line in r.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def list_windows():
    """Visible top-level windows that match the configured process and/or title."""
    pids = _image_pids(CFG["exe"]) if CFG["exe"] else None
    res = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        if pids is not None and _pid_of(h) not in pids:
            return True
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if CFG["title"] and CFG["title"] not in buf.value:
            return True
        rc = _rect(h)
        if rc[2] - rc[0] > 50 and rc[3] - rc[1] > 50:  # skip tiny helper windows
            res.append({"hwnd": h, "pid": _pid_of(h), "title": buf.value, "rect": rc})
        return True

    user32.EnumWindows(CB(cb), 0)
    return res


def main_hwnd():
    ws = list_windows()
    if not ws:
        fail(error="no visible window matches the target", exe=CFG["exe"], title=CFG["title"])
    return max(ws, key=lambda w: (w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1]))["hwnd"]


def target_hwnd(arg=None):
    """The largest matching window when arg is omitted or '-'. An explicit hwnd must also be
    one of the target's own windows, so a call can never reach a window outside the target."""
    if arg in (None, "", "-"):
        return main_hwnd()
    h = int(arg)
    if h not in {w["hwnd"] for w in list_windows()}:
        fail(error="hwnd %d is not a window of the target" % h)
    return h


def _grab(bbox):
    try:
        from PIL import ImageGrab
    except ImportError:
        fail(error="Pillow is not installed (pip install pillow)")
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def shot(path, hwnd=None, maxw=1920):
    h = target_hwnd(hwnd)
    rc = _rect(h)
    img = _grab(tuple(rc))
    full = os.path.splitext(path)[0] + "_full.png"
    img.save(full)
    w, hh = img.size
    scale = 1.0
    small = img
    if w > int(maxw):
        scale = int(maxw) / w
        small = img.resize((int(w * scale), int(hh * scale)))
    small.save(path)
    out(ok=True, path=path, full_res=full, hwnd=h, rect=rc, size=list(small.size), size_full=[w, hh],
        scale_to_full=round(1 / scale, 4),
        note="multiply coordinates read off the downscaled image by scale_to_full")


def shotrect(path, l, t, r, b):
    bbox = (int(l), int(t), int(r), int(b))
    img = _grab(bbox)
    img.save(path)
    out(ok=True, path=path, bbox=list(bbox), size=list(img.size))


def focus(hwnd=None):
    h = target_hwnd(hwnd)
    user32.ShowWindow(h, 9)  # SW_RESTORE: un-minimises, and returns a maximised window to normal size
    user32.SetForegroundWindow(h)
    time.sleep(0.3)
    fg = user32.GetForegroundWindow()
    out(ok=(fg == h), hwnd=h, foreground=fg)  # ok only when the read-back shows the target in front


# ---- SendInput -------------------------------------------------------------------------------
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
if ctypes.sizeof(INPUT) != _EXPECTED_INPUT_SIZE:
    fail(error="INPUT struct is %d bytes, expected %d" % (ctypes.sizeof(INPUT), _EXPECTED_INPUT_SIZE))


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        fail(error="SendInput accepted %d of %d" % (n, len(inputs)), winerr=ctypes.get_last_error())


def _mouse(flags, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(0, 0, data, flags, 0, 0)
    return i


def click(x, y, hwnd=None, right=False, double=False):
    h = target_hwnd(hwnd)
    rc = _rect(h)
    fx, fy = int(float(x)), int(float(y))
    width, height = rc[2] - rc[0], rc[3] - rc[1]
    if not (0 <= fx < width and 0 <= fy < height):
        fail(error="point is outside the target window", point=[fx, fy], window_size=[width, height])
    sx, sy = rc[0] + fx, rc[1] + fy
    user32.SetCursorPos(sx, sy)
    time.sleep(0.05)
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    if (pt.x, pt.y) != (sx, sy):  # read-back BEFORE any button event is sent
        fail(error="cursor did not reach the target; no click sent", screen=[sx, sy], cursor=[pt.x, pt.y])
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    _send([_mouse(down), _mouse(up)] * (2 if double else 1))
    time.sleep(0.3)
    out(ok=True, window_point=[fx, fy], screen=[sx, sy], button="right" if right else "left", double=double)


VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "delete": 0x2E,
      "backspace": 0x08, "f2": 0x71, "f4": 0x73, "pgup": 0x21, "pgdn": 0x22}


def _vk(name):
    n = name.lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        return ord(n.upper())
    fail(error="unknown key %r" % name)


def key(*names):
    if {"alt", "f4"} <= {n.lower() for n in names}:
        fail(error="alt+f4 refused by driver: it closes the window")
    vks = [_vk(n) for n in names]  # every name is checked before anything is sent
    seq = []
    for v in vks:
        i = INPUT(type=1); i.u.ki = KEYBDINPUT(v, 0, 0, 0, 0); seq.append(i)
    for v in reversed(vks):
        i = INPUT(type=1); i.u.ki = KEYBDINPUT(v, 0, 2, 0, 0); seq.append(i)
    _send(seq)
    time.sleep(0.3)
    out(ok=True, keys=list(names))


def type_text(text):
    seq = []
    for ch in text:
        if ord(ch) > 0xFFFF:
            fail(error="characters above U+FFFF are not supported", char=repr(ch))
        for fl in (0x0004, 0x0004 | 0x0002):  # KEYEVENTF_UNICODE, then + KEYUP
            i = INPUT(type=1); i.u.ki = KEYBDINPUT(0, ord(ch), fl, 0, 0); seq.append(i)
    _send(seq)
    time.sleep(0.3)
    out(ok=True, typed=len(text))


# ---- entry point ------------------------------------------------------------------------------
def _leading_flags(argv):
    exe = os.environ.get("GUIDRV_EXE") or None
    title = os.environ.get("GUIDRV_TITLE") or None
    while len(argv) >= 2 and argv[0] in ("--exe", "--title"):
        if argv[0] == "--exe":
            exe = argv[1]
        else:
            title = argv[1]
        argv = argv[2:]
    return exe, title, argv


def main(argv):
    if user32 is None:
        fail(error="Windows only (uses user32 through ctypes)")
    exe, title, rest = _leading_flags(argv)
    if not rest:
        print(__doc__)
        return 0
    if not exe and not title:
        fail(error="no target: set GUIDRV_EXE and/or GUIDRV_TITLE, or pass --exe / --title")
    CFG["exe"], CFG["title"] = exe, title
    cmd, a = rest[0], rest[1:]
    if cmd == "windows":
        out(ok=True, windows=list_windows())
    elif cmd == "shot":
        shot(*a)
    elif cmd == "shotrect":
        shotrect(*a)
    elif cmd == "focus":
        focus(*a)
    elif cmd == "click":
        click(a[0], a[1], a[2] if len(a) > 2 else None, right=len(a) > 3 and a[3] == "right")
    elif cmd == "dclick":
        click(a[0], a[1], a[2] if len(a) > 2 else None, double=True)
    elif cmd == "type":
        type_text(a[0])
    elif cmd == "key":
        key(*a)
    else:
        fail(error="unknown command " + cmd)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # any other error still produces a JSON failure object
        fail(error="%s: %s" % (type(e).__name__, e))
