"""lib/asks.py, stage 1.5 (task 3): the pure "apply a pending amendment" step.

apply_pending claims amend/pending.md by rename onto .applying.md, validates it
and either applies it (Applied: the new k, the normalised text, the parsed Task,
the diff and the checklist counts) or rejects it (Rejected: an enum reason from
REASONS, the bytes moved aside, the reason on the log -- never the parser's
message); checklist_counts / task_diff / coder_block are the pure parts the
coder prompt is built from; leftover_pending moves an amendment the loop never
reached. Every test drives the module in-process (conftest puts
skill/local-auditor on sys.path); the symlink halves are POSIX-only."""
import json
import os
import sys

import pytest

from lib import asks
from lib.taskfile import Item, parse

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="symlinks and the uid check are POSIX")

CURRENT = ("- [ ] value is good -- check: cmd true\n"
           "- [ ] the notes are updated -- check: none\n")
# The prescribed 2 -> 3 amendment: item 1 carried verbatim (equal), item 2 CHANGED
# (a replace with m=1, n=1) and item 3 added (insert) -- (1, 0, 1).
AMENDED = ("- [ ] value is good -- check: cmd true\n"
           "- [ ] the notes are updated and reviewed -- check: none\n"
           "- [ ] cover it with a test -- check: cmd pytest -q tests/test_x.py\n")
# A second amendment pair that REWRITES instead of appending: two items are
# replaced by one (m=2, n=1: one changed, one removed) and one item is inserted,
# so the apply path exercises a replace opcode too.
REWRITE_FROM = ("- [ ] value is good -- check: cmd true\n"
                "- [ ] the notes are updated -- check: none\n"
                "- [ ] the repo is clean -- check: none\n"
                "- [ ] the tests pass -- check: cmd pytest -q\n")
REWRITE_TO = ("- [ ] value is good -- check: cmd true\n"
              "- [ ] the notes are updated and reviewed -- check: none\n"
              "- [ ] the tests pass -- check: cmd pytest -q\n"
              "- [ ] the lints are clean -- check: cmd ruff -q\n")


def make_run(tmp_path, monkeypatch, has_test_cmd=True, name="run"):
    """An initialised run folder: the ambient QWEN_STREAM and QWEN_ASK_LEASE of
    the developer's shell never decide what the test sees."""
    monkeypatch.delenv("QWEN_STREAM", raising=False)
    monkeypatch.delenv("QWEN_ASK_LEASE", raising=False)
    run = tmp_path / name
    assert asks.init_run(str(run), has_test_cmd) is True
    return str(run)


def pending_path(run):
    return os.path.join(run, asks.AMEND_DIR, asks.PENDING_FILE)


def applying_path(run):
    return os.path.join(run, asks.AMEND_DIR, asks.APPLYING_FILE)


def write_pending(run, data):
    """What --amend left in the inbox: bytes verbatim, a str as UTF-8."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    asks.safe_write(pending_path(run), data)


def listed(run, leaf):
    return sorted(os.listdir(os.path.join(run, asks.AMEND_DIR, leaf)))


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def log_records(run):
    try:
        data = read_bytes(os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE))
    except OSError:
        return []
    return [json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()]


def log_raw(run):
    try:
        return read_bytes(os.path.join(run, asks.ASKS_DIR, asks.LOG_FILE))
    except OSError:
        return b""


# ============================================================ apply_pending


def test_apply_none_when_no_pending(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    assert asks.apply_pending(run, CURRENT, 0, True, 1) is None
    # nothing was claimed, nothing landed anywhere, nothing was logged
    assert not os.path.lexists(pending_path(run))
    assert not os.path.lexists(applying_path(run))
    assert listed(run, asks.APPLIED) == []
    assert listed(run, asks.REJECTED) == []
    assert log_records(run) == []
    # a run interrupted mid-apply leaves a stray .applying.md: with no pending
    # there is still nothing to apply, and the stray is left for what it is
    asks.safe_write(applying_path(run), b"stale claim")
    assert asks.apply_pending(run, CURRENT, 0, True, 2) is None
    assert read_bytes(applying_path(run)) == b"stale claim"
    assert log_records(run) == []


def test_apply_valid_writes_applied_and_counts(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    write_pending(run, AMENDED.replace("\n", "\r\n"))    # a Windows-edited file
    res = asks.apply_pending(run, CURRENT, 0, True, 4)
    assert isinstance(res, asks.Applied)
    assert res.k == 1                                     # the applied number is k+1
    assert res.text == AMENDED                            # normalised to LF
    assert res.task.items == parse(AMENDED).items         # the parsed Task
    assert (res.added, res.removed, res.changed) == (1, 0, 1)  # the 2 -> 3 shape
    assert res.diff.startswith("--- task.md (before)\n+++ task.md (amendment 1)\n")
    assert "-- [ ] the notes are updated -- check: none\n" in res.diff
    assert "+- [ ] the notes are updated and reviewed -- check: none\n" in res.diff
    assert ("+- [ ] cover it with a test -- check: cmd pytest -q tests/test_x.py\n"
            in res.diff)
    applied = os.path.join(run, asks.AMEND_DIR, asks.APPLIED, "1.md")
    assert read_bytes(applied).decode("utf-8") == AMENDED  # the normalised bytes
    assert not os.path.lexists(pending_path(run))          # claimed by rename...
    assert not os.path.lexists(applying_path(run))         # ...and the claim is gone
    assert listed(run, asks.REJECTED) == []
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["amend_applied"]
    assert recs[0]["k"] == 1 and recs[0]["round"] == 4
    # non-UTF-8 bytes are decoded leniently (the notes.py -f rule), never refused:
    # the replacement char rides into the applied text as any other character
    write_pending(run, AMENDED.encode("utf-8") + b"# caf\xe9 x\n")
    res2 = asks.apply_pending(run, CURRENT, 1, True, 4)
    assert isinstance(res2, asks.Applied) and res2.k == 2
    assert res2.text == AMENDED + "# caf� x\n"
    applied2 = os.path.join(run, asks.AMEND_DIR, asks.APPLIED, "2.md")
    assert read_bytes(applied2) == (AMENDED + "# caf� x\n").encode("utf-8")
    assert [r["ev"] for r in log_records(run)] == ["amend_applied", "amend_applied"]
    # a current_text that does not parse at all is not a rejection: its checklist
    # is no checklist, so every new item counts as added
    write_pending(run, AMENDED)
    res3 = asks.apply_pending(run, "# Goal\n\nprose only\n", 2, True, 4)
    assert isinstance(res3, asks.Applied) and res3.k == 3
    assert (res3.added, res3.removed, res3.changed) == (3, 0, 0)
    # a rewrite, not an append: one equal item, a replace with m=2, n=1 (two old
    # items paired into one: one changed AND one removed), one equal item, one
    # insert -- and the '-'/'+' prefixes pin which side each rewritten line went
    # to, so a miscounted replace (changed += m/n) cannot hide behind the append
    write_pending(run, REWRITE_TO)
    res4 = asks.apply_pending(run, REWRITE_FROM, 3, True, 4)
    assert isinstance(res4, asks.Applied) and res4.k == 4
    assert (res4.added, res4.removed, res4.changed) == (1, 1, 1)
    assert "+- [ ] the lints are clean -- check: cmd ruff -q\n" in res4.diff
    assert "+- [ ] the notes are updated and reviewed -- check: none\n" in res4.diff
    assert "-- [ ] the repo is clean -- check: none\n" in res4.diff
    assert "-- [ ] the notes are updated -- check: none\n" in res4.diff
    assert read_bytes(os.path.join(run, asks.AMEND_DIR, asks.APPLIED,
                                   "4.md")).decode("utf-8") == REWRITE_TO
    assert [r["ev"] for r in log_records(run)] == ["amend_applied"] * 4


def test_apply_crlf_identical_is_noop(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    raw = CURRENT.replace("\n", "\r\n").encode("utf-8")    # same text, CRLF bytes
    write_pending(run, raw)
    res = asks.apply_pending(run, CURRENT, 0, True, 2)
    assert isinstance(res, asks.Rejected) and res.reason == "noop"
    assert res.reason in asks.REASONS
    assert not os.path.lexists(pending_path(run))
    assert not os.path.lexists(applying_path(run))
    assert listed(run, asks.APPLIED) == []
    files = listed(run, asks.REJECTED)
    assert len(files) == 1 and files[0].endswith(".md")
    moved = os.path.join(run, asks.AMEND_DIR, asks.REJECTED, files[0])
    assert read_bytes(moved) == raw                        # the bytes, verbatim
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["amend_rejected"]
    assert recs[0]["reason"] == "noop" and recs[0]["round"] == 2


def test_apply_parse_error_reason_is_enum(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    bad = "# Goal\n\nprose only, no checklist anywhere\n"
    write_pending(run, bad)
    res = asks.apply_pending(run, CURRENT, 5, True, 1)
    assert isinstance(res, asks.Rejected) and res.reason == "parse_error"
    assert res.reason in asks.REASONS
    assert not os.path.lexists(pending_path(run))
    assert not os.path.lexists(applying_path(run))
    assert listed(run, asks.APPLIED) == []
    assert len(listed(run, asks.REJECTED)) == 1
    # the parser's own message must NOT reach asks/log.jsonl: only the enum
    assert b"no checklist items" not in log_raw(run)
    assert "prose only".encode("utf-8") not in log_raw(run)
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["amend_rejected"]
    assert recs[0]["reason"] == "parse_error" and recs[0]["round"] == 1
    # a second, different parse failure: the same enum, and its message stays
    # out of the log too
    write_pending(run, "- [ ] thing -- check: bogus\n")
    res2 = asks.apply_pending(run, CURRENT, 5, True, 1)
    assert isinstance(res2, asks.Rejected) and res2.reason == "parse_error"
    assert b"unknown check kind" not in log_raw(run)
    assert len(listed(run, asks.REJECTED)) == 2
    # the rejection path is uniform for every REASON: an over-cap body is the
    # same enum move (safe_read's reason rides straight through)
    write_pending(run, b"x" * (asks.AMEND_MAX + 1))
    res3 = asks.apply_pending(run, CURRENT, 5, True, 1)
    assert isinstance(res3, asks.Rejected) and res3.reason == "too_large"
    assert res3.reason in asks.REASONS
    assert not os.path.lexists(applying_path(run))
    assert len(listed(run, asks.REJECTED)) == 3
    assert [r["reason"] for r in log_records(run)] == ["parse_error",
                                                       "parse_error", "too_large"]


def test_apply_test_item_without_cmd(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, has_test_cmd=False)
    with_test = CURRENT + "- [ ] cover it -- check: test pytest -q t.py\n"
    write_pending(run, with_test)
    res = asks.apply_pending(run, CURRENT, 0, False, 7)
    assert isinstance(res, asks.Rejected) and res.reason == "test_without_cmd"
    assert res.reason in asks.REASONS
    assert not os.path.lexists(pending_path(run))
    assert not os.path.lexists(applying_path(run))
    assert listed(run, asks.APPLIED) == []
    assert len(listed(run, asks.REJECTED)) == 1
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["amend_rejected"]
    assert recs[0]["reason"] == "test_without_cmd" and recs[0]["round"] == 7
    # the very same amendment applies once the run HAS a test command
    write_pending(run, with_test)
    res2 = asks.apply_pending(run, CURRENT, 0, True, 7)
    assert isinstance(res2, asks.Applied) and res2.k == 1
    assert (res2.added, res2.removed, res2.changed) == (1, 0, 0)


@posix_only
def test_apply_symlinked_pending_is_rejected(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text(AMENDED, encoding="utf-8")
    os.symlink(str(outside), pending_path(run))
    res = asks.apply_pending(run, CURRENT, 0, True, 1)
    assert isinstance(res, asks.Rejected) and res.reason == "symlink"
    assert res.reason in asks.REASONS
    assert not os.path.lexists(pending_path(run))          # the link is unlinked...
    assert not os.path.lexists(applying_path(run))
    assert outside.read_text(encoding="utf-8") == AMENDED  # ...the target untouched
    assert listed(run, asks.APPLIED) == []
    assert listed(run, asks.REJECTED) == []                # a link is not filed
    recs = log_records(run)
    assert [r["ev"] for r in recs] == ["amend_rejected"]
    assert recs[0]["reason"] == "symlink" and recs[0]["round"] == 1


# ================================================== counts, diff, coder block


def test_checklist_counts_opcodes():
    a, b, c = Item(1, "alpha", "none", ""), Item(2, "beta", "none", ""), \
        Item(3, "gamma", "none", "")
    # a pure insert: one item added after an equal prefix
    assert asks.checklist_counts([a], [a, b]) == (1, 0, 0)
    # a pure delete: one item dropped, the rest equal
    assert asks.checklist_counts([a, b], [a]) == (0, 1, 0)
    # a replace with m=1, n=3: min(1, 3) paired as changed, the surplus added
    assert asks.checklist_counts([a], [b, c, Item(3, "delta", "none", "")]) == (2, 0, 1)
    # and the mirror: m=3, n=1 gives the surplus to removed
    assert asks.checklist_counts([b, c, Item(3, "delta", "none", "")], [a]) == (0, 2, 1)
    # a like-for-like replace (m=n=1): changed, with nothing added or removed
    assert asks.checklist_counts([a, b], [a, Item(2, "beta", "cmd", "run")]) == (0, 0, 1)
    # a replace with m=2, n=3: two paired, one added
    assert asks.checklist_counts([a, b], [c, Item(2, "beta2", "none", ""),
                                          Item(3, "gamma2", "none", "")]) == (1, 0, 2)
    # untouched checklists count nothing, and an empty side is all one kind
    assert asks.checklist_counts([a, b], [a, b]) == (0, 0, 0)
    assert asks.checklist_counts([], [a, b]) == (2, 0, 0)
    assert asks.checklist_counts([a, b], []) == (0, 2, 0)
    # the comparison is (kind, arg, text): renumbering the same items changes no
    # checklist (the index is positional, so it never counts)
    assert asks.checklist_counts([a, b], [Item(7, "alpha", "none", ""),
                                          Item(8, "beta", "none", "")]) == (0, 0, 0)


def test_coder_block_capped_at_60_lines():
    diff = "".join("line %d\n" % i for i in range(100))
    block = asks.coder_block(7, diff)
    lines = block.splitlines()
    assert lines[0] == "The main session changed the task's checklist (amendment 7):"
    assert lines[1] == "line 0"
    assert lines[asks.DIFF_LINES] == "line %d" % (asks.DIFF_LINES - 1)  # "line 59"
    assert lines[asks.DIFF_LINES + 1] == "... (diff cut at 60 lines)"   # the marker
    assert len(lines) == asks.DIFF_LINES + 2                            # cut, not buried
    assert "line 60" not in lines[1:]
    # exactly DIFF_LINES diff lines: nothing is cut and no marker is claimed
    exact = "".join("line %d\n" % i for i in range(asks.DIFF_LINES))
    block2 = asks.coder_block(1, exact)
    assert block2.splitlines()[1:] == ["line %d" % i for i in range(asks.DIFF_LINES)]
    assert "cut" not in block2
    # an empty diff leaves just the header line
    assert asks.coder_block(2, "") == \
        "The main session changed the task's checklist (amendment 2):\n"
    # task_diff: the two headers and n=2 context; the k= label when given
    d = asks.task_diff("a\nb\n", "a\nc\n")
    assert d.startswith("--- task.md (before)\n+++ task.md (amendment k)\n")
    assert d.splitlines()[2] == "@@ -1,2 +1,2 @@"
    d4 = asks.task_diff("a\nb\n", "a\nc\n", k=4)
    assert d4.startswith("--- task.md (before)\n+++ task.md (amendment 4)\n")


@posix_only
def test_leftover_pending_moved_to_unapplied(tmp_path, monkeypatch):
    """posix_only: this test plants a symlinked pending.md, so the whole of it
    skips off POSIX (the spec's symlink rule: marked, never inline-guarded)."""
    run = make_run(tmp_path, monkeypatch)
    assert asks.leftover_pending(run) is None              # nothing to move
    write_pending(run, AMENDED)
    dest = asks.leftover_pending(run)
    assert dest == os.path.join(run, asks.AMEND_DIR, asks.UNAPPLIED_FILE)
    assert not os.path.lexists(pending_path(run))
    assert read_bytes(dest).decode("utf-8") == AMENDED     # whole, verbatim
    assert log_records(run) == []                          # housekeeping logs nothing
    assert asks.leftover_pending(run) is None              # and again: still nothing
    # a run with no amend/ at all is None, never a raise
    assert asks.leftover_pending(str(tmp_path / "no-such-run")) is None
    # a symlinked pending is unlinked, and the target keeps its bytes
    outside = tmp_path / "outside.md"
    outside.write_text(AMENDED, encoding="utf-8")
    os.symlink(str(outside), pending_path(run))
    assert asks.leftover_pending(run) is None
    assert not os.path.lexists(pending_path(run))
    assert outside.read_text(encoding="utf-8") == AMENDED
    # a run folder under a path with spaces moves the same way (the whole
    # housekeeping happens through plain os paths, nothing shell-shaped)
    spaced = make_run(tmp_path, monkeypatch, name="a b run")
    write_pending(spaced, CURRENT)
    dest2 = asks.leftover_pending(spaced)
    assert dest2 == os.path.join(spaced, asks.AMEND_DIR, asks.UNAPPLIED_FILE)
    assert read_bytes(dest2).decode("utf-8") == CURRENT
