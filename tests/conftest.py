import os
import pathlib
import sys
# The skill directory is the install unit, so the library lives inside it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor"))

import pytest  # noqa: E402


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
