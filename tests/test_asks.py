"""lib/asks.py, stage 1.5: the run-folder layout, safe IO, the lease, the ask
text, the claim and the asks log.

Every test drives the module in-process (conftest puts skill/local-auditor on
sys.path); nothing here spawns a child or touches the watch dir. The symlink and
uid halves are POSIX-only and skip on Windows."""
import json
import os
import sys

import pytest

from lib import asks

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="symlinks and the uid check are POSIX")

RUN_ID = "a1b2c3d4e5f6"          # a watch id as watch.ID_RE demands: 12 lowercase hex

ASK_KWARGS = dict(
    n=3, max_rounds=10, title="Fix the flaky parse", run_id=RUN_ID,
    repo="/work/repo", probe=False, task_path="/work/repo/task.md",
    next_started="21:04:07", start="8c348e8",
    diffstat=" lib/p.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)",
    fails="test_parse: AssertionError: nope")


def make_run(tmp_path, monkeypatch, has_test_cmd=True):
    """An initialised run folder with messaging on (the ambient QWEN_STREAM and
    QWEN_ASK_LEASE of the developer's shell never decide what the test sees)."""
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    run = tmp_path / "run"
    assert asks.init_run(str(run), has_test_cmd) is True
    return str(run)


# ----------------------------------------------------------------- the layout

def test_init_run_layout_and_modes(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    for rel in (asks.ASKS_DIR, asks.AMEND_DIR,
                os.path.join(asks.ASKS_DIR, asks.NEW),
                os.path.join(asks.ASKS_DIR, asks.CLAIMED),
                os.path.join(asks.AMEND_DIR, asks.APPLIED),
                os.path.join(asks.AMEND_DIR, asks.REJECTED)):
        assert os.path.isdir(os.path.join(run, rel)), rel
    meta = os.path.join(run, asks.AMEND_DIR, asks.META_FILE)
    with open(meta, "rb") as fh:
        assert json.loads(fh.read()) == {"has_test_cmd": True}
    # idempotent: a second init (a resumed loop) rewrites only the meta
    assert asks.init_run(run, False) is True
    with open(meta, "rb") as fh:
        assert json.loads(fh.read()) == {"has_test_cmd": False}
    if os.name == "posix":
        for rel in (asks.ASKS_DIR, asks.AMEND_DIR,
                    os.path.join(asks.ASKS_DIR, asks.NEW),
                    os.path.join(asks.ASKS_DIR, asks.CLAIMED),
                    os.path.join(asks.AMEND_DIR, asks.APPLIED),
                    os.path.join(asks.AMEND_DIR, asks.REJECTED)):
            assert os.stat(os.path.join(run, rel)).st_mode & 0o777 == 0o700, rel
        assert os.stat(meta).st_mode & 0o777 == 0o600
    # the layout is never the loop's problem: a blocked folder reads as "no
    # asks here" (False), it does not raise -- and nothing is half-built
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / asks.ASKS_DIR).write_text("not a directory", encoding="utf-8")
    assert asks.init_run(str(blocked), True) is False
    assert not (blocked / asks.AMEND_DIR).exists()


def test_init_run_stream_off_makes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_STREAM", "0")          # the no-messaging baseline (P5)
    run = tmp_path / "run"
    assert asks.init_run(str(run), True) is False
    assert not run.exists()                          # not even the run folder itself


# ----------------------------------------------------------------- the lease

def test_lease_live_and_expired(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    assert asks.read_lease(run) is None              # nothing written yet
    assert asks.lease_live(run) is False
    asks.write_lease(run, 4242)
    rec = asks.read_lease(run)
    assert isinstance(rec, dict) and set(rec) == {"pid", "until", "t"}
    assert rec["pid"] == 4242 and isinstance(rec["t"], float)
    assert rec["until"] == pytest.approx(rec["t"] + asks.LEASE_DEFAULT, abs=0.01)
    # landed by rename, no temp file left behind in the asks directory
    assert sorted(os.listdir(os.path.join(run, asks.ASKS_DIR))) == ["claimed",
                                                                    asks.LEASE_FILE,
                                                                    "new"]
    assert asks.lease_live(run) is True
    assert asks.lease_live(run, now=rec["until"]) is True    # boundary: still live
    assert asks.lease_live(run, now=rec["until"] + 0.001) is False
    asks.delete_lease(run, only_pid=99)              # another pid's name: it stays
    assert asks.read_lease(run) is not None
    asks.delete_lease(run, only_pid=4242)            # the owner's own: gone
    assert not os.path.exists(os.path.join(run, asks.ASKS_DIR, asks.LEASE_FILE))
    assert asks.lease_live(run) is False
    asks.delete_lease(run)                           # missing file is fine
    asks.delete_lease(run, only_pid=4242)            # ... and a named pid's too
    # a lease.json that is not a JSON object reads as no lease at all
    with open(os.path.join(run, asks.ASKS_DIR, asks.LEASE_FILE), "wb") as fh:
        fh.write(b"[1, 2]")
    assert asks.read_lease(run) is None
    assert asks.lease_live(run) is False


def test_lease_seconds_env(monkeypatch, capsys):
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    assert asks.LEASE_DEFAULT == 600
    assert asks.lease_seconds() == 600
    assert capsys.readouterr().err == ""              # unset: no warning at all
    monkeypatch.setenv("QWEN_ASK_LEASE", "900")
    assert asks.lease_seconds() == 900
    assert capsys.readouterr().err == ""              # a good value: no warning
    monkeypatch.setenv("QWEN_ASK_LEASE", "60")        # the bounds are inclusive
    assert asks.lease_seconds() == 60
    monkeypatch.setenv("QWEN_ASK_LEASE", "3600")
    assert asks.lease_seconds() == 3600
    for bad in ("0", "59", "3601", "10.5", "abc", ""):
        monkeypatch.setenv("QWEN_ASK_LEASE", bad)
        assert asks.lease_seconds() == 600, bad
        err = capsys.readouterr().err
        assert "QWEN_ASK_LEASE" in err
        assert err.count("\n") == 1                   # exactly ONE line per call


# ----------------------------------------------------------------- claims

def test_claim_oldest_is_numeric(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    new = os.path.join(run, asks.ASKS_DIR, asks.NEW)
    claimed = os.path.join(run, asks.ASKS_DIR, asks.CLAIMED)
    path2 = asks.write_ask(run, 2, "two")
    assert path2 == os.path.join(new, "2.md") and os.path.isfile(path2)
    asks.write_ask(run, 10, "ten")
    asks.write_ask(run, 3, "three")
    # "2.md.999.0.tmp" is the tail of a safe_write interrupted before its
    # replace: never claim material, and never touched
    for junk in ("0.md", "01.md", "x.md", "4.txt", "5.tmp", "2.md.999.0.tmp"):
        with open(os.path.join(new, junk), "wb") as fh:
            fh.write(b"junk")
    first = asks.claim_oldest(run)
    assert first == os.path.join(claimed, "2.md")     # the claimed path...
    assert os.path.isfile(first)                      # ...now in claimed/
    assert not os.path.exists(path2)                  # ...and gone from new/
    assert asks.claim_oldest(run) == os.path.join(claimed, "3.md")
    assert asks.claim_oldest(run) == os.path.join(claimed, "10.md")   # numeric order
    assert asks.claim_oldest(run) is None             # only junk is left
    assert sorted(os.listdir(new)) == ["0.md", "01.md", "2.md.999.0.tmp",
                                       "4.txt", "5.tmp", "x.md"]
    assert sorted(os.listdir(claimed)) == ["10.md", "2.md", "3.md"]
    # a source that vanished (the race against another claimer) moves on: 7 is
    # the oldest candidate, but it is gone before the replace, so 8 lands
    asks.write_ask(run, 7, "seven")
    asks.write_ask(run, 8, "eight")
    os.remove(os.path.join(new, "7.md"))
    assert asks.claim_oldest(run) == os.path.join(claimed, "8.md")
    # a run with no asks/ directory at all claims nothing, never raises
    assert asks.claim_oldest(str(tmp_path / "no-such-run")) is None
    # a relative run dir still yields an ABSOLUTE claimed path (spec 10.5)
    monkeypatch.chdir(str(tmp_path))
    asks.write_ask("run", 5, "five")
    got = asks.claim_oldest("run")
    assert os.path.isabs(got)
    assert got == os.path.join(str(tmp_path), "run", asks.ASKS_DIR,
                               asks.CLAIMED, "5.md")


# ----------------------------------------------------------------- the ask text

def test_ask_text_layout():
    text = asks.ask_text(**ASK_KWARGS)
    assert text.endswith("\n") and "\r" not in text
    lines = text.splitlines()
    assert lines[0] == "# Ask 3 for Fix the flaky parse (run %s)" % RUN_ID
    assert lines[1].startswith("repo: ")
    i_task = lines.index("task: /work/repo/task.md")
    i_round = lines.index("round: 3 of 10")
    i_next = lines.index("next round: started at 21:04:07")
    i_start = lines.index("start commit: 8c348e8")
    i_files = lines.index("changed files:")
    i_fails = lines.index("## Failing checks")
    i_answer = lines.index("## How to answer")
    assert 1 < i_task < i_round < i_next < i_start < i_files < i_fails < i_answer
    assert lines[i_files + 1] == " lib/p.py | 2 +-"           # diffstat follows
    assert lines[i_files + 2] == " 1 file changed, 1 insertion(+), 1 deletion(-)"
    assert lines[i_fails + 1] == "test_parse: AssertionError: nope"
    assert lines[i_answer + 1] == "qwen-agent --send %s -f - <<'EOF'" % RUN_ID
    assert lines[i_answer + 2].strip()                        # its explanation
    assert lines[i_answer + 3] == "qwen-agent --amend %s -f - <<'EOF'" % RUN_ID
    assert lines[i_answer + 4].strip()                        # its explanation
    assert len(text.encode("utf-8")) <= asks.ASK_MAX


def test_ask_text_cap_keeps_header_and_answer():
    text = asks.ask_text(**dict(ASK_KWARGS, fails="x" * (4 * asks.ASK_MAX)))
    assert len(text.encode("utf-8")) <= asks.ASK_MAX          # 16 KiB whole
    assert text.startswith("# Ask 3 for Fix the flaky parse (run %s)\n" % RUN_ID)
    for needle in ("repo: /work/repo", "## Failing checks", "## How to answer",
                   "qwen-agent --send %s -f - <<'EOF'" % RUN_ID,
                   "qwen-agent --amend %s -f - <<'EOF'" % RUN_ID):
        assert needle in text
    # a fails longer than FAILS_MAX is cut to FAILS_MAX chars first (and the
    # whole still fits, so that cut is the only one that applied)
    text2 = asks.ask_text(**dict(ASK_KWARGS, fails="y" * (asks.FAILS_MAX + 500)))
    body = text2.split("## Failing checks\n", 1)[1].split("\n\n## How to answer", 1)[0]
    assert body == "y" * asks.FAILS_MAX
    # the cap counts UTF-8 BYTES, not characters: 6000 three-byte characters are
    # already past ASK_MAX at the FAILS_MAX cut, so the further cut must fit bytes
    text3 = asks.ask_text(**dict(ASK_KWARGS, fails="你" * (4 * asks.ASK_MAX)))
    assert len(text3.encode("utf-8")) <= asks.ASK_MAX
    assert text3.startswith("# Ask 3 for Fix the flaky parse (run %s)\n" % RUN_ID)
    assert "## How to answer" in text3 and "qwen-agent --amend" in text3
    # the cap is absolute: a single header field bigger than ASK_MAX cannot
    # smuggle an over-cap ask out -- that field is cut, the structure survives
    fat = asks.ask_text(n=1, max_rounds=1, title="x" * 20000, run_id=RUN_ID,
                        repo="r", probe=False, task_path="t", next_started="0",
                        start="s", diffstat="", fails="f")
    assert len(fat.encode("utf-8")) <= asks.ASK_MAX
    assert fat.startswith("# Ask 1 for ")
    assert "## Failing checks" in fat and "## How to answer" in fat
    assert "qwen-agent --send %s -f - <<'EOF'" % RUN_ID in fat
    assert "repo: r" in fat.splitlines()             # the short fields stayed whole
    for huge in ("repo", "diffstat", "task_path", "next_started", "start"):
        fat2 = asks.ask_text(**dict(ASK_KWARGS, **{huge: "z" * (4 * asks.ASK_MAX)}))
        assert len(fat2.encode("utf-8")) <= asks.ASK_MAX, huge
        assert "## How to answer" in fat2, huge
        assert "qwen-agent --amend %s -f - <<'EOF'" % RUN_ID in fat2, huge
    # an oversized diffstat is what gives: title and round facts stay untouched
    fat3 = asks.ask_text(**dict(ASK_KWARGS, diffstat="d" * (4 * asks.ASK_MAX)))
    assert fat3.splitlines()[0] == "# Ask 3 for Fix the flaky parse (run %s)" % RUN_ID
    assert "round: 3 of 10" in fat3.splitlines()


def test_ask_text_probe_label():
    on = asks.ask_text(**dict(ASK_KWARGS, probe=True))
    assert "repo: /work/repo (probe sandbox: read only)" in on.splitlines()
    off = asks.ask_text(**dict(ASK_KWARGS, probe=False))
    assert "repo: /work/repo" in off.splitlines()
    assert "probe sandbox" not in off


# ----------------------------------------------------------------- safe IO

@posix_only
def test_safe_write_refuses_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    link = tmp_path / "link.txt"
    os.symlink(str(target), str(link))
    with pytest.raises(asks.AsksError) as e:
        asks.safe_write(str(link), b"new")
    assert e.value.reason == "symlink" and isinstance(e.value.code, int)
    assert target.read_bytes() == b"old"              # nothing went through the link
    real = tmp_path / "real"
    real.mkdir()
    dir_link = tmp_path / "dirlink"
    os.symlink(str(real), str(dir_link))
    with pytest.raises(asks.AsksError) as e2:
        asks.safe_write(str(dir_link / "x.json"), b"new")
    assert e2.value.reason == "symlink"
    assert os.listdir(str(real)) == []                # and nothing landed beside it
    # an honest write lands by rename at 0600, leaving no temp file behind
    plain = tmp_path / "plain.json"
    asks.safe_write(str(plain), b'{"a": 1}')
    assert plain.read_bytes() == b'{"a": 1}'
    assert os.stat(str(plain)).st_mode & 0o777 == 0o600
    # a path with spaces and a non-UTF-8 body are bytes in, bytes out
    spaced = tmp_path / "a b.md"
    asks.safe_write(str(spaced), b"bo\xff\xfddy")
    assert asks.safe_read(str(spaced), 16) == b"bo\xff\xfddy"
    assert sorted(os.listdir(str(tmp_path))) == ["a b.md", "dirlink", "link.txt",
                                                 "plain.json", "real", "target.txt"]


@posix_only
def test_safe_read_refusals(tmp_path):
    good = tmp_path / "g.bin"
    good.write_bytes(b"hello")
    assert asks.safe_read(str(good), 16) == b"hello"
    assert asks.safe_read(str(good), 5) == b"hello"   # exactly the limit is fine
    with pytest.raises(asks.AsksError) as e:
        asks.safe_read(str(tmp_path / "missing"), 16)
    assert e.value.reason == "read_error"
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * 100)
    with pytest.raises(asks.AsksError) as e:
        asks.safe_read(str(big), 99)
    assert e.value.reason == "too_large"
    link = tmp_path / "l.bin"
    os.symlink(str(good), str(link))
    with pytest.raises(asks.AsksError) as e:
        asks.safe_read(str(link), 16)
    assert e.value.reason == "symlink"
    other = tmp_path / "other.bin"
    other.write_bytes(b"x")
    if os.geteuid() != 0:
        pytest.skip("planting a foreign-uid file needs root")
    os.chown(str(other), 1, -1)                       # owned by uid 1, not us
    with pytest.raises(asks.AsksError) as e:
        asks.safe_read(str(other), 16)
    assert e.value.reason == "uid"


# ----------------------------------------------------------------- the log

def test_log_event_line_size(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    path = os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE)
    asks.log_event(run, "ask_written", n=7, note="x" * (4 * asks.LOG_MAX))
    with open(path, "rb") as fh:
        data = fh.read()
    raw_lines = data.splitlines(keepends=True)
    assert len(raw_lines) == 1
    assert all(len(ln) < asks.LOG_MAX for ln in raw_lines)
    rec = json.loads(raw_lines[0].decode("utf-8"))
    assert rec["ev"] == "ask_written" and isinstance(rec["t"], float)
    assert rec["n"] == 7                              # the small field survives...
    assert "note" not in rec                          # ...the big one is dropped
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600
    asks.log_event(run, "lease_refreshed", pid=4242)  # append, never overwrite
    with open(path, "rb") as fh:
        recs = [json.loads(ln) for ln in fh.read().decode("utf-8").splitlines()]
    assert [r["ev"] for r in recs] == ["ask_written", "lease_refreshed"]
    assert recs[1]["pid"] == 4242
    # an ev so long that even the bare {"t","ev"} line would reach the cap is
    # truncated, never dropped: the event lands, the line still stays under it
    asks.log_event(run, "e" * (4 * asks.LOG_MAX))
    with open(path, "rb") as fh:
        raw = fh.read()
    raw_lines = raw.splitlines(keepends=True)
    assert len(raw_lines) == 3                     # appended, not refused
    assert all(len(ln) < asks.LOG_MAX for ln in raw_lines)
    third = json.loads(raw_lines[2].decode("utf-8"))
    assert third["ev"].startswith("e" * 100)       # truncated prefix of the ev...
    assert len(third["ev"]) < 4 * asks.LOG_MAX     # ...not the whole thing
    assert isinstance(third["t"], float)            # and the stamp is intact


@posix_only
def test_init_run_symlinked_layout_returns_false(tmp_path):
    # a bool, never a raise: the loop's contract is init_run -> True/False
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside"
    os.symlink(str(outside), str(run / asks.ASKS_DIR))
    assert asks.init_run(str(run), True) is False
    assert not outside.exists()                    # nothing was built through the link
    # a planted meta.json link: the write is refused, init_run still only returns
    run2 = tmp_path / "run2"
    assert asks.init_run(str(run2), True) is True
    meta = str(run2 / asks.AMEND_DIR / asks.META_FILE)
    kept = tmp_path / "kept.json"
    kept.write_bytes(b'{"has_test_cmd": true}')
    os.unlink(meta)
    os.symlink(str(kept), meta)
    assert asks.init_run(str(run2), False) is False
    assert kept.read_bytes() == b'{"has_test_cmd": true}'


@posix_only
def test_log_event_symlinked_asks_dir_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    elsewhere = tmp_path / "elsewhere"
    os.symlink(str(elsewhere), str(run / asks.ASKS_DIR))
    asks.log_event(str(run), "anything")              # silent refusal, no raise
    assert not elsewhere.exists()
