"""qwen-cc: launch and steer an interactive session, against a fake qwen-agent.

Every tmux and qwen-cc call here carries TMUX_TMPDIR=<the test's temp dir> and no
TMUX, so it talks to a tmux SERVER OF ITS OWN -- created by that first call and
killed in the fixture's finalizer -- and never to the developer's. A fake
`qwen-agent` sits at the front of PATH, so the real one is never launched.
"""
import atexit
import os
import pathlib
import re
import shutil
import tempfile
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CC = ROOT / "skill" / "local-auditor" / "qwen-cc.sh"
TMUX = shutil.which("tmux")
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
# What a launch needs from the host, symlinked into a PATH dir of its own. A test
# that wants to control which terminal opener is found cannot simply prepend to the
# host PATH: this machine really does have a gnome-terminal on it.
REAL_TOOLS = ("bash", "sh", "tmux", "date", "uname", "grep", "sleep")

FAKE_AGENT = r'''#!/bin/bash
# Stands in for `qwen-agent --interactive`: announces itself, echoes every line
# it is typed into, and ends on /exit -- so --peek shows the work and --stop works.
echo READY
while IFS= read -r line; do
  printf 'GOT: %s\n' "$line"
  [ "$line" = "/exit" ] && exit 0
done
exit 0
'''

FAKE_LISTENER = r'''#!/bin/bash
# A session that will not be talked out of: only --stop --force ends it. exec, so
# the pane process IS the sleep and killing the session leaves nothing behind.
echo READY
exec sleep 300
'''

FAKE_ARGV = r'''#!/bin/bash
# The same agent, but it prints its own argv first, one argument per line, so a test
# can see how the words survived the tmux command string -- boundaries intact or not.
for a in "$@"; do printf 'ARG[%s]\n' "$a"; done
echo READY
while IFS= read -r line; do
  printf 'GOT: %s\n' "$line"
  [ "$line" = "/exit" ] && exit 0
done
exit 0
'''


def posix(p):
    return str(p).replace("\\", "/")


def shq(s):
    """Single-quoting exactly as qwen-cc.sh's q_sq() does it: the string the pane's
    shell will be handed, and what --dry-run prints so the line can be pasted."""
    return "'" + s.replace("'", "'\\''") + "'"


def expected_command(name, proj, *extra):
    """The 'command:' line a launch of PROJ (+ EXTRA arguments) must print: every
    argument single-quoted into one string for the pane's shell, and that string
    quoted again as one tmux argument -- which is what makes the line pasteable."""
    cmd = "qwen-agent --interactive -C " + shq(posix(proj))
    for a in extra:
        cmd += " " + shq(a)
    return "command: tmux new-session -d -s %s %s" % (name, shq(cmd))


def command_line(r):
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("command: ")]
    assert len(lines) == 1, r.stdout
    return lines[0]


def _script_dir(tmp_path, name, files=None, links=()):
    """A bin dir of fake scripts and/or symlinks to real tools, for PATH."""
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    for fn, body in (files or {}).items():
        p = d / fn
        p.write_text(body, encoding="utf-8", newline="\n")
        p.chmod(0o755)
    for tool in links:
        src = shutil.which(tool)
        if src is not None:
            os.symlink(src, str(d / tool))
    return d


_SOCK_ROOTS = {}
# Tests that never start the tmuxd fixture still make a root; remove whatever is left.
atexit.register(lambda: [shutil.rmtree(r, ignore_errors=True) for r in _SOCK_ROOTS.values()])


def _sock_root(tmp_path):
    """A SHORT directory for this test's tmux socket. A unix socket path is capped at about
    104 bytes, and macOS's pytest temp dirs (/private/var/folders/...) are longer than
    that, so the socket cannot live under tmp_path itself."""
    key = str(tmp_path)
    if key not in _SOCK_ROOTS:
        base = "/tmp" if os.path.isdir("/tmp") else None
        root = tempfile.mkdtemp(prefix="qcc", dir=base)
        sock = os.path.join(root, "tmux-%d" % getattr(os, "getuid", lambda: 0)())
        os.mkdir(sock)
        os.chmod(sock, 0o700)                # tmux refuses a group-writable socket dir
        _SOCK_ROOTS[key] = root
    return _SOCK_ROOTS[key]


def _base_env(tmp_path, path=None):
    """The environment every tmux / qwen-cc call gets: private server, no TMUX,
    no display (so no window is opened by accident), no SSH session (so no remote
    attach line) and no QWEN_* from the host."""
    skip = ("TMUX", "TMUX_PANE", "DISPLAY", "WAYLAND_DISPLAY", "PATH", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    env = {k: v for k, v in os.environ.items()
           if k not in skip and not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["TMUX_TMPDIR"] = _sock_root(tmp_path)
    env["PATH"] = path if path is not None else str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]
    return env


def cc(tmp_path, *args, path=None, env_extra=None):
    env = _base_env(tmp_path, path)
    env.update(env_extra or {})
    return subprocess.run([BASH, posix(CC), *[str(a) for a in args]], env=env, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=180)


def tmux(tmp_path, *args, check=True):
    p = subprocess.run([TMUX, *[str(a) for a in args]], env=_base_env(tmp_path),
                       capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise AssertionError("tmux %s failed (%d): %s" % (" ".join(map(str, args)), p.returncode, p.stderr))
    return p


def session_of(r):
    lines = r.stdout.splitlines()
    assert lines and lines[0].startswith("session: "), r.stdout + r.stderr
    return lines[0][len("session: "):]


def live_names(tmp_path):
    """Every tmux session name on the private server (exact, no prefix matching)."""
    return tmux(tmp_path, "list-sessions", "-F", "#S", check=False).stdout.split()


def wait_for(tmp_path, name, needle, seconds=20):
    """Poll --peek until the pane shows NEEDED (the pane is asynchronous)."""
    deadline = time.time() + seconds
    screen = ""
    while time.time() < deadline:
        screen = cc(tmp_path, "--peek", name).stdout
        if needle in screen:
            return screen
        time.sleep(0.2)
    raise AssertionError("no %r in the pane within %ds; last screen:\n%s" % (needle, seconds, screen))


@pytest.fixture
def tmuxd(tmp_path):
    """A private tmux server plus a fake qwen-agent on PATH, torn down after."""
    if TMUX is None:
        pytest.skip("tmux is not on PATH")
    d = _script_dir(tmp_path, "bin", files={"qwen-agent": FAKE_AGENT})
    yield d
    subprocess.run([TMUX, "kill-server"], env=_base_env(tmp_path),
                   capture_output=True, text=True, timeout=60)
    shutil.rmtree(_SOCK_ROOTS.pop(str(tmp_path), ""), ignore_errors=True)


def test_launch_creates_tagged_session(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    r = cc(tmp_path, str(proj), "--no-window")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 2, r.stdout                    # the two lines, nothing else
    name = session_of(r)
    assert lines[1] == "attach: tmux attach -t %s" % name
    assert re.match(r"^qwen-proj-\d{6}$", name), name
    assert name in live_names(tmp_path)                   # detached, and it exists
    tagged = tmux(tmp_path, "show-options", "-qv", "-t", name, "@qwen_cc")
    assert tagged.stdout.strip() == "1"
    assert "READY" in wait_for(tmp_path, name, "READY")   # the fake qwen-agent is running
    assert name in cc(tmp_path, "--list").stdout.splitlines()
    # it is detached: no client is attached to it
    assert tmux(tmp_path, "list-clients", "-t", name, check=False).stdout.strip() == ""


def test_launch_hands_qwen_agent_its_arguments(tmp_path, tmuxd):
    # --dry-run prints the quoting; this checks that it works: what the pane's
    # qwen-agent receives is one argument per line, a value with a space included.
    (tmuxd / "qwen-agent").write_text(FAKE_ARGV, encoding="utf-8", newline="\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window", "--", "--model", "a b",
                         "--effort", "medium"))
    screen = wait_for(tmp_path, name, "ARG[medium]")
    for needle in ("ARG[--interactive]", "ARG[-C]", "ARG[%s]" % posix(proj),
                   "ARG[--model]", "ARG[a b]", "ARG[--effort]"):
        assert needle in screen, needle


def test_session_names_are_unique(tmp_path, tmuxd):
    # A frozen clock makes the collision real instead of timing-dependent: three
    # launches in the same second on the same directory cannot share a name.
    clock = _script_dir(tmp_path, "clock", files={"date": "#!/bin/sh\necho 121314\n"})
    path = posix(clock) + os.pathsep + posix(tmuxd) + os.pathsep + os.environ["PATH"]
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    names = []
    for _ in range(3):
        r = cc(tmp_path, str(alpha), "--no-window", path=path)
        assert r.returncode == 0, r.stdout + r.stderr
        names.append(session_of(r))
    assert names == ["qwen-alpha-121314", "qwen-alpha-121314-2", "qwen-alpha-121314-3"], names
    # a different directory gives a different name, with non-alphanumerics as '-'
    odd = tmp_path / "beta gamma"
    odd.mkdir()
    r = cc(tmp_path, str(odd), "--no-window", path=path)
    assert r.returncode == 0, r.stdout + r.stderr
    other = session_of(r)
    assert other == "qwen-beta-gamma-121314", other
    assert "READY" in wait_for(tmp_path, other, "READY")   # the quoted path reached it
    assert sorted(cc(tmp_path, "--list").stdout.split()) == sorted(names + [other])
    # --stop works on each, so nothing is left behind for the next test
    for name in names + [other]:
        assert cc(tmp_path, "--stop", name).stdout.strip() == "stopped %s" % name


def test_list_peek_say_stop(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    assert cc(tmp_path, "--list").stdout.splitlines() == [name]
    assert "READY" in wait_for(tmp_path, name, "READY")

    r = cc(tmp_path, "--say", name, "fix the failing test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GOT: fix the failing test" in wait_for(tmp_path, name, "GOT: fix the failing test")
    # --say types the text literally: a key name stays a word, and multiple
    # arguments are one line
    assert cc(tmp_path, "--say", name, "Enter", "~/x").returncode == 0
    assert "GOT: Enter ~/x" in wait_for(tmp_path, name, "GOT: Enter ~/x")

    # Enough output to scroll the pane, so LINES has history to count.
    for i in range(25):
        cc(tmp_path, "--say", name, "line%d" % i)
    assert "GOT: line24" in wait_for(tmp_path, name, "GOT: line24")
    # LINES is how far back into the SCROLLBACK to reach; the visible pane is part
    # of every capture (that is what tmux's -S means), so what a short --peek proves
    # is that the older work is out of reach, and a big count brings it back.
    short = cc(tmp_path, "--peek", name, "5")
    assert short.returncode == 0, short.stderr
    assert "GOT: line24" in short.stdout
    assert "line0" not in short.stdout and "READY" not in short.stdout
    whole = cc(tmp_path, "--peek", name, "200")
    assert "READY" in whole.stdout and "GOT: line0" in whole.stdout
    assert cc(tmp_path, "--peek", name, "0").returncode == 2
    assert cc(tmp_path, "--peek", name, "many").returncode == 2

    r = cc(tmp_path, "--stop", name)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "stopped %s" % name
    assert name not in live_names(tmp_path)
    assert cc(tmp_path, "--list").stdout.strip() == ""


def test_stop_force_kills_a_session_that_will_not_exit(tmp_path, tmuxd):
    (tmuxd / "qwen-agent").write_text(FAKE_LISTENER, encoding="utf-8", newline="\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    assert "READY" in wait_for(tmp_path, name, "READY")
    r = cc(tmp_path, "--stop", "--force", name)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "stopped %s" % name
    assert name not in live_names(tmp_path)


def test_refuses_untagged_sessions(tmp_path, tmuxd):
    # The user's own sessions are none of qwen-cc's business: not to read, not to
    # type into, not to kill.
    tmux(tmp_path, "new-session", "-d", "-s", "user-work", "sleep 300")
    for args, name in (
            (["--peek", "user-work"], "user-work"),
            (["--peek", "user-work", "5"], "user-work"),
            (["--say", "user-work", "rm -rf /"], "user-work"),
            (["--stop", "user-work"], "user-work"),
            (["--stop", "--force", "user-work"], "user-work"),
            (["--peek", "never-existed"], "never-existed"),
            (["--stop", "gone-away"], "gone-away")):
        r = cc(tmp_path, *args)
        assert r.returncode == 2, (args, r.stdout, r.stderr)
        assert name in r.stderr                       # the message names the session
    assert "user-work" in live_names(tmp_path)        # still alive
    screen = tmux(tmp_path, "capture-pane", "-p", "-t", "user-work").stdout
    assert "rm -rf" not in screen
    assert tmux(tmp_path, "show-options", "-qv", "-t", "user-work", "@qwen_cc",
                check=False).stdout.strip() == ""
    assert "user-work" not in cc(tmp_path, "--list").stdout


def test_a_name_that_is_a_prefix_of_another_is_not_that_session(tmp_path, tmuxd):
    # tmux resolves a plain -t argument by PREFIX as well as by exact name, so identity
    # is settled against the exact session list first. Two launches in one second make
    # exactly this pair of names: "qwen-a-121314" is a prefix of "qwen-a-121314-2".
    # The session is tagged the way a launch tags it: @qwen_cc and the agent's pane.
    pane = tmux(tmp_path, "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "qwen-a-121314-2",
                "sleep 300").stdout.strip()
    tmux(tmp_path, "set-option", "-t", "=qwen-a-121314-2:", "@qwen_cc_pane", pane)
    tmux(tmp_path, "set-option", "-t", "=qwen-a-121314-2:", "@qwen_cc", "1")
    assert tmux(tmp_path, "has-session", "-t", "qwen-a-121314", check=False).returncode == 0
    for args in (["--peek", "qwen-a-121314"], ["--say", "qwen-a-121314", "hi"],
                 ["--stop", "qwen-a-121314"], ["--stop", "--force", "qwen-a-121314"]):
        r = cc(tmp_path, *args)
        assert r.returncode == 2, (args, r.stdout, r.stderr)
        assert "qwen-a-121314" in r.stderr
    assert "qwen-a-121314-2" in live_names(tmp_path)             # the real one, untouched
    assert "hi" not in tmux(tmp_path, "capture-pane", "-p", "-t", "qwen-a-121314-2").stdout
    assert cc(tmp_path, "--list").stdout.split() == ["qwen-a-121314-2"]
    assert cc(tmp_path, "--peek", "qwen-a-121314-2").returncode == 0


def test_window_dry_run(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    mac = sys.platform == "darwin"            # macOS opens Terminal through osascript
    fake = "#!/bin/sh\necho \"$0\"\n"
    both = _script_dir(tmp_path, "gui-both",
                       files={"gnome-terminal": fake, "x-terminal-emulator": fake, "osascript": fake},
                       links=REAL_TOOLS)
    r = cc(tmp_path, str(proj), "--window", "--dry-run", path=posix(both))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    name = session_of(r)
    assert lines[1] == "attach: tmux attach -t %s" % name
    assert command_line(r) == expected_command(name, proj)
    window = [ln for ln in lines if ln.startswith("window: ")][0]
    if mac:
        assert window.startswith("window: osascript -e ") and "tmux attach -t %s" % name in window
    else:
        assert window == "window: gnome-terminal -- tmux attach -t %s" % name
    assert cc(tmp_path, "--list").stdout.strip() == ""    # --dry-run ran nothing

    if not mac:
        only = _script_dir(tmp_path, "gui-xterm", files={"x-terminal-emulator": "#!/bin/sh\necho x\n"},
                           links=REAL_TOOLS)
        r = cc(tmp_path, str(proj), "--window", "--dry-run", path=posix(only))
        assert "window: x-terminal-emulator -e tmux attach -t %s" % session_of(r) in r.stdout

    # no opener at all: say what to do instead, and keep going with the session
    none = _script_dir(tmp_path, "gui-none", links=REAL_TOOLS)
    r = cc(tmp_path, str(proj), "--window", "--dry-run", path=posix(none))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "window: none (attach with the command above)" in r.stdout
    # a display makes the window automatic; --no-window and --dry-run say no
    r = cc(tmp_path, str(proj), "--dry-run", path=posix(none), env_extra={"DISPLAY": ":0"})
    assert "window: none (attach with the command above)" in r.stdout
    r = cc(tmp_path, str(proj), "--dry-run", "--no-window", path=posix(none),
           env_extra={"WAYLAND_DISPLAY": "wayland-0"})
    assert "window:" not in r.stdout
    # extra arguments go to qwen-agent, each quoted so a space does not split it
    r = cc(tmp_path, str(proj), "--no-window", "--dry-run", "--", "--model", "a b",
           "--effort", "medium", path=posix(both))
    assert r.returncode == 0, r.stdout + r.stderr
    assert command_line(r) == expected_command(session_of(r), proj, "--model", "a b",
                                               "--effort", "medium")


def test_launch_refuses_a_missing_directory(tmp_path, tmuxd):
    r = cc(tmp_path, str(tmp_path / "nope"), "--no-window")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no such directory" in r.stderr and "nope" in r.stderr
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    r = cc(tmp_path, str(file), "--no-window")
    assert r.returncode == 2 and "not a directory" in r.stderr
    assert cc(tmp_path, "--list").stdout.strip() == ""   # nothing was created


def test_requires_tmux(tmp_path):
    # Runs whether or not tmux is installed here: PATH simply has no tmux on it,
    # which is the situation on a machine without one.
    empty = tmp_path / "no-tmux"
    empty.mkdir()
    for args in ([str(tmp_path)], ["--list"], ["--peek", "x"], ["--say", "x", "hi"],
                 ["--stop", "x"], ["--stop", "--force", "x"]):
        r = cc(tmp_path, *args, path=posix(empty))
        assert r.returncode == 2, (args, r.stdout, r.stderr)
        assert "tmux" in r.stderr.lower() and "required" in r.stderr.lower(), r.stderr


def test_a_mode_refuses_another_modes_flags(tmp_path, tmuxd):
    # Ignoring a flag silently would advertise an effect the mode does not have.
    for args, needle in (
            (["--peek", "gone", "--window"], "--window"),
            (["--list", "--no-window"], "--no-window"),
            (["--say", "gone", "hi", "--dry-run"], "--dry-run"),
            (["--peek", "gone", "--force"], "--force"),
            (["--list", "gone"], "--list takes no arguments"),
            (["--stop", "gone", "extra"], "--stop needs exactly one")):
        r = cc(tmp_path, *args)
        assert r.returncode == 2, (args, r.stdout, r.stderr)
        assert needle in r.stderr, (args, r.stderr)
    assert cc(tmp_path, "--list").stdout.strip() == ""


def test_help_documents_every_mode(tmp_path, tmuxd):
    r = cc(tmp_path, "--help")
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("--list", "--peek", "--say", "--stop", "--force", "--window", "--no-window",
                   "--dry-run", "qwen-agent --interactive", "@qwen_cc", "gnome-terminal",
                   "x-terminal-emulator", "osascript", "window: none", "tmux attach -t",
                   "stopped", "still running", "session:", "attach:"):
        assert needle in r.stdout, needle
    assert "qwen-agent --interactive" in r.stdout


def test_stop_counts_a_dead_pane_as_stopped(tmp_path, tmuxd):
    # With remain-on-exit (some tmux configs set it globally) the session outlives the
    # program it ran; --stop must still see that the program ended, and clean up.
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    assert "READY" in wait_for(tmp_path, name, "READY")
    tmux(tmp_path, "set-option", "-t", name, "remain-on-exit", "on")
    r = cc(tmp_path, "--stop", name)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "stopped %s" % name
    assert name not in live_names(tmp_path)


def test_ssh_session_prints_a_remote_attach_line(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    over_ssh = cc(tmp_path, str(proj), "--no-window", "--dry-run",
                  env_extra={"SSH_CONNECTION": "192.0.2.2 50000 192.0.2.1 22"})
    assert over_ssh.returncode == 0, over_ssh.stderr
    remote = [ln for ln in over_ssh.stdout.splitlines() if ln.startswith("remote: ssh -t ")]
    assert len(remote) == 1 and remote[0].endswith("tmux' attach -t %s" % session_of(over_ssh))
    local = cc(tmp_path, str(proj), "--no-window", "--dry-run")
    assert not [ln for ln in local.stdout.splitlines() if ln.startswith("remote:")]


def test_steering_goes_to_the_agent_pane_not_the_active_one(tmp_path, tmuxd):
    # A person attached and opened a shell in a second window: --say and --peek must
    # still reach the agent's own pane, never type into that shell.
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    assert "READY" in wait_for(tmp_path, name, "READY")
    marker = tmp_path / "SHELL-RAN"
    tmux(tmp_path, "new-window", "-t", "=%s:" % name, "cat")      # becomes the active pane
    r = cc(tmp_path, "--say", name, "touch %s" % marker)
    assert r.returncode == 0, r.stderr
    assert "GOT: touch" in wait_for(tmp_path, name, "GOT: touch")
    assert not marker.exists()


def test_say_refuses_line_breaks(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    r = cc(tmp_path, "--say", name, "first\nsecond")
    assert r.returncode == 2 and "line break" in r.stderr


def test_force_is_refused_on_a_launch(tmp_path, tmuxd):
    proj = tmp_path / "proj"
    proj.mkdir()
    r = cc(tmp_path, str(proj), "--force", "--no-window")
    assert r.returncode == 2 and "--force" in r.stderr


@pytest.mark.parametrize("compat", ["", "32"])
def test_hostile_directory_name_is_one_argument(tmp_path, tmuxd, compat):
    # A quote in the directory name must not break out of the command tmux runs,
    # including on bash 3.2 (macOS), where the old quoting let x';touch PWNED;# run.
    proj = tmp_path / "x';touch PWNED;#"
    proj.mkdir()
    env = {"BASH_COMPAT": compat} if compat else None
    r = cc(tmp_path, str(proj), "--no-window", env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    name = session_of(r)
    assert "READY" in wait_for(tmp_path, name, "READY")
    # An injected command would run once qwen-agent EXITS cleanly (Ctrl-C would take the
    # whole command line down with it), so ask the fake agent to exit.
    assert cc(tmp_path, "--say", name, "/exit").returncode == 0
    deadline = time.time() + 10
    while name in live_names(tmp_path) and time.time() < deadline:
        time.sleep(0.2)
    time.sleep(0.5)
    assert not list(tmp_path.rglob("PWNED"))


def test_launch_never_goes_through_the_users_shell(tmp_path, tmuxd):
    # tmux runs a single command STRING through $SHELL, and no quoting is safe in every
    # shell (fish reads \' inside single quotes differently from sh). The launch hands
    # tmux an argv instead: a $SHELL that records any use and refuses to run proves it.
    marker = tmp_path / "SHELL-USED"
    shell = tmp_path / "bin" / "logging-shell"
    shell.write_text("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % shq(str(marker)),
                     encoding="utf-8", newline="\n")
    shell.chmod(0o755)
    proj = tmp_path / "x\\';touch PWNED;#"
    proj.mkdir()
    r = cc(tmp_path, str(proj), "--no-window", env_extra={"SHELL": str(shell)})
    assert r.returncode == 0, r.stdout + r.stderr
    name = session_of(r)
    assert "READY" in wait_for(tmp_path, name, "READY")
    assert cc(tmp_path, "--say", name, "/exit").returncode == 0
    deadline = time.time() + 10
    while name in live_names(tmp_path) and time.time() < deadline:
        time.sleep(0.2)
    assert not marker.exists() or "qwen-agent" not in marker.read_text()
    assert not list(tmp_path.rglob("PWNED"))


def test_refuses_an_agent_pane_in_a_linked_window(tmp_path, tmuxd):
    # A window linked in from another session carries keys to that session as well.
    proj = tmp_path / "proj"
    proj.mkdir()
    tmux(tmp_path, "new-session", "-d", "-s", "victim", "cat")
    name = session_of(cc(tmp_path, str(proj), "--no-window"))
    tmux(tmp_path, "link-window", "-s", "=victim:0", "-t", "=%s:9" % name)
    tmux(tmp_path, "select-window", "-t", "=%s:9" % name)
    victim_pane = tmux(tmp_path, "display-message", "-p", "-t", "=victim:0", "#{pane_id}").stdout.strip()
    tmux(tmp_path, "set-option", "-t", "=%s:" % name, "@qwen_cc_pane", victim_pane)
    r = cc(tmp_path, "--say", name, "hello")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "hello" not in tmux(tmp_path, "capture-pane", "-p", "-t", victim_pane).stdout
