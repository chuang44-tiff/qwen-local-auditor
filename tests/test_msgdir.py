"""lib/msgdir.py: one agent's message folder -- the out queue, the published
replies, the driver's seen markers, the open-question state, the share state
and the per-phase findings log.

Every test drives the module in-process (conftest puts skill/local-auditor on
sys.path) under tmp_path; only test_reply_publish_race spawns processes, and
only the symlink halves are POSIX-only. Nothing here registers a run or
touches the watch registry or the real cache dirs."""
import json
import multiprocessing
import os
import sys
import time

import pytest

from lib import msgdir, notes

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="symlinks and the uid check are POSIX")

NOTE_ID = "1700000000000000000-a1b2c3"      # a note id as notes.NOTE_ID_RE wants it


def make_msgdir(tmp_path, name="M"):
    """An initialised msg dir under tmp_path (init_msgdir creates every parent,
    so the run folder comes with it)."""
    M = str(tmp_path / name)
    msgdir.init_msgdir(M)
    return M


# ----------------------------------------------------------------- the layout

def test_init_and_layout_modes(tmp_path):
    M = str(tmp_path / "run" / "msg")
    msgdir.init_msgdir(M)
    dirs = [M,
            os.path.join(M, "out"),
            os.path.join(M, "out", "new"),
            os.path.join(M, "out", "taken"),
            os.path.join(M, "replies"),
            os.path.join(M, "seen")]
    for path in dirs:
        assert os.path.isdir(path), path
    msgdir.init_msgdir(M)                     # idempotent: a resumed loop re-inits
    if os.name == "posix":
        for path in dirs:
            assert os.stat(path).st_mode & 0o777 == 0o700, path


def test_unit_named_like_layout_dirs(tmp_path):
    run = str(tmp_path / "run")
    for unit in ("_phases", "units"):
        assert msgdir.unit_msgdir(run, unit) == os.path.join(run, "msg", "units", unit)
    assert msgdir.unit_msgdir(run, "_phases") != msgdir.unit_msgdir(run, "units")
    assert msgdir.unit_msgdir(run, "_phases") != msgdir.phase_dir(run, "p1")
    assert msgdir.unit_msgdir(run, "units") != msgdir.phase_dir(run, "p1")
    assert msgdir.phase_dir(run, "p1") == os.path.join(run, "msg", "_phases", "p1")
    assert msgdir.loop_msgdir(run) == os.path.join(run, "msg")
    watch = str(tmp_path / "watch")
    assert msgdir.single_msgdir(watch, "a1b2c3d4e5f6") \
        == os.path.join(watch, "a1b2c3d4e5f6.msg")
    # and on disk they coexist: a unit named like a layout dir keeps its msg
    # dir while the findings of a phase (even one with the same name) land
    # under _phases/ -- nothing collides
    for unit in ("_phases", "units"):
        msgdir.init_msgdir(msgdir.unit_msgdir(run, unit))
    msgdir.findings_append(run, "p1", {"id": NOTE_ID, "text": "a finding"})
    msgdir.findings_append(run, "_phases", {"id": NOTE_ID, "text": "another"})
    assert msgdir.findings_read(run, "p1") == [{"id": NOTE_ID, "text": "a finding"}]
    assert msgdir.findings_read(run, "_phases") == [{"id": NOTE_ID, "text": "another"}]
    for unit in ("_phases", "units"):
        assert os.path.isdir(os.path.join(msgdir.unit_msgdir(run, unit), "out", "new"))


# ----------------------------------------------------------------- the out queue

def test_write_take_out(tmp_path, monkeypatch):
    M = make_msgdir(tmp_path)
    note_id = msgdir.write_out(M, "share",
                               {"phase": "p1", "finding": "found it", "paths": ["a.py"]})
    # list_out takes the note grammar only: junk names, a .tmp and a directory
    # that would otherwise match are none of its business
    new_dir = os.path.join(M, "out", "new")
    with open(os.path.join(new_dir, "junk.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    with open(os.path.join(new_dir, ".%s.7.tmp" % note_id), "w",
              encoding="utf-8") as fh:
        fh.write("{}")
    os.mkdir(os.path.join(new_dir, "1700000000000000000-ff00ff.json"))
    assert msgdir.list_out(M) == [note_id]
    body = msgdir.take_out(M, note_id)
    assert body["id"] == note_id
    assert body["kind"] == "share"
    assert body["finding"] == "found it" and body["paths"] == ["a.py"]
    assert isinstance(body["t"], float)
    assert not os.path.exists(os.path.join(M, "out", "new", note_id + ".json"))
    assert os.path.isfile(os.path.join(M, "out", "taken", note_id + ".json"))
    assert msgdir.list_out(M) == []
    assert msgdir.take_out(M, note_id) is None          # the file is gone
    with pytest.raises(msgdir.MsgError):
        msgdir.write_out(M, "bogus", {})
    # an unreadable file moves to taken/ too, so nobody reads it again
    bad_id = "9999999999999999999-dead01"
    with open(os.path.join(M, "out", "new", bad_id + ".json"), "w",
              encoding="utf-8") as fh:
        fh.write("this is not json")
    assert msgdir.list_out(M) == [bad_id]
    assert msgdir.take_out(M, bad_id) is None
    assert os.path.isfile(os.path.join(M, "out", "taken", bad_id + ".json"))
    assert msgdir.list_out(M) == []

    # the true lost race: a sibling wins between our body read and our move,
    # so the ENOENT arrives from os.replace inside move_note -- take_out must
    # answer None, never let the FileNotFoundError ride out of the module
    def rival_taker(src, dst, retries=4):
        os.unlink(src)                                   # the sibling took it
        return notes.move_note(src, dst, retries)        # ... and raises there
    monkeypatch.setattr(msgdir, "move_note", rival_taker)
    rival_id = msgdir.write_out(M, "question", {"question": "rival?"})
    assert msgdir.take_out(M, rival_id) is None
    assert msgdir.list_out(M) == []                      # nothing pending to re-read
    monkeypatch.undo()                                   # real move_note again

    # the other ENOENT: out/taken/ gone (an external sweep removed it) while
    # the source is still here. That is a broken layout, NOT a lost race --
    # None here would leave the item pending for the caller to spin on.
    M2 = make_msgdir(tmp_path, name="M2")
    os.rmdir(os.path.join(M2, "out", "taken"))
    keep_id = msgdir.write_out(M2, "share", {"finding": "keep me"})
    with pytest.raises(msgdir.MsgError):
        msgdir.take_out(M2, keep_id)
    assert os.path.isfile(os.path.join(M2, "out", "new", keep_id + ".json"))
    os.mkdir(os.path.join(M2, "out", "taken"))            # layout restored
    assert msgdir.take_out(M2, keep_id)["finding"] == "keep me"


# ----------------------------------------------------------------- seen, count, question

def test_seen_markers_and_main_count(tmp_path):
    M = make_msgdir(tmp_path)
    assert msgdir.ask_seen(M, NOTE_ID) is False
    msgdir.mark_ask_seen(M, NOTE_ID)
    assert msgdir.ask_seen(M, NOTE_ID) is True
    msgdir.mark_ask_seen(M, NOTE_ID)                    # idempotent
    assert msgdir.ask_seen(M, NOTE_ID) is True
    marker = os.path.join(M, "seen", "ask-" + NOTE_ID)
    assert os.path.isfile(marker) and os.path.getsize(marker) == 0
    assert msgdir.main_count(M) == 0
    assert msgdir.bump_main_count(M) == 1
    assert msgdir.bump_main_count(M) == 2
    assert msgdir.main_count(M) == 2
    with open(os.path.join(M, "seen", "main-count"), "rb") as fh:
        assert fh.read() == b"2"
    with open(os.path.join(M, "seen", "main-count"), "wb") as fh:
        fh.write(b"not a number")               # a torn counter reads as 0, not a crash
    assert msgdir.main_count(M) == 0
    assert msgdir.bump_main_count(M) == 1


def test_open_question_closes_on_main_note_or_clear(tmp_path):
    M = make_msgdir(tmp_path)
    assert msgdir.open_question(M) is None
    seq = msgdir.main_count(M)
    msgdir.set_open_question(M, NOTE_ID, seq)
    assert msgdir.open_question(M) == {"q_id": NOTE_ID, "main_seq": seq}
    msgdir.bump_main_count(M)                           # a main note landed: closed
    assert msgdir.open_question(M) is None
    msgdir.set_open_question(M, NOTE_ID, msgdir.main_count(M))
    assert msgdir.open_question(M) is not None
    msgdir.clear_open_question(M)
    assert msgdir.open_question(M) is None
    # a malformed state file reads as "no open question", never a raise
    with open(os.path.join(M, "open-question.json"), "w", encoding="utf-8") as fh:
        fh.write('{"q_id": "x", "main_seq": "nope"}\n')
    assert msgdir.open_question(M) is None


# ----------------------------------------------------------------- replies

def _publish_worker(M, note_id, text, barrier, result_q):
    """One racing reply: wait for the sibling at the barrier, then publish."""
    try:
        barrier.wait()
        result_q.put((text, msgdir.publish_reply(M, note_id, text)))
    except BaseException as exc:                        # report, never hang
        result_q.put((text, repr(exc)))


def test_reply_publish_race(tmp_path):
    M = make_msgdir(tmp_path)
    texts = ["reply from the first process", "reply from the second process"]
    ctx = multiprocessing.get_context()
    barrier = ctx.Barrier(2, timeout=30)
    result_q = ctx.Queue()
    procs = [ctx.Process(target=_publish_worker,
                         args=(M, NOTE_ID, text, barrier, result_q))
             for text in texts]
    try:
        for proc in procs:
            proc.start()
        results = [result_q.get(timeout=30) for _ in procs]
        for proc in procs:
            proc.join(timeout=30)
            assert proc.exitcode == 0, proc.exitcode
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=10)
    oks = [ok for _text, ok in results]
    assert sorted(repr(ok) for ok in oks) == ["False", "True"], results
    winners = [text for text, ok in results if ok is True]
    assert len(winners) == 1
    reply = msgdir.read_reply(M, NOTE_ID)
    assert reply is not None, results
    assert reply == {"note_id": NOTE_ID, "t": reply["t"],
                     "source": "reply", "text": winners[0]}
    with open(os.path.join(M, "replies", NOTE_ID + ".json"), "rb") as fh:
        json.loads(fh.read())                           # parses whole, no partial
    # the one final file and no .tmp (nor a .lock) left behind
    assert os.listdir(os.path.join(M, "replies")) == [NOTE_ID + ".json"]
    # and the answer is final: a later publish for the same id loses, quietly
    assert msgdir.publish_reply(M, NOTE_ID, "third time unlucky") is False
    assert os.listdir(os.path.join(M, "replies")) == [NOTE_ID + ".json"]
    assert msgdir.read_reply(M, NOTE_ID)["text"] == winners[0]


def test_reply_stale_tmp_swept(tmp_path):
    M = make_msgdir(tmp_path)
    replies = os.path.join(M, "replies")
    stale = os.path.join(replies, ".%s.111.tmp" % NOTE_ID)
    fresh = os.path.join(replies, ".%s.222.tmp" % NOTE_ID)
    final = os.path.join(replies, NOTE_ID + ".json")
    for path in (stale, fresh, final):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
    long_ago = time.time() - 2 * 3600                   # written 2 h ago
    os.utime(stale, (long_ago, long_ago))
    assert msgdir.sweep_stale_tmp(M) == 1
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)
    assert os.path.exists(final)                        # a published reply is untouched
    assert msgdir.sweep_stale_tmp(M) == 0


# ----------------------------------------------------------------- share state, findings

def test_share_state_roundtrip(tmp_path):
    # a msg dir whose path holds spaces: every join in the module must cope
    M = make_msgdir(tmp_path, name="a run dir with spaces")
    assert msgdir.share_state(M) == {"count": {}, "seen_texts": {}}
    state = {"count": {"p1": 2}, "seen_texts": {"p1": ["0" * 64, "1" * 64]}}
    msgdir.save_share_state(M, state)
    assert msgdir.share_state(M) == state
    assert msgdir.list_out(M) == []             # the queue still works in this dir
    # an absent or broken file reads as the fresh default, never a raise
    assert msgdir.share_state(str(tmp_path / "never-init-ed")) \
        == {"count": {}, "seen_texts": {}}
    if os.name == "posix":
        assert os.stat(os.path.join(M, "share-state.json")).st_mode & 0o777 == 0o600
    with open(os.path.join(M, "share-state.json"), "w", encoding="utf-8") as fh:
        fh.write("nonsense")
    assert msgdir.share_state(M) == {"count": {}, "seen_texts": {}}


def test_findings_roundtrip(tmp_path):
    run = str(tmp_path / "run")
    assert msgdir.findings_read(run, "p1") == []        # no file, no folders yet
    first = {"id": "1700000000000000001-aaaa01", "sender": "unit a",
             "role": "probe", "t": 1.0, "text": "first finding", "paths": []}
    second = {"id": "1700000000000000002-bbbb02", "sender": "unit b",
              "role": "fixer", "t": 2.0, "text": "second finding", "paths": ["x.py"]}
    msgdir.findings_append(run, "p1", first)
    path = os.path.join(run, "msg", "_phases", "p1", "findings.jsonl")
    assert os.path.isfile(path)
    if os.name == "posix":
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
    with open(path, "a", encoding="utf-8") as fh:       # a corrupt line in between
        fh.write("{ not json, a torn tail\n")
    msgdir.findings_append(run, "p1", second)
    assert msgdir.findings_read(run, "p1") == [first, second]


# ----------------------------------------------------------------- symlink refusals

@posix_only
def test_symlinked_msgdir_refused(tmp_path):
    # the msg dir itself is a link: refused, and nothing is created through it
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = str(tmp_path / "msg")
    os.symlink(str(real), link)
    with pytest.raises(msgdir.MsgError):
        msgdir.init_msgdir(link)
    assert os.listdir(str(real)) == []
    # a symlinked subfolder is refused too, before anything is created
    M = str(tmp_path / "run" / "msg")
    os.makedirs(M)
    target = tmp_path / "target"
    target.mkdir()
    os.symlink(str(target), os.path.join(M, "replies"))
    with pytest.raises(msgdir.MsgError):
        msgdir.init_msgdir(M)
    assert os.listdir(str(target)) == []
    assert not os.path.exists(os.path.join(M, "out"))


def test_write_out_payload_cannot_override_fixed_fields(tmp_path):
    M = str(tmp_path / "m")
    msgdir.init_msgdir(M)
    nid = msgdir.write_out(M, "share", {"kind": "question", "id": "1-abcdef", "finding": "f"})
    body = msgdir.take_out(M, nid)
    assert body["kind"] == "share" and body["id"] == nid and body["finding"] == "f"
