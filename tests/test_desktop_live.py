"""qla-desktop on a real X server: Xvfb with a small Tk window that logs what it gets.
Skipped unless Linux has Xvfb, Tk, Pillow, libX11 and libXtst."""
import ctypes.util
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

DRIVER = pathlib.Path(__file__).resolve().parent.parent / "skill" / "local-auditor" / "lib" / "desktop.py"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("Xvfb")
    or not importlib.util.find_spec("tkinter") or not importlib.util.find_spec("PIL")
    or not ctypes.util.find_library("Xtst"),
    reason="needs Linux with Xvfb, tkinter, Pillow and libXtst")

APP = r'''
import sys, tkinter as tk
log = open(sys.argv[1], "a", buffering=1)
r = tk.Tk(); r.title("qla live test"); r.geometry("600x400+40+30")
tk.Button(r, text="Press", command=lambda: log.write("pressed\n")).place(x=20, y=20, width=100, height=40)
e = tk.Entry(r); e.place(x=20, y=100, width=300, height=30)
r.bind("<Control-s>", lambda _: log.write("entry=%s\n" % e.get()))
log.write("up\n")
r.mainloop()
'''


@pytest.fixture
def xapp(tmp_path):
    disp = ":%d" % (90 + os.getpid() % 500)
    # a Wayland user's shell: the session says wayland, but DISPLAY is Xvfb, which is drivable
    env = dict(os.environ, DISPLAY=disp, XDG_SESSION_TYPE="wayland")
    x = subprocess.Popen(["Xvfb", disp, "-screen", "0", "1280x1024x24"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = None
    try:
        time.sleep(0.8)
        (tmp_path / "app.py").write_text(APP, encoding="utf-8")
        log = tmp_path / "log.txt"
        app = subprocess.Popen([sys.executable, str(tmp_path / "app.py"), str(log)], env=env)
        for _ in range(50):
            if log.exists() and "up" in log.read_text():
                break
            time.sleep(0.1)
        time.sleep(0.5)
        yield env, log
    finally:
        if app:
            app.kill()
            app.wait()
        x.kill()
        x.wait()


# A plain Xlib window, mapped above everything and never touching the input focus:
# the self-raising window of "another application" that must take the click.
COVER = r'''
import ctypes, ctypes.util, sys, time
X = ctypes.CDLL(ctypes.util.find_library("X11"))
V, U, I = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
X.XOpenDisplay.restype = V
X.XDefaultRootWindow.argtypes = [V]
X.XCreateSimpleWindow.restype = U
X.XCreateSimpleWindow.argtypes = [V, U, I, I, U, U, U, U, U]
X.XMapRaised.argtypes = [V, U]
X.XSync.argtypes = [V, I]
d = X.XOpenDisplay(None)
w = X.XCreateSimpleWindow(d, X.XDefaultRootWindow(d), 300, 150, 100, 100, 0, 0, 0xFF0000)
X.XMapRaised(d, w)
X.XSync(d, 0)
open(sys.argv[1], "w").write("up\n")
time.sleep(300)
'''


def drive(env, tmp_path, *args):
    name = os.path.basename(sys.executable)
    r = subprocess.run([sys.executable, str(DRIVER), "--app", name, "--title", "qla live",
                        "--dir", str(tmp_path / "out"), "--lock"] + list(args),
                       env=env, capture_output=True, text=True, timeout=30)
    out = json.loads(r.stdout)
    assert (r.returncode == 0) == out["ok"]
    return out


def test_click_type_key_and_resize_reach_the_window(xapp, tmp_path):
    env, log = xapp
    assert drive(env, tmp_path, "check")["ok"]
    [w] = drive(env, tmp_path, "windows")["windows"]
    assert w["rect"][2:] == [600, 400]
    shot = drive(env, tmp_path, "shot", "s1", "grid")
    assert shot["ok"] and shot["scale"] == 1.0 and pathlib.Path(shot["path"]).is_file()
    assert drive(env, tmp_path, "click", "70", "40")["ok"]
    assert drive(env, tmp_path, "click", "150", "115")["ok"]
    assert drive(env, tmp_path, "type", "Hi There!")["ok"]
    assert drive(env, tmp_path, "key", "ctrl", "s")["ok"]
    assert not drive(env, tmp_path, "key", "alt", "f4")["ok"]
    r = drive(env, tmp_path, "resize", "800", "500")
    assert r["ok"] and r["after"][2:] == [800, 500]
    assert drive(env, tmp_path, "restore")["after"][2:] == [600, 400]
    crop = drive(env, tmp_path, "crop", "c1", "0", "0", "200", "150", "3", "grid")
    assert crop["ok"] and crop["size"] == [600, 450]
    time.sleep(0.3)
    lines = log.read_text().splitlines()
    assert "pressed" in lines and "entry=Hi There!" in lines


def test_click_on_a_covered_point_is_refused(xapp, tmp_path):
    env, log = xapp
    assert drive(env, tmp_path, "check")["ok"]
    assert drive(env, tmp_path, "focus")["ok"]          # the target is in front and owns the keyboard
    (tmp_path / "cover.py").write_text(COVER, encoding="utf-8")
    flag = tmp_path / "cover.txt"
    cover = subprocess.Popen([sys.executable, str(tmp_path / "cover.py"), str(flag)], env=env)
    try:
        for _ in range(50):
            if flag.exists() and "up" in flag.read_text():
                break
            time.sleep(0.1)
        # window point 310,170 is screen 350,200: inside the target, under the cover window
        out = drive(env, tmp_path, "click", "310", "170")
        assert not out["ok"] and "covers" in out["error"]
        # 550,380 (screen 590,410) belongs to the target alone: the click goes through
        assert drive(env, tmp_path, "click", "550", "380")["ok"]
    finally:
        cover.kill()
        cover.wait()
