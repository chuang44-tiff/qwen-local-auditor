"""lib/desktop.py, the qla-desktop driver: the commands and their refusals, on a fake
backend that records what it was asked to send. The X11 backend is driven for real in
test_desktop_live.py (Xvfb); the Windows and macOS backends are only imported here."""
import importlib.util
import io
import json
import pathlib
import types
import contextlib

import pytest

LIB = pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" / "lib"
spec = importlib.util.spec_from_file_location("desktop", LIB / "desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class FakeBackend:
    """Two windows of pid 10 ('app') and one of pid 99 ('other'); screen coordinates."""

    def __init__(self, front=10, cursor_sticks=True, focusable=True, owner=None):
        self.front, self.cursor_sticks, self.focusable = front, cursor_sticks, focusable
        self.owner = owner
        self.sent, self.pos, self.listed = [], (0, 0), 0
        self.wins = [
            {"id": 1, "pid": 10, "name": "app", "title": "Main", "rect": [100, 50, 800, 600]},
            {"id": 2, "pid": 10, "name": "app", "title": "Open file", "rect": [200, 100, 400, 300]},
            {"id": 3, "pid": 99, "name": "other", "title": "Other", "rect": [0, 0, 500, 500]},
            {"id": 4, "pid": 10, "name": "app", "title": "tooltip", "rect": [0, 0, 30, 20]},
        ]

    def check(self):
        pass

    def windows(self, app):
        self.listed += 1
        return [dict(w, active=False) for w in self.wins if desktop.same_app(app, w["name"])]

    def foreground_pid(self):
        return self.front

    def owner_at(self, x, y):
        return self.owner

    def focus(self, w):
        self.sent.append(("focus", w["id"]))
        if self.focusable:
            self.front = w["pid"]

    def move(self, x, y):
        self.sent.append(("move", x, y))
        if self.cursor_sticks:
            self.pos = (x, y)

    def cursor(self):
        return self.pos

    def button(self, which, down):
        self.sent.append(("button", which, down))

    def scroll(self, n):
        self.sent.append(("scroll", n))

    def keys(self, keys):
        self.sent.append(("keys", tuple(keys)))

    def type(self, text):
        self.sent.append(("type", text))

    def set_rect(self, w, x, y, cw, ch):
        self.sent.append(("rect", w["id"], x, y, cw, ch))
        for v in self.wins:
            if v["id"] == w["id"]:
                v["rect"] = [x, y, cw, ch]

    def grab(self, x, y, w, h):
        from PIL import Image
        return Image.new("RGB", (w, h), (200, 200, 200))


def call(be, *argv, app="app", outdir=None, platform="linux", title=None):
    """main() with the fake backend: (exit status, the one JSON object printed)."""
    opts = ["--app", app] + (["--dir", str(outdir)] if outdir else []) \
        + (["--title", title] if title else [])
    buf = io.StringIO()
    orig = desktop.sys.platform
    desktop.sys.platform = platform
    try:
        with contextlib.redirect_stdout(buf):
            rc = desktop.main(opts + list(argv), backend=be)
    finally:
        desktop.sys.platform = orig
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1, lines
    return rc, json.loads(lines[0])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)


def inputs(be):
    return [s for s in be.sent if s[0] in ("button", "keys", "type", "scroll")]


# ------------------------------------------------------------------ the target

def test_windows_are_the_targets_only_and_no_tiny_helpers():
    rc, out = call(FakeBackend(), "windows")
    assert rc == 0 and out["ok"]
    assert [w["id"] for w in out["windows"]] == [1, 2]


def test_app_names_match_without_exe_or_case():
    assert desktop.same_app("Notepad", "notepad.exe")
    assert desktop.same_app("TextEdit.app", "textedit")
    assert not desktop.same_app("note", "notepad.exe")
    assert not desktop.same_app("", "")


def test_a_locked_target_cannot_be_changed():
    be = FakeBackend()
    for extra in (["--app", "other"], ["--dir", "/tmp/x"], ["--title", "Other"], ["--lock"]):
        rc, out = call(be, "--lock", *extra, "windows")
        assert rc == 1 and not out["ok"] and "fixed for this run" in out["error"]
    rc, out = call(be, "--lock", "windows")
    assert rc == 0


def test_no_target_is_refused():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = desktop.main(["windows"], backend=FakeBackend())
    assert rc == 1 and "no target" in json.loads(buf.getvalue())["error"]


def test_a_window_of_another_application_is_refused():
    rc, out = call(FakeBackend(front=10), "click", "5", "5", "3")
    assert rc == 1 and "not one of" in out["error"]


# ------------------------------------------------------------------ input safety

def test_click_maps_window_pixels_to_the_screen():
    be = FakeBackend()
    rc, out = call(be, "click", "70", "40", "1")
    assert rc == 0 and out["point"] == [70, 40]
    assert ("move", 170, 90) in be.sent
    assert inputs(be) == [("button", "left", True), ("button", "left", False)]


def test_dclick_and_rclick():
    be = FakeBackend()
    call(be, "dclick", "1", "1", "1")
    assert inputs(be).count(("button", "left", True)) == 2
    be = FakeBackend()
    call(be, "rclick", "1", "1", "1")
    assert inputs(be) == [("button", "right", True), ("button", "right", False)]


def test_click_outside_the_window_sends_nothing():
    be = FakeBackend()
    rc, out = call(be, "click", "800", "10", "1")
    assert rc == 1 and "outside the window" in out["error"] and "nothing was sent" in out["error"]
    rc, out = call(be, "click", "-150", "0", "1")            # inside pid 99's window only
    assert rc == 1 and "outside the window" in out["error"]
    assert be.sent == []


def test_a_point_in_another_window_of_the_app_is_allowed():
    # a dropdown is a window of its own: window 2 covers screen 200..600 x 100..400,
    # so a point counted from window 1 may land in it beyond window 1's edge
    be = FakeBackend()
    be.wins[1]["rect"] = [850, 100, 300, 300]
    rc, out = call(be, "click", "780", "60", "1")             # inside window 1
    assert rc == 0
    rc, out = call(be, "click", "900", "60", "1")             # screen 1000,110: window 2
    assert rc == 0 and ("move", 1000, 110) in be.sent


def test_the_target_is_brought_forward_before_input():
    be = FakeBackend(front=99)
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 0
    assert be.sent[0] == ("focus", 1)


def test_input_is_refused_when_the_target_cannot_come_forward():
    be = FakeBackend(front=99, focusable=False)
    for argv in (["click", "10", "10"], ["type", "hello"], ["key", "ctrl", "s"], ["scroll", "5", "5", "3"]):
        rc, out = call(be, *argv)
        assert rc == 1 and "not sent" in out["error"], argv
    assert inputs(be) == []


def test_no_click_when_the_cursor_does_not_arrive():
    be = FakeBackend(cursor_sticks=False)
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 1 and "did not reach" in out["error"]
    assert inputs(be) == []


def test_drag_always_releases_the_button():
    be = FakeBackend()
    rc, _ = call(be, "drag", "10", "10", "100", "10", "1")
    assert rc == 0
    b = inputs(be)
    assert b[0] == ("button", "left", True) and b[-1] == ("button", "left", False)


def test_drag_releases_the_button_when_a_move_fails():
    be = FakeBackend()
    moves = []

    def move(x, y):
        moves.append((x, y))
        if len(moves) == 2:                            # mid-drag: the button is held down
            raise RuntimeError("simulated")
        be.sent.append(("move", x, y))
        be.pos = (x, y)
    be.move = move
    rc, out = call(be, "drag", "10", "10", "100", "10", "1")
    assert rc == 1 and not out["ok"]
    assert inputs(be) == [("button", "left", True), ("button", "left", False)]


def test_click_refused_when_another_app_covers_the_point():
    be = FakeBackend(owner=99)                          # pid 99's window lies over the point
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 1 and "covers" in out["error"] and "nothing was clicked" in out["error"]
    assert inputs(be) == []
    be = FakeBackend(owner=10)                          # the front window is the target's own
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 0
    assert inputs(be) == [("button", "left", True), ("button", "left", False)]


def test_hover_focus_title_and_newline():
    be = FakeBackend()
    rc, out = call(be, "hover", "10", "10", "1")        # a tooltip opens on hover alone
    assert rc == 0 and ("move", 110, 60) in be.sent and inputs(be) == []
    be = FakeBackend(front=99, focusable=False)
    rc, out = call(be, "focus")
    assert rc == 1 and "could not bring" in out["error"]
    rc, out = call(FakeBackend(), "windows", title="Open")
    assert [w["id"] for w in out["windows"]] == [2]     # only the window whose title matches
    be = FakeBackend()
    rc, out = call(be, "type", "one\ntwo")              # a newline reaches the backend unchanged
    assert rc == 0 and ("type", "one\ntwo") in be.sent


def test_one_window_listing_per_click(tmp_path):
    be = FakeBackend()
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 0 and be.listed == 1                   # window, point, pids and owner share one
    be = FakeBackend(front=99)                          # bringing forward changes the windows
    rc, out = call(be, "click", "10", "10", "1")
    assert rc == 0 and be.listed == 2                   # a fresh listing after the focus
    be = FakeBackend()
    rc, out = call(be, "resize", "900", "700", "1", outdir=tmp_path)
    assert rc == 0 and be.listed == 2                   # the read-back sees the new size
    be = FakeBackend()
    rc, out = call(be, "windows")
    assert rc == 0 and be.listed == 1


def test_unexpected_error_is_one_json_line():
    be = FakeBackend()

    def boom(keys):
        raise RuntimeError("driver exploded")
    be.keys = boom
    rc, out = call(be, "key", "ctrl", "s")
    assert rc == 1 and not out["ok"] and "RuntimeError" in out["error"] and inputs(be) == []


def test_scroll_steps_are_bounded():
    rc, out = call(FakeBackend(), "scroll", "10", "10", "500")
    assert rc == 1 and "1 to 50" in out["error"]


@pytest.mark.parametrize("platform,chord", [
    ("linux", ["alt", "f4"]), ("win32", ["alt+f4"]), ("win32", ["ctrl", "p"]),
    ("darwin", ["cmd", "q"]), ("darwin", ["cmd+w"]), ("darwin", ["cmd", "p"]),
    ("darwin", ["command", "space"]), ("linux", ["shift", "alt", "F4"]),
])
def test_closing_and_printing_are_refused(platform, chord):
    be = FakeBackend()
    rc, out = call(be, "key", *chord, platform=platform)
    assert rc == 1 and out["error"].startswith("refused:")
    assert inputs(be) == []


def test_destructive_chords_are_refused_ctrl_w_is_not():
    for platform, chord in (("linux", ["ctrl", "q"]), ("win32", ["shift", "delete"]),
                            ("linux", ["shift", "del"]), ("darwin", ["cmd", "backspace"]),
                            ("win32", ["shift+delete"]), ("linux", ["CTRL", "Q"]),
                            ("linux", ["Ctrl+Q"])):          # case and the plus form reach the same check
        be = FakeBackend()
        rc, out = call(be, "key", *chord, platform=platform)
        assert rc == 1 and out["error"].startswith("refused:"), (platform, chord)
        assert inputs(be) == []
    for platform in ("win32", "linux"):                 # an application's own tabs are legitimate work
        be = FakeBackend()
        rc, out = call(be, "key", "ctrl", "w", platform=platform)
        assert rc == 0 and ("keys", ("ctrl", "w")) in be.sent, platform


def test_chords_are_parsed_and_checked_before_sending():
    assert desktop.parse_chord(["ctrl+shift+s"], "linux") == ["ctrl", "shift", "s"]
    assert desktop.parse_chord(["Control", "Return"], "linux") == ["ctrl", "enter"]
    be = FakeBackend()
    rc, out = call(be, "key", "ctrl", "nosuchkey")
    assert rc == 1 and "unknown key" in out["error"] and inputs(be) == []
    rc, out = call(be, "key", "cmd", "s", platform="linux")
    assert rc == 1 and "macOS only" in out["error"]


def test_type_joins_words_and_is_bounded():
    be = FakeBackend()
    rc, out = call(be, "type", "hello", "world")
    assert rc == 0 and ("type", "hello world") in be.sent
    rc, out = call(be, "type", "x" * 2001)
    assert rc == 1 and "2000" in out["error"]


# ------------------------------------------------- win32 and darwin, off their own soil
# The two backends that cannot run here are built from bare instances with a fake
# user32 / Quartz in place of the DLLs: the code under test is the real code.

class FakeWinInput:
    def __init__(self, type=0):
        self.type = type
        self.u = types.SimpleNamespace()


class FakeWinMouse:
    def __init__(self, dx, dy, data, flags, time_, extra):
        self.dx, self.dy, self.data, self.flags = dx, dy, data, flags


class FakeWinKey:
    def __init__(self, vk, scan, flags, time_, extra):
        self.vk, self.scan, self.flags = vk, scan, flags


def win_backend(vk_scan=None):
    be = desktop.WinBackend.__new__(desktop.WinBackend)
    be.sent = []
    be.u = types.SimpleNamespace(VkKeyScanW=lambda c: (vk_scan or {}).get(chr(c), -1))
    be._send = lambda inputs: be.sent.append(inputs)
    be.INPUT, be.MOUSEINPUT, be.KEYBDINPUT = FakeWinInput, FakeWinMouse, FakeWinKey
    return be


def pressed_vks(be):
    return [i.u.ki.vk for batch in be.sent for i in batch if i.type == 1 and not i.u.ki.flags & 0x2]


def test_windows_shifted_punctuation_adds_shift():
    be = win_backend(vk_scan={"!": 0x0131, "%": 0x0235})   # '!' is shift+1, '%' is ctrl+5 on US
    be.keys(["ctrl", "!"])
    pressed = pressed_vks(be)
    assert 0x10 in pressed and 0x11 in pressed and 0x31 in pressed   # VK_SHIFT is added
    be = win_backend(vk_scan={"!": 0x0131})
    be.keys(["shift", "!"])                                  # already in the chord: not added twice
    assert pressed_vks(be).count(0x10) == 1
    be = win_backend(vk_scan={"%": 0x0235})                  # the ctrl bit is not the shift bit
    be.keys(["%"])
    assert 0x10 not in pressed_vks(be)
    with pytest.raises(desktop.Fail):                        # VkKeyScanW's -1 still means "no key"
        be.keys(["€"])


def test_windows_focus_fallback_sends_no_key(monkeypatch):
    be = win_backend()
    slept = []
    monkeypatch.setattr(desktop.time, "sleep", slept.append)
    state = {"setfg": 0}

    def set_fg(h):
        state["setfg"] += 1
        return state["setfg"] > 1                    # the first attempt meets the foreground lock
    be.u.IsIconic = lambda h: False
    be.u.SetForegroundWindow = set_fg
    be.u.GetForegroundWindow = lambda: 999           # and the window is still not in front
    be.focus({"id": 7, "pid": 10, "title": "t", "rect": [0, 0, 800, 600]})
    assert state["setfg"] == 2 and slept == [0.1]
    sent = [i for batch in be.sent for i in batch]
    assert [i for i in sent if i.type == 1] == []    # no Alt tap, no key of any kind
    moves = [i for i in sent if i.type == 0]
    assert len(moves) == 1 and moves[0].u.mi.flags == 0x0001    # MOUSEEVENTF_MOVE
    assert moves[0].u.mi.dx == 0 and moves[0].u.mi.dy == 0      # zero distance
    be2 = win_backend()                                         # when the first try works, nothing else is sent
    be2.u.IsIconic = lambda h: False
    be2.u.SetForegroundWindow = lambda h: True
    be2.u.GetForegroundWindow = lambda: 7
    be2.focus({"id": 7, "pid": 10, "title": "t", "rect": [0, 0, 800, 600]})
    assert be2.sent == [] and slept == [0.1]                    # no fallback, no new sleep


class MacTestBackend(desktop.MacBackend):
    """A MacBackend without a Mac: it records the event kinds move and button post."""

    def __init__(self):
        self.pos, self.ev, self.app = None, [], "app"
        self.q = types.SimpleNamespace(
            CGEventCreateMouseEvent=lambda src, kind, pt, btn: ("mouse", kind, int(pt.x), int(pt.y)),
            CGEventSetIntegerValueField=lambda ev, field, v: None)
        self.cf = None

    def _post(self, ev):
        self.ev.append(ev)

    def windows(self, app):
        return [{"id": "10:0", "pid": 10, "title": "Doc", "rect": [100, 50, 800, 600], "active": True}]

    def foreground_pid(self):
        return 10

    def cursor(self):
        return (int(self.pos.x), int(self.pos.y)) if self.pos else (0, 0)


def test_macos_drag_posts_dragged_events():
    be = MacTestBackend()
    rc, out = call(be, "drag", "10", "10", "110", "10", platform="darwin")
    assert rc == 0, out
    kinds = [e[1] for e in be.ev]
    assert kinds[0] == 5 and kinds[1] == 1           # move before the press is mouseMoved
    assert set(kinds[2:-1]) == {6}                   # leftMouseDragged while the button is held
    assert kinds[-1] == 2                            # release
    rc, out = call(be, "hover", "20", "20", platform="darwin")
    assert rc == 0 and be.ev[-1][1] == 5             # after the release, moves are moves again


def test_macos_scroll_error_names_10_13():
    be = MacTestBackend()                            # no CGEventCreateScrollWheelEvent2: too old
    with pytest.raises(desktop.Fail) as e:
        be.scroll(1)
    assert "macOS 10.13" in str(e.value)


def test_macos_check_refuses_pillow_older_than_9_2(monkeypatch):
    PIL = pytest.importorskip("PIL")
    be = MacTestBackend()
    monkeypatch.setattr(PIL, "__version__", "9.0.1", raising=False)
    with pytest.raises(desktop.Fail) as e:
        be.check()
    assert "Pillow 9.2" in str(e.value)
    monkeypatch.setattr(PIL, "__version__", "10.1.0", raising=False)
    be.q = types.SimpleNamespace(AXIsProcessTrusted=lambda: True)
    assert be.check() is None                        # the gate lets 9.2 and later through


# ------------------------------------------------------------------ images

def test_shot_saves_inside_the_folder_and_reports_scale(tmp_path):
    pytest.importorskip("PIL")
    be = FakeBackend()
    rc, out = call(be, "shot", "../../etc/evil name", "1", outdir=tmp_path)
    assert rc == 0, out
    p = pathlib.Path(out["path"])
    assert p.parent == tmp_path and p.name == "evil_name.png" and p.is_file()
    assert out["scale"] == 1.0 and out["size"] == [800, 600]
    be.wins[0]["rect"] = [0, 0, 2560, 1440]
    rc, out = call(be, "shot", "big", "1", "grid", outdir=tmp_path)
    assert rc == 0 and out["size"] == [1280, 720] and out["scale"] == 2.0
    assert "resize the window" in out["note"]


def test_shot_without_a_window_id_takes_the_dialog_in_front(tmp_path):
    pytest.importorskip("PIL")
    be = FakeBackend()
    be.windows = lambda app: [dict(w, active=w["id"] == 2) for w in be.wins if w["pid"] == 10]
    rc, out = call(be, "shot", "s", outdir=tmp_path)
    assert rc == 0 and out["window"] == 2


def test_crop_is_window_relative_zoomed_and_clipped(tmp_path):
    pytest.importorskip("PIL")
    be = FakeBackend()
    grabbed = []
    real = be.grab
    be.grab = lambda x, y, w, h: grabbed.append((x, y, w, h)) or real(x, y, w, h)
    rc, out = call(be, "crop", "c", "10", "20", "100", "50", "3", "grid", "1", outdir=tmp_path)
    assert rc == 0, out
    assert grabbed == [(110, 70, 100, 50)] and out["size"] == [300, 150] and out["zoom"] == 3
    rc, out = call(be, "crop", "c", "750", "550", "200", "200", "2", "1", outdir=tmp_path)
    assert rc == 0 and grabbed[-1] == (850, 600, 50, 50)       # clipped to the window
    rc, out = call(be, "crop", "c", "1", "1", "10", "10", "9", outdir=tmp_path)
    assert rc == 1 and "zoom" in out["error"]


def test_grid_step_is_round():
    assert desktop.grid_step(1280) == 200
    assert desktop.grid_step(100) == 10
    assert desktop.grid_step(40) == 5


# ------------------------------------------------------------------ window size

def test_resize_saves_the_first_size_and_restore_puts_it_back(tmp_path):
    be = FakeBackend()
    rc, out = call(be, "resize", "1280", "800", "1", outdir=tmp_path)
    assert rc == 0 and out["before"] == [100, 50, 800, 600] and out["after"] == [100, 50, 1280, 800]
    call(be, "resize", "1000", "700", "0", "0", "1", outdir=tmp_path)
    rc, out = call(be, "restore", "1", outdir=tmp_path)
    assert rc == 0 and out["after"] == [100, 50, 800, 600]       # the FIRST size, not the second
    rc, out = call(be, "restore", "2", outdir=tmp_path)
    assert rc == 1 and "never resized" in out["error"]


def test_resize_reports_a_size_the_application_kept(tmp_path):
    be = FakeBackend()
    be.set_rect = lambda w, x, y, cw, ch: None                  # the app ignores it
    rc, out = call(be, "resize", "1280", "800", "1", outdir=tmp_path)
    assert rc == 0 and "kept another size" in out["note"]


# ------------------------------------------------------------------ the rest

def test_errors_are_one_json_line_never_a_traceback():
    be = FakeBackend()
    for argv in (["nosuch"], ["click"], ["click", "x", "y"], ["wait", "99"], ["shot"]):
        rc, out = call(be, *argv)
        assert rc == 1 and not out["ok"] and out["error"], argv


def test_help_works_even_when_locked():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = desktop.main(["--app", "a", "--dir", ".", "--lock", "--help"], backend=FakeBackend())
    assert rc == 0 and "resize W H" in buf.getvalue()


def test_check_needs_pillow_and_a_window():
    be = FakeBackend()
    rc, out = call(be, "check", app="nothing-running")
    assert rc == 1
    if not importlib.util.find_spec("PIL"):
        assert "Pillow is not installed" in out["error"]
        return
    assert "start the application first" in out["error"]
    rc, out = call(be, "check")
    assert rc == 0 and out["windows"] == 2


def test_the_driver_never_imports_pillow_at_load():
    src = (LIB / "desktop.py").read_text(encoding="utf-8")
    assert not [ln for ln in src.splitlines() if ln.startswith(("import PIL", "from PIL"))]
