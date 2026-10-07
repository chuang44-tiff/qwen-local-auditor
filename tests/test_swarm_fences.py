import json
import sys

import pytest

from lib import swarm
from lib.swarm_engine import fences
from swarm_fixtures import FAKE


def test_each_fence_maps_to_the_released_flags(tmp_path):
    mcp = tmp_path / "mcp.json"
    assert fences.unit_fields("none") == {"toolset": "none", "grants": "", "web": False,
                                          "mcp_config": None}
    assert fences.unit_fields("search", mcp) == {"toolset": "none", "grants": "mcp__search__search",
                                                 "web": False, "mcp_config": mcp}
    assert fences.unit_fields("web", mcp) == {"toolset": "none", "grants": "mcp__search__search",
                                              "web": True, "mcp_config": mcp}
    # browser is `none` plus qwen-agent's own --browser: no toolset, no grants, and no
    # --mcp-config of ours (qwen-agent writes the Playwright server config for itself)
    assert fences.unit_fields("browser") == {"toolset": "none", "grants": "", "web": False,
                                             "mcp_config": None, "browser": True}
    assert fences.unit_fields("read") == {"toolset": "Read,Glob,Grep", "grants": "Read,Glob,Grep",
                                          "web": False, "mcp_config": None}
    sb = fences.unit_fields("sandbox")
    assert sb["toolset"] == sb["grants"] == "Read,Edit,Write,Bash,Glob,Grep"
    assert sb["web"] is False and sb["mcp_config"] is None
    # no other fence asks for the browser, so no other unit carries the key at all
    assert "browser" not in fences.unit_fields("none")
    assert "browser" not in fences.unit_fields("sandbox")
    assert "browser" not in fences.unit_fields("web", mcp)


def test_unknown_fence_and_web_without_mcp_are_errors():
    with pytest.raises(ValueError):
        fences.unit_fields("net")
    with pytest.raises(ValueError):
        fences.unit_fields("web")


@pytest.mark.parametrize("fence,present,absent", [
    ("none", [("--toolset", "none")], ["-t", "--web", "--mcp-config"]),
    ("browser", [("--toolset", "none")], ["-t", "--web", "--mcp-config"]),
    ("search", [("--toolset", "none"), ("-t", "mcp__search__search")], ["--web"]),
    ("web", [("--toolset", "none"), ("-t", "mcp__search__search")], []),
    ("read", [("--toolset", "Read,Glob,Grep"), ("-t", "Read,Glob,Grep")], ["--web", "--mcp-config"]),
    ("sandbox", [("--toolset", "Read,Edit,Write,Bash,Glob,Grep")], ["--web", "--mcp-config"]),
])
def test_fence_reaches_the_agent_argv(tmp_path, monkeypatch, fence, present, absent):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    role = tmp_path / "r.md"
    role.write_text("role", encoding="utf-8")
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    u = swarm.Unit(name="u-1", role_file=role, prompt="p",
                   **fences.unit_fields(fence, mcp if fence in fences.WEB_FENCES else None))
    sw = swarm.Swarm([sys.executable, str(FAKE)], tmp_path / "run", seats=1, timeout=60, backoff=0)
    sw.run_phase([u])
    argv = json.loads((d / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    for flag, value in present:
        assert argv[argv.index(flag) + 1] == value, flag
    for flag in absent:
        assert flag not in argv, flag
    assert ("--web" in argv) == (fence == "web")
    assert ("--browser" in argv) == (fence == "browser")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and "--warn-denials" in argv


def test_a_browser_unit_keeps_its_own_empty_folder(tmp_path, monkeypatch):
    # The browser fence drives a local UI suite: it is `none` plus --browser, so its
    # working directory is the unit's own empty agents/<unit> folder, not --target.
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    role = tmp_path / "r.md"
    role.write_text("role", encoding="utf-8")
    run = tmp_path / "run"
    u = swarm.Unit(name="u-1", role_file=role, prompt="p", **fences.unit_fields("browser"))
    swarm.Swarm([sys.executable, str(FAKE)], run, seats=1, timeout=60, backoff=0).run_phase([u])
    argv = json.loads((d / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert argv[argv.index("-C") + 1] == str(run / "agents" / "u-1")
    assert "--browser" in argv and "--mcp-config" not in argv


def test_the_browser_fence_needs_neither_search_nor_a_target():
    # A UI suite runs against local URLs: a browser role is not a web fence (it runs at
    # --seats and asks for no search preflight) and not a target fence (it works in its
    # own folder), so a manifest of browser roles alone needs no mcp.json and no --target.
    class _Role:
        def __init__(self, fence):
            self.fence = fence

    class _Manifest:
        def __init__(self, *fences_):
            self.roles = {str(i): _Role(f) for i, f in enumerate(fences_)}

    assert "browser" not in fences.WEB_FENCES and "browser" not in fences.TARGET_FENCES
    assert fences.needs_search(_Manifest("none", "browser", "read", "sandbox")) is False
    assert fences.needs_search(_Manifest("browser", "web")) is True


def test_mcp_config_never_holds_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_SEARCH_KEY", "sekret-key-123")
    monkeypatch.setenv("QWEN_SEARCH_URL", "http://192.0.2.1:8888")
    path = fences.mcp_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "sekret-key-123" not in text
    cfg = json.loads(text)["mcpServers"]["search"]
    assert cfg["env"]["QWEN_SEARCH_URL"] == "http://192.0.2.1:8888"
    assert cfg["args"][0].endswith("search_mcp.py")
