"""lib/browser_mcp.py: the Playwright MCP server entry qwen-agent --browser and
wf.claude_check(browser=True) share. Golden values are what qwen-agent.sh's inline
heredoc wrote before the module existed (advisor db92159), for Linux and for a forced
Windows (os.name == "nt") branch."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from lib import browser_mcp

SA = pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"
SWITCHES = ["--isolated", "--output-dir", "/r/b/x", "--image-responses", "allow",
            "--viewport-size", "1280,900"]
NPX = ["-y", "--prefer-offline", "@playwright/mcp@0.0.83"]
NO_CYGPATH = {"PATH": "/nonexistent-bin"}


@pytest.fixture
def posix_host(monkeypatch):
    monkeypatch.setattr(browser_mcp, "OS_NAME", "posix")


@pytest.fixture
def nt_host(monkeypatch):
    monkeypatch.setattr(browser_mcp, "OS_NAME", "nt")


def test_linux_default_is_todays_entry(posix_host):
    assert browser_mcp.build("/r/b/x", env=NO_CYGPATH) == {"mcpServers": {"playwright": {
        "command": "npx", "args": NPX + ["--headless"] + SWITCHES}}}


def test_forced_nt_starts_npx_through_cmd_c(nt_host):
    assert browser_mcp.build("/r/b/x", env=NO_CYGPATH) == {"mcpServers": {"playwright": {
        "command": "cmd", "args": ["/c", "npx"] + NPX + ["--headless"] + SWITCHES}}}


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX stand-in for cygpath")
def test_cygpath_on_path_means_git_bash(posix_host, tmp_path):
    cyg = tmp_path / "cygpath"
    cyg.write_text("#!/bin/sh\n", encoding="utf-8")
    cyg.chmod(0o755)
    entry = browser_mcp.build("/r/b/x", env={"PATH": str(tmp_path)})["mcpServers"]["playwright"]
    assert entry["command"] == "cmd" and entry["args"][:2] == ["/c", "npx"]


@pytest.mark.parametrize("host", ["posix", "nt"])
def test_override_replaces_the_command_on_every_host(monkeypatch, host):
    monkeypatch.setattr(browser_mcp, "OS_NAME", host)
    env = dict(NO_CYGPATH, QWEN_PLAYWRIGHT_MCP="  node\t/x/*.js \n --flag ")
    assert browser_mcp.build("/r/b/x", env=env) == {"mcpServers": {"playwright": {
        "command": "node", "args": ["/x/*.js", "--flag", "--headless"] + SWITCHES}}}


def test_blank_override_is_the_default(posix_host):
    env = dict(NO_CYGPATH, QWEN_PLAYWRIGHT_MCP=" \t\r\n")
    assert browser_mcp.build("/r/b/x", env=env)["mcpServers"]["playwright"]["command"] == "npx"


def test_headed_copies_the_x_display(posix_host):
    env = dict(NO_CYGPATH, DISPLAY=":9", XAUTHORITY="/tmp/xauth.1", WAYLAND_DISPLAY="w-1")
    assert browser_mcp.build("/r/b/x", headed=True, env=env) == {"mcpServers": {"playwright": {
        "command": "npx", "args": NPX + SWITCHES,
        "env": {"DISPLAY": ":9", "XAUTHORITY": "/tmp/xauth.1"}}}}


def test_headed_on_wayland_has_no_display_key(posix_host):
    env = dict(NO_CYGPATH, DISPLAY="", WAYLAND_DISPLAY="wayland-1", XDG_RUNTIME_DIR="/run/x")
    entry = browser_mcp.build("/r/b/x", headed=True, env=env)["mcpServers"]["playwright"]
    assert entry["env"] == {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/x"}
    assert "--headless" not in entry["args"]


def test_headed_without_any_display_has_an_empty_env(posix_host):
    entry = browser_mcp.build("/r/b/x", headed=True, env=NO_CYGPATH)["mcpServers"]["playwright"]
    assert entry["env"] == {}


@pytest.mark.parametrize("host", ["posix", "nt"])
@pytest.mark.parametrize("env_kw", [
    {},
    {"QWEN_PLAYWRIGHT_MCP": "node /x/*.js"},
    {"QWEN_PLAYWRIGHT_MCP": " \t"}])
def test_browser_mcp_never_allows_unrestricted_file_access(monkeypatch, host, env_kw):
    # Playwright's --allow-unrestricted-file-access lets the server read outside its
    # --output-dir. No build variant -- default, override or blank override, POSIX or
    # the cmd /c form, headed or headless -- may ever pass it.
    monkeypatch.setattr(browser_mcp, "OS_NAME", host)
    for headed in (False, True):
        entry = browser_mcp.build("/r/b/x", headed=headed,
                                  env=dict(NO_CYGPATH, **env_kw))["mcpServers"]["playwright"]
        assert "--allow-unrestricted-file-access" not in entry["args"]


def test_env_defaults_to_os_environ(posix_host, monkeypatch):
    monkeypatch.setenv("QWEN_PLAYWRIGHT_MCP", "node /y/cli.js")
    assert browser_mcp.build("/o")["mcpServers"]["playwright"]["command"] == "node"


def test_cli_prints_the_file_text_byte_for_byte(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QWEN_PLAYWRIGHT_MCP"}
    env["QWEN_PLAYWRIGHT_MCP"] = "node /x/cli.js"
    for argv in ([sys.executable, "-m", "lib.browser_mcp"],
                 [sys.executable, str(SA / "lib" / "browser_mcp.py")]):
        r = subprocess.run(argv + ["--output-dir", "/r/b/x"], cwd=str(SA), env=env,
                           capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr
        want = ('{\n  "mcpServers": {\n    "playwright": {\n      "command": "node",\n'
                '      "args": [\n        "/x/cli.js",\n        "--headless",\n'
                '        "--isolated",\n        "--output-dir",\n        "/r/b/x",\n'
                '        "--image-responses",\n        "allow",\n        "--viewport-size",\n'
                '        "1280,900"\n      ]\n    }\n  }\n}\n')
        assert r.stdout == want.encode("utf-8")              # no "\r\n", even on Windows
        assert json.loads(r.stdout) == browser_mcp.build("/r/b/x", env=env)


def test_cli_headed_flag(tmp_path):
    env = dict(os.environ, QWEN_PLAYWRIGHT_MCP="node /x/cli.js", DISPLAY=":3")
    r = subprocess.run([sys.executable, str(SA / "lib" / "browser_mcp.py"), "--output-dir",
                        "/o", "--headed"], env=env, capture_output=True, timeout=60)
    entry = json.loads(r.stdout)["mcpServers"]["playwright"]
    assert "--headless" not in entry["args"] and entry["env"]["DISPLAY"] == ":3"
