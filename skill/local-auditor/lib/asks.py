"""Asks for running until-done loops: the run-folder layout, safe IO, the lease,
the ask text, the claim and the asks log (spec 10).

An until-done round that fails in the same way twice asks the MAIN session instead
of flailing on: the loop writes an ask, the main session answers with a note
(``qwen-agent --send``) or a task amendment (``qwen-agent --amend``). This module
owns everything ask-shaped under the run folder, and the sender-side CLI the main
session runs (``main`` with the ``next-ask`` and ``amend`` modes, the twin of
lib/notes.py's ``send``/``notes``), and the pure apply step the loop calls before a
check run (``apply_pending``: claim ``amend/pending.md``, validate, apply or reject
with an enum reason; ``leftover_pending`` moves an amendment nobody reached at run
end); the rest of the loop side -- the supervisor writing asks and the wiring that
calls these -- comes later.

Run-folder layout (spec 4.1 and 11.3)::

    <RUN>/asks/new/<n>.md       the failing-round asks, written by the loop
    <RUN>/asks/new/q<n>.md      an agent's ask_main question, admitted by the relay
    <RUN>/asks/claimed/<n>.md   the ask one answerer holds (claimed by rename)
    <RUN>/asks/lease.json       {"pid","until","t"}: who may ask right now
    <RUN>/asks/log.jsonl        the asks event log, one JSON line per event
    <RUN>/asks/questions.json   {"admitted": N}: the run's question counter
    <RUN>/asks/.admit.lock      admission's O_EXCL lock (transient, 30 s stale)
    <RUN>/amend/pending.md      the amendment --amend queued for the loop
    <RUN>/amend/.applying.md    the amendment mid-claim by apply_pending (transient)
    <RUN>/amend/applied/        amendments the loop applied
    <RUN>/amend/rejected/       amendments the loop rejected, one <time_ns>.md per rejection
    <RUN>/amend/unapplied.md    a pending.md left over when the run ended
    <RUN>/amend/meta.json       {"has_test_cmd": <bool>}
    <RUN>/asks/.ended           the supervisor's end-of-run sentinel

The sender CLI (spec 10.2): ``python lib/asks.py next-ask ID [--wait SECONDS]
[--release]`` stamps the lease and polls once a second -- the claimed ask's
absolute path on stdout (exit 0, logged ``claimed``), ``no ask yet`` and 10 when
the wait runs out or the lease is released under us, ``run ended`` and 11 when the
loop has ended (the check runs BEFORE the first lease stamp, and a SIGTERM/SIGINT/
SIGHUP gives our own lease up); --release deletes the lease and exits 0, always.
``python lib/asks.py amend ID -f FILE|-f -`` parses the corrected task file with
lib/taskfile.py and queues it at amend/pending.md (``amend queued``, 0): 2 for a
parse error (an empty checklist is one), an over-AMEND_MAX body, a wrong grammar
or a test item in a run with no test command; 3 for an ended run, a closed inbox
or a missing amend folder. The ask's text is never printed. qwen-agent.sh
dispatches --next-ask/--amend here and exports QWEN_ASK_LEASE for the dispatch.
--next-ask (and --release) resolve their run through ``resolve_asks_run``: an
until-done loop or a swarm/research run; --amend keeps ``resolve_until_done``
(until-done only). Admission (``admit_question``, the relays' side, spec
11.3) is the only writer of ``q<n>.md``, the counter and the lock.

Directories are 0700 and files 0600 on POSIX, like lib/notes.py; every write goes
through safe_write (a unique temp name in the destination's own directory, pid plus
a per-process counter, then os.replace, retried on PermissionError the way
notes.move_note retries on a still-open Windows handle). Reads refuse anything
symlinked, foreign-owned or oversized -- the reasons a later CLI maps onto exit
codes -- so a planted file can never trick the loop into reading or writing
somewhere else. The file-name grammar for an ask is ASK_RE: 1.md, 2.md, ... (the
failing-round asks) and q1.md, q2.md, ... (the question asks an agent's ask_main
question becomes once admitted), either with no leading zeros; each kind queues in
its own numeric order and the two queues claim by file mtime (the older head first,
a tie to the round ask), and a source that vanished mid-claim (the race against
another claimer) moves on to the next candidate.

Safety rules, inherited from notes.py and enforced here rather than assumed:
writes refuse a symlinked path or a symlinked parent directory (reason "symlink"),
reads open with O_NOFOLLOW where the platform has it, check the fstat uid on
posix (reason "uid") and the size against a limit (reason "too_large"); any other
OSError on the read is "read_error". REASONS is the closed set of reasons the
later layers name.

The lease (spec 10.3) is the loop's "I am still asking on purpose" stamp:
write_lease stamps asks/lease.json with the owner's pid and an expiry
``now + lease_seconds()``; lease_seconds reads QWEN_ASK_LEASE (an int in
LEASE_MIN..LEASE_MAX, else the default with ONE stderr line naming the variable,
and no warning at all when the variable is unset). A lease is live while
``now <= until``; delete_lease may name the pid it must still match, so an
answerer that woke after its own expiry never deletes the next owner's lease.

The ask text (spec 4.3) is produced whole by ask_text and must stay under ASK_MAX
UTF-8 bytes: when it would not, the failing-checks section is cut further and the
header and the "How to answer" block survive; a header FIELD that alone exceeds
the cap is itself cut (longest free-text field first), because the cap is hard.
log_event appends one JSON line to
asks/log.jsonl with ONE os.write on an append fd; a line is kept under LOG_MAX
bytes by dropping fields (biggest first) and, if the ev alone would reach the
cap, by truncating the ev -- every event lands, and a symlinked asks/ directory
is refused in silence; log_event NEVER raises, so a missing or blocked asks/
folder is refused the same way -- the log is never a reason to fail the round
that wrote it.
"""
from __future__ import annotations

import contextlib
import difflib
import itertools
import json
import os
import re
import signal
import sys
import threading
import time
from typing import NamedTuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import msgdir, taskfile  # noqa: E402
from lib.notes import (DIR_MODE, FILE_MODE, _is_link, _pointer,  # noqa: E402
                       _strip_controls, pid_alive)

# ----------------------------------------------------------------- constants
LEASE_DEFAULT = 600        # seconds a lease holds without a refresh
LEASE_MIN = 60             # QWEN_ASK_LEASE bounds ...
LEASE_MAX = 3600           # ... inclusive
REFRESH = 30               # seconds between lease refreshes of a live ask
ASK_MAX = 16 * 1024        # UTF-8 bytes of one ask file, whole
FAILS_MAX = 6000           # characters of the failing-checks section
AMEND_MAX = 256 * 1024     # UTF-8 bytes of one amendment body
LOG_MAX = 512              # bytes of one asks log line, newline included
DIFF_LINES = 60            # diffstat lines shown before the tail is cut
ASK_RE = re.compile(r"^(?:[1-9][0-9]*|q[1-9][0-9]*)\.md$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")   # the plain watch id -- no /<unit> here
QUESTION_PREFIX = "q"                       # a question ask is q<seq>.md (11.3)
ASKS_KINDS = ("until-done", "swarm", "research")   # pointers that hold asks
REASONS = ("noop", "parse_error", "test_without_cmd", "symlink", "uid",
           "read_error", "too_large")

ASKS_DIR = "asks"
AMEND_DIR = "amend"
NEW, CLAIMED = "new", "claimed"
APPLIED, REJECTED = "applied", "rejected"
LEASE_FILE = "lease.json"
LOG_FILE = "log.jsonl"
META_FILE = "meta.json"
PENDING_FILE = "pending.md"     # the amendment --amend queues for the loop
APPLYING_FILE = ".applying.md"  # the amendment mid-claim by apply_pending (transient)
UNAPPLIED_FILE = "unapplied.md"  # a pending.md left at run end, moved aside whole
INBOX_DIR = "inbox"             # the loop's inbox: its .closed ends the run for us
CLOSED_SENTINEL = ".closed"
ENDED_FILE = ".ended"           # the supervisor's end-of-run sentinel under RUN/asks
QUESTIONS_FILE = "questions.json"   # {"admitted": N}: the run's question counter
ADMIT_LOCK_FILE = ".admit.lock"     # admission's O_EXCL lock (transient)
ADMIT_LOCK_STALE = 30.0         # seconds before an .admit.lock is taken over
ADMIT_LOCK_POLL = 0.05          # seconds between lock retries
ADMIT_LOCK_GIVEUP = 35.0        # seconds, then the lock is refused (AsksError)
ASK_MAIN_MAX_DEFAULT = 5        # QWEN_ASK_MAIN_MAX default: questions per run
ASK_MAIN_MAX_BOUND = 20         # ... and its clamp, 0..20
_STATE_MAX = 4096               # a questions.json we will even read

POLL = 1                 # seconds between --next-ask's polls of the ask queue
WAIT_MIN, WAIT_MAX = 1, 3600    # --wait SECONDS bounds, like notes' --wait
WAIT_DEFAULT = 3600      # the --next-ask wait when no --wait is given

_EXIT_USAGE = 2          # the notes.py exit-code convention this module joins
_EXIT_UNAVAILABLE = 3
EXIT_NO_ASK = 10         # --next-ask: the wait ran out, or the lease went away
EXIT_RUN_ENDED = 11      # --next-ask: the run ended while we waited (or before)
_LEASE_MAX_BYTES = 4096    # a lease.json we cannot parse in full is not a lease

_TMP_SEQ = itertools.count()


class AsksError(Exception):
    """A refusal the later layers turn into an exit code and a log reason:
    `code` is the exit code, `reason` one of REASONS (or None for a plain
    "it did not work" failure that has no rejection reason of its own)."""

    def __init__(self, message, code=_EXIT_UNAVAILABLE, reason=None):
        super().__init__(message)
        self.code = code
        self.reason = reason


def _now_t():
    """The one stamp format (the notes.py convention): a Unix epoch float
    rounded to 3 decimals."""
    return round(time.time(), 3)


def _chmod_dir(path):
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, DIR_MODE)


# ----------------------------------------------------------------- the layout
def init_run(run_dir, has_test_cmd):
    """Create the ask layout under RUN and write the amendment meta. Returns
    False and creates nothing when QWEN_STREAM is "0" (the no-messaging baseline,
    P5); returns False -- never raises -- when the folders cannot be built or
    any part of the layout is a symlink (a planted link is refused, not walked)."""
    if os.environ.get("QWEN_STREAM", "1") == "0":
        return False
    try:
        for top in (ASKS_DIR, AMEND_DIR):
            top_path = os.path.join(run_dir, top)
            if _is_link(top_path):
                raise AsksError("asks folder is a symlink: %s" % top_path,
                                reason="symlink")
            os.makedirs(top_path, mode=DIR_MODE, exist_ok=True)
            _chmod_dir(top_path)
            for leaf in ((NEW, CLAIMED) if top == ASKS_DIR else (APPLIED, REJECTED)):
                path = os.path.join(top_path, leaf)
                if _is_link(path):
                    raise AsksError("asks folder is a symlink: %s" % path,
                                    reason="symlink")
                os.makedirs(path, mode=DIR_MODE, exist_ok=True)
                _chmod_dir(path)
        safe_write(os.path.join(run_dir, AMEND_DIR, META_FILE),
                   json.dumps({"has_test_cmd": bool(has_test_cmd)}).encode("utf-8"))
        return True
    except (OSError, AsksError):
        # the contract is bool only: an unusable layout (blocked, unreadable or
        # symlinked anywhere in it) is False, never a raise, never a half-built run
        return False


def init_asks_only(run_dir) -> bool:
    """Create the ASKS half of the layout only -- asks/, asks/new/, asks/claimed/
    at 0700 with the same symlink refusal as init_run, no amend/ and no meta --
    for a swarm or research run, whose questions land here but which has no
    task-file amendment. False when it cannot build or any part is a symlink,
    and False when QWEN_STREAM is "0" (the no-messaging baseline, P5)."""
    if os.environ.get("QWEN_STREAM", "1") == "0":
        return False
    try:
        top = os.path.join(run_dir, ASKS_DIR)
        for path in (top, os.path.join(top, NEW), os.path.join(top, CLAIMED)):
            if _is_link(path):
                raise AsksError("asks folder is a symlink: %s" % path,
                                reason="symlink")
            os.makedirs(path, mode=DIR_MODE, exist_ok=True)
            _chmod_dir(path)
        return True
    except (OSError, AsksError):
        return False


# ----------------------------------------------------------------- safe file IO
def safe_write(path, data):
    """Land bytes at `path` atomically and never through a link: a unique temp
    name in the destination's own directory (pid plus a per-process counter),
    O_EXCL|O_NOFOLLOW at 0600, then os.replace -- retried on the Windows
    PermissionError of a still-open handle, the way notes.move_note retries.
    Raises AsksError(reason="symlink") when `path` or its parent is a link;
    any other failure propagates (after the temp file is cleaned up)."""
    parent = os.path.dirname(path) or "."
    if _is_link(path):
        raise AsksError("refusing to write through a symlink: %s" % path,
                        reason="symlink")
    if _is_link(parent):
        raise AsksError("refusing to write into a symlinked directory: %s" % parent,
                        reason="symlink")
    tmp = os.path.join(parent, ".%s.%d.%d.tmp" % (os.path.basename(path),
                                                  os.getpid(), next(_TMP_SEQ)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                 FILE_MODE)
    try:
        os.write(fd, data)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    finally:
        os.close(fd)
    delay = 0.05
    for _ in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
    try:
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def safe_read(path, limit):
    """Read at most `limit` bytes, refusing anything a loop must not trust: the
    open carries O_NOFOLLOW where the platform has it, on posix the fstat uid
    must be ours, and a longer file is not read at all. The reason names what
    was refused (REASONS); the bytes are exactly the file's contents when all
    checks pass."""
    if _is_link(path):
        raise AsksError("refusing to read through a symlink: %s" % path,
                        reason="symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        reason = "symlink" if _is_link(path) else "read_error"
        raise AsksError("cannot read %s (%s)" % (path, e), reason=reason) from e
    try:
        info = os.fstat(fd)
        if os.name == "posix" and info.st_uid != os.getuid():
            raise AsksError("file is owned by uid %d, not us: %s"
                            % (info.st_uid, path), reason="uid")
        if info.st_size > limit:
            raise AsksError("%s is %d bytes; the limit is %d"
                            % (path, info.st_size, limit), reason="too_large")
        data = os.read(fd, limit + 1)
    except OSError as e:                            # an AsksError is not an OSError:
        raise AsksError("cannot read %s (%s)"                       # the refusals above
                        % (path, e), reason="read_error") from e   # ride through
    finally:
        os.close(fd)
    if len(data) > limit:
        raise AsksError("%s grew past its %d-byte limit while being read"
                        % (path, limit), reason="too_large")
    return data


# ----------------------------------------------------------------- the lease
def _lease_path(run_dir):
    return os.path.join(run_dir, ASKS_DIR, LEASE_FILE)


def lease_seconds():
    """The lease length: QWEN_ASK_LEASE as an int in LEASE_MIN..LEASE_MAX, or
    LEASE_DEFAULT -- with ONE stderr line naming the variable per bad call, and
    no warning at all when the variable is unset."""
    raw = os.environ.get("QWEN_ASK_LEASE")
    if raw is None:
        return LEASE_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is not None and LEASE_MIN <= value <= LEASE_MAX:
        return value
    if raw not in _LEASE_WARNED:               # one warning, not one per refresh
        _LEASE_WARNED.add(raw)
        sys.stderr.write("asks: QWEN_ASK_LEASE=%r is not an integer in %d..%d;"
                         " using %d\n" % (raw, LEASE_MIN, LEASE_MAX, LEASE_DEFAULT))
    return LEASE_DEFAULT


_LEASE_WARNED = set()


def write_lease(run_dir, pid):
    """Stamp the ask lease for `pid` (spec 10.3): {"pid","until","t"} at
    asks/lease.json, 0600, temp+rename through safe_write."""
    now = _now_t()
    body = {"pid": pid, "until": now + lease_seconds(), "t": now}
    safe_write(_lease_path(run_dir), json.dumps(body).encode("utf-8"))


def read_lease(run_dir):
    """The lease dict, or None: unreadable, unparseable, not a JSON object --
    any of them reads as no lease, never an exception."""
    try:
        data = safe_read(_lease_path(run_dir), _LEASE_MAX_BYTES)
    except (AsksError, OSError):
        return None
    try:
        rec = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def lease_live(run_dir, now=None):
    """True while a readable lease still holds: its `until` is a number and
    `now <= until` (the boundary instant is still live)."""
    rec = read_lease(run_dir)
    if rec is None:
        return False
    until = rec.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    if now is None:
        now = time.time()
    return now <= until


def delete_lease(run_dir, only_pid=None):
    """Remove the lease; with `only_pid`, only the lease that names that pid
    goes -- and a missing lease file is success, not an error."""
    path = _lease_path(run_dir)
    if only_pid is not None:
        rec = read_lease(run_dir)
        if rec is None or rec.get("pid") != only_pid:
            return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


# ----------------------------------------------------------------- the ask
def ask_text(*, n, max_rounds, title, run_id, repo, probe, task_path,
             next_started, start, diffstat, fails):
    """The whole ask file (spec 4.3): header, repo line (the probe sandbox is
    named when probe), the round facts, the diffstat under "changed files:",
    the failing checks, and the "How to answer" block with the two commands the
    main session runs. The text is ALWAYS within ASK_MAX UTF-8 bytes: the
    failing section is cut first (at FAILS_MAX characters, then further if
    needed) so the header and the answer block survive; if the header content
    itself would overflow, its longest free-text field gives way (diffstat,
    title, repo, task, next round, start commit -- each kept as long as the cap
    allows), so the structural lines and the answer block still fit."""
    parts = {"diff": str(diffstat), "title": str(title), "repo": str(repo),
             "task": str(task_path), "next": str(next_started),
             "start": str(start)}
    body = str(fails)[:FAILS_MAX]

    def assemble(cut_fails, cut):
        head = [
            "# Ask %d for %s (run %s)" % (n, cut["title"], run_id),
            "repo: %s%s" % (cut["repo"],
                            " (probe sandbox: read only)" if probe else ""),
            "task: %s" % cut["task"],
            "round: %d of %d" % (n, max_rounds),
            "next round: started at %s (it is running now)" % cut["next"],
            "start commit: %s" % cut["start"],
            "changed files:",
            cut["diff"],
        ]
        answer = [
            "## How to answer",
            "qwen-agent --send %s -f - <<'EOF'" % run_id,
            "advice; reaches the running round as a note",
            "qwen-agent --amend %s -f - <<'EOF'" % run_id,
            "the whole corrected task file; applies at the next check run",
        ]
        sections = ["\n".join(head),
                    "## Failing checks\n" + cut_fails,
                    "\n".join(answer)]
        return "\n\n".join(sections) + "\n"

    def over(cut_fails, cut):
        return len(assemble(cut_fails, cut).encode("utf-8")) > ASK_MAX

    def fit(value, test):
        """The longest prefix of `value` the cap test still accepts ('' when
        even the empty prefix does not fit: this field gives everything it can)."""
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if test(value[:mid]):
                lo = mid
            else:
                hi = mid - 1
        return value[:lo]

    if over(body, parts):                       # the failing section is cut first
        body = fit(body, lambda v: not over(v, parts))
    for _ in range(len(parts)):                 # then the header's longest field
        if not over(body, parts):
            break
        key = max(parts, key=lambda k: len(parts[k]))
        if not parts[key]:
            break                               # nothing free-text left to cut
        parts[key] = fit(parts[key],
                          lambda v, k=key: not over(body, {**parts, k: v}))
    text = assemble(body, parts)
    data = text.encode("utf-8")
    if len(data) > ASK_MAX:
        # only the structural parts (run_id, n, the section marks) could still
        # overflow once every free-text field is empty: then the cap is absolute
        text = data[:ASK_MAX].decode("utf-8", "ignore")
    return text


def write_ask(run_dir, n, text):
    """Land ask <n> in asks/new/ and return its path."""
    path = os.path.join(run_dir, ASKS_DIR, NEW, "%d.md" % n)
    safe_write(path, text.encode("utf-8"))
    return path


def _ask_queues(new_dir):
    """The two claim queues from asks/new/: the round asks and the question asks,
    each a list of its own numbers in numeric order (2 before 10; q2 before q10),
    names outside ASK_RE not queues at all."""
    rounds, questions = [], []
    try:
        names = os.listdir(new_dir)
    except OSError:
        return rounds, questions
    for name in names:
        if not ASK_RE.fullmatch(name):
            continue
        stem = name[:-len(".md")]
        if stem.startswith(QUESTION_PREFIX):
            questions.append(int(stem[len(QUESTION_PREFIX):]))
        else:
            rounds.append(int(stem))
    rounds.sort()
    questions.sort()
    return rounds, questions


def _head_mtime_ns(new_dir, name):
    """The head's mtime_ns, or None when the file vanished since the listing."""
    try:
        return os.stat(os.path.join(new_dir, name)).st_mtime_ns
    except OSError:
        return None


def claim_oldest(run_dir):
    """Claim the oldest pending ask by rename across the two queues (spec 11.3):
    the round asks ``<n>.md`` queue in numeric order and the question asks
    ``q<n>.md`` queue in their own numeric order, and the head with the older
    file mtime (st_mtime_ns) claims first -- a tie goes to the round ask. The
    rename is os.replace asks/new/<name> into asks/claimed/, and the claimed path
    comes back absolute (a relative run_dir still yields an openable one). A head
    that vanished (the race against another claimer) moves on to the next
    candidate; None when nothing can be claimed."""
    new_dir = os.path.join(run_dir, ASKS_DIR, NEW)
    claimed_dir = os.path.join(run_dir, ASKS_DIR, CLAIMED)
    rounds, questions = _ask_queues(new_dir)
    while rounds or questions:
        if rounds and questions:
            r_ns = _head_mtime_ns(new_dir, "%d.md" % rounds[0])
            q_ns = _head_mtime_ns(new_dir,
                                  "%s%d.md" % (QUESTION_PREFIX, questions[0]))
            if r_ns is None:
                rounds.pop(0)                     # vanished head: next candidate
                continue
            if q_ns is None:
                questions.pop(0)
                continue
            kind = "round" if r_ns <= q_ns else "question"   # tie: the round ask
        elif rounds:
            kind = "round"
        else:
            kind = "question"
        if kind == "round":
            name = "%d.md" % rounds.pop(0)
        else:
            name = "%s%d.md" % (QUESTION_PREFIX, questions.pop(0))
        src = os.path.join(new_dir, name)
        dst = os.path.join(claimed_dir, name)
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            continue                              # lost the race: try the next one
        except OSError:
            return None                           # this one cannot move; stop here
        return os.path.abspath(dst)               # absolute, whoever passed the run dir
    return None


# ----------------------------------------------------------------- the log
def _event_line(rec):
    return (json.dumps(rec, ensure_ascii=False, default=str)
            + "\n").encode("utf-8", "replace")


def log_event(run_dir, ev, **fields):
    """Append one {"t","ev",**fields} JSON line to asks/log.jsonl: ONE os.write
    on an O_APPEND fd at 0600. The line stays under LOG_MAX bytes -- fields are
    dropped rather than the line exceeded (biggest value first) -- and an `ev`
    so long that even the bare {"t","ev"} line would reach the cap is truncated
    to the longest prefix that fits: every event lands on the log. A symlinked
    asks/ directory is refused in silence, and so is any OSError (a missing or
    blocked asks/): log_event never raises -- the log is never the round's
    problem."""
    asks_dir = os.path.join(run_dir, ASKS_DIR)
    if _is_link(asks_dir):
        return
    rec = {"t": _now_t(), "ev": ev}
    rec.update(fields)
    line = _event_line(rec)
    if len(line) >= LOG_MAX:
        # biggest value first: the small fields are the ones worth keeping
        for key in sorted(fields, key=lambda k: -len(str(fields[k]))):
            del rec[key]
            line = _event_line(rec)
            if len(line) < LOG_MAX:
                break
    if len(line) >= LOG_MAX:
        # no fields left and still too long: ev is the offender -- cut it to the
        # longest prefix that fits (the bare {"t","ev":""} line always does)
        ev_str = str(ev)
        lo, hi = 0, len(ev_str)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(_event_line({"t": rec["t"], "ev": ev_str[:mid]})) < LOG_MAX:
                lo = mid
            else:
                hi = mid - 1
        rec["ev"] = ev_str[:lo]
        line = _event_line(rec)
    with contextlib.suppress(OSError):        # never raises: not even a missing or
        fd = os.open(os.path.join(asks_dir, LOG_FILE),   # blocked asks/ is the
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND   # round's problem
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                     FILE_MODE)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


# ------------------------------------------------- question asks and admission (11.3)
def ask_main_max() -> int:
    """The per-run question cap (spec 11.3): QWEN_ASK_MAIN_MAX as an int,
    clamped to 0..ASK_MAIN_MAX_BOUND; unset or non-integer is the default
    ASK_MAIN_MAX_DEFAULT. The relay reads it once and passes it to
    admit_question as `limit`."""
    raw = os.environ.get("QWEN_ASK_MAIN_MAX")
    try:
        value = int(raw)
    except (TypeError, ValueError):                # unset, "" or not an integer
        return ASK_MAIN_MAX_DEFAULT
    return max(0, min(value, ASK_MAIN_MAX_BOUND))


def question_text(*, q, title, run_id, asker_address, question, options) -> str:
    """The whole question ask file: the header naming the asker and the run, a
    `Run:` line with the task title, then the question and every option (as
    `- <option>` before quoting) quoted the way render_note quotes a share --
    every line prefixed "> ", control characters stripped by notes' own
    _strip_controls -- and a "## How to answer" block with the one command that
    reaches the asker. Nothing but these fields ever lands in the text: a
    question is advice material for the main session, never a place for the
    asker's metadata."""
    text = question if isinstance(question, str) else ""
    opts = options if isinstance(options, (list, tuple)) else []
    head = ["# Question %s%s from %s (run %s)"
            % (QUESTION_PREFIX, q, asker_address, run_id),
            "Run: %s" % (title if isinstance(title, str) else "")]
    quoted = []
    for chunk in [text] + ["- %s" % (o if isinstance(o, str) else "")
                           for o in opts]:
        quoted.extend("> " + line for line in _strip_controls(chunk).split("\n"))
    answer = [
        "## How to answer",
        "qwen-agent --send %s -f - <<'EOF'" % asker_address,
        "your answer, on the lines before the EOF",
        "EOF",
        "Not answering is fine: the agent was told to decide for itself and"
        " keep working.",
    ]
    return "\n\n".join(["\n".join(head), "\n".join(quoted),
                        "\n".join(answer)]) + "\n"


def _pointer_for_run(run_dir, asker_address):
    """The registry pointer of the asking run (the question ask's title and run
    id come from it): the asker's address starts with the run's watch id
    (`<id>` or `<id>/<unit>`), and the pointer counts only when its run_path is
    this run folder. None otherwise -- the caller then uses "" for both fields."""
    run_id = asker_address.split("/", 1)[0] if isinstance(asker_address, str) else ""
    if not RUN_ID_RE.fullmatch(run_id):
        return None
    pointer = _pointer(run_id)
    if not isinstance(pointer, dict):
        return None
    run_path = pointer.get("run_path")
    if not isinstance(run_path, str) or not run_path:
        return None
    if os.path.realpath(run_path) != os.path.realpath(run_dir):
        return None
    return pointer


def _questions_path(run_dir):
    return os.path.join(run_dir, ASKS_DIR, QUESTIONS_FILE)


def _admitted_count(run_dir):
    """asks/questions.json's counter: a missing, unreadable, symlinked,
    unparsable or malformed file reads as 0, never a raise and never a block."""
    try:
        data = safe_read(_questions_path(run_dir), _STATE_MAX)
    except (AsksError, OSError):
        return 0
    try:
        rec = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return 0
    if not isinstance(rec, dict):
        return 0
    n = rec.get("admitted")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return 0
    return n


def _admit_lock_path(run_dir):
    return os.path.join(run_dir, ASKS_DIR, ADMIT_LOCK_FILE)


def _acquire_admit_lock(run_dir):
    """Take asks/.admit.lock (O_CREAT|O_EXCL, the relay's one admission lock):
    an existing lock whose mtime is over ADMIT_LOCK_STALE seconds old is removed
    and retried (the crashed relay's lock is nobody's), any other is waited out
    at ADMIT_LOCK_POLL s and given up after ADMIT_LOCK_GIVEUP with an AsksError.
    A symlinked asks/ folder is refused outright -- O_NOFOLLOW guards the final
    component only, and an O_CREAT open would otherwise follow the planted
    DIRECTORY link and land the lock somewhere else (the log_event/safe_write
    rule, kept here). A stale link is unlinked, never its target; an UNLINKABLE
    "lock" (a planted directory) is polled, never spun on."""
    asks_dir = os.path.join(run_dir, ASKS_DIR)
    if _is_link(asks_dir):
        raise AsksError("asks folder is a symlink: %s" % asks_dir,
                        reason="symlink")
    lock = _admit_lock_path(run_dir)
    deadline = time.time() + ADMIT_LOCK_GIVEUP
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        except FileExistsError:
            if time.time() >= deadline:
                raise AsksError("the asks admission lock is held: %s" % lock)
            try:
                stale = (time.time() - os.lstat(lock).st_mtime) > ADMIT_LOCK_STALE
            except OSError:
                stale = True               # gone since the open failed: retry now
            if stale:
                try:
                    os.unlink(lock)
                except OSError:
                    # not removable (a planted directory): one poll of patience
                    # between retries -- never spin to the deadline
                    time.sleep(ADMIT_LOCK_POLL)
                continue
            time.sleep(ADMIT_LOCK_POLL)
            continue
        except OSError as e:
            raise AsksError("cannot take the asks admission lock %s (%s)"
                            % (lock, e)) from e
        os.close(fd)
        return


def _release_admit_lock(run_dir):
    with contextlib.suppress(OSError):
        os.unlink(_admit_lock_path(run_dir))


def _rec_field(rec, key):
    return rec.get(key) if isinstance(rec, dict) else None


def _log_drop(run_dir, asker_address, msg_dir, rec, reason):
    """The one drop: question_dropped with reason, asker and the record's id --
    never the question text -- and the asker's open question cleared when it
    names exactly this record (a drop for someone else's open question leaves it
    open: it is still the asker's pending answer)."""
    qid = _rec_field(rec, "id")
    log_event(run_dir, "question_dropped", reason=reason,
              asker=asker_address, id=qid)
    if not isinstance(qid, str):
        return
    open_rec = msgdir.open_question(msg_dir)
    if open_rec is not None and open_rec.get("q_id") == qid:
        msgdir.clear_open_question(msg_dir)


def admit_question(run_dir, asker_address, msg_dir, rec, limit) -> tuple[str, str]:
    """Admit one agent question (spec 11.3, the relay's side of ask_main): under
    asks/.admit.lock, checking IN THIS ORDER -- the lease live (else drop
    "no_listener"); the asker has no OTHER open question (the server opens the
    asker's own at queue time, so its own record never blocks itself -- another
    one drops as "open_question"); the run's admitted count in
    asks/questions.json below `limit` (else "run_limit"). Admitted: q = N + 1,
    asks/new/q<q>.md written from question_text (title and run id read from the
    run's registry pointer, "" when there is none), the counter saved (temp +
    replace) and question_admitted logged, and ("admitted", path) comes back.
    Every drop logs question_dropped (reason, asker, id -- never the text) and
    clears the asker's open question when it names this record; None-shaped or
    malformed `rec` fields simply read as empty. `rec` is the body
    msgdir.take_out returned: {"id","t","kind":"question","question","options"}."""
    _acquire_admit_lock(run_dir)
    try:
        qid = _rec_field(rec, "id")
        if not lease_live(run_dir):
            _log_drop(run_dir, asker_address, msg_dir, rec, "no_listener")
            return "dropped", "no_listener"
        open_rec = msgdir.open_question(msg_dir)
        if open_rec is not None and open_rec.get("q_id") != qid:
            _log_drop(run_dir, asker_address, msg_dir, rec, "open_question")
            return "dropped", "open_question"
        admitted = _admitted_count(run_dir)
        if admitted >= limit:
            _log_drop(run_dir, asker_address, msg_dir, rec, "run_limit")
            return "dropped", "run_limit"
        q = admitted + 1
        pointer = _pointer_for_run(run_dir, asker_address) or {}
        title = pointer.get("title")
        run_id = pointer.get("id")
        text = question_text(
            q=q, title=title if isinstance(title, str) else "",
            run_id=run_id if isinstance(run_id, str) else "",
            asker_address=asker_address,
            question=_rec_field(rec, "question"),
            options=_rec_field(rec, "options"))
        path = os.path.join(run_dir, ASKS_DIR, NEW,
                            "%s%d.md" % (QUESTION_PREFIX, q))
        safe_write(path, text.encode("utf-8"))
        safe_write(_questions_path(run_dir),
                   json.dumps({"admitted": q}).encode("utf-8"))
        log_event(run_dir, "question_admitted", q="%s%d" % (QUESTION_PREFIX, q),
                  asker=asker_address, id=qid)
        return "admitted", os.path.abspath(path)
    finally:
        _release_admit_lock(run_dir)


def drop_question(run_dir, asker_address, msg_dir, rec, reason) -> None:
    """The drop half, bare (for the loop-end drain): the same
    question_dropped logging and open-question clearing as a refused admission,
    with no lock and no checks -- whoever calls it has already decided."""
    _log_drop(run_dir, asker_address, msg_dir, rec, reason)


# ------------------------------------------------- applying an amendment (4.5, 9)
class Applied(NamedTuple):
    """The amendment the loop will run on: the new number (k+1), the normalised
    task text, the parsed Task, the diff for the coder prompt, and the checklist
    counts against the old checklist."""

    k: int
    text: str
    task: object
    diff: str
    added: int
    removed: int
    changed: int


class Rejected(NamedTuple):
    """The amendment the loop will not run on: the enum reason (one of REASONS)
    -- the only thing the rejection is ever allowed to say about itself."""

    reason: str


def _normalise(text):
    """The one amendment normalisation (both directions): a Windows-edited task
    file reads as LF to the loop, in the amendment and in the current text."""
    return text.replace("\r\n", "\n")


def checklist_counts(old_items, new_items) -> tuple[int, int, int]:
    """(added, removed, changed) between two checklists, compared as (kind, arg,
    text) tuples (the index is positional, so it never counts): SequenceMatcher
    opcodes decide, with autojunk off so a long repeated checklist is not "..."
    away. A replace pairs min(m, n) items as changed and counts the surplus of
    the longer side as insertions or deletions; equal spans count nothing."""
    old = [(item.kind, item.arg, item.text) for item in old_items]
    new = [(item.kind, item.arg, item.text) for item in new_items]
    added = removed = changed = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        m, n = i2 - i1, j2 - j1
        if tag == "insert":
            added += n
        elif tag == "delete":
            removed += m
        elif tag == "replace":
            changed += min(m, n)
            added += max(0, n - m)
            removed += max(0, m - n)
    return added, removed, changed


def task_diff(old: str, new: str, k: int | None = None) -> str:
    """The unified diff of the two (LF-normalised) task texts, n=2 lines of
    context, headers naming task.md; the second header carries the amendment
    number when the caller knows it (plain "amendment k" until then)."""
    to = "task.md (amendment k)" if k is None else "task.md (amendment %d)" % k
    return "".join(difflib.unified_diff(_normalise(old).splitlines(True),
                                        _normalise(new).splitlines(True),
                                        "task.md (before)", to, n=2))


def coder_block(k: int, diff: str) -> str:
    """The coder-prompt block: the line naming the amendment, then the diff cut
    to its first DIFF_LINES lines -- a cut diff says so on a final line, so the
    coder is never told it saw everything."""
    head = "The main session changed the task's checklist (amendment %d):\n" % k
    lines = diff.splitlines()
    body = "\n".join(lines[:DIFF_LINES])
    if len(lines) > DIFF_LINES:
        body += ("\n" if body else "") + "... (diff cut at %d lines)" % DIFF_LINES
    return head + body


def _reject_amendment(run_dir, applying, reason, round_no, unlink=False):
    """File one rejection: the bytes aside in amend/rejected/<time_ns>.md (the
    symlink case only unlinks the link -- its target is not ours to move), the
    enum reason on the log, and the Rejected. Never raises: a rejection that
    cannot be filed is still a rejection, never a crash of the round that found
    it. The parser's own message is not among what gets logged."""
    if unlink:
        with contextlib.suppress(OSError):
            os.unlink(applying)
    else:
        with contextlib.suppress(OSError):
            os.replace(applying, os.path.join(run_dir, AMEND_DIR, REJECTED,
                                              "%d.md" % time.time_ns()))
    log_event(run_dir, "amend_rejected", round=round_no, reason=reason)
    return Rejected(reason)


def apply_pending(run_dir: str, current_text: str, k: int, has_test_cmd: bool,
                  round_no: int) -> Applied | Rejected | None:
    """The pure apply step the loop runs BEFORE a check round (spec 4.5, 9):
    claim amend/pending.md by rename onto .applying.md (None when there is
    nothing to claim), then validate -- a symlink, an unreadable or over-cap
    body, a text identical to the (LF-normalised) current one, a parse error,
    or a `test` item in a run without a test command -- and reject with the
    enum reason: the bytes go to amend/rejected/, the reason to the log, and a
    rejection never raises and never repeats what the parser said. A good
    amendment lands in amend/applied/<k+1>.md by safe_write, the claim goes,
    the log says amend_applied, and the Applied names the new k, the
    normalised text, the parsed Task, the diff and the checklist counts (the
    old checklist is whatever taskfile.parse makes of current_text; a current
    file that does not parse counts as no items at all)."""
    amend_dir = os.path.join(run_dir, AMEND_DIR)
    applying = os.path.join(amend_dir, APPLYING_FILE)
    try:
        os.replace(os.path.join(amend_dir, PENDING_FILE), applying)
    except FileNotFoundError:
        return None                                # no pending: nothing to apply
    except OSError:
        return _reject_amendment(run_dir, applying, "read_error", round_no)
    if _is_link(applying):                         # claimed by rename, still a link
        return _reject_amendment(run_dir, applying, "symlink", round_no, unlink=True)
    try:
        data = safe_read(applying, AMEND_MAX)
    except AsksError as e:
        return _reject_amendment(run_dir, applying, e.reason, round_no)
    except OSError:
        return _reject_amendment(run_dir, applying, "read_error", round_no)
    new_text = _normalise(data.decode("utf-8", "replace"))
    old_text = _normalise(current_text)
    if new_text == old_text:
        return _reject_amendment(run_dir, applying, "noop", round_no)
    try:
        task = taskfile.parse(new_text)
    except ValueError:
        # the enum reason and nothing else: the parser's message must not reach
        # asks/log.jsonl (it is the coder's own text, not the log's)
        return _reject_amendment(run_dir, applying, "parse_error", round_no)
    if not has_test_cmd and any(item.kind == "test" for item in task.items):
        return _reject_amendment(run_dir, applying, "test_without_cmd", round_no)
    safe_write(os.path.join(amend_dir, APPLIED, "%d.md" % (k + 1)),
               new_text.encode("utf-8"))
    with contextlib.suppress(OSError):
        os.unlink(applying)
    log_event(run_dir, "amend_applied", k=k + 1, round=round_no)
    try:
        old_items = taskfile.parse(old_text).items
    except ValueError:
        old_items = []                             # an unparsable current file has
    added, removed, changed = checklist_counts(old_items, task.items)  # no checklist
    return Applied(k=k + 1, text=new_text, task=task, added=added,
                   removed=removed, changed=changed,
                   diff=task_diff(old_text, new_text, k + 1))


def leftover_pending(run_dir: str) -> str | None:
    """End-of-run housekeeping: a pending.md the loop never reached moves whole
    to amend/unapplied.md (the path comes back so the end-of-run report can
    name it); a symlinked pending is unlinked and nothing is returned, and a
    missing one is None. Never raises -- housekeeping is never the end's problem."""
    pending = os.path.join(run_dir, AMEND_DIR, PENDING_FILE)
    if _is_link(pending):
        with contextlib.suppress(OSError):
            os.unlink(pending)
        return None
    if not os.path.lexists(pending):
        return None
    dest = os.path.join(run_dir, AMEND_DIR, UNAPPLIED_FILE)
    try:
        os.replace(pending, dest)
    except OSError:
        return None
    return dest


# ----------------------------------------------------------------- addressing (10.2)
def _resolve_run(run_id, kinds, wrong_kind):
    """The shared half of the two resolvers: id grammar (usage 2 for anything but
    the plain 12-hex watch id -- an `<id>/<unit>` address is usage), pointer,
    kind (the caller's message on a wrong one, code 3), run_path and an asks/
    folder that is neither missing nor a symlink. Returns (run_path, pointer)."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise AsksError("run id must be 12 lowercase hex characters (got %r)"
                        % (run_id,), code=_EXIT_USAGE)
    pointer = _pointer(run_id)
    if pointer is None:
        raise AsksError("no such run: %s" % run_id)
    if pointer.get("kind") not in kinds:
        raise AsksError(wrong_kind % (run_id, pointer.get("kind")))
    run_path = pointer.get("run_path")
    if not isinstance(run_path, str) or not run_path:
        raise AsksError("run %s has no run folder" % run_id)
    asks_dir = os.path.join(run_path, ASKS_DIR)
    if _is_link(asks_dir) or not os.path.isdir(asks_dir):
        raise AsksError("no asks folder at %s (is the loop running with "
                        "messaging on?)" % asks_dir)
    return run_path, pointer


def resolve_until_done(run_id):
    """The registry pointer and run folder of an until-done loop, for --amend
    (--next-ask and --release take resolve_asks_run, which also admits the two
    swarm pointer kinds). AsksError code 2 for an id that is not the plain
    12-hex watch id (an `<id>/<unit>` address is usage -- an until-done loop has
    no units); code 3
    for no pointer, a pointer of another kind, no run_path, or an asks/ folder
    that is missing or a symlink."""
    return _resolve_run(run_id, ("until-done",),
                        "run %s is not an until-done loop (kind %r)")


def resolve_asks_run(run_id):
    """The registry pointer and run folder of a run that HOLDS asks: an
    until-done loop or a swarm/research run (spec 11.3 -- both swarm
    pointer kinds ask, a single-shot never does). The same grammar, errors and
    asks-folder checks as resolve_until_done; any other pointer kind (an
    `agent` single-shot) is AsksError code 3. --next-ask and --release use
    this; --amend keeps resolve_until_done."""
    return _resolve_run(run_id, ASKS_KINDS,
                        "run %s holds no asks (kind %r)")


def ended_path(run_dir: str) -> str:
    """The end-of-run marker's path (spec 4.1): RUN/asks/.ended -- under asks/,
    beside the queue it closes. The supervisor writes it and never creates asks/
    to do so; readers only look."""
    return os.path.join(run_dir, ASKS_DIR, ENDED_FILE)


def run_ended(run_dir: str, pointer: dict) -> bool:
    """True when the loop is over: RUN/asks/.ended exists, or the pointer -- RE-READ
    here, so an `end` set while we wait counts -- has `end` set, or its pid is
    dead (notes.pid_alive judges, watch's _alive would answer True off posix)."""
    if os.path.lexists(ended_path(run_dir)):
        return True
    rec = pointer if isinstance(pointer, dict) else {}
    run_id = rec.get("id")
    if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id):
        fresh = _pointer(run_id)
        if fresh is not None:
            rec = fresh
    if rec.get("end") is not None:
        return True
    return not pid_alive(rec.get("pid"))


# ----------------------------------------------------------------- the CLI (10.2)
USAGE_NEXT_ASK = "usage: qwen-agent --next-ask ID [--wait SECONDS] [--release]"
USAGE_AMEND = "usage: qwen-agent --amend ID -f FILE|-f -"


def _parse_next_ask(argv):
    """(run_id, wait, release), hand-rolled like notes._parse_send: every wrong
    shape is an AsksError code 2. SECONDS is an int WAIT_MIN..WAIT_MAX (default
    WAIT_DEFAULT); --wait together with --release is usage."""
    wait = None
    release = False
    positional = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--wait":
            if wait is not None:
                raise AsksError("--wait may be given only once", code=_EXIT_USAGE)
            if i + 1 >= len(argv):
                raise AsksError("--wait needs its seconds", code=_EXIT_USAGE)
            try:
                wait = int(argv[i + 1])
            except ValueError:
                raise AsksError("--wait SECONDS must be an integer",
                                code=_EXIT_USAGE) from None
            if not WAIT_MIN <= wait <= WAIT_MAX:
                raise AsksError("--wait SECONDS must be %d..%d"
                                % (WAIT_MIN, WAIT_MAX), code=_EXIT_USAGE)
            i += 1
        elif token == "--release":
            if release:
                raise AsksError("--release may be given only once", code=_EXIT_USAGE)
            release = True
        elif token.startswith("-") and len(token) > 1:
            raise AsksError("unknown option: %s" % token, code=_EXIT_USAGE)
        else:
            positional.append(token)
        i += 1
    if len(positional) != 1:
        raise AsksError("%s (got %d positional(s))"
                        % (USAGE_NEXT_ASK, len(positional)), code=_EXIT_USAGE)
    if wait is not None and release:
        raise AsksError("--release waits for nothing: drop --wait", code=_EXIT_USAGE)
    return positional[0], WAIT_DEFAULT if wait is None else wait, release


def _install_lease_cleanup(run_dir):
    """SIGTERM/SIGINT (and SIGHUP where the platform has it) give up OUR lease --
    only the one that still names this pid -- and end the wait (spec 10.2): the
    handler raises SystemExit, which unwinds cmd_next_ask through its finally.
    Handlers belong to the main thread; off it nothing is installed. The saved
    pairs go back through _restore_handlers."""
    saved = {}
    if threading.current_thread() is not threading.main_thread():
        return saved

    def _give_up(_signum, _frame):
        delete_lease(run_dir, only_pid=os.getpid())
        raise SystemExit(1)

    names = ["SIGINT", "SIGTERM"]
    if hasattr(signal, "SIGHUP"):
        names.append("SIGHUP")
    for name in names:
        num = getattr(signal, name, None)
        if num is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            saved[num] = signal.getsignal(num)
            signal.signal(num, _give_up)
    return saved


def _restore_handlers(saved):
    for num, old in saved.items():
        with contextlib.suppress(OSError, ValueError):
            signal.signal(num, old)


def cmd_next_ask(argv) -> int:
    """The --next-ask mode (spec 10.2). The ended check runs BEFORE the lease is
    stamped -- an ended run exits 11 with no lease written and nothing claimed.
    Then poll once a second: (1) ended: delete our own lease, `run ended`, 11;
    (2) an ask claims: print its absolute path, log `claimed` (with
        kind="round", round=<int> or kind="question", q="q<n>"), refresh, 0;
    (3) the lease vanished under us (someone ran --release): `no ask yet`, 10;
    (4) the wait over: refresh, `no ask yet`, 10. The lease is refreshed every
    REFRESH seconds while waiting -- REFRESH is read at call time. The ask's
    text is never printed: the answerer opens the path the claim returns."""
    run_id, wait, release = _parse_next_ask(argv)
    run_dir, pointer = resolve_asks_run(run_id)
    if release:
        delete_lease(run_dir)              # any pid's, even after the run ended
        return 0
    if run_ended(run_dir, pointer):
        sys.stderr.write("run ended\n")
        return EXIT_RUN_ENDED
    write_lease(run_dir, os.getpid())
    saved = _install_lease_cleanup(run_dir)
    try:
        deadline = time.time() + wait
        last_refresh = time.time()
        while True:
            if run_ended(run_dir, pointer):
                delete_lease(run_dir, only_pid=os.getpid())
                sys.stderr.write("run ended\n")
                return EXIT_RUN_ENDED
            path = claim_oldest(run_dir)
            if path is not None:
                print(path)
                sys.stdout.flush()
                stem = os.path.basename(path)[:-len(".md")]
                if stem.startswith(QUESTION_PREFIX):
                    log_event(run_dir, "claimed", kind="question", q=stem,
                              pid=os.getpid())
                else:
                    log_event(run_dir, "claimed", kind="round",
                              round=int(stem), pid=os.getpid())
                write_lease(run_dir, os.getpid())
                return 0
            if not os.path.lexists(_lease_path(run_dir)):
                sys.stderr.write("no ask yet\n")   # --release by another caller
                return EXIT_NO_ASK
            now = time.time()
            if now >= deadline:
                write_lease(run_dir, os.getpid())  # a waiter still owns the lease
                sys.stderr.write("no ask yet\n")
                return EXIT_NO_ASK
            if now - last_refresh >= REFRESH:      # read at call time, not before
                write_lease(run_dir, os.getpid())
                last_refresh = now
            time.sleep(POLL)
    finally:
        _restore_handlers(saved)


def _parse_amend(argv):
    """(run_id, file_spec): exactly one ID and exactly one -f FILE|-f -, no
    positional text -- the amendment is the file, never an argument. Usage 2."""
    file_spec = None
    positional = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "-f":
            if file_spec is not None:
                raise AsksError("-f may be given only once", code=_EXIT_USAGE)
            if i + 1 >= len(argv):
                raise AsksError("-f needs FILE or -", code=_EXIT_USAGE)
            file_spec = argv[i + 1]
            i += 1
        elif token.startswith("-") and len(token) > 1:
            raise AsksError("unknown option: %s" % token, code=_EXIT_USAGE)
        else:
            positional.append(token)
        i += 1
    if len(positional) != 1:
        raise AsksError("%s (got %d positional(s))"
                        % (USAGE_AMEND, len(positional)), code=_EXIT_USAGE)
    if file_spec is None:
        raise AsksError("--amend needs -f FILE or -f -", code=_EXIT_USAGE)
    return positional[0], file_spec


def _read_amend_source(spec):
    """-f's operand: a file path, or `-` for stdin; bytes decoded leniently. Over
    AMEND_MAX UTF-8 bytes is usage 2 -- the loop could never apply a file that
    big -- and an unreadable file is usage too (like notes._read_text_source)."""
    if spec == "-":
        raw = (sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer")
               else sys.stdin.read().encode("utf-8", "replace"))
    else:
        try:
            with open(spec, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            raise AsksError("cannot read -f %s (%s)" % (spec, e),
                            code=_EXIT_USAGE) from e
    text = raw.decode("utf-8", "replace")
    size = max(len(raw), len(text.encode("utf-8")))   # replacements grow invalid bytes
    if size > AMEND_MAX:
        raise AsksError("the amendment is %d bytes; the limit is %d"
                        % (size, AMEND_MAX), code=_EXIT_USAGE)
    return text


def _has_test_cmd(amend_dir):
    """What amend/meta.json says about the run's test command. Only an explicit
    false refuses a `test` item: a missing or unreadable meta does not say false."""
    try:
        rec = json.loads(safe_read(os.path.join(amend_dir, META_FILE),
                                   _LEASE_MAX_BYTES).decode("utf-8", "replace"))
    except (AsksError, OSError, ValueError):
        return True
    if not isinstance(rec, dict) or "has_test_cmd" not in rec:
        return True
    return bool(rec.get("has_test_cmd"))


def cmd_amend(argv) -> int:
    """The --amend mode (spec 10.2): the run must be there to take it (3 for an
    ended run, a closed inbox, or a missing amend/), the corrected task file must
    parse through taskfile.parse (a parse error -- an empty checklist is one --
    prints the parser's message and writes nothing, 2), a `test` item needs a run
    that has a test command (2), and then the bytes land in amend/pending.md by
    safe_write with `amend queued` on stdout (0)."""
    run_id, file_spec = _parse_amend(argv)
    run_dir, pointer = resolve_until_done(run_id)
    text = _read_amend_source(file_spec)       # before the checks: stdin can be slow
    closed = os.path.join(run_dir, INBOX_DIR, CLOSED_SENTINEL)
    if run_ended(run_dir, pointer):
        raise AsksError("the run has ended: %s" % run_id)
    if os.path.lexists(closed):
        raise AsksError("the run's inbox is closed: %s" % run_id)
    amend_dir = os.path.join(run_dir, AMEND_DIR)
    if _is_link(amend_dir) or not os.path.isdir(amend_dir):
        raise AsksError("no amend folder at %s (is the loop running with "
                        "messaging on?)" % amend_dir)
    try:
        task = taskfile.parse(text)
    except ValueError as e:
        sys.stderr.write("%s\n" % e)           # the parser's own message
        return _EXIT_USAGE
    if any(item.kind == "test" for item in task.items) and not _has_test_cmd(amend_dir):
        raise AsksError("the run has no test command (amend/meta.json): a "
                        "'-- check: test' item could never be checked",
                        code=_EXIT_USAGE)
    pending = os.path.join(amend_dir, PENDING_FILE)
    safe_write(pending, text.encode("utf-8"))
    # The loop closes its inbox BEFORE it moves a leftover pending.md aside, so a
    # write that lands after that move sees the sentinel here and takes the file
    # back. If the file is already gone, the loop took it (applied, or listed as
    # unapplied in the report): it is queued either way.
    if os.path.lexists(closed) or run_ended(run_dir, pointer):
        try:
            os.remove(pending)
        except FileNotFoundError:
            pass
        else:
            raise AsksError("the run ended while the amendment was being queued: %s"
                            % run_id)
    print("amend queued")
    sys.stdout.flush()
    return 0


def main(argv=None) -> int:
    """qwen-agent.sh dispatches --next-ask/--amend here (the twin of notes.main's
    send/notes): every refusal that is not printed by the mode itself rides on an
    AsksError and leaves as its exit code (2 usage, 3 unavailable); the modes
    return 0, 10 or 11 themselves."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt":                        # bash reads our lines: never a \r (K8)
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(newline="\n")
    if argv and argv[0] in ("next-ask", "--next-ask"):
        handler = cmd_next_ask
    elif argv and argv[0] in ("amend", "--amend"):
        handler = cmd_amend
    else:
        sys.stderr.write("%s\n%s\n" % (USAGE_NEXT_ASK, USAGE_AMEND))
        return _EXIT_USAGE
    try:
        return handler(argv[1:])
    except AsksError as e:
        sys.stderr.write("asks: %s\n" % e)
        return e.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
