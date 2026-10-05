"""The qwen-deep-research wrapper: everything the bash side owns, offline.

For the pieces that need a model server and a fake `claude` (the wrapper's own
--check without the agent override runs the real qwen-agent.sh preflight), the
fixtures are exactly what tests/test_cli.py sets up, reused from there.
"""
import http.server
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading

import pytest

import test_cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRAP = ROOT / "skill" / "local-auditor" / "qwen-deep-research.sh"
AGENT = ROOT / "skill" / "local-auditor" / "qwen-agent.sh"
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


def run(tmp_path, args, **env):
    # A developer's own QWEN_* (a base URL, a search backend) must not leak into
    # a wrapper test: strip every one of them, then add back what the test names.
    e = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    e.update(QWEN_CONFIG=str(tmp_path / "noconfig"), HOME=str(tmp_path), **env)
    return subprocess.run([BASH, str(WRAP), *args], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(tmp_path), env=e, timeout=120)


@pytest.fixture
def models_server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), test_cli._Models)
    httpd.models = [{"id": "local-model", "object": "model", "max_model_len": 262144}]
    httpd.payload = None
    httpd.key = None
    # poll_interval keeps shutdown() snappy (see the server fixture in test_cli.py).
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fake_claude(tmp_path):
    p = tmp_path / "fake-claude"
    p.write_text(test_cli.FAKE_CLAUDE, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def test_wrapper_usage_error_exits_2(tmp_path):
    r = run(tmp_path, [])
    assert r.returncode == 2 and "question" in r.stderr


def test_wrapper_check_uses_agent(tmp_path):
    fake = ROOT / "tests" / "fake_swarm_agent.py"
    d = tmp_path / "fake"
    d.mkdir()
    r = run(tmp_path, ["--check"], FAKE_SWARM_DIR=str(d), QWEN_DR_SKIP_SEARCH_CHECK="1",
            QWEN_DR_AGENT_OVERRIDE="%s %s" % (sys.executable, fake))
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_wrapper_check_without_override(tmp_path, models_server, fake_claude):
    # No QWEN_DR_AGENT_OVERRIDE: the wrapper hands research.py `bash qwen-agent.sh`
    # (both paths through native_path), so the real preflight must run against a
    # fake model server and a fake claude, exactly as a person's machine would.
    r = run(tmp_path, ["--check"], QWEN_DR_SKIP_SEARCH_CHECK="1",
            QWEN_BASE_URL=test_cli.base_url(models_server),
            QWEN_CLAUDE_BIN=test_cli.posix(fake_claude))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout


def test_wrapper_version_matches_qwen_agent(tmp_path):
    # One version: the wrapper reads qwen-agent.sh's QA_VERSION line, so the two
    # cannot drift apart.
    r = run(tmp_path, ["--version"])
    assert r.returncode == 0
    qa_version = re.search(r'^QA_VERSION="(.*)"$', AGENT.read_text(encoding="utf-8"), re.M)
    assert qa_version, "qwen-agent.sh has no QA_VERSION line to read"
    assert r.stdout.strip() == "qwen-deep-research %s" % qa_version.group(1)


def test_help_flag_later_is_not_help(tmp_path):
    # -h/--help/--version mean "print this" only as the FIRST argument. Anywhere
    # else they reach research.py, which reports a usage error instead of printing
    # a help screen nobody asked for.
    for args in (["the question", "--help"], ["--out", "run", "the question", "--help"],
                 ["--depth", "quick", "-h"], ["the question", "--version"]):
        r = run(tmp_path, args)
        assert r.returncode == 2, "%s: %s" % (args, r.stdout + r.stderr)
        assert "EXIT CODES" not in r.stdout, args        # the wrapper's screen stayed closed
        assert "usage" in r.stderr.lower() and "--depth" in r.stderr
    # as the FIRST argument they are still the promised output
    r = run(tmp_path, ["--help"])
    assert r.returncode == 0 and "EXIT CODES" in r.stdout
    r = run(tmp_path, ["-h"])
    assert r.returncode == 0 and "EXIT CODES" in r.stdout
