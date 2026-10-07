"""qwen-agent's browser switches, offline: --browser, --headed, --browser-eval and the
tester role. The fake claude, the model server and the runner are test_cli_deep's (the
fake records every call's argv NUL-separated), and the MCP config is read back from the
--mcp-config file the fake saw -- exactly the file a real claude would have loaded.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

import test_cli_deep
from test_cli import _git_repo, flag, posix, run, same_path
from test_cli_deep import calls, go, sys_prompt

# the fake claude of test_cli_deep (records argv per call) and its server/fake fixtures
server = test_cli_deep.server
fake = test_cli_deep.fake

BROWSER_TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot",
                 "browser_click", "browser_type", "browser_fill_form", "browser_press_key",
                 "browser_select_option", "browser_hover", "browser_drag", "browser_file_upload",
                 "browser_handle_dialog", "browser_tabs", "browser_resize", "browser_wait_for",
                 "browser_take_screenshot", "browser_console_messages",
                 "browser_network_requests", "browser_close"]


def bdir(tmp_path):
    """env pinning the browser run folders under this test's tmp dir."""
    return {"QWEN_BROWSER_DIR": posix(tmp_path / "browser")}


def mcp_entry(argv):
    """(config path, playwright server entry) of the one MCP config the run passed."""
    cfg = flag(argv, "--mcp-config")
    assert cfg, "no --mcp-config in argv: %s" % argv
    servers = json.loads(pathlib.Path(cfg).read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["playwright"]
    return cfg, servers["playwright"]


def out_dir(entry):
    args = entry["args"]
    return args[args.index("--output-dir") + 1]


# ------------------------------------------------------------------ the config

def test_browser_writes_one_playwright_server_and_passes_it(tmp_path, server, fake):
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert "--strict-mcp-config" in argv                    # no other MCP server loads
    cfg, entry = mcp_entry(argv)
    assert entry["command"] == "npx"
    assert entry["args"][:3] == ["-y", "--prefer-offline", "@playwright/mcp@0.0.83"]
    assert "--headless" in entry["args"] and "--isolated" in entry["args"]
    d = out_dir(entry)
    assert same_path(d).startswith(same_path(tmp_path / "browser"))
    assert pathlib.Path(d).is_dir()                         # kept after the run: the evidence
    assert same_path(pathlib.Path(cfg).parent) == same_path(d)   # the config lives in it
    assert "browser: screenshots and page snapshots in %s" % posix(d) in posix(r.stderr)


def test_git_bash_default_uses_cmd_c(tmp_path, server, fake):
    # Native Windows Claude Code cannot spawn npx directly as a stdio MCP server
    # (it is npx.cmd): on Git Bash -- cygpath on PATH -- with no QWEN_PLAYWRIGHT_MCP
    # the config must start the server through the cmd /c wrapper.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cyg = bindir / "cygpath"
    cyg.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"${@: -1}\"\n",
                   encoding="utf-8", newline="\n")   # prints its last argument
    cyg.chmod(0o755)
    extra = dict(bdir(tmp_path),
                 PATH="%s%s%s" % (posix(bindir), os.pathsep, os.environ["PATH"]))
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    cfg, entry = mcp_entry(argv)
    assert pathlib.Path(cfg).is_file()
    assert entry["command"] == "cmd"
    assert entry["args"][:5] == ["/c", "npx", "-y", "--prefer-offline",
                                 "@playwright/mcp@0.0.83"]
    assert "--headless" in entry["args"] and "--isolated" in entry["args"]


# ------------------------------------------------------------------ the grants

def test_browser_grants_the_tools_in_one_list(tmp_path, server, fake):
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert argv.count("--allowed-tools") == 1               # one list, never a second -t
    grants = flag(argv, "--allowed-tools").split(",")
    for t in BROWSER_TOOLS:
        assert "mcp__playwright__%s" % t in grants, t
    assert "mcp__playwright__browser_evaluate" not in grants
    # the run's toolset stays: the read-only default grants are still in the list
    assert "Read" in grants and "Glob" in grants and "Grep" in grants


def test_browser_grants_find_and_friends(tmp_path, server, fake):
    # The server offers these tools; they join the one list.
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert argv.count("--allowed-tools") == 1
    grants = flag(argv, "--allowed-tools").split(",")
    for t in ["browser_find", "browser_drop", "browser_emulate_media"]:
        assert "mcp__playwright__%s" % t in grants, t


def test_browser_hides_unsafe_tools(tmp_path, server, fake):
    # Offered by the server but never granted: a tool that is only ungranted still
    # shows in the model's tool list, so it is disallowed out of sight as well.
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert argv.count("--disallowedTools") == 1             # one value, never a second flag
    hidden = flag(argv, "--disallowedTools").split(",")
    for t in ["mcp__playwright__browser_run_code_unsafe", "mcp__playwright__browser_install",
              "mcp__playwright__browser_evaluate"]:
        assert t in hidden, t
    grants = flag(argv, "--allowed-tools").split(",")
    for t in hidden:
        assert t not in grants, t                           # hidden and never granted

    # --browser-eval: evaluate joins the grants and leaves the hidden list; the other
    # two stay hidden, and the flag is still passed exactly once.
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--browser-eval", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert argv.count("--disallowedTools") == 1
    hidden = flag(argv, "--disallowedTools").split(",")
    assert "mcp__playwright__browser_evaluate" not in hidden
    assert "mcp__playwright__browser_run_code_unsafe" in hidden
    assert "mcp__playwright__browser_install" in hidden
    grants = flag(argv, "--allowed-tools").split(",")
    assert "mcp__playwright__browser_evaluate" in grants


def test_network_request_body_hidden_by_default(tmp_path, server, fake):
    # browser_network_request answers ONE request WITH its response body -- the
    # app's scripts, styles and server replies, i.e. its source, which a black-box
    # tester must not read (seen in use: a tester quoted app.js fetched with it).
    # So it is hidden by default; the request LIST (URLs, methods, statuses, no
    # bodies) stays granted.
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    hidden = flag(argv, "--disallowedTools").split(",")
    grants = flag(argv, "--allowed-tools").split(",")
    assert "mcp__playwright__browser_network_request" in hidden
    assert "mcp__playwright__browser_network_request" not in grants
    assert "mcp__playwright__browser_network_requests" in grants
    assert "mcp__playwright__browser_network_requests" not in hidden


def test_browser_eval_opts_into_network_request(tmp_path, server, fake):
    # --browser-eval opts into both source-reaching tools, browser_evaluate and
    # browser_network_request -- both granted, neither hidden.
    r = go(tmp_path, ["--browser-eval", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    grants = flag(argv, "--allowed-tools").split(",")
    hidden = flag(argv, "--disallowedTools").split(",")
    for t in ["mcp__playwright__browser_network_request", "mcp__playwright__browser_evaluate"]:
        assert t in grants, t
        assert t not in hidden, t


def test_browser_eval_adds_evaluate(tmp_path, server, fake):
    r = go(tmp_path, ["--browser-eval", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    grants = flag(argv, "--allowed-tools").split(",")
    assert "mcp__playwright__browser_evaluate" in grants
    assert "mcp__playwright__browser_navigate" in grants    # the whole set still comes with it
    assert "--strict-mcp-config" in argv                    # --browser-eval implies --browser
    mcp_entry(argv)


def test_browser_with_test_warns(tmp_path, server, fake):
    # The browser opens any URL, internet included, independently of --web -- so a
    # --test run with a browser can browse the upstream answers its tests and checks
    # are supposed to derive. It proceeds, but says so, in the --web warning's style.
    repo = _git_repo(tmp_path / "repo")
    extra = dict(bdir(tmp_path), QWEN_TEST_CMD="true",
                 QWEN_TEST_WORKTREES=posix(tmp_path / "wts"))
    r = go(tmp_path, ["--test", "--browser", "-C", posix(repo), "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    # the LINE starts with the flag pair, not "qwen-agent: WARNING: ..."
    assert any(ln.startswith("WARNING: --browser with --test") for ln in r.stderr.splitlines())
    assert "tests and checks can be gamed by browsing upstream answers" in r.stderr
    # without --test there is no warning (nothing to game), browser still on
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "--browser with --test" not in r.stderr
    argv, _ = calls(tmp_path)[0]
    assert "--strict-mcp-config" in argv


# --------------------------------------------------- the folder is readable

def test_browser_folder_is_readable_with_toolset_none(tmp_path, server, fake):
    # 0.0.83 answers a screenshot GIVEN a filename with a LINK only: the image is
    # the file in the run folder. A session with no built-in tool at all must
    # still be able to open it -- the folder joins --add-dir and Read joins the
    # grants, Read alone.
    r = go(tmp_path, ["--browser", "--toolset", "none", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    adds = [same_path(argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    assert same_path(out_dir(entry)) in adds, adds
    grants = flag(argv, "--allowed-tools").split(",")
    assert "Read" in grants
    for t in ("Glob", "Grep", "Bash", "Write"):
        assert t not in grants, t                            # Read ONLY, nothing wider


def test_browser_add_dir_does_not_trip_probe_refusal(tmp_path, server, fake):
    # The browser run folder is not the user's tree: --browser's own --add-dir
    # must not fire --probe's -D/--add-dir refusal.
    repo = _git_repo(tmp_path / "repo")
    r = go(tmp_path, ["--browser", "--probe", "-r", "auditor", "-C", posix(repo), "hi"],
           server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "-D/--add-dir" not in r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    adds = [same_path(argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    assert same_path(out_dir(entry)) in adds, adds


# ------------------------------------------------------------------ --headed

def test_headed_drops_headless_and_passes_display(tmp_path, server, fake):
    extra = dict(bdir(tmp_path), DISPLAY=":9", XAUTHORITY="/tmp/xauth.1")
    r = go(tmp_path, ["--headed", "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    assert "--headless" not in entry["args"]
    assert entry["env"]["DISPLAY"] == ":9"
    assert entry["env"]["XAUTHORITY"] == "/tmp/xauth.1"


def test_headed_on_wayland_passes_wayland_env(tmp_path, server, fake):
    # No X here: the Wayland socket is what the browser needs, and an empty DISPLAY
    # key would only send the server looking for an X display.
    extra = dict(bdir(tmp_path), DISPLAY="", WAYLAND_DISPLAY="wayland-1",
                 XDG_RUNTIME_DIR="/run/x")
    r = go(tmp_path, ["--headed", "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    assert "--headless" not in entry["args"]
    assert entry["env"]["WAYLAND_DISPLAY"] == "wayland-1"
    assert entry["env"]["XDG_RUNTIME_DIR"] == "/run/x"
    assert "DISPLAY" not in entry["env"]
    assert "XAUTHORITY" not in entry["env"]


def test_headed_without_display_is_refused(tmp_path, server, fake):
    extra = dict(bdir(tmp_path), DISPLAY="", WAYLAND_DISPLAY="")
    r = go(tmp_path, ["--headed", "hi"], server, fake, extra=extra)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--headed needs a display (DISPLAY is not set)" in r.stderr
    assert calls(tmp_path) == []                            # claude never ran
    assert not (tmp_path / "browser").exists()              # and nothing was created


# ------------------------------------------------------------------ refusals

@pytest.mark.parametrize("args,needle", [
    (["--browser", "--mcp-config", "mcp.json", "hi"], "--browser brings its own MCP config"),
    (["--browser", "--until-done", "t.md"], "--until-done"),
    (["--browser", "--interactive"], "--interactive"),
    (["--browser=1", "hi"], "option --browser takes no value (got '--browser=1')"),
    (["--headed=1", "hi"], "option --headed takes no value (got '--headed=1')"),
    (["--browser-eval=1", "hi"], "option --browser-eval takes no value (got '--browser-eval=1')"),
], ids=["mcp-config", "until-done", "interactive", "browser-value", "headed-value", "eval-value"])
def test_browser_refusals(tmp_path, server, fake, args, needle):
    (tmp_path / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    r = go(tmp_path, args, server, fake, extra=bdir(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert calls(tmp_path) == []                            # claude never ran
    assert not (tmp_path / "browser").exists()              # and no run folder was made


# ------------------------------------------------- the folder only for real runs

def test_no_browser_folder_on_early_failure(tmp_path, server, fake):
    # The run folder is made only after every refusal/validation/preflight, just
    # before claude starts: a run that dies early and a --dry-run leave the browser
    # root with no entries.
    def entries():
        d = tmp_path / "browser"
        return [] if not d.exists() else list(d.iterdir())

    r = go(tmp_path, ["--browser", "-o", "/nonexistent/dir/x.md", "hi"], server, fake,
           extra=bdir(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr      # --out: no such directory
    assert entries() == []
    assert calls(tmp_path) == []                       # claude never ran

    r = go(tmp_path, ["--browser", "--dry-run", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    assert entries() == []                             # nothing created for a dry run
    assert calls(tmp_path) == []
    # the would-be config path is shown as a placeholder, folder and all
    assert "[<browser run folder>/mcp.json]" in r.stdout
    assert "--strict-mcp-config" in r.stdout
    assert "browser: screenshots" not in r.stderr


# ------------------------------------------------------------------ the role

def test_tester_role_implies_browser_and_has_its_text(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "tester", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = " ".join(sys_prompt(argv).split())                  # whitespace-insensitive read
    assert "BROWSER TESTER" in p
    assert "accessibility snapshot" in p
    assert "steps to reproduce, expected, actual" in p
    assert "Only report what you observed in the browser" in p
    assert "Test as a user would, from the outside" in p
    assert "--strict-mcp-config" in argv                    # the role implies --browser
    _, entry = mcp_entry(argv)
    assert pathlib.Path(out_dir(entry)).is_dir()
    assert "mcp__playwright__browser_navigate" in flag(argv, "--allowed-tools").split(",")
    # tester has no role variants: the existing unknown-variant usage error
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["-r", "tester", "--role-variant", "deep", "hi"], server, fake,
           extra=bdir(tmp_path))
    assert r.returncode == 2 and "has no 'deep' variant" in r.stderr
    assert calls(tmp_path) == []


def test_tester_text_is_black_box(tmp_path, server, fake):
    # Seen in use: a tester fetched app.js with browser_network_request and quoted
    # source lines. A tester tests behaviour from the outside -- the role has to say
    # so outright, and make clear no verdict may rest on reading code.
    r = go(tmp_path, ["-r", "tester", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = " ".join(sys_prompt(argv).split())                  # whitespace-insensitive read
    assert "do not open, fetch or quote the application's source code" in p
    assert ("Test as a user would, from the outside: do not open, fetch or quote the "
            "application's source code (scripts, styles, server files) unless the task "
            "asks you to, and never base a verdict on reading code instead of on what "
            "the page does.") in p


def test_tester_text_asks_for_sequences(tmp_path, server, fake):
    # A screen that looks right after one click can still leave the wrong state: the
    # role has to ask for a run of actions and a check of the state they leave behind.
    r = go(tmp_path, ["-r", "tester", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = " ".join(sys_prompt(argv).split())                  # whitespace-insensitive read
    assert "chain actions into sequences" in p
    assert ("empty, invalid and repeated input; chain actions into sequences (do something, "
            "reset or undo it, then do it again) and check that the state after the sequence "
            "is what a user would expect, not only what the screen shows right after each "
            "click; after each action take a snapshot") in p


def test_tester_text_screenshot_without_filename(tmp_path, server, fake):
    # 0.0.83 returns the image only for a screenshot taken WITHOUT a filename, and
    # a saved one is worth re-checking at a phone width and in an alternate theme:
    # the role has to say so.
    r = go(tmp_path, ["-r", "tester", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = " ".join(sys_prompt(argv).split())                  # whitespace-insensitive read
    assert "WITHOUT a filename" in p
    assert "browser_resize to 400x800" in p
    assert ("call browser_take_screenshot WITHOUT a filename so the image comes back "
            "to you; a filename only saves the file") in p
    assert "and repeat a key flow with any dark or alternate theme the app offers" in p


def test_unknown_role_lists_tester(tmp_path, server, fake):
    # tester is a built-in, so the built-in list printed for an unknown role and by
    # --list-roles names it too.
    r = go(tmp_path, ["-r", "nosuch", "hi"], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert calls(tmp_path) == []                       # claude never ran
    assert "built-in: auditor, coder, mechanic, plain, tester" in r.stderr
    r = run(tmp_path, ["--list-roles"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "built-in: auditor, coder, mechanic, plain, tester"


# ---------------------------------------------------- the run folder's location

def test_browser_dir_is_never_the_callers_dir(tmp_path, server, fake):
    # The caller stands INSIDE a git repo and names no QWEN_BROWSER_DIR: every byte
    # the run leaves behind must land under the cache dir, never in the tree.
    repo = _git_repo(tmp_path / "repo")
    cache = tmp_path / "cache"
    r = run(tmp_path, ["--browser", "-C", posix(repo), "hi"], server, fake, cwd=str(repo),
            extra={"XDG_CACHE_HOME": posix(cache), "FAKE_DIR": posix(tmp_path / "calls")})
    assert r.returncode == 0, r.stdout + r.stderr
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, encoding="utf-8")
    assert st.stdout == "", st.stdout                       # the repo is as clean as it was
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    d = out_dir(entry)
    assert same_path(d).startswith(same_path(cache / "qwen-agent" / "browser"))
    assert not same_path(d).startswith(same_path(repo))


# ------------------------------------------------------------------ the override

def test_playwright_mcp_override(tmp_path, server, fake):
    extra = dict(bdir(tmp_path), QWEN_PLAYWRIGHT_MCP="node /x/cli.js")
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    assert entry["command"] == "node"
    assert entry["args"][0] == "/x/cli.js"                  # the rest of the command leads the args
    assert "@playwright/mcp@0.0.83" not in entry["args"]
    assert "--isolated" in entry["args"]                    # the browser switches still follow


def test_playwright_mcp_override_is_not_globbed(tmp_path, server, fake):
    # The override splits on whitespace WITHOUT pathname expansion: files matching
    # the pattern must not replace the pattern in the server's argv.
    (tmp_path / "x").mkdir()
    for name in ("a.js", "b.js"):
        (tmp_path / "x" / name).write_text("// stub\n", encoding="utf-8")
    pattern = "%s/*.js" % posix(tmp_path / "x")
    extra = dict(bdir(tmp_path), QWEN_PLAYWRIGHT_MCP="node %s" % pattern)
    r = go(tmp_path, ["--browser", "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    assert entry["command"] == "node"
    assert entry["args"][0] == pattern                  # the literal pattern itself
    for name in ("a.js", "b.js"):
        assert posix(tmp_path / "x" / name) not in entry["args"]
    assert "--isolated" in entry["args"]                # the browser switches still follow


# ------------------------------------------------------------------ the JSON record

def test_browser_json_record(tmp_path, server, fake):
    r = go(tmp_path, ["--browser", "--json", "hi"], server, fake, extra=bdir(tmp_path))
    assert r.returncode == 0, r.stderr
    meta = json.loads(r.stdout)["qwen_agent"]               # the key is created for --browser
    assert meta["browser"]["headed"] is False
    assert meta["browser"]["eval"] is False
    argv, _ = calls(tmp_path)[0]
    _, entry = mcp_entry(argv)
    assert same_path(meta["browser"]["dir"]) == same_path(out_dir(entry))
    assert same_path(meta["browser"]["dir"]).startswith(same_path(tmp_path / "browser"))


# --------------------------------------------------- the existing roles unchanged

def test_existing_role_texts_unchanged(tmp_path, server, fake):
    # the sha256 pins of test_cli_deep, run as that test runs them
    test_cli_deep.test_existing_role_texts_are_unchanged(tmp_path, server, fake)
