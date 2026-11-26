# ruff: noqa: F811
"""An until-done coder can ask the main session a question while its round runs
(spec 11.3, 12.3), and answers to questions survive across rounds.

Three pieces, one file (spec 12.3, 12.4, 12.2, 11.2, 12.7):
  * the msg env: every messaged round is handed RUN/msg as its qla_msg folder, its
    name and its tool list -- plus the asks dir wherever ask_main rides; the
    deviation audit and a QWEN_STREAM=0 loop carry none of it, and no RUN/msg is
    built there;
  * the relay: a daemon thread admits questions from RUN/msg/out/new/ into the asks
    layout while a round runs (asks.admit_question owns the refusals), answers
    relay-side refusals with a harness note, and _close_run_inbox's final pass drops
    what a round leaves queued as run_ended;
  * the drain: a deferred `ask` gets its seen/ask-<id> marker BEFORE it is rendered
    (so the reply tool accepts an answer next round) and note/ask bodies bump
    seen/main-count -- the marking and counting the stream driver does.

Driven on the test_supervisor harness (env/script/sup, the fake agent -- extended
here with the plant_question / run_write_then_sleep / record_env step keys) and the
test_supervisor_asks helpers (page, run_dir_of, live_lease, fresh, ...). XDG_CACHE_HOME
is pinned under tmp_path for the whole file, so a registered loop never reaches the
real watch registry; QWEN_STREAM and a developer's QWEN_ASK_LEASE / QWEN_ASK_MAIN_MAX
are scrubbed so only a test decides messaging and the question cap."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from lib import asks, msgdir, notes, supervisor
from test_supervisor import FAKE, PY, calls, env, script, sup  # noqa: E402,F401
from test_supervisor_asks import (claimed_asks, fresh, live_lease, loop_events,  # noqa: E402,F401
                                  new_asks, page, report_of, run_dir_of)

MSG_ENV_NAMES = ["QWEN_MSG_DIR", "QWEN_MSG_SELF", "QWEN_MSG_TOOLS", "QWEN_ASKS_DIR"]


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path, monkeypatch):
    """The file's rules (test_supervisor_asks' twin, plus the question cap):
    XDG_CACHE_HOME at tmp_path/cache, messaging on unless a test says otherwise,
    and no developer lease or ask_main cap decides what the relay admits."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    monkeypatch.delenv("QWEN_ASK_MAIN_MAX", raising=False)


# ------------------------------------------------------------------- helpers

def ask_log_records(run):
    """The run's asks/log.jsonl records (a torn line is skipped, never an error)."""
    path = os.path.join(str(run), "asks", asks.LOG_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def env_records(tmp):
    """$FAKE_AGENT_RECORD.env: one dict per call that asked for it, in call order."""
    p = tmp / "record.jsonl.env"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]


def watch_id_of(tmp, run):
    """The watch id of the registered loop whose run folder this is (the pointer's
    run_path names it), or None when no pointer does."""
    d = tmp / "cache" / "qwen-agent" / "watch"
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("run_path") == str(run):
            return f.stem
    return None


def prompt_of(call_argv):
    with open(call_argv[call_argv.index("-f") + 1], encoding="utf-8") as fh:
        return fh.read()


def q_asks(run):
    """Every question ask this run holds -- new or claimed (the q<seq> grammar)."""
    return ([n for n in new_asks(run) if n.startswith("q")]
            + [n for n in claimed_asks(run) if n.startswith("q")])


def _ask_while_round_runs(tmp, repo, task):
    """One registered loop: round 1 sleeps while the TEST queues an `ask` note for
    the loop -- round 1's drain ran before its prompt was written, so only round 2's
    drain can deliver it -- and round 2 fixes the tree. The loop runs as a child
    process (the queue must land while a round is in flight). Returns
    (rc, run dir, note id, child output) -- the caller asserts the rc, so a stuck
    child still says what it said."""
    script(tmp, [{"result": "r1", "sleep": 6},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    envv = dict(os.environ,
                PYTHONPATH=os.path.dirname(os.path.dirname(supervisor.__file__)),
                QWEN_WATCH="1", QWEN_WATCH_NOSERVE="1",
                XDG_CACHE_HOME=str(tmp / "cache"))
    for name in ("QWEN_WATCH_CHILD", "QWEN_WATCH_PARENT", "QWEN_WATCH_ID",
                 "QWEN_WATCH_ACTIVITY"):
        envv.pop(name, None)                    # the suite's outer run is not this one's
    p = subprocess.Popen([PY, supervisor.__file__, "--task", str(task),
                          "--repo", str(repo), "--agent", PY, "--agent", FAKE,
                          "--no-deviation-audit"],
                         env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    out = err = ""
    note_id = None
    try:
        sd = supervisor.repo_state_dir(str(repo))
        run = None
        deadline = time.time() + 30
        while run is None:
            if os.path.isdir(sd):
                for d in sorted(os.listdir(sd)):
                    if os.path.isfile(os.path.join(sd, d, "round-1.prompt.md")):
                        run = Path(sd) / d
                        break
            if run is None:
                if p.poll() is not None or time.time() >= deadline:
                    raise AssertionError(
                        "the loop died (exit %s) before round 1 ran: %s"
                        % (p.returncode, "".join(p.communicate(timeout=10))))
                time.sleep(0.05)
        wid = watch_id_of(tmp, run)
        assert wid, "the loop never registered a pointer"
        # the queue lands while round 1 sleeps: only round 2's drain can take it
        note_id = notes.queue_note(str(run / "inbox"), wid,
                                   "Why not the queue approach?", kind="ask")
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    return p.returncode, run, note_id, out + err


# ------------------------------------------------------------------- the relay

def test_question_admitted_mid_round(env, page, tmp_path):
    # Round 1 asks (the fake plants what the ask_main tool writes) and then sleeps
    # 4 s. The relay's 1 s poll admits it into the asks layout WHILE the round is
    # still running: the admission's log stamp predates round 1's own round_end.
    repo, task, tmp = env
    question = "Should the backlog be a deque or a plain list?"
    script(tmp, [{"result": "thinking", "sleep": 4,
                  "plant_question": {"question": question, "options": []},
                  "run_write_then_sleep": True,
                  "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run = run_dir_of(tmp)
    q1 = os.path.join(str(run), "asks", "new", "q1.md")
    if not os.path.isfile(q1):                   # a claimed question is equally admitted
        q1 = os.path.join(str(run), "asks", "claimed", "q1.md")
    assert os.path.isfile(q1), q_asks(run)
    with open(q1, encoding="utf-8") as fh:
        assert question in fh.read()             # the admitted ask carries the question
    admitted = [r for r in ask_log_records(run) if r.get("ev") == "question_admitted"]
    assert len(admitted) == 1
    assert admitted[0].get("asker") == watch_id_of(tmp, run)
    ends = [e for e in loop_events(run)
            if e.get("kind") == "round_end" and e.get("round") == 1]
    assert len(ends) == 1
    assert admitted[0]["t"] < ends[0]["t"], (admitted[0]["t"], ends[0]["t"])


def test_question_dropped_run_limit_sends_harness_note(env, page, tmp_path, monkeypatch):
    # QWEN_ASK_MAIN_MAX=0: the relay's admission hits the run cap (spec 11.3) and
    # the refusal answers the asker (spec 12.4): a `harness` note rides into round
    # 2's prompt with notes.render_note's own header. The loop is not steered by
    # it: the no-progress decision kept looking at the tree and the checks alone.
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_ASK_MAIN_MAX", "0")
    script(tmp, [{"result": "thinking", "sleep": 4,
                  "plant_question": {"question": "Which table layout?",
                                     "options": ["rows", "columns"]},
                  "run_write_then_sleep": True,
                  "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run = run_dir_of(tmp)
    assert q_asks(run) == []                     # never admitted
    dropped = [r for r in ask_log_records(run) if r.get("ev") == "question_dropped"]
    assert len(dropped) == 1 and dropped[0].get("reason") == "run_limit"
    prompt2 = prompt_of(calls(tmp)[1])
    assert "[Note from the harness at" in prompt2
    assert "your question was not sent (run_limit)" in prompt2
    assert "decide yourself and say what you assumed" in prompt2


def test_question_left_at_end_dropped(env, page, tmp_path, monkeypatch):
    # The only round asks and the loop ends before the relay ever polls (RELAY_POLL
    # raised to a minute -- the test hook): _close_run_inbox's final pass takes the
    # queue itself and drops the question as run_ended (spec 12.3). No ask file
    # ever stands and nothing stays pending in the msg queue.
    repo, task, tmp = env
    monkeypatch.setattr(supervisor, "RELAY_POLL", 60.0)
    script(tmp, [{"result": "thinking",
                  "plant_question": {"question": "Any preference on the module name?",
                                     "options": []},
                  "run_write": {"asks/lease.json": live_lease()}}])
    assert sup(repo, task, "--max-rounds", "1") == 11
    run = run_dir_of(tmp)
    assert q_asks(run) == []
    dropped = [r for r in ask_log_records(run) if r.get("ev") == "question_dropped"]
    assert len(dropped) == 1 and dropped[0].get("reason") == "run_ended"
    assert msgdir.list_out(os.path.join(str(run), "msg")) == []


# ------------------------------------------------------------------- the drain

def test_deferred_ask_marked_and_rendered(env):
    # An ask note for the loop queued while round 1 sleeps is drained into round
    # 2's prompt (spec 11.2: the until-done msg dir persists across rounds) --
    # marked the way the driver marks (seen/ask-<id> written BEFORE the render)
    # and counted (seen/main-count 1, spec 11.3/12.2).
    repo, task, tmp = env
    rc, run, note_id, log = _ask_while_round_runs(tmp, repo, task)
    assert rc == 0, log
    seen = os.path.join(str(run), "msg", "seen")
    assert os.path.isfile(os.path.join(seen, "ask-%s" % note_id))
    cc = calls(tmp)
    assert len(cc) == 2
    prompt2 = prompt_of(cc[1])
    assert "[Question from main at" in prompt2
    assert note_id in prompt2
    assert "Why not the queue approach?" in prompt2
    assert "[Question from main at" not in prompt_of(cc[0])   # round 1 never saw it
    with open(os.path.join(seen, "main-count"), encoding="utf-8") as fh:
        assert fh.read().strip() == "1"


def test_reply_to_deferred_ask_works_next_round(env):
    # The marker is what the reply tool checks (spec 12.2): the drain wrote it
    # before the model could see the id, so after that drain a reply publishes
    # exactly once and reads back whole -- across rounds, from RUN/msg.
    repo, task, tmp = env
    rc, run, note_id, log = _ask_while_round_runs(tmp, repo, task)
    assert rc == 0, log
    M = os.path.join(str(run), "msg")
    assert msgdir.ask_seen(M, note_id)
    assert msgdir.publish_reply(M, note_id, "x") is True
    rec = msgdir.read_reply(M, note_id)
    assert rec is not None
    assert rec["note_id"] == note_id and rec["text"] == "x"
    assert rec["source"] == "reply"


# ------------------------------------------------------------------- the round env

def test_round_env_has_msg_vars_only_with_inbox(env, page, tmp_path, monkeypatch):
    repo, task, tmp = env

    # (a) a registered loop with the asks layout open and a LIVE lease (planted by
    # the round itself, as every question-asking test does): the full msg env rides
    # on every round -- the loop's own folder, name, tools and asks dir (spec 12.7)
    script(tmp, [{"result": "ok", "write": {"value.txt": "good\n"},
                  "run_write": {"asks/lease.json": live_lease()},
                  "record_env": MSG_ENV_NAMES}])
    assert sup(repo, task) == 0
    run_a = run_dir_of(tmp)
    assert os.path.isfile(os.path.join(str(run_a), "asks", "lease.json"))  # the live lease really stood
    recs = env_records(tmp)
    assert len(recs) == 1
    assert recs[0]["QWEN_MSG_DIR"] == os.path.join(str(run_a), "msg")
    assert recs[0]["QWEN_MSG_SELF"] == watch_id_of(tmp, run_a)
    assert recs[0]["QWEN_MSG_TOOLS"] == "reply,ask_main"
    assert recs[0]["QWEN_ASKS_DIR"] == os.path.join(str(run_a), "asks")

    # (b) QWEN_STREAM=0: the no-messaging baseline -- none of the four keys is set
    # and no RUN/msg is built (the matrix's all-no row)
    fresh(tmp, repo)
    monkeypatch.setenv("QWEN_STREAM", "0")
    script(tmp, [{"result": "ok", "write": {"value.txt": "good\n"},
                  "record_env": MSG_ENV_NAMES}])
    assert sup(repo, task) == 0
    run_b = run_dir_of(tmp)
    recs = env_records(tmp)
    assert len(recs) == 2
    assert all(recs[1][name] is None for name in MSG_ENV_NAMES)
    assert not (run_b / "msg").exists()
    monkeypatch.delenv("QWEN_STREAM", raising=False)

    # (c) the deviation audit call: none of the four keys -- its argv and env are
    # what they were before the msg env existed (the audited diff sees no server)
    fresh(tmp, repo)
    script(tmp, [{"result": "NO CONTRADICTIONS", "write": {"value.txt": "good\n"},
                  "record_env": MSG_ENV_NAMES}])
    assert supervisor.main(["--task", str(task), "--repo", str(repo),
                            "--agent", PY, "--agent", FAKE]) == 0
    cc = calls(tmp)
    recs = env_records(tmp)
    assert len(cc) == 4 and len(recs) == 4       # (a) + (b) + round + audit
    run_c = run_dir_of(tmp)
    assert cc[-2][cc[-2].index("-r") + 1] == "coder"
    assert recs[-2]["QWEN_MSG_DIR"] == os.path.join(str(run_c), "msg")
    assert recs[-2]["QWEN_MSG_TOOLS"] == "reply,ask_main"
    assert cc[-1][cc[-1].index("-r") + 1] == "auditor"
    assert all(recs[-1][name] is None for name in MSG_ENV_NAMES)


# ------------------------------------------------------------------- the sentinel

def test_thinking_sentinel_absent_until_done(env, page, tmp_path, monkeypatch):
    # The fake's result text and a planted transcript line carry SENTINEL-7f3a;
    # the question it plants does not. Once the run is over the sentinel names no
    # messaging or asks material (spec 12.8): no file under RUN/msg/ or RUN/asks/,
    # not report.md, not the loop's events file. The admission must be real for
    # the absence to mean anything -- hence the q1.md pin.
    repo, task, tmp = env
    # The planted transcript line (the session the loop's tool counter reads)
    # carries the sentinel in its text block: the supervisor sees this file every
    # round, but its counter takes tool NAMES only -- the sentinel has no way into
    # the report's tool table through a name, only through a leak.
    tdir = tmp / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "s1.jsonl").write_text(
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "thinking SENTINEL-7f3a twice"}]}})
        + "\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tdir))
    script(tmp, [{"result": "I turned SENTINEL-7f3a over twice before choosing",
                  "sleep": 4,
                  "plant_question": {"question": "Plain wording, no marker here.",
                                     "options": ["left", "right"]},
                  "run_write_then_sleep": True,
                  "run_write": {"asks/lease.json": live_lease()}},
                 {"result": "ok", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run = run_dir_of(tmp)
    # the sentinel really was in the round's output and in a transcript line
    assert "SENTINEL-7f3a" in (run / "round-1.json").read_text(encoding="utf-8")
    tp = tdir / "s1.jsonl"
    assert tp.is_file() and "SENTINEL-7f3a" in tp.read_text(encoding="utf-8")
    # and the question really was admitted
    assert q_asks(run) == ["q1.md"]
    needle = b"SENTINEL-7f3a"
    for folder in (run / "msg", run / "asks"):
        for base, _dirs, files in os.walk(str(folder)):
            for name in files:
                path = os.path.join(base, name)
                with open(path, "rb") as fh:
                    assert needle not in fh.read(), path
    assert needle not in report_of(run).encode("utf-8")
    with open(os.path.join(str(run), "events.jsonl"), "rb") as fh:
        assert needle not in fh.read()
