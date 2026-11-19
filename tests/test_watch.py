"""Task 1 of the watch page: the run registry, the events.jsonl fold, the viewer.

Every test here sees the watch dir and the config file under its own tmp_path (the
autouse fixture below), and undoes the session's QWEN_WATCH=0 where it registers.
Nothing here ever starts a real server through register(): start_server=False, or the
spawn is mocked (the health probe itself runs against a junk listener). The viewer's own
lifetime (serve, stop) is tested against one real server, started as a subprocess and
ended by `stop` itself -- the only way to prove the port is actually handed back."""
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from lib import watch, watch_hook

WATCH_PY = os.path.abspath(watch.__file__)          # runnable on its own: stdlib imports only
HOOK_PY = os.path.abspath(os.path.join(os.path.dirname(WATCH_PY),
                                       "watch_hook.py"))   # the same, for the activity hook


@pytest.fixture(autouse=True)
def _watch_home(tmp_path, monkeypatch):
    """watch_dir() lives under this test's tmp_path, and no real config file can
    change host/port defaults; a previous test's register() may have set the child
    switch, the owner pid and the run-id stamp into os.environ (register writes them
    directly), so they go -- before the test AND after it: a delenv of a variable
    that was not set records nothing to restore, so what THIS test's register()
    wrote would otherwise leak into the next test file (the advisor's ledger tests
    read QWEN_WATCH_ID as the run id)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("QWEN_CONFIG", str(tmp_path / "no-such-config"))
    names = ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in names:
        os.environ.pop(name, None)


def ev(kind, **fields):
    """One events.jsonl line as the engine writes it: kind and t first."""
    return json.dumps(dict(fields, kind=kind, t=time.time()))


def ev_at(kind, t, **fields):
    """The same at a chosen t: the fold's clock is the events' own stamps, so the
    phase usage and the per-agent bar are computed on seconds the test decides."""
    return json.dumps(dict(fields, kind=kind, t=t))


def write_events(run, lines):
    """The events file as the engine's emitter would hand it: EXACTLY these bytes.
    Written binary on purpose -- text mode would turn every line ending into CRLF on
    Windows, and the incremental fold counts bytes."""
    run.mkdir(parents=True, exist_ok=True)
    (run / "events.jsonl").write_bytes(("".join(x + "\n" for x in lines)).encode("utf-8"))


def pointer(**over):
    """A registered pointer dict as the page would load it, alive by default."""
    base = {"id": "ab12cd34ef56", "kind": "swarm", "title": "t", "run_path": None,
            "pid": os.getpid(), "start": time.time(), "timeout": None,
            "max_rounds": None, "end": None, "exit": None}
    base.update(over)
    return base


def read_pointer(run_id):
    with open(os.path.join(watch.watch_dir(), "%s.json" % run_id), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- registry

def test_register_writes_pointer_and_sets_child_env(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    rid = watch.register("swarm", "my run", run_path=str(run), timeout=60,
                         max_rounds=5, start_server=False)
    assert rid == watch.run_id_for(str(run))
    assert len(rid) == 12 and all(c in "0123456789abcdef" for c in rid)
    p = read_pointer(rid)
    assert set(p) == {"id", "kind", "title", "run_path", "pid", "start", "timeout",
                      "max_rounds", "end", "exit"}
    assert p["id"] == rid and p["kind"] == "swarm" and p["title"] == "my run"
    assert p["run_path"] == str(run) and p["pid"] == os.getpid()
    assert p["timeout"] == 60 and p["max_rounds"] == 5
    assert p["end"] is None and p["exit"] is None
    assert isinstance(p["start"], float) and p["start"] <= time.time()
    assert os.environ["QWEN_WATCH_CHILD"] == "1"      # children of this run never register


def test_register_disabled_by_env_returns_none(tmp_path, monkeypatch):
    run = tmp_path / "run"
    monkeypatch.setenv("QWEN_WATCH", "0")
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
    assert watch.register("swarm", "t", run_path=str(run), start_server=False) is None
    monkeypatch.delenv("QWEN_WATCH")
    monkeypatch.setenv("QWEN_WATCH_CHILD", "1")
    assert watch.register("swarm", "t", run_path=str(run), start_server=False) is None
    assert not os.path.exists(watch.watch_dir())      # disabled: nothing is written at all


def test_register_owner_pid(tmp_path, monkeypatch):
    """WHO owns a registration: register() stamps QWEN_WATCH_PARENT with its own pid,
    and the refusals read that stamp, not the legacy child switch alone. Another pid
    in QWEN_WATCH_PARENT means a registered run's child (refuse); QWEN_WATCH_CHILD=1
    with no parent of ours means the same (refuse, the legacy reading); the owning
    process -- parent == own pid, child switch set and all -- may register again, and
    two register() calls in one process both succeed."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "first", run_path=str(tmp_path / "run"),
                         start_server=False)
    assert rid is not None
    assert os.environ["QWEN_WATCH_PARENT"] == str(os.getpid())    # the stamp it leaves
    assert os.environ["QWEN_WATCH_CHILD"] == "1"
    rid2 = watch.register("swarm", "second", run_path=str(tmp_path / "run2"),
                          start_server=False)                     # same process, same owner
    assert rid2 is not None and rid2 != rid
    # another pid owns a registration: this process is that run's child; refuse, write nothing
    for other in ("1", "999999"):
        kid = tmp_path / ("kid-%s" % other)               # parenthesized: `/` and `%`
        # are same-precedence left-associative operators, so unparenthesized the join
        # happens first and the format lands on a PosixPath
        monkeypatch.setenv("QWEN_WATCH_PARENT", other)
        assert watch.register("swarm", "kid", run_path=str(kid),
                              start_server=False) is None, other
        assert not os.path.exists(watch._pointer_path(watch.run_id_for(str(kid)))), other
    # the legacy switch without a parent of our own: the same child, refused
    monkeypatch.delenv("QWEN_WATCH_PARENT")
    monkeypatch.setenv("QWEN_WATCH_CHILD", "1")
    assert watch.register("swarm", "kid", run_path=str(tmp_path / "kid2"),
                          start_server=False) is None
    # and the switch WITH our own pid: that is the owner itself -- the legacy flag
    # cannot refuse what the parent stamp just allowed (the pair register itself sets)
    monkeypatch.setenv("QWEN_WATCH_PARENT", str(os.getpid()))
    assert watch.register("swarm", "again", run_path=str(tmp_path / "run3"),
                          start_server=False) is not None


def test_config_file_switch_and_default_form(tmp_path, monkeypatch):
    """_setting resolves the config file the way the sourcing shell would: a line of
    the config.example form NAME="${NAME:-value}" reads `value` when NAME is not in
    the environment (it is not -- the environment was checked first), quoted or not,
    and a plain NAME=value line reads its quote-stripped value. register() reads
    QWEN_WATCH through it, so QWEN_WATCH="${QWEN_WATCH:-0}" in the file is a real
    switch. And config.example keeps printing those lines in that form."""
    cfg = tmp_path / "config"
    cfg.write_text('# comment\nQWEN_WATCH="${QWEN_WATCH:-0}"\n'
                   'QWEN_WATCH_NOSERVE=${QWEN_WATCH_NOSERVE:-1}\n'
                   'QWEN_WATCH_HOST="${QWEN_WATCH_HOST:-127.0.0.1,127.0.0.2}"\n'
                   'export QWEN_WATCH_PORT="8791"\n', encoding="utf-8")
    monkeypatch.setenv("QWEN_CONFIG", str(cfg))
    for name in ("QWEN_WATCH", "QWEN_WATCH_NOSERVE", "QWEN_WATCH_HOST",
                 "QWEN_WATCH_PORT", "QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT"):
        monkeypatch.delenv(name, raising=False)
    assert watch._setting("QWEN_WATCH") == "0"              # the default IS the value
    assert watch._setting("QWEN_WATCH_NOSERVE") == "1"      # the unquoted form too
    assert watch._setting("QWEN_WATCH_PORT") == "8791"      # quotes stripped
    assert watch._host_list() == ["127.0.0.1", "127.0.0.2"] # read through _setting
    assert watch.watch_host() == "127.0.0.1"                 # the link names the first
    assert watch._setting("QWEN_WATCH_ABSENT", "d") == "d"  # not on any line: default
    assert watch.register("swarm", "t", run_path=str(tmp_path / "run"),
                          start_server=False) is None       # register reads it too
    assert not os.path.exists(watch.watch_dir())            # off: nothing written
    monkeypatch.setenv("QWEN_WATCH", "1")                    # the environment still wins
    assert watch._setting("QWEN_WATCH") == "1"
    assert watch.register("swarm", "t", run_path=str(tmp_path / "run"),
                          start_server=False) is not None
    example = pathlib.Path(os.path.dirname(os.path.abspath(watch.__file__))).parents[2] \
        / "config.example"
    text = example.read_text(encoding="utf-8")
    for name in ("QWEN_WATCH", "QWEN_WATCH_HOST", "QWEN_WATCH_PORT"):
        assert '# %s="${%s:-' % (name, name) in text, name   # the form the doc promises


def test_config_file_last_assignment_wins(tmp_path, monkeypatch):
    """A config file is SOURCED, and a sourced file keeps the LAST value of a repeated
    assignment: when NAME= is on several lines -- in any of the accepted forms (bare,
    quoted, export-ed, the NAME="${NAME:-default}" default form) -- _setting reads the
    last one, exactly as `source` would leave the variable. The environment still
    outranks the file, and a name on no line still takes its default."""
    cfg = tmp_path / "config"
    cfg.write_text('QWEN_WATCH_PORT="8790"\nexport QWEN_WATCH_PORT=8791\n'
                   'QWEN_WATCH_PORT="${QWEN_WATCH_PORT:-8792}"\n'
                   "QWEN_WATCH_HOST=one\nQWEN_WATCH_HOST=127.0.0.1,127.0.0.2\n"
                   "QWEN_WATCH=1\nQWEN_WATCH=0\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_CONFIG", str(cfg))
    for name in ("QWEN_WATCH", "QWEN_WATCH_PORT", "QWEN_WATCH_HOST",
                 "QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT"):
        monkeypatch.delenv(name, raising=False)
    assert watch._setting("QWEN_WATCH_PORT") == "8792"       # last of the three forms
    assert watch.watch_port() == 8792
    assert watch._host_list() == ["127.0.0.1", "127.0.0.2"]  # the second HOST line wins
    assert watch._setting("QWEN_WATCH") == "0"               # and register reads that
    assert watch.register("swarm", "t", run_path=str(tmp_path / "run"),
                          start_server=False) is None        # switch off: no pointer
    monkeypatch.setenv("QWEN_WATCH", "1")                    # the environment still wins
    assert watch._setting("QWEN_WATCH") == "1"
    assert watch.register("swarm", "t", run_path=str(tmp_path / "run"),
                          start_server=False) is not None
    assert watch._setting("QWEN_WATCH_ABSENT", "d") == "d"   # no line at all: default


def test_register_keeps_start_on_resume(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    rid = watch.register("swarm", "first", run_path=str(run), start_server=False)
    p = read_pointer(rid)
    p["start"] = 1234.0                                # the start the resume must keep
    p["end"] = 9999.0
    p["exit"] = 5
    with open(os.path.join(watch.watch_dir(), "%s.json" % rid), "w", encoding="utf-8") as fh:
        json.dump(p, fh)
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)     # the resume is a fresh process
    rid2 = watch.register("swarm", "second", run_path=str(run), start_server=False)
    assert rid2 == rid                                 # same run_path: the same run
    p2 = read_pointer(rid)
    assert p2["start"] == 1234.0
    assert p2["title"] == "second" and p2["end"] is None and p2["exit"] is None


def test_run_ids_never_collide_within_one_clock_tick(tmp_path, monkeypatch):
    """A coarse Windows clock hands two register() calls in one process the SAME
    time.time(): the no-path id mixes in os.urandom as well, so 50 calls under a
    frozen clock are still 50 distinct ids (one shared id meant the second pointer
    overwrote the first -- dismiss(gone) deleted mine's activity file, same id). The
    PATH form mixes in nothing and must not change: a resume recomputes it."""
    monkeypatch.setattr(watch.time, "time", lambda: 1700000000.0)
    ids = [watch.run_id_for() for _ in range(50)]
    assert len(set(ids)) == 50
    for rid in ids:
        assert len(rid) == 12 and all(c in "0123456789abcdef" for c in rid)
    run = str(tmp_path / "run")
    assert watch.run_id_for(run) == watch.run_id_for(run)      # the same path, the same id
    assert watch.run_id_for(run) != watch.run_id_for(str(tmp_path / "run2"))


def test_finish_sets_exit(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("agent", "a session", start_server=False)
    before = time.time()
    watch.finish(rid, 3)
    p = read_pointer(rid)
    assert p["exit"] == 3
    assert before <= p["end"] <= time.time()
    watch.finish(None, 0)                              # never registered: a silent no-op
    watch.finish("ffffffffffff", 0)                    # unknown id: likewise


def test_dismiss_ended_and_refuse_running(tmp_path, monkeypatch, capsys):
    """`watch.py dismiss <id>` clears a FINISHED card now instead of waiting out the
    prune: an ended or a killed run's pointer is deleted and the line says so (exit 0);
    a run still running keeps its pointer and its card and gets the refusal (exit 1);
    an unknown id, an invalid one and a pointer that does not even read are all
    `watch: no such run <id>` with exit 1. One line on stdout, like `stop`."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    try:
        os.kill(dead.pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.skip("pid %d was reused, it is not dead" % dead.pid)
    # ended: the pointer goes now, and says so
    rid = watch.register("swarm", "done", run_path=str(tmp_path / "r1"), start_server=False)
    watch.finish(rid, 0)
    assert watch.main(["dismiss", rid]) == 0
    assert capsys.readouterr().out == "watch: dismissed %s\n" % rid
    assert not os.path.exists(watch._pointer_path(rid))
    # killed (the pid is gone, so is the run): likewise
    killed = watch.run_id_for(str(tmp_path / "r2"))
    watch._write_json(watch._pointer_path(killed),
                      pointer(id=killed, kind="swarm", run_path=str(tmp_path / "r2"),
                              pid=dead.pid, start=time.time() - 60))
    assert watch.main(["dismiss", killed]) == 0
    assert capsys.readouterr().out == "watch: dismissed %s\n" % killed
    assert not os.path.exists(watch._pointer_path(killed))
    # still running: refused, and the pointer stays where its card needs it
    rid2 = watch.register("swarm", "live", run_path=str(tmp_path / "r3"), start_server=False)
    assert watch.main(["dismiss", rid2]) == 1
    assert capsys.readouterr().out == "watch: %s is still running\n" % rid2
    assert os.path.exists(watch._pointer_path(rid2))
    # unknown id, invalid id, unreadable pointer: one refusal line each, exit 1
    assert watch.main(["dismiss", "ffffffffffff"]) == 1
    assert capsys.readouterr().out == "watch: no such run ffffffffffff\n"
    assert watch.main(["dismiss", "nope!"]) == 1
    assert capsys.readouterr().out == "watch: no such run nope!\n"
    junk = watch.run_id_for(str(tmp_path / "r4"))
    with open(watch._pointer_path(junk), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    assert watch.main(["dismiss", junk]) == 1
    assert capsys.readouterr().out == "watch: no such run %s\n" % junk
    assert os.path.exists(watch._pointer_path(junk))     # dismiss cannot name it, so it
    # leaves it for the prune rather than deleting a file it never understood


# ---------------------------------------------------------------- the fold

def test_fold_unit_states(tmp_path):
    lines = [
        ev("run_start", workflow="swarm", goal="the goal"),
        ev("phase_start", label="build", n=4, seats=2, units=[
            {"name": "a", "role": "worker"}, {"name": "b", "role": "worker"},
            {"name": "c", "role": "worker"}, {"name": "d", "role": "worker"}]),
        ev("unit_start", unit="a", role="worker"),
        ev("unit_start", unit="b", role="worker"),
        ev("unit_done", unit="a", role="worker", ok=True, seconds=12.5),
        ev("unit_done", unit="c", role="worker", ok=True, cached=True, seconds=0.0),
        ev("unit_dropped", unit="b", role="worker", why="no answer after 2 tries"),
    ]
    f = watch.fold_events(lines)
    assert {n: u["state"] for n, u in f["units"].items()} == {
        "a": "done", "b": "dropped", "c": "cached", "d": "queued"}
    assert all(u["state"] in watch.UNIT_STATES for u in f["units"].values())
    assert f["units"]["b"]["why"] == "no answer after 2 tries"
    assert f["units"]["a"]["seconds"] == 12.5
    assert f["workflow"] == "swarm" and f["goal"] == "the goal"
    assert f["phase"] == {"label": "build", "n": 4, "seats": 2,
                          "units": ["a", "b", "c", "d"]}
    assert f["ended"] is False and f["exit"] is None
    assert len(f["recent"]) == len(lines)


def test_fold_skips_bad_lines(tmp_path):
    lines = [
        ev("run_start", workflow="swarm", goal="g"),
        ev("phase_start", label="build", n=2, seats=1,
           units=[{"name": "a", "role": "r"}, {"name": "b", "role": "r"}]),
        "this is not json at all",
        b'{"kind": "unit_done", "unit": "b", "\xff\xfe"',     # non-UTF-8 bytes: gone too
        ev("unit_done", unit="a", role="r", ok=True, seconds=1.0),
        '{"kind": "unit_dropped", "unit": "b", "ro',          # truncated last line
    ]
    f = watch.fold_events(lines)                              # skipped, never raised
    assert f["units"]["a"]["state"] == "done"
    assert f["units"]["b"]["state"] == "queued"
    assert [r["kind"] for r in f["recent"]] == ["run_start", "phase_start", "unit_done"]


def test_fold_resets_on_second_run_start(tmp_path):
    lines = [
        ev("run_start", workflow="swarm", goal="first goal"),
        ev("phase_start", label="build", n=1, seats=1, units=[{"name": "a", "role": "r"}]),
        ev("unit_done", unit="a", role="r", ok=True, cached=True, seconds=0.0),
        ev("attention", item="a", reason="dropped", detail="see run.log"),
        ev("run_end", exit=5, report=None),
        ev("run_start", workflow="swarm", goal="second goal", resumed=True),
        ev("phase_start", label="plan", n=2, seats=2,
           units=[{"name": "c", "role": "r"}, {"name": "d", "role": "r"}]),
    ]
    f = watch.fold_events(lines)
    assert f["goal"] == "second goal"
    assert f["phase"] == {"label": "plan", "n": 2, "seats": 2, "units": ["c", "d"]}
    assert set(f["units"]) == {"c", "d"}                      # a's folded state is gone
    assert f["ended"] is False and f["exit"] is None          # only after the LAST run_start
    assert f["attention"] == []
    assert [r["kind"] for r in f["recent"]] == ["run_start", "phase_start"]


def test_eta_none_until_first_finish_then_median(tmp_path):
    run = tmp_path / "run"
    names = [{"name": n, "role": "r"} for n in ("a", "b", "c", "d", "e", "f")]
    lines = [ev("run_start", workflow="swarm", goal="g"),
             ev("phase_start", label="build", n=6, seats=2, units=names),
             ev("unit_start", unit="a", role="r"), ev("unit_start", unit="b", role="r")]
    write_events(run, lines)
    st = watch.run_state(pointer(run_path=str(run)), time.time())
    assert st["state"] == "running"
    assert st["progress"] == 0.0
    assert st["eta"] is None                                  # no finished unit yet
    lines += [ev("unit_done", unit="a", role="r", ok=True, seconds=60.0),
              ev("unit_done", unit="b", role="r", ok=True, seconds=120.0)]
    write_events(run, lines)
    st = watch.run_state(pointer(run_path=str(run)), time.time())
    assert st["progress"] == pytest.approx(2 / 6)
    assert st["eta"] == pytest.approx(90.0 * 4 / 2)           # median 90, 4 left, 2 seats


def test_phase_usage_and_tail(tmp_path, monkeypatch):
    """Per-phase seat usage, folded off the events' own stamps: three units did 40
    unit-seconds of work on 2 seats inside 30 wall-seconds -- usage 40 / (2 x 30),
    min(seats, n) x wall -- and the last one started at 10 and held the phase open to
    30: a 20-second tail. A phase whose units have not all reported is still running:
    end, and everything read off it, is None. A cached unit did no work and counts 0."""
    run = tmp_path / "run"
    roster = [{"name": n, "role": "r"} for n in ("a", "b", "c")]
    lines = [
        ev_at("run_start", 0, workflow="swarm", goal="g"),
        ev_at("phase_start", 0, label="build", n=3, seats=2, units=roster),
        ev_at("unit_start", 0, unit="a", role="r", timeout=100),
        ev_at("unit_start", 0, unit="b", role="r", timeout=100),
        ev_at("unit_start", 10, unit="c", role="r", timeout=100),
        ev_at("unit_done", 10, unit="a", role="r", ok=True, cached=False, seconds=10),
        ev_at("unit_done", 10, unit="b", role="r", ok=True, cached=False, seconds=10),
        ev_at("unit_done", 30, unit="c", role="r", ok=True, cached=False, seconds=20),
    ]
    write_events(run, lines)
    ph = watch.fold_events(lines)["phases"][0]
    assert ph["label"] == "build" and ph["n"] == 3 and ph["seats"] == 2
    assert ph["start"] == 0 and ph["end"] == 30               # wall 30
    assert ph["wall"] == 30 and ph["busy"] == 40              # 10 + 10 + 20
    assert ph["usage"] == pytest.approx(40 / 60)              # busy / (min(2, 3) x 30)
    assert ph["tail"] == 20                                   # end minus the last start
    # the run view's table of finished phases reads exactly this off the detail
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["phases"][0]["usage"] == pytest.approx(40 / 60)
    # still running: c never reported, so the phase has no end and no numbers
    ph = watch.fold_events(lines[:-1])["phases"][0]
    assert ph["end"] is None and ph["wall"] is None and ph["busy"] is None
    assert ph["usage"] is None and ph["tail"] is None
    # a cached unit counts 0 busy: it never started and it never sets the end
    cached = watch.fold_events([
        ev_at("run_start", 0, workflow="swarm", goal="g"),
        ev_at("phase_start", 0, label="reuse", n=2, seats=2, units=[
            {"name": "a", "role": "r"}, {"name": "d", "role": "r"}]),
        ev_at("unit_start", 0, unit="a", role="r", timeout=100),
        ev_at("unit_done", 10, unit="a", role="r", ok=True, cached=False, seconds=10),
        ev_at("unit_done", 20, unit="d", role="r", ok=True, cached=True, seconds=0.0),
    ])["phases"][0]
    assert cached["end"] == 10 and cached["busy"] == 10 and cached["tail"] == 10
    assert cached["usage"] == pytest.approx(10 / 20)          # one seat busy of two


def test_truncated_phase_keeps_n_and_progress(tmp_path, monkeypatch):
    """The too-wide phase_start fallback keeps n (events.emit_phase_start): without a
    roster the page still has a bar. Progress is then the DISTINCT units that reported
    unit_done or unit_dropped since that phase_start over n -- a done and a dropped
    over n=4 is 0.5 -- queued is n minus the units that started, and only the units
    that started are ever listed (their names are the run's own events', nobody else's)."""
    run = tmp_path / "run"
    lines = [
        ev_at("run_start", 0, workflow="swarm", goal="g"),
        ev_at("phase_start", 1, label="fan out", n=4, seats=2, truncated=True),
        ev_at("unit_start", 2, unit="a", role="r", timeout=60),
        ev_at("unit_start", 2, unit="b", role="r", timeout=60),
        ev_at("unit_done", 12, unit="a", role="r", ok=True, cached=False, seconds=10),
        ev_at("unit_dropped", 22, unit="b", role="r", why="no answer"),
    ]
    write_events(run, lines)
    f = watch.fold_events(lines)
    assert f["phase"] == {"label": "fan out", "n": 4, "seats": 2, "units": []}
    assert sorted(f["units"]) == ["a", "b"]                   # started units only
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["progress"] == pytest.approx(0.5)               # a done + b dropped / 4
    assert st["eta"] == pytest.approx(10.0)                   # median 10 x 2 left / 2 seats


def test_no_roster_progress_counts_finishes_of_its_own_phase_only(tmp_path):
    """The distinct units a rosterless phase counts are those that reported unit_done
    or unit_dropped SINCE that phase_start -- and a cached unit_done deliberately carries
    no `ended` (a cached unit did no work), so the filter must read the outcome's stamp
    (finished_t). A unit cached under the previous phase must not be re-counted as this
    phase's progress, and units that report after its phase_start must be, cached or not."""
    run = tmp_path / "run"
    lines = [
        ev_at("run_start", 0, workflow="swarm", goal="g"),
        ev_at("phase_start", 0, label="plan", n=1, seats=1,
              units=[{"unit": "a", "name": "a", "role": "r"}]),
        ev_at("unit_done", 5, unit="a", role="r", ok=True, cached=True, seconds=0.0),
        ev_at("phase_start", 10, label="fan out", n=3, seats=2, truncated=True),
    ]
    write_events(run, lines)
    f = watch.fold_events(lines)
    assert f["units"]["a"]["ended"] is None                   # cached: no work clock,
    assert f["units"]["a"]["finished_t"] == 5                 # but a stamped outcome
    assert watch._phase_progress(f, f["units"]) == (0.0, None)  # a finished at t=5: 0/3
    lines += [
        ev_at("unit_start", 11, unit="x", role="r", timeout=60),
        ev_at("unit_done", 12, unit="x", role="r", ok=True, cached=False, seconds=1),
        ev_at("unit_done", 13, unit="y", role="r", ok=True, cached=True, seconds=0),
    ]
    write_events(run, lines)
    f = watch.fold_events(lines)                              # x and y report at t>=10:
    assert watch._phase_progress(f, f["units"]) == (pytest.approx(2 / 3),
                                                    pytest.approx(0.5))  # both count


# ------------------------------------------------------- the incremental fold
# _fold_for keeps a byte offset and the raw fold state per events file (the design
# spec's incremental read), so a poll folds only the bytes written since the last
# one. These tests hold it to that and to the rule it may never break: whatever it
# hands back EQUALS a full fold of the same bytes.

def test_incremental_fold_equals_full_fold_and_reads_only_new_bytes(tmp_path, monkeypatch):
    """Each poll reads and folds only the new bytes: the seek starts at the committed
    offset and the read stops where the file ends. A half-written trailing line is not
    folded and its bytes stay behind the offset until its newline arrives, then it is
    folded exactly once. A run_start mid-file (a resume) still resets the fold. And
    every result equals watch.fold_events of the same lines, while the page's own
    derivations -- wreck the returned copy below -- never touch the cached state."""
    run = tmp_path / "run"
    path = os.path.join(str(run), watch.EVENTS_FILE)
    pointer = {"run_path": str(run)}
    lines = [ev_at("run_start", 100.0, workflow="swarm", goal="g"),
             ev_at("phase_start", 101.0, label="build", n=2, seats=2,
                   units=[{"name": "a", "role": "r"}, {"name": "b", "role": "r"}])]
    write_events(run, lines)
    committed = sum(len(x) + 1 for x in lines)          # bytes folded so far (all ASCII)
    log = []
    real_open = open

    class _Spied:
        def __init__(self, inner):
            self._inner = inner

        def seek(self, n):
            log.append(("seek", n))
            return self._inner.seek(n)

        def read(self, *a):
            data = self._inner.read(*a)
            log.append(("read", len(data)))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

    def spy(file, *a, **kw):
        fh = real_open(file, *a, **kw)
        return _Spied(fh) if str(file) == path else fh

    monkeypatch.setattr("builtins.open", spy)

    def poll():
        del log[:]
        return watch._fold_for(pointer)

    f = poll()
    assert f == watch.fold_events(lines)
    assert log == [("seek", 0), ("read", committed)]    # the first poll reads all of it
    assert poll() == watch.fold_events(lines)
    assert log == [("seek", committed), ("read", 0)]    # nothing new: zero new bytes
    done = ev_at("unit_done", 102.0, unit="a", role="r", ok=True, cached=False,
                 seconds=5.0, why="")                   # a line appended mid-poll
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(done + "\n")
    f = poll()
    assert f == watch.fold_events(lines + [done])
    assert log == [("seek", committed), ("read", len(done) + 1)]   # only the new bytes
    lines.append(done)
    committed += len(done) + 1
    assert f["units"]["a"]["state"] == "done"
    f["units"]["a"]["state"] = "hacked"                 # the page derives on its copy;
    f["attention"].append({"item": "junk"})              # wreck it and the cache must
    f["phases"].append({"label": "junk"})                # never remember a trace
    part = ev_at("unit_done", 103.0, unit="b", role="r", ok=True, cached=False,
                 seconds=2.0, why="")
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:  # mid-line
        fh.write(part[:20])
    f = poll()
    assert f == watch.fold_events(lines)                # no half line is folded...
    assert log == [("seek", committed), ("read", 20)]   # ...and its bytes stay unread
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:  # the newline lands
        fh.write(part[20:] + "\n")
    f = poll()
    assert f == watch.fold_events(lines + [part])       # folded EXACTLY once: the wreck
    assert f["units"]["b"]["state"] == "done" \
        and f["units"]["a"]["state"] == "done" and f["attention"] == []
    lines.append(part)
    committed += len(part) + 1
    resume = ev_at("run_start", 200.0, workflow="swarm", goal="again")
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:  # a resume mid-file
        fh.write(resume + "\n")                          # the fold resets, as it would
    f = poll()
    assert f == watch.fold_events(lines + [resume])
    assert f["goal"] == "again" and f["units"] == {} and f["phases"] == []
    # and threaded polling cannot corrupt the cache: hammer _fold_for from several
    # threads while the emitter appends (attention records are NOT idempotent: a
    # double-folded byte shows up twice) -- the last fold still equals the full one
    extra = [ev_at("attention", 400.0 + i, item="i%d" % i, reason="r", detail=None)
             for i in range(40)]

    def emit():
        for rec in extra:
            with real_open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(rec + "\n")
            time.sleep(0.001)

    def hammer():
        for _ in range(20):
            watch._fold_for(pointer)
    threads = [threading.Thread(target=emit)] + \
        [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert watch._fold_for(pointer) == watch.fold_events(lines + [resume] + extra)
    # and a Windows emitter: the next line arrives with a CRLF ending. The incremental
    # fold must take exactly those bytes, fold the line into a fold EQUAL to the full
    # one, and a \r must never end up inside a value (_fold_for cuts at the last \n
    # and splitlines() eats the \r that rides in front of it)
    crlf = ev_at("attention", 500.0, item="crlf", reason="r", detail="d")
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(crlf + "\r\n")
    f = poll()
    assert f == watch.fold_events(lines + [resume] + extra + [crlf])
    assert f["recent"][-1]["item"] == "crlf"
    assert log[-1] == ("read", len(crlf) + 2)            # only the CRLF line came in
    dumped = json.dumps(f)
    assert "\r" not in dumped and "\\r" not in dumped    # no CR in the fold, no CR in
    # any value either (json would print a real one as an escaped \r -- none is there)


def test_incremental_fold_rereads_from_zero_when_the_file_is_truncated_or_replaced(
        tmp_path):
    """The offset may only be trusted while the file it was taken from is the file it
    is still reading: an identity change (st_ino/st_dev) or a size below the offset
    re-reads the whole file from 0 -- a truncated or replaced events file folds as it
    should and never as old state spliced onto new bytes. A vanished file drops its
    cache entry entirely."""
    run = tmp_path / "run"
    path = os.path.join(str(run), watch.EVENTS_FILE)
    pointer = {"run_path": str(run)}
    lines = [ev_at("run_start", 0.0, workflow="swarm", goal="g"),
             ev_at("phase_start", 1.0, label="build", n=2, seats=1,
                   units=[{"name": "a", "role": "r"}, {"name": "b", "role": "r"}]),
             ev_at("unit_start", 2.0, unit="a", role="r", timeout=60),
             ev_at("unit_done", 7.0, unit="a", role="r", ok=True, cached=False,
                   seconds=5.0, why="ok"),
             ev_at("attention", 8.0, item="b", reason="retrying", detail="attempt 2")]
    write_events(run, lines)
    assert watch._fold_for(pointer) == watch.fold_events(lines)
    assert watch._fold_for(pointer) == watch.fold_events(lines)   # out of the cache
    # truncated: the same file, now shorter than the offset -- re-read from 0
    write_events(run, lines[:2])
    assert watch._fold_for(pointer) == watch.fold_events(lines[:2])
    assert watch._fold_for(pointer) == watch.fold_events(lines[:2])
    # replaced: a DIFFERENT file moved into place -- and so big that only the identity
    # rule (not the size rule) can force the re-read from 0
    other = [ev_at("run_start", 50.0, workflow="swarm", goal="second"),
             ev_at("phase_start", 51.0, label="check", n=1, seats=1, truncated=True),
             ev_at("unit_start", 52.0, unit="x", role="r", timeout=10),
             ev_at("unit_done", 58.0, unit="x", role="r", ok=True, cached=False,
                   seconds=6.0, why=""),
             ev_at("run_end", 59.0, exit=0)]
    fresh = tmp_path / "fresh-events.jsonl"
    fresh.write_text("".join(x + "\n" for x in other), encoding="utf-8")
    assert os.path.getsize(str(fresh)) >= os.path.getsize(path)
    new_st = os.stat(str(fresh))
    os.replace(str(fresh), path)
    entry = watch._fold_cache[path]
    if (new_st.st_ino, new_st.st_dev) == (entry["ino"], entry["dev"]):
        pytest.skip("the filesystem handed out the very same inode again: the size"
                    " rule cannot be separated from the identity rule here")
    assert watch._fold_for(pointer) == watch.fold_events(other)
    assert watch._fold_cache[path]["ino"] == new_st.st_ino
    assert watch._fold_cache[path]["dev"] == new_st.st_dev
    # a pointer with no run_path at all (an agent session registers none) folds empty
    assert watch._fold_for({}) == watch._empty_fold()
    assert watch._fold_for({"run_path": ""}) == watch._empty_fold()
    assert watch._fold_for("not even a dict") == watch._empty_fold()
    # a file that is simply gone folds empty AND drops its entry: no state lingers
    os.unlink(path)
    assert watch._fold_for(pointer) == watch._empty_fold()
    assert path not in watch._fold_cache
    # and the cache is bounded: poll a couple hundred more runs through it and it never
    # grows past its cap -- old entries leave, one way or the other, and the page of a
    # run that comes back simply re-reads from 0
    for i in range(watch.FOLD_CACHE_MAX + 5):
        r = tmp_path / ("run-%d" % i)
        r.mkdir()
        write_events(r, [ev_at("run_start", 0.0, workflow="swarm", goal="g%d" % i)])
        assert watch._fold_for({"run_path": str(r)})["goal"] == "g%d" % i
    assert len(watch._fold_cache) <= watch.FOLD_CACHE_MAX
    assert str(tmp_path / ("run-%d" % (watch.FOLD_CACHE_MAX + 4)) / watch.EVENTS_FILE) \
        in watch._fold_cache                                # the newest entry stayed


def test_unit_progress_running_and_finished(tmp_path, monkeypatch):
    """The per-agent time bar divides by the budget the unit_start carried: a unit
    started 30 s ago on a 100 s budget reads 0.3 and a done unit with 50 of 100 reads
    0.5; a running one is capped at 1 (a full bar is exactly the moment the watchdog
    bites); a DROPPED unit keeps the seconds its unit_done left behind (swarm's worker
    path emits both), so it gets its seconds over the budget too -- a dropped unit that
    spent 3.2 of 60 reads 3.2/60, not an empty bar; and a unit with no timeout -- or one
    that never ran -- gets no bar."""
    now = time.time()
    run = tmp_path / "run"
    roster = [{"name": n, "role": "r"} for n in ("a", "b", "c", "d", "e", "q")]
    lines = [
        ev_at("run_start", now - 60, workflow="swarm", goal="g"),
        ev_at("phase_start", now - 60, label="build", n=6, seats=2, units=roster),
        ev_at("unit_start", now - 30, unit="a", role="r", timeout=100),
        ev_at("unit_start", now - 60, unit="b", role="r", timeout=100),
        ev_at("unit_done", now - 10, unit="b", role="r", ok=True, cached=False, seconds=50),
        ev_at("unit_start", now - 6000, unit="c", role="r", timeout=100),  # long past it
        ev_at("unit_start", now - 60, unit="d", role="r", timeout=60),     # worker path:
        ev_at("unit_done", now - 52, unit="d", role="r", ok=False, cached=False,
              seconds=3.2, why="no result block"),                         # unit_done,
        ev_at("unit_dropped", now - 50, unit="d", role="r", why="answer unusable"),
        ev_at("unit_start", now - 30, unit="e", role="r"),                 # no budget given
    ]
    write_events(run, lines)
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, now)
    assert st["units"]["a"]["started"] == pytest.approx(now - 30)   # kept by the fold
    assert st["units"]["a"]["timeout"] == 100
    assert st["units"]["a"]["progress"] == pytest.approx(0.3)      # (now - started)/100
    assert st["units"]["b"]["progress"] == pytest.approx(0.5)      # 50 seconds / 100
    assert st["units"]["c"]["progress"] == 1.0                     # capped, never above
    assert st["units"]["d"]["state"] == "dropped"
    assert st["units"]["d"]["seconds"] == pytest.approx(3.2)       # unit_done's, kept
    assert st["units"]["d"]["progress"] == pytest.approx(3.2 / 60)  # dropped: spent too
    assert st["units"]["e"]["progress"] is None                    # no timeout, no bar
    assert st["units"]["q"]["progress"] is None                    # queued: not started


# ---------------------------------------------------------------- run state

def test_killed_when_pid_dead_and_no_end(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    try:
        os.kill(dead.pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.skip("pid %d was reused, it is not dead" % dead.pid)
    run = tmp_path / "run"
    write_events(run, [
        ev("run_start", workflow="swarm", goal="g"),
        ev("phase_start", label="build", n=3, seats=1, units=[
            {"name": "a", "role": "r"}, {"name": "b", "role": "r"}, {"name": "c", "role": "r"}]),
        ev("unit_start", unit="a", role="r"),
        ev("unit_done", unit="a", role="r", ok=True, seconds=5.0),
        ev("unit_start", unit="b", role="r"),                 # b was mid-flight
    ])
    now = time.time()
    st = watch.run_state(pointer(kind="swarm", run_path=str(run), pid=dead.pid,
                                start=now - 60), now)
    assert st["state"] == "killed"
    assert st["end"] is None
    assert {n: u["state"] for n, u in st["units"].items()} == {
        "a": "done", "b": "not run", "c": "not run"}          # never ran, say so
    assert st["elapsed"] == pytest.approx(60, abs=1)


def test_agent_progress_is_elapsed_over_timeout(tmp_path):
    now = time.time()
    st = watch.run_state(pointer(kind="agent", start=now - 40, timeout=100), now)
    assert st["state"] == "running"
    assert st["progress"] == pytest.approx(0.4)
    assert st["eta"] is None
    assert st["elapsed"] == pytest.approx(40, abs=1)
    st = watch.run_state(pointer(kind="agent", start=now - 250, timeout=100), now)
    assert st["progress"] == 1.0                              # capped, never above 1
    assert watch.run_state(pointer(kind="agent", start=now - 40), now)["progress"] is None


def test_until_done_rounds_from_filenames(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    run.mkdir()
    for name in ("round-1.prompt.md", "round-2.prompt.md", "round-10.prompt.md"):
        (run / name).write_text("a prompt body the page may never show", encoding="utf-8")
    for name in ("round-1.json", "round-2.json", "goal.md"):  # not the round-file pattern
        (run / name).write_text("{}", encoding="utf-8")
    (run / "report.md").write_text(
        "# until-done report\n\nstop: goal reached (exit 0)\nrounds: 3\n", encoding="utf-8")
    rid = watch.register("until-done", "the loop", run_path=str(run), max_rounds=4,
                         start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["progress"] == pytest.approx(3 / 4)             # counted by FILENAME only
    assert st["rounds"] == 3 and st["eta"] is None
    assert st["report_stop"] == "stop: goal reached (exit 0)"
    # a stop: line beyond the first 20 lines is not read at all
    (run / "report.md").write_text("x\n" * 21 + "stop: late (exit 0)\n", encoding="utf-8")
    assert watch.run_detail(rid, time.time())["report_stop"] is None


# ---------------------------------------------------------------- the viewer

def test_url_has_slash_before_hash(capsys, monkeypatch):
    """The link every launcher prints is http://HOST:PORT/#<id> -- the slash BEFORE the
    hash, so a browser loads the page and the hash then selects the run (a url that
    opened with a bare `#id` would fetch nothing). With no id it is the plain page url,
    and a comma list in QWEN_WATCH_HOST puts the FIRST address in both."""
    monkeypatch.setenv("QWEN_WATCH_HOST", "127.0.0.1,127.0.0.2")
    monkeypatch.setenv("QWEN_WATCH_PORT", "8791")
    assert watch.url("ab12cd34ef56") == "http://127.0.0.1:8791/#ab12cd34ef56"
    assert watch.url() == "http://127.0.0.1:8791/"
    monkeypatch.setenv("QWEN_WATCH_HOST", " 127.0.0.1 , 127.0.0.2 ")   # padded list:
    assert watch.url("ab12cd34ef56") == "http://127.0.0.1:8791/#ab12cd34ef56"   # trimmed
    assert watch.main(["url", "ab12cd34ef56"]) == 0      # the CLI prints the same link
    assert capsys.readouterr().out == "http://127.0.0.1:8791/#ab12cd34ef56\n"
    assert watch.main(["url"]) == 0
    assert capsys.readouterr().out == "http://127.0.0.1:8791/\n"


def test_server_health_and_api(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev("run_start", workflow="swarm", goal="g"),
                       ev("phase_start", label="build", n=1, seats=1,
                          units=[{"name": "a", "role": "r"}]),
                       ev("attention", item="a", reason="retrying", detail="attempt 2")])
    rid = watch.register("swarm", "server run", run_path=str(run), start_server=False)
    httpd = watch.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp, resp.read()
        resp, body = get("/health")
        assert resp.status == 200
        assert json.loads(body) == {"app": "qwen-watch"}
        resp, body = get("/api/runs")
        runs = json.loads(body)["runs"]              # wrapped: {"runs": [...], "page": ...}
        assert [r["id"] for r in runs] == [rid]               # newest first, one run
        assert runs[0]["state"] == "running" and runs[0]["title"] == "server run"
        resp, body = get("/api/run/" + rid)
        detail = json.loads(body)
        assert detail["phase"]["label"] == "build" and detail["goal"] == "g"
        assert detail["attention"][0]["reason"] == "retrying"
        resp, body = get("/")
        assert resp.status == 200 and "text/html" in resp.headers.get("Content-Type", "")
        assert b"qwen watch" in body
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/nope", timeout=10)
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/run/deadbeefdead", timeout=10)
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_api_runs_carries_until_done_rounds(tmp_path, monkeypatch):
    """The overview card's "Round N of M" and the stop reason come straight from
    /api/runs: an until-done entry carries `rounds` -- the round-*.prompt.md FILENAME
    count, the same number run_detail computes -- and `report_stop`, report.md's stop:
    line; a run of any other kind carries neither. And the response proves the read
    stayed on names: a round file's body appears nowhere in it."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "loop"
    run.mkdir()
    secret = "the prompt body, which the page may never read"
    for n in (1, 2, 10):
        (run / ("round-%d.prompt.md" % n)).write_text(secret, encoding="utf-8")
    (run / "round-1.json").write_text(secret, encoding="utf-8")   # not the pattern
    (run / "report.md").write_text("# until-done report\nstop: goal reached (exit 0)\n",
                                   encoding="utf-8")
    rid = watch.register("until-done", "the loop", run_path=str(run), max_rounds=4,
                         start_server=False)
    rid2 = watch.register("swarm", "not a loop", run_path=str(tmp_path / "swarm"),
                          start_server=False)
    rid3 = watch.register("until-done", "no folder yet", run_path=str(tmp_path / "gone"),
                          start_server=False)              # the loop never wrote a file
    httpd = watch.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/runs" % port,
                                    timeout=10) as resp:
            body = resp.read()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
    assert secret not in body.decode("utf-8", "replace")          # filename count only
    runs = {r["id"]: r for r in json.loads(body)["runs"]}     # wrapped: {"runs", "page"}
    assert runs[rid]["rounds"] == 3 == watch.run_detail(rid, time.time())["rounds"]
    assert runs[rid]["report_stop"] == "stop: goal reached (exit 0)"
    assert runs[rid3]["rounds"] == 0 and runs[rid3]["report_stop"] is None   # a missing
    # or vanished folder reads as zero rounds and no stop line, never as an error
    assert "rounds" not in runs[rid2] and "report_stop" not in runs[rid2]


def test_register_port_taken_warns_and_continues(tmp_path, monkeypatch, capsys):
    """The port is held by a foreign server (real probe against a junk listener), then
    nothing listens and the spawn fails: one `watch:` line each time, the id back, and
    the pointer on disk either way."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    monkeypatch.delenv("QWEN_WATCH_NOSERVE", raising=False)
    spawned = []

    def fake_spawn():                                        # stand in for the detached child
        spawned.append(True)
        raise OSError(98, "Address already in use")          # it cannot bind: the port is taken

    monkeypatch.setattr(watch, "_spawn_server", fake_spawn)
    monkeypatch.setattr(watch, "HEALTH_TIMEOUT", 5.0)   # never let a loaded box time out
    junk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    junk.bind(("127.0.0.1", 0))                              # some other server owns a port
    junk.listen(8)
    junk_port = junk.getsockname()[1]                # snapshot: the finally closes junk
    monkeypatch.setattr(watch, "watch_port", lambda: junk_port)
    stop = threading.Event()

    def serve_junk():
        while not stop.is_set():
            try:
                conn, _ = junk.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
                except OSError:
                    pass

    thread = threading.Thread(target=serve_junk, daemon=True)
    thread.start()
    try:
        rid = watch.register("swarm", "t", run_path=str(tmp_path / "run"))
        assert rid is not None and read_pointer(rid)["kind"] == "swarm"
        assert spawned == []                                 # nothing to spawn: the port speaks
        err = capsys.readouterr().err
        assert len(err.splitlines()) == 1 and err.startswith("watch:")
    finally:
        stop.set()
        try:
            # Unstick the accept() the thread is parked in: closing a listening
            # socket under a blocked accept() is unspecified on Linux, and a
            # listener that outlives close() would answer the next probe too.
            wake = socket.create_connection(("127.0.0.1", junk_port), timeout=5)
            wake.close()
        except OSError:
            pass                                        # already gone: the thread woke
        junk.close()
        thread.join(timeout=5)
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)     # the next run is another process
    rid2 = watch.register("research", "t2", run_path=str(tmp_path / "run2"))
    assert rid2 is not None and read_pointer(rid2)["kind"] == "research"
    assert spawned == [True]                                 # the spawn was attempted once
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and err.startswith("watch:")


# ----------------------------------------------------- the viewer's own lifetime

def test_serve_default_never_idles(monkeypatch):
    """The page is there when the user comes back: `serve`'s idle_exit defaults to 0
    (= never exit on idle), and with it no idle watcher is started at all -- the old
    self-exit thread would have closed a page that was only waiting for its reader.
    server.json covers the whole serve: written once bound, removed on the way out."""
    assert inspect.signature(watch.serve).parameters["idle_exit"].default == 0
    seen = {}

    class Stub:                                          # a server that serves nothing
        server_address = ("127.0.0.1", 4321)

        def serve_forever(self):
            seen["idle_threads"] = [t for t in threading.enumerate() if t not in before]
            seen["recorded"] = os.path.exists(watch._server_path())
            seen["record"] = watch.server_info()

        def server_close(self):
            seen["closed"] = True

    def fake_make_server(host, port, idle_exit=None):
        seen["idle_exit"] = idle_exit
        return Stub()

    before = set(threading.enumerate())
    monkeypatch.setattr(watch, "make_server", fake_make_server)
    watch.serve("127.0.0.1", 0)                          # the default: no idle timer
    assert seen["idle_exit"] == 0
    assert seen["idle_threads"] == []                    # nothing ticking toward an exit
    assert seen["recorded"] is True                      # server.json while it serves...
    rec = seen["record"]                                 # ...exactly as `stop` reads it
    assert set(rec) == {"pid", "host", "port", "start"}
    assert rec["pid"] == os.getpid() and rec["host"] == "127.0.0.1" and rec["port"] == 4321
    assert isinstance(rec["start"], float) and rec["start"] <= time.time()
    assert seen["closed"] is True
    assert not os.path.exists(watch._server_path())      # ...and gone after a clean exit


def test_make_server_never_resolves_its_host_name(monkeypatch):
    """The viewer may never ask the resolver a thing: HTTPServer.server_bind calls
    socket.getfqdn(host), a reverse DNS lookup that hangs a macOS runner (and any
    machine with slow DNS) so long that no server.json is EVER written and the run's
    health probe waits forever. With getfqdn rigged to raise, make_server still
    binds -- and names itself from the host it was given and the port it got."""
    def no_lookup(*args, **kwargs):
        raise AssertionError("the viewer must never resolve its host name")
    monkeypatch.setattr(socket, "getfqdn", no_lookup)
    httpd = watch.make_server("127.0.0.1", 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1" and httpd.server_address[1] > 0
        assert httpd.server_name == "127.0.0.1"           # the host string, not a lookup
        assert httpd.server_port == httpd.server_address[1]   # the port ACTUALLY bound
    finally:
        httpd.server_close()


def test_serve_binds_every_listed_address():
    """QWEN_WATCH_HOST may be a comma-separated list, and `serve` binds EVERY address
    on the same port in ONE process: each answers /health, the single live process is
    the one server.json names, and the record carries the FIRST address. macOS loopback
    holds only 127.0.0.1 by default, so the test first probes whether 127.0.0.2 can be
    bound at all: when that probe fails the address is not ours to assign, and the test
    asserts instead that 127.0.0.1 serves, server.json names it, and the viewer printed
    exactly one `watch:` warning naming 127.0.0.2."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))                      # a port nothing else holds
    port = listener.getsockname()[1]
    listener.close()
    bind_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bind_probe.bind(("127.0.0.2", 0))            # probe: is 127.0.0.2 bindable
        can_bind_002 = True                          # here at all? (macOS loopback:
    except OSError:                                  # 127.0.0.1 only by default)
        can_bind_002 = False
    finally:
        bind_probe.close()
    proc = subprocess.Popen([sys.executable, WATCH_PY, "serve", "--host",
                             "127.0.0.1,127.0.0.2", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            encoding="utf-8")
    record = os.path.join(watch.watch_dir(), watch.SERVER_FILE)
    try:
        deadline = time.time() + 30
        while not os.path.exists(record):
            if proc.poll() is not None:
                pytest.fail("the viewer exited with %s: %s"
                            % (proc.returncode, proc.stderr.read()))
            if time.time() > deadline:
                pytest.fail("no %s after 30 s" % record)
            time.sleep(0.05)
        for host in ("127.0.0.1", "127.0.0.2") if can_bind_002 else ("127.0.0.1",):
            with urllib.request.urlopen("http://%s:%d/health" % (host, port),
                                        timeout=10) as resp:
                assert json.loads(resp.read()) == {"app": "qwen-watch"}, host
        info = watch.server_info()
        assert info["pid"] == proc.pid                    # ONE process, both addresses
        assert info["host"] == "127.0.0.1" and info["port"] == port   # the first one
        if can_bind_002 and os.path.isdir("/proc"):       # one server thread per address
            assert len(os.listdir("/proc/%d/task" % proc.pid)) >= 2   # the caller's own
            # serve_forever plus the extra address's thread, all in this one process
        if not can_bind_002:
            # the address we could not bind is exactly what the page lost, and no more:
            # stop first (a closed stderr is the complete warning record), then name it
            watch.main(["stop"])
            err = proc.stderr.read()
            proc.wait(timeout=10)
            warn = [ln for ln in err.splitlines() if ln.startswith("watch:")]
            assert len(warn) == 1 and "127.0.0.2" in warn[0], err
    finally:
        if proc.poll() is None:
            watch.main(["stop"])                          # the documented way down
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        proc.stderr.close()


def test_serve_survives_one_bad_address(monkeypatch, capsys):
    """An address that cannot be bound (not ours to assign) costs exactly one
    `watch:` warning naming it, and the others still serve: the good address answers
    /health, server.json names it, and the record is cleaned on the way out. When
    NOTHING binds, `serve` is the thing that fails -- it returns non-zero, and no
    record is left behind."""
    real = watch.make_server
    made = []
    bad = "192.0.2.1"                                     # TEST-NET-1: nobody may bind it
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def fake(host, p, idle_exit=0):
        if host == bad:
            raise OSError(99, "Cannot assign requested address")
        srv = real(host, p, idle_exit=idle_exit)
        made.append(srv)
        return srv

    monkeypatch.setattr(watch, "make_server", fake)
    capsys.readouterr()                                   # start from a clean stderr
    thread = threading.Thread(target=watch.serve, args=("%s,127.0.0.1" % bad, port),
                              daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while watch.server_info() is None:
            if not thread.is_alive() or time.time() > deadline:
                pytest.fail("serve never recorded a bound address")
            time.sleep(0.05)
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                    timeout=10) as resp:
            assert json.loads(resp.read()) == {"app": "qwen-watch"}   # the good one serves
        info = watch.server_info()
        assert info["host"] == "127.0.0.1"                  # the first BOUND address
        err = capsys.readouterr().err
        lines = [ln for ln in err.splitlines() if ln.startswith("watch:")]
        assert len(lines) == 1 and bad in lines[0]          # one line, naming the loser
    finally:
        for srv in made:
            srv.shutdown()                                  # ends serve's serve_forever
        thread.join(timeout=10)
    assert watch.server_info() is None                      # the record goes with it

    def all_bad(host, p, idle_exit=0):
        raise OSError(99, "Cannot assign requested address")

    monkeypatch.setattr(watch, "make_server", all_bad)
    assert watch.serve("%s,192.0.2.2" % bad, port) != 0     # none binds: serve fails...
    assert watch.main(["serve", "--host", "%s,192.0.2.2" % bad,   # ...and so does the CLI
                       "--port", str(port)]) != 0              # (watch.py exits on it)
    assert watch.server_info() is None                      # and recorded nothing
    # a list of nothing but separators is no list at all: one `watch:` line, non-zero,
    # and make_server is never even asked
    assert watch.serve(" , , ", port) != 0
    assert [ln for ln in capsys.readouterr().err.splitlines() if "no address" in ln]


def test_stop_signals_pid_from_server_json(capsys):
    """The viewer runs as its own process on a real port; `watch.py stop` finds it through
    server.json alone, SIGTERMs that pid, and it is gone within 5 s with the record gone."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))                       # a port nothing else holds
    port = listener.getsockname()[1]
    listener.close()                                      # hand it to the viewer
    proc = subprocess.Popen([sys.executable, WATCH_PY, "serve", "--host", "127.0.0.1",
                             "--port", str(port)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, encoding="utf-8")
    record = os.path.join(watch.watch_dir(), watch.SERVER_FILE)
    try:
        deadline = time.time() + 30
        while not os.path.exists(record):
            if proc.poll() is not None:
                pytest.fail("the viewer exited with %s: %s"
                            % (proc.returncode, proc.stderr.read()))
            if time.time() > deadline:
                pytest.fail("no %s after 30 s" % record)
            time.sleep(0.05)
        info = watch.server_info()
        assert info["pid"] == proc.pid                     # the file names the live process
        assert info["host"] == "127.0.0.1" and info["port"] == port
        assert isinstance(info["start"], float) and info["start"] <= time.time()
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=10) as resp:
            assert json.loads(resp.read()) == {"app": "qwen-watch"}     # the page answers
        assert watch.main(["stop"]) == 0
        assert capsys.readouterr().out.strip() == "watch: stopped"
        proc.wait(timeout=5)                               # SIGTERM got through to it
        assert not os.path.exists(record)                  # stop took the record with it
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stderr.close()


def test_stop_when_not_running(capsys, monkeypatch):
    """Nothing to stop -- no record, a record naming a process that already exited, a
    record that does not read, a record whose pid is not a pid, a live pid we may not
    signal: each says `watch: not running`, exits 0, and clears the stale file so the
    next run is not told to leave a dead viewer alone."""
    assert watch.main(["stop"]) == 0                       # never served: not an error
    assert capsys.readouterr().out.strip() == "watch: not running"
    assert not os.path.exists(watch._server_path())        # nothing was invented

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    try:
        os.kill(dead.pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.skip("pid %d was reused, it is not dead" % dead.pid)
    for junk in ({"pid": dead.pid, "host": "127.0.0.1", "port": 8790, "start": time.time()},
                 {"pid": 2 ** 40},                          # not a pid at all: OverflowError
                 {"pid": "not a pid"}, {"host": "127.0.0.1"}):
        watch._write_json(watch._server_path(), junk)
        assert watch.main(["stop"]) == 0, junk
        assert capsys.readouterr().out.strip() == "watch: not running", junk
        assert not os.path.exists(watch._server_path()), junk     # the stale file is cleared
    with open(watch._server_path(), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")                       # a half-written record
    assert watch.main(["stop"]) == 0
    assert capsys.readouterr().out.strip() == "watch: not running"
    assert not os.path.exists(watch._server_path())

    # and a record naming a live process this command may not end: EPERM from os.kill (a
    # pid reused by another user, a hand-written record, a platform that signals no one).
    # `stop` cannot stop it either, and a server.json we can neither act on nor own is as
    # stale as a dead one -- it goes, so the next register brings up a viewer it can stop.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_kill = os.kill                                      # what the finally puts back
    try:
        os.kill(other.pid, 0)                                # the real kill: the pid IS alive
        assert other.poll() is None

        seen = {}

        def refusing(pid, sig):                              # ... we simply may not signal it
            seen["call"] = (pid, sig)
            raise PermissionError("Operation not permitted")
        monkeypatch.setattr(os, "kill", refusing)
        watch._write_json(watch._server_path(), {"pid": other.pid, "host": "127.0.0.1",
                                                 "port": 8790, "start": time.time()})
        assert watch.main(["stop"]) == 0
        assert capsys.readouterr().out.strip() == "watch: not running"
        assert not os.path.exists(watch._server_path())       # the record is removed all the same
        assert seen.get("call") == (other.pid, signal.SIGTERM)   # it tried, then cleared: the
        # record is gone because stop reached the kill, not because it never looked
    finally:
        monkeypatch.setattr(os, "kill", real_kill)           # the real kill comes back, for
        other.kill()                                         # the kill this cleanup itself
        other.wait(timeout=10)                               # has to send


def test_conftest_disables_watch():
    """The pollution fix: tests/conftest.py puts QWEN_WATCH=0 AND QWEN_WATCH_NOSERVE=1
    around every test, so an end-to-end test that runs a real launcher never registers
    a run -- and a launcher whose environment still says the page is on never spawns
    the viewer into the developer's real watch dir or onto their port either."""
    assert os.environ["QWEN_WATCH"] == "0"
    assert os.environ["QWEN_WATCH_NOSERVE"] == "1"


# ------------------------------------------------------------------ the page

def page_text():
    """lib/watch_page.html as written: the one file the viewer serves at /."""
    with open(os.path.join(os.path.dirname(os.path.abspath(watch.__file__)),
                           "watch_page.html"), encoding="utf-8") as fh:
        return fh.read()


def test_page_has_no_external_urls():
    """The page is one self-contained file: no CDN, no font, no image, no analytics --
    nothing leaves this machine when the page loads. The simplest rule that proves it
    (comments included) is that no url of any kind appears in it at all."""
    text = page_text()
    assert "http://" not in text and "https://" not in text
    assert text.lstrip().lower().startswith("<!doctype html>")


def test_page_version_served_and_carried_by_both_api_routes(tmp_path, monkeypatch):
    """The version a long-open tab compares against: the first 12 hex digits of the
    sha256 of the watch_page.html bytes, counted BEFORE the placeholder replacement
    (the file on disk still carries `__PAGE_VERSION__`). The served HTML contains the
    version and holds no placeholder left, and BOTH API routes answer the same value
    at the top level: /api/runs wraps its list the way the page reads it (`runs`),
    /api/run/<id> carries `page` beside the run's own fields. And the page's source
    holds the reload logic and the sessionStorage guard that make the comparison pay
    off -- a tab whose PAGE_VERSION differs from the server's reloads itself once."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev("run_start", workflow="swarm", goal="g")])
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    with open(os.path.join(os.path.dirname(os.path.abspath(watch.__file__)),
                           "watch_page.html"), "rb") as fh:
        version = hashlib.sha256(fh.read()).hexdigest()[:12]
    assert len(version) == 12 and all(c in "0123456789abcdef" for c in version)
    httpd = watch.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp, resp.read()
        resp, body = get("/")
        assert resp.status == 200 and "text/html" in resp.headers.get("Content-Type", "")
        assert b"__PAGE_VERSION__" not in body              # replaced at serve time
        assert ('var PAGE_VERSION="%s";' % version).encode("ascii") in body
        resp, body = get("/api/runs")
        listed = json.loads(body)
        assert listed["page"] == version                     # same version, top level
        assert [r["id"] for r in listed["runs"]] == [rid]    # the list, wrapped
        resp, body = get("/api/run/" + rid)
        assert json.loads(body)["page"] == version           # both routes agree
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
    text = page_text()                    # the page's own side of the bargain, as it
    assert "__PAGE_VERSION__" in text     # sits on disk: placeholder, reload, guard
    assert "var PAGE_VERSION=" in text
    assert "location.reload()" in text
    assert "sessionStorage" in text


def test_page_maps_every_builtin_role():
    """Every role a built-in workflow ships belongs to a family of the page's FAM
    table: a role the table forgot would turn up on the page as an anonymous grey
    robot, and that is exactly the kind of drift no one looks for until a run is
    half-grey. The new page maps the OTHER way round -- each family LISTS its roles
    (`roles:["planner",...]`) and famOf() searches them -- so membership in some
    family's roles list IS the mapping the old key-position check tested for."""
    roles = pathlib.Path(os.path.dirname(os.path.abspath(watch.__file__))) / "workflows"
    stems = sorted({p.stem for p in roles.glob("*/roles/*.md")})
    assert len(stems) >= 16, stems                             # the built-ins are there
    # only the FAM object counts: the page's other quoted words (KIND_FAM's keys, the
    # prop names) are not role-to-family mappings, and a FAMILY named like a role
    # (tester) maps nothing until its own roles list names the role
    text = page_text()
    at = text.index("var FAM={")
    block = text[at:text.index("\n};", at)]
    mapped = set()
    for lst in re.findall(r"roles:\[([^\]]*)\]", block):
        mapped |= set(re.findall(r'"([^"]+)"', lst))
    assert mapped                                              # the table is not empty
    for stem in stems:
        assert stem in mapped, "no family for the %s role" % stem


def test_run_headline_counts_the_current_phase_only():
    """The run view's `done of n` headline belongs to the CURRENT phase, like the bar
    next to it: fold["units"] accumulates every phase until run_start, so counting ALL
    of it against the current phase's n made a two-phase run (phase 1: n=3 all done;
    phase 2: n=2, one done) read "4 of 2". The page builds the headline by walking the
    current phase's OWN roster (ph.units) and dividing by that phase's own n -- never
    by a progress-times-n arithmetic on the server's number, and never by counting
    every unit the fold holds."""
    text = page_text()
    assert "var done=names.filter" not in text                 # never: all units / this n
    assert "d.progress*" not in text                           # never: progress x this n
    # the phase-scoped source: the done count walks ph.units only...
    assert "(ph.units||[]).forEach" in text
    # ...counts one per finished unit of THAT roster...
    assert ('(ph.units||[]).forEach(function(n){var u=us[n];'
            'if(u&&["done","cached","dropped"].indexOf(u.state)>=0)done++;});') in text
    # ...and the denominator is that phase's own n -- "1 agent", not "1 agents"
    assert '" of "+cnt(ph.n||0,"agent")+" done"' in text


def test_page_tolerates_junk_event_stamps():
    """recent is passed through from the events file verbatim, so an event's t may be
    NaN (json.dumps writes it) or an absurd number (a hand-built or third-party line):
    Date rejects |t| over 8.64e12 seconds and toTimeString() then THROWS, which would
    take the whole run view down for one junk event. Every stamp reaches the DOM
    through clock(), so the guard lives there: a t that is not a number, is not
    finite, or is out of Date's range renders as no time at all."""
    text = page_text()
    at = text.index("function clock(t)")
    line = text[at:text.index("\n", at)]                       # the guard, in the one
    # formatter every rendered stamp goes through
    assert "typeof t!==\"number\"" in line                     # not a number at all
    assert "!isFinite(t)" in line                              # NaN and Infinity cut
    assert "8.64e12" in line                                   # Date's range, both ways


def test_page_shows_the_one_session_its_activity_on_its_card():
    """The single agent's and the coder loop's own activity reaches BOTH places the
    page shows them: the run view's solo tile, and the agent row inside the overview
    card (the card itself says progress and time only -- no second status line, no
    second context meter, those facts live in the row). The row is built by agentRows
    from the run's own fold (r.activity) with the same actPose mapping and mood words
    the unit tiles use, so one agent reads the same on both screens."""
    text = page_text()
    overview = text[text.index("function overview(runs)"):text.index("function detail(r)")]
    assert "agentRows([r])" in overview                    # the run's own rows, in its card
    assert "actLine" not in overview and "ctxHtml" not in overview   # not twice: the row says it
    rows = text[text.index("function agentRows(live)"):text.index("function agentRowHtml")]
    assert 'actPose({state:"running",activity:a2})' in rows          # the session's own pose
    assert "moodWords(a2)" in rows and "r.activity" in rows          # its words, its own fold
    solo = text[text.index("function soloHtml(r,o)"):text.index("function evText(e)")]
    assert "actLine(fake)" in solo and "ctxHtml(a)" in solo          # the run view, likewise


def test_page_escapes_the_exit_code_it_puts_in_html():
    """An exit is DATA -- a hand-built events file can carry any JSON under it -- so
    every place that drops `r.exit` into HTML must run it through esc(String(r.exit)):
    the run view's status pill (painted into HTML UNSCAPED around it) and the finished
    row's right text alike; the `run_end` feed line has escaped its exit all along."""
    text = page_text()
    assert '"exit "+r.exit' not in text                     # no raw exit into HTML
    assert '"exit "+ r.exit' not in text
    assert text.count("esc(String(r.exit))") >= 2           # the pill word and the row
    detail = text[text.index("function detail(r)"):text.index("var CLS_WORD")]
    assert 'Failed (exit "+esc(String(r.exit))+")' in detail      # the status pill
    overview = text[text.index("function overview(runs)"):text.index("function detail(r)")]
    assert '"exit "+esc(String(r.exit))+", "' in overview         # the finished row
    feed = text[text.index("function evText(e)"):]
    assert 'esc(e.exit)' in feed                                   # unchanged: still so


def test_page_shows_the_coder_loop_step_on_its_tile():
    """Between rounds the coder tile is never blank: the loop's `step` drives it. Testing
    -- the tester robot running, "Running N checks", no round bar; Reviewing -- the
    checker robot, "Checking the diff against the task"; between, or a "done" activity
    (or none) while the loop still runs -- the builder thinking, "Between rounds". The
    review round titles itself, and the checklist spins while testing."""
    text = page_text()
    solo = text[text.index("function soloHtml(r,o)"):text.index("function evText(e)")]
    assert 'o==="running"&&r.kind==="until-done"' in solo     # the tile renders at all
    assert "FAM.tester" in solo and "FAM.checker" in solo      # robots of the hour
    assert '"Testing"' in solo and '"Reviewing"' in solo
    assert ">Between rounds</span>" in solo                     # never a blank tile
    assert '"Running "' in solo                                # "Running N checks"
    assert '"Checking the diff against the task"' in solo
    assert 'class="k test"' in solo                             # the tester-coloured key
    assert '" (review)"' in solo                                # the review round's title
    assert solo.count("bar(rs/r.round.timeout") == 1            # the bar: only while coding
    assert "Checklist, testing now" in solo                     # header while testing
    assert 'class="spin"' in solo                               # rows spin...
    assert '"passed last round"' in solo and '"unverified last round"' in solo
    assert "Check '+i+' of '" in solo                           # round 1 counts out the slots
    ov = text[text.index("function overview(runs)"):text.index("function detail(r)")]
    assert "agentRows([r])" in ov                                  # the loop's row, in its card
    assert '"Between rounds"' not in ov and 'class="k test"' not in ov  # the line lives in the row
    loop = text[text.index("function loopMood(r)"):text.index("function agentRows(live)")]
    assert "FAM.tester" in loop and "FAM.checker" in loop          # robots of the hour
    assert '"waiting between rounds"' in loop                      # the step's one word


def test_page_feed_sort_is_stable_within_one_stamp():
    """The activity feed is latest first, but same-stamp lines keep FILE order -- by an
    explicit index tie-break, not the engine's grace: a phase_start (or a checks_start,
    or an audit_start) must never land below the lines that followed it in the file."""
    text = page_text()
    detail = text[text.index("function detail(r)"):text.index("var CLS_WORD")]
    assert "{e:e,i:i}" in detail                                # each line keeps its index
    assert ".sort(function(x,y){return y.e.t-x.e.t||x.i-y.i;});" in detail


def test_page_prints_one_agent_and_one_check_in_the_singular():
    """Counts say "1 agent" and "1 check": one cnt() helper does the pluralising, and
    every agents/checks count goes through it -- the phase_start feed line, the run
    headline, the checks progress, and the new Testing lines -- while the fixed words of
    the review feed stay exactly as written."""
    text = page_text()
    assert 'function cnt(n,w){return esc(n)+" "+(n===1?w:w+"s");}' in text
    assert '" agents on"' not in text                           # no bare plurals left
    assert '" agents done"' not in text
    feed = text[text.index("function evText(e)"):]
    assert 'started with "+cnt(e.n,"agent")' in feed            # phase_start, singular now
    assert '"Testing round "+esc(e.round)+": running "+cnt(e.total,"check")' in feed
    assert '"Reviewing the diff against the task"' in feed
    assert '"Review: "' in feed and '"unlogged deviations found"' in feed
    assert '"reply unusable"' in feed                           # and "failed" for the rest
    assert '" (review round)"' in feed
    prog = text[text.index("function progressText(r)"):text.index("function etaText(r)")]
    assert 'cnt(c.total,"check")+" pass"' in prog               # "1 of 1 check pass"
    assert 'cnt(ph.n||0,"agent")+" done"' in prog               # "1 of 1 agent done"


def test_page_bounds_and_escapes_the_testing_checklist_total():
    """While testing before any checks line arrived, the page counts out "Check i of N"
    rows from the checks_start's `total` -- and a total is DATA a hand-built events line
    can set to anything: the loop must count to a bound of 50 (a bogus 1e11 renders at
    most fifty rows, in no time at all), the total reaches HTML only through esc(), and
    the counted-out row keeps its words."""
    text = page_text()
    solo = text[text.index("function soloHtml(r,o)"):text.index("function evText(e)")]
    assert "Math.min(Math.max(0, Math.floor(+r.step.total || 0)), 50)" in solo
    assert "i<=r.step.total" not in solo                        # the loop counts to the bound
    assert "' of '+esc(n)+" in solo                              # the total, only escaped
    assert "Check '+i+' of '" in solo                            # the words are unchanged


# ------------------------------------------------------------ the activity hook

def payload(event="PreToolUse", tool="Bash", cwd="/work", **over):
    """A Claude Code hook payload: the event, the tool it is about, the inputs it was
    called with, and the session's own directory. `transcript_path` is empty unless a
    test hands one over: the hook must cope with a session that names none."""
    p = {"hook_event_name": event, "session_id": "s", "cwd": cwd, "transcript_path": None}
    if tool is not None:
        p["tool_name"] = tool
        p["tool_input"] = over.pop("tool_input", {})
    p.update(over)
    return p


def line_of(p, now=1000.0, win=None):
    """The one line the hook would write for this payload, as the record it holds (the
    writing itself is the subprocess tests' business below)."""
    return json.loads(watch_hook.line_for(json.loads(json.dumps(p)), now, win))


def run_hook(tmp_path, text="{}", env=None):
    """The hook exactly as Claude Code runs it: the payload on stdin, the file to append
    to named by QWEN_WATCH_ACTIVITY. `text` may be BYTES, for a payload that is no text at
    all. Returns (exit, stdout, stderr, EVERY line the file holds now -- calls append, so a
    later call sees the earlier ones too)."""
    e = dict(os.environ)
    for name in ("QWEN_WATCH_ACTIVITY", "QWEN_WATCH_CTX_WINDOW"):
        e.pop(name, None)                                        # the dev's own never leaks in
    e.update(env or {})
    binary = isinstance(text, bytes)
    p = subprocess.run([sys.executable, HOOK_PY], input=text, env=e, capture_output=True,
                       text=not binary, encoding=None if binary else "utf-8")
    out, err = ((p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))
                if binary else (p.stdout, p.stderr))
    path = e.get("QWEN_WATCH_ACTIVITY")
    lines = []
    if path and os.path.isfile(path):              # isfile, not exists: the target may be a
        with open(path, encoding="utf-8") as fh:   # folder, which is the hook's problem, not
            lines = fh.read().splitlines()         # this helper's
    return p.returncode, out, err, lines


def test_hook_writes_one_line_per_tool_call(tmp_path):
    """The line is the five fields the fold reads and no more: the event, the tool, its
    trimmed input, whether it answered, and the time. A second call appends a second
    line; the file is the session's own (0600) and holds nothing else."""
    out = tmp_path / "a.jsonl"
    env = {"QWEN_WATCH_ACTIVITY": str(out)}
    ask = json.dumps(payload(tool="Bash", tool_input={"command": "git status"}))
    rc, so, se, lines = run_hook(tmp_path, ask, env=env)
    assert (rc, so, se) == (0, "", "")                            # silent, whatever happens
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"t", "ev", "tool", "target", "ok"}
    assert rec["ev"] == "pre" and rec["tool"] == "Bash"
    assert rec["target"] == "git status" and rec["ok"] is True
    assert 0 < rec["t"] <= time.time() + 1
    run_hook(tmp_path, json.dumps(payload("PostToolUse", tool="Bash",
                                          tool_input={"command": "git status"})), env=env)
    with open(out, encoding="utf-8") as fh:
        lines = fh.read().splitlines()                              # appended, never rewritten
    assert [json.loads(ln)["ev"] for ln in lines] == ["pre", "post"]
    if os.name == "posix":                                # Windows reports 0o666: NTFS
        assert oct(os.stat(out).st_mode & 0o777) == "0o600"   # has no POSIX file modes
    # the four events the page registers, and a SubagentStop as a stop too
    for event, want in (("PreToolUse", "pre"), ("PostToolUse", "post"),
                        ("PostToolUseFailure", "post"),
                        ("Stop", "stop"), ("SubagentStop", "stop")):
        rc, so, se, lines = run_hook(tmp_path, json.dumps(payload(event, tool=None)), env=env)
        assert (rc, so, se) == (0, "", "")
        assert json.loads(lines[-1])["ev"] == want, event
    # and a stop names no tool and no input: the agent stopped, that is the whole news
    rec = line_of(payload("Stop", tool="Bash", tool_input={"command": "x"}))
    assert rec["target"] is None and rec["ev"] == "stop" and rec["ok"] is True


def test_hook_target_is_the_trimmed_input(tmp_path, monkeypatch):
    """One trimmed INPUT per tool kind and nothing else: the start of a command with its
    newlines folded, a path inside the session's directory made relative to it (and one
    outside left where it is), a search pattern, a URL's HOST rather than its url, a
    query, a subagent type -- an mcp__ tool gets its own NAME (test below) -- and no
    target at all for a tool the hook does not know, or a call whose inputs are missing
    or are not an object. The caps are what keep a line short: 80 characters of a
    command, 60 of a pattern or a query."""
    cwd = str(tmp_path)
    inside = os.path.join(cwd, "src", "a.py")
    long_cmd = "python3 -m pytest tests/" + "x" * 120
    long_pat = "QWEN_" + "y" * 120
    cases = [
        (payload(tool="Bash", tool_input={"command": "ls -la\nsecond line\rthird"}),
         "ls -la second line third"),
        (payload(tool="Bash", tool_input={"command": long_cmd}), long_cmd[:80]),
        (payload(tool="Read", cwd=cwd, tool_input={"file_path": inside}),
         os.path.join("src", "a.py")),
        (payload(tool="Write", cwd=cwd, tool_input={"file_path": "/etc/hosts"}), "/etc/hosts"),
        (payload(tool="MultiEdit", cwd=cwd, tool_input={"file_path": cwd + "/b.py"}), "b.py"),
        (payload(tool="NotebookEdit", cwd=cwd, tool_input={"notebook_path": cwd + "/n.ipynb"}),
         "n.ipynb"),
        (payload(tool="Grep", tool_input={"pattern": long_pat}), long_pat[:60]),
        (payload(tool="Glob", tool_input={"pattern": "**/*.py"}), "**/*.py"),
        (payload(tool="WebFetch", tool_input={"url": "https://docs.example.com/a/b?k=v"}),
         "docs.example.com"),
        (payload(tool="WebSearch", tool_input={"query": "how to " + "z" * 120}),
         ("how to " + "z" * 120)[:60]),
        (payload(tool="Task", tool_input={"subagent_type": "Explore"}), "Explore"),
        (payload(tool="Agent", tool_input={"description": "no type given"}), None),
        (payload(tool="mcp__playwright__browser_click", tool_input={"target": "button"}),
         "mcp__playwright__browser_click"),                       # the tool names itself
        (payload(tool="TodoWrite", tool_input={"todos": [{"content": "a prompt body"}]}), None),
        (payload(tool="Bash", tool_input={"command": ""}), None),          # an empty command
        (payload(tool="Read", tool_input=None), None),                     # not an object
        (payload(tool=None), None),                                        # a bare Stop-ish ask
    ]
    for p, want in cases:
        rec = line_of(p)
        assert rec["target"] == want, p
        assert rec["ev"] == "pre"
        assert rec["tool"] == (p.get("tool_name") if "tool_name" in p else None)
    # nothing but an input is ever kept: neither a tool's answer nor a prompt body nor
    # the model's own words, however the payload is stuffed with them
    leak = payload(tool="Bash", tool_input={"command": "ls"},
                   tool_response={"stdout": "the whole file contents",
                                  "stderr": "a prompt body, in full"},
                   prompt="the prompt body", transcript="model words")
    assert "file contents" not in json.dumps(line_of(leak), ensure_ascii=False)
    assert "prompt body" not in json.dumps(line_of(leak), ensure_ascii=False)
    assert "model words" not in json.dumps(line_of(leak), ensure_ascii=False)
    # the path that cannot be made relative to the cwd -- on Windows, one on another
    # drive, where os.path.relpath answers ValueError -- is shown where it really is
    def no_relpath(a, b):
        raise ValueError("paths are on different drives")
    monkeypatch.setattr(watch_hook.os.path, "relpath", no_relpath)
    assert watch_hook._relative("/work/a.py", "/work") == "/work/a.py"
    assert line_of(payload(tool="Read", cwd="/work",
                           tool_input={"file_path": "/work/a.py"}))["target"] == "/work/a.py"
    # a relative path the payload already gives relative is kept as given, and a cwd of
    # no kind leaves every path alone
    assert watch_hook._relative("src/a.py", "/work") == "src/a.py"
    assert watch_hook._relative("/work/a.py", None) == "/work/a.py"
    assert watch_hook._relative("/work/a.py", "") == "/work/a.py"


def test_hook_target_names_the_mcp_tool(tmp_path):
    """The spec's "or the MCP tool name": a tool whose name starts with mcp__ gets its
    OWN NAME as target, capped at 60 characters -- its input has no shape this hook
    knows, so which MCP tool is running is the whole news (and still only an input:
    nothing of an answer, and none of the payload's fields, is kept). A tool without
    that prefix still gets None unless a known branch names its input."""
    assert watch_hook.target_for("mcp__db__query", {"sql": "select 1"}, "/w") \
        == "mcp__db__query"
    assert watch_hook.target_for("mcp__a__b", None, "/w") == "mcp__a__b"
    assert watch_hook.target_for("mcp__a__b", {}, "/w") == "mcp__a__b"
    long_name = "mcp__srv__" + "y" * 100                 # capped like a pattern is
    assert watch_hook.target_for(long_name, {}, "/w") == long_name[:60]
    assert len(watch_hook.target_for(long_name, {}, "/w")) == 60
    assert watch_hook.target_for("mcp", {"a": 1}, "/w") is None          # no prefix
    assert watch_hook.target_for("mcp_", {"a": 1}, "/w") is None         # not even
    assert watch_hook.target_for("TodoWrite", {"todos": [1]}, "/w") is None
    assert watch_hook.target_for("Bash", None, "/w") is None             # as before
    assert watch_hook.target_for(None, None, "/w") is None
    # through the line builder...
    rec = line_of(payload(tool="mcp__playwright__browser_click",
                           tool_input={"ref": "e12"}))
    assert rec["target"] == "mcp__playwright__browser_click"
    assert rec["tool"] == "mcp__playwright__browser_click"
    # ...and through the hook exactly as Claude Code runs it
    out = tmp_path / "mcp.jsonl"
    rc, so, se, lines = run_hook(
        tmp_path, json.dumps(payload("PostToolUse", tool="mcp__db__query",
                                     tool_input={"sql": "SELECT secret FROM t"})),
        env={"QWEN_WATCH_ACTIVITY": str(out)})
    assert (rc, so, se) == (0, "", "") and len(lines) == 1
    got = json.loads(lines[0])
    assert got["target"] == "mcp__db__query" and got["ev"] == "post"
    assert "SELECT" not in lines[0] and "secret" not in lines[0]   # inputs stay inputs


def test_hook_marks_a_failed_call_not_ok(tmp_path):
    """`ok` is false ONLY when the PostToolUse payload's own error flags say so -- a tool
    whose answer mentions an error, or contains a traceback, is a call that worked. The
    flags it reads are is_error, then error, then a tool_response's is_error."""
    def ok_of(**over):
        return line_of(payload("PostToolUse", tool="Bash", **over))["ok"]

    assert ok_of() is True
    assert ok_of(is_error=False) is True
    assert ok_of(is_error=True) is False
    assert ok_of(error="the tool refused") is False
    assert ok_of(tool_response={"is_error": True}) is False
    assert ok_of(tool_response={"is_error": False, "stdout": "Traceback: error"}) is True
    assert ok_of(tool_response="error: everything failed") is True      # not even an object
    # a pre and a stop are never failed by these flags: nothing has come back yet
    assert line_of(payload("PreToolUse", is_error=True))["ok"] is True
    assert line_of(payload("Stop", is_error=True))["ok"] is True


def test_hook_maps_a_post_tool_use_failure_to_a_failed_post(tmp_path):
    """A call that FAILED fires PostToolUseFailure, not PostToolUse -- and its payload is
    written as that call's post: the line that answers its `pre`, with `ok: false`
    whatever error flags it happens to carry (even `is_error: false`: the event itself is
    the report of the failure, flags cannot clear it). Without that event mapped, a failed
    call's pre would never get a post and the fold would read the tool as running forever."""
    assert watch_hook.EVENTS["PostToolUseFailure"] == "post"

    def ok_of(**over):
        return line_of(payload("PostToolUseFailure", tool="Bash", **over))["ok"]

    assert ok_of() is False                       # failed by the event alone...
    assert ok_of(is_error=False) is False         # ...whatever the flags say
    assert ok_of(error=None) is False
    assert ok_of(tool_response={"is_error": False, "stdout": "fine"}) is False
    # ...and it is still that call's post, with its input and all
    rec = line_of(payload("PostToolUseFailure", tool="Grep",
                          tool_input={"pattern": "SECRET"}))
    assert (rec["ev"], rec["tool"], rec["target"], rec["ok"]) == ("post", "Grep", "SECRET", False)
    # a failure whose payload names no tool at all (a hand-built one) is still a post:
    # the fold needs the ANSWER, and a junk tool_input is no target rather than a crash
    rec = line_of(payload("PostToolUseFailure", tool=None, tool_input="not an object"))
    assert (rec["ev"], rec["tool"], rec["target"], rec["ok"]) == ("post", None, None, False)
    # and the hook as Claude Code actually runs it appends exactly that line, answering
    # the pre the same run wrote before it
    out = tmp_path / "f.jsonl"
    env = {"QWEN_WATCH_ACTIVITY": str(out)}
    rc, so, se, lines = run_hook(tmp_path, json.dumps(payload(tool="Grep",
                                                   tool_input={"pattern": "SECRET"})),
                                 env=env)
    assert (rc, so, se) == (0, "", "")
    rc, so, se, lines = run_hook(tmp_path, json.dumps(
        payload("PostToolUseFailure", tool="Grep", tool_input={"pattern": "SECRET"},
                is_error=False, error="the tool refused")), env=env)
    assert (rc, so, se) == (0, "", "") and len(lines) == 2
    pre, post = (json.loads(ln) for ln in lines)
    assert pre["ev"] == "pre" and pre["ok"] is True
    assert set(post) == {"t", "ev", "tool", "target", "ok"}
    assert (post["ev"], post["tool"], post["target"], post["ok"]) == ("post", "Grep",
                                                                       "SECRET", False)


def test_hook_is_silent_and_exits_zero_without_a_page(tmp_path):
    """No QWEN_WATCH_ACTIVITY at all, a payload that is not JSON at all, a payload that is
    JSON but not the object Claude Code sends, an event the page does not track, a target
    that cannot be written (a folder in the way): in every case NOTHING is appended, the
    hook prints nothing on either stream and exits 0. A watcher that broke a session is a
    watcher someone removes the next morning."""
    rc, so, se, lines = run_hook(tmp_path, json.dumps(payload(tool="Bash")))
    assert (rc, so, se, lines) == (0, "", "", [])                  # nowhere to write: no file
    out = tmp_path / "a.jsonl"
    env = {"QWEN_WATCH_ACTIVITY": str(out)}
    for junk in ("", "   ", "not json at all", "[1,2,3]", "null", "3",
                 '{"hook_event_name":"Notification"}', '{"hook_event_name":null}', "{}"):
        rc, so, se, lines = run_hook(tmp_path, junk, env=env)
        assert (rc, so, se, lines) == (0, "", "", []), junk
    busy = tmp_path / "a-folder"                                   # a folder where the file
    busy.mkdir()                                                   # should be: no crash, no line
    rc, so, se = run_hook(tmp_path, json.dumps(payload(tool="Bash")),
                          env={"QWEN_WATCH_ACTIVITY": str(busy)})[:3]
    assert (rc, so, se) == (0, "", "")
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_hook_keeps_its_line_under_one_kilobyte(tmp_path):
    """One line per call, and never a line big enough to matter: `target` is the only
    field that can grow, so it gives first, one character at a time, and a record that
    still will not fit -- a tool name no target can shorten around -- is dropped whole
    rather than written long. Non-ASCII costs bytes, not characters, so the cap is on
    the encoded line."""
    out = tmp_path / "a.jsonl"
    wide = "ü" * 60
    rc, so, se, lines = run_hook(tmp_path, json.dumps(
        payload(tool="Glob", tool_input={"pattern": wide})),
        env={"QWEN_WATCH_ACTIVITY": str(out), "QWEN_WATCH_CTX_WINDOW": "262144"})
    assert (rc, so, se) == (0, "", "") and len(lines) == 1
    assert len(lines[0].encode("utf-8")) <= 1024
    assert json.loads(lines[0])["target"] == wide[:60]             # the pattern cap, first
    assert json.loads(lines[0])["win"] == 262144
    assert len(json.dumps(payload(tool="Bash",
                                  tool_input={"command": "echo " + "x" * 5000}))) > 1024
    rec = watch_hook.line_for({"hook_event_name": "PreToolUse", "tool_name": "x" * 2000,
                               "tool_input": {"command": "y" * 5000}, "cwd": "/w"}, 1.0, 7)
    assert rec is None or len(rec.encode("utf-8")) <= 1024         # short, or gone: never long


def transcript(path, *records):
    """A session's transcript file, one JSON object per line, as Claude Code writes it."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def assistant(usage, text="the model's own words, which must never be copied"):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}],
                                             "usage": usage}}


def test_hook_ctx_is_the_last_assistant_usage(tmp_path):
    """The context meter's number is the four usage counters of the LAST assistant message
    of the session's transcript that carries a usage object, added up -- an earlier
    message's numbers, a user message and a whole unreadable transcript give nothing, and
    a message without a usage object is passed over on the way back. Only the integer is
    kept: the message text is never returned, and never reaches the line the hook writes.
    A tail read means a long transcript still costs one bounded read, and the newest
    message is the only one that matters."""
    t = transcript(tmp_path / "t.jsonl",
                   assistant({"input_tokens": 100, "cache_read_input_tokens": 1000,
                              "cache_creation_input_tokens": 5, "output_tokens": 20}),
                   {"type": "user", "message": {"role": "user", "content": "the prompt body"}},
                   assistant({"input_tokens": 200, "cache_read_input_tokens": 2000,
                              "cache_creation_input_tokens": 6, "output_tokens": 30}))
    assert watch_hook.ctx_from_transcript(str(t)) == 2236          # the LAST one, not the first
    # an assistant message with no usage object is stepped over; a usage that holds no
    # number at all is no number (and not a zero the meter would show as an empty bar)
    transcript(tmp_path / "u.jsonl", assistant({"input_tokens": 5}),
               {"type": "assistant", "message": {"role": "assistant", "content": []}})
    assert watch_hook.ctx_from_transcript(str(tmp_path / "u.jsonl")) == 5
    transcript(tmp_path / "z.jsonl", assistant({"input_tokens": 5}),
               assistant({"total_tokens": "9999"}))                  # the newest usage holds
    assert watch_hook.ctx_from_transcript(str(tmp_path / "z.jsonl")) is None  # no number: none
    transcript(tmp_path / "n.jsonl", assistant({"usage": None}))
    assert watch_hook.ctx_from_transcript(str(tmp_path / "n.jsonl")) is None
    # absent, a folder, a name that is not a path, an empty name: the same nothing
    assert watch_hook.ctx_from_transcript(str(tmp_path / "nope.jsonl")) is None
    assert watch_hook.ctx_from_transcript(str(tmp_path)) is None
    assert watch_hook.ctx_from_transcript(None) == watch_hook.ctx_from_transcript("")
    junk = tmp_path / "j.jsonl"
    junk.write_text("x\n[]\n{\"broken\nnull\n", encoding="utf-8")
    assert watch_hook.ctx_from_transcript(str(junk)) is None
    # a transcript whose last line is only part of a line (the hook appends as it is read)
    # does not hide the complete message above it
    transcript(tmp_path / "h.jsonl", assistant({"input_tokens": 7, "output_tokens": 3}))
    with open(tmp_path / "h.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","message":{"ro')
    assert watch_hook.ctx_from_transcript(str(tmp_path / "h.jsonl")) == 10
    # the number reaches the line; the words never do
    rec = line_of(payload(tool="Read", cwd=str(tmp_path), transcript_path=str(t),
                          tool_input={"file_path": os.path.join(str(tmp_path), "a.py")}))
    assert rec["ctx"] == 2236
    assert "model's own words" not in json.dumps(rec)
    assert "prompt body" not in json.dumps(rec)
    # a window only when the launcher knew one, and an unusable one is not a window
    assert line_of(payload(tool="Bash", transcript_path=str(t)), win=8192)["win"] == 8192
    assert "win" not in line_of(payload(tool="Bash", transcript_path=str(t)), win=None)
    assert "ctx" in line_of(payload(tool="Bash", transcript_path=str(t)), win=None)


def test_hook_ctx_absent_leaves_the_plain_line(tmp_path):
    """An unreadable or missing transcript costs the meter and nothing else: the line is
    still written, still exactly the five fields, and a QWEN_WATCH_CTX_WINDOW that is not
    a number is no window at all -- the page then shows no meter."""
    out = tmp_path / "a.jsonl"
    rc, so, se, lines = run_hook(tmp_path, json.dumps(
        payload(tool="Bash", tool_input={"command": "true"},
                transcript_path=str(tmp_path / "no-such-transcript"))),
        env={"QWEN_WATCH_ACTIVITY": str(out), "QWEN_WATCH_CTX_WINDOW": "131072"})
    assert (rc, so, se) == (0, "", "") and len(lines) == 1
    rec = json.loads(lines[0])
    assert "ctx" not in rec and rec["win"] == 131072
    for bad in ("", "abc", "0", "-4", "1.5", "1e9x"):
        rc, so, se, lines = run_hook(tmp_path, json.dumps(payload(tool="Bash")),
                                     env={"QWEN_WATCH_ACTIVITY": str(out),
                                          "QWEN_WATCH_CTX_WINDOW": bad})
        assert (rc, so, se) == (0, "", "")
        assert "win" not in json.loads(lines[-1]), bad


def test_hook_survives_input_that_is_no_text_at_all(tmp_path):
    """The payload arrives as BYTES from Claude Code, and bytes are not always text: a
    stdin that is not valid UTF-8 at all, a command carrying one byte that cannot be
    decoded, a transcript holding the same, and an activity file whose own path has a
    space and a non-ASCII letter in it. None of them crash, none of them print, all of
    them exit 0 -- and an input that is still readable is still recorded, replacement
    character included. This is the hook Claude Code would otherwise uninstall."""
    out = tmp_path / "a.jsonl"
    env = {"QWEN_WATCH_ACTIVITY": str(out)}
    rc, so, se, lines = run_hook(tmp_path, b"\xff\xfe{\x00 not json at all", env=env)
    assert (rc, so, se, lines) == (0, "", "", [])                   # not text: nothing written
    rc, so, se, lines = run_hook(tmp_path, "é".encode("utf-8"), env=env)
    assert (rc, so, se, lines) == (0, "", "", [])                   # text, but not JSON
    # one undecodable byte inside an otherwise fine payload: it becomes U+FFFD and the
    # rest of the input is still worth a line
    raw = (b'{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"/w",'
           b'"tool_input":{"command":"cat \xff/data"}}')
    rc, so, se, lines = run_hook(tmp_path, raw, env=env)
    assert (rc, so, se) == (0, "", "") and len(lines) == 1
    assert json.loads(lines[0])["target"] == "cat �/data"
    # a lone surrogate -- what a non-UTF-8 argv byte becomes once it is in JSON -- is kept
    # as it came, escaped; the line stays valid JSON under the cap. Two calls, two lines:
    # the hook appends, it never rewrites what the session already wrote.
    rc, so, se, lines = run_hook(tmp_path, json.dumps(
        payload(tool="Glob", tool_input={"pattern": "\ud800tail"})), env=env)
    assert (rc, so, se) == (0, "", "") and len(lines) == 2
    assert json.loads(lines[-1])["target"] == "\ud800tail"
    assert json.loads(lines[0])["target"] == "cat �/data"     # appended, never rewritten
    # a transcript that is not text: the bad line is skipped, the good one below it in
    # the scan still gives the number, and no exception escapes
    raw_t = tmp_path / "t.bin"
    raw_t.write_bytes(b'{"message":{"role":"assistant","usage":{"input_tokens":4,'
                      b'"output_tokens":6}}}\n{"message":{"role":"assistant",'
                      b'"usage":{"input_tokens":\xff9}}}\n')
    assert watch_hook.ctx_from_transcript(str(raw_t)) == 10
    # the activity file's OWN path: a space and a non-ASCII directory name are a path.
    # The folder is whoever made the run's business (the launcher, the swarm): the hook
    # only appends to the file named for it, and a file whose folder was never made is
    # the same nothing it always was.
    (tmp_path / "a dir" / "ünïcode").mkdir(parents=True)
    odd = str(tmp_path / "a dir" / "ünïcode" / "the activity.jsonl")
    rc, so, se, lines = run_hook(tmp_path, json.dumps(
        payload(tool="Bash", tool_input={"command": "ls"})),
        env={"QWEN_WATCH_ACTIVITY": odd})
    assert (rc, so, se) == (0, "", "") and len(lines) == 1
    assert json.loads(lines[0])["target"] == "ls"
    blocked = str(tmp_path / "a.jsonl" / "deeper.jsonl")            # a.jsonl is a FILE
    rc, so, se, lines = run_hook(tmp_path, json.dumps(payload(tool="Bash")),
                                 env={"QWEN_WATCH_ACTIVITY": blocked})
    assert (rc, so, se) == (0, "", "") and lines == []
    assert not os.path.exists(blocked)


def test_hook_ctx_reads_only_the_tail_of_a_huge_transcript(tmp_path):
    """A session's transcript grows without bound and the hook runs on every tool call, so
    the ctx scan reads the last TAIL_BYTES and no more: the newest message is in that tail
    and is still found past a line far bigger than the window, while a message pushed out
    of the tail is simply gone -- and the fold then keeps the last figure the hook did
    read, which is the whole point of reading only a tail."""
    big = "x" * (watch_hook.TAIL_BYTES + 40000)
    newest = transcript(tmp_path / "new.jsonl", {"filler": big},
                        assistant({"input_tokens": 11, "output_tokens": 1}))
    assert os.path.getsize(str(newest)) > watch_hook.TAIL_BYTES
    # the tail starts mid-line inside the giant one: that fragment is junk, the newest
    # complete message after it is the answer
    assert watch_hook.ctx_from_transcript(str(newest)) == 12
    old = transcript(tmp_path / "old.jsonl", assistant({"input_tokens": 11, "output_tokens": 1}),
                     {"filler": big})
    assert os.path.getsize(str(old)) > watch_hook.TAIL_BYTES
    assert watch_hook.ctx_from_transcript(str(old)) is None          # out of the tail, out of scope


# ------------------------------------------------------------------ the fold

def act(ev, t, tool=None, target=None, ok=True, **more):
    """One activity line as the hook writes it."""
    return dict(more, t=t, ev=ev, tool=tool, target=target, ok=ok)


def act_lines(path, records):
    """Write one agent's activity file and hand the path back."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_activity_fold_follows_one_agent_through_its_states(tmp_path):
    """The four states a card shows, and what each one names: a call running (under
    WAIT_AFTER seconds of it), the same call long enough to read as waiting, the model
    composing between calls, and the agent stopped. `since` is when the CURRENT state
    began -- the call's own pre, the answer's post, the stop -- and tool, target and cls
    name what is running now, and are none of the three once it stopped."""
    p = act_lines(tmp_path / "a.jsonl", [
        act("pre", 100.0, tool="Bash", target="pytest -q"),
        act("post", 104.0, tool="Bash", target="pytest -q"),
        act("pre", 110.0, tool="Read", target="src/a.py")])
    f = watch.activity_fold(str(p), 112.0)
    assert f["state"] == "tool" and f["since"] == 110.0
    assert (f["tool"], f["target"], f["cls"]) == ("Read", "src/a.py", "read")
    assert f["calls"][-1] == {"tool": "Bash", "target": "pytest -q", "seconds": 4.0,
                              "ok": True, "cls": "python"}          # a test run is python
    f = watch.activity_fold(str(p), 125.0)                            # 15 s: waiting now
    assert f["state"] == "waiting" and f["since"] == 110.0 and f["cls"] == "read"
    p2 = act_lines(tmp_path / "b.jsonl", [
        act("pre", 100.0, tool="Bash", target="ls"),
        act("post", 101.0, tool="Bash", target="ls")])
    f = watch.activity_fold(str(p2), 109.0)              # answered, nothing open: thinking
    assert f["state"] == "thinking" and f["since"] == 101.0
    assert f["tool"] is None and f["target"] is None and f["cls"] is None
    p3 = act_lines(tmp_path / "c.jsonl", [
        act("pre", 100.0, tool="Bash", target="ls"),
        act("post", 101.0, tool="Bash", target="ls"),
        act("stop", 103.0)])
    f = watch.activity_fold(str(p3), 200.0)                             # and then done
    assert f["state"] == "done" and f["since"] == 103.0
    assert f["tool"] is None and f["target"] is None and f["cls"] is None
    # a call that is still open when a stop arrives cannot keep the card working
    p4 = act_lines(tmp_path / "d.jsonl", [act("pre", 100.0, tool="Bash", target="sleep 100"),
                                          act("stop", 105.0)])
    assert watch.activity_fold(str(p4), 400.0)["state"] == "done"


def test_activity_fold_pairs_answers_with_their_calls_and_keeps_ten(tmp_path):
    """A finished call is a pre and its post: its seconds are the gap between them, its
    ok is the answer's, and the list the card offers is the last ten, oldest first. Two
    calls of one tool open at once close last-in-first-out, so each answer pays for its
    own ask; an answer that never had an ask is still a call, with no seconds to report."""
    recs = []
    for i in range(12):
        recs += [act("pre", 100.0 + 10 * i, tool="Bash", target="cmd%d" % i),
                 act("post", 103.0 + 10 * i, tool="Bash", target="cmd%d" % i, ok=(i != 7))]
    f = watch.activity_fold(str(act_lines(tmp_path / "a.jsonl", recs)), 220.0)
    assert [c["target"] for c in f["calls"]] == ["cmd%d" % i for i in range(2, 12)]
    assert [c["ok"] for c in f["calls"]] == [i != 7 for i in range(2, 12)]
    assert all(c["seconds"] == 3.0 for c in f["calls"]) and f["tools_s"] == 36.0
    assert all(set(c) == {"tool", "target", "seconds", "ok", "cls"} for c in f["calls"])
    nested = act_lines(tmp_path / "b.jsonl", [
        act("pre", 10.0, tool="Agent", target="Explore"),
        act("pre", 11.0, tool="Read", target="a.py"),
        act("post", 12.0, tool="Read", target="a.py"),
        act("post", 20.0, tool="Agent", target="Explore")])
    f = watch.activity_fold(str(nested), 21.0)
    assert [(c["tool"], c["seconds"]) for c in f["calls"]] == [("Read", 1.0), ("Agent", 10.0)]
    orphan = act_lines(tmp_path / "c.jsonl", [act("post", 10.0, tool="Bash", target="ls")])
    f = watch.activity_fold(str(orphan), 11.0)
    assert f["calls"] == [{"tool": "Bash", "target": "ls", "seconds": None, "ok": True,
                           "cls": "shell"}]
    assert f["state"] == "thinking"                       # nothing is open after a lone post
    # a long session: five thousand calls is still one fold, still the last ten, and the
    # time split still adds up over all of them -- the page polls this every refresh
    many = []
    for i in range(5000):
        many += [act("pre", 1000.0 + 2 * i, tool="Read", target="f%d.py" % i),
                 act("post", 1001.0 + 2 * i, tool="Read", target="f%d.py" % i)]
    big = watch.activity_fold(str(act_lines(tmp_path / "big.jsonl", many)), 20000.0)
    assert len(big["calls"]) == 10
    assert [c["target"] for c in big["calls"]] == ["f%d.py" % i for i in range(4990, 5000)]
    assert big["state"] == "thinking" and big["tools_s"] == 5000.0
    assert big["thinking_s"] == pytest.approx(20000.0 - 1000.0 - 5000.0)


def test_activity_fold_pairs_parallel_calls_of_one_tool_by_target(tmp_path):
    """Two parallel Reads of different files keep their OWN durations: a post closes
    the most recent open pre of the same tool AND the same target first; if there is
    none, the most recent open pre of the same tool (an answer that came back under
    another target is still that tool's); if there is still none, the most recent open
    pre of any tool -- as before. So the call that answers first no longer bills the
    ask that opened last."""
    par = act_lines(tmp_path / "par.jsonl", [
        act("pre", 100.0, tool="Read", target="a.py"),
        act("pre", 102.0, tool="Read", target="b.py"),
        act("post", 103.0, tool="Read", target="a.py"),   # the FIRST ask answers first
        act("post", 120.0, tool="Read", target="b.py")])
    f = watch.activity_fold(str(par), 121.0)
    assert [(c["tool"], c["target"], c["seconds"]) for c in f["calls"]] == [
        ("Read", "a.py", 3.0), ("Read", "b.py", 18.0)]    # each answer paid for its own
    assert f["tools_s"] == 21.0
    same_tool = act_lines(tmp_path / "renamed.jsonl", [   # no open pre carries c.py:
        act("pre", 100.0, tool="Read", target="a.py"),    # the most recent same-tool
        act("pre", 102.0, tool="Read", target="b.py"),    # one takes the answer
        act("post", 105.0, tool="Read", target="c.py")])
    f = watch.activity_fold(str(same_tool), 106.0)
    assert (f["calls"][0]["tool"], f["calls"][0]["target"],
            f["calls"][0]["seconds"]) == ("Read", "c.py", 3.0)     # 105 - 102, not - 100
    assert f["state"] == "tool" and f["tool"] == "Read" and f["target"] == "a.py"
    any_tool = act_lines(tmp_path / "crossed.jsonl", [    # Bash has nothing open: the
        act("pre", 100.0, tool="Read", target="a.py"),    # most recent pre of ANY tool
        act("pre", 101.0, tool="Read", target="b.py"),    # closes
        act("post", 111.0, tool="Bash", target="ls")])
    f = watch.activity_fold(str(any_tool), 112.0)
    assert (f["calls"][0]["tool"], f["calls"][0]["seconds"]) == ("Bash", 10.0)  # 111-101
    assert f["state"] == "tool" and f["tool"] == "Read" and f["target"] == "a.py"
    lone = act_lines(tmp_path / "lone.jsonl", [act("post", 10.0, tool="Grep", target="x")])
    f = watch.activity_fold(str(lone), 11.0)              # and a lone post is still no
    assert f["calls"] == [{"tool": "Grep", "target": "x", "seconds": None,  # ask to bill
                           "ok": True, "cls": "read"}]


def test_activity_fold_abandons_an_orphan_pre_that_never_answered(tmp_path):
    """A `pre` whose post never came must not pin the card: when a NEW `pre` arrives and
    at least one `post` has come in after that older call started, the agent has moved on
    -- hook dropped, tool denied, crash -- and the older call is closed as ABANDONED: it
    joins `calls` with `seconds: None`, `ok: false` and `abandoned: true`, and bills
    nothing to `tools_s`. A `stop` closes every still-open call the same way."""
    p = act_lines(tmp_path / "a.jsonl", [
        act("pre", 100.0, tool="Grep", target="SECRET"),         # the call that failed
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),          # the agent moved on...
        act("pre", 110.0, tool="Edit", target="b.py")])           # ...and asked again
    f = watch.activity_fold(str(p), 120.0)
    assert (f["state"], f["tool"], f["target"]) == ("tool", "Edit", "b.py")
    assert f["calls"][0] == {"tool": "Read", "target": "a.py", "seconds": 1.0,
                             "ok": True, "cls": "read"}           # paired: no abandoned key
    assert f["calls"][1] == {"tool": "Grep", "target": "SECRET", "seconds": None,
                             "ok": False, "abandoned": True, "pre_t": 100.0,
                             "cls": "read"}
    assert f["tools_s"] == 1.0                             # an abandoned call bills nothing
    # only calls OLDER than that answer are abandoned: one that opened after it is simply
    # a call whose own post has not come yet
    d = act_lines(tmp_path / "d.jsonl", [
        act("pre", 100.0, tool="Grep", target="x"),              # older than the next post
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 105.0, tool="Read", target="a.py"),
        act("pre", 106.0, tool="Read", target="b.py"),           # opened after it: not orphaned
        act("post", 107.0, tool="Read", target="b.py")])
    f = watch.activity_fold(str(d), 120.0)
    assert [(c["tool"], c.get("abandoned", False)) for c in f["calls"]] == [
        ("Read", False), ("Grep", True), ("Read", False)]
    assert f["state"] == "thinking"                              # nothing left open
    # and a stop closes every still-open call: the round is over, nothing runs any more
    s = act_lines(tmp_path / "s.jsonl", [act("pre", 100.0, tool="Bash", target="sleep 100"),
                                         act("stop", 105.0)])
    f = watch.activity_fold(str(s), 400.0)
    assert f["state"] == "done"
    assert f["calls"] == [{"tool": "Bash", "target": "sleep 100", "seconds": None,
                           "ok": False, "abandoned": True, "pre_t": 100.0,
                           "cls": "shell"}]
    assert f["tools_s"] == 0.0
    # a stop with nothing open abandons nothing: it closes a round, it is not an accusation
    f = watch.activity_fold(str(act_lines(tmp_path / "s0.jsonl", [act("stop", 105.0)])), 200.0)
    assert (f["state"], f["calls"], f["tools_s"]) == ("done", [], 0.0)


def test_activity_fold_orphan_rule_spares_parallel_calls_and_a_long_parallel_bash(tmp_path):
    """The rule fires only on a `pre` that FOLLOWS a `post`: two calls opened side by
    side and answered one after the other -- in any order -- abandon nothing, and a long
    call opened beside a quick one stays live after the quick one is answered: no new
    `pre` arrived, so it is still simply running (waiting once it runs long)."""
    par = act_lines(tmp_path / "par.jsonl", [
        act("pre", 100.0, tool="Bash", target="make -j4"),
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),          # the quick one answers first
        act("post", 111.0, tool="Bash", target="make -j4")])     # the long one later
    f = watch.activity_fold(str(par), 112.0)
    assert [(c["tool"], c["seconds"], c.get("abandoned", False)) for c in f["calls"]] == [
        ("Read", 1.0, False), ("Bash", 11.0, False)]             # both paired: nothing abandoned
    assert f["tools_s"] == 12.0
    slow = act_lines(tmp_path / "slow.jsonl", [
        act("pre", 100.0, tool="Bash", target="pytest -q"),       # a long call in parallel...
        act("pre", 101.0, tool="Read", target="a.py"),            # ...with a quick Read
        act("post", 102.0, tool="Read", target="a.py")])          # Read answered; Bash not yet
    f = watch.activity_fold(str(slow), 113.0)                     # and no new pre has come
    assert (f["state"], f["tool"]) == ("tool", "Bash")            # Bash is still running
    assert [c["tool"] for c in f["calls"]] == ["Read"]            # not an abandoned call
    f = watch.activity_fold(str(slow), 120.0)                     # past WAIT_AFTER: waiting
    assert (f["state"], f["tool"], f["cls"]) == ("waiting", "Bash", "python")
    assert f["tools_s"] == 1.0                                    # only the Read was billed


def test_activity_fold_orphan_rule_spares_an_open_subagent_call(tmp_path):
    """A subagent call (Task/Agent) is never abandoned by the calls running INSIDE it:
    those open new pres while it is itself still open, and that is its own doing, not the
    agent having moved on without an answer. It stays live -- waiting once it runs long --
    until its own post. A normal call opened beside it is abandoned as usual when the
    agent asks again after an answer; and a `stop` closes even a subagent call still open,
    its round being over."""
    t = act_lines(tmp_path / "t.jsonl", [
        act("pre", 100.0, tool="Task", target="Explore"),
        act("pre", 101.0, tool="Read", target="a.py"),            # the subagent's own work...
        act("post", 102.0, tool="Read", target="a.py"),
        act("pre", 103.0, tool="Grep", target="x"),               # ...while it is open
        act("post", 104.0, tool="Grep", target="x")])
    f = watch.activity_fold(str(t), 110.0)
    assert (f["state"], f["tool"], f["cls"]) == ("tool", "Task", "delegate")
    assert [c["tool"] for c in f["calls"]] == ["Read", "Grep"]   # only its inner calls closed
    assert all(not c.get("abandoned", False) for c in f["calls"])
    f = watch.activity_fold(str(t), 200.0)                        # it runs long: still its own
    assert (f["state"], f["tool"]) == ("waiting", "Task")         # call, not an abandoned one
    # the exemption is the subagent's alone: a quick call opened beside it is orphaned
    # when the agent moves on, while the subagent keeps its own open line
    mix = act_lines(tmp_path / "mix.jsonl", [
        act("pre", 100.0, tool="Agent", target="general-purpose"),
        act("pre", 101.0, tool="Bash", target="ls"),              # open alongside it
        act("post", 102.0, tool="Agent", target="general-purpose"),
        act("pre", 103.0, tool="Edit", target="b.py")])           # the agent moved on
    f = watch.activity_fold(str(mix), 110.0)
    assert (f["state"], f["tool"]) == ("tool", "Edit")
    assert f["calls"][0] == {"tool": "Agent", "target": "general-purpose",
                             "seconds": 2.0, "ok": True, "cls": "delegate"}
    assert f["calls"][1] == {"tool": "Bash", "target": "ls", "seconds": None,
                             "ok": False, "abandoned": True, "pre_t": 101.0,
                             "cls": "shell"}
    # and the stop spares nobody: a subagent call that outlived its session is closed too
    s = act_lines(tmp_path / "s.jsonl", [act("pre", 100.0, tool="Task", target="Explore"),
                                         act("stop", 105.0)])
    f = watch.activity_fold(str(s), 400.0)
    assert f["state"] == "done"
    assert f["calls"] == [{"tool": "Task", "target": "Explore", "seconds": None,
                           "ok": False, "abandoned": True, "pre_t": 100.0,
                           "cls": "delegate"}]


def test_activity_fold_late_post_reclaims_its_abandoned_orphan_call(tmp_path):
    """The audit finding: a call closed as abandoned can still get its real post -- the
    hook line was late, not lost. That post belongs to the abandoned call, not to some
    unrelated open `pre`: when no open pre carries the answer's tool AND target, the most
    recent abandoned entry with both is re-opened -- `seconds` from its kept `pre_t`, `ok`
    from the post, `abandoned` cleared, its time billed to `tools_s`. Only with nothing
    to reclaim do the old same-tool / any-tool pairings take the answer, and one post
    reclaims a call exactly once."""
    p = act_lines(tmp_path / "a.jsonl", [
        act("pre", 100.0, tool="Grep", target="SECRET"),              # the call that "failed"
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),               # the agent moved on...
        act("pre", 110.0, tool="Edit", target="b.py"),                # ...Grep abandoned...
        act("post", 115.0, tool="Grep", target="SECRET", ok=False)])  # ...and answers late
    f = watch.activity_fold(str(p), 116.0)
    assert f["calls"][1] == {"tool": "Grep", "target": "SECRET", "seconds": 15.0,
                             "ok": False, "cls": "read", "pre_t": 100.0}  # reclaimed, not abandoned
    assert (f["state"], f["tool"], f["target"]) == ("tool", "Edit", "b.py")   # NOT mispaired
    assert f["tools_s"] == 1.0 + 15.0                                # its own span is billed now
    # an OPEN pre of the same tool and target pairs first: no abandoned lookalike is touched
    q = act_lines(tmp_path / "b.jsonl", [
        act("pre", 100.0, tool="Bash", target="make"),
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),              # abandons the first ask
        act("pre", 110.0, tool="Bash", target="make"),                # the same ask opens again
        act("post", 112.0, tool="Bash", target="make")])              # pairs the OPEN pre
    f = watch.activity_fold(str(q), 113.0)
    assert [(c["tool"], c["seconds"], c.get("abandoned", False)) for c in f["calls"]] == [
        ("Read", 1.0, False), ("Bash", None, True), ("Bash", 2.0, False)]
    assert f["tools_s"] == 3.0
    # the MOST RECENT abandoned match wins; an earlier twin stays abandoned
    r = act_lines(tmp_path / "c.jsonl", [
        act("pre", 100.0, tool="Bash", target="ls"),                  # first abandoned ask
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),
        act("pre", 110.0, tool="Bash", target="ls"),                  # the same ask, twice...
        act("pre", 111.0, tool="Read", target="b.py"),
        act("post", 112.0, tool="Read", target="b.py"),
        act("pre", 113.0, tool="Grep", target="x"),                   # ...both abandoned...
        act("post", 120.0, tool="Bash", target="ls")])                # ...the later one answers
    f = watch.activity_fold(str(r), 121.0)
    bash = [c for c in f["calls"] if c["tool"] == "Bash"]
    assert [(c["seconds"], c.get("abandoned", False)) for c in bash] == [
        (None, True), (10.0, False)]
    assert bash[0]["pre_t"] == 100.0                                  # the twin stays as it was
    assert (f["state"], f["tool"]) == ("tool", "Grep")                # the open call keeps its line
    assert f["tools_s"] == 1.0 + 1.0 + 10.0
    # the match is tool AND target: a same-tool abandoned call of another target is left
    # alone, and the post falls back to the ordinary same-tool open-pre pairing
    m = act_lines(tmp_path / "d.jsonl", [
        act("pre", 100.0, tool="Bash", target="make"),
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),               # abandons `make`
        act("pre", 110.0, tool="Bash", target="pytest"),              # open, same tool, other target
        act("post", 112.0, tool="Bash", target="ls")])                # no ls ask, open or abandoned
    f = watch.activity_fold(str(m), 113.0)
    assert [(c["tool"], c["seconds"], c.get("abandoned", False)) for c in f["calls"]] == [
        ("Read", 1.0, False), ("Bash", None, True), ("Bash", 2.0, False)]
    # one post reclaims once: the SECOND late post finds no abandoned entry to reclaim
    # and stands as the answer with no ask it always was
    z = act_lines(tmp_path / "e.jsonl", [
        act("pre", 100.0, tool="Bash", target="ls"),
        act("pre", 101.0, tool="Read", target="a.py"),
        act("post", 102.0, tool="Read", target="a.py"),
        act("pre", 103.0, tool="Grep", target="x"),                   # the pre that abandons ls
        act("post", 104.0, tool="Grep", target="x"),
        act("post", 105.0, tool="Bash", target="ls"),                 # late: reclaims it (5 s)
        act("post", 106.0, tool="Bash", target="ls")])                # nothing left to reclaim
    f = watch.activity_fold(str(z), 107.0)
    assert [(c["tool"], c["seconds"], c.get("abandoned", False)) for c in f["calls"]] == [
        ("Read", 1.0, False), ("Bash", 5.0, False), ("Grep", 1.0, False),
        ("Bash", None, False)]
    assert f["tools_s"] == 7.0


def test_activity_fold_splits_thinking_from_tool_time(tmp_path):
    """The card's summary says how the session's time went. `tools_s` is the finished
    calls' own spans; `thinking_s` is what is left of the span the file covers, counting
    the composing that is going on right now. A call that never answered contributes
    nothing to tools_s, and the split never goes negative."""
    p = act_lines(tmp_path / "a.jsonl", [
        act("pre", 100.0, tool="Bash", target="make"),
        act("post", 104.0, tool="Bash", target="make"),
        act("pre", 110.0, tool="Read", target="a.py"),
        act("post", 113.0, tool="Read", target="a.py")])
    f = watch.activity_fold(str(p), 113.0)
    assert f["tools_s"] == 7.0 and f["thinking_s"] == 6.0
    assert watch.activity_fold(str(p), 120.0)["thinking_s"] == 13.0    # composing until now
    open_call = act_lines(tmp_path / "b.jsonl", [act("pre", 100.0, tool="Bash",
                                                     target="sleep 99")])
    f = watch.activity_fold(str(open_call), 130.0)
    assert f["state"] == "waiting" and f["tools_s"] == 0.0 and f["thinking_s"] == 0.0
    # a post stamped BEFORE its pre (a clock that jumped, a hand-built file): no negative
    # seconds, and no negative tools
    back = act_lines(tmp_path / "c.jsonl", [act("pre", 100.0, tool="Bash", target="ls"),
                                            act("post", 90.0, tool="Bash", target="ls")])
    f = watch.activity_fold(str(back), 101.0)
    assert f["calls"][0]["seconds"] is None and f["tools_s"] == 0.0


def test_activity_fold_classifies_each_tool(tmp_path):
    """The page's verbs, decided from the tool and its trimmed input alone: a Bash line is
    a shell command unless it runs Python or a test suite, which the page words
    differently; reads, edits, web, mcp, delegation, and `other` for the rest."""
    cases = [("Bash", "ls -la", "shell"), ("Bash", "python3 -m pytest -q", "python"),
             ("Bash", "python x.py", "python"), ("Bash", "pytest -q tests", "python"),
             ("Bash", "pip install thing", "python"), ("Bash", "uv run main", "python"),
             ("Bash", "sh -c 'pytest -q'", "python"), ("Bash", "make build", "shell"),
             ("Bash", "./run.sh", "shell"), ("Bash", "PYTHONPATH=x pytest -q", "python"),
             ("Bash", None, "shell"), ("Bash", "", "shell"),      # no command recorded is
             # still a shell call: the tool decides, not a missing input
             ("Read", "a.py", "read"), ("Grep", "SECRET", "read"), ("Glob", "**/*.py", "read"),
             ("Edit", "a.py", "edit"), ("Write", "a.py", "edit"),
             ("MultiEdit", "a.py", "edit"), ("NotebookEdit", "n.ipynb", "edit"),
             ("WebFetch", "docs.example.com", "web"), ("WebSearch", "a query", "web"),
             ("mcp__db__query", "select 1", "mcp"), ("mcp__x", None, "mcp"),
             ("Task", "Explore", "delegate"), ("Agent", "general-purpose", "delegate"),
             ("TodoWrite", "a todo", "other"), (None, None, "other")]
    for tool, target, cls in cases:
        assert watch._tool_class(tool, target) == cls, (tool, target)
    # and the fold carries the class on every finished call, not only on the live one
    recs = []
    for i, (tool, target, _c) in enumerate(cases[:10]):
        recs += [act("pre", 100.0 + 2 * i, tool=tool, target=target),
                 act("post", 101.0 + 2 * i, tool=tool, target=target)]
    f = watch.activity_fold(str(act_lines(tmp_path / "a.jsonl", recs)), 120.0)
    assert [c["cls"] for c in f["calls"]] == [c for _t, _g, c in cases[:10]]


def test_activity_fold_reads_nothing_that_is_not_there(tmp_path):
    """A tolerant reader, because the hook is writing while the page is reading: no path,
    no file, a folder, a file of pure junk, a half-written last line, a line that is not
    an object, an event the fold does not know, a line with no usable t -- all give None or
    are skipped, and one bad line costs the good ones around it nothing."""
    now = time.time()
    assert watch.activity_fold(None, now) is None
    assert watch.activity_fold("", now) is None
    assert watch.activity_fold(str(tmp_path / "absent.jsonl"), now) is None
    assert watch.activity_fold(str(tmp_path), now) is None            # a folder, not a file
    empty = tmp_path / "e.jsonl"                                       # created by the launcher,
    empty.write_text("", encoding="utf-8")                             # nothing hooked yet
    assert watch.activity_fold(str(empty), now) is None
    all_junk = tmp_path / "j.jsonl"
    all_junk.write_text("not json\n[1,2]\nnull\n\n{\"no\":\"event\"}\n"
                        + json.dumps(act("pre", "soon")) + "\n"
                        + json.dumps({"ev": "pre", "t": None}) + "\n"
                        + json.dumps({"ev": "pre", "t": -5}) + "\n"
                        + json.dumps({"ev": "bogus", "t": 100.0}) + "\n"
                        + json.dumps(act("pre", float("nan"))) + "\n", encoding="utf-8")
    assert watch.activity_fold(str(all_junk), now) is None
    mixed = act_lines(tmp_path / "m.jsonl", [act("pre", 100.0, tool="Bash", target="ls")])
    with open(mixed, "a", encoding="utf-8") as fh:                    # a tail caught mid-write
        fh.write('{"t":101.0,"ev":"po')                               # half a line: skipped
    f = watch.activity_fold(str(mixed), 101.0)
    assert f["state"] == "tool" and f["tool"] == "Bash"
    # a target that is not a string, and an ok that is not false, read as no target and
    # as a call that worked: the fold has no way to invent either
    odd = act_lines(tmp_path / "o.jsonl", [{"t": 100.0, "ev": "pre", "tool": "Bash",
                                            "target": 42, "ok": "yes"}])
    f = watch.activity_fold(str(odd), 100.0)
    assert f["target"] is None and f["cls"] == "shell"
    assert f["calls"] == []


def test_activity_fold_carries_ctx_and_win(tmp_path):
    """The last context reading the hook recorded, and the window it belongs to, come out
    of the fold for the meter -- the LAST line that carried each one winning, so a session
    that stopped reporting keeps its last known figure. A session whose hook never saw a
    usage block has neither, and the page then shows no meter."""
    p = act_lines(tmp_path / "a.jsonl", [
        act("pre", 10.0, tool="Bash", target="ls", ctx=1200, win=8192),
        act("post", 12.0, tool="Bash", target="ls", ctx=1500, win=8192),
        act("pre", 13.0, tool="Read", target="a.py", ctx=1600)])
    f = watch.activity_fold(str(p), 14.0)
    assert (f["ctx"], f["win"]) == (1600, 8192)
    # a junk ctx or win (a float, a string, a negative, a bool) is not a number to keep
    junk = act_lines(tmp_path / "b.jsonl", [
        act("pre", 10.0, tool="Bash", target="ls", ctx="1500", win=1.5),
        act("pre", 11.0, tool="Bash", target="ls", ctx=-3, win=True)])
    f = watch.activity_fold(str(junk), 12.0)
    assert f["ctx"] is None and f["win"] is None
    plain = act_lines(tmp_path / "c.jsonl", [act("pre", 10.0, tool="Bash", target="ls")])
    f = watch.activity_fold(str(plain), 11.0)
    assert f["ctx"] is None and f["win"] is None


# --------------------------------------------------- the fold, on the page's data

def test_run_detail_attaches_activity_to_each_unit(tmp_path, monkeypatch):
    """A swarm unit's card reads RUN/activity/<unit>.jsonl -- the file the unit's launcher
    environment names. While the unit runs, the fold's live state; once it finished, its
    calls and the time split and NO tool it is no longer holding; a unit with no file of
    its own carries no activity at all. Never another unit's file, and never a name that
    could walk out of the run folder."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    base, now = 1700000000.0, 1700000002.0        # fixed stamps: the seconds below are exact
    write_events(run, [ev("run_start", workflow="w", goal="g"),
                       ev("phase_start", label="build", n=3, seats=3,
                          units=[{"unit": n, "name": n, "role": "r"} for n in "abc"]),
                       ev("unit_start", unit="a", role="r", timeout=60),
                       ev("unit_start", unit="b", role="r", timeout=60),
                       ev("unit_start", unit="c", role="r", timeout=60),
                       ev("unit_done", unit="b", role="r", ok=True, cached=False,
                          seconds=3)])
    act_lines(run / watch.ACTIVITY_DIR / "a.jsonl",
              [act("pre", base, tool="Bash", target="pytest -q"),
               act("post", base + 1, tool="Bash", target="pytest -q"),
               act("pre", base + 2, tool="Read", target="src/x.py")])
    act_lines(run / watch.ACTIVITY_DIR / "b.jsonl",
              [act("pre", base - 200, tool="Read", target="src/y.py"),
               act("post", base - 199, tool="Read", target="src/y.py"),
               act("stop", base - 198)])
    # a unit's file is reached by its own name only: a name that could walk out of the
    # activity folder, or that was never a unit name, gets no path at all
    assert watch._unit_activity_file(str(run), "a") \
        == os.path.join(str(run), "activity", "a.jsonl")
    for bad in ("../escape", "a/b", "", "con f", ".hidden", "..hidden", None, 7):
        assert watch._unit_activity_file(str(run), bad) is None, bad
    assert watch._unit_activity_file(None, "a") is None
    # a name lib/swarm.py itself would accept (any length, dots and dashes) is a file
    assert watch._unit_activity_file(str(run), "search-1.v2_old") \
        == os.path.join(str(run), "activity", "search-1.v2_old.jsonl")
    assert watch._unit_activity_file(str(run), "x" * 200) \
        == os.path.join(str(run), "activity", "%s.jsonl" % ("x" * 200))
    rid = watch.register("swarm", "s", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, now)
    live = st["units"]["a"]["activity"]
    assert live["state"] == "tool" and live["tool"] == "Read"        # live, not the summary
    assert live["calls"][0] == {"tool": "Bash", "target": "pytest -q", "seconds": 1.0,
                                "ok": True, "cls": "python"}
    assert live["thinking_s"] == pytest.approx(1.0)
    done = st["units"]["b"]["activity"]
    assert set(done) == {"state", "calls", "thinking_s", "tools_s"}
    assert done["state"] == "done" and done["thinking_s"] == 1.0 and done["tools_s"] == 1.0
    assert "tool" not in done and "target" not in done and "cls" not in done
    assert "activity" not in st["units"]["c"]                        # wrote nothing: shows none


def test_run_detail_and_runs_give_a_session_its_activity(tmp_path, monkeypatch):
    """A single-shot session has no run folder, so its lines land beside its pointer at
    watch.activity_path(id): run_detail carries the WHOLE fold on the run, and /api/runs
    the small one (state, cls, tool, target, since, ctx, win -- no call list) that the
    overview card draws. A run of another kind carries no run-level activity: its units
    have files of their own."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    now = time.time()
    rid = watch.register("agent", "auditor: check the diff", start_server=False, timeout=300)
    sid = watch.register("swarm", "a swarm", run_path=str(tmp_path / "sw"), start_server=False)
    assert "activity" in watch.run_detail(rid, now)                  # the key is there...
    assert watch.run_detail(rid, now)["activity"] is None            # ...empty until it is fed
    act_lines(pathlib.Path(watch.activity_path(rid)),
              [act("pre", now - 5, tool="Grep", target="SECRET", ctx=1200, win=8192),
               act("post", now - 4, tool="Grep", target="SECRET", ctx=1300, win=8192),
               act("pre", now - 3, tool="Edit", target="src/a.py", ctx=1400, win=8192)])
    st = watch.run_detail(rid, now)
    a = st["activity"]
    assert a["state"] == "tool" and a["cls"] == "edit" and a["target"] == "src/a.py"
    assert a["ctx"] == 1400 and a["win"] == 8192 and a["since"] == now - 3
    assert [c["tool"] for c in a["calls"]] == ["Grep"]
    assert st["units"] == {}                                          # a session has no crew
    small = {r["id"]: r for r in watch.list_runs(now)}[rid]["activity"]
    assert set(small) == set(watch.ACTIVITY_SMALL)
    assert small["state"] == "tool" and small["tool"] == "Edit" and small["ctx"] == 1400
    assert "calls" not in small
    assert "activity" not in {r["id"]: r for r in watch.list_runs(now)}[sid]


def test_a_resumed_loop_folds_its_latest_round_into_one_activity_file(tmp_path, monkeypatch):
    """A loop interrupted and started again writes ON into the same two files: its round
    events and its activity lines accumulate. The page shows the LATEST round started and
    the LATEST checks event -- which, while a fresh round is still running, is the
    previous round's, and says so with a `round` of its own rather than passing itself off
    as the new one's. The calls the first attempt finished are still the calls the third
    shows, the step is the latest of the loop's own six lines, and nothing here reads a
    round-*.json."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [
        ev_at("round_start", 100.0, round=1, max_rounds=3, timeout=120),
        ev_at("checks", 140.0, round=1, passed=0, failed=1, unverified=0, total=1,
              items=[{"text": "value is good", "status": "FAIL"}]),
        ev_at("round_end", 141.0, round=1, rc=11)])
    act_lines(run / watch.ACTIVITY_FILE,
              [act("pre", 101.0, tool="Read", target="a.py"),
               act("post", 102.0, tool="Read", target="a.py")])
    rid = watch.register("until-done", "loop", run_path=str(run), start_server=False,
                         max_rounds=3, timeout=120)
    with open(run / watch.EVENTS_FILE, "a", encoding="utf-8") as fh:          # the resume,
        fh.write(ev_at("round_start", 200.0, round=2, max_rounds=3, timeout=120) + "\n")
        fh.write(ev_at("checks", 240.0, round=2, passed=1, failed=0, unverified=0,
                       total=1, items=[{"text": "value is good", "status": "PASS"}]) + "\n")
        fh.write(ev_at("round_end", 241.0, round=2, rc=11) + "\n")
        fh.write(ev_at("round_start", 300.0, round=3, max_rounds=3, timeout=90) + "\n")
    with open(run / watch.ACTIVITY_FILE, "a", encoding="utf-8") as fh:        # and it goes on
        fh.write(json.dumps(act("pre", 301.0, tool="Bash", target="pytest -q")) + "\n")
    st = watch.run_detail(rid, 305.0)
    assert st["round"] == {"n": 3, "timeout": 90, "started": 300.0, "review": False}  # the newest round
    assert st["step"] == {"name": "coding", "since": 300.0}            # and the loop IS in it
    assert st["checks"]["round"] == 2 and st["checks"]["passed"] == 1          # the last checks
    assert st["checks"]["items"] == [{"text": "value is good", "status": "PASS"}]
    a = st["activity"]
    assert (a["state"], a["tool"], a["cls"], a["since"]) == ("tool", "Bash", "python", 301.0)
    assert [c["tool"] for c in a["calls"]] == ["Read"]                         # from attempt one
    small = {r["id"]: r for r in watch.list_runs(305.0)}[rid]["activity"]
    assert (small["state"], small["tool"], small["since"]) == ("tool", "Bash", 301.0)


@pytest.mark.parametrize("tail,extra,name", [
    ("round_start", {"round": 2, "max_rounds": 3, "timeout": 90}, "coding"),
    ("checks_start", {"round": 2, "total": 3}, "testing"),
    ("audit_start", {"round": 1}, "reviewing"),
    ("checks", {"round": 1, "passed": 1, "failed": 0, "unverified": 0, "total": 1,
                "items": [{"text": "value is good", "status": "PASS"}]}, "between"),
    ("round_end", {"round": 1, "rc": 0}, "between"),
    ("audit_end", {"round": 1, "result": "clean"}, "between"),
])
def test_loop_step_names_the_latest_of_the_loop_s_six_kinds(tmp_path, monkeypatch,
                                                            tail, extra, name):
    """The between-rounds step is the LATEST of the loop's own six kinds: a round_start
    says coding, a checks_start says testing and carries the checklist's size, an
    audit_start says reviewing, and every other line -- checks, round_end, audit_end --
    says between. `since` is that line's own stamp; nothing else is read."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev_at("round_start", 100.0, round=1, max_rounds=3, timeout=120),
                      ev_at(tail, 200.0, **extra)])
    rid = watch.register("until-done", "loop", run_path=str(run), start_server=False,
                         max_rounds=3, timeout=120)
    st = watch.run_detail(rid, 205.0)
    assert st["state"] == "running"
    want = {"name": name, "since": 200.0}
    if tail == "checks_start":
        want["total"] = 3
    assert st["step"] == want


def test_loop_step_exposed_to_run_detail_and_api(tmp_path, monkeypatch):
    """run_detail carries the full step (the testing size included -- the checklist rows
    count it out); /api/runs carries name and stamp ONLY, small enough for a card. And a
    run that has ENDED exposes no step anywhere: the key is None, so no testing spinner
    ever turns on a run that finished -- that state belongs to the report now."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev_at("round_start", 100.0, round=1, max_rounds=3, timeout=120),
                       ev_at("checks_start", 110.0, round=1, total=2)])
    rid = watch.register("until-done", "loop", run_path=str(run), start_server=False,
                         max_rounds=3)
    st = watch.run_detail(rid, 115.0)
    assert st["step"] == {"name": "testing", "since": 110.0, "total": 2}
    small = {r["id"]: r for r in watch.list_runs(115.0)}[rid]
    assert small["step"] == {"name": "testing", "since": 110.0}      # no total on the card
    watch.finish(rid, 0)                                             # the loop is over
    st = watch.run_detail(rid, 120.0)
    assert st["step"] is None and st["state"] == "ended"
    small = {r["id"]: r for r in watch.list_runs(120.0)}[rid]
    assert small["step"] is None


def test_loop_step_absent_when_the_loop_wrote_no_loop_line(tmp_path, monkeypatch):
    """None of the six kinds, no step: a run_start or a claude_call line belongs to the
    engine's language, not to the loop's, and must not be read as one of its steps --
    and a junk stamp on a real kind leaves `since` None rather than a crash."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev("run_start", workflow="x"), ev("claude_call", name="n")])
    rid = watch.register("until-done", "loop", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["step"] is None
    assert {r["id"]: r for r in watch.list_runs(time.time())}[rid]["step"] is None
    run2 = tmp_path / "run2"
    write_events(run2, [json.dumps({"kind": "checks_start", "t": "noon", "round": 1,
                                    "total": 2})])
    rid2 = watch.register("until-done", "loop2", run_path=str(run2), start_server=False)
    assert watch.run_detail(rid2, time.time())["step"] == {"name": "testing",
                                                           "since": None, "total": 2}


def test_dismiss_and_the_prune_take_the_activity_file_with_the_pointer(tmp_path, monkeypatch):
    """Nothing of a gone run stays in the registry: `dismiss` removes the session's
    activity file with its pointer, and the prune does the same for a run whose end (or,
    killed, last activity) is older than PRUNE_AFTER."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    now = time.time()
    mine = watch.register("agent", "keep me", start_server=False)
    gone = watch.register("agent", "gone run", start_server=False)
    stale = watch.register("agent", "stale run", start_server=False)
    for rid in (mine, gone, stale):
        act_lines(pathlib.Path(watch.activity_path(rid)),
                  [act("pre", now, tool="Bash", target="ls")])
    watch.finish(gone, 0)
    watch.finish(stale, 0)
    old = now - watch.PRUNE_AFTER - 10
    path = os.path.join(watch.watch_dir(), "%s.json" % stale)
    with open(path, encoding="utf-8") as fh:
        pointer = json.load(fh)
    pointer.update({"start": old, "end": old})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pointer, fh)
    assert os.path.exists(watch.activity_path(gone))                 # ended, and still there
    assert watch.dismiss(gone) == 0
    assert not os.path.exists(watch.activity_path(gone))              # the file went too
    assert not os.path.exists(os.path.join(watch.watch_dir(), "%s.json" % gone))
    assert watch.dismiss(mine) == 1                                   # still running: kept
    assert os.path.exists(watch.activity_path(mine))
    runs = watch.list_runs(now)                                       # the prune runs here
    assert [r["id"] for r in runs] == [mine]
    assert not os.path.exists(watch.activity_path(stale))
    assert not os.path.exists(os.path.join(watch.watch_dir(), "%s.json" % stale))
    # an id that is not an id can name no file, so it deletes nothing outside the registry
    watch._drop_activity("../../etc/passwd")
    watch._drop_activity(None)
    assert os.path.exists(watch.activity_path(mine))


# ------------------------------------------------------------- the phase outline

def _plan(*labels):
    return ev_at("phase_plan", 0, phases=[{"label": lb, "n": n} for lb, n in labels])


def _phase(label, t, *names):
    return ev_at("phase_start", t, label=label, n=len(names), seats=2,
                 units=[{"name": n, "role": "r"} for n in names])


def test_old_events_have_no_outline_and_fold_as_before():
    """An events file written before phase_plan existed folds exactly as it did: an
    empty plan, nothing planned, and phases untouched."""
    lines = [ev_at("run_start", 0, workflow="w", goal="g"), _phase("classify", 1, "a")]
    fold = watch.fold_events(lines)
    assert fold["plan"] == [] and watch.planned_phases(fold) == []
    assert [p["label"] for p in fold["phases"]] == ["classify"]


def test_outline_entries_are_taken_in_order_and_leave_phases_alone():
    """Each phase_start takes the first pending outline entry with its label; what is
    left is planned, in outline order. The outline never adds to `phases`, so the seat
    usage of a finished phase is the same with or without it."""
    body = [_phase("classify", 1, "a", "b"),
            ev_at("unit_start", 1, unit="a", role="r", timeout=60),
            ev_at("unit_start", 1, unit="b", role="r", timeout=60),
            ev_at("unit_done", 11, unit="a", role="r", ok=True, cached=False, seconds=10),
            ev_at("unit_done", 21, unit="b", role="r", ok=True, cached=False, seconds=20),
            _phase("check", 21, "c")]
    start = ev_at("run_start", 0, workflow="w", goal="g")
    plain = watch.fold_events([start] + body)
    outlined = watch.fold_events([start, _plan(("classify", 2), ("check", 1),
                                               ("report", 1))] + body)
    assert [{k: v for k, v in p.items() if k != "pos"}            # byte for byte,
            for p in outlined["phases"]] \
        == [{k: v for k, v in p.items() if k != "pos"}            # except the outline
            for p in plain["phases"]]                              # position it carries
    assert [p["pos"] for p in outlined["phases"]] == [0, 1]        # the entries it took
    assert [p["pos"] for p in plain["phases"]] == [None, None]     # no outline: no pos
    assert outlined["phases"][0]["usage"] == pytest.approx(30 / 40)
    assert watch.planned_phases(outlined) == [{"label": "report", "n": 1}]
    before = watch.fold_events([start, _plan(("classify", 2), ("check", 1))])
    assert watch.planned_phases(before) == [{"label": "classify", "n": 2},
                                            {"label": "check", "n": 1}]


def test_a_repeated_label_takes_one_entry_per_phase_start():
    lines = [ev_at("run_start", 0, workflow="w", goal="g"),
             _plan(("check", 1), ("check", 1), ("report", None)), _phase("check", 1, "a")]
    assert watch.planned_phases(watch.fold_events(lines)) == [
        {"label": "check", "n": 1}, {"label": "report", "n": None}]


def test_a_phase_the_outline_does_not_name_takes_nothing():
    """A phase added at run time shows as a phase and leaves the outline as it was."""
    lines = [ev_at("run_start", 0, workflow="w", goal="g"),
             _plan(("classify", 1), ("report", 1)), _phase("classify", 1, "a"),
             _phase("round 2: retry", 2, "b")]
    fold = watch.fold_events(lines)
    assert [p["label"] for p in fold["phases"]] == ["classify", "round 2: retry"]
    assert watch.planned_phases(fold) == [{"label": "report", "n": 1}]


def test_an_outline_entry_the_run_went_past_stops_being_a_placeholder():
    """A review with nothing to review never starts; once the phase after it starts it
    is passed over -- no placeholder left behind the phases that did run."""
    lines = [ev_at("run_start", 0, workflow="w", goal="g"),
             _plan(("probe", 3), ("review", None), ("report", 1)),
             _phase("probe", 1, "a"), _phase("report", 2, "b")]
    assert watch.planned_phases(watch.fold_events(lines)) == []


def test_a_resume_starts_the_outline_over_and_junk_entries_are_skipped():
    lines = [ev_at("run_start", 0, workflow="w", goal="g"), _plan(("a", 1)),
             _phase("a", 1, "x"),
             ev_at("run_start", 5, workflow="w", goal="g"),
             ev_at("phase_plan", 5, phases=[{"label": "a", "n": 1}, {"label": ""}, "b",
                                            {"label": "c", "n": -2}, {"n": 3}])]
    assert watch.planned_phases(watch.fold_events(lines)) == [
        {"label": "a", "n": 1}, {"label": "c", "n": None}]
    assert watch.planned_phases(watch.fold_events(
        [ev_at("phase_plan", 0, phases="not a list")])) == []


def test_run_detail_exposes_planned(tmp_path, monkeypatch):
    """/api/run/<id> carries `planned`: the outline's entries still to come, read through
    the incremental fold, and [] for a run with no outline."""
    run = tmp_path / "run"
    write_events(run, [ev_at("run_start", 0, workflow="w", goal="g"),
                       _plan(("classify", 2), ("check", None)), _phase("classify", 1, "a", "b")])
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["planned"] == [{"label": "check", "n": None, "pos": 1}]   # pos: outline order
    assert [p["label"] for p in st["phases"]] == ["classify"]
    write_events(run, [ev_at("run_start", 0, workflow="w", goal="g")])
    assert watch.run_detail(rid, time.time())["planned"] == []


def _page_fn(name):
    """One top-level function of the page's script, as written (up to the next one)."""
    text = page_text()
    at = text.index("function %s(" % name)
    end = text.index("\nfunction ", at + 1)
    return text[at:end]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_phase_collapse_defaults_and_the_users_choice_wins():
    """phaseOpen: a finished phase is collapsed, a running one expanded, and a choice the
    user made (true or false) wins either way; a non-boolean stored value is no choice."""
    src = _page_fn("phaseOpen")
    probe = src + "\nconsole.log(JSON.stringify([phaseOpen(null,true),phaseOpen(null,false)," \
        "phaseOpen(true,true),phaseOpen(false,false),phaseOpen('x',true)]));"
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [False, True, True, False, False]


def test_phase_headers_are_buttons_and_the_choice_survives_the_poll():
    """The phase header is a real button with aria-expanded and aria-controls; the
    user's choice lives in sessionStorage per run and phase (every access wrapped), the
    poll's re-render reads it back, and the old <details> toggle no longer records
    a default as a choice -- the bug that kept a finished phase expanded forever."""
    text = page_text()
    wave = _page_fn("wave")
    assert '<button type="button" class="ph-head" data-ph="' in wave
    assert 'aria-expanded="' in wave and 'aria-controls="' in wave
    assert "phaseOpen(phaseChoice(key),finished)" in wave
    assert '<details class="wave"' not in text
    assert 'contains("wave")' not in text                     # toggle tracks calls only
    store = text[text.index('var PH_KEY="qwen-watch-phases"'):text.index("function focusKey")]
    assert store.count("try{") >= 2 and "sessionStorage.getItem(PH_KEY" in store
    assert "sessionStorage.setItem(PH_KEY" in store
    assert ".ph-head:focus-visible" in text                     # a visible focus
    assert "refocus(fk)" in text                                # kept across the swap
    detail = _page_fn("detail")
    assert "r.planned" in detail and "planned:true" in detail
    assert 'r.id+"|"+l+"|"+occ[l]' in detail                    # keyed by run and phase


def test_detail_page_keeps_long_runs_scannable():
    """The run view's noise control: agents waiting for a seat are one row of small
    sleepers rather than a full tile each, the feed shows its latest FEED_SHOW lines with
    the rest behind a button (whose focus survives the poll like a phase header's), and
    a running swarm's hero names the phase it is in among all of them."""
    text = page_text()
    wave = _page_fn("wave")
    assert 'class="queue"' in wave and 'robot(famOf(us[n].role),"queued")' in wave
    assert "busy.map(function(n){return tileHtml(" in wave        # tiles: working only
    detail = _page_fn("detail")
    assert "ev.slice(0,FEED_SHOW)" in detail and 'data-more="' in detail
    assert "nowHtml(r,o)" in detail
    now = _page_fn("nowHtml")
    assert '"Phase "+(r.phases||[]).length+" of "+total' in now    # planned ones count
    assert 'hasAttribute("data-fk")' in text                    # focus kept for it too


def test_status_text_on_chips_uses_the_contrast_inks():
    """Status words on the tinted chips are set in the --*-ink tokens, defined for light
    and both dark selectors, never in the plain status colours that fall under 4.5:1."""
    text = page_text()
    assert text.count("--run-ink:") == 3 and text.count("--warn-ink:") == 3
    assert text.count("--test-ink:") == 3                    # the Testing/Reviewing chip
    assert text.count("--mark-ink:") == 3                    # and the marks' glyph
    assert ".pill.running{color:var(--run-ink)}" in text
    assert ".act .k.wait{color:var(--warn-ink)}" in text
    assert ".act .k.test{color:var(--test-ink)}" in text
    assert "k.test{color:#D65A9A}" not in text               # 2.94:1 in light: gone
    assert ".flag{color:var(--warn-ink)" in text             # the "items need you" flag
    assert ".ctx.hot span:last-child{color:var(--warn-ink)" in text   # the hot token count
    assert "span:last-child{color:var(--warn);" not in text


# ------------------------------------------- the page's own script, run under node
# The audit's checks evaluate the page's top-level functions in node with DOM stubs.
# The stubs cover everything the script touches while LOADING (the listeners, the
# getElementById, the first render() and its 2.5 s interval) and nothing else: the
# builders (overview/detail/wave/...) are pure string functions and do the real work.

_PAGE_STUB = """
var _store={};
global.window={addEventListener:function(){},scrollTo:function(){}};
global.location={hash:""};
global.setInterval=function(){return 0};global.setTimeout=function(){return 0};
global.fetch=function(){return {then:function(){return this},catch:function(){return this}}};
global.localStorage={getItem:function(k){return _store["l"+k]===undefined?null:_store["l"+k]},
  setItem:function(k,v){_store["l"+k]=String(v)},removeItem:function(k){delete _store["l"+k]}};
global.sessionStorage={getItem:function(k){return _store["s"+k]===undefined?null:_store["s"+k]},
  setItem:function(k,v){_store["s"+k]=String(v)},removeItem:function(k){delete _store["s"+k]}};
global.document={addEventListener:function(){},activeElement:null,
  getElementById:function(){return null},
  querySelector:function(){return null},querySelectorAll:function(){return []},
  documentElement:{classList:{add:function(){},remove:function(){},contains:function(){return false}}}};
"""


def _page_script():
    """The page's <script> content, ready to run under _PAGE_STUB."""
    text = page_text()
    start = text.index("<script>") + len("<script>")
    return _PAGE_STUB + text[start:text.index("</script>")]


def _page_eval(expr, printer):
    """The audit's node pattern: its harness evals the snippet and reports the
    completion value of its LAST statement, so a snippet may open with `var`
    declarations -- the page's own functions are the globals it evaluates against.
    The program (the DOM stubs plus the page's own script) goes in on STDIN and the
    snippet in the environment: both are data, never quoted into code -- and a
    whole page on a Windows command line would not fit one."""
    out = subprocess.run(
        ["node"],
        input=_page_script() + "\n"
              + printer % "eval(process.env.WATCH_PAGE_EXPR)",
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=dict(os.environ, WATCH_PAGE_EXPR=expr))
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def _page_true(expr):
    """The audit's exit-0-iff-truthy node checks: evaluate one JS snippet's value."""
    return json.loads(_page_eval(expr, "console.log(JSON.stringify(!!(%s)));"))


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def _page_html(expr):
    """The same, for a snippet whose last statement produces HTML to assert on in python."""
    return _page_eval(expr, "console.log(String(%s));").rstrip("\n")


# ---------------------------------------------------------------- the audit's fixes


def test_only_a_status_region_is_live():
    """Finding 1: <main> is rewritten every 2.5 s, so it must NOT be a live region
    (a screen reader would re-read the whole page every poll). The one announcement
    channel is a small sr-only status div outside it, set only on real transitions."""
    text = page_text()
    assert '<main id="app" aria-live' not in text               # the page is not a loudspeaker
    assert '<main id="app"></main>' in text
    assert 'id="sr-status" class="sr-only" role="status" aria-live="polite"' in text
    assert ".sr-only{position:absolute;width:1px;height:1px;overflow:hidden;" \
           "clip:rect(0 0 0 0);white-space:nowrap}" in text
    assert text.index('id="sr-status"') > text.index("</main>")  # outside the swapped main
    say = _page_fn("srSay")
    assert 'getElementById("sr-status")' in say
    check = _page_fn("srCheck")                                  # transitions only...
    assert "Run finished." in check and "Run stopped." in check
    assert "Phase " in check and "Needs you: " in check
    assert "dur(" not in check and "elapsed" not in check        # ...never a timer


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_every_control_keeps_focus_across_the_poll():
    """Finding 2: every poll replaces the HTML, so every focusable element -- links,
    buttons, the tool-call summaries -- must carry a stable data-fk that focusKey()
    reads and refocus() finds again; a focusable without a key loses the keyboard."""
    shared = ('(h.match(/<(a|button|summary)[\\s>]/g)||[]).length==='
              '(h.match(/data-fk="/g)||[]).length')
    both = ('var R=[{id:"aaaaaaaaaaaa",kind:"agent",state:"running",title:"t",'
            'elapsed:5,units:{}},{id:"bbbbbbbbbbbb",kind:"agent",state:"ended",'
            'exit:0,title:"u",elapsed:5,end:1}];'
            'var h=overview(R)+detail({id:"cccccccccccc",kind:"agent",state:"running",'
            'title:"t",elapsed:5,units:{},recent:[],activity:{state:"thinking",since:1,'
            'calls:[{tool:"Read",target:"x",seconds:1}]}});')
    assert _page_true(both + shared +
                      '&&/data-fk="agent:/.test(h)&&/data-fk="run:/.test(h)&&'
                      '/data-fk="clear"/.test(h)&&/data-fk="row:/.test(h)&&'
                      '/data-fk="hide:/.test(h)&&/data-fk="back"/.test(h)&&'
                      '/data-fk="calls:/.test(h)')
    swarm = ('var us={a:{state:"running",role:"reader",started:1,timeout:60},'
             'b:{state:"done",role:"writer",seconds:3},'
             'c:{state:"queued",role:"checker"}};'
             # 12 events: past FEED_SHOW+2, so the earlier-lines button is there --
             # finding 22 hides it when only two or fewer lines would be hidden
             'var evs=[];for(var i=0;i<12;i++)evs.push({kind:"run_start",t:100+i});'
             'var h=detail({id:"dddddddddddd",kind:"swarm",state:"running",title:"t",'
             'elapsed:5,units:us,recent:evs,planned:[],'
             'phase:{label:"read",n:3,units:["a","b","c"]},'
             'phases:[{label:"read",n:3,end:null,units:["a","b","c"]},'
             '{label:"write",n:1,end:50,units:[]}]});')
    assert _page_true(swarm + shared +
                      '&&/data-fk="ph:/.test(h)&&/data-fk="more:/.test(h)&&'
                      '/data-fk="back"/.test(h)')
    assert 'hasAttribute("data-fk")' in _page_fn("focusKey")
    assert "preventScroll:true" in _page_fn("refocus")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_lost_viewer_shows_the_banner_and_freezes_the_page():
    """Finding 3: when the viewer stops answering, the page must not read "Up to date"
    over a still-spinning run: Reconnecting in the warn ink, a dated banner after the
    second miss, and every loop paused while we are offline."""
    assert _page_true('typeof offlineHtml==="function" && '
                      '/Lost the viewer/.test(offlineHtml(Date.now()/1000-30))')
    assert _page_true('/role="status"/.test(offlineHtml(Date.now()/1000-30))')
    text = page_text()
    assert "animation-play-state:paused" in text
    assert ".offline svg.robot *,.offline .spin,.offline .dot{animation-play-state:paused!important}" in text
    assert ".offline .pill.running{color:var(--muted)}" in text
    top = _page_fn("topbar")
    assert "Reconnecting" in top and "Up to date" in top and '"Live"' in top
    render = text[text.index("function render()"):text.index("window.addEventListener")]
    assert "offlineHtml(failSince)" in render                    # the banner, after 2 misses
    assert 'htmlClass("offline"' in render                       # and the freeze, both ways
    assert 'srSay("Lost the viewer.")' in render and 'srSay("Viewer back.")' in render


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_an_ended_run_has_no_live_phase():
    """Finding 4: on an ended, failed or killed run nothing may look live -- no
    spinner, no seat queue, no working tiles -- and "done" counts only done/cached."""
    assert _page_true('''var h=wave({p:{label:"implement",n:3,end:null},i:1,
list:["a","b","c"],key:"k"},{id:"r"},{a:{state:"queued",role:"x"},
b:{state:"done",role:"x"},c:{state:"running",role:"x"}},"failed");
h.indexOf('class="spin')<0&&h.indexOf('class="queue')<0&&h.indexOf('class="tiles')<0''')
    assert _page_true('''var h=wave({p:{label:"attack",n:3,end:null},i:0,
list:["a","b","c"],key:"k"},{id:"r"},{a:{state:"done",role:"x"},
b:{state:"not run",role:"x"},c:{state:"not run",role:"x"}},"killed");
h.indexOf('class="spin')<0&&h.indexOf("3 of 3 done")<0''')
    h = _page_html('wave({p:{label:"attack",n:3,end:null},i:0,list:["a","b","c"],'
                   'key:"k"},{id:"r"},{a:{state:"done",role:"x"},'
                   'b:{state:"not run",role:"x"},c:{state:"not run",role:"x"}},'
                   '"running")')
    assert "1 of 3 done" in h and "2 did not run" in h     # only done and cached count


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_unit_done_not_ok_folds_ok_false():
    """Finding 5: a unit_done with ok:false is a unit that ended BADLY: the fold keeps
    the verdict, and the row shows a red mark and the word failed, not a green check."""
    lines = [ev("run_start", workflow="swarm", goal="g"),
             ev("phase_start", label="build", n=2, seats=1,
                units=[{"name": "a", "role": "r"}, {"name": "b", "role": "r"}]),
             ev("unit_done", unit="a", role="r", ok=False, seconds=19 * 60 + 58,
                why="patch did not apply"),
             ev("unit_done", unit="b", role="r", ok=True, seconds=5.0)]
    f = watch.fold_events(lines)
    assert f["units"]["a"]["state"] == "done"               # it did finish its turn...
    assert f["units"]["a"]["ok"] is False                   # ...but the emitter said no
    assert f["units"]["b"]["ok"] is True
    assert _page_true('var h=rowHtml("a",{state:"done",ok:false,role:"r",seconds:5});'
                      '/mark bad/.test(h)&&/failed/.test(h)')
    h = _page_html('wave({p:{label:"implement",n:2,end:9},i:0,list:["a","b"],key:"k"},'
                   '{id:"r"},{a:{state:"done",ok:false,role:"r"},'
                   'b:{state:"done",ok:true,role:"r"}},"failed")')
    assert "1 failed" in h                                  # and the phase says so too


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_run_page_lists_what_needs_you():
    """Finding 6: the overview counts attention items, so the run page must show them:
    a Needs you block under the hero, latest first, every item."""
    assert _page_true('/needs a login/.test(detail({id:"r",kind:"swarm",'
                      'state:"running",title:"t",elapsed:5,units:{},recent:[],'
                      'attention:[{item:"x",reason:"needs a login",t:1}]}))')
    h = _page_html('detail({id:"r",kind:"swarm",state:"running",title:"t",elapsed:5,'
                   'units:{},recent:[],attention:[{item:"x",reason:"first one",t:1},'
                   '{item:"y",reason:"newest one",t:2}]})')
    assert "<h2>Needs you <small>2</small></h2>" in h
    assert 'class="attn"' in h                              # the feed's attention style
    assert h.index("newest one") < h.index("first one")     # latest first


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_silent_run_says_so():
    """Finding 7: a run whose every number froze 9 hours ago must not read healthy:
    the flag names the silence, on the card and in the hero, and an over-budget unit
    says it is over its limit."""
    quiet = ('var n=Date.now()/1000;var r={id:"r",kind:"swarm",state:"running",'
             'title:"t",elapsed:9*3600,start:n-9*3600,units:{a:{state:"running",'
             'role:"reviewer",started:n-9*3600,timeout:1800}},'
             'phase:{label:"review",n:1,units:["a"]},'
             'phases:[{label:"review",n:1,end:null,units:["a"]}],'
             'recent:[{kind:"unit_start",unit:"a",t:n-9*3600}]};')
    assert _page_true(quiet + '/No update for 9h 0m/.test(detail(r))')
    assert _page_true(quiet + '/No update for/.test(overview([r]))')
    assert _page_true('var h=tileHtml("a",{state:"running",role:"r",'
                      'started:Date.now()/1000-7200,timeout:1800},"r");'
                      '/over its 30m limit/.test(h)&&/class="over"/.test(h)')
    text = page_text()
    assert ".tile .meta .over{color:var(--warn-ink)" in text    # in the warn ink


def test_list_runs_carries_last_t(tmp_path, monkeypatch):
    """Finding 7, server side: every /api/runs entry carries last_t -- the freshest
    moment the run spoke (its start when it has no events) -- the number the card's
    "No update for" flag measures its silence against."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    now = time.time()
    quiet = now - 9 * 3600
    write_events(run, [
        ev_at("run_start", quiet, workflow="swarm", goal="g"),
        ev_at("phase_start", quiet, label="build", n=1, seats=1,
              units=[{"name": "a", "role": "r"}]),
        ev_at("unit_start", quiet, unit="a", role="r"),
    ])
    rid = watch.register("swarm", "quiet run", run_path=str(run), start_server=False)
    p = read_pointer(rid)
    p["start"] = quiet                                       # it started 9 h ago too
    with open(os.path.join(watch.watch_dir(), "%s.json" % rid), "w",
              encoding="utf-8") as fh:
        json.dump(p, fh)
    entry = {r["id"]: r for r in watch.list_runs(now)}[rid]
    assert entry["last_t"] == pytest.approx(quiet, abs=2)
    fresh = watch.register("swarm", "busy run", run_path=str(tmp_path / "r2"),
                           start_server=False)
    entry = {r["id"]: r for r in watch.list_runs(now)}[fresh]
    assert entry["last_t"] == pytest.approx(now, abs=2)      # its start is the stamp


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_pause_motion_button_stops_everything():
    """Finding 9 (WCAG 2.2.2): endless motion needs an in-page pause. The button is a
    real toggle (aria-pressed), its state is the tab's choice kept in localStorage, it
    is applied before the first render, and one CSS rule freezes every animation."""
    text = page_text()
    assert "aria-pressed" in text
    top = _page_fn("topbar")
    assert '<button type="button" class="still-btn" data-fk="still" aria-pressed="' in top
    assert "Pause motion" in top and "Play motion" in top
    assert "qwen-watch-still" in text                        # the stored choice
    assert "try{" in _page_fn("getStill") and "try{" in _page_fn("setStill")
    assert ".still *,.still *::before,.still *::after{animation-play-state:paused!important}" in text
    assert ".still .robot .zzz text{opacity:1}" in text
    assert "applyStill();\nrender(); setInterval" in text     # applied before the first frame


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_no_button_sits_inside_a_link_or_an_h2():
    """Finding 10: a button inside an <a> is invalid HTML and untouchable, and a
    heading is no container for controls: the rows are <li>s with sibling link and
    button, and Clear finished is a sibling of its h2."""
    assert _page_true('''var h=overview([{id:"bbbbbbbbbbbb",kind:"agent",state:"ended",
exit:0,title:"u",elapsed:5,end:1}]);
!/<a [^>]*class="row"(?:(?!<\\/a>)[\\s\\S])*<button/.test(h)&&
!/<h2>(?:(?!<\\/h2>)[\\s\\S])*<button/.test(h)''')
    text = page_text()
    assert '<ul class="done-list">' in text and '<li class="row">' in text
    assert '<a class="row-link" href=' in text               # the grid moved to the link
    assert '<div class="sec-head"><h2>Finished <small>' in text
    assert ".sec-head{display:flex;align-items:baseline;justify-content:space-between}" in text


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_marks_spinners_and_bars_carry_names():
    """Finding 17: an aria-label on a role-less span is ignored -- the glyph gets read
    instead. Status marks and spinners become role="img" with a name, and every bar
    gets a label and a valuetext that says what the fill means."""
    assert _page_true('/aria-label="/.test(bar(0.5))&&'
                      '/role="img"/.test(rowHtml("a",{state:"done",role:"r"}))')
    assert _page_true('/aria-label="finished"/.test(rowHtml("a",{state:"done",role:"r"}))&&'
                      '/aria-label="failed"/.test(rowHtml("a",{state:"done",ok:false,'
                      'role:"r"}))&&/aria-label="did not run"/.test(rowHtml("a",'
                      '{state:"not run",role:"r"}))')
    assert _page_true('''var h=wave({p:{label:"read",n:1,end:null},i:0,list:["a"],
key:"k"},{id:"r"},{a:{state:"running",role:"r",started:Date.now()/1000}},"running");
/role="img" aria-label="running"/.test(h)''')
    assert _page_true('/aria-valuetext="probe: 2 of 40 agents done"/.test(bar(0.5,"",'
                      '"Run progress","probe: 2 of 40 agents done"))')
    h = _page_html('overview([{id:"bbbbbbbbbbbb",kind:"swarm",state:"killed",title:"u",'
                   'elapsed:5}])')
    assert 'role="img" aria-label="Stopped"' in h            # the gone row's cross is named


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_failed_run_bar_shows_its_real_progress():
    """Finding 18: a run killed at 1 of 3 must not read as a full 100 % bar --
    card and hero show the progress the API reported, whatever the outcome."""
    assert _page_true('/aria-valuenow="33"/.test(cardMeter({state:"killed",'
                      'progress:0.33,kind:"swarm"}))')
    assert _page_true('/aria-valuenow="33"/.test(cardMeter({state:"failed",'
                      'progress:0.33,exit:2,kind:"swarm"}))')
    assert _page_true('/class="bar bad"[^>]*aria-valuenow="33"/.test(detail({id:"r",'
                      'kind:"swarm",state:"killed",title:"t",elapsed:5,progress:0.33,'
                      'units:{},recent:[]}))')


def test_dark_status_marks_use_a_dark_glyph():
    """Finding 19: a white glyph on the dark status colours is 2.2-3.0:1 -- below the
    non-text minimum. --mark-ink darkens the glyph in dark themes only."""
    text = page_text()
    assert text.count("--mark-ink:") == 3
    assert text.count("--mark-ink:#0F141A") == 2             # both dark selectors
    assert "color:var(--mark-ink)" in text                   # the marks draw with it


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_copy_uses_one_set_of_verbs():
    """Finding 20: one vocabulary across the page -- Running, Finished, Failed,
    Stopped -- correct plurals, no raw exit codes as decoration."""
    assert _page_true('''var ps=evText({kind:"phase_start",label:"a",n:2,seats:1});
var ok0=evText({kind:"run_end",exit:0}),bad=evText({kind:"run_end",exit:2});
var ov=overview([{id:"bbbbbbbbbbbb",kind:"swarm",state:"killed",title:"u",elapsed:5}]);
var w1=wave({p:{label:"a",n:1,end:5,usage:1},i:0,list:[],key:"k"},{id:"r"},{},"ended");
var wq=wave({p:{label:"a",n:1,end:5,usage:0.75},i:0,list:[],key:"k"},{id:"r"},{},"ended");
var rn=rowHtml("a",{state:"not run",role:"r"});
/on 1 seat$/.test(ps)&&!/1 seats/.test(ps)&&
ok0==="Run finished"&&/Run failed with exit code 2/.test(bad)&&
!/killed/.test(ov)&&/kept all seats busy/.test(w1)&&
/seats busy 75% of the time/.test(wq)&&/did not run/.test(rn)''')
    assert _page_true('!/"1 seats"/.test(evText({kind:"phase_start",label:"a",n:1,'
                      'seats:1}))')
    text = page_text()
    pill = text[text.index('var pillWord={'):]                # the pill keeps its form
    assert 'Failed (exit "+esc(String(r.exit))+")' in pill


# --------------------------------------- the audit's fixes, second half (11-16, 21-26)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_an_unchanged_poll_rebuilds_only_what_changed():
    """Finding 11: a poll that changed nothing must rebuild nothing. Live durations
    are tick spans the 1 s ticker fills, so a run that said nothing between two
    polls produces a byte-identical page; the swap then skips entirely, or replaces
    only the data-sec sections whose HTML changed (hero, each wave, the feed, the
    run lists), keeping the crew strip's scroll."""
    assert _page_true('var n=Date.now()/1000,r={id:"r",kind:"agent",state:"running",'
                      'title:"t",start:n-60,elapsed:60,units:{},recent:[],'
                      'activity:{state:"thinking",since:n-30}};var a=detail(r);'
                      'var D=Date.now;Date.now=function(){return D()+5000};'
                      'r.elapsed=65;var b=detail(r);Date.now=D;a===b')
    text = page_text()
    assert '<span class="tick" data-since=' in _page_fn("tickSpan")
    assert "setInterval(tickAll,1000)" in text                # the 1 s ticker
    assert "tickSpan(a.since" in _page_fn("actLine")          # "Asking the model, 30s" ticks
    assert 'data-sec="hero"' in text and 'data-sec="feed"' in text
    assert 'data-sec="runs"' in _page_fn("overview")          # the overview run list
    assert 'data-sec="wave:' in _page_fn("wave")              # every wave section, keyed
    swap = _page_fn("swap")
    assert "html===lastHtml" in swap                          # identical page: no swap at all
    assert "scrollLeft" in swap                               # the crew strip keeps its place
    assert "refocus(fk)" in text                              # and finding 2 still restores focus


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_two_hundred_agent_phase_shows_twelve_tiles_and_a_button():
    """Finding 12: the walls of robots are capped -- twelve working tiles then a
    button (the choice kept like the feed's), twenty-four sleepers then a count.
    (The overview's walls are capped too: see the test_card_agents_caps test.)"""
    assert _page_true('var us={},L=[];for(var i=0;i<200;i++){us["u"+i]={state:"running",'
                      'role:"reader",started:1,timeout:60};L.push("u"+i)}'
                      'var h=wave({p:{label:"read",n:200,end:null},i:0,list:L,key:"k"},'
                      '{id:"r"},us,"running");'
                      '(h.match(/class="tile[" ]/g)||[]).length<=12&&/Show all 200/.test(h)')
    assert _page_true('var us={},L=[];for(var i=0;i<30;i++){us["u"+i]={state:"queued",'
                      'role:"r"};L.push("u"+i)}'
                      'var h=wave({p:{label:"read",n:30,end:null},i:0,list:L,key:"k"},'
                      '{id:"r"},us,"running");'
                      '(h.match(/<li title=/g)||[]).length<=24&&/and 6 more/.test(h)')
    assert 'data-more="tiles:' in _page_fn("wave")            # toggled like feedAll's


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_long_goal_clamps_to_three_lines_under_its_status():
    """Finding 13: the stat row sits above the goal, the goal clamps at three lines
    on a phone, and Show full goal / Show less opens it (a button with a focus key,
    so the poll does not eat it)."""
    text = page_text()
    assert re.search(r"\.hero h1\{[^}]*line-clamp", text)
    assert ".hero.full h1{" in text
    long_goal = "x" * 300
    h = _page_html('detail({id:"r",kind:"swarm",state:"running",title:"t",goal:"%s",'
                   'elapsed:5,units:{},recent:[]})' % long_goal)
    assert "Show full goal" in h and 'data-fk="goal"' in h
    assert h.index('class="stat"') < h.index("<h1>")          # status before the wall of text
    short = _page_html('detail({id:"r",kind:"swarm",state:"running",title:"t",'
                       'elapsed:5,units:{},recent:[]})')
    assert "Show full goal" not in short                      # a short goal needs no button


def test_the_small_tap_targets_grew():
    """Finding 15: x, Clear finished, the tool-call toggle -- every one clears a
    comfortable minimum, in rules that carry the size on their own line."""
    text = page_text()
    assert re.search(r"\.x-btn\{[^}]*width:44px", text)
    assert re.search(r"\.clear\{[^}]*min-height:44px", text)
    assert re.search(r"\.back\{[^}]*padding:10px 12px 10px 0", text)
    assert re.search(r"\.calls summary\{[^}]*min-height:(3[2-9]|4[0-9])px", text)


def test_run_detail_exposes_skipped(tmp_path, monkeypatch):
    """Finding 16 (the API): a phase_start marks the outline entries it passed over
    and run_detail carries them as `skipped`, in outline order -- the page cannot
    show what the outline knew but the run dropped."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev_at("run_start", 0, workflow="w", goal="g"),
                       _plan(("probe", 3), ("review", None), ("report", 1)),
                       _phase("probe", 1, "a"), _phase("report", 2, "b")])
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["skipped"] == [{"label": "review", "n": None, "pos": 1}]   # pos: outline order
    assert st["planned"] == []                                # nothing left to come
    assert watch.skipped_phases(watch.fold_events(
        [ev_at("run_start", 0, workflow="w", goal="g"),
         _plan(("a", 1), ("b", 2))])) == []                   # no jump, nothing skipped


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_skipped_phase_shows_and_counts():
    """Finding 16 (the page): the passed-over entries show as quiet .wave.planned
    placeholders saying Skipped, and they count in "Phase N of M"."""
    assert _page_true('/Skipped/.test(detail({id:"r",kind:"swarm",state:"running",'
                      'title:"t",elapsed:5,units:{},recent:[],phases:[],planned:[],'
                      'skipped:[{label:"search",n:12}]}))')
    h = _page_html('detail({id:"r",kind:"swarm",state:"running",title:"t",elapsed:5,'
                   'units:{a:{state:"running",role:"r"}},'
                   'phase:{label:"plan",n:1,units:["a"]},'
                   'phases:[{label:"plan",n:1,end:null,units:["a"]}],'
                   'planned:[],skipped:[{label:"search",n:12}],recent:[]})')
    assert 'class="wave planned"' in h                        # the placeholder's quiet shape
    assert "12 agents, Skipped" in h                           # with its note
    assert "Phase 1 of 2" in h                                 # counted among all the phases


def test_the_testing_checklist_gives_its_room_on_a_phone():
    """Finding 21: on a phone the checklist hands the status column's width back to
    the text: two columns, the status word under the item on the second."""
    text = page_text()
    assert re.search(r"@media \(max-width:480px\)\{[^@]*"
                     r"\.checks li\{grid-template-columns:20px 1fr", text)
    assert ".checks .tm{grid-column:2}" in text


def test_a_cut_checklist_line_says_so(tmp_path, monkeypatch):
    """Finding 21 (the server): the checks line is trimmed to 100 characters -- and
    a reader must see that the text went past the cut, not that it ended there."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev_at("run_start", 0, workflow="w", goal="g"),
                       ev_at("checks", 5, round=1, passed=1, failed=0, unverified=0,
                             total=2, items=[{"text": "x" * 150, "status": "PASS"},
                                             {"text": "short one", "status": "FAIL"}])])
    rid = watch.register("until-done", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["checks"]["items"][0]["text"] == "x" * 100 + "…"    # the cut is visible
    assert st["checks"]["items"][1]["text"] == "short one"        # an uncut line: no …


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_feed_keeps_fifty_and_says_show_earlier():
    """Finding 22: the window holds fifty events, not fifteen; the page shows eight
    and its button counts the EARLIER lines it will bring -- but when only two or
    fewer lines would hide, all of them show with no button at all, and a full
    window says where the rest went."""
    lines = [json.dumps({"kind": "note_%d" % i, "t": i}) for i in range(60)]
    f = watch.fold_events(lines)
    assert len(f["recent"]) == 50
    assert f["recent"][0]["t"] == 10                           # the oldest ten are gone
    assert _page_true('function R(k){return detail({id:"r",kind:"swarm",state:"running",'
                      'title:"t",elapsed:5,units:{},recent:Array.from({length:k},'
                      'function(_,i){return{kind:"run_start",t:i}})})}'
                      '!/data-more/.test(R(10))&&/Show 12 earlier/.test(R(20))')
    text = page_text()
    assert "RECENT_KEEP=50" in text
    assert "Older events are not kept here." in text
    assert "Show N more" not in text                           # the old lie, gone


def test_the_focus_ring_on_finished_rows_is_not_clipped():
    """Finding 23: the list clips its corners -- the ring must sit inside the row."""
    assert re.search(r"\.row(-link)?:focus-visible\{[^}]*outline-offset:-2px", page_text())


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_tab_title_follows_the_run():
    """Finding 24: the tab strip says what the page says -- the live count on the
    overview, the outcome (one set of verbs) and a needs-you on a run."""
    text = page_text()
    render = text[text.index("function render()"):text.index("window.addEventListener")]
    assert "document.title=" in render
    assert '" running, ' in render and '"qwen watch"' in render
    assert _page_true('titleFor({state:"killed",title:"x"})==="Stopped: x - qwen watch"&&'
                      'titleFor({state:"ended",exit:2,title:"y"})==="Failed: y - qwen watch"&&'
                      'titleFor({state:"ended",exit:0,title:"y"})==='
                      '"Finished: y - qwen watch"&&'
                      'titleFor({state:"running",title:"z",attention:[{t:1}]})==='
                      '"Needs you: Running: z - qwen watch"')


def test_the_dead_css_is_gone():
    """Finding 25: the rules no markup ever produced -- the old <details> finisher,
    the table the page never renders, the quiet tile -- are out of the file."""
    text = page_text()
    for dead in (".fin ", ".fin{", ".fin[", "table{", "th,td{", "th:first-child",
                 ".tablewrap", ".tile.quiet"):
        assert dead not in text, dead                          # gone, every one
    assert ".frows li{" in text                                # the live siblings stayed


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_phases_subtitle_only_says_how_far_the_outline_got():
    """Finding 26: "Phases" keeps its name and the rail keeps its placeholders --
    the subtitle is only "X of Y started"; the hero's now line already names who
    is working and who is waiting."""
    assert _page_true('var h=detail({id:"r",kind:"swarm",state:"running",title:"t",'
                      'elapsed:5,units:{a:{state:"running",role:"r"}},'
                      'phase:{label:"p",n:1,units:["a"]},'
                      'phases:[{label:"p",n:1,end:null,units:["a"]}],'
                      'planned:[{label:"q",n:1}],recent:[]});'
                      '!/<h2>Phases <small>[^<]*working/.test(h)&&'
                      '/<h2>Phases <small>1 of 2 started<\\/small>/.test(h)')
    text = page_text()
    assert "Did not run" in text                               # the placeholders it keeps
    assert "not run\":\"did not run" in text                   # the same words in agent rows
    assert "FEED_SHOW=8" in text                               # and eight feed lines


# ------------------------------------------------- the overview's moods and agent rows

def _swarm_events(run, names, now, attention_item=None, done=(), queued=()):
    """One phase over named units, all started at the same moment (the moods tests
    hand out activity files themselves; `done` marks units as finished, `queued`
    leaves them waiting for a seat): the events a moods test needs and nothing else."""
    lines = [ev_at("run_start", now - 300, workflow="w", goal="g"),
             ev_at("phase_start", now - 300, label="build", n=len(names), seats=len(names),
                   units=[{"name": n, "role": "reviewer"} for n in names])]
    lines += [ev_at("unit_start", now - 300, unit=n, role="reviewer", timeout=600)
              for n in names if n not in done and n not in queued]
    lines += [ev_at("unit_done", now - 200, unit=n, role="reviewer", ok=True,
                    cached=False, seconds=100) for n in done]
    if attention_item:
        lines.append(ev_at("attention", now - 100, item=attention_item,
                           reason="needs a login", detail=None))
    write_events(run, lines)


def test_list_runs_carries_capped_moods_for_a_running_swarm(tmp_path, monkeypatch):
    """A RUNNING swarm's /api/runs entry carries `moods`: one entry per running unit,
    in the fold's order (the order the page shows them), at most 16 -- the queued and
    finished units have no mood to show (a finished unit keeps its activity file all
    the same and still gets no entry), and a unit whose file folds to nothing is
    skipped so the units after it still get one. Each entry is the same per-unit
    activity fold run_detail uses, reduced to state/cls/since, plus role, started,
    timeout, ctx, win, tool and target (cut to 120 characters) and needs_you for the
    unit an attention item names."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    now = time.time()
    names = ["a1"] + ["n%02d" % i for i in range(1, 18)] + ["x18", "q1", "d1"]
    _swarm_events(run, names, now, attention_item="n02", done=("d1",), queued=("q1",))
    long_target = "y" * 200
    for i in range(1, 18):                                     # a1 and x18 write nothing
        act_lines(run / watch.ACTIVITY_DIR / ("n%02d.jsonl" % i),
                  [act("pre", now - 5, tool="Read", target=long_target,
                       ctx=107520, win=524288)])
    act_lines(run / watch.ACTIVITY_DIR / "d1.jsonl",
              [act("pre", now - 5, tool="Read", target="src/done.py")])  # done: no mood
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    entry = {r["id"]: r for r in watch.list_runs(now)}[rid]
    assert list(entry["moods"]) == ["n%02d" % i for i in range(1, 17)]   # running order,
    #                                                                     16 at most: the
    #                                                                     no-file unit is
    #                                                                     skipped over
    assert "a1" not in entry["moods"] and "x18" not in entry["moods"]
    assert "q1" not in entry["moods"] and "d1" not in entry["moods"]
    m = entry["moods"]["n01"]
    assert set(m) == {"state", "cls", "since", "role", "started", "timeout",
                      "ctx", "win", "tool", "target", "needs_you"}
    assert (m["state"], m["cls"], m["tool"]) == ("tool", "read", "Read")
    assert m["target"] == "y" * 120                            # cut to 120 characters
    assert m["since"] == pytest.approx(now - 5, abs=1)
    assert m["role"] == "reviewer" and m["started"] == pytest.approx(now - 300, abs=1)
    assert m["timeout"] == 600 and m["ctx"] == 107520 and m["win"] == 524288
    assert m["needs_you"] is False
    assert entry["moods"]["n02"]["needs_you"] is True          # the attention names it
    # the three pose keys are the fold's own, reduced (one source, no drift)
    folded = watch.activity_fold(str(run / watch.ACTIVITY_DIR / "n01.jsonl"), now)
    assert (m["state"], m["cls"], m["since"]) == (folded["state"], folded["cls"],
                                                  folded["since"])


def test_list_runs_has_no_moods_on_an_ended_run(tmp_path, monkeypatch):
    """An ended run carries no moods at all -- nobody is in a mood once the run is
    over, and the page must not keep a thinking robot grinning on a finished swarm."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    now = time.time()
    _swarm_events(run, ["a1"], now)
    act_lines(run / watch.ACTIVITY_DIR / "a1.jsonl",
              [act("pre", now - 5, tool="Read", target="src/x.py")])
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    entry = {r["id"]: r for r in watch.list_runs(now)}[rid]
    assert "moods" in entry and "a1" in entry["moods"]
    watch.finish(rid, 0)
    entry = {r["id"]: r for r in watch.list_runs(now)}[rid]
    assert entry["state"] == "ended" and "moods" not in entry


def test_the_overview_moods_fold_unit_activity_incrementally(tmp_path, monkeypatch):
    """The second list_runs call reads only the new bytes of each unit's activity
    file -- the way _fold_for caches events.jsonl by byte offset -- so a 200-agent
    swarm does not re-read every file every 2.5 s. An idle poll reads nothing; the
    poll after the hook appends reads exactly the new line; the fold of the cached
    state equals the plain fold of the same file; and wrecking the returned entry
    must not leave a trace in the cache."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    now = time.time()
    _swarm_events(run, ["a", "b"], now)
    path = os.path.join(str(run), watch.ACTIVITY_DIR, "a.jsonl")
    act_lines(path, [act("pre", now - 5, tool="Read", target="src/x.py")])
    committed = os.path.getsize(path)
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    log = []
    real_open = open

    class _Spied:
        def __init__(self, inner):
            self._inner = inner

        def seek(self, n):
            log.append(("seek", n))
            return self._inner.seek(n)

        def read(self, *a):
            data = self._inner.read(*a)
            log.append(("read", len(data)))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

    def spy(file, *a, **kw):
        fh = real_open(file, *a, **kw)
        return _Spied(fh) if str(file) == path else fh

    monkeypatch.setattr("builtins.open", spy)

    def poll():
        del log[:]
        return {r["id"]: r for r in watch.list_runs(now)}[rid]

    entry = poll()
    assert log == [("seek", 0), ("read", committed)]           # first poll: all of it
    entry = poll()
    assert log == [("seek", committed), ("read", 0)]           # nothing new: zero bytes
    entry["moods"]["a"]["state"] = "hacked"                    # wreck the returned copy
    entry["moods"]["a"]["target"] = "hacked"
    line = json.dumps(act("post", now - 4, tool="Read", target="src/x.py"))
    with real_open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    entry = poll()
    assert log == [("seek", committed), ("read", len(line) + 1)]   # only the new bytes
    m = entry["moods"]["a"]
    assert m["state"] == "thinking"                            # the post came in
    assert m["target"] is None                                 # not "hacked": the cache
    #                                                              stayed untouched
    committed += len(line) + 1
    folded = watch.activity_fold(path, now)                    # cached fold == plain fold
    assert (m["state"], m["cls"], m["since"]) == (folded["state"], folded["cls"],
                                                  folded["since"])


def test_skipped_and_planned_entries_carry_their_outline_order_position(tmp_path,
                                                                        monkeypatch):
    """run_detail hands the page the outline POSITION of every placeholder entry (and
    of every started phase that took one): a skipped phase must render before the
    phase the run jumped to, among the started ones, and only the page can put it
    there once it knows where the outline had each entry."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev_at("run_start", 0, workflow="w", goal="g"),
                       _plan(("probe", 3), ("review", None), ("report", 1), ("ship", 1)),
                       _phase("probe", 1, "a"), _phase("report", 2, "b")])
    rid = watch.register("swarm", "t", run_path=str(run), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["skipped"] == [{"label": "review", "n": None, "pos": 1}]
    assert st["planned"] == [{"label": "ship", "n": 1, "pos": 3}]
    assert [p["pos"] for p in st["phases"]] == [0, 2]          # where the outline had
    #                                                            the phases that ran
    f = watch.fold_events([ev_at("run_start", 0, workflow="w"),
                           ev_at("phase_start", 1, label="x", n=1, seats=1, units=["a"])])
    assert f["phases"][0]["pos"] is None                       # no outline: no position


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_skipped_phase_renders_in_outline_order_before_the_phase_it_jumped_to():
    """The passed-over placeholder must sit among the started phases at its outline
    position -- the run jumped from probe to report, so the reader sees the review it
    skipped BETWEEN them, not after the phases still to come (and a planned entry
    keeps its place behind the last started phase)."""
    h = _page_html('detail({id:"r",kind:"swarm",state:"running",title:"t",elapsed:5,'
                   'units:{a:{state:"done",role:"r",seconds:5},b:{state:"running",role:"r"}},'
                   'phase:{label:"report",n:1,units:["b"]},'
                   'phases:[{label:"probe",n:1,end:9,units:["a"],pos:0},'
                   '{label:"report",n:1,end:null,units:["b"],pos:2}],'
                   'planned:[{label:"ship",n:1,pos:3}],'
                   'skipped:[{label:"review",n:2,pos:1}],recent:[]})')
    i_probe = h.index('<span class="wl">probe</span>')
    i_skip = h.index('<span class="wl">review</span>')
    i_report = h.index('<span class="wl">report</span>')
    i_ship = h.index('<span class="wl">ship</span>')
    assert i_probe < i_skip < i_report < i_ship                # outline order, and the
    #                                                            placeholder stays quiet
    assert 'class="wave planned"' in h                         # skipped: a placeholder
    assert "2 agents, Skipped" in h                            # that says so
    assert h.count('<span class="wl">review</span>') == 1      # exactly once, in place


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_rows_pose_with_the_unit_mood():
    """The row robot does not paint every unit mid-stride: each poses with actPose
    -- the very mapping the run page's tiles use -- on its mood entry, and says the
    mood in words beside a ticking duration: "thinking", "waiting on a tool". A unit
    with no mood entry keeps the plain running pose and the word "working", and a run
    past the "No update for" threshold shows every row in the waiting pose however it
    last felt. No crew: these rows are the only robots the overview draws."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"aaaaaaaaaaaa",kind:"swarm",state:"running",title:"t",'
                   'elapsed:100,start:n-100,'
                   'units:{a:{state:"running",role:"checker",started:n-100,timeout:600},'
                   'b:{state:"running",role:"reader",started:n-100,timeout:600},'
                   'c:{state:"running",role:"tester",started:n-100,timeout:600}},'
                   'moods:{a:{state:"thinking",cls:null,since:n-90,role:"checker",'
                   'started:n-100,timeout:600,ctx:null,win:null,tool:null,target:null},'
                   'b:{state:"waiting",cls:"read",since:n-111,tool:"Read",'
                   'target:"src/x.py",role:"reader",started:n-100,timeout:600,'
                   'ctx:null,win:null}}}])')
    assert 'class="robot thinking' in h                        # the checker is composing
    assert 'class="robot waiting' in h                         # the reader sleeps on the call
    assert 'class="mood">thinking <span class="tick"' in h     # words + a ticking span
    assert 'class="mood">waiting on a tool <span class="tick"' in h
    assert '<span class="mood">working</span>' in h            # no mood entry: plainly so
    assert 'class="mate"' not in h                             # the crew strip is gone
    h2 = _page_html('var n=Date.now()/1000;'
                    'overview([{id:"aaaaaaaaaaaa",kind:"swarm",state:"running",title:"t",'
                    'elapsed:9*3600,start:n-9*3600,'
                    'units:{a:{state:"running",role:"checker",started:n-9*3600,'
                    'timeout:600}},moods:{a:{state:"thinking",since:n-9*3600}}}])')
    assert 'class="robot waiting' in h2                        # silent run: asleep
    assert 'class="robot thinking' not in h2                   # whatever it last said


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_row_shows_a_thinking_unit():
    """Each running card is a <section class="run"> (never a link: links cannot nest)
    whose header link names the run (data-fk="run:<id>") and which holds one compact
    row per running agent of THAT run in a <ul class="agents">: the small robot in
    its mood, the NAME as the row's link (data-fk="agent:<run>:<unit>" -- the card
    header already names the run), the role, the mood in words with a live duration
    (a ticking span, never a baked number), the elapsed against the timeout -- in the
    warn ink past it -- and the context use, in the warn ink above 85 %. Needs-you
    rows first, then the longest in its state."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"swarm",state:"running",'
                   'title:"fix the parser",elapsed:213,start:n-213,'
                   'units:{a:{state:"running",role:"reviewer",started:n-213,timeout:600},'
                   'b:{state:"running",role:"checker",started:n-700,timeout:600}},'
                   'moods:{a:{state:"thinking",cls:null,since:n-87,role:"reviewer",'
                   'started:n-213,timeout:600,ctx:107520,win:524288,tool:null,'
                   'target:null,needs_you:false},'
                   'b:{state:"thinking",cls:null,since:n-30,role:"checker",'
                   'started:n-700,timeout:600,ctx:460000,win:524288,tool:null,'
                   'target:null,needs_you:true}}}])')
    assert '<section class="run">' in h                         # the card is a section
    assert '<a class="run-head" href="#ab12cd34ef56" ' \
           'data-fk="run:ab12cd34ef56">' in h                   # its header links to the run
    assert '<div class="title">fix the parser</div>' in h       # the header names the run
    assert '<ul class="agents">' in h and h.count("<ul") == 1  # the run's rows are ONE ul, inside it
    assert 'class="robot thinking p-check"' in h                # reviewer: the checker
    assert ">a</b></a>" in h and ">reviewer</small>" in h       # the NAME is the row's link
    assert 'class="mood">thinking <span class="tick"' in h     # words + a ticking span
    assert 'data-fmt="dur"' in h
    assert "/ 10m" in h                                         # elapsed / the budget
    assert "105k / 512k" in h                                   # the context reading
    assert 'data-fk="agent:ab12cd34ef56:a"' in h               # the name link, focus-keyed
    assert 'data-fk="agent:ab12cd34ef56:b"' in h
    assert '<li class="attn">' in h                             # the needs-you row wears it
    assert h.index('agent:ab12cd34ef56:b') < h.index('agent:ab12cd34ef56:a')
    #                                                                     needs-you first
    assert 'class="atime over"' in h                            # 700s past a 600s budget
    assert 'class="actx hot"' in h                              # 460k of 512k: over 85 %
    assert 'class="arun"' not in h                              # no run-title link in the row


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_row_shows_a_coder_loop_testing():
    """A coder loop's row inside its card follows the step like its tile on the run
    page: the tester robot while testing with "running tests" and its live time, the
    checker while reviewing, the builder in the waiting pose between rounds -- plus
    its name (round and checks sit once, on the card). The card itself carries no
    activity line and no context meter of its own: the row is the one place that
    says what the loop is doing."""
    base = ('var n=Date.now()/1000;overview([{id:"cd12ef56ab12",kind:"until-done",'
            'state:"running",title:"add retry",elapsed:213,start:n-213,'
            'activity:{state:"done",cls:null,tool:null,target:null,since:n-1,'
            'ctx:107520,win:524288},'
            'round:{n:3,timeout:600,started:n-213},checks:{passed:5,total:7},'
            'step:%s}])')
    h = _page_html(base % '{name:"testing",since:n-22}')
    assert 'class="robot running p-net"' in h                   # the tester's net
    assert 'class="mood">running tests <span class="tick"' in h
    assert "checks passing" not in h                            # round + checks: once, on the card
    assert ">coder</b></a>" in h and ">coder loop</small>" not in h  # the name is the link; no role repeat
    assert 'data-fk="agent:cd12ef56ab12:coder"' in h
    assert "/ 10m" in h and "105k / 512k" in h
    assert 'class="act"' not in h                               # no activity line on the card
    h = _page_html(base % '{name:"reviewing",since:n-10}')
    assert 'class="robot running p-check"' in h                 # the checker's stamp
    assert "reviewing the diff" in h
    h = _page_html(base % '{name:"between",since:n-2}')
    assert 'class="robot waiting p-wrench"' in h                # asleep between rounds
    assert "waiting between rounds" in h


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_caps_at_six_rows_and_links_the_run():
    """Six rows at most inside one card -- nine working units are not an information
    wall on the landing page -- and the overflow is one "and N more" row linking to
    that run, focus-keyed like every other link."""
    h = _page_html('var us={},mo={},n=Date.now()/1000;'
                   'for(var i=0;i<9;i++){us["u"+i]={state:"running",role:"reader",'
                   'started:n-30,timeout:600};'
                   'mo["u"+i]={state:"thinking",since:n-30,role:"reader",started:n-30,'
                   'timeout:600}}'
                   'overview([{id:"aaaaaaaaaaaa",kind:"swarm",state:"running",'
                   'title:"t",elapsed:30,start:n-30,units:us,moods:mo}])')
    assert h.count('class="a1"') == 6                            # six rows, no more
    assert '<li class="am"><a href="#aaaaaaaaaaaa" ' \
           'data-fk="agent:aaaaaaaaaaaa:more">and 3 more</a></li>' in h
    assert "Active agents" not in h                              # the old roll is gone


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_no_running_unit_shows_no_list():
    """A running swarm (or research run) whose units are all queued or done shows NO
    list at all: nobody works, so the card says progress and time and nothing else
    -- an empty <ul> would be a fact-shaped hole, not a fact."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"swarm",state:"running",'
                   'title:"between phases",elapsed:213,start:n-213,'
                   'units:{a:{state:"done",role:"reviewer",seconds:5},'
                   'b:{state:"queued",role:"checker"}}}])')
    assert '<section class="run">' in h                          # the card is still there
    assert '<ul class="agents">' not in h                        # but no list, not an empty one
    assert 'class="a1"' not in h
    assert 'class="robot ' not in h                              # and no robot of its own either
    assert "<h1>Between steps</h1>" in h                      # nothing counts, said once


def test_card_agents_rows_are_two_lines_on_a_phone():
    """Phones (<=480 px): each row inside the card is two lines -- robot, name and
    mood on the first, time and context on the second -- and nothing scrolls
    sideways: the groups shrink (min-width:0) and the mood text wraps on word
    boundaries. The card's own padding carries the rows: they run full width."""
    text = page_text()
    assert re.search(r"@media \(max-width:480px\)\{[^@]*"
                     r"\.agents li\{display:block\}", text)   # the row stacks in two
    assert ".agents .a2{margin:2px 0 0 32px" in text           # the second line, under the name
    assert re.search(r"\.agents \.a1\{[^}]*flex-wrap:wrap[^}]*min-width:0", text)
    #                         whole items move to a new line before they shrink, and
    #                         words break only when one word is wider than the row
    #                         ("anywhere" let the mood shrink to one letter per line)
    assert re.search(r"\.agents \.a1 \.mood\{[^}]*overflow-wrap:break-word", text)
    assert ".agents .atime,.agents .actx{white-space:nowrap}" in text  # "1h / 1h 0m" whole
    assert ".run .agents{margin:10px -14px -14px" in text      # inside the card the rows
    assert ".run .agents li{padding-left:14px" in text          # fill the card's full width


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_card_agents_row_links_all_carry_a_data_fk():
    """Every link the poll may replace carries a stable data-fk (finding 2) -- in the
    card rows that is every name link and the overflow link, no exceptions."""
    h = _page_html('var us={},mo={},n=Date.now()/1000;'
                   'for(var i=0;i<18;i++){us["u"+i]={state:"running",role:"reader",'
                   'started:n-30,timeout:600};'
                   'mo["u"+i]={state:"thinking",since:n-30,role:"reader",started:n-30,'
                   'timeout:600}}'
                   'var R=[{id:"aaaaaaaaaaaa",kind:"swarm",state:"running",title:"t",'
                   'elapsed:30,start:n-30,units:us,moods:mo},'
                   '{id:"bbbbbbbbbbbb",kind:"until-done",state:"running",title:"loop",'
                   'elapsed:30,start:n-30,step:{name:"testing",since:n-5}},'
                   '{id:"cccccccccccc",kind:"agent",state:"running",title:"solo",'
                   'elapsed:30,start:n-30,activity:{state:"thinking",since:n-7}}];'
                   'overview(R)')
    blocks = re.findall(r'<ul class="agents">.*?</ul>', h, re.S)
    assert len(blocks) == 3                                      # one ul per card
    assert sum(b.count("<a ") for b in blocks) >= 6              # rows and the overflow
    assert all(b.count("<a ") == b.count('data-fk="') for b in blocks)  # every link keyed


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_overview_has_no_overlap():
    """The merged overview says each fact ONCE: one card per running run holding its
    agents' rows -- so a running swarm's page has no crew strip of robots (no
    class="crew", no class="mate" link), no standalone Active agents list and no
    Claude usage panel with totals. The card's own line carries the run's calls."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"swarm",state:"running",'
                   'title:"fix the parser",elapsed:213,start:n-213,'
                   'units:{a:{state:"running",role:"reviewer",started:n-213,timeout:600},'
                   'b:{state:"running",role:"checker",started:n-213,timeout:600}},'
                   'moods:{a:{state:"thinking",since:n-87,needs_you:true},'
                   'b:{state:"waiting",cls:"shell",since:n-30,tool:"Bash",'
                   'target:"make test",needs_you:false}},'
                   'claude:{n:2,cost_usd:0.12,unpriced:0}},'
                   '{id:"bbbbbbbbbbbb",kind:"agent",state:"ended",exit:0,'
                   'title:"u",elapsed:5,end:1}])')
    assert '<section class="run">' in h                          # the one card is there
    assert '<ul class="agents">' in h                            # and holds its rows
    assert 'class="crew"' not in h                               # no crew strip for a run...
    assert 'class="mate"' not in h                               # ...no robots, no +N chip
    assert "Active agents" not in h                              # no standalone roll
    assert "Claude usage" not in h                               # no panel of totals
    assert "2 Claude calls, $0.12" in h                          # the run's own line
    # the floor keeps its two lines of words: how many agents work, in how many runs
    w = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"a",elapsed:5,start:n-5},'
                   '{id:"cd12ef56ab12",kind:"agent",state:"running",'
                   'title:"b",elapsed:5,start:n-5}])')
    assert "<h1>2 agents at work</h1>" in w                      # one row each, said once
    assert "2 runs in progress" in w
    assert '<section class="run">' in w and 'data-fk="agent:ab12cd34ef56:agent"' in w
    o = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"a",elapsed:5,start:n-5}])')
    assert "<h1>1 agent at work</h1>" in o and "One run in progress" in o
    # and with nothing running, the empty state stays exactly as it was
    e = _page_html('overview([])')
    assert "<h1>All quiet</h1>" in e
    assert "Start a local agent and it appears here." in e
    assert e.count('class="mate" aria-hidden="true"') == 3       # the quiet queued trio


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_overview_nested_link():
    """The card is a section, never a wrapping link: its header link carries the
    run's data-fk (data-fk="run:<id>") and no <a> sits inside another <a> anywhere
    in the overview HTML -- rows link by name beside the header, not inside it."""
    h = _page_html('var n=Date.now()/1000;'
                   'var us={},mo={};for(var i=0;i<9;i++){us["u"+i]={state:"running",'
                   'role:"reader",started:n-30,timeout:600};'
                   'mo["u"+i]={state:"thinking",since:n-30}}'
                   'overview([{id:"aaaaaaaaaaaa",kind:"swarm",state:"running",title:"t",'
                   'elapsed:30,start:n-30,units:us,moods:mo,attention_count:2},'
                   '{id:"bbbbbbbbbbbb",kind:"agent",state:"ended",exit:0,title:"u",'
                   'elapsed:5,end:1}])')
    assert '<a class="run-head" href="#aaaaaaaaaaaa" ' \
           'data-fk="run:aaaaaaaaaaaa">' in h                    # the header link, keyed
    open_a = re.compile(r"<a[\s>]")
    i = 0
    while True:
        m = open_a.search(h, i)
        if not m:
            break
        close = h.find("</a>", m.start())
        assert close >= 0, "an <a> that never closes: " + h[m.start():m.start() + 80]
        inner = open_a.search(h, m.start() + 1, close)
        assert inner is None, "a link inside a link: " + h[m.start():inner.start()]
        i = close + 4


# ------------------------------------------------------- the claude usage ledger
# The durable history of the REAL Claude calls (advisor ask/plan/rescue, swarm
# claude-checks): one JSON line each, nine fields, no prompt text anywhere near it.

LEDGER_KEYS = {"t", "kind", "call", "role", "model", "cost_usd", "seconds", "ok", "run"}


def ledger_raw():
    """The ledger as bytes on disk, or "" when nothing has been written yet."""
    try:
        with open(watch.claude_ledger_path(), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def ledger_lines():
    return [json.loads(x) for x in ledger_raw().splitlines() if x.strip()]


def test_claude_ledger_records_advisor_and_claude_check_without_prompt_text(tmp_path):
    """Both writers land in the one durable file, each keeping only the nine allowed
    fields: a question, an item or an answer smuggled in on the record does not reach
    the disk at all. The file is 0600 on posix, opened APPEND, and its run is a watch
    id or null -- an owner PID (what QWEN_WATCH_PARENT carries) is not an id."""
    now = time.time()
    assert watch.record_claude_call({"kind": "advisor", "call": "ask", "role": "coder",
                                     "model": "claude-opus-5", "cost_usd": 0.21,
                                     "seconds": 12.5, "ok": True, "run": "ab12cd34ef56",
                                     "question": "SECRET-QUESTION", "prompt": "SECRET-PROMPT"})
    assert watch.record_claude_call({"kind": "claude-check", "call": "confirm-ui",
                                     "role": "claude-check", "model": "sonnet",
                                     "cost_usd": None, "seconds": 3.0, "ok": False,
                                     "run": None, "item": "SECRET-ITEM",
                                     "answer": "SECRET-ANSWER"})
    assert watch.record_claude_call({"kind": "advisor", "call": "ask", "run": "31337"})
    assert watch.claude_ledger_path() == str(tmp_path / "qwen-agent" / "claude-calls.jsonl")
    raw = ledger_raw()
    for secret in ("SECRET-QUESTION", "SECRET-PROMPT", "SECRET-ITEM", "SECRET-ANSWER",
                   "question", "prompt", "item", "answer"):
        assert secret not in raw
    a, b, c = ledger_lines()
    assert set(a) == LEDGER_KEYS and set(b) == LEDGER_KEYS and set(c) == LEDGER_KEYS
    assert (a["kind"], a["call"], a["role"], a["model"]) == ("advisor", "ask", "coder",
                                                             "claude-opus-5")
    assert (a["cost_usd"], a["seconds"], a["ok"], a["run"]) == (0.21, 12.5, True,
                                                                "ab12cd34ef56")
    assert a["t"] >= now                                      # stamped when it landed
    assert (b["kind"], b["call"], b["role"], b["ok"]) == ("claude-check", "confirm-ui",
                                                          "claude-check", False)
    assert b["cost_usd"] is None and b["run"] is None         # no run known: null
    assert c["run"] is None                                   # a PID is not a watch id
    if os.name == "posix":
        assert os.stat(watch.claude_ledger_path()).st_mode & 0o777 == 0o600
    # a line that does not parse (a writer cut off mid-append) is skipped, never raised,
    # and a readable line with no stamp does not become "now" on the read side
    with open(watch.claude_ledger_path(), "a", encoding="utf-8") as fh:
        fh.write("not json at all\n" + json.dumps({"kind": "advisor", "call": "ask"}) + "\n")
    assert watch.claude_summary()["all"]["n"] == 3


def test_claude_ledger_run_stamp_register_sets_for_children(tmp_path, monkeypatch):
    """register() stamps the run id into QWEN_WATCH_ID beside the child switches: a
    descendant that records a call (the advisor's MCP server, a swarm unit) inherits
    it, and record_claude_call takes the run from it when the caller knows none."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    rid = watch.register("swarm", "stamper", run_path=str(tmp_path / "r1"),
                         start_server=False)
    assert os.environ.get("QWEN_WATCH_ID") == rid
    assert watch.record_claude_call({"kind": "advisor", "call": "ask", "model": "opus",
                                     "cost_usd": 0.1, "seconds": 1.0, "ok": True})
    assert ledger_lines()[-1]["run"] == rid


def test_claude_ledger_write_error_never_fails_the_call(tmp_path, monkeypatch):
    """The ledger is fire-and-forget: no error of its own -- the cache folder occupied
    by a file, the ledger path itself a directory, a path function that raises -- may
    fail or delay the call it records. It reports False and stays silent."""
    rec = {"kind": "advisor", "call": "ask", "model": "opus", "cost_usd": 0.1,
           "seconds": 2.0, "ok": True}
    (tmp_path / "qwen-agent").write_text("occupied", encoding="utf-8")
    assert watch.record_claude_call(rec) is False             # the folder cannot be made
    (tmp_path / "qwen-agent").unlink()
    os.makedirs(os.path.join(str(tmp_path), "qwen-agent", "claude-calls.jsonl"))
    assert watch.record_claude_call(rec) is False             # the file cannot be opened

    def boom():
        raise RuntimeError("no path for you")

    monkeypatch.setattr(watch, "claude_ledger_path", boom)
    assert watch.record_claude_call(rec) is False             # not even a broken path
    assert watch.record_claude_call("nope") is False          # not a record at all:
                                                              # folded away before the
                                                              # path is ever asked


def test_claude_ledger_rereads_only_when_size_or_mtime_changes(tmp_path):
    """The reader caches on (size, mtime): a rewrite of the same bytes at the same
    instant is NOT noticed (the poll does not re-read a file nobody appended to), and
    a fresh mtime is."""
    assert watch.record_claude_call({"kind": "advisor", "call": "ask", "role": "coder",
                                     "model": "m1", "cost_usd": 0.1, "seconds": 1,
                                     "ok": True})
    assert watch.claude_summary()["roles"][0]["models"] == {"m1": 1}
    path = watch.claude_ledger_path()
    st = os.stat(path)
    data = ledger_raw()
    with open(path, "w", encoding="utf-8", newline="") as fh:  # no CRLF on Windows
        fh.write(data.replace('"m1"', '"m2"'))                # same bytes...
    assert os.path.getsize(path) == st.st_size
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))       # ...written at the same stamp
    assert watch.claude_summary()["roles"][0]["models"] == {"m1": 1}   # still the cached read
    # a full second ahead: an ns bump could round back onto a coarse-filesystem stamp
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000000))
    assert watch.claude_summary()["roles"][0]["models"] == {"m2": 1}   # noticed at last


def test_claude_summary_buckets_today_week_roles_and_since():
    """today counts from LOCAL midnight, week over the last 7 days, all over the file;
    roles covers the week, most-called first; cost_usd sums the known costs and
    unpriced counts the calls that reported none; since is the oldest stamp."""
    now = time.time()
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    assert midnight <= now < midnight + 86400                 # the day the buckets read

    def add(**over):
        rec = {"kind": "advisor", "call": "ask", "role": "coder", "model": "opus",
               "cost_usd": 0.1, "seconds": 5.0, "ok": True, "run": None}
        rec.update(over)
        assert watch.record_claude_call(rec)

    # every stamp and the summary's clock come from ONE captured now: a wall clock
    # crossing midnight mid-test must not move the buckets under the assertions
    add(t=now)                                                # today
    add(t=now, cost_usd=None, model="sonnet", call="rescue")  # today, no cost
    add(t=midnight - 1, cost_usd=0.20, role="auditor", call="plan")   # yesterday: week
    add(t=now - 8 * 86400, cost_usd=1.00)                     # last week: all only
    s = watch.claude_summary(now)
    assert (s["today"]["n"], s["today"]["unpriced"]) == (2, 1)
    assert s["today"]["cost_usd"] == pytest.approx(0.10)
    assert (s["week"]["n"], s["week"]["unpriced"]) == (3, 1)
    assert s["week"]["cost_usd"] == pytest.approx(0.30)
    assert (s["all"]["n"], s["all"]["unpriced"]) == (4, 1)
    assert s["all"]["cost_usd"] == pytest.approx(1.30)
    assert s["since"] == pytest.approx(now - 8 * 86400, abs=2)
    coder, auditor = s["roles"]                               # n descending
    assert (coder["role"], coder["n"], coder["unpriced"]) == ("coder", 2, 1)
    assert coder["cost_usd"] == pytest.approx(0.10)
    assert coder["models"] == {"opus": 1, "sonnet": 1} and coder["calls"] == {"ask": 1, "rescue": 1}
    assert (auditor["role"], auditor["n"]) == ("auditor", 1)
    assert auditor["cost_usd"] == pytest.approx(0.20) and auditor["unpriced"] == 0
    assert auditor["models"] == {"opus": 1} and auditor["calls"] == {"plan": 1}


def test_claude_summary_tolerates_a_missing_ledger_as_zeros(tmp_path):
    """No file yet (nobody has called) is not an error: zero counts, no roles and
    since null; an empty or junk file reads the same way."""
    assert not os.path.exists(watch.claude_ledger_path())
    s = watch.claude_summary()
    assert s["today"]["n"] == 0 and s["week"]["n"] == 0 and s["all"]["n"] == 0
    assert s["all"]["cost_usd"] == 0 and s["all"]["unpriced"] == 0
    assert s["roles"] == [] and s["since"] is None
    os.makedirs(os.path.dirname(watch.claude_ledger_path()))
    with open(watch.claude_ledger_path(), "w", encoding="utf-8") as fh:
        fh.write("garbage\n{not json\n")
    assert watch.claude_summary()["all"]["n"] == 0
    assert watch.claude_run_view({"id": "ab12cd34ef56"}) == {"n": 0, "cost_usd": 0.0,
                                                             "unpriced": 0, "roles": []}


def test_claude_summary_api_runs_and_run_carry_the_role_breakdown(tmp_path, monkeypatch):
    """/api/runs answers a top-level `claude` (buckets + roles + since), and
    /api/run/<id> its own `claude` -- the calls whose run is the run's id or one of
    its child ids (a pointer registered under the run's own folder)."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev("run_start", workflow="swarm", goal="g")])
    rid = watch.register("swarm", "ledger run", run_path=str(run), start_server=False)
    child_id = "abcdef012345"                                 # nested under run/
    watch._write_json(watch._pointer_path(child_id),
                      pointer(id=child_id, run_path=str(run / "nested")))
    now = time.time()
    # register() stamped QWEN_WATCH_ID for its CHILDREN; these lines name their own
    # runs, so the stamp goes (a plain pop: it must stay absent for the whole test)
    os.environ.pop("QWEN_WATCH_ID", None)

    def add(**over):
        rec = {"kind": "advisor", "call": "ask", "role": "coder", "model": "opus",
               "cost_usd": 0.10, "seconds": 2.0, "ok": True, "run": rid, "t": now}
        rec.update(over)
        assert watch.record_claude_call(rec)

    add()                                                     # this run
    add(run=child_id, role="auditor", call="rescue", cost_usd=0.25)   # its child
    add(run=None, call="plan", cost_usd=None)                 # nobody's: overview only
    add(run="ffffffffffff", model="sonnet")                   # another run's: not this page
    httpd = watch.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return json.loads(resp.read())
        body = get("/api/runs")
        assert sorted(r["id"] for r in body["runs"]) == sorted([rid, child_id])
        c = body["claude"]                                    # beside runs and page
        assert set(c) == {"today", "week", "all", "roles", "since"}
        assert c["all"]["n"] == 4 and c["today"]["n"] == 4
        assert c["today"]["unpriced"] == 1
        assert c["today"]["cost_usd"] == pytest.approx(0.45)
        assert [g["role"] for g in c["roles"]] == ["coder", "auditor"]   # n descending
        d = get("/api/run/" + rid)["claude"]                  # the run plus its child
        assert d["n"] == 2 and d["unpriced"] == 0
        assert d["cost_usd"] == pytest.approx(0.35)
        assert [(g["role"], g["n"], g["calls"]) for g in d["roles"]] == [
            ("auditor", 1, {"rescue": 1}), ("coder", 1, {"ask": 1})]   # ties: by name
        kid = get("/api/run/" + child_id)["claude"]           # the child on its own too
        assert kid["n"] == 1 and kid["roles"][0]["role"] == "auditor"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_claude_list_runs_per_run(tmp_path, monkeypatch):
    """Additive API: every list_runs entry whose run made Claude calls carries its
    own `claude`: {n, cost_usd, unpriced} -- the same calls claude_run_view counts
    for that run (its id plus its CHILD ids: a pointer registered under the run's
    own folder), over the run's WHOLE ledger. A run that made no calls carries no
    `claude` key at all; nobody's calls (run null) belong to no entry. The map is
    built once per ledger re-read, so a second poll reads the same counts for free."""
    monkeypatch.delenv("QWEN_WATCH", raising=False)
    run = tmp_path / "run"
    write_events(run, [ev("run_start", workflow="swarm", goal="g")])
    rid = watch.register("swarm", "carded", run_path=str(run), start_server=False)
    solo = tmp_path / "solo"
    write_events(solo, [ev("run_start", goal="g")])
    other = watch.register("agent", "quiet", run_path=str(solo), start_server=False)
    child_id = "abcdef012345"                                 # nested under run/
    watch._write_json(watch._pointer_path(child_id),
                      pointer(id=child_id, run_path=str(run / "nested")))
    now = time.time()
    # register() stamped QWEN_WATCH_ID for its CHILDREN; these lines name their own
    # runs, so the stamp goes (a plain pop: it must stay absent for the whole test)
    os.environ.pop("QWEN_WATCH_ID", None)

    def add(**over):
        rec = {"kind": "advisor", "call": "ask", "role": "coder", "model": "opus",
               "cost_usd": 0.06, "seconds": 2.0, "ok": True, "run": rid, "t": now}
        rec.update(over)
        assert watch.record_claude_call(rec)

    add()                                                     # this run, priced
    add()                                                     # ...twice: $0.12 in all
    add(run=child_id, role="auditor", call="rescue", cost_usd=None)   # its child, no cost
    add(run=None, call="plan")                                # nobody's: no entry has it
    entries = {r["id"]: r for r in watch.list_runs(now)}
    assert entries[rid]["claude"] == {"n": 3, "cost_usd": pytest.approx(0.12),
                                      "unpriced": 1}          # own calls plus the child's
    assert entries[child_id]["claude"] == {"n": 1, "cost_usd": 0.0, "unpriced": 1}
    assert "claude" not in entries[other]                     # a quiet run: no key
    again = {r["id"]: r for r in watch.list_runs(now)}        # the cached map, unchanged
    assert again[rid]["claude"] == {"n": 3, "cost_usd": pytest.approx(0.12),
                                    "unpriced": 1}


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_claude_run_page_section_keeps_the_role_sections():
    """The run page's Claude calls section (the only Claude block the page has --
    the overview shows each card's own line, never totals): one section with the
    heading and the run's count and cost, one line per role with its count, cost,
    models and calls broken down. A run that made no calls shows nothing at all.
    A cost above 0 that rounds to zero reads "<$0.01", and the calls without one
    are counted. (The overview panel and its collapse control are gone: the cards
    carry the facts, see test_claude_card_*.)"""
    assert _page_true('claudeRunPanel(null)===""&&claudeRunPanel({n:0})===""')
    h = _page_html('claudeRunPanel({n:2,cost_usd:0.35,unpriced:0,roles:[{role:"coder",'
                   'n:2,cost_usd:0.35,unpriced:0,models:{opus:2},calls:{ask:2}}]})')
    assert '<section class="claude" data-sec="claude">' in h   # one section, own data-sec
    assert "<h2>Claude calls <small>" in h and "2 calls, $0.35" in h
    assert "coder</b>: 2 calls" in h and "(ask 2)" in h       # the role section
    assert "opus 2" in h                                      # models used, with counts
    h = _page_html('claudeRunPanel({n:1,cost_usd:0.004,unpriced:1,roles:[]})')
    assert "<$0.01" in h and "(1 without a cost)" in h        # rounding, the unpriced count
    assert _page_true('fmtCost(0)===\'$0.00\'&&fmtCost(0.004)===\'<$0.01\'&&'
                      'fmtCost(0.42)===\'$0.42\'&&fmtCost(2.1)===\'$2.10\'&&'
                      'fmtCost(null)===\'$0.00\'')
    assert '<section class="claude" data-sec="claude">' in page_text()  # the CSS card
    assert "claudeRunPanel(r.claude)" in _page_fn("detail")    # the run view hook


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_claude_card_sub_line_shows_the_run_calls():
    """A running card whose run made Claude calls says so in ITS sub line, with the
    run's own calls and cost only (the API hands the entry its `claude`): "2 Claude
    calls, $0.12", singular when it is one, and the under-a-cent marker when the
    cost is too small to round up."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"t",elapsed:30,start:n-30,'
                   'claude:{n:2,cost_usd:0.12,unpriced:0}}])')
    assert '<section class="run">' in h                        # the card is there
    assert "2 Claude calls, $0.12" in h
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"t",elapsed:30,start:n-30,'
                   'claude:{n:1,cost_usd:0.004,unpriced:0}}])')
    assert "1 Claude call, <$0.01" in h                        # singular, under a cent


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_claude_card_notes_calls_without_a_cost():
    """A call that reported no cost is counted in the open: the card's line adds
    "(N without a cost)" beside the sum, in the warn ink."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"t",elapsed:30,start:n-30,'
                   'claude:{n:3,cost_usd:0.12,unpriced:1}}])')
    assert "3 Claude calls, $0.12" in h and "(1 without a cost)" in h


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_claude_card_says_nothing_for_a_run_without_calls():
    """No calls: the sub line says nothing about Claude -- and a finished row never
    shows the field whatever its entry carries."""
    h = _page_html('var n=Date.now()/1000;'
                   'overview([{id:"ab12cd34ef56",kind:"agent",state:"running",'
                   'title:"t",elapsed:30,start:n-30,'
                   'claude:{n:0,cost_usd:0,unpriced:0}}])')
    assert '<section class="run">' in h                        # the card is there
    assert "Claude" not in h                                   # ...and Claude is not (n:0 too)
    h = _page_html('overview([{id:"bbbbbbbbbbbb",kind:"agent",state:"ended",exit:0,'
                   'title:"u",elapsed:5,end:1,claude:{n:4,cost_usd:0.5,unpriced:0}}])')
    assert '<li class="row">' in h                             # the finished row
    assert "Claude" not in h                                   # never carries the line
