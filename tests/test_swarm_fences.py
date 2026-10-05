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
    assert fences.unit_fields("read") == {"toolset": "Read,Glob,Grep", "grants": "Read,Glob,Grep",
                                          "web": False, "mcp_config": None}
    sb = fences.unit_fields("sandbox")
    assert sb["toolset"] == sb["grants"] == "Read,Edit,Write,Bash,Glob,Grep"
    assert sb["web"] is False and sb["mcp_config"] is None


def test_unknown_fence_and_web_without_mcp_are_errors():
    with pytest.raises(ValueError):
        fences.unit_fields("net")
    with pytest.raises(ValueError):
        fences.unit_fields("web")


@pytest.mark.parametrize("fence,present,absent", [
    ("none", [("--toolset", "none")], ["-t", "--web", "--mcp-config"]),
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
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and "--warn-denials" in argv


def test_mcp_config_never_holds_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_SEARCH_KEY", "sekret-key-123")
    monkeypatch.setenv("QWEN_SEARCH_URL", "http://192.0.2.1:8888")
    path = fences.mcp_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "sekret-key-123" not in text
    cfg = json.loads(text)["mcpServers"]["search"]
    assert cfg["env"]["QWEN_SEARCH_URL"] == "http://192.0.2.1:8888"
    assert cfg["args"][0].endswith("search_mcp.py")
