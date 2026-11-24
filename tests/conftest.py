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


@pytest.fixture(autouse=True, scope="session")
def _no_outer_watch_run_for_the_session():
    """The suite must not see a watched run it happens to be running INSIDE (a qwen-agent
    --until-done loop with the page on passes these down to every process it starts,
    pytest included). None of them belongs to the suite: a swarm unit spawned by a test
    would inherit the outer run's activity file and parent pid -- the file an unregistered
    unit's environment must never name, and the register() the real launchers do would
    refuse as another run's child. QWEN_WATCH_ID (the ledger's run stamp, set by
    register()) and QA_ROLE (the role the advisor records, exported by qwen-agent.sh)
    go the same way: a test's ledger line must not claim an outer run or an outer role.
    All of them leave the environment for the whole session
    (each test's own registration sets what it needs with monkeypatch, and the harnesses
    that build a launcher's child env from scratch drop every QWEN_*, these with them);
    whatever the outer run set comes back when the session ends."""
    names = ("QWEN_WATCH_ACTIVITY", "QWEN_WATCH_PARENT",
             "QWEN_WATCH_CHILD", "QWEN_WATCH_CTX_WINDOW", "QWEN_WATCH_ID",
             "QWEN_INBOX", "QWEN_INBOX_DEFER",
             "QA_ROLE")   # the ledger's run stamp and the advisor's recorded role:
                         # neither is the suite's -- a run started under an agent with
                         # -r would otherwise stamp its role into every ledger line.
                         # The two messaging vars go the same way: they name an outer
                         # run's inbox, and a launcher that found one inherited would
                         # stream notes into the suite's own runs (spec 4.2/P5).
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    yield
    for name, prev in saved.items():
        if prev is None:
            os.environ.pop(name, None)             # a test leaked it: the outer run had
        else:                                      # none, and none goes back
            os.environ[name] = prev


@pytest.fixture(autouse=True, scope="session")
def _watch_off_for_the_session():
    """The watch page registers every real run and can spawn the viewer; the
    end-to-end tests run the real launchers, so the whole session runs with
    QWEN_WATCH=0 (lib/watch.py reads it before registering anything) AND
    QWEN_WATCH_NOSERVE=1 -- the second line is what makes the suite never touch the
    real port even if some harness leaks past the switch and registers anyway: the
    viewer never starts on 127.0.0.1:8790 (or any configured address) under pytest.
    The watch's own tests (test_watch.py) undo this where they need to register."""
    old = os.environ.get("QWEN_WATCH")
    old_ns = os.environ.get("QWEN_WATCH_NOSERVE")
    os.environ["QWEN_WATCH"] = "0"
    os.environ["QWEN_WATCH_NOSERVE"] = "1"
    yield
    for name, prev in (("QWEN_WATCH", old), ("QWEN_WATCH_NOSERVE", old_ns)):
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


@pytest.fixture(autouse=True)
def _watch_off_into_run(monkeypatch):
    """The child-env twins of the session's QWEN_WATCH=0 and QWEN_WATCH_NOSERVE=1:
    test_cli.run() and the swarm/research/sweep/testrun harnesses build the launcher's
    env from scratch, dropping every QWEN_* (see _depth_shallow_into_run for the
    pattern), so the switches are re-injected unless the test names them itself --
    otherwise the real launchers would register runs and spawn the watch viewer under
    the suite (lib/watch.py registers whenever QWEN_WATCH is not "0", and spawns the
    viewer unless QWEN_WATCH_NOSERVE is "1"). A real run once left an `agent` pointer
    titled ": hi" in the developer's watch dir; the whole point here is that the suite
    never touches the real watch dir or port.

    QA_SUPERVISED rides along for the same reason, as a guard rather than a switch:
    qwen-agent.sh reads it from the environment (it is what --until-done exports for
    its rounds) and it is not a QWEN_* name, so a pytest started under a supervisor
    would leak it into every launcher child and silence the claude: summary line the
    settings tests assert. "0" behaves exactly like unset; a test that wants a
    supervised round names QA_SUPERVISED="1" itself and setdefault leaves it alone."""
    import test_cli
    import test_cli_research
    import test_cli_swarm
    import test_sweep_cli
    import test_testrun

    def wrapper(orig, extra_style):
        def run(*a, **kw):
            if extra_style:                                  # test_cli.run / sweep: env rides in "extra"
                extra = dict(kw.get("extra") or {})
                extra.setdefault("QWEN_WATCH", "0")
                extra.setdefault("QWEN_WATCH_NOSERVE", "1")
                extra.setdefault("QA_SUPERVISED", "0")
                kw["extra"] = extra
            else:                                            # **env kwargs
                kw.setdefault("QWEN_WATCH", "0")
                kw.setdefault("QWEN_WATCH_NOSERVE", "1")
                kw.setdefault("QA_SUPERVISED", "0")
            return orig(*a, **kw)
        return run

    for mod, attr, style in ((test_cli, "run", "extra"), (test_sweep_cli, "sweep", "extra"),
                             (test_cli_swarm, "run", "kw"), (test_cli_research, "run", "kw"),
                             (test_testrun, "run_wrapper", "kw")):
        orig = getattr(mod, attr)
        patched = wrapper(orig, style == "extra")
        for other in list(sys.modules.values()):             # modules that did `from ... import run`
            if getattr(other, "__name__", "").startswith("test_") and getattr(other, attr, None) is orig:
                monkeypatch.setattr(other, attr, patched)
        monkeypatch.setattr(mod, attr, patched)


@pytest.fixture(autouse=True, scope="session")
def _cache_home_in_tmp(tmp_path_factory):
    """The Claude usage ledger (lib/watch.py's claude_ledger_path) appends to
    $XDG_CACHE_HOME/qwen-agent/claude-calls.jsonl -- and an advisor or claude-check call
    made anywhere in the suite lands there. The whole session therefore runs with the
    cache dir at a temporary path: no test ever appends to the developer's real
    ~/.cache/qwen-agent/claude-calls.jsonl (a launcher child built from os.environ
    inherits it too; the harnesses that strip QWEN_* keep it, and the one harness
    that strips it on purpose -- qwen-sweep's, whose run-dir default test needs the
    HOME fallback -- drives a fake dispatcher that never reaches the recording code).
    Tests that want the dir elsewhere -- the watch's own, the advisor-log assertions
    -- pin it themselves with monkeypatch/extra, which overrides and restores this.
    (The real WATCH dir is already kept clean by QWEN_WATCH=0; this is its twin for the
    one durable file the watch keeps OUTSIDE the watch dir.)"""
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(tmp_path_factory.mktemp("xdg-cache"))
    yield
    if old is None:
        del os.environ["XDG_CACHE_HOME"]
    else:
        os.environ["XDG_CACHE_HOME"] = old


@pytest.fixture(autouse=True, scope="session")
def _state_home_in_tmp(tmp_path_factory):
    """The one-time notice writes a marker under XDG_STATE_HOME: a test run must never
    touch the developer's real one (runners that do not pass QWEN_NO_NOTICE land here)."""
    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(tmp_path_factory.mktemp("xdg-state"))
    os.environ["QWEN_NO_NOTICE"] = "1"   # a runner that forgets it stays quiet; notice tests set ""
    yield
    os.environ.pop("QWEN_NO_NOTICE", None)
    if old is None:
        del os.environ["XDG_STATE_HOME"]
    else:
        os.environ["XDG_STATE_HOME"] = old


@pytest.fixture(autouse=True)
def _no_qwen_swarm_env(monkeypatch):
    """A developer's QWEN_SWARM_* or QWEN_DR_* shell settings must not change test results."""
    for name in list(os.environ):
        if name.startswith(("QWEN_SWARM_", "QWEN_DR_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_advisor_budget_env(monkeypatch):
    """A developer's QWEN_ADVISOR_MAX_CALLS shell setting must not change the expected budget
    text; the tests that need a budget set it themselves."""
    monkeypatch.delenv("QWEN_ADVISOR_MAX_CALLS", raising=False)


@pytest.fixture(autouse=True)
def _no_ask_lease_env(monkeypatch):
    """A developer's QWEN_ASK_LEASE shell setting must not decide how long an
    asks/lease.json lives in a test (asks.lease_seconds reads it, and the asks CLI
    tests assert on lease expiry and freshness); the tests that need a lease length
    set it themselves."""
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)


@pytest.fixture(autouse=True)
def _claude_probe_off(monkeypatch):
    """The claude availability probe (claude_check.probe, and with it ui-test's run-start
    notice) never runs against this machine's real claude or network from a test. Probe
    tests switch it back on (monkeypatch.delenv) and own their target: a local http.server
    or a refused port in QWEN_CLAUDE_PROBE_URL."""
    monkeypatch.setenv("QWEN_CLAUDE_PROBE", "off")
