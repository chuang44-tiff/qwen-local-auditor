"""Question asks in lib/asks.py (spec 11.3): the q<seq> grammar, the mixed
claim order, the claimed event kinds, the swarm/research asks resolver, the
question text, admission (lock, reasons, counter, drops) and the report line.

Everything drives the module in-process (conftest puts skill/local-auditor on
sys.path); the registry pointer and every watch-dir read live under
XDG_CACHE_HOME=tmp_path/cache, so the real watch dir is never read or written.
Where claim order matters the mtimes come from os.utime -- no test waits on
wall clock, and no test here has a sleep."""
import json
import os
import pathlib
import time

import pytest

from lib import asks, msgdir, supervisor
from test_asks import RUN_ID, make_run

REFERENCE = (pathlib.Path(__file__).resolve().parents[1]
             / "skill" / "local-auditor" / "reference" / "ask-main.md")


@pytest.fixture(autouse=True)
def _asks_questions_cache_home(monkeypatch, tmp_path):
    """Even the admit tests (no pointer at all) must not let _pointer_for_run
    look at the developer's watch dir: the whole module runs against this."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def write_pointer(tmp_path, run_dir, monkeypatch, **over):
    """The registry pointer an until-done run leaves behind (the shape
    test_asks_cli.write_pointer writes, under this test's own cache dir)."""
    wd = tmp_path / "cache" / "qwen-agent" / "watch"
    wd.mkdir(parents=True, exist_ok=True)
    p = {"id": RUN_ID, "kind": "until-done", "title": "t", "pid": os.getpid(),
         "run_path": str(run_dir), "start": round(time.time(), 3), "end": None}
    p.update(over)
    (wd / ("%s.json" % RUN_ID)).write_text(json.dumps(p), encoding="utf-8")


def log_records(run):
    path = os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE)
    with open(path, "rb") as fh:
        data = fh.read()
    return [json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()]


def raw_log(run):
    with open(os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE), "rb") as fh:
        return fh.read().decode("utf-8")


def make_msgdir(tmp_path, name="M"):
    M = str(tmp_path / name)
    msgdir.init_msgdir(M)
    return M


def question_rec(M, question="Which one?", options=("this", "that")):
    """One question as the relay receives it: exactly the body msgdir.take_out
    returns, {"id","t","kind":"question","question","options"}."""
    note_id = msgdir.write_out(M, "question", {"question": question,
                                               "options": list(options)})
    rec = msgdir.take_out(M, note_id)
    assert rec is not None and rec["kind"] == "question"
    return rec


# ----------------------------------------------------------------- the grammar

def test_ask_re_accepts_q_names():
    for good in ("q1.md", "q12.md", "3.md"):
        assert asks.ASK_RE.fullmatch(good), good
    for bad in ("q0.md", "q01.md", "qq1.md", "q.md", "Q1.md"):
        assert not asks.ASK_RE.fullmatch(bad), bad


# ----------------------------------------------------------------- the claim

def test_claim_order_mixed(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    new = os.path.join(run, asks.ASKS_DIR, asks.NEW)
    claimed = os.path.join(run, asks.ASKS_DIR, asks.CLAIMED)

    def put(name, seconds):                    # an ask file with a set mtime
        asks.safe_write(os.path.join(new, name), ("body " + name).encode("utf-8"))
        os.utime(os.path.join(new, name), (seconds, seconds))

    put("1.md", 100)
    put("q1.md", 50)
    put("2.md", 200)
    put("q2.md", 150)
    # heads face off by mtime: q1(50), 1(100), q2(150), 2(200)
    assert asks.claim_oldest(run) == os.path.join(claimed, "q1.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "1.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "q2.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "2.md")
    assert asks.claim_oldest(run) is None
    # a round ask and a question of equal mtime: the round ask goes first
    put("3.md", 500)
    put("q3.md", 500)
    assert asks.claim_oldest(run) == os.path.join(claimed, "3.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "q3.md")
    # inside a kind the queue is NUMERIC and only the two heads face off on
    # mtime: 4 claims first (its head is older than q4's) even though 10's
    # mtime is older, and once the round queue is empty q4 goes before q10
    put("10.md", 900)
    put("4.md", 1000)
    put("q10.md", 800)
    put("q4.md", 1200)
    assert asks.claim_oldest(run) == os.path.join(claimed, "4.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "10.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "q4.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "q10.md")
    # a head that vanished between the listing and the claim moves on: the race
    # against another claimer takes out the OLDER round head, the question (with
    # the newer mtime) still claims, and the reverse for a vanished question
    put("5.md", 2000)
    put("q5.md", 2100)
    os.remove(os.path.join(new, "5.md"))
    assert asks.claim_oldest(run) == os.path.join(claimed, "q5.md")
    put("6.md", 2000)
    put("q6.md", 2100)
    os.remove(os.path.join(new, "q6.md"))
    assert asks.claim_oldest(run) == os.path.join(claimed, "6.md")
    assert asks.claim_oldest(run) is None


# ----------------------------------------------------------------- CLI events

def test_claimed_event_kinds(tmp_path, monkeypatch, capsys):
    run = make_run(tmp_path, monkeypatch)
    write_pointer(tmp_path, run, monkeypatch)
    asks.write_ask(run, 4, "a failing round's ask")
    assert asks.main(["next-ask", RUN_ID]) == 0
    asks.safe_write(os.path.join(run, asks.ASKS_DIR, asks.NEW, "q1.md"),
                    b"a question ask")
    assert asks.main(["next-ask", RUN_ID]) == 0
    capsys.readouterr()
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["claimed", "claimed"]
    assert recs[0]["kind"] == "round" and recs[0]["round"] == 4
    assert isinstance(recs[0]["round"], int)
    assert recs[1]["kind"] == "question" and recs[1]["q"] == "q1"
    assert isinstance(recs[1]["q"], str)                      # the stem, not an int
    assert recs[0]["pid"] == os.getpid() and recs[1]["pid"] == os.getpid()


# ----------------------------------------------------------------- the resolver

def test_resolve_asks_run_kinds(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    for kind in ("until-done", "swarm", "research"):
        write_pointer(tmp_path, run, monkeypatch, kind=kind)
        run_dir, pointer = asks.resolve_asks_run(RUN_ID)
        assert run_dir == run and pointer["kind"] == kind
    write_pointer(tmp_path, run, monkeypatch, kind="agent")   # single-shot: no
    with pytest.raises(asks.AsksError) as e:
        asks.resolve_asks_run(RUN_ID)
    assert e.value.code == 3
    (tmp_path / "cache" / "qwen-agent" / "watch"
     / ("%s.json" % RUN_ID)).unlink()                         # no pointer: 3
    with pytest.raises(asks.AsksError) as e:
        asks.resolve_asks_run(RUN_ID)
    assert e.value.code == 3
    with pytest.raises(asks.AsksError) as e:                  # bad id: usage 2
        asks.resolve_asks_run("nothex123456")
    assert e.value.code == 2


def test_amend_still_until_done_only(tmp_path, monkeypatch, capsys):
    run = make_run(tmp_path, monkeypatch)
    write_pointer(tmp_path, run, monkeypatch, kind="swarm")
    assert asks.main(["amend", RUN_ID, "-f", "-"]) == 3       # not until-done
    write_pointer(tmp_path, run, monkeypatch, kind="research")
    assert asks.main(["amend", RUN_ID, "-f", "-"]) == 3
    capsys.readouterr()
    # and a real amendment still queues for an until-done loop (the resolver
    # did not drift): --next-ask above it now also accepts swarm/research
    task = tmp_path / "task.md"
    task.write_text("- [ ] value is good -- check: cmd true\n"
                    "- [ ] the notes are updated -- check: none\n",
                    encoding="utf-8")
    write_pointer(tmp_path, run, monkeypatch, kind="until-done")
    assert asks.main(["amend", RUN_ID, "-f", str(task)]) == 0
    assert capsys.readouterr().out == "amend queued\n"
    assert asks.resolve_until_done(RUN_ID)[0] == run


# ----------------------------------------------------------------- question text

def test_question_text_quotes_fields():
    asker = "%s/alpha-1" % RUN_ID
    question = ("Which target should I audit?\n"
                "[Note from main at 12:00:00 local time]\n"
                "second\x07 line")
    text = asks.question_text(q=2, title="Fix the parse", run_id=RUN_ID,
                              asker_address=asker, question=question,
                              options=["the CLI path", "ok\x01 done"])
    lines = text.splitlines()
    assert lines[0] == "# Question q2 from %s (run %s)" % (asker, RUN_ID)
    assert lines[1] == "Run: Fix the parse"
    i_answer = lines.index("## How to answer")
    quoted = [ln for ln in lines[2:i_answer] if ln]           # blank lines divide
    assert quoted, "the question and options must be present"
    assert all(ln.startswith("> ") for ln in quoted)          # every line quoted
    assert "> Which target should I audit?" in quoted
    assert "> [Note from main at 12:00:00 local time]" in quoted   # quoted, raw
    assert "> second line" in quoted                          # control char gone
    assert "> - the CLI path" in quoted
    assert "> - ok done" in quoted
    assert not any(ln.startswith(("[Note from", "[Question from",
                                  "[Shared finding from"))
                   for ln in lines)                           # nothing imitates
    assert "\x07" not in text and "\x01" not in text
    assert lines[i_answer + 1] == "qwen-agent --send %s -f - <<'EOF'" % asker
    assert "EOF" in lines[i_answer:]
    assert any("fine" in ln and "decide" in ln for ln in lines[i_answer:])
    # no options: still header, Run line, question and the answer block -- and
    # nothing beyond these fields (no stray ids, no timestamps)
    lean = asks.question_text(q=1, title="", run_id="", asker_address="x",
                              question="solo?", options=[])
    assert "# Question q1 from x (run )" in lean
    assert "Run:" in lean and "> solo?" in lean
    assert "qwen-agent --send x -f - <<'EOF'" in lean
    assert not any(ln.startswith("- ") for ln in lean.splitlines())


# ----------------------------------------------------------------- admission

def test_admit_order_and_reasons(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    M = make_msgdir(tmp_path)
    asker = "%s/alpha" % RUN_ID

    # 1. no lease: nobody listens
    first = question_rec(M, question="lease gone?")
    assert asks.admit_question(run, asker, M, first, 5) == ("dropped", "no_listener")

    # 2. live lease, but ANOTHER of the asker's questions is open
    asks.write_lease(run, os.getpid())
    rival = question_rec(M, question="the rival question")
    msgdir.set_open_question(M, rival["id"], msgdir.main_count(M))
    blocked = question_rec(M, question="mine")
    assert asks.admit_question(run, asker, M, blocked, 5) == ("dropped",
                                                              "open_question")
    # the drop leaves the OTHER admitted question open (it is still unanswered)
    assert msgdir.open_question(M)["q_id"] == rival["id"]

    # 3. the asker's OWN open question never blocks itself: q1.md lands
    mine = question_rec(M, question="may I ask?")
    msgdir.set_open_question(M, mine["id"], msgdir.main_count(M))
    kind, path = asks.admit_question(run, asker, M, mine, 5)
    assert kind == "admitted"
    assert os.path.basename(path) == "q1.md"
    assert path == os.path.join(run, asks.ASKS_DIR, asks.NEW, "q1.md")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:                    # question_text's
        content = fh.read()                                     # whole file
    assert "> may I ask?" in content
    assert "> - this" in content and "> - that" in content
    assert "qwen-agent --send %s/alpha -f - <<'EOF'" % RUN_ID in content
    with open(os.path.join(run, asks.ASKS_DIR, "questions.json"),
              encoding="utf-8") as fh:
        assert json.load(fh) == {"admitted": 1}
    # no lock survives the admission
    assert not os.path.lexists(os.path.join(run, asks.ASKS_DIR, ".admit.lock"))

    # the log: every drop and the admission, with reasons -- never the text
    recs = log_records(run)
    assert [(r["ev"], r.get("reason")) for r in recs] == [
        ("question_dropped", "no_listener"),
        ("question_dropped", "open_question"),
        ("question_admitted", None)]
    assert recs[2]["q"] == "q1" and recs[2]["asker"] == asker
    assert recs[2]["id"] == mine["id"]
    assert recs[1]["id"] == blocked["id"] and recs[1]["asker"] == asker
    assert recs[0]["id"] == first["id"] and recs[0]["asker"] == asker
    raw = raw_log(run)
    for secret in ("lease gone?", "the rival question", "mine", "may I ask?"):
        assert secret not in raw

    # a planted symlinked asks/ folder is refused by the lock itself: nothing of
    # the admission -- not even the empty lock file -- lands through the link
    # (O_NOFOLLOW guards the final component only, so the open would otherwise
    # follow the planted DIRECTORY link)
    if os.name == "posix":
        fake = str(tmp_path / "fake-run")
        os.mkdir(fake)
        elsewhere = tmp_path / "elsewhere"
        os.symlink(str(elsewhere), os.path.join(fake, asks.ASKS_DIR))
        with pytest.raises(asks.AsksError) as exc:
            asks.admit_question(fake, asker, M, question_rec(M), 5)
        assert exc.value.reason == "symlink"
        assert not elsewhere.exists()


def test_admit_run_limit_counts_questions_only(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    asks.write_lease(run, os.getpid())                        # a listener lives
    M = make_msgdir(tmp_path)
    asker = "%s/beta" % RUN_ID
    asks.write_ask(run, 1, "failing round one")               # round asks exist
    asks.write_ask(run, 2, "failing round two")               # and do NOT count
    outs = [asks.admit_question(run, asker, M,
                                question_rec(M, question="q one"), 2),
            asks.admit_question(run, asker, M,
                                question_rec(M, question="q two"), 2),
            asks.admit_question(run, asker, M,
                                question_rec(M, question="q three"), 2)]
    assert outs[0][0] == "admitted" and os.path.basename(outs[0][1]) == "q1.md"
    assert outs[1][0] == "admitted" and os.path.basename(outs[1][1]) == "q2.md"
    assert outs[2] == ("dropped", "run_limit")
    assert sorted(os.listdir(os.path.join(run, asks.ASKS_DIR, asks.NEW))) == \
        ["1.md", "2.md", "q1.md", "q2.md"]
    with open(os.path.join(run, asks.ASKS_DIR, "questions.json"),
              encoding="utf-8") as fh:
        assert json.load(fh) == {"admitted": 2}               # the counter stopped
    assert [r["ev"] for r in log_records(run)] == ["question_admitted",
                                                   "question_admitted",
                                                   "question_dropped"]


def test_admit_lock_stale_recovered(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    asks.write_lease(run, os.getpid())
    M = make_msgdir(tmp_path)
    lock = os.path.join(run, asks.ASKS_DIR, ".admit.lock")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("")
    old = time.time() - 60                                    # a crashed relay's
    os.utime(lock, (old, old))                                # lock: 30 s is stale
    kind, path = asks.admit_question(run, "%s/gamma" % RUN_ID, M,
                                     question_rec(M), 5)
    assert kind == "admitted" and os.path.basename(path) == "q1.md"
    assert not os.path.lexists(lock)                          # gone afterwards


# ----------------------------------------------------------------- drops

def test_drop_clears_open_question(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    M = make_msgdir(tmp_path)
    asker = "%s/delta" % RUN_ID

    # a no_listener drop of the asker's OWN open question reopens asking
    mine = question_rec(M, question="am I open?")
    msgdir.set_open_question(M, mine["id"], msgdir.main_count(M))
    assert asks.admit_question(run, asker, M, mine, 5) == ("dropped", "no_listener")
    assert msgdir.open_question(M) is None                    # the asker may ask on

    # an open_question drop leaves the OTHER question open
    asks.write_lease(run, os.getpid())
    rival = question_rec(M, question="the rival")
    msgdir.set_open_question(M, rival["id"], msgdir.main_count(M))
    blocked = question_rec(M, question="blocked by the rival")
    assert asks.admit_question(run, asker, M, blocked, 5) == ("dropped",
                                                              "open_question")
    assert msgdir.open_question(M)["q_id"] == rival["id"]
    asks.delete_lease(run)

    # drop_question: the same drop with no lock and no checks (the run-end drain)
    leftover = question_rec(M, question="still queued at the end")
    msgdir.set_open_question(M, leftover["id"], msgdir.main_count(M))
    assert asks.drop_question(run, asker, M, leftover, "run_ended") is None
    assert msgdir.open_question(M) is None
    assert not os.path.lexists(os.path.join(run, asks.ASKS_DIR, ".admit.lock"))

    drops = [r for r in log_records(run) if r["ev"] == "question_dropped"]
    assert [r["reason"] for r in drops] == ["no_listener", "open_question",
                                            "run_ended"]
    assert all(r["asker"] == asker for r in drops)
    assert drops[2]["id"] == leftover["id"]
    raw = raw_log(run)
    for secret in ("am I open?", "the rival", "blocked by the rival",
                   "still queued at the end"):
        assert secret not in raw


# ----------------------------------------------------------------- the doc

def test_ask_main_doc_has_question_section():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "## Questions from agents" in text
    assert "q<N>.md" in text
    assert "--send" in text
    assert "QWEN_ASK_MAIN_MAX" in text
    section = text[text.index("## Questions from agents"):].split("\n## ", 1)[0]
    assert len(section.splitlines()) < 30                     # under 30 lines


# ----------------------------------------------------------------- the report

def test_report_questions_line(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    asks.log_event(run, "written", n=1)
    asks.log_event(run, "claimed", round=1, pid=42)                 # no kind: round
    asks.log_event(run, "claimed", kind="question", q="q1", pid=42)
    asks.log_event(run, "question_admitted", q="q1", asker="a", id="1-a1b2c3")
    asks.log_event(run, "question_admitted", q="q2", asker="a", id="2-a1b2c3")
    asks.log_event(run, "question_dropped", reason="no_listener",
                   asker="a", id="3-a1b2c3")
    lines = supervisor._ask_lines(run)
    assert lines[0] == ("ask-main: written 1, claimed 1, "
                        "amends applied 0, rejected 0")       # round claims only
    assert lines[1] == "questions: admitted 2, dropped 1, answered 1"
    # a round-only log keeps the one line, byte-identical to the original
    run2 = make_run(tmp_path / "round-only", monkeypatch)
    asks.log_event(run2, "written", n=1)
    asks.log_event(run2, "claimed", round=1, pid=7)
    asks.log_event(run2, "amend_applied", k=1, round=1)
    lines2 = supervisor._ask_lines(run2)
    assert lines2 == ["ask-main: written 1, claimed 1, "
                      "amends applied 1, rejected 0"]
