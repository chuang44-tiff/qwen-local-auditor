import os
import pathlib
import signal
import sys
# The skill directory is the install unit, so the library lives inside it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"))

import pytest  # noqa: E402


def sigint_default():
    """preexec_fn for the tests that send SIGINT to a child: pytest itself may have been
    started with SIGINT ignored (nohup, a background job), and the child would inherit
    SIG_IGN and never see the interrupt. Runs in the child between fork and exec, so it
    hands it the default disposition instead. POSIX only -- every test that passes it is
    already skipped off POSIX."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.fixture(autouse=True, scope="session")
def _depth_shallow_for_the_session():
    """Depth is the DEFAULT for real qwen-agent runs; this suite pins the pre-depth
    (shallow) behaviour, so the whole session runs with QWEN_DEPTH=shallow. Tests
    for the default depth pass QWEN_DEPTH explicitly (see test_cli_default_depth)."""
    old = os.environ.get("QWEN_DEPTH")
    os.environ["QWEN_DEPTH"] = "shallow"
    yield
    if old is None:
        del os.environ["QWEN_DEPTH"]
    else:
        os.environ["QWEN_DEPTH"] = old


@pytest.fixture(autouse=True)
def _depth_shallow_into_run(monkeypatch):
    """test_cli.run() builds the child env from scratch (every QWEN_* is stripped so a
    developer's own settings cannot leak in) -- so the session's QWEN_DEPTH has to be
    re-injected there unless the test sets QWEN_DEPTH itself. Modules that did
    `from test_cli import run` keep a direct binding, so the wrapper goes onto each."""
    import test_cli
    orig = test_cli.run

    def run(tmp_path, args, *a, **kw):
        extra = dict(kw.get("extra") or {})
        extra.setdefault("QWEN_DEPTH", os.environ.get("QWEN_DEPTH", "shallow"))
        kw["extra"] = extra
        return orig(tmp_path, args, *a, **kw)

    for mod in list(sys.modules.values()):
        if getattr(mod, "__name__", "").startswith("test_") and getattr(mod, "run", None) is orig:
            monkeypatch.setattr(mod, "run", run)
    monkeypatch.setattr(test_cli, "run", run)


@pytest.fixture(autouse=True)
def _no_qwen_swarm_env(monkeypatch):
    """A developer's QWEN_SWARM_* or QWEN_DR_* shell settings must not change test results."""
    for name in list(os.environ):
        if name.startswith(("QWEN_SWARM_", "QWEN_DR_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _claude_probe_off(monkeypatch):
    """The claude availability probe (claude_check.probe, and with it ui-test's run-start
    notice) never runs against this machine's real claude or network from a test. Probe
    tests switch it back on (monkeypatch.delenv) and own their target: a local http.server
    or a refused port in QWEN_CLAUDE_PROBE_URL."""
    monkeypatch.setenv("QWEN_CLAUDE_PROBE", "off")
