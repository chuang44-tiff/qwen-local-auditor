# ruff: noqa: F811
"""Stage 1.5: the until-done supervisor writes an ask for a listening main
session when a round fails (spec 10.3): the ask lands in RUN/asks/new/, the loop
goes on without waiting, --rescue records its skip instead of calling Claude, and
every way out stamps RUN/asks/.ended (spec 4.1, 10.1).

Driven on test_supervisor's harness (env/script/sup, the fake agent, and the
fake's round-1 "run_write" of asks/lease.json that plants the live lease), and on
test_supervisor_rescue's fake_claude fixture where --rescue is involved: the
rescue is exercised on its REAL path, so the stdin.N count is the no-Claude-call
proof (this file's tests that ask assert it stayed zero; today's-rescue tests
assert it is one, which also drives the refactored _secret_exclude pathspec code
with a committed secret file in the diff). The page comes on with QWEN_WATCH=1
where an ask is due (asks_on needs a watch id), and XDG_CACHE_HOME is pinned to
tmp_path/cache for EVERY test here, so the loop's pointer never reaches the real
watch registry. The fake-claude script and the SIGINT case are POSIX and skip off
POSIX."""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import sigint_default
from lib import asks, supervisor
from test_supervisor import FAKE, PY, calls, env, git, script, sup  # noqa: E402,F401
from test_supervisor_rescue import fake_claude  # noqa: E402,F401

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX fake claude script and POSIX signals")


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path, monkeypatch):
    """The file's rules: XDG_CACHE_HOME at tmp_path/cache (the loop's pointer
    stays out of the real watch registry), and a developer's QWEN_STREAM or
    QWEN_ASK_LEASE never decides what the test sees -- messaging is on unless a
    test says otherwise."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)


@pytest.fixture
def page(monkeypatch):
    """QWEN_WATCH=1: the loop registers and gets a watch id (asks_on's other
    half). register() stamps the ownership trio into this process's environment;
    it goes back off here so the next loop starts from a clean registration."""
    monkeypatch.setenv("QWEN_WATCH", "1")
    yield
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID"):
        os.environ.pop(name, None)


# ------------------------------------------------------------------- helpers

def run_dir_of(tmp):
    return next((tmp / "state").rglob("report.md")).parent


def new_asks(run):
    d = os.path.join(str(run), "asks", "new")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def claimed_asks(run):
    d = os.path.join(str(run), "asks", "claimed")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def ask_of(run, n):
    with open(os.path.join(str(run), "asks", "new", "%d.md" % n),
              encoding="utf-8") as fh:
        return fh.read()


def log_events(run):
    path = os.path.join(str(run), "asks", asks.LOG_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln)["ev"] for ln in fh if ln.strip()]
    except OSError:
        return []


def report_of(run):
    with open(os.path.join(str(run), "report.md"), encoding="utf-8") as fh:
        return fh.read()


def rescue_records(run):
    path = os.path.join(str(run), "claude-calls.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []
    # _report's own rule: the run's rescue records are the ones with "called"; the
    # per-call log lines _keep_call_logs merges in at the end have no "called"
    # (they carry model/n instead) and are not the attempt records under test.
    return [r for r in rows if r.get("call") == "rescue" and "called" in r]


def loop_events(run):
    path = os.path.join(str(run), "events.jsonl")
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def stdin_files(fake_dir):
    return sorted(f for f in os.listdir(str(fake_dir)) if f.startswith("stdin."))


def live_lease():
    now = round(time.time(), 3)
    return json.dumps({"pid": 1, "until": now + 600, "t": now})


def expired_lease():
    now = round(time.time(), 3)
    return json.dumps({"pid": 1, "until": now - 60, "t": now - 660})


def pointer_id(tmp):
    """The watch id of the registered run (the pointer's file name)."""
    d = tmp / "cache" / "qwen-agent" / "watch"
    ids = sorted(p.stem for p in d.glob("*.json"))
    assert len(ids) == 1, ids
    return ids[0]


def fresh(tmp, repo, value="bad\n"):
    """Put the repo back to a clean tree at a NEW start commit and rewind the
    fake agent's call counter, so a later sup() in the same test is a real
    second run (a commit whose content equals HEAD is --allow-empty)."""
    (repo / "value.txt").write_text(value)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "reset", "--allow-empty")
    counter = str(tmp / "record.jsonl") + ".n"
    if os.path.exists(counter):
        os.remove(counter)
    # Run folders are named to the second: move the earlier runs aside so a run
    # that starts in the same second gets its own folder, and run_dir_of() finds
    # only the new run's report.
    state = tmp / "state"
    if state.is_dir():
        for rep in sorted(state.rglob("report.md")):
            moved = rep.parent.with_name("prev-%d-%s" % (time.time_ns(), rep.parent.name))
            rep.parent.rename(moved)
            (moved / "report.md").rename(moved / "report.prev.md")


# ------------------------------------------------------------------- the ask

@posix_only
def test_live_lease_writes_ask_and_skips_rescue(env, page, fake_claude, tmp_path):
    # Two failing rounds, one listening main session (a live lease planted by the
    # fake's round-1 run_write): TWO asks queue, each failing round records the
    # rescue skip instead of calling Claude, and no round waits for an answer.
    repo, task, tmp = env
    script(tmp, [{"result": "r1", "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "r2", "write": {"value.txt": "meh\n"}},
                 {"result": "r3", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--rescue", "opus") == 0
    assert stdin_files(fake_claude) == []          # Claude was NEVER called, on
    assert not (fake_claude / "stdin.1").exists()  # the real rescue path's own terms
    run = run_dir_of(tmp)
    assert new_asks(run) == ["1.md", "2.md"]       # both asks queued
    watch = pointer_id(tmp)
    text = ask_of(run, 1)
    assert text.startswith("# Ask 1 for ")
    assert "(run %s)" % watch in text
    assert "round: 1 of 8" in text
    assert "changed files:" in text
    assert "## Failing checks" in text and "FAIL item 1" in text
    assert re.search(r"next round: started at \d{2}:\d{2}:\d{2}", text)
    assert "qwen-agent --send %s" % watch in text  # the answer block, named
    assert "qwen-agent --amend %s" % watch in text
    assert ask_of(run, 2).startswith("# Ask 2 for ") and "round: 2 of 8" in ask_of(run, 2)
    # the two rescue skips (spec 10.3): the records and the notes both say who
    # they stood down for, and each record carries the reply of the round after it
    recs = rescue_records(run)
    assert len(recs) == 2
    assert [r["round"] for r in recs] == [1, 2]
    for r in recs:
        assert r["called"] is False and r["advice"] is None
        assert r["skipped_reason"] == "main session listening"
    assert recs[0]["reply"] == "r2" and recs[1]["reply"] == "r3"
    report = report_of(run)
    assert "rescue after round 1 skipped: main session listening" in report
    assert "rescue after round 2 skipped: main session listening" in report
    # the report row, shown even though neither --advisor nor --rescue spent a call
    assert "## Claude calls" in report
    assert "ask-main: written 2, claimed 0, amends applied 0, rejected 0" in report
    assert log_events(run) == ["written", "written"]


def test_ask_written_without_waiting(env, page, tmp_path):
    # Nobody answers the ask -- and the run does not care: it writes the ask and
    # goes straight on. The proof is the loop's OWN events: round 2's round_start
    # follows round 1's round_end with the ask write (between them) costing no
    # more time than the loop's own bookkeeping -- a --next-ask-style wait parked
    # there would show up as the gap.
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert len(calls(tmp)) == 2                    # round 2 ran regardless
    run = run_dir_of(tmp)
    assert new_asks(run) == ["1.md"]               # still queued, unanswered
    assert claimed_asks(run) == []
    ev = loop_events(run)
    end1 = [e for e in ev if e.get("kind") == "round_end" and e.get("round") == 1]
    start2 = [e for e in ev if e.get("kind") == "round_start" and e.get("round") == 2]
    assert end1 and start2
    assert 0 <= start2[0]["t"] - end1[0]["t"] < 10.0   # the ask cost the loop
    #                                                      nothing but the write
    c2 = calls(tmp)[1]
    prompt = open(c2[c2.index("-f") + 1], encoding="utf-8").read()
    assert "Not done" in prompt                    # the coder moved on to it


@posix_only
def test_watch_off_writes_no_ask(env, fake_claude, tmp_path, monkeypatch):
    # A live lease is not enough: with no page there is no watch id to address,
    # so asks stay off -- and watch off must NOT silence the rescue either: the
    # real fake_claude gets its call.
    monkeypatch.setenv("QWEN_WATCH", "0")
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--rescue", "opus") == 0
    assert len(stdin_files(fake_claude)) == 1      # today's rescue still ran
    run = run_dir_of(tmp)
    assert new_asks(run) == []
    assert log_events(run) == []
    assert "ask-main:" not in report_of(run)


@posix_only
def test_no_lease_is_todays_rescue(env, page, fake_claude, tmp_path):
    # No lease, no listener: the rescue path runs exactly as it did before asks
    # existed -- the REAL advisor_mcp call goes out (stdin.1), the refactored
    # secret-pathspec code keeps a committed .env out of the brief, and no ask
    # appears anywhere.
    repo, task, tmp = env
    (repo / ".env").write_text("API=A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "carry an env file")
    script(tmp, [{"result": "tried", "write": {".env": "API=B\n",
                                                "value.txt": "meh\n"}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--rescue", "opus") == 0
    # Claude was asked, on the real advisor_mcp path, exactly once -- the stdin.N
    # count is the no-mock proof.
    assert stdin_files(fake_claude) == ["stdin.1"]
    sent = (fake_claude / "stdin.1").read_text(encoding="utf-8")
    assert supervisor.RESCUE_Q in sent
    assert "TEST ALL FAILED" in sent and "value.txt" in sent
    assert ".env" not in sent                       # the refactored pathspec code
    assert "API=B" not in sent                      # held the secret out of the brief
    run = run_dir_of(tmp)
    recs = rescue_records(run)
    assert len(recs) == 1 and recs[0]["round"] == 1
    assert recs[0]["called"] is True and recs[0]["skipped_reason"] is None
    assert recs[0]["reply"] == "ok"                 # round 2 answered the record
    assert "The value must be the word good" in (recs[0]["advice"] or "")
    report = report_of(run)
    assert "rescue calls: 1" in report
    assert "### Rescue after failing rounds" in report
    c2 = calls(tmp)[1]
    prompt = open(c2[c2.index("-f") + 1], encoding="utf-8").read()
    assert "Advice from a stronger model on these failures" in prompt
    assert new_asks(run) == [] and log_events(run) == []   # ...and no ask at all


@posix_only
def test_expired_lease_writes_no_ask(env, page, fake_claude, tmp_path):
    # "until" in the past = nobody listening: the stamp does not silence the
    # rescue (the real call goes out) and buys no ask.
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "run_write": {"asks/lease.json": expired_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--rescue", "opus") == 0
    assert len(stdin_files(fake_claude)) == 1
    run = run_dir_of(tmp)
    assert new_asks(run) == [] and log_events(run) == []


def test_last_round_failure_writes_no_ask(env, page, tmp_path):
    # --max-rounds 1: EVERY failing round is the last one, and ask_ok gates on
    # rounds < max_rounds -- so the layout is open but no ask is ever written.
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "write": {"value.txt": "meh\n"},
                  "run_write": {"asks/lease.json": live_lease()}}])
    assert sup(repo, task, "--max-rounds", "1") == 11         # round limit
    run = run_dir_of(tmp)
    assert os.path.isdir(os.path.join(str(run), "asks", "new"))   # layout opened
    assert new_asks(run) == []                                # but asked nothing
    assert log_events(run) == []


def test_ask_has_no_model_output(env, page, tmp_path):
    # The ask carries the HARNESS's failing checks -- including a check's own
    # printed output, which MUST reach the human -- and never what the coder
    # said: the round's result text and its stderr stay out of the ask.
    repo, task, tmp = env
    # a failing check that PRINTS: its output is harness evidence and must reach
    # the ask (it fails while value.txt is bad, passes once the fix lands)
    (repo / "shout.py").write_text(
        "import sys\n"
        "print('CHECK-MARKER-QQZ')\n"
        "sys.exit(0 if open('value.txt').read().strip() == 'good' else 1)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add a printing check")
    task = tmp / "task2.md"
    task.write_text("# Goal\nMake the value good and shout.\n\n"
                    "- [ ] value is good -- check: test ALL\n"
                    "- [ ] the marker prints -- check: cmd %s shout.py\n" % PY)
    script(tmp, [{"result": "ZZTOKENZZ the model's own words",
                  "stderr": "ZZERRZZ the model's own stderr",
                  "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    text = ask_of(run_dir_of(tmp), 1)
    assert "CHECK-MARKER-QQZ" in text               # the check's output belongs
    assert "FAIL item 1" in text and "FAIL item 2" in text
    assert "ZZTOKENZZ" not in text                  # the coder's words do not
    assert "the model's own words" not in text
    assert "ZZERRZZ" not in text


def test_ask_diffstat_excludes_secrets(env, page, tmp_path):
    # The ask's "changed files:" is a git --stat under the rescue's secret rule:
    # a changed .env is named by NO line of it, honest files still show.
    repo, task, tmp = env
    (repo / ".env").write_text("API=A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "carry an env file")
    start = git(repo, "rev-parse", "HEAD").strip()
    script(tmp, [{"result": "tried",
                  "write": {".env": "API=A\nTOKEN=zzz-secret-value\n",
                            "value.txt": "meh\n"},
                  "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    text = ask_of(run_dir_of(tmp), 1)
    assert "changed files:" in text
    assert "value.txt" in text
    assert ".env" not in text
    assert "TOKEN=zzz-secret-value" not in text
    # the same rule straight from the helper
    stat = supervisor._diffstat(str(repo), start)
    assert "value.txt" in stat and ".env" not in stat


def test_stream_off_no_asks_dirs(env, page, tmp_path, monkeypatch):
    # QWEN_STREAM=0 is the no-messaging baseline (P5): _asks_init opens nothing,
    # so a failing round with a page on still makes no ask dirs -- and no marker.
    monkeypatch.setenv("QWEN_STREAM", "0")
    repo, task, tmp = env
    script(tmp, [{"result": "tried"},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run = run_dir_of(tmp)
    assert not (run / "asks").exists()
    assert not (run / "amend").exists()
    assert not (run / ".ended").exists()
    assert not (run / "inbox").exists()                  # the baseline stays baseline


# ------------------------------------------------------------------- the end

def test_ended_written_on_every_exit(env, page, tmp_path, monkeypatch):
    # Five ways out, five markers -- and the marker rides AFTER the report
    # (10.1): a listener that exits 11 must be able to read a finished report.md,
    # so asks/.ended's stamp never precedes report.md's.
    repo, task, tmp = env
    # (a) exit 0: done
    script(tmp, [{"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run1 = run_dir_of(tmp)
    assert (run1 / "asks" / ".ended").is_file()
    assert not (run1 / ".ended").exists()                # not the old location
    assert (run1 / "asks" / ".ended").stat().st_mtime >= \
        (run1 / "report.md").stat().st_mtime              # ended after report
    # (b) exit 11: round limit
    fresh(tmp, repo)
    script(tmp, [{"result": "1", "write": {"value.txt": "a\n"}},
                 {"result": "2", "write": {"value.txt": "b\n"}},
                 {"result": "3", "write": {"value.txt": "c\n"}}])
    assert sup(repo, task, "--max-rounds", "3") == 11
    run2 = run_dir_of(tmp)
    assert (run2 / "asks" / ".ended").is_file()
    assert (run2 / "asks" / ".ended").stat().st_mtime >= \
        (run2 / "report.md").stat().st_mtime
    # (c) exit 11: budget exhausted (each fake round spends the default 100 tokens)
    fresh(tmp, repo)
    script(tmp, [{"result": "1", "write": {"value.txt": "a\n"}}])
    assert sup(repo, task, "--budget-tokens", "1") == 11
    assert (run_dir_of(tmp) / "asks" / ".ended").is_file()
    # (d) exit 12: no progress -- round 2 changes nothing and fails the same set
    fresh(tmp, repo)
    script(tmp, [{"result": "1", "write": {"value.txt": "a\n"}},
                 {"result": "2"}])
    assert sup(repo, task, "--max-rounds", "5") == 12
    assert (run_dir_of(tmp) / "asks" / ".ended").is_file()
    # (e) the crash way out: it re-raises without reporting, and the finally
    # still closes the inbox and stamps the marker (10.1: only those two there).
    fresh(tmp, repo)
    script(tmp, [{"result": "1", "write": {"value.txt": "a\n"}}])

    def boom(*args, **kwargs):
        # NOT a RuntimeError: the loop's `except RuntimeError` around run_checks
        # turns that into a reported "check runner failed" exit; a ValueError is
        # the crash the finally stamps on its way to re-raising.
        raise ValueError("harness-boom")
    monkeypatch.setattr(supervisor.checks, "run_checks", boom)
    sd = supervisor.repo_state_dir(str(repo))
    before = set(os.listdir(sd))
    with pytest.raises(ValueError, match="harness-boom"):
        sup(repo, task)
    new_dirs = [d for d in os.listdir(sd) if d not in before]
    assert len(new_dirs) == 1
    run5 = Path(sd) / new_dirs[0]
    assert (run5 / "asks" / ".ended").is_file()           # the finally stamped it
    assert not (run5 / "report.md").exists()              # this exit never reports


@posix_only
def test_ended_written_on_interrupt(env, page, tmp_path):
    # Ctrl-C leaves the way out the os._exit path, which skips the finally: the
    # marker is stamped there too (10.1), before the 130.
    repo, task, tmp = env
    script(tmp, [{"result": "x", "sleep": 30}])            # holds round 1 open
    envv = dict(os.environ,
                PYTHONPATH=os.path.dirname(os.path.dirname(supervisor.__file__)),
                QWEN_WATCH="1", QWEN_WATCH_NOSERVE="1",
                XDG_CACHE_HOME=str(tmp / "cache"))
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID",
                 "QWEN_WATCH_ACTIVITY"):
        envv.pop(name, None)
    p = subprocess.Popen([PY, supervisor.__file__, "--task", str(task),
                          "--repo", str(repo), "--agent", PY, "--agent", FAKE,
                          "--no-deviation-audit"],
                         env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, preexec_fn=sigint_default)
    sd = supervisor.repo_state_dir(str(repo))
    run = None
    deadline = time.time() + 30
    while run is None:
        if os.path.isdir(sd):
            for d in os.listdir(sd):
                if os.path.isfile(os.path.join(sd, d, "round-1.prompt.md")):
                    run = Path(sd) / d
                    break
        if run is None:
            assert time.time() < deadline and p.poll() is None, p.communicate()
            time.sleep(0.05)
    p.send_signal(signal.SIGINT)                           # Ctrl-C reaches the loop
    out, err = p.communicate(timeout=60)
    assert p.returncode == 130, out + err
    assert (run / "asks" / ".ended").is_file()
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "stop: interrupted (exit 130)" in report
    assert (run / "asks" / ".ended").stat().st_mtime >= \
        (run / "report.md").stat().st_mtime
