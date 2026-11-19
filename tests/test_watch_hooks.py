"""Task 2 of the watch page: the engine's phase events, and every launcher's run pointer.

What is pinned here is the contract the page is built on: a phase announces itself before
any of its units starts, a unit says it started only when it really spawned, and each
launcher -- the swarm runner, the until-done supervisor, the single-shot shell script --
leaves EXACTLY ONE pointer for its own run, closed with its exit code on the way out.
"Exactly one" holds because `register` puts QWEN_WATCH_CHILD=1 -- and its owner stamp
QWEN_WATCH_PARENT=<own pid> -- into the environment for the registered process's
children, so a swarm unit's agent or a supervisor round's launcher never registers a run
of its own (the shell launcher re-exports the pair as its own $$ after registering) -- and
one mode registers none at all: a detached `qwen-agent -w`, whose pointer would belong to
the parent's already-dead pid.

Every test switches the page ON (QWEN_WATCH=1) and pins the registry under its own
tmp_path, with QWEN_WATCH_NOSERVE=1 so no real viewer is spawned under the suite (the one
real server is test_watch.py's); the one test that exercises a held port takes that switch
off, and nothing spawns there either -- a port that answers is never treated as empty. The
session-wide QWEN_WATCH=0 (tests/conftest.py) is what keeps every OTHER test out of the
registry entirely."""
import contextlib
import json
import os
import pathlib
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

import lib.swarm as sw
from lib import supervisor, watch
from lib.swarm_engine import events
from test_cli import fake, server              # noqa: F401  (the fixture is used by name --
                                               # the CLI suite's fake claude and /v1/models)
from test_swarm import fake as fake_swarm, swarm, unit  # noqa: F401  (the fixture is used by name)
from test_swarm_runner import echo, swarm_main
from test_supervisor import env, script, sup   # noqa: F401  (the fixture is used by name)
import test_cli
from swarm_fixtures import FAKE


@pytest.fixture(autouse=True)
def _watch_home(tmp_path, monkeypatch):
    """The registry and the config file under this test's tmp_path, no real config file
    able to move the host or port, and no inherited child switch or owner stamp:
    register() writes QWEN_WATCH_CHILD and QWEN_WATCH_PARENT straight into os.environ,
    and a previous test's run set them."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("QWEN_CONFIG", str(tmp_path / "no-such-config"))
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
    monkeypatch.delenv("QWEN_WATCH_PARENT", raising=False)
    # the link lines these tests compare against are built from host and port: a
    # developer's own QWEN_WATCH_* setting must not move them (lib/watch.py reads env first)
    monkeypatch.delenv("QWEN_WATCH_HOST", raising=False)
    monkeypatch.delenv("QWEN_WATCH_PORT", raising=False)


@pytest.fixture(autouse=True)
def _watch_on(monkeypatch):
    """The page is on for every test in this file (the session has it off), and the viewer
    is never spawned: these tests read the registry and the events, not a server."""
    monkeypatch.setenv("QWEN_WATCH", "1")
    monkeypatch.setenv("QWEN_WATCH_NOSERVE", "1")


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """swarm.install_stop_signals() takes SIGINT/SIGTERM/SIGHUP for the whole process;
    every test gets the handlers it found back (a run here calls runner.main)."""
    saved = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)              # SIGHUP is not everywhere
        if sig is not None:
            saved[sig] = signal.getsignal(sig)
    yield
    for sig, handler in saved.items():
        with contextlib.suppress(OSError, ValueError):  # only the main thread may change them
            signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def _no_agent_script_leak():
    """test_supervisor.script() names FAKE_AGENT_SCRIPT straight in os.environ; it belongs
    to this module's own run, and no later module should inherit it."""
    os.environ.pop("FAKE_AGENT_SCRIPT", None)
    yield
    os.environ.pop("FAKE_AGENT_SCRIPT", None)


# ---------------------------------------------------------------------- helpers

def events_of(run_dir):
    """RUN/events.jsonl as the engine wrote it, one dict per line."""
    text = (pathlib.Path(run_dir) / watch.EVENTS_FILE).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def lines_of(run_dir):
    """The same file as raw lines -- what watch.fold_events is handed."""
    return (pathlib.Path(run_dir) / watch.EVENTS_FILE).read_text(encoding="utf-8").splitlines()


def pointers():
    """Every pointer the run left in the registry, one dict each. The viewer's own
    server.json is not a pointer (it names no run) and is skipped by name."""
    d = pathlib.Path(watch.watch_dir())
    if not d.is_dir():
        return []
    return [json.loads((d / name).read_text(encoding="utf-8"))
            for name in sorted(os.listdir(str(d)))
            if name.endswith(".json") and name != watch.SERVER_FILE]


# ------------------------------------------------------------------ the engine

def test_run_phase_emits_phase_start_and_unit_start(tmp_path, fake_swarm):  # noqa: F811
    """run_phase announces the whole phase -- label, size, seats, units -- BEFORE its
    first unit spawns, and every unit that really runs says so on the way in; a phase
    nobody labelled numbers itself. That is all the page needs to show a phase running."""
    s = swarm(tmp_path, seats=2)
    events.emit(s.run_dir, "run_start", workflow="echo", goal="say hi")
    res = s.run_phase([unit(tmp_path, "a"), unit(tmp_path, "b"), unit(tmp_path, "c")],
                      seats=2, label="round 1: hypothesize")
    assert [r["name"] for r in res] == ["a", "b", "c"]
    assert all(r["ok"] and not r["cached"] for r in res), res
    evs = events_of(s.run_dir)
    assert [e["kind"] for e in evs[:2]] == ["run_start", "phase_start"]   # nothing in
    ph = evs[1]                                                            # between either
    assert set(ph) == {"kind", "t", "label", "n", "seats", "units"}
    assert ph["label"] == "round 1: hypothesize"
    assert ph["n"] == 3 and ph["seats"] == 2
    # a unit names itself both ways: "unit" is the event's own field, "name" is what the
    # watch fold reads a phase's items by (lib/watch.py fold_events)
    assert [(x["unit"], x["name"], x["role"]) for x in ph["units"]] == [
        ("a", "a", "worker"), ("b", "b", "worker"), ("c", "c", "worker")]
    starts = [e for e in evs if e["kind"] == "unit_start"]
    assert sorted(e["unit"] for e in starts) == ["a", "b", "c"]            # one per unit
    # unit_start carries the seconds budget the call is spawned with (the page's bar
    # divides by it): here the Swarm's own timeout, what --timeout the fake saw
    assert all(set(e) == {"kind", "t", "unit", "role", "timeout"}
               and e["role"] == "worker" and e["timeout"] == 60 for e in starts)
    assert all(e["ok"] and not e["cached"] for e in evs if e["kind"] == "unit_done")
    # an unlabelled phase is still a phase, numbered over this Swarm
    assert s.run_phase([unit(tmp_path, "d")])[0]["ok"]
    ph2 = [e for e in events_of(s.run_dir) if e["kind"] == "phase_start"][1]
    assert ph2["label"] == "phase 2" and ph2["n"] == 1 and ph2["seats"] == 2
    # and the fold turns the two kinds into the page's phase and unit states
    f = watch.fold_events(lines_of(s.run_dir))
    assert f["workflow"] == "echo" and f["goal"] == "say hi"
    assert f["phase"] == {"label": "phase 2", "n": 1, "seats": 2, "units": ["d"]}
    assert {n: u["state"] for n, u in f["units"].items()} == {
        "a": "done", "b": "done", "c": "done", "d": "done"}
    assert all(u["role"] == "worker" and u["state"] in watch.UNIT_STATES
               for u in f["units"].values())


def test_unit_start_carries_timeout(tmp_path, fake_swarm):  # noqa: F811
    """unit_start's timeout is the very seconds budget that unit's qwen-agent call is
    spawned with -- the Swarm's, the unit's own, or, for a review-round unit, the
    halved budget it actually gets -- and the fake records exactly that number at its
    --timeout, so the page's per-agent bar and the watchdog count down one clock."""
    s = swarm(tmp_path, seats=1)
    events.emit(s.run_dir, "run_start", workflow="echo", goal="say hi")
    a = unit(tmp_path, "a")                          # the Swarm's own timeout
    b = unit(tmp_path, "b")
    b.timeout = 90                                   # the unit's own
    c = unit(tmp_path, "c")
    c.deep = ("review_round",)                       # two calls share the budget
    c.timeout = 1200
    assert [r["ok"] for r in s.run_phase([a, b, c], seats=1, label="build")] == [True] * 3
    starts = {e["unit"]: e for e in events_of(s.run_dir) if e["kind"] == "unit_start"}
    assert starts["a"]["timeout"] == 60
    assert starts["b"]["timeout"] == 90
    assert starts["c"]["timeout"] == 600             # the halved budget, what it gets
    calls = [json.loads(x) for x in (fake_swarm / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]              # seats=1: the order is the deal order
    assert [x[x.index("--timeout") + 1] for x in calls] == ["60", "90", "600"]
    assert "--review-round" in calls[2]
    f = watch.fold_events(lines_of(s.run_dir))       # and the fold keeps it for the bar
    assert [f["units"][n]["timeout"] for n in ("a", "b", "c")] == [60, 90, 600]
    assert f["units"]["a"]["started"] == starts["a"]["t"]


def test_cached_unit_emits_no_unit_start(tmp_path, fake_swarm):  # noqa: F811
    """A cache hit is not work, and neither is a unit the run's deadline stopped before it
    started: no unit_start line for either, so the page never turns a spinner on a unit
    that was never running."""
    s = swarm(tmp_path, seats=1)
    first = s.run_phase([unit(tmp_path, "a")], label="build")
    assert first[0]["ok"] and not first[0]["cached"]
    again = swarm(tmp_path, seats=1)                     # a fresh Swarm over the same folder
    second = again.run_phase([unit(tmp_path, "a")], label="build again")
    assert second[0]["ok"] and second[0]["cached"]
    evs = events_of(s.run_dir)
    assert [e["unit"] for e in evs if e["kind"] == "unit_start"] == ["a"]      # once only
    assert [e.get("cached") for e in evs if e["kind"] == "unit_done"] == [False, True]
    f = watch.fold_events(lines_of(s.run_dir))
    assert f["units"]["a"]["state"] == "cached"          # cached, never running
    assert f["units"]["a"]["seconds"] == 0.0
    # a unit the deadline stopped never started, and never reported a result either
    late = sw.Swarm([sys.executable, str(FAKE)], s.run_dir, seats=1, timeout=60, backoff=0,
                    deadline=time.time() - 1)
    stopped = late.run_phase([unit(tmp_path, "z")], label="too late")
    assert stopped[0]["deadline"] is True and not stopped[0]["ok"]
    evs = events_of(s.run_dir)
    assert "z" not in [e["unit"] for e in evs if e["kind"] == "unit_start"]
    assert "z" not in [e["unit"] for e in evs if e["kind"] == "unit_done"]
    assert watch.fold_events(lines_of(s.run_dir))["units"]["z"]["state"] == "queued"


def test_the_two_kinds_are_engine_kinds():
    """phase_start and unit_start belong to the engine: a workflow's own wf.event cannot
    write them, and events.py's docstring -- what a workflow author reads -- lists both."""
    assert {"phase_start", "unit_start"} <= events.ENGINE_KINDS
    doc = events.__doc__ or ""
    for line in ("phase_start  label, n, seats, units", "unit_start   unit, role"):
        assert line in doc
    assert "phase %d" not in doc                      # the default is the swarm's business
    assert "label" in doc and "cache hit" in doc


def test_a_phase_line_that_will_not_fit_still_folds(tmp_path):
    """phase_start is the first event that carries a LIST, and one line may not pass 4 KB.
    A roster too wide for a line goes out WITHOUT the roster but WITH its size (the
    emit_phase_start fallback keeps n): letting the clip ladder shorten the line would cut
    the unit names inside it, and a cut name stops matching the unit_start and unit_done
    lines, so the page would carry a unit that ran as one that never started and a phantom
    queued one beside it; a part-roster beside a true `n` would strand the progress bar at
    roster/n forever. A phase too wide to announce still has its n, and its units say who
    they are from their own events: progress over the no-roster phase counts the distinct
    units that report done or dropped since its phase_start (lib/watch.py). The fold
    survives all of that, a phase of no units, and a label carrying a byte that is not
    valid UTF-8."""
    run = tmp_path / "run"
    run.mkdir()

    def roster(n, width=1):
        name = "u" * width
        return [{"unit": "%s%d" % (name, i), "name": "%s%d" % (name, i), "role": "worker"}
                for i in range(n)]

    events.emit(run, "run_start", workflow="echo", goal="g")
    events.emit_phase_start(run, "round 1: fan out", 50, 8, roster(50))
    assert all(len(ln.encode("utf-8")) < events.MAX_LINE for ln in lines_of(run))
    f = watch.fold_events(lines_of(run))                # a normal fan-out: the whole roster
    assert f["phase"]["label"] == "round 1: fan out" and f["phase"]["n"] == 50
    assert f["phase"]["units"] == ["u%d" % i for i in range(50)]

    # too wide for one line, either by name length or by count: no roster, nothing that
    # pretends to be one -- but the size survives, and so does the bar off it
    for n, width in ((40, 40), (400, 1)):
        items = roster(n, width=width)
        label = "round %d: too wide" % n
        events.emit_phase_start(run, label, n, 8, items)
        ln = lines_of(run)[-1]
        assert len(ln.encode("utf-8")) < events.MAX_LINE and "[cut]" not in ln
        rec = json.loads(ln)                            # whole names, or no names at all
        assert set(rec) == {"kind", "t", "label", "n", "seats", "truncated"}
        assert rec["truncated"] is True and rec["label"] == label and rec["n"] == n
        f = watch.fold_events(lines_of(run))
        assert f["phase"] == {"label": label, "n": n, "seats": 8, "units": []}
        done = {u["unit"]: {"state": "done", "seconds": 1.0, "role": "worker", "why": ""}
                for u in items}
        # every one of the n announced itself done: 1.0, not stranded at roster/n
        assert watch._phase_progress(f, done) == (1.0, 0.0)
    events.emit(run, "unit_start", unit="u0", role="worker")
    events.emit(run, "unit_done", unit="u0", role="worker", ok=True, cached=False, seconds=1.0)
    assert watch.fold_events(lines_of(run))["units"]["u0"]["state"] == "done"

    # an over-wide event through the generic path still degrades to one short line
    events.emit(run, "phase_start", label="round 4", n=400, seats=8, units=roster(400))
    f = watch.fold_events(lines_of(run))
    assert f["recent"][-1]["kind"] == "phase_start" and f["recent"][-1]["truncated"] is True
    assert set(f["recent"][-1]) == {"kind", "t", "truncated"}
    assert len(lines_of(run)[-1].encode("utf-8")) < events.MAX_LINE
    assert f["phase"] == {"label": None, "n": 0, "seats": None, "units": []}
    assert watch._phase_progress(f, f["units"]) == (None, None)
    events.emit(run, "phase_start", label="nothing to do", n=0, seats=4, units=[])
    f = watch.fold_events(lines_of(run))              # an empty phase is an empty phase
    assert f["phase"] == {"label": "nothing to do", "n": 0, "seats": 4, "units": []}
    events.emit(run, "phase_start", label="patch \udcff round", n=1, seats=1,
                units=[{"unit": "p", "name": "p", "role": "worker"}])
    f = watch.fold_events(lines_of(run))              # read back strict UTF-8: it was written
    assert f["phase"]["label"].startswith("patch ") and f["phase"]["units"] == ["p"]


# --------------------------------------------------------------------- runners

def test_swarm_run_registers_and_finishes_one_pointer(tmp_path, fake_swarm, capsys):  # noqa: F811
    """One engine run leaves exactly one pointer: the run's own folder, the goal it was
    started with, and its exit once it is over -- and the agents it spawned add none of
    their own, because register() handed them QWEN_WATCH_CHILD."""
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    captured = capsys.readouterr()
    ps = pointers()
    assert len(ps) == 1, ps
    p = ps[0]
    assert p["kind"] == "swarm" and p["title"] == "say hi"
    # the runner resolves the folder it registers (runner.main), and its own lines name the
    # program (tests/test_swarm_runner.py:729 pins "qwen-swarm: run folder:")
    assert p["run_path"] == str(out.resolve()) and p["pid"] == os.getpid()
    assert p["end"] is not None and p["exit"] == 0
    # the link went to stderr right after the folder the run is in, and it is the bare
    # `watch: <url>` every launcher prints -- not a qwen-swarm line, so one grep finds it
    err = captured.err.splitlines()
    at = [n for n, ln in enumerate(err) if ln == "qwen-swarm: run folder: %s" % out.resolve()]
    assert at and err[at[0] + 1] == "watch: " + watch.url(p["id"])
    # the page's own view of the finished run
    st = watch.run_detail(p["id"], time.time())
    assert st["state"] == "ended" and st["exit"] == 0
    assert st["workflow"] == "echo" and st["goal"] == "say hi"
    assert st["phase"]["label"] == "phase 1"            # the echo workflow names none
    assert sorted(st["units"]) == ["work-1", "work-2", "work-3"]
    assert all(u["state"] == "done" for u in st["units"].values())
    assert st["progress"] == 1.0
    # every unit's agent saw the child switch: that is what kept the count at one
    envs = [json.loads(x) for x in (fake_swarm / "env.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert envs and all(e.get("QWEN_WATCH_CHILD") == "1" for e in envs)
    assert all(e.get("QWEN_WATCH") == "1" for e in envs)


def test_swarm_run_with_the_page_off_registers_nothing(tmp_path, fake_swarm, monkeypatch,  # noqa: F811
                                                       capsys):
    """QWEN_WATCH=0 is the switch, not a broken page: no pointer, no link line, and the
    run is exactly the run it was before the watch page existed."""
    monkeypatch.setenv("QWEN_WATCH", "0")
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    captured = capsys.readouterr()
    assert pointers() == []
    assert not os.path.isdir(watch.watch_dir())          # nothing written at all
    assert not [ln for ln in captured.err.splitlines() if "watch:" in ln]
    assert [ln for ln in captured.err.splitlines() if "run folder:" in ln]


def test_watch_failure_never_changes_exit_code(tmp_path, fake_swarm, monkeypatch, capsys):  # noqa: F811
    """The best-effort promise, on the failure the page can really meet: another service
    holds QWEN_WATCH_PORT. The run prints one `watch: ...` warning line, still gets its
    pointer, and leaves by exactly the exit the same run leaves by with the page off."""
    junk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    junk.bind(("127.0.0.1", 0))                          # a stranger owns this port
    junk.listen(8)
    junk_port = junk.getsockname()[1]                    # snapshot: the finally closes junk
    stop = threading.Event()

    def serve_junk():
        while not stop.is_set():
            try:
                conn, _ = junk.accept()
            except OSError:
                return
            with conn:
                with contextlib.suppress(OSError):
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    thread = threading.Thread(target=serve_junk, daemon=True)
    thread.start()
    monkeypatch.setattr(watch, "HEALTH_TIMEOUT", 5.0)    # never let a loaded box time out
    script_path = echo(tmp_path)
    try:
        # first the baseline: the same run with the page switched off entirely
        monkeypatch.setenv("QWEN_WATCH", "0")
        base = swarm_main(script_path, "say hi", "--out", str(tmp_path / "base"))
        capsys.readouterr()
        # then with the page on and its port held by the junk listener
        monkeypatch.setenv("QWEN_WATCH", "1")
        monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
        monkeypatch.delenv("QWEN_WATCH_NOSERVE", raising=False)   # the probe really runs
        monkeypatch.setenv("QWEN_WATCH_PORT", str(junk_port))
        held = swarm_main(script_path, "say hi", "--out", str(tmp_path / "held"))
        err = capsys.readouterr().err
        assert held == base == 0
        # a page that could not come up costs the run neither its pointer nor its exit
        ps = pointers()
        assert len(ps) == 1 and ps[0]["run_path"] == str((tmp_path / "held").resolve())
        assert ps[0]["end"] is not None and ps[0]["exit"] == 0
        # at most one warning line, and it is watch._warn's own. The link line is a bare
        # `watch: <url>` as well now -- every launcher prints the same one -- so it is
        # excluded by name rather than by a program prefix that no longer tells them apart
        link = "watch: " + watch.url(ps[0]["id"])
        warn = [ln for ln in err.splitlines() if ln.startswith("watch:") and ln != link]
        assert len(warn) == 1, err                       # one warning, no more
        assert "not " + watch.APP in warn[0]             # and it names what went wrong
        assert link in err.splitlines()                  # the link still went out, unprefixed
    finally:
        stop.set()
        try:
            # unstick the accept() the thread is parked in: closing a listener under a
            # blocked accept() is unspecified on Linux
            wake = socket.create_connection(("127.0.0.1", junk_port), timeout=5)
            wake.close()
        except OSError:
            pass                                         # already gone: the thread woke
        junk.close()
        thread.join(timeout=5)


def test_a_junk_port_costs_the_link_and_nothing_else(tmp_path, fake_swarm, env, server, fake,  # noqa: F811
                                                     monkeypatch, capsys):
    """QWEN_WATCH_PORT is read with int() and has no fallback (watch.watch_port), so a value
    that is not a number -- a proxy-style "8790,8791" -- makes watch.url() raise, and it
    raises AFTER the pointer was written. The run must still leave by its own exit, its
    pointer must still be closed, and its one warning must say the LINK failed rather than
    that the run was never registered: it was registered, and `python lib/watch.py url ID`
    still gives the page's address. All three launchers take the same shape."""
    monkeypatch.setenv("QWEN_WATCH_PORT", "8790,8791")
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    err = capsys.readouterr().err
    ps = pointers()
    assert len(ps) == 1 and ps[0]["end"] is not None and ps[0]["exit"] == 0, ps
    lines = [ln for ln in err.splitlines() if ln.startswith("watch:")]
    assert len(lines) == 1, err                           # the warning, and no link beside it
    assert "the page link failed" in lines[0], lines
    assert ps[0]["id"] in lines[0]                        # it hands over the id to build with
    assert "could not be registered" not in err           # the run IS on the page: it was
    assert [ln for ln in err.splitlines() if "run folder:" in ln]   # the run's own lines intact

    repo, task, tmp = env
    script(tmp, [{"result": "tried"},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)  # the runner's register left it set
    assert sup(repo, task, "--max-rounds", "3") == 0
    err = capsys.readouterr().err
    ps = [p for p in pointers() if p["kind"] == "until-done"]
    assert len(ps) == 1 and ps[0]["end"] is not None and ps[0]["exit"] == 0, ps
    lines = [ln for ln in err.splitlines() if ln.startswith("watch:")]
    assert len(lines) == 1 and "the page link failed" in lines[0], err
    assert ps[0]["id"] in lines[0] and "could not be registered" not in err

    # the shell launcher wraps the two calls apart as well: the script survives a url()
    # that cannot be built, and the id it wrote for cleanup is still the run's own
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
    r = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake,
                     extra={"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
                            "QWEN_WATCH_PORT": "8790,8791"})
    assert r.returncode == 0, r.stderr
    ps = [p for p in pointers() if p["kind"] == "agent"]
    assert len(ps) == 1 and ps[0]["end"] is not None and ps[0]["exit"] == 0, ps
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("watch:")]
    assert len(lines) == 1 and "the page link failed" in lines[0], r.stderr
    assert ps[0]["id"] in lines[0] and "could not be registered" not in r.stderr


# ------------------------------------------------------------------------ CLI

def test_qwen_agent_single_shot_registers_agent_pointer(tmp_path, server, fake):  # noqa: F811
    """A single-shot session puts itself on the page under its role and the first 60
    characters of the ask, and cleanup records its exit; the child switch and QWEN_WATCH=0
    each keep a second pointer out, so the whole run is one pointer."""
    ask = "review the diff and report every leaked secret in the config loader, then stop"
    assert len(ask) > 60                                 # the title is the ask's first 60
    on = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1", "QWEN_TIMEOUT": "300"}
    spawned = {}
    r = test_cli.run(tmp_path, ["-r", "auditor", ask], server, fake, extra=dict(on), proc=spawned)
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    p = ps[0]
    assert p["kind"] == "agent"
    assert p["title"] == "auditor: " + ask[:60]
    assert p["run_path"] is None                         # a session has no run folder
    assert p["pid"] == spawned["pid"] != os.getpid()     # the script's own process, $$
    assert p["timeout"] == 300                           # the script's own TIMEOUT
    assert p["max_rounds"] is None
    assert p["end"] is not None and p["exit"] == 0
    assert "watch: %s\n" % watch.url(p["id"]) in r.stderr
    # a launcher that is already a registered run's child never registers one of its own
    r2 = test_cli.run(tmp_path, ["-r", "auditor", ask], server, fake,
                      extra=dict(on, QWEN_WATCH_CHILD="1"))
    assert r2.returncode == 0, r2.stderr
    assert len(pointers()) == 1, pointers()
    assert not [ln for ln in r2.stderr.splitlines() if ln.startswith("watch:")]
    # and the switch off is likewise silent
    r3 = test_cli.run(tmp_path, ["-r", "auditor", ask], server, fake,
                      extra=dict(on, QWEN_WATCH="0"))
    assert r3.returncode == 0, r3.stderr
    assert len(pointers()) == 1, pointers()
    assert not [ln for ln in r3.stderr.splitlines() if ln.startswith("watch:")]


def test_single_shot_exports_child_flag(tmp_path, server):  # noqa: F811
    """The processes the single-shot launcher spawns are the registered run's children.
    python's register() sets QWEN_WATCH_CHILD=1 only in the child python's own
    environment -- which dies one line later -- so after a SUCCESSFUL registration the
    shell exports the switch itself, and the agent (and anything it runs) inherits it
    and never registers a pointer of its own. The fake claude records its environment;
    the flag is in it. An empty WATCH_ID (page off, python dead) sets nothing."""
    fake = tmp_path / "fake-env-claude"  # noqa: F811  (a path, not the imported fixture)
    fake.write_text("""#!/usr/bin/env bash
# like test_cli.FAKE_CLAUDE, minus the modes, plus the whole QWEN_ environment.
if [ "${1:-}" = --help ]; then
  echo "  --permission-mode <mode>  Permission mode to use for the session"
  echo "  --restricted              Restricted mode"
  exit 0
fi
env | grep '^QWEN_' | sort > "$FAKE_CHILD_ENV"
printf '%s\\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
""", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    on = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1", "QWEN_TIMEOUT": "300",
          "FAKE_CHILD_ENV": test_cli.posix(tmp_path / "child-env.txt")}
    r = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake, extra=dict(on))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1 and ps[0]["kind"] == "agent", ps     # the run, and no other
    assert ps[0]["end"] is not None and ps[0]["exit"] == 0
    child = {}
    for ln in (tmp_path / "child-env.txt").read_text(encoding="utf-8").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            child[k] = v
    assert child.get("QWEN_WATCH_CHILD") == "1"      # the export the spawn saw
    assert child.get("QWEN_WATCH") == "1"            # the page stays on for the child


def test_single_shot_exports_parent_pid(tmp_path, server):  # noqa: F811
    """The export says WHICH registration the spawn must not redo, not just that one
    exists: after a SUCCESSFUL registration the shell exports QWEN_WATCH_PARENT=$$
    beside the child switch -- its own pid, the very pid the pointer carries -- so a
    child's register() reads a parent that is not its own pid and refuses even when
    the CHILD flag was lost on the way (a re-exec, an env scrub). No registration,
    no exports: with the page off the shell sets neither switch."""
    fake = tmp_path / "fake-parent-claude"  # noqa: F811  (a path, not the imported fixture)
    fake.write_text("""#!/usr/bin/env bash
# like test_cli.FAKE_CLAUDE, minus the modes, plus the whole QWEN_ environment.
if [ "${1:-}" = --help ]; then
  echo "  --permission-mode <mode>  Permission mode to use for the session"
  echo "  --restricted              Restricted mode"
  exit 0
fi
env | grep '^QWEN_' | sort > "$FAKE_CHILD_ENV"
printf '%s\\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
""", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    on = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1", "QWEN_TIMEOUT": "300",
          "FAKE_CHILD_ENV": test_cli.posix(tmp_path / "parent-env.txt")}
    r = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake, extra=dict(on))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1 and ps[0]["kind"] == "agent", ps      # the run, and no other
    child = {}
    for ln in (tmp_path / "parent-env.txt").read_text(encoding="utf-8").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            child[k] = v
    assert child.get("QWEN_WATCH_PARENT") == str(ps[0]["pid"])   # the shell's own $$,
    # the pid the pointer was written under (qwen-agent.sh exports it after registering)
    assert child.get("QWEN_WATCH_CHILD") == "1"                  # the pair, together
    assert child.get("QWEN_WATCH") == "1"
    off = dict(on, QWEN_WATCH="0",
               FAKE_CHILD_ENV=test_cli.posix(tmp_path / "off-env.txt"))
    r2 = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake, extra=off)
    assert r2.returncode == 0, r2.stderr
    offenv = (tmp_path / "off-env.txt").read_text(encoding="utf-8")
    assert "QWEN_WATCH_PARENT=" not in offenv              # nothing was registered:
    assert "QWEN_WATCH_CHILD=" not in offenv               # the shell exports neither


def test_qwen_agent_title_of_a_prompt_file_is_its_name(tmp_path, server, fake):  # noqa: F811
    """A prompt read from a file gives the page the file's NAME only: the body is a prompt,
    and the page may never show one. The exits that leave the script early -- --dry-run and
    --preflight-only among them -- register nothing either."""
    ask = tmp_path / "the leaked secret ask.md"          # a space in the name, as any file may
    ask.write_text("the whole prompt body, which the page must never read\n", encoding="utf-8")
    on = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1"}
    r = test_cli.run(tmp_path, ["-r", "auditor", "--no-timeout", "-f", test_cli.posix(ask)],
                     server, fake, extra=dict(on))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["title"] == "auditor: the leaked secret ask.md"
    assert "prompt body" not in json.dumps(ps[0])        # nothing of it reached the registry
    assert ps[0]["timeout"] is None                      # --no-timeout: nothing to count down
    for args in (["--dry-run", "-r", "auditor", "hi"], ["--preflight-only"]):
        rr = test_cli.run(tmp_path, args, server, fake, extra=dict(on))
        assert rr.returncode == 0, (args, rr.stderr[-2000:])
        assert len(pointers()) == 1, (args, pointers())
        assert not [ln for ln in rr.stderr.splitlines() if ln.startswith("watch:")]


def test_a_detached_run_registers_nothing(tmp_path, server, fake):  # noqa: F811
    """`-w` is the one mode that must stay off the page: the branch below the registration
    hands TMPD to a detached subshell, drops the EXIT trap and exits 0, so a pointer written
    by the parent would belong to a pid that dies at once while the job goes on running --
    the page would read a live run as killed, and nothing would ever finish it."""
    r = test_cli.run(tmp_path, ["-w", "-o", test_cli.posix(tmp_path / "bg.md"), "hi"],
                     server, fake, extra={"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1"})
    assert r.returncode == 0, r.stderr
    assert pointers() == []                                # no pointer for a pid already gone
    assert not [ln for ln in r.stderr.splitlines() if ln.startswith("watch:")]


def test_until_done_registers_and_finishes(env, capsys):  # noqa: F811
    """The loop is the run: one `until-done` pointer, titled for the task FILE, pointed at
    the run folder, its rounds counted against --max-rounds, and closed with the exit."""
    repo, task, tmp = env
    script(tmp, [{"result": "tried"},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--max-rounds", "3") == 0
    ps = pointers()
    assert len(ps) == 1, ps                              # the rounds' agents add none
    p = ps[0]
    assert p["kind"] == "until-done" and p["title"] == "task.md"
    assert p["pid"] == os.getpid() and p["timeout"] is None
    assert p["max_rounds"] == 3                          # --max-rounds: the denominator
    assert p["end"] is not None and p["exit"] == 0
    report = next((tmp / "state").rglob("report.md"))
    assert p["run_path"] == str(report.parent)           # the folder the page follows
    assert "watch: %s\n" % watch.url(p["id"]) in capsys.readouterr().err
    st = watch.run_detail(p["id"], time.time())
    assert st["state"] == "ended" and st["exit"] == 0
    assert st["rounds"] == 2                             # counted by FILENAME only
    assert st["progress"] == pytest.approx(2 / 3)
    assert st["report_stop"] and st["report_stop"].startswith("stop:")


def test_until_done_finishes_the_pointer_on_every_exit(env):  # noqa: F811
    """A loop that never reaches its goal still closes its pointer: every way out of the
    run finishes it, with the exit the caller gets, so the page never keeps showing a loop
    that ended long ago as running."""
    repo, task, tmp = env
    script(tmp, [{"result": "nothing changes"}])
    assert sup(repo, task, "--max-rounds", "2") == 12    # the same failures twice: give up
    ps = pointers()
    assert len(ps) == 1, ps
    p = ps[0]
    assert p["kind"] == "until-done" and p["max_rounds"] == 2
    assert p["end"] is not None and p["exit"] == 12
    assert watch.run_detail(p["id"], time.time())["state"] == "ended"


def test_until_done_closes_the_pointer_when_the_run_crashes(env, monkeypatch):  # noqa: F811
    """A crash closes the pointer in both of its shapes: without --probe the exception runs
    straight through the finally and out to the caller, and with --probe the run reports and
    returns 8 -- either way that finally finishes the pointer with 8, so the page never goes
    on showing a loop that died as a loop that is still running. (A RuntimeError from the
    check runner would not exercise this: it is caught and reported as its own failure.)"""
    repo, task, tmp = env
    script(tmp, [{"result": "x"}])

    def boom(*a, **k):
        raise ValueError("the checks exploded")
    monkeypatch.setattr(supervisor.checks, "run_checks", boom)   # after the run folder exists
    with pytest.raises(ValueError):                     # no --probe: it goes on to the caller
        sup(repo, task)
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["end"] is not None and ps[0]["exit"] == 8
    assert watch.run_detail(ps[0]["id"], time.time())["state"] == "ended"
    # register() leaves QWEN_WATCH_CHILD=1 in THIS process's environment -- it is meant for
    # the registered run's children (lib/watch.py:216), and it is what keeps one run to one
    # pointer. A second run here is a second process in life, so the switch goes first: with
    # it up, the run below would register nothing and the count would lie.
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
    assert sup(repo, task, "--probe") == 8               # under --probe the 8 comes back as
    ps = pointers()                                       # a return, and it closes too
    assert len(ps) == 2, ps                               # each run its own pointer
    assert all(p["end"] is not None and p["exit"] == 8 for p in ps)
    assert len({p["id"] for p in ps}) == 2                # the run folders differ, so the ids


def test_until_done_closes_the_pointer_when_the_loop_is_interrupted(env, monkeypatch):  # noqa: F811
    """Ctrl-C on the way out of a round: the 130 path finishes the pointer like every
    other, so the page shows an interrupted run as ended at 130 rather than running."""
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}])
    real, n = supervisor.call_agent, {"calls": 0}

    def flaky(*a, **k):
        n["calls"] += 1
        if n["calls"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(supervisor, "call_agent", flaky)
    assert sup(repo, task) == 130
    ps = pointers()
    assert len(ps) == 1, ps
    assert ps[0]["end"] is not None and ps[0]["exit"] == 130
    assert watch.run_detail(ps[0]["id"], time.time())["state"] == "ended"


# ------------------------------------------------------------ the settings file

FAKE_CFG = r'''#!/usr/bin/env bash
# Records its argv and COPIES every *.json argument it is handed: the --settings file
# lives in the run's own temporary folder, which cleanup takes away with the run.
if [ "${1:-}" = --help ]; then
  echo "  --permission-mode <mode>  Permission mode to use for the session"
  echo "  --restricted              Restricted mode"
  exit 0
fi
{
  for a in "$@"; do printf 'ARG:%s\n' "$a"; done
} > "$FAKE_RECORD"
for _a in "$@"; do
  case "$_a" in
    *.json) [ -f "$_a" ] && cp "$_a" "$FAKE_CFG_DIR/saved.$(basename "$_a")" ;;
  esac
done
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
'''


def cfg_fake(tmp_path):
    """The fake claude, plus the folder its copy of the settings file lands in."""
    p = tmp_path / "fake-cfg-claude"
    p.write_text(FAKE_CFG, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "cfg").mkdir(exist_ok=True)
    return p


def cfg_on(tmp_path, **over):
    """The environment of a run that puts itself on the page: on, no viewer spawned, and
    the folder the fake copies the settings file it is handed into."""
    e = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
         "FAKE_CFG_DIR": test_cli.posix(tmp_path / "cfg")}
    e.update(over)
    return e


def saved_settings(tmp_path):
    """What the fake copied off the argv, as written."""
    p = tmp_path / "cfg" / "saved.settings.json"
    assert p.exists(), "the run passed no --settings file at all"
    return json.loads(p.read_text(encoding="utf-8"))


def act_entries(cfg):
    """The activity hook's four entries, keyed by event, however many hooks share them."""
    hooks = cfg.get("hooks", {})
    got = {}
    for kind in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"):
        for entry in hooks.get(kind, []):
            for h in entry.get("hooks", []):
                if h.get("command", "").endswith("watch_hook.py") \
                        or "watch_hook.py" in h.get("command", ""):
                    got[kind] = entry
    return got


def test_settings_file_holds_both_hooks_when_both_are_on(tmp_path, server):  # noqa: F811
    """--plan's hook and the activity hook share ONE settings file, because claude takes
    one --settings: the plan's entry is read in and the activity hook's four are appended
    beside it, the plan hook's matcher, timeout and command unchanged. PreToolUse,
    PostToolUse and PostToolUseFailure match every tool ("*"); Stop takes no matcher.
    Every one of the four runs the same command, and no argument names the activity file
    -- the session's own environment does (QWEN_WATCH_ACTIVITY), so the file holds no
    path of the run's."""
    fake = cfg_fake(tmp_path)  # noqa: F811  (a path, not the imported fixture)
    r = test_cli.run(tmp_path, ["--shallow", "-r", "coder", "-q", "--plan", "opus", "hi"],
                     server, fake, extra=cfg_on(tmp_path))
    assert r.returncode == 0, r.stderr
    argv, _ = test_cli.record(tmp_path)
    settings = test_cli.flag(argv, "--settings")
    assert settings and settings.endswith("settings.json")
    assert argv.count("--settings") == 1                    # one file, holding both
    assert "qwen-watch" in settings and "qwen-plan" not in settings   # the merged copy, not
    # the plan hook's own file: that one stays what --plan wrote
    cfg = saved_settings(tmp_path)
    plan = [e for e in cfg["hooks"]["PreToolUse"]
            if e.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit"]
    assert len(plan) == 1, cfg["hooks"]["PreToolUse"]        # the plan hook, intact...
    [ph] = plan[0]["hooks"]
    assert ph["type"] == "command" and ph["timeout"] == 660
    assert "advisor_hook.py" in ph["command"] and "--state" in ph["command"]
    act = act_entries(cfg)
    assert set(act) == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
    assert act["PreToolUse"]["matcher"] == "*" and act["PostToolUse"]["matcher"] == "*"
    assert act["PostToolUseFailure"]["matcher"] == "*"       # a failed call reports too
    assert "matcher" not in act["Stop"]                      # Stop has no matcher to give
    commands = {kind: act[kind]["hooks"][0]["command"] for kind in act}
    assert len(set(commands.values())) == 1                  # one command, four events
    parts = shlex.split(commands["Stop"])
    assert len(parts) == 2                                    # the interpreter and the hook
    assert parts[1].replace("\\", "/").endswith("lib/watch_hook.py")
    assert pathlib.Path(test_cli.same_path(parts[1])).is_absolute()   # what claude can run
    assert pathlib.Path(parts[0].replace("\\", "/")).name.startswith("python")
    for kind in act:                                         # every entry well-formed
        [h] = act[kind]["hooks"]
        assert h["type"] == "command" and set(h) == {"type", "command"}
    # the file holds no path of the run's: which file to append to is the session's own
    # environment's business (QWEN_WATCH_ACTIVITY), so one file serves every session
    assert "activity" not in json.dumps(cfg).lower()
    # and the run is on the page, so the hook does have a file to fill
    ps = pointers()
    assert len(ps) == 1 and ps[0]["kind"] == "agent", ps
    assert os.path.exists(watch.activity_path(ps[0]["id"])) is False   # the hook makes it


def test_settings_file_names_only_the_activity_hook(tmp_path, server):  # noqa: F811
    """With the page on and --plan off, the file holds the activity hook and nothing else;
    with the page off it is not written or passed at all, and --plan's own file goes
    through untouched (the behaviour the plan suite pins)."""
    fake = cfg_fake(tmp_path)  # noqa: F811  (a path, not the imported fixture)
    on = cfg_on(tmp_path)
    r = test_cli.run(tmp_path, ["--shallow", "-r", "coder", "-q", "hi"], server, fake,
                     extra=dict(on))
    assert r.returncode == 0, r.stderr
    argv, _ = test_cli.record(tmp_path)
    settings = test_cli.flag(argv, "--settings")
    assert settings and "qwen-watch" in settings             # the page's own folder, not
    # --plan's (which is off here, and has no file to hold at all)
    assert argv.count("--settings") == 1
    cfg = saved_settings(tmp_path)
    assert set(act_entries(cfg)) == {"PreToolUse", "PostToolUse", "PostToolUseFailure",
                                     "Stop"}
    assert list(cfg["hooks"]) == ["PreToolUse", "PostToolUse", "PostToolUseFailure",
                                  "Stop"]
    assert len(cfg["hooks"]["PreToolUse"]) == 1               # only the activity hook shares it
    for entries in cfg["hooks"].values():                    # no plan hook in sight
        for e in entries:
            for h in e["hooks"]:
                assert "advisor_hook.py" not in h["command"]
    # the page off: no activity file to fill, so no --settings flag at all
    r2 = test_cli.run(tmp_path, ["--shallow", "-r", "coder", "-q", "hi"], server, fake,
                      extra=dict(on, QWEN_WATCH="0"))
    assert r2.returncode == 0, r2.stderr
    argv2, _ = test_cli.record(tmp_path)
    assert test_cli.flag(argv2, "--settings") is None, argv2
    # the page off with --plan on: --plan keeps its own file, exactly as before
    r3 = test_cli.run(tmp_path, ["--shallow", "-r", "coder", "-q", "--plan", "opus", "hi"],
                      server, fake, extra=dict(on, QWEN_WATCH="0"))
    assert r3.returncode == 0, r3.stderr
    argv3, _ = test_cli.record(tmp_path)
    settings3 = test_cli.flag(argv3, "--settings")
    assert settings3 and "qwen-plan" in settings3 and "qwen-watch" not in settings3
    cfg3 = saved_settings(tmp_path)
    assert list(cfg3["hooks"]) == ["PreToolUse"]              # the plan hook alone
    assert cfg3["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"


def test_settings_file_registers_post_tool_use_failure(tmp_path, server):  # noqa: F811
    """A failed tool call fires PostToolUseFailure, not PostToolUse -- so that event is
    registered too, next to the other watch entries: matcher "*" and the very same hook
    command, written beside --plan's entry untouched. Without this registration the hook
    never runs for a failing call, its `pre` line waits for a `post` that never comes,
    and the page reads the tool as running forever."""
    fake = cfg_fake(tmp_path)  # noqa: F811  (a path, not the imported fixture)
    r = test_cli.run(tmp_path, ["--shallow", "-r", "coder", "-q", "--plan", "opus", "hi"],
                     server, fake, extra=cfg_on(tmp_path))
    assert r.returncode == 0, r.stderr
    cfg = saved_settings(tmp_path)
    act = act_entries(cfg)
    assert set(act) == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
    assert act["PostToolUseFailure"]["matcher"] == "*"        # every tool, like its twins
    [h] = act["PostToolUseFailure"]["hooks"]
    assert h["type"] == "command" and set(h) == {"type", "command"}
    assert h["command"] == act["PreToolUse"]["hooks"][0]["command"]   # one command, four events
    # the last argument, not the raw line: the command ENDS in the closing quote of the
    # hook path, so endswith could never see it (same parse the entry above pins)
    assert shlex.split(h["command"])[-1].replace("\\", "/").endswith("lib/watch_hook.py")
    # the plan hook's own entry sits in the same file, untouched
    plan = [e for e in cfg["hooks"]["PreToolUse"]
            if e.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit"]
    assert len(plan) == 1 and "advisor_hook.py" in plan[0]["hooks"][0]["command"]


# ------------------------------------- the settings merge refuses, never drops the hook

def merge_block():
    """The watch settings merge of qwen-agent.sh -- the whole
    `if [ -n "${QWEN_WATCH_ACTIVITY:-}" ] ... fi` block, heredoc included -- as one
    runnable bash snippet (the same extract-and-run style as the skill doc's watch
    recipe in test_skill_doc.py): the outer block opens at column 0 and the first
    column-0 `fi` closes it, every line inside it is indented or heredoc content."""
    lines = test_cli.AGENT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith('if [ -n "${QWEN_WATCH_ACTIVITY:-}" ]'))
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1]) + "\n"


def merge_script(block):
    """The python heredoc of that block, as source: what argv[1:5] maps to
    out/py/hook/plan and what it must never write when the plan base fails."""
    lines = block.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.endswith("<<'PY'"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "PY")
    return "\n".join(lines[start + 1:end]) + "\n"


def run_merge_block(watch_tmpd, plan_settings, tmpdir=None):
    """Run the extracted merge block with the environment of a registered single-shot
    session. Returns the process and the parsed WATCH_SETTINGS/RESOLVED lines --
    RESOLVED is `${WATCH_SETTINGS:-$PLAN_SETTINGS}`, the very expansion the argv build
    hands to --settings (qwen-agent.sh:3338). `watch_tmpd` "" leaves the block to its
    own fallback mktemp, and a `tmpdir` that cannot hold a folder makes THAT fail."""
    script = ('native_path() { printf "%s" "$1"; }\n'  # noqa: F811  (a string, not the fixture)
              "DRY_RUN=0\nINTERACTIVE=0\nCTX=\n"
              + merge_block()
              + 'printf "WATCH_SETTINGS=%s\\n" "${WATCH_SETTINGS:-}"\n'
              + 'printf "RESOLVED=%s\\n" "${WATCH_SETTINGS:-$PLAN_SETTINGS}"\n')
    env = dict(os.environ)  # noqa: F811  (a dict, not the imported fixture)
    env.pop("CTX", None)
    env.update({"QA_PY": sys.executable,
                "SKILL_DIR": str(test_cli.AGENT.parent),
                "PLAN_SETTINGS": plan_settings,
                "QWEN_WATCH_ACTIVITY": str(tmp_watch_activity_file(plan_settings)),
                "WATCH_TMPD": str(watch_tmpd)})
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    p = subprocess.run([test_cli.BASH, "-c", script], env=env, capture_output=True,
                       text=True, timeout=60)
    out = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if "=" in ln)
    return p, out


def tmp_watch_activity_file(plan_settings):
    return os.path.join(os.path.dirname(plan_settings), "merge-activity.jsonl")


def test_unreadable_plan_settings_file_keeps_the_plan_hook_kept(tmp_path):
    """THE PLAN HOOK IS NEVER DROPPED. When --plan is on but its settings file cannot
    be read, or is JSON that is not an object, the merge script exits non-zero WITHOUT
    writing the merged file; the block's `then` branch drops WATCH_SETTINGS; and the
    argv build's `${WATCH_SETTINGS:-$PLAN_SETTINGS}` hands claude the PLAN'S OWN file,
    so its PreToolUse hook still runs. Only the activity lines are lost -- the
    best-effort outcome. A readable plan object still merges beside it, untouched."""
    plan_entry = {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                  "hooks": [{"type": "command", "timeout": 660,
                             "command": "python advisor_hook.py --state /plan-dir"}]}
    # (1) the merge script alone: every plan-base failure exits non-zero, writing
    # NOTHING -- no half merged file to mislead anyone
    merge_py = tmp_path / "merge.py"
    merge_py.write_text(merge_script(merge_block()), encoding="utf-8")
    plan_dir = tmp_path / "plan-settings-dir"           # a directory cannot be read
    plan_dir.mkdir()                                    # as a settings file
    cases = {"missing": str(tmp_path / "no-such-settings.json"),
             "unreadable": str(plan_dir),
             "not-json": None, "list": None, "string": None, "null": None}
    for name, direct in cases.items():
        if direct is None:
            junk = tmp_path / ("plan-%s.json" % name)
            junk.write_text({"not-json": '{"hooks": ', "list": "[1, 2]",
                              "string": '"plan"', "null": "null"}[name],
                             encoding="utf-8")
            plan = str(junk)
        else:
            plan = direct
        out = tmp_path / ("merged-%s.json" % name)
        p = subprocess.run([sys.executable, str(merge_py), str(out), sys.executable,
                            str(test_cli.AGENT.parent / "lib" / "watch_hook.py"), plan],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode != 0, (name, p.stdout, p.stderr)
        assert not out.exists(), name                   # refused BEFORE writing
    # a readable object still merges: the plan's entries are the base, the four watch
    # entries land beside them, the plan's matcher, timeout and command intact
    good = tmp_path / "plan-good.json"
    good.write_text(json.dumps({"hooks": {"PreToolUse": [plan_entry]}, "keepme": 1}),
                    encoding="utf-8")
    out = tmp_path / "merged-good.json"
    p = subprocess.run([sys.executable, str(merge_py), str(out), sys.executable,
                        str(test_cli.AGENT.parent / "lib" / "watch_hook.py"), str(good)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (p.stdout, p.stderr)
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert cfg["keepme"] == 1                           # the plan's file was the base
    assert cfg["hooks"]["PreToolUse"][0] == plan_entry  # its hook entry, untouched
    assert set(cfg["hooks"]) == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
    assert any("watch_hook.py" in h["command"]
               for e in cfg["hooks"]["PreToolUse"][1:] for h in e["hooks"])
    assert "matcher" not in cfg["hooks"]["Stop"][0]

    # (2) the whole block, run as the launcher runs it: on the same failures the
    # `then` branch takes WATCH_TMPD away, WATCH_SETTINGS stays EMPTY, and --settings
    # resolves to the PLAN'S OWN file (the plan hook runs; only activity lines are lost)
    for name in ("unreadable", "junk"):
        wd = tmp_path / ("wd-" + name)
        wd.mkdir()
        if name == "unreadable":
            plan_f = tmp_path / "plan-dir-keep"        # PLAN_SETTINGS is a directory
            plan_f.mkdir()
            plan_f = str(plan_f)
        else:
            plan_f = tmp_path / "plan-junk.json"
            plan_f.write_text("[42]", encoding="utf-8")
            plan_f = str(plan_f)
        before = None if name == "unreadable" else open(plan_f).read()
        p, got = run_merge_block(wd, plan_f)
        assert p.returncode == 0, (p.stdout, p.stderr)  # best effort: no louder failure
        assert got.get("WATCH_SETTINGS") == "", (name, got, p.stderr)
        assert got.get("RESOLVED") == plan_f, (name, got)
        assert not (wd / "settings.json").exists(), name  # nothing merged was written
        if before is not None:
            assert open(plan_f).read() == before        # the plan's file untouched
    # the other way the block can end in its `else` WITHOUT a written merged file: no
    # folder at all (an inherited-empty WATCH_TMPD and a TMPDIR that cannot hold one, so
    # even the fallback mktemp fails). Then the settings stay away as well -- never the
    # stray "/settings.json" an unguarded else would name, but the plan's own file.
    p, got = run_merge_block("", str(good), tmpdir=str(tmp_path / "no-such-tmpdir"))
    assert p.returncode == 0, (p.stdout, p.stderr)      # best effort: still no louder failure
    assert got.get("WATCH_SETTINGS") == "", (got, p.stderr)
    assert got.get("RESOLVED") == str(good), got        # claude gets the plan's own file
    # and with a good plan file the block builds the shared file and hands THAT out
    wd = tmp_path / "wd-good"
    wd.mkdir()
    plan_f = tmp_path / "plan-block-good.json"
    plan_f.write_text(json.dumps({"hooks": {"PreToolUse": [plan_entry]}}),
                      encoding="utf-8")
    p, got = run_merge_block(wd, str(plan_f))
    assert p.returncode == 0, (p.stdout, p.stderr)
    # the block joins the folder and settings.json with /, Python joins them with \ on
    # Windows: compare paths, not spellings
    assert os.path.normpath(got.get("WATCH_SETTINGS", "")) \
        == os.path.normpath(str(wd / "settings.json")), (got, p.stderr)
    assert os.path.normpath(got.get("RESOLVED", "")) \
        == os.path.normpath(str(wd / "settings.json"))
    cfg = json.loads((wd / "settings.json").read_text(encoding="utf-8"))
    assert cfg["hooks"]["PreToolUse"][0] == plan_entry
    assert {e.get("matcher") for e in cfg["hooks"]["PreToolUse"][1:]} == {"*"}


def test_single_shot_exports_its_activity_file(tmp_path, server):  # noqa: F811
    """The single-shot session's hook writes beside its pointer: the launcher exports the
    path python's activity_path(id) handed back, so the agent -- and every hook Claude Code
    runs for it -- inherits the very file the page will read for that id. A session that
    registered nothing exports nothing, and a child launcher keeps what it was given."""
    fake = tmp_path / "fake-act-claude"  # noqa: F811  (a path, not the imported fixture)
    fake.write_text("""#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then
  echo "  --permission-mode <mode>  Permission mode to use for the session"
  echo "  --restricted              Restricted mode"
  exit 0
fi
env | grep '^QWEN_' | sort > "$FAKE_CHILD_ENV"
printf '%s\\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
""", encoding="utf-8", newline="\n")
    fake.chmod(0o755)

    def child_env(name):
        d = {}
        for ln in (tmp_path / name).read_text(encoding="utf-8").splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                d[k] = v
        return d

    on = {"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
          "FAKE_CHILD_ENV": test_cli.posix(tmp_path / "act-env.txt")}
    r = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake, extra=dict(on))
    assert r.returncode == 0, r.stderr
    ps = pointers()
    assert len(ps) == 1 and ps[0]["kind"] == "agent", ps
    env = child_env("act-env.txt")  # noqa: F811  (a dict, not the imported fixture)
    assert env["QWEN_WATCH_ACTIVITY"] == watch.activity_path(ps[0]["id"])   # the id's own file
    assert env["QWEN_WATCH_ACTIVITY"].endswith(".activity.jsonl")           # never a pointer
    assert os.path.dirname(env["QWEN_WATCH_ACTIVITY"]) == watch.watch_dir()
    # the window the model was sized into travels with it, for the context meter
    assert env.get("QWEN_WATCH_CTX_WINDOW") == "262144"          # what the fake server reports
    # the page off: nothing was registered, so nothing is named
    r2 = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake,
                      extra=dict(on, QWEN_WATCH="0",
                                 FAKE_CHILD_ENV=test_cli.posix(tmp_path / "off2.txt")))
    assert r2.returncode == 0, r2.stderr
    assert "QWEN_WATCH_ACTIVITY" not in child_env("off2.txt")
    # a child launcher does not register, and keeps the file the launcher above it gave it
    r3 = test_cli.run(tmp_path, ["-r", "auditor", "say hi"], server, fake,
                      extra=dict(on, QWEN_WATCH_CHILD="1",
                                 QWEN_WATCH_ACTIVITY="/somewhere/else/unit.jsonl",
                                 FAKE_CHILD_ENV=test_cli.posix(tmp_path / "child3.txt")))
    assert r3.returncode == 0, r3.stderr
    assert len(pointers()) == 1, pointers()                     # no second pointer
    assert child_env("child3.txt")["QWEN_WATCH_ACTIVITY"] == "/somewhere/else/unit.jsonl"


# ---------------------------------------------------------------- the units

def _unit_envs(fake_swarm):  # noqa: F811
    """The QWEN_ environment of every unit the fake swarm agent ran, one dict per spawn
    (a retry is a spawn of its own, so the same unit appears once per try)."""
    return [json.loads(ln) for ln in
            (fake_swarm / "env.jsonl").read_text(encoding="utf-8").splitlines()]


def test_a_swarm_unit_environment_names_its_own_activity_file(tmp_path, fake_swarm,  # noqa: F811
                                                              monkeypatch):
    """RUN/activity/<unit name>.jsonl, one file per unit: the unit's launcher inherits the
    switch and the folder from the run that registered, so the unit's session and its hooks
    append to the file the page will read for THAT unit. A retry -- the same unit spawned
    again -- is told the very same path, so the unit's whole history stays on one card. A
    swarm nobody registered adds nothing to a unit's environment and creates no folder at
    all: the page stays out of a run that does not use it."""
    s = swarm(tmp_path, seats=2)
    monkeypatch.setenv("QWEN_WATCH_CHILD", "1")                 # the runner registered the run
    res = s.run_phase([unit(tmp_path, "a"), unit(tmp_path, "b")], seats=2)
    assert all(r["ok"] for r in res), res
    envs = _unit_envs(fake_swarm)
    assert len(envs) == 2, envs
    assert sorted(e["QWEN_WATCH_ACTIVITY"] for e in envs) == sorted([
        os.path.join(str(s.activity_dir), "a.jsonl"),
        os.path.join(str(s.activity_dir), "b.jsonl")])
    for e in envs:                                              # inside the run, never beside it
        assert os.path.dirname(e["QWEN_WATCH_ACTIVITY"]) == os.path.join(str(s.run_dir),
                                                                         watch.ACTIVITY_DIR)
    assert s.activity_dir.is_dir()                              # made for the units that ran
    # a unit whose first try dies on a server error is told the SAME file on its retry: the
    # card keeps the whole unit, the two tries in one append-only file
    seen = tmp_path / "seen"
    (fake_swarm / "worker.py").write_text(
        "import pathlib\n"
        "def answer(p, r):\n"
        "    f = pathlib.Path(%r)\n"
        "    if not f.exists():\n"
        "        f.write_text('1'); return 4, ''\n"
        "    return 0, '```json\\n[2]\\n```'\n" % str(seen))
    res = s.run_phase([unit(tmp_path, "c")], seats=1)
    assert res[0]["ok"], res
    retried = _unit_envs(fake_swarm)[2:]
    assert len(retried) == 2, retried                            # the try and its retry
    assert {e["QWEN_WATCH_ACTIVITY"] for e in retried} \
        == {os.path.join(str(s.activity_dir), "c.jsonl")}
    # and a run the page never saw leaves the unit's environment, and the run folder, alone
    other = tmp_path / "run2"
    other.mkdir()
    monkeypatch.delenv("QWEN_WATCH_CHILD")
    s2 = sw.Swarm([sys.executable, str(FAKE)], other, seats=2, timeout=60, backoff=0)
    assert all(r["ok"] for r in s2.run_phase([unit(tmp_path, "d")], seats=1))
    assert "QWEN_WATCH_ACTIVITY" not in _unit_envs(fake_swarm)[-1]
    assert not (other / watch.ACTIVITY_DIR).exists()


# ------------------------------------------------------------- the coder loop

def _loop(env, monkeypatch, steps, *extra):  # noqa: F811
    """One until-done run of the fake agent, with the page on: (exit code, pointer, run
    folder). QWEN_TIMEOUT is the round's own budget, which round_start reports."""
    repo, task, tmp = env
    script(tmp, steps)
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)      # a round must not re-register,
    monkeypatch.delenv("QWEN_WATCH_PARENT", raising=False)     # but this process may register
    monkeypatch.setenv("QWEN_TIMEOUT", "120")
    rc = sup(repo, task, "--max-rounds", "3", *extra)
    ps = [p for p in pointers() if p["kind"] == "until-done"]
    assert len(ps) == 1, ps
    return rc, ps[0], pathlib.Path(ps[0]["run_path"])


def test_until_done_loop_writes_its_round_events(env, monkeypatch):  # noqa: F811
    """The loop announces itself into RUN/events.jsonl through the engine's own writer: a
    round_start before the call (which round, of how many, on what budget, and whether it
    is the review round), a checks_start immediately before the harness is asked (which
    round, and how many items the checklist holds), a checks line right after the harness
    ran them (the counts, and every item's text and state), and a round_end when the round
    is over -- so the page can show a coder loop with nothing but these lines. The names
    and the counts are the contract; the numbers come from the checks, not from the
    agent's say-so. And every round's call is spawned with the RUN's activity file in its
    environment, so the tools the coder uses land there."""
    seen = []
    real = supervisor._run_agent

    def spy(argv, extra_env=None):
        seen.append(dict(extra_env or {}))
        return real(argv, extra_env)
    monkeypatch.setattr(supervisor, "_run_agent", spy)
    rc, p, run = _loop(env, monkeypatch,
                       [{"result": "tried"},
                        {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert rc == 0, rc
    assert len(seen) == 2, seen                                   # one round's call each
    for e in seen:
        assert e["QWEN_WATCH_ACTIVITY"] == str(run / watch.ACTIVITY_FILE)
    evs = events_of(run)
    assert [e["kind"] for e in evs] == ["round_start", "checks_start", "checks",
                                        "round_end", "round_start", "checks_start",
                                        "checks", "round_end"], evs
    r1, k1, c1, e1, r2, k2, c2, e2 = evs
    assert set(r1) == {"kind", "t", "round", "max_rounds", "timeout", "review"}
    assert (r1["round"], r1["max_rounds"], r1["timeout"]) == (1, 3, 120)
    assert (r2["round"], r2["timeout"]) == (2, 120)
    # no --review-round here: every round says so with a plain False
    assert r1["review"] is False and r2["review"] is False
    # the between-rounds lines carry a count and fixed words, never check output
    assert set(k1) == {"kind", "t", "round", "total"}
    assert (k1["round"], k1["total"]) == (1, 1)          # the one-item checklist
    assert (k2["round"], k2["total"]) == (2, 1)
    assert set(c1) == {"kind", "t", "round", "passed", "failed", "unverified", "total",
                       "items"}
    assert (c1["round"], c1["passed"], c1["failed"], c1["unverified"], c1["total"]) \
        == (1, 0, 1, 0, 1)
    assert c1["items"] == [{"text": "value is good", "status": "FAIL"}]
    assert (c2["round"], c2["passed"], c2["failed"], c2["total"]) == (2, 1, 0, 1)
    assert c2["items"] == [{"text": "value is good", "status": "PASS"}]
    assert set(e1) == {"kind", "t", "round", "rc"} and e1["round"] == 1 and e1["rc"] == 0
    assert e2["round"] == 2 and e2["rc"] == 0
    # every line carries the engine's own stamp, and no line names the agent's words
    assert all(isinstance(e["t"], float) for e in evs)


def test_until_done_run_detail_and_api_expose_the_loop(env, monkeypatch):  # noqa: F811
    """What the page builds a coder loop from: run_detail's `round` (n, timeout, started),
    `checks` (the counts, the items and the round they belong to), `activity` -- folded
    from RUN/activity.jsonl, the file each round's launcher was told to fill -- and
    `recent`, the events themselves; /api/runs carries the two the overview card needs,
    round.n and the checks' passed/total. A round-*.json is never opened: its body, the
    agent's own reply, appears nowhere in what the page is handed."""
    rc, p, run = _loop(env, monkeypatch,
                       [{"result": "the coder's own words"},
                        {"result": "its words again", "write": {"value.txt": "good\n"}}])
    assert rc == 0, rc
    # what a round-*.json holds, in no part of what the page is given: the round's file is
    # replaced with a sentinel, so "never opened" is a claim a test can actually check
    secret = "SENTINEL-ROUND-FILE-BODY"
    (run / "round-1.json").write_text(json.dumps({"result": secret, "rc": 0,
                                                  "session": "s1", "tokens": 1}),
                                      encoding="utf-8")
    (run / "round-1.prompt.md").write_text("the whole first prompt: %s\n" % secret,
                                           encoding="utf-8")
    (run / watch.ACTIVITY_FILE).write_text(
        json.dumps({"t": time.time() - 3, "ev": "pre", "tool": "Bash",
                    "target": "pytest -q tests/test_value.py", "ok": True,
                    "ctx": 4200, "win": 262144}) + "\n"
        + json.dumps({"t": time.time() - 2, "ev": "post", "tool": "Bash",
                      "target": "pytest -q tests/test_value.py", "ok": True}) + "\n"
        + json.dumps({"t": time.time() - 1, "ev": "pre", "tool": "Edit",
                      "target": "value.txt", "ok": True}) + "\n", encoding="utf-8")
    now = time.time()
    st = watch.run_detail(p["id"], now)
    assert st["round"] is not None and st["round"]["n"] == 2
    assert st["round"]["timeout"] == 120 and st["round"]["started"] > 0
    assert st["checks"]["passed"] == 1 and st["checks"]["total"] == 1
    assert st["checks"]["round"] == 2 and st["checks"]["items"][0]["status"] == "PASS"
    assert st["checks"]["items"][0]["text"] == "value is good"
    a = st["activity"]
    assert a["state"] == "tool" and a["cls"] == "edit" and a["target"] == "value.txt"
    assert a["ctx"] == 4200 and a["win"] == 262144                  # the last known reading
    assert [c["tool"] for c in a["calls"]] == ["Bash"] and a["calls"][0]["cls"] == "python"
    assert [e["kind"] for e in st["recent"]][-1] == "round_end"
    assert secret not in json.dumps(st)                             # never a round-*.json
    assert "the coder's own words" not in json.dumps(st)            # nor the agent's reply
    runs = {r["id"]: r for r in watch.list_runs(now)}
    assert runs[p["id"]]["round"] == {"n": 2, "timeout": 120,
                                      "started": st["round"]["started"]}
    # the overview's Active agents row measures the loop's round against its budget
    assert runs[p["id"]]["checks"] == {"passed": 1, "total": 1}
    assert runs[p["id"]]["activity"]["state"] == "tool"
    assert set(runs[p["id"]]["activity"]) == set(watch.ACTIVITY_SMALL)
    assert secret not in json.dumps(runs[p["id"]])


def test_until_done_page_data_survives_a_loop_that_wrote_nothing(tmp_path, monkeypatch):
    """A coder loop the page knows about but whose folder holds nothing -- no events, no
    activity, no report, a run_path that is not even a directory: the fold answers None for
    each of the three and the run still opens. Nothing here may raise: a watch failure must
    never change what the page shows, let alone what a run costs."""
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)
    monkeypatch.delenv("QWEN_WATCH_PARENT", raising=False)
    rid = watch.register("until-done", "a loop that wrote nothing",
                         run_path=str(tmp_path / "no-such-run"), start_server=False)
    st = watch.run_detail(rid, time.time())
    assert st["round"] is None and st["checks"] is None and st["activity"] is None
    assert st["recent"] == [] and st["rounds"] == 0 and st["report_stop"] is None
    small = {r["id"]: r for r in watch.list_runs(time.time())}[rid]
    assert small["round"] is None and small["checks"] is None
    assert small["activity"] is None


def test_until_done_round_events_close_a_round_the_loop_lost(env, monkeypatch):  # noqa: F811
    """A round that never reaches its checks still closes: the checks_start line said the
    runner was asked, and then it died -- the loop emits its round_end and no checks line
    (there is nothing to say), so the page is left with a closed round and a run that
    ended, not a spinner stuck mid-round."""
    def dying(items, repo, *, test_cmd, timeout):
        raise RuntimeError("the check runner died")
    monkeypatch.setattr(supervisor.checks, "run_checks", dying)
    rc, p, run = _loop(env, monkeypatch, [{"result": "x"}])
    assert rc == supervisor.EXIT_HARNESS, rc                        # 8: the harness failed
    evs = events_of(run)
    assert [e["kind"] for e in evs] == ["round_start", "checks_start", "round_end"], evs
    assert evs[0]["round"] == 1 and evs[1]["round"] == 1 and evs[1]["total"] == 1
    assert evs[2]["round"] == 1 and evs[2]["rc"] == 0
    st = watch.run_detail(p["id"], time.time())
    assert st["round"]["n"] == 1 and st["checks"] is None           # the round closed, no checks
    assert st["step"] is None                        # the run ended: no "right now" to show


_FAKE_AGENT = os.path.join(os.path.dirname(__file__), "fake_agent.py")


def _audited_run(env, monkeypatch, steps, expect_rc):  # noqa: F811
    """One until-done run WITH the deviation audit on (the `sup` helper switches it off),
    the fake agent scripted, the page on: returns (pointer, events). Like every supervisor
    test, one run per test: the fake agent walks its script by a counter file of its own."""
    repo, task, tmp = env
    monkeypatch.delenv("QWEN_WATCH_CHILD", raising=False)      # a round must not re-register,
    monkeypatch.delenv("QWEN_WATCH_PARENT", raising=False)     # but this process may register
    monkeypatch.setenv("QWEN_TIMEOUT", "120")
    script(tmp, steps)
    assert supervisor.main(["--task", str(task), "--repo", str(repo),
                            "--agent", sys.executable, "--agent", _FAKE_AGENT,
                            "--max-rounds", "3"]) == expect_rc
    ps = [p for p in pointers() if p["kind"] == "until-done"]
    assert len(ps) == 1, ps
    return ps[0], events_of(pathlib.Path(ps[0]["run_path"]))


def test_until_done_writes_checks_start_before_the_checks(env, monkeypatch):  # noqa: F811
    """`checks_start` is in the file by the moment checks.run_checks is called -- not
    only after it returns: the page must already say Testing while a check still runs.
    It carries the round and the checklist's size, and nothing a check could print."""
    seen = {}
    real = supervisor.checks.run_checks

    def wrapping(items, repo, *, test_cmd, timeout):
        # what the events file holds at the very instant the runner is asked
        run = pathlib.Path([p for p in pointers() if p["kind"] == "until-done"][0]["run_path"])
        seen["kinds"] = [e["kind"] for e in events_of(run)]
        return real(items, repo, test_cmd=test_cmd, timeout=timeout)
    monkeypatch.setattr(supervisor.checks, "run_checks", wrapping)
    rc, p, run = _loop(env, monkeypatch,
                       [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert rc == 0, rc
    assert seen["kinds"] == ["round_start", "checks_start"], seen["kinds"]
    evs = events_of(run)
    ks = [e for e in evs if e["kind"] == "checks_start"]
    assert len(ks) == 1 and set(ks[0]) == {"kind", "t", "round", "total"}
    assert (ks[0]["round"], ks[0]["total"]) == (1, 1)              # the one-item checklist
    assert evs.index(ks[0]) == 1                          # right after round_start...
    assert evs[2]["kind"] == "checks"                     # ...and right before the checks


def test_until_done_writes_audit_events_around_the_deviation_audit(env, monkeypatch):  # noqa: F811
    """The deviation audit announces itself too: audit_start immediately before
    deviation_audit is called and audit_end right after it returns, with `result` one
    fixed word -- "clean" when nothing went unlogged. The two lines carry the round and
    that word only: the audit's reply stays in its own file, never on the line."""
    p, evs = _audited_run(env, monkeypatch,
                          [{"result": "fixed", "write": {"value.txt": "good\n"}},
                           {"result": "NO CONTRADICTIONS"}], 0)
    assert [(e["kind"], e.get("result")) for e in evs] == [
        ("round_start", None), ("checks_start", None), ("checks", None),
        ("round_end", None), ("audit_start", None), ("audit_end", "clean")], evs
    a0, a1 = evs[4], evs[5]
    assert set(a0) == {"kind", "t", "round"} and a0["round"] == 1
    assert set(a1) == {"kind", "t", "round", "result"} and a1["round"] == 1
    assert "NO CONTRADICTIONS" not in json.dumps(evs)     # the reply never rides the line
    st = watch.run_detail(p["id"], time.time())
    assert st["step"] is None                             # ended: no step, no spinner


def test_until_done_audit_end_says_deviations_when_something_went_unlogged(env, monkeypatch):  # noqa: F811
    """audit_end calls found contradictions "deviations" -- and the next round's clean
    audit says "clean" right beside it: two audits, two honest lines, each with its own
    audit_start before it. The run itself behaves exactly as before the lines existed."""
    _p, evs = _audited_run(env, monkeypatch, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},                        # round 1
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: NONE\n"},    # audit
        {"result": "## DEVIATION\nSPEC: s\nDID: d\nWHY: w\nEVIDENCE: value.txt:1\n"},  # round 2
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: D1\n"}],     # audit
        0)
    assert [(e["kind"], e.get("result")) for e in evs if e["kind"] == "audit_end"] \
        == [("audit_end", "deviations"), ("audit_end", "clean")], evs
    assert [e["kind"] for e in evs].count("audit_start") == 2   # every end had its start


def test_until_done_audit_end_says_unusable_when_the_reply_never_parses(env, monkeypatch):  # noqa: F811
    """A reply that parses as nothing twice over is "unusable" -- written on the path
    that breaks the run: the loop emits the line and THEN hands the diff to a human with
    its own EXIT_PARTIAL. The line rides along; it does not steer the exit."""
    _p, evs = _audited_run(env, monkeypatch, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},
        {"result": "looks fine"}, {"result": "still not an audit"}], supervisor.EXIT_PARTIAL)
    assert [(e["kind"], e.get("result")) for e in evs][-1] == ("audit_end", "unusable")
    assert [e["kind"] for e in evs][-2] == "audit_start"


def test_until_done_audit_end_says_error_when_the_audit_call_dies(env, monkeypatch):  # noqa: F811
    """A dead or refused audit call is "error" -- not "unusable", even though the reply
    is as unreadable as one -- and the line is there before the run breaks with its own
    server-error exit, so the page shows Reviewing that ended, not one that hangs."""
    _p, evs = _audited_run(env, monkeypatch, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},
        {"rc": 4}], supervisor.EXIT_APIERR)
    assert [(e["kind"], e.get("result")) for e in evs][-1] == ("audit_end", "error")
    assert [e["kind"] for e in evs][-2] == "audit_start"        # written before the break


def test_until_done_review_round_marked_on_its_round_start(env, monkeypatch):  # noqa: F811
    """--review-round asks one extra round after the checks pass; that round's
    round_start says `review: true` and every other round's says false -- the rounds
    AFTER the review too, when it broke a check and the loop had to keep going."""
    rc, p, run = _loop(env, monkeypatch,
                       [{"result": "fixed", "write": {"value.txt": "good\n"}},
                        {"result": "broke it", "write": {"value.txt": "bad\n"}},
                        {"result": "fixed again", "write": {"value.txt": "good\n"}}],
                       "--review-round")
    assert rc == 0, rc
    starts = [e for e in events_of(run) if e["kind"] == "round_start"]
    assert [(e["round"], e["review"]) for e in starts] == [(1, False), (2, True),
                                                           (3, False)], starts


# ------------------------------------------------------------- the phase outline

def test_a_manifest_outline_goes_out_as_phase_plan_after_run_start(tmp_path, fake_swarm):  # noqa: F811
    """A workflow.json "phases" outline reaches the page as ONE phase_plan line, right
    after run_start and before the first phase_start, every n resolved: an int as given,
    a knob name as that knob's value this run, an absent n as null. The fold takes the
    outline entry its phase_start names, and the rest are the page's placeholders."""
    wf = echo(tmp_path, manifest={"phases": [{"label": "phase 1", "n": "items"},
                                             {"label": "wrap up", "n": 1},
                                             {"label": "tidy"}]})
    out = tmp_path / "run"
    assert swarm_main(wf, "say hi", "--out", str(out)) == 0
    evs = events_of(out)
    kinds = [e["kind"] for e in evs]
    assert kinds[:3] == ["run_start", "phase_plan", "phase_start"]
    assert evs[1]["phases"] == [{"label": "phase 1", "n": 3}, {"label": "wrap up", "n": 1},
                                {"label": "tidy", "n": None}]
    fold = watch.fold_events(lines_of(out))
    assert watch.planned_phases(fold) == [{"label": "wrap up", "n": 1},
                                          {"label": "tidy", "n": None}]


def test_no_outline_writes_no_phase_plan(tmp_path, fake_swarm):  # noqa: F811
    """A manifest without "phases" -- every workflow written before it existed -- adds
    no line: the event stream is the one it always was."""
    out = tmp_path / "run"
    assert swarm_main(echo(tmp_path), "say hi", "--out", str(out)) == 0
    assert "phase_plan" not in [e["kind"] for e in events_of(out)]
    assert watch.planned_phases(watch.fold_events(lines_of(out))) == []


def test_phase_plan_is_an_engine_kind():
    """A workflow cannot forge an outline through wf.event: phase_plan is the engine's."""
    assert "phase_plan" in events.ENGINE_KINDS
    assert "phase_plan   phases: [{label, n}]" in (events.__doc__ or "")


@pytest.mark.parametrize("phases, ok", [
    ([], True),
    ([{"label": "a"}, {"label": "b", "n": 0}, {"label": "c", "n": "items"}], True),
    ("a, b", False),                                   # not a list
    ([{"label": ""}], False),                          # empty label
    ([{"label": "x" * 61}], False),                    # longer than one line can hold
    ([{"label": "a", "n": -1}], False),
    ([{"label": "a", "n": True}], False),
    ([{"label": "a", "n": "nope"}], False),            # no such knob
    ([{"label": "a", "seats": 2}], False),             # unknown key
    ([{"label": "p%d" % i} for i in range(33)], False),  # over MAX_PHASES
])
def test_manifest_validates_the_outline(tmp_path, phases, ok):
    from lib.swarm_engine import manifest
    from swarm_fixtures import make_workflow
    folder = make_workflow(tmp_path / "wfs", manifest={"phases": phases})
    if ok:
        assert manifest.load(folder).phases == phases
    else:
        with pytest.raises(manifest.ManifestError, match="phases"):
            manifest.load(folder)


def test_a_str_knob_cannot_count_agents(tmp_path):
    from lib.swarm_engine import manifest
    from swarm_fixtures import make_workflow
    folder = make_workflow(tmp_path / "wfs", manifest={
        "knobs": {"items": "int", "word": "str"},
        "presets": {"quick": {"items": 3, "word": "", "budget": 100, "retries": 0, "rounds": 1}},
        "phases": [{"label": "a", "n": "word"}]})
    with pytest.raises(manifest.ManifestError, match="int knob"):
        manifest.load(folder)


def test_builtin_outlines_name_labels_their_workflows_pass():
    """Every built-in workflow's outline entry is a label its workflow.py really passes
    (with round 1 filled in for a round label): an outline label nothing passes would sit
    as a placeholder until the run ended. Each outline also loads, n and all."""
    from lib.swarm_engine import manifest
    root = pathlib.Path(watch.__file__).resolve().parent / "workflows"
    for folder in sorted(p for p in root.iterdir() if (p / "workflow.json").is_file()):
        m = manifest.load(folder)
        assert m.phases, folder.name
        src = (folder / "workflow.py").read_text(encoding="utf-8")
        for ph in m.phases:
            label = ph["label"]
            plain = 'label="%s"' % label
            rounded = 'label="%s"' % label.replace("round 1:", "round %d:")
            assert plain in src or rounded in src, (folder.name, label)
