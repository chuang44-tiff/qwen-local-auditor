"""qwen-agent --desktop APP, offline: the one-command Bash fence, the run folder and its
qla-desktop wrapper, and the refusals. QWEN_DESKTOP_DRIVER points the wrapper at a fake
driver, so no display is needed; the real driver is test_desktop's."""
import os
import pathlib
import shlex

import pytest

import test_cli_deep
from test_cli import flag, posix, same_path
from test_cli_deep import calls, go, sys_prompt

server = test_cli_deep.server
fake = test_cli_deep.fake

FAKE_DRIVER = r'''import json, sys
args = sys.argv[1:]
if "check" in args and "--fail" in open(__file__ + ".mode").read():
    print(json.dumps({"ok": False, "error": "no visible window of 'gedit'"})); sys.exit(1)
print(json.dumps({"ok": True, "argv": args}))
'''


@pytest.fixture
def driver(tmp_path):
    p = tmp_path / "fake_driver.py"
    p.write_text(FAKE_DRIVER, encoding="utf-8")
    (tmp_path / "fake_driver.py.mode").write_text("ok", encoding="utf-8")
    return p


def env(tmp_path, driver):
    return {"QWEN_DESKTOP_DIR": posix(tmp_path / "desktop"), "QWEN_DESKTOP_DRIVER": posix(driver)}


def folders(tmp_path):
    d = tmp_path / "desktop"
    return [] if not d.exists() else sorted(d.iterdir())


def add_dirs(argv):
    """Every --add-dir value the session was given (flag() only ever sees the first)."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def test_bash_is_granted_for_qla_desktop_only(tmp_path, server, fake, driver):
    r = go(tmp_path, ["--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    argv = calls(tmp_path)[0][0]
    tools = flag(argv, "--tools").split(",")
    grants = flag(argv, "--allowed-tools").split(",")
    assert tools.count("Bash") == 1
    assert [g for g in grants if g.startswith("Bash")] == ["Bash(qla-desktop:*)"]
    assert "Edit" not in tools and "Write" not in tools              # read-only by default
    note = sys_prompt(argv)
    assert "driving the desktop application 'gedit'" in note
    assert "resize 1280 800" in note and "never maximize" in note and "restore" in note
    assert "crop NAME X Y W H 3 grid" in note and "unreadable" in note
    assert "desktop: real mouse and keyboard input to 'gedit'" in r.stderr


def test_the_run_folder_holds_a_locked_wrapper(tmp_path, server, fake, driver):
    r = go(tmp_path, ["-q", "--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    [d] = folders(tmp_path)                                          # kept after the run
    wrapper = d / "bin" / "qla-desktop"
    text = wrapper.read_text(encoding="utf-8")
    assert "--app gedit" in text and "--lock" in text and text.rstrip().endswith('"$@"')
    assert os.access(str(wrapper), os.X_OK)
    argv = calls(tmp_path)[0][0]
    adds = [same_path(a) for a in add_dirs(argv)]
    shots = same_path(str(d / "shots"))
    assert adds == [shots]                                           # screenshots are readable, nothing else
    binp = same_path(str(d / "bin"))
    assert not any(binp == a or binp.startswith(a.rstrip("/") + "/") for a in adds)
    assert "desktop: real mouse" in r.stderr                         # even with -q
    p = posix(str(d / "shots"))
    # Compare spellings, not paths: on Windows the note holds C:\...\shots while p is C:/.../shots.
    assert p in posix(sys_prompt(argv)) and p in posix(r.stderr)   # the note and the startup line name shots/


def test_the_wrapper_is_outside_every_readable_dir(tmp_path, server, fake, driver):
    r = go(tmp_path, ["-q", "--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    [d] = folders(tmp_path)
    wrapper = d / "bin" / "qla-desktop"
    argv = calls(tmp_path)[0][0]
    w = same_path(str(wrapper))
    for a in (same_path(x) for x in add_dirs(argv)):     # a --write run edits inside these dirs;
        assert not w.startswith(a.rstrip("/") + "/"), (w, a)   # it must not reach the wrapper
    assert not os.access(str(wrapper), os.W_OK)          # and it is read-only even to its owner
    # The folder holding it too -- but not on Windows, where a directory's read-only
    # flag does not stop writes there (the wrapper file itself stays read-only, and
    # the session is given write access to shots/ only).
    if os.name != "nt":
        assert not os.access(str(wrapper.parent), os.W_OK)


def test_wrapper_uses_native_paths_and_absolute_python(tmp_path, server, fake, driver):
    # MSYS2_ARG_CONV_EXCL is set for the child, so nothing converts POSIX paths for a
    # native python.exe: the wrapper must carry the native spelling and an absolute interpreter.
    r = go(tmp_path, ["-q", "--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    [d] = folders(tmp_path)
    text = (d / "bin" / "qla-desktop").read_text(encoding="utf-8")
    words = shlex.split([ln for ln in text.splitlines() if ln.startswith("exec ")][0])
    assert words[0] == "exec" and words[-1] == "$@"
    assert os.path.isabs(words[1]) and not words[1].endswith(".py")   # the interpreter itself
    assert same_path(words[2]) == same_path(str(driver))              # the driver, native_path'd
    assert same_path(words[words.index("--dir") + 1]) == same_path(str(d / "shots"))


def test_the_wrapper_is_first_on_the_sessions_path(tmp_path, server, fake, driver):
    # The fake claude runs what a session would: `qla-desktop windows` through PATH.
    sh = tmp_path / "fake-claude"
    sh.write_text('#!/usr/bin/env bash\n'
                  'if [ "${1:-}" = --help ]; then exit 0; fi\n'
                  'qla-desktop windows > "%s" 2>&1\n'
                  'printf \'{"type":"result","subtype":"success","is_error":false,"num_turns":1,'
                  '"result":"ok","session_id":"s1","usage":{"input_tokens":1,"output_tokens":1},'
                  '"permission_denials":[]}\\n\'\n' % posix(tmp_path / "seen.json"),
                  encoding="utf-8", newline="\n")
    sh.chmod(0o755)
    r = go(tmp_path, ["--shallow", "--desktop", "gedit", "hi"], server, sh, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    seen = (tmp_path / "seen.json").read_text(encoding="utf-8")
    assert '"ok": true' in seen
    assert '"--app", "gedit"' in seen and '"--lock", "windows"' in seen


FAKE_CYGPATH = r'''#!/usr/bin/env bash
# cygpath stand-in: -u turns a drive path X:/a/b into /x/a/b and -w/-m turn the Git
# Bash mount spelling /x/a/b back into X:/a/b, as the real tool does (so the native
# python in the wrapper still gets paths it can read); any other path prints unchanged.
p="${@: -1}"
case "${1:-}" in
  -u) case "$p" in
        [A-Za-z]:/*) printf '/%s%s\n' "$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')" "${p#?:}" ;;
        *) printf '%s\n' "$p" ;;
      esac ;;
  -w|-m) case "$p" in
        /?/*) printf '%s:%s\n' "$(printf '%s' "${p:1:1}" | tr 'a-z' 'A-Z')" "${p:2}" ;;
        *) printf '%s\n' "$p" ;;
      esac ;;
  *) printf '%s\n' "$p" ;;
esac
'''

FAKE_CYGPATH_U = r'''#!/usr/bin/env bash
# cygpath stand-in whose -u answer is visibly NOT its argument: a drive path X:/a/b
# answers /x/a/b (the Git Bash mount form, which is real there) and any other path
# answers /fakeu<a> -- so on Linux the PATH entry can still be pinned to the -u
# answer. -w/-m map the mount spelling back, as in FAKE_CYGPATH.
p="${@: -1}"
case "${1:-}" in
  -u) case "$p" in
        [A-Za-z]:/*) printf '/%s%s\n' "$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')" "${p#?:}" ;;
        *) printf '/fakeu%s\n' "$p" ;;
      esac ;;
  -w|-m) case "$p" in
        /?/*) printf '%s:%s\n' "$(printf '%s' "${p:1:1}" | tr 'a-z' 'A-Z')" "${p:2}" ;;
        *) printf '%s\n' "$p" ;;
      esac ;;
  *) printf '%s\n' "$p" ;;
esac
'''

FAKE_ANSWER = ('printf \'{"type":"result","subtype":"success","is_error":false,"num_turns":1,'
               '"result":"ok","session_id":"s1","usage":{"input_tokens":1,"output_tokens":1},'
               '"permission_denials":[]}\\n\'\n')


def test_wrapper_bin_goes_on_path_in_posix_form(tmp_path, server, driver):
    # Git Bash: DESKTOP_DIR can be a drive path (C:/...), and bash splits PATH on ':'
    # -- the raw entry would be two bogus folders and the session's Bash would say
    # "qla-desktop: command not found". What joins PATH is the `cygpath -u` form.
    cyg = tmp_path / "cygbin"
    cyg.mkdir()
    c = cyg / "cygpath"
    c.write_text(FAKE_CYGPATH, encoding="utf-8", newline="\n")
    c.chmod(0o755)
    with_cygpath = {"PATH": "%s%s%s" % (posix(cyg), os.pathsep, os.environ["PATH"])}

    # End to end: with a cygpath on PATH the session still finds the wrapper (on
    # Linux its own bin/ is POSIX and the fake passes it through unchanged).
    sh = tmp_path / "fake-claude"
    sh.write_text('#!/usr/bin/env bash\n'
                  'if [ "${1:-}" = --help ]; then exit 0; fi\n'
                  'qla-desktop windows > "%s" 2>&1\n' % posix(tmp_path / "seen.json")
                  + FAKE_ANSWER,
                  encoding="utf-8", newline="\n")
    sh.chmod(0o755)
    r = go(tmp_path, ["--shallow", "--desktop", "gedit", "hi"], server, sh,
           extra=dict(env(tmp_path, driver), **with_cygpath))
    assert r.returncode == 0, r.stderr
    seen = (tmp_path / "seen.json").read_text(encoding="utf-8")
    assert '"ok": true' in seen and '"--app", "gedit"' in seen   # the wrapper ran through PATH

    # Unit level: the exported PATH entry is exactly the `cygpath -u` answer for
    # bin/, not the raw DESKTOP_DIR entry. The drive form X:/... is not usable as a
    # root on Linux, so this run keeps a real, colon-free POSIX root and the fake
    # makes -u answer visibly differently from its argument (/fakeu...; on Git Bash
    # it answers the mount form -- the entry that is real, and where the bug was).
    cyg2 = tmp_path / "cygbin-u"
    cyg2.mkdir()
    c2 = cyg2 / "cygpath"
    c2.write_text(FAKE_CYGPATH_U, encoding="utf-8", newline="\n")
    c2.chmod(0o755)
    sh = tmp_path / "records-path"
    sh.write_text('#!/usr/bin/env bash\n'
                  'if [ "${1:-}" = --help ]; then exit 0; fi\n'
                  'printf \'%s\' "$PATH" > "%s"\n' % ("%s", posix(tmp_path / "path.txt"))
                  + FAKE_ANSWER,
                  encoding="utf-8", newline="\n")
    sh.chmod(0o755)
    r = go(tmp_path, ["--shallow", "--desktop", "gedit", "hi"], server, sh,
           extra={"QWEN_DESKTOP_DIR": posix(tmp_path / "desktop2"),
                  "QWEN_DESKTOP_DRIVER": posix(driver),
                  "PATH": "%s%s%s" % (posix(cyg2), os.pathsep, os.environ["PATH"])})
    assert r.returncode == 0, r.stderr
    [d2] = sorted((tmp_path / "desktop2").iterdir())
    b = posix(d2 / "bin")                       # bin/ in the run's own spelling
    first = (tmp_path / "path.txt").read_text(encoding="utf-8").split(":")[0]
    assert first == ("/" + b[0].lower() + b[2:] if os.name == "nt" else "/fakeu" + b)


def test_write_adds_the_edit_tools_not_more_shell(tmp_path, server, fake, driver):
    r = go(tmp_path, ["--write", "--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    argv = calls(tmp_path)[0][0]
    assert "Edit" in flag(argv, "--tools").split(",")
    assert [g for g in flag(argv, "--allowed-tools").split(",") if g.startswith("Bash")] == ["Bash(qla-desktop:*)"]


def test_default_depth_brings_no_probe_shell(tmp_path, server, fake, driver):
    repo = test_cli_deep.dirty_repo(tmp_path)
    r = go(tmp_path, ["--desktop", "gedit", "-C", posix(repo), "hi"], server, fake,
           extra=dict(env(tmp_path, driver), QWEN_DEPTH=""))
    assert r.returncode == 0, r.stderr
    argv = calls(tmp_path)[0][0]
    assert "--probe" not in r.stderr and "full shell" not in sys_prompt(argv)
    assert [g for g in flag(argv, "--allowed-tools").split(",") if g.startswith("Bash")] == ["Bash(qla-desktop:*)"]


def test_a_failed_check_stops_the_run_and_leaves_no_folder(tmp_path, server, fake, driver):
    (tmp_path / "fake_driver.py.mode").write_text("--fail", encoding="utf-8")
    r = go(tmp_path, ["--desktop", "gedit", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no visible window of 'gedit'" in r.stderr
    assert calls(tmp_path) == [] and folders(tmp_path) == []


def test_dry_run_makes_nothing(tmp_path, server, fake, driver):
    r = go(tmp_path, ["--desktop", "gedit", "--dry-run", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    assert "Bash(qla-desktop:*)" in r.stdout and "<desktop run folder>" in r.stdout
    assert folders(tmp_path) == [] and calls(tmp_path) == []


@pytest.mark.parametrize("args,needle", [
    (["--browser"], "--browser"),
    (["-r", "tester"], "--browser"),
    (["--toolset", "Read,Bash"], "--toolset"),
    (["--read-only"], "--toolset"),
    (["--all-tools"], "--all-tools"),
    (["--probe"], "--probe"),
    (["--until-done", "t.md"], "--until-done"),
], ids=["browser", "tester", "toolset", "read-only", "all-tools", "probe", "until-done"])
def test_refusals(tmp_path, server, fake, driver, args, needle):
    (tmp_path / "t.md").write_text("# task\n", encoding="utf-8")
    r = go(tmp_path, ["--desktop", "gedit"] + args + (["hi"] if "--until-done" not in args else []),
           server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--desktop cannot be combined with %s" % needle in r.stderr
    assert calls(tmp_path) == [] and folders(tmp_path) == []


@pytest.mark.parametrize("grant", ["Bash", "Read,Bash", "Bash(ls -la)", "Edit,Bash(qla-desktop:*)"],
                         ids=["bare", "mixed", "with-rule", "redundant-rule"])
def test_tools_granting_bash_are_refused(tmp_path, server, fake, driver, grant):
    r = go(tmp_path, ["--desktop", "gedit", "-t", grant, "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--desktop cannot be combined with -t Bash" in r.stderr
    assert calls(tmp_path) == [] and folders(tmp_path) == []


def test_tools_without_bash_stay_allowed(tmp_path, server, fake, driver):
    r = go(tmp_path, ["--desktop", "gedit", "-t", "Read,WebFetch", "hi"], server, fake,
           extra=env(tmp_path, driver))
    assert r.returncode == 0, r.stderr
    argv = calls(tmp_path)[0][0]
    grants = flag(argv, "--allowed-tools").split(",")
    assert [g for g in grants if g.startswith("Bash")] == ["Bash(qla-desktop:*)"]
    assert "Read" in grants and "WebFetch" in grants


def test_permission_mode_and_mcp_config_are_refused(tmp_path, server, fake, driver):
    (tmp_path / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    for flag_ in ("--permission-mode", "--mcp-config"):
        value = "acceptEdits" if flag_ == "--permission-mode" else posix(tmp_path / "mcp.json")
        r = go(tmp_path, ["--desktop", "gedit", flag_, value, "hi"], server, fake,
               extra=env(tmp_path, driver))
        assert r.returncode == 2, (flag_, r.stdout + r.stderr)
        assert "--desktop cannot be combined with %s" % flag_ in r.stderr
        assert calls(tmp_path) == [] and folders(tmp_path) == []


def test_interactive_and_bad_names_are_refused(tmp_path, server, fake, driver):
    r = go(tmp_path, ["--interactive", "--desktop", "gedit"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2 and "--interactive cannot be combined with --desktop" in r.stderr
    r = go(tmp_path, ["--desktop", "a'; rm -rf x", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2 and "not a process name" in r.stderr
    r = go(tmp_path, ["--desktop", "", "hi"], server, fake, extra=env(tmp_path, driver))
    assert r.returncode == 2
    assert not pathlib.Path(tmp_path / "desktop").exists()
