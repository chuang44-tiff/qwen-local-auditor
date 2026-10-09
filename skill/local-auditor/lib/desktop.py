"""qla-desktop: screenshots, clicks and keys for ONE application's windows.

Windows, macOS and Linux (X11 only: a Wayland session is refused). Every command
prints one JSON object with an `ok` field; ok is false, and the exit status 1, on any
failure, and a refused or failed action sends nothing. Screenshots need Pillow.

qwen-agent --desktop APP runs it through a fenced Bash rule as `qla-desktop`, with the
target and the output folder locked; it also works by hand:

  desktop.py --app NAME [--title TEXT] [--dir DIR] COMMAND [ARGS]

  --app NAME     process name of the target (notepad.exe, gedit, TextEdit); required
  --title TEXT   only windows whose title contains TEXT
  --dir DIR      where screenshots and the saved window size go (default: .)
  --lock         no further option may follow (set by qwen-agent)

Commands. X Y W H are window pixels: 0,0 is the window's top-left corner. WIN is an
id from `windows`; without it, the target window in front is used (an open dialog,
say), else the largest.
  check                       is this desktop drivable, and does the target have a window?
  windows                     the target's windows: id, title, rect [x, y, w, h] on screen
  shot NAME [WIN] [grid]      screenshot of the window as NAME.png (long side <= 1280)
  crop NAME X Y W H [ZOOM] [grid]
                              a part of the window, enlarged ZOOM times (default 2, max 4)
  focus [WIN]                 bring the window to the front
  click X Y [WIN]             left click; also dclick (double) and rclick (right)
  hover X Y [WIN]             move the mouse there (tooltips, menus)
  drag X1 Y1 X2 Y2 [WIN]      press at X1,Y1, move, release at X2,Y2
  scroll X Y N [WIN]          N wheel steps, positive scrolls down
  type TEXT                   type text into the focused control
  key NAME...                 a chord: key ctrl s | key ctrl+shift+s | key enter
  resize W H [X Y] [WIN]      set the window size (and position); the first resize of a
                              window saves its size, which `restore` puts back
  restore [WIN]               back to the size saved by the first resize
  wait SECONDS                pause (at most 30), e.g. while a dialog opens
`grid` draws labelled window coordinates over the image. Refused everywhere: closing the
application (alt+f4, cmd+q, cmd+w, ctrl+q), printing (ctrl+p, cmd+p) and a permanent
delete (shift+delete, cmd+backspace). ctrl+w is not refused: closing the application's
own tabs is legitimate work.
"""
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import sys
import time

MAX_SIDE = 1280      # a screenshot's long side: bigger images cost tokens and add no detail
MAX_ZOOM = 4
MAX_WAIT = 30
MIN_WINDOW = 50      # smaller windows are helpers (tooltips, shadows), never a target


class Fail(Exception):
    """A refused or failed command: printed as ok=false, exit status 1."""

    def __init__(self, error, **extra):
        Exception.__init__(self, error)
        self.extra = dict(extra, error=error)


def _num(v, what):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        raise Fail("%s must be a number, got %r" % (what, v))


def same_app(want, have):
    """Process names match case-insensitively, with or without .exe / .app."""
    def norm(s):
        s = os.path.basename(s or "").lower()
        for ext in (".exe", ".app"):
            if s.endswith(ext):
                s = s[:-len(ext)]
        return s
    return bool(want) and norm(want) == norm(have)


# --------------------------------------------------------------------- key chords

ALIASES = {"control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt", "return": "enter",
           "escape": "esc", "del": "delete", "pageup": "pgup", "pagedown": "pgdn",
           "command": "cmd", "meta": "cmd", "spacebar": "space", "bksp": "backspace"}
NAMED = {"ctrl", "shift", "alt", "enter", "esc", "tab", "space", "backspace", "delete",
         "insert", "home", "end", "pgup", "pgdn", "up", "down", "left", "right"} \
    | {"f%d" % i for i in range(1, 13)}
REFUSED = {
    "win32": [({"alt", "f4"}, "alt+f4 closes the window"), ({"ctrl", "p"}, "ctrl+p prints"),
              ({"shift", "delete"}, "shift+delete deletes permanently in file managers")],
    "linux": [({"alt", "f4"}, "alt+f4 closes the window"), ({"ctrl", "p"}, "ctrl+p prints"),
              ({"ctrl", "q"}, "ctrl+q quits GTK/Qt applications"),
              ({"shift", "delete"}, "shift+delete deletes permanently in file managers")],
    "darwin": [({"cmd", "q"}, "cmd+q quits the application"), ({"cmd", "w"}, "cmd+w closes the window"),
               ({"cmd", "p"}, "cmd+p prints"), ({"cmd", "space"}, "cmd+space opens Spotlight"),
               ({"cmd", "tab"}, "cmd+tab switches application"),
               ({"cmd", "backspace"}, "cmd+backspace moves the selection to the Trash in Finder")],
}   # ctrl+w is deliberately absent: closing an application's own tabs is legitimate work


def parse_chord(names, platform):
    """['ctrl', 's'] or ['ctrl+s'] -> ['ctrl', 's'], every name checked before anything is sent."""
    keys = []
    for n in names:
        for part in n.split("+") if n != "+" else ["+"]:
            p = ALIASES.get(part.lower(), part.lower())
            if not p:
                continue
            if p == "cmd" and platform != "darwin":
                raise Fail("cmd exists on macOS only; use ctrl")
            if p not in NAMED and p != "cmd" and len(p) != 1:
                raise Fail("unknown key %r" % part)
            keys.append(p)
    if not keys:
        raise Fail("key needs a key name, e.g. key ctrl s")
    for combo, why in REFUSED.get(platform, []):
        if combo <= set(keys):
            raise Fail("refused: %s" % why)
    return keys


# ----------------------------------------------------------------- the commands

class Desktop:
    """The commands, on top of one backend that speaks screen coordinates."""

    def __init__(self, backend, app, title=None, outdir=".", platform=None):
        self.be, self.app, self.title, self.dir = backend, app, title, outdir
        platform = platform or sys.platform
        self.platform = "linux" if platform.startswith("linux") else platform
        self._wins = None

    # -- windows (one listing per command, until something can have changed them:
    # listing costs a full round trip, most of all on macOS, and inside one command
    # the windows only change when this command itself focuses, resizes or clicks)
    def windows(self):
        if self._wins is None:
            self._wins = [w for w in self.be.windows(self.app)
                          if w["rect"][2] >= MIN_WINDOW and w["rect"][3] >= MIN_WINDOW
                          and (not self.title or self.title in w["title"])]
        return self._wins

    def _fresh(self):
        self._wins = None

    def pids(self):
        """The processes whose windows count as the target's (a dialog has the same pid)."""
        return {w["pid"] for w in self.windows()}

    def window(self, wid=None):
        ws = self.windows()
        if not ws:
            raise Fail("no visible window of %r%s" % (self.app, " titled *%s*" % self.title if self.title else ""))
        if wid not in (None, "", "-"):
            for w in ws:
                if str(w["id"]) == str(wid):
                    return w
            raise Fail("window %s is not one of %r's windows" % (wid, self.app),
                       windows=[w["id"] for w in ws])
        front = self.be.foreground_pid()
        fronts = [w for w in ws if w.get("active")] or [w for w in ws if w["pid"] == front and front]
        pool = fronts or ws
        return max(pool, key=lambda w: w["rect"][2] * w["rect"][3])

    # -- input safety: the target must be in front before anything is sent
    def ready(self, w):
        pids = self.pids()
        if self.be.foreground_pid() not in pids:
            self.be.focus(w)
            self._fresh()                                # focusing can map or restack windows
            time.sleep(0.3)
            if self.be.foreground_pid() not in self.pids():
                raise Fail("another application is in front and %r could not be brought forward; "
                           "the click or keys were not sent" % self.app)

    def point(self, w, x, y):
        """Window pixels to the screen. The point must lie in one of the application's
        windows: a menu or a ribbon dropdown is a window of its own and may hang outside
        the one the coordinates are counted from."""
        x, y = _num(x, "x"), _num(y, "y")
        sx, sy = w["rect"][0] + x, w["rect"][1] + y
        for v in self.windows():
            vx, vy, vw, vh = v["rect"]
            if vx <= sx < vx + vw and vy <= sy < vy + vh:
                return sx, sy
        _, _, ww, wh = w["rect"]
        raise Fail("point %d,%d is outside the window (size %dx%d) and every other window of %r; "
                   "nothing was sent" % (x, y, ww, wh, self.app))

    def moveto(self, sx, sy):
        self.be.move(sx, sy)
        time.sleep(0.05)
        got = self.be.cursor()
        if got is not None and (abs(got[0] - sx) > 1 or abs(got[1] - sy) > 1):
            raise Fail("the cursor did not reach %d,%d (it is at %d,%d); nothing was clicked" % (sx, sy, got[0], got[1]))
        owner = self.be.owner_at(sx, sy)
        if owner is not None and owner not in self.pids():
            raise Fail("another application's window covers %d,%d; nothing was clicked" % (sx, sy))

    # -- images
    def _save(self, img, name):
        base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(name)))
        base = re.sub(r"\.png$", "", base, flags=re.I).strip("._") or "shot"
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, base + ".png")
        img.save(path)
        return os.path.abspath(path)

    def shot(self, name, *rest):
        grid = "grid" in rest
        rest = [r for r in rest if r != "grid"]
        w = self.window(rest[0] if rest else None)
        x, y, ww, wh = w["rect"]
        img = self.be.grab(x, y, ww, wh)
        if img.size != (ww, wh):            # a Retina grab is in pixels; coordinates are points
            img = img.resize((ww, wh), _lanczos())
        scale = 1.0
        if max(ww, wh) > MAX_SIDE:
            scale = max(ww, wh) / float(MAX_SIDE)
            img = img.resize((int(ww / scale), int(wh / scale)), _lanczos())
        if grid:
            img = draw_grid(img, 0, 0, 1.0 / scale)
        res = dict(path=self._save(img, name), window=w["id"], title=w["title"], rect=w["rect"],
                   size=list(img.size), scale=round(scale, 4))
        res["note"] = ("pixels in this image are window pixels" if scale == 1.0 else
                       "multiply coordinates read off this image by scale; or resize the window "
                       "to at most %d px wide and shoot again" % MAX_SIDE)
        return res

    def crop(self, name, x, y, cw, ch, *rest):
        grid = "grid" in rest
        rest = [r for r in rest if r != "grid"]
        zoom = float(rest[0]) if rest else 2.0
        if not 1 <= zoom <= MAX_ZOOM:
            raise Fail("zoom must be between 1 and %d" % MAX_ZOOM)
        w = self.window(rest[1] if len(rest) > 1 else None)
        x, y, cw, ch = _num(x, "x"), _num(y, "y"), _num(cw, "w"), _num(ch, "h")
        _, _, ww, wh = w["rect"]
        x, y = max(0, x), max(0, y)
        cw, ch = min(cw, ww - x), min(ch, wh - y)
        if cw < 1 or ch < 1:
            raise Fail("the area is outside the window (size %dx%d)" % (ww, wh))
        zoom = min(zoom, MAX_SIDE / float(max(cw, ch)))
        zoom = max(zoom, 1.0)
        img = self.be.grab(w["rect"][0] + x, w["rect"][1] + y, cw, ch)
        img = img.resize((int(cw * zoom), int(ch * zoom)), _lanczos())
        if grid:
            img = draw_grid(img, x, y, zoom)
        return dict(path=self._save(img, name), window=w["id"], origin=[x, y], zoom=round(zoom, 4),
                    size=list(img.size),
                    note="a pixel u,v in this image is window point %d + u/%g, %d + v/%g" % (x, zoom, y, zoom))

    # -- input
    def focus(self, wid=None):
        w = self.window(wid)
        self.be.focus(w)
        self._fresh()                                    # focusing can map or restack windows
        time.sleep(0.3)
        if self.be.foreground_pid() not in self.pids():
            raise Fail("could not bring %r to the front" % self.app)
        return dict(window=w["id"], title=w["title"])

    def click(self, x, y, wid=None, button="left", count=1):
        w = self.window(wid)
        sx, sy = self.point(w, x, y)
        self.ready(w)
        self.moveto(sx, sy)
        for _ in range(count):
            self.be.button(button, True)
            self.be.button(button, False)
        self._fresh()                                    # a click can open a window (a menu, a dropdown)
        time.sleep(0.3)
        return dict(window=w["id"], point=[sx - w["rect"][0], sy - w["rect"][1]], button=button, clicks=count)

    def hover(self, x, y, wid=None):
        w = self.window(wid)
        sx, sy = self.point(w, x, y)
        self.ready(w)
        self.be.move(sx, sy)
        time.sleep(0.5)
        return dict(window=w["id"], point=[sx - w["rect"][0], sy - w["rect"][1]])

    def drag(self, x1, y1, x2, y2, wid=None):
        w = self.window(wid)
        a, b = self.point(w, x1, y1), self.point(w, x2, y2)
        self.ready(w)
        self.moveto(*a)
        self.be.button("left", True)
        try:
            for i in range(1, 11):
                self.be.move(a[0] + (b[0] - a[0]) * i // 10, a[1] + (b[1] - a[1]) * i // 10)
                time.sleep(0.02)
        finally:
            self.be.button("left", False)
        self._fresh()
        time.sleep(0.3)
        return dict(window=w["id"], start=[_num(x1, "x1"), _num(y1, "y1")], end=[_num(x2, "x2"), _num(y2, "y2")])

    def scroll(self, x, y, n, wid=None):
        w = self.window(wid)
        sx, sy = self.point(w, x, y)
        n = _num(n, "N")
        if not n or abs(n) > 50:
            raise Fail("N must be 1 to 50 steps (negative scrolls up)")
        self.ready(w)
        self.moveto(sx, sy)
        self.be.scroll(n)
        self._fresh()
        time.sleep(0.3)
        return dict(window=w["id"], steps=n)

    def type(self, *words):
        if not words:
            raise Fail("type needs the text")
        text = " ".join(words)
        if len(text) > 2000:
            raise Fail("at most 2000 characters per call")
        self.ready(self.window())
        self.be.type(text)
        self._fresh()
        time.sleep(0.3)
        return dict(typed=len(text))

    def key(self, *names):
        keys = parse_chord(names, self.platform)
        self.ready(self.window())
        self.be.keys(keys)
        self._fresh()
        time.sleep(0.3)
        return dict(keys=keys)

    # -- window size
    def _state(self):
        p = os.path.join(self.dir, "desktop-state.json")
        try:
            with open(p, encoding="utf-8") as fh:
                return p, json.load(fh)
        except (OSError, ValueError):
            return p, {}

    def resize(self, nw, nh, *rest):
        nw, nh = _num(nw, "W"), _num(nh, "H")
        if nw < 200 or nh < 150:
            raise Fail("at least 200x150")
        wid = rest[2] if len(rest) >= 3 else (rest[0] if len(rest) == 1 else None)
        w = self.window(wid)
        x, y = (_num(rest[0], "X"), _num(rest[1], "Y")) if len(rest) >= 2 else (w["rect"][0], w["rect"][1])
        p, st = self._state()
        if str(w["id"]) not in st:
            st[str(w["id"])] = w["rect"]
            os.makedirs(self.dir, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
        return self._setrect(w, [x, y, nw, nh])

    def restore(self, wid=None):
        w = self.window(wid)
        _, st = self._state()
        old = st.get(str(w["id"]))
        if not old:
            raise Fail("window %s was never resized: nothing to restore" % w["id"])
        return self._setrect(w, old)

    def _setrect(self, w, rect):
        self.be.set_rect(w, *rect)
        self._fresh()                                    # the read-back must see the new size
        time.sleep(0.4)
        now = self.window(w["id"])["rect"]
        res = dict(window=w["id"], before=w["rect"], after=now)
        if abs(now[2] - rect[2]) > 8 or abs(now[3] - rect[3]) > 8:
            res["note"] = "the application kept another size (it may have a minimum size or be maximized)"
        return res

    # -- the rest
    def check(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise Fail("Pillow is not installed (pip install pillow): screenshots need it")
        self.be.check()
        ws = self.windows()
        if not ws:
            raise Fail("no visible window of %r%s; start the application first" % (
                self.app, " titled *%s*" % self.title if self.title else ""))
        return dict(platform=self.platform, windows=len(ws))

    def wait(self, s):
        s = float(s)
        if not 0 <= s <= MAX_WAIT:
            raise Fail("wait 0 to %d seconds" % MAX_WAIT)
        time.sleep(s)
        return dict(waited=s)

    def run(self, cmd, args):
        simple = {"check": self.check, "shot": self.shot, "crop": self.crop, "focus": self.focus,
                  "hover": self.hover, "drag": self.drag, "scroll": self.scroll, "type": self.type,
                  "key": self.key, "resize": self.resize, "restore": self.restore, "wait": self.wait}
        if cmd == "windows":
            return dict(windows=self.windows())
        if cmd in ("click", "dclick", "rclick"):
            if len(args) < 2:
                raise Fail("%s needs X Y" % cmd)
            return self.click(args[0], args[1], args[2] if len(args) > 2 else None,
                              "right" if cmd == "rclick" else "left", 2 if cmd == "dclick" else 1)
        if cmd not in simple:
            raise Fail("unknown command %r (see --help)" % cmd)
        try:
            return simple[cmd](*args)
        except TypeError as e:
            raise Fail("%s: wrong arguments (%s); see --help" % (cmd, e))


def _lanczos():
    from PIL import Image
    return getattr(Image, "Resampling", Image).LANCZOS


def grid_step(span):
    """A round step giving about ten lines over `span` window pixels."""
    for s in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if span / s <= 12:
            return s
    return 1000


def draw_grid(img, ox, oy, zoom):
    """Lines and labels in WINDOW coordinates over an image showing window point ox,oy at
    its top-left, `zoom` image pixels per window pixel."""
    from PIL import Image, ImageDraw
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step = grid_step(max(base.size) / zoom)
    w, h = base.size
    gx = (ox // step + 1) * step if ox % step else ox
    while (gx - ox) * zoom < w:
        px = int((gx - ox) * zoom)
        d.line([(px, 0), (px, h)], fill=(255, 0, 255, 110))
        d.text((px + 2, 1), str(gx), fill=(255, 0, 255, 255))
        gx += step
    gy = (oy // step + 1) * step if oy % step else oy
    while (gy - oy) * zoom < h:
        py = int((gy - oy) * zoom)
        d.line([(0, py), (w, py)], fill=(255, 0, 255, 110))
        d.text((1, py + 1), str(gy), fill=(255, 0, 255, 255))
        gy += step
    return Image.alpha_composite(base, layer).convert("RGB")


def _grab_pil(x, y, w, h):
    try:
        from PIL import ImageGrab
    except ImportError:
        raise Fail("Pillow is not installed (pip install pillow)")
    kw = {"all_screens": True} if sys.platform == "win32" else {}
    return ImageGrab.grab(bbox=(x, y, x + w, y + h), **kw).convert("RGB")


# ------------------------------------------------------------------ Windows

class WinBackend:
    def __init__(self):
        import ctypes.wintypes as wt
        self.wt = wt
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwm = None
        try:  # PER_MONITOR_AWARE_V2: rectangles and the cursor in physical pixels
            self.u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        self.u.GetForegroundWindow.restype = wt.HWND
        self.u.WindowFromPoint.restype = wt.HWND
        self.u.WindowFromPoint.argtypes = [wt.POINT]
        self.u.GetAncestor.restype = wt.HWND
        self.k.OpenProcess.restype = wt.HANDLE
        # HWND/HANDLE argtypes: without them a 64-bit handle could be truncated into
        # the wrong window. VkKeyScanW returns a SHORT: without the restype its -1
        # ("no such key") would come back as a big positive number and pass the check.
        U = self.u
        U.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        U.IsIconic.argtypes = [wt.HWND]
        U.IsZoomed.argtypes = [wt.HWND]
        U.IsWindowVisible.argtypes = [wt.HWND]
        U.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        U.SetForegroundWindow.argtypes = [wt.HWND]
        U.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
        U.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        U.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
        U.GetWindowTextW.argtypes = [wt.HWND, ctypes.POINTER(ctypes.c_wchar), ctypes.c_int]
        U.GetWindowTextLengthW.argtypes = [wt.HWND]
        U.VkKeyScanW.restype = ctypes.c_short
        self.k.CloseHandle.argtypes = [wt.HANDLE]
        self.k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                      ctypes.POINTER(ctypes.c_wchar),
                                                      ctypes.POINTER(wt.DWORD)]

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

        want = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != want:
            raise Fail("INPUT struct is %d bytes, expected %d" % (ctypes.sizeof(INPUT), want))
        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT

    def check(self):
        pass

    def _pid(self, h):
        pid = self.wt.DWORD()
        self.u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value

    def _exe(self, pid):
        h = self.k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = self.wt.DWORD(1024)
            ok = self.k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            return os.path.basename(buf.value) if ok else ""
        finally:
            self.k.CloseHandle(h)

    def _rect(self, h):
        """The visible frame (DWM bounds, without the invisible resize border)."""
        r = self.wt.RECT()
        if not (self.dwm and self.dwm.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0):
            self.u.GetWindowRect(h, ctypes.byref(r))
        return [r.left, r.top, r.right - r.left, r.bottom - r.top]

    def windows(self, app):
        res, names = [], {}
        fg = self.u.GetForegroundWindow()
        CB = ctypes.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)

        def cb(h, _):
            if not self.u.IsWindowVisible(h) or self.u.IsIconic(h):
                return True
            cloaked = ctypes.c_int(0)
            if self.dwm:
                self.dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            pid = self._pid(h)
            if pid not in names:
                names[pid] = self._exe(pid)
            if not same_app(app, names[pid]):
                return True
            n = self.u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            self.u.GetWindowTextW(h, buf, n + 1)
            res.append({"id": h, "pid": pid, "title": buf.value, "rect": self._rect(h), "active": h == fg})
            return True

        self.u.EnumWindows(CB(cb), 0)
        return res

    def foreground_pid(self):
        h = self.u.GetForegroundWindow()
        return self._pid(h) if h else None

    def owner_at(self, x, y):
        h = self.u.WindowFromPoint(self.wt.POINT(x, y))
        return self._pid(self.u.GetAncestor(h, 2)) if h else None    # GA_ROOT

    def focus(self, w):
        h = w["id"]
        if self.u.IsIconic(h):
            self.u.ShowWindow(h, 9)                     # SW_RESTORE
        if not self.u.SetForegroundWindow(h) or self.u.GetForegroundWindow() != h:
            # Windows lets a process take the foreground only right after the foreground
            # received input: a mouse move of zero distance counts as that input without
            # touching anything (a tapped key would go to whatever window holds it).
            self._send([self._mouse(0x0001)])           # MOUSEEVENTF_MOVE, dx=dy=0
            self.u.SetForegroundWindow(h)
            time.sleep(0.1)

    def _send(self, inputs):
        arr = (self.INPUT * len(inputs))(*inputs)
        n = self.u.SendInput(len(inputs), arr, ctypes.sizeof(self.INPUT))
        if n != len(inputs):
            raise Fail("SendInput accepted %d of %d events" % (n, len(inputs)), winerr=ctypes.get_last_error())

    def _mouse(self, flags, data=0):
        i = self.INPUT(type=0)
        i.u.mi = self.MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    def _key(self, vk, up, scan=0, unicode=False):
        ext = vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E)
        flags = (0x0004 if unicode else 0) | (0x0002 if up else 0) | (0x0001 if ext else 0)
        i = self.INPUT(type=1)
        i.u.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def move(self, x, y):
        self.u.SetCursorPos(x, y)

    def cursor(self):
        pt = self.wt.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def button(self, which, down):
        flag = {("left", True): 0x2, ("left", False): 0x4, ("right", True): 0x8, ("right", False): 0x10}
        self._send([self._mouse(flag[(which, down)])])

    def scroll(self, n):
        self._send([self._mouse(0x0800, -120 * n)])     # MOUSEEVENTF_WHEEL; positive n = down

    VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
          "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
          "pgup": 0x21, "pgdn": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}

    def _vk(self, k):
        """The virtual key for a name, and whether it sits under shift on this keyboard."""
        if k in self.VK:
            return self.VK[k], False
        if re.match(r"^f([1-9]|1[0-2])$", k):
            return 0x6F + int(k[1:]), False
        if k.isalnum() and k.isascii():
            return ord(k.upper()), False
        r = self.u.VkKeyScanW(ord(k))                   # punctuation: this keyboard's key for it
        if r == -1 or r & 0xFF == 0xFF:
            raise Fail("no key for %r on this keyboard" % k)
        return r & 0xFF, bool((r >> 8) & 1)             # bit 0 of the high byte: shift is needed

    def keys(self, keys):
        pairs = [self._vk(k) for k in keys]
        vks = [v for v, _ in pairs]
        if any(s for _, s in pairs) and "shift" not in keys:
            vks.insert(0, 0x10)                         # VK_SHIFT: punctuation lives under it
        self._send([self._key(v, False) for v in vks] + [self._key(v, True) for v in reversed(vks)])

    def type(self, text):
        seq = []
        for ch in text.replace("\r\n", "\n"):
            if ch in "\n\t":
                v = 0x0D if ch == "\n" else 0x09
                seq += [self._key(v, False), self._key(v, True)]
                continue
            b = ch.encode("utf-16-le")
            for i in range(0, len(b), 2):               # a surrogate pair is two units
                unit = b[i] | b[i + 1] << 8
                seq += [self._key(0, False, unit, True), self._key(0, True, unit, True)]
        self._send(seq)

    def set_rect(self, w, x, y, cw, ch):
        h = w["id"]
        if self.u.IsZoomed(h):
            self.u.ShowWindow(h, 9)                     # un-maximize first, or the size is ignored
            time.sleep(0.2)
        # SetWindowPos counts the invisible resize border that the DWM rectangle leaves out
        outer, inner = self.wt.RECT(), self._rect(h)
        self.u.GetWindowRect(h, ctypes.byref(outer))
        dl, dt = inner[0] - outer.left, inner[1] - outer.top
        dr = (outer.right - outer.left) - inner[2] - dl
        db = (outer.bottom - outer.top) - inner[3] - dt
        self.u.SetWindowPos(h, 0, x - dl, y - dt, cw + dl + dr, ch + dt + db, 0x0014)  # NOZORDER|NOACTIVATE

    grab = staticmethod(_grab_pil)


# ------------------------------------------------------------------- Linux X11

class XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
                ("root", ctypes.c_ulong), ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int)]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5), ("pad", ctypes.c_long * 12)]


class XResClientIdSpec(ctypes.Structure):
    _fields_ = [("client", ctypes.c_ulong), ("mask", ctypes.c_uint)]


class XResClientIdValue(ctypes.Structure):
    _fields_ = [("spec", XResClientIdSpec), ("length", ctypes.c_long), ("value", ctypes.c_void_p)]


class X11Backend:
    def __init__(self):
        if not os.environ.get("DISPLAY"):
            raise Fail("no X display (DISPLAY is not set)")
        x11, xtst = ctypes.util.find_library("X11"), ctypes.util.find_library("Xtst")
        if not x11 or not xtst:
            raise Fail("libX11 and libXtst are needed (apt install libx11-6 libxtst6)")
        X, T = ctypes.CDLL(x11), ctypes.CDLL(xtst)
        U, L, I, P, V = ctypes.c_ulong, ctypes.c_long, ctypes.c_int, ctypes.POINTER, ctypes.c_void_p
        sig = {
            "XOpenDisplay": (V, [ctypes.c_char_p]), "XDefaultRootWindow": (U, [V]),
            "XInternAtom": (U, [V, ctypes.c_char_p, I]),
            "XGetWindowProperty": (I, [V, U, U, L, L, I, U, P(U), P(I), P(U), P(U), P(V)]),
            "XQueryTree": (I, [V, U, P(U), P(U), P(V), P(ctypes.c_uint)]),
            "XGetWindowAttributes": (I, [V, U, P(XWindowAttributes)]),
            "XTranslateCoordinates": (I, [V, U, U, I, I, P(I), P(I), P(U)]),
            "XGetImage": (P(XImage), [V, U, I, I, ctypes.c_uint, ctypes.c_uint, U, I]),
            "XQueryPointer": (I, [V, U, P(U), P(U), P(I), P(I), P(I), P(I), P(ctypes.c_uint)]),
            "XGetInputFocus": (I, [V, P(U), P(I)]), "XSetInputFocus": (I, [V, U, I, U]),
            "XRaiseWindow": (I, [V, U]), "XMoveResizeWindow": (I, [V, U, I, I, ctypes.c_uint, ctypes.c_uint]),
            "XSendEvent": (I, [V, U, I, L, V]), "XFlush": (I, [V]), "XSync": (I, [V, I]),
            "XStringToKeysym": (U, [ctypes.c_char_p]), "XKeysymToKeycode": (ctypes.c_ubyte, [V, U]),
            "XKeycodeToKeysym": (U, [V, ctypes.c_ubyte, I]), "XFree": (I, [V]),
            "XDisplayWidth": (I, [V, I]), "XDisplayHeight": (I, [V, I]),
            "XFetchName": (I, [V, U, P(ctypes.c_char_p)]),
        }
        for name, (res, args) in sig.items():
            f = getattr(X, name)
            f.restype, f.argtypes = res, args
        for name, args in {"XTestFakeMotionEvent": [V, I, I, I, U], "XTestFakeButtonEvent": [V, ctypes.c_uint, I, U],
                           "XTestFakeKeyEvent": [V, ctypes.c_uint, I, U]}.items():
            getattr(T, name).argtypes = args
        self.errors = []
        HANDLER = ctypes.CFUNCTYPE(I, V, V)
        self._handler = HANDLER(lambda d, e: self.errors.append(1) or 0)   # never exit on an X error
        X.XSetErrorHandler(self._handler)
        self.X, self.T = X, T
        xres = ctypes.util.find_library("XRes")       # the owner's pid when a window has no _NET_WM_PID
        self.R = ctypes.CDLL(xres) if xres else None
        if self.R:
            self.R.XResQueryClientIds.restype = I
            self.R.XResQueryClientIds.argtypes = [V, L, P(XResClientIdSpec), P(L), P(P(XResClientIdValue))]
            self.R.XResClientIdsDestroy.argtypes = [L, P(XResClientIdValue)]
        self.d = X.XOpenDisplay(None)
        if not self.d:
            raise Fail("cannot open the X display %r" % os.environ.get("DISPLAY"))
        self.root = X.XDefaultRootWindow(self.d)
        # XWayland, the X server of a Wayland session, shows a program only its own windows.
        # It is told by the server itself, not by XDG_SESSION_TYPE: an application under
        # Xvfb in a Wayland user's shell is still drivable.
        a, b, c = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        X.XQueryExtension.argtypes = [V, ctypes.c_char_p, P(I), P(I), P(I)]
        if X.XQueryExtension(self.d, b"XWAYLAND", ctypes.byref(a), ctypes.byref(b), ctypes.byref(c)):
            raise Fail("DISPLAY %s is XWayland, the X server of a Wayland session, which lets no "
                       "program read or click another program's windows: log in to an X11 session, "
                       "or run the application under Xvfb (Xvfb :99 & DISPLAY=:99 APP)"
                       % os.environ.get("DISPLAY"))

    def check(self):
        pass

    def _atom(self, name):
        return self.X.XInternAtom(self.d, name.encode(), 0)

    def _prop(self, w, name, kind=0):
        """A window property: a list of ints (format 32) or bytes (format 8), or None."""
        t, f, n, after, data = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if self.X.XGetWindowProperty(self.d, w, self._atom(name), 0, 1 << 16, 0, kind, ctypes.byref(t),
                                     ctypes.byref(f), ctypes.byref(n), ctypes.byref(after),
                                     ctypes.byref(data)) != 0 or not data.value:
            return None
        try:
            if f.value == 32:
                return list((ctypes.c_ulong * n.value).from_address(data.value))
            if f.value == 8:
                return ctypes.string_at(data.value, n.value)
            return None
        finally:
            self.X.XFree(data)

    def _pid(self, w):
        v = self._prop(w, "_NET_WM_PID")
        if v:
            return int(v[0])
        if not self.R:
            return None
        spec, n, vals = XResClientIdSpec(w, 2), ctypes.c_long(), ctypes.POINTER(XResClientIdValue)()
        if self.R.XResQueryClientIds(self.d, 1, ctypes.byref(spec), ctypes.byref(n), ctypes.byref(vals)) != 0:
            return None                                 # XRES_CLIENT_ID_PID_MASK above
        try:
            for i in range(n.value):
                if vals[i].spec.mask == 2 and vals[i].length >= 4:
                    return ctypes.c_uint32.from_address(vals[i].value).value
            return None
        finally:
            self.R.XResClientIdsDestroy(n, vals)

    def _title(self, w):
        v = self._prop(w, "_NET_WM_NAME")
        if v:
            return v.decode("utf-8", "replace")
        name = ctypes.c_char_p()
        if self.X.XFetchName(self.d, w, ctypes.byref(name)) and name.value:
            return name.value.decode("latin-1")
        return ""

    @staticmethod
    def _names(pid):
        names = []
        for f in ("comm", "cmdline"):
            try:
                with open("/proc/%d/%s" % (pid, f), "rb") as fh:
                    raw = fh.read().split(b"\0")[0].strip()
                names.append(os.path.basename(raw.decode("utf-8", "replace")))
            except OSError:
                pass
        try:
            names.append(os.path.basename(os.readlink("/proc/%d/exe" % pid)))
        except OSError:
            pass
        return names

    def _children(self, w):
        r, p, kids, n = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p(), ctypes.c_uint()
        if not self.X.XQueryTree(self.d, w, ctypes.byref(r), ctypes.byref(p), ctypes.byref(kids), ctypes.byref(n)):
            return [], None
        out = list((ctypes.c_ulong * n.value).from_address(kids.value)) if kids.value and n.value else []
        if kids.value:
            self.X.XFree(kids)
        return out, p.value

    def _rect(self, w):
        a = XWindowAttributes()
        if not self.X.XGetWindowAttributes(self.d, w, ctypes.byref(a)):
            return None, None
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        self.X.XTranslateCoordinates(self.d, w, self.root, 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child))
        return [x.value, y.value, a.width, a.height], a.map_state == 2       # IsViewable

    def _active(self):
        v = self._prop(self.root, "_NET_ACTIVE_WINDOW")
        if v and v[0]:
            return v[0]
        f, rev = ctypes.c_ulong(), ctypes.c_int()
        self.X.XGetInputFocus(self.d, ctypes.byref(f), ctypes.byref(rev))
        return f.value if f.value > 1 else None          # None / PointerRoot

    def windows(self, app):
        clients = self._prop(self.root, "_NET_CLIENT_LIST")
        if clients is None:                             # no window manager (Xvfb): the root's children
            clients = self._children(self.root)[0]
        active, res, cache = self._active(), [], {}
        for w in clients:
            pid = self._pid(w)
            if pid is None:
                continue
            if pid not in cache:
                cache[pid] = self._names(pid)
            if not any(same_app(app, n) for n in cache[pid]):
                continue
            rect, viewable = self._rect(w)
            if rect and viewable:
                res.append({"id": w, "pid": pid, "title": self._title(w), "rect": rect, "active": w == active})
        return res

    def foreground_pid(self):
        w = self._active()
        for _ in range(20):                             # the focus may sit on a child of the window
            if not w or w == self.root:
                return None
            pid = self._pid(w)
            if pid is not None:
                return pid
            w = self._children(w)[1]
        return None

    def owner_at(self, x, y):
        """The pid owning the deepest window under the root point (x,y), or None.

        A self-raising window of another application takes a click there, so the
        driver asks who owns the point before it sends a button. The chain is built
        without moving anything: XTranslateCoordinates' child answer gives the child
        of the source window that holds the point, so translating a window into
        itself descends one level at a time, root -> ... -> deepest."""
        chain, w = [], self.root
        rx, ry = x, y                                   # the point relative to the window w
        for _ in range(20):
            xr, yr, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            if not self.X.XTranslateCoordinates(self.d, w, w, rx, ry, ctypes.byref(xr),
                                               ctypes.byref(yr), ctypes.byref(child)):
                break                                   # the server refuses, or the point leaves w
            c = child.value
            if not c or c in chain:
                break
            chain.append(c)
            xr, yr, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            if not self.X.XTranslateCoordinates(self.d, w, c, rx, ry, ctypes.byref(xr),
                                               ctypes.byref(yr), ctypes.byref(child)):
                break
            w, rx, ry = c, xr.value, yr.value
        for w in reversed(chain):                       # the deepest window with a _NET_WM_PID wins;
            v = self._prop(w, "_NET_WM_PID")            # a window manager's frame has none of its own
            if v:
                return int(v[0])
        return self._pid(chain[-1]) if chain else None  # Tk sets no property: XRes on the deepest one

    def focus(self, w):
        wid = w["id"]
        if self._prop(self.root, "_NET_SUPPORTED") is not None:   # a window manager: ask it
            ev = XClientMessageEvent(type=33, send_event=1, display=self.d, window=wid,
                                     message_type=self._atom("_NET_ACTIVE_WINDOW"), format=32)
            ev.data[0] = 2                              # source: a pager/tool, honoured by most WMs
            self.X.XSendEvent(self.d, self.root, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))
        self.X.XRaiseWindow(self.d, wid)
        self.X.XSetInputFocus(self.d, wid, 2, 0)        # RevertToParent, CurrentTime
        self.X.XSync(self.d, 0)

    def move(self, x, y):
        self.T.XTestFakeMotionEvent(self.d, -1, x, y, 0)
        self.X.XSync(self.d, 0)

    def cursor(self):
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy, m = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        self.X.XQueryPointer(self.d, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                             ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m))
        return rx.value, ry.value

    def button(self, which, down):
        self.T.XTestFakeButtonEvent(self.d, 1 if which == "left" else 3, 1 if down else 0, 0)
        self.X.XSync(self.d, 0)

    def scroll(self, n):
        b = 5 if n > 0 else 4
        for _ in range(abs(n)):
            self.T.XTestFakeButtonEvent(self.d, b, 1, 0)
            self.T.XTestFakeButtonEvent(self.d, b, 0, 0)
        self.X.XSync(self.d, 0)

    KEYSYM = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "enter": "Return", "esc": "Escape",
              "tab": "Tab", "space": "space", "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
              "home": "Home", "end": "End", "pgup": "Prior", "pgdn": "Next", "up": "Up", "down": "Down",
              "left": "Left", "right": "Right"}

    def _code(self, keysym, what):
        kc = self.X.XKeysymToKeycode(self.d, keysym)
        if not kc:
            raise Fail("no key for %r on this keyboard map" % what)
        return kc

    def _sym(self, k):
        if k in self.KEYSYM or re.match(r"^f([1-9]|1[0-2])$", k):
            return self.X.XStringToKeysym(self.KEYSYM.get(k, k.upper()).encode())
        return ord(k) if ord(k) < 0x100 else 0x01000000 + ord(k)

    def _press(self, codes):
        for c in codes:
            self.T.XTestFakeKeyEvent(self.d, c, 1, 0)
        for c in reversed(codes):
            self.T.XTestFakeKeyEvent(self.d, c, 0, 0)
        self.X.XSync(self.d, 0)

    def keys(self, keys):
        self._press([self._code(self._sym(k), k) for k in keys])

    def type(self, text):
        shift = self._code(self.X.XStringToKeysym(b"Shift_L"), "shift")
        plan = []
        for ch in text:
            sym = self._sym({"\n": "enter", "\t": "tab"}.get(ch, ch))
            kc = self._code(sym, ch)
            if self.X.XKeycodeToKeysym(self.d, kc, 0) == sym:
                plan.append([kc])
            elif self.X.XKeycodeToKeysym(self.d, kc, 1) == sym:
                plan.append([shift, kc])
            else:
                raise Fail("no key for %r on this keyboard map" % ch)
        for codes in plan:                              # every character checked before any is sent
            self._press(codes)
            time.sleep(0.005)

    def set_rect(self, w, x, y, cw, ch):
        self.X.XMoveResizeWindow(self.d, w["id"], x, y, cw, ch)
        self.X.XSync(self.d, 0)

    def grab(self, x, y, w, h):
        try:
            from PIL import Image
        except ImportError:
            raise Fail("Pillow is not installed (pip install pillow)")
        sw, sh = self.X.XDisplayWidth(self.d, 0), self.X.XDisplayHeight(self.d, 0)
        cx, cy = max(0, x), max(0, y)
        cw, ch = min(x + w, sw) - cx, min(y + h, sh) - cy
        if cw < 1 or ch < 1:
            raise Fail("the window is off screen")
        img = self.X.XGetImage(self.d, self.root, cx, cy, cw, ch, 0xFFFFFFFF, 2)    # ZPixmap
        if not img:
            raise Fail("XGetImage failed")
        im = img.contents
        if im.bits_per_pixel != 32:
            raise Fail("unsupported %d-bit display" % im.bits_per_pixel)
        raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
        part = Image.frombuffer("RGB", (cw, ch), raw, "raw", "BGRX", im.bytes_per_line, 1)
        if (cw, ch) == (w, h):
            return part.copy()
        out = Image.new("RGB", (w, h))                  # off-screen parts stay black
        out.paste(part, (cx - x, cy - y))
        return out


# ------------------------------------------------------------------ macOS

JXA = r"""
function run(argv) {
  var se = Application('System Events'), cmd = argv[0], app = argv[1];
  function norm(s) { return String(s).toLowerCase().replace(/\.app$/, ''); }
  if (cmd === 'front') {
    var f = se.processes.whose({frontmost: true})();
    return JSON.stringify(f.length ? f[0].unixId() : null);
  }
  var procs = se.processes.whose({name: app.replace(/\.app$/i, '')})();
  if (!procs.length) procs = se.processes().filter(function (p) { return norm(p.name()) === norm(app); });
  var out = [];
  procs.forEach(function (p) {
    p.windows().forEach(function (w, i) {
      var pos = w.position(), size = w.size();
      out.push({id: p.unixId() + ':' + i, pid: p.unixId(), title: String(w.name() || ''),
                rect: [pos[0], pos[1], size[0], size[1]], active: i === 0 && p.frontmost(), proc: p, win: w});
    });
  });
  var hit = out.filter(function (o) { return o.id === argv[2]; })[0];
  if (cmd === 'focus' && hit) { hit.proc.frontmost = true; try { hit.win.actions['AXRaise'].perform(); } catch (e) {} }
  if (cmd === 'rect' && hit) { hit.win.position = [+argv[3], +argv[4]]; hit.win.size = [+argv[5], +argv[6]]; }
  return JSON.stringify(out.map(function (o) { return {id: o.id, pid: o.pid, title: o.title, rect: o.rect, active: o.active}; }));
}
"""


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class MacBackend:
    KC = dict(zip("asdfhgzxcv", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]), b=11, q=12, w=13, e=14, r=15, y=16, t=17,
              o=31, u=32, i=34, p=35, l=37, j=38, k=40, n=45, m=46,
              **{"1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
                 "enter": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53, "delete": 117,
                 "home": 115, "end": 119, "pgup": 116, "pgdn": 121, "left": 123, "right": 124, "down": 125,
                 "up": 126, "-": 27, "=": 24, "[": 33, "]": 30, ";": 41, "'": 39, ",": 43, ".": 47, "/": 44,
                 "\\": 42, "`": 50, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
                 "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111})
    FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000}

    def __init__(self):
        q = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        V, U32 = ctypes.c_void_p, ctypes.c_uint32
        q.CGEventCreateMouseEvent.restype, q.CGEventCreateMouseEvent.argtypes = V, [V, U32, CGPoint, U32]
        q.CGEventCreateKeyboardEvent.restype, q.CGEventCreateKeyboardEvent.argtypes = V, [V, ctypes.c_uint16, ctypes.c_bool]
        q.CGEventPost.argtypes = [U32, V]
        q.CGEventSetFlags.argtypes = [V, ctypes.c_uint64]
        q.CGEventSetIntegerValueField.argtypes = [V, U32, ctypes.c_int64]
        q.CGEventKeyboardSetUnicodeString.argtypes = [V, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
        q.CGEventCreate.restype, q.CGEventCreate.argtypes = V, [V]
        q.CGEventGetLocation.restype, q.CGEventGetLocation.argtypes = CGPoint, [V]
        q.AXIsProcessTrusted.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [V]
        self.q, self.cf, self.pos = q, cf, None

    def check(self):
        try:
            import PIL
            ver = tuple(int(p) for p in str(PIL.__version__).split(".")[:2])
        except (AttributeError, ValueError, ImportError):
            ver = (0, 0)                                # no version to trust is no 9.2
        if ver < (9, 2):                                # older Pillow grabs the wrong region on Retina
            raise Fail("Pillow 9.2 or later is needed on macOS (pip install -U pillow)")
        if not self.q.AXIsProcessTrusted():
            raise Fail("macOS has not allowed this terminal to control the computer: System Settings > "
                       "Privacy & Security > Accessibility, add the terminal app that runs claude")
        pre = getattr(self.q, "CGPreflightScreenCaptureAccess", None)
        if pre is not None:
            pre.restype = ctypes.c_bool
            if not pre():
                raise Fail("macOS has not allowed this terminal to record the screen: System Settings > "
                           "Privacy & Security > Screen Recording, add the terminal app that runs claude")

    def _jxa(self, *args):
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise Fail("osascript: %s" % (r.stderr.strip() or "failed"))
        return json.loads(r.stdout or "null")

    def windows(self, app):
        return self._jxa("list", app)

    def foreground_pid(self):
        return self._jxa("front", "")

    def owner_at(self, x, y):
        # No public macOS API says which window owns a screen point, so a point
        # covered by another application's window cannot be refused here (documented).
        return None

    def focus(self, w):
        self._jxa("focus", self._app_of(w), w["id"])

    def set_rect(self, w, x, y, cw, ch):
        self._jxa("rect", self._app_of(w), w["id"], x, y, cw, ch)

    def _app_of(self, w):
        return self.app

    def _post(self, ev):
        self.q.CGEventPost(0, ev)                       # kCGHIDEventTap
        self.cf.CFRelease(ev)

    def move(self, x, y):
        self.pos = CGPoint(x, y)
        kind = 6 if getattr(self, "_down", False) else 5    # leftMouseDragged while held, else mouseMoved
        self._post(self.q.CGEventCreateMouseEvent(None, kind, self.pos, 0))

    def cursor(self):
        ev = self.q.CGEventCreate(None)
        p = self.q.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return int(round(p.x)), int(round(p.y))

    def button(self, which, down):
        kind = {("left", True): 1, ("left", False): 2, ("right", True): 3, ("right", False): 4}[(which, down)]
        if which == "left":
            self._down = down                           # move() drags while the button is held
        ev = self.q.CGEventCreateMouseEvent(None, kind, self.pos or CGPoint(*self.cursor()), 0 if which == "left" else 1)
        if down:                                        # click count, so a fast second click is a double click
            self.clicks = getattr(self, "clicks", 0) + 1
        self.q.CGEventSetIntegerValueField(ev, 1, getattr(self, "clicks", 1))   # kCGMouseEventClickState
        self._post(ev)

    def scroll(self, n):
        f = getattr(self.q, "CGEventCreateScrollWheelEvent2", None)
        if f is None:
            raise Fail("scrolling needs macOS 10.13 or later")
        f.restype, f.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                  ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
        self._post(f(None, 1, 1, -n, 0, 0))             # line units; positive n = down

    def _kc(self, k):
        if k not in self.KC:
            raise Fail("no key for %r on macOS (US layout)" % k)
        return self.KC[k]

    def keys(self, keys):
        mods = [k for k in keys if k in self.FLAGS]
        rest = [k for k in keys if k not in self.FLAGS] or mods[-1:]
        codes = [self._kc(k) if k not in self.FLAGS else {"shift": 56, "ctrl": 59, "alt": 58, "cmd": 55}[k]
                 for k in rest]
        flags = 0
        for m in mods:
            flags |= self.FLAGS[m]
        for c in codes:
            for down in (True, False):
                ev = self.q.CGEventCreateKeyboardEvent(None, c, down)
                self.q.CGEventSetFlags(ev, flags)
                self._post(ev)

    def type(self, text):
        for ch in text:
            if ch in "\n\t":
                self.keys(["enter" if ch == "\n" else "tab"])
                continue
            units = ch.encode("utf-16-le")
            buf = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
            for down in (True, False):
                ev = self.q.CGEventCreateKeyboardEvent(None, 0, down)
                self.q.CGEventKeyboardSetUnicodeString(ev, len(buf), buf)
                self._post(ev)
            time.sleep(0.005)

    grab = staticmethod(_grab_pil)


def backend_for(platform=sys.platform):
    if platform == "win32":
        return WinBackend()
    if platform == "darwin":
        return MacBackend()
    if platform.startswith("linux") or "bsd" in platform:
        return X11Backend()
    raise Fail("unsupported platform %r" % platform)


# ------------------------------------------------------------------ entry point

def parse(argv):
    """Leading options, then the command. After --lock no option may follow, so a call
    cannot point the driver at another application or folder."""
    opts, locked = {"app": None, "title": None, "dir": "."}, False
    while argv and argv[0].startswith("--"):
        o = argv[0]
        if o in ("-h", "--help"):
            return opts, None, []
        if locked:
            raise Fail("refused: the target is fixed for this run (%s cannot be changed)" % o)
        if o == "--lock":
            locked, argv = True, argv[1:]
            continue
        if o[2:] not in opts or len(argv) < 2:
            raise Fail("unknown option or missing value: %s" % o)
        opts[o[2:]], argv = argv[1], argv[2:]
    if not argv:
        return opts, None, []
    return opts, argv[0], argv[1:]


def main(argv, backend=None):
    try:
        opts, cmd, args = parse(argv)
        if cmd is None or cmd in ("help", "-h"):
            print(__doc__)
            return 0
        if not opts["app"]:
            raise Fail("no target: pass --app NAME (the application's process name)")
        d = Desktop(backend or backend_for(), opts["app"], opts["title"], opts["dir"])
        d.be.app = opts["app"]
        res = d.run(cmd, args)
        print(json.dumps(dict({"ok": True}, **res)))
        return 0
    except Fail as e:
        print(json.dumps(dict({"ok": False}, **e.extra)))
        return 1
    except Exception as e:                              # never a traceback: always one JSON line
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
