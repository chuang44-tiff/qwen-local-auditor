"""Asks for running until-done loops: the run-folder layout, safe IO, the lease,
the ask text, the claim and the asks log (stage 1.5).

An until-done round that fails in the same way twice asks the MAIN session instead
of flailing on: the loop writes an ask, the main session answers with a note
(``qwen-agent --send``) or a task amendment (``qwen-agent --amend``). This module
owns everything ask-shaped under the run folder, and the sender-side CLI the main
session runs (``main`` with the ``next-ask`` and ``amend`` modes, the twin of
lib/notes.py's ``send``/``notes``); the loop side -- the supervisor writing asks
and applying amendments -- comes later.

Run-folder layout (spec 4.1)::

    <RUN>/asks/new/<n>.md       the asks, written by the failing round
    <RUN>/asks/claimed/<n>.md   the ask one answerer holds (claimed by rename)
    <RUN>/asks/lease.json       {"pid","until","t"}: who may ask right now
    <RUN>/asks/log.jsonl        the asks event log, one JSON line per event
    <RUN>/amend/pending.md      the amendment --amend queued for the loop
    <RUN>/amend/applied/        amendments the loop applied
    <RUN>/amend/rejected/       amendments the loop rejected
    <RUN>/amend/meta.json       {"has_test_cmd": <bool>}
    <RUN>/.ended                the supervisor's end-of-run sentinel

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

Directories are 0700 and files 0600 on POSIX, like lib/notes.py; every write goes
through safe_write (a unique temp name in the destination's own directory, pid plus
a per-process counter, then os.replace, retried on PermissionError the way
notes.move_note retries on a still-open Windows handle). Reads refuse anything
symlinked, foreign-owned or oversized -- the reasons a later CLI maps onto exit
codes -- so a planted file can never trick the loop into reading or writing
somewhere else. The file-name grammar for an ask is ASK_RE: 1.md, 2.md, ... with
no leading zeros; claiming is numeric (2 before 10), and a source that vanished
mid-claim (the race against another claimer) moves on to the next candidate.

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
import contextlib
import itertools
import json
import os
import re
import signal
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import taskfile  # noqa: E402
from lib.notes import DIR_MODE, FILE_MODE, _is_link, _pointer, pid_alive  # noqa: E402

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
ASK_RE = re.compile(r"^[1-9][0-9]*\.md$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")   # the plain watch id -- no /<unit> here
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
INBOX_DIR = "inbox"             # the loop's inbox: its .closed ends the run for us
CLOSED_SENTINEL = ".closed"
ENDED_FILE = ".ended"           # the supervisor's end-of-run sentinel under RUN

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
    sys.stderr.write("asks: QWEN_ASK_LEASE=%r is not an integer in %d..%d;"
                     " using %d\n" % (raw, LEASE_MIN, LEASE_MAX, LEASE_DEFAULT))
    return LEASE_DEFAULT


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


def claim_oldest(run_dir):
    """Claim the oldest pending ask by rename (2 before 10 -- numeric order is
    the queue): os.replace asks/new/<n>.md into asks/claimed/ and return the
    claimed path as an absolute path (a relative run_dir still yields an openable
    one). A source that vanished (the race against another claimer) moves on to
    the next candidate; None when nothing can be claimed."""
    new_dir = os.path.join(run_dir, ASKS_DIR, NEW)
    claimed_dir = os.path.join(run_dir, ASKS_DIR, CLAIMED)
    try:
        names = os.listdir(new_dir)
    except OSError:
        return None
    candidates = []
    for name in names:
        if ASK_RE.fullmatch(name):
            candidates.append((int(name[:-len(".md")]), name))
    for _number, name in sorted(candidates):
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


# ----------------------------------------------------------------- addressing (10.2)
def resolve_until_done(run_id):
    """The registry pointer and run folder of an until-done loop, for --next-ask
    and --amend. AsksError code 2 for an id that is not the plain 12-hex watch id
    (an `<id>/<unit>` address is usage -- an until-done loop has no units); code 3
    for no pointer, a pointer of another kind, no run_path, or an asks/ folder
    that is missing or a symlink."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise AsksError("run id must be 12 lowercase hex characters (got %r)"
                        % (run_id,), code=_EXIT_USAGE)
    pointer = _pointer(run_id)
    if pointer is None:
        raise AsksError("no such run: %s" % run_id)
    if pointer.get("kind") != "until-done":
        raise AsksError("run %s is not an until-done loop (kind %r)"
                        % (run_id, pointer.get("kind")))
    run_path = pointer.get("run_path")
    if not isinstance(run_path, str) or not run_path:
        raise AsksError("run %s has no run folder" % run_id)
    asks_dir = os.path.join(run_path, ASKS_DIR)
    if _is_link(asks_dir) or not os.path.isdir(asks_dir):
        raise AsksError("no asks folder at %s (is the loop running with "
                        "messaging on?)" % asks_dir)
    return run_path, pointer


def run_ended(run_dir: str, pointer: dict) -> bool:
    """True when the loop is over: RUN/.ended exists, or the pointer -- RE-READ
    here, so an `end` set while we wait counts -- has `end` set, or its pid is
    dead (notes.pid_alive judges, watch's _alive would answer True off posix)."""
    if os.path.lexists(os.path.join(run_dir, ENDED_FILE)):
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
    (2) an ask claims: print its absolute path, log `claimed`, refresh, 0;
    (3) the lease vanished under us (someone ran --release): `no ask yet`, 10;
    (4) the wait over: refresh, `no ask yet`, 10. The lease is refreshed every
    REFRESH seconds while waiting -- REFRESH is read at call time. The ask's
    text is never printed: the answerer opens the path the claim returns."""
    run_id, wait, release = _parse_next_ask(argv)
    run_dir, pointer = resolve_until_done(run_id)
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
                log_event(run_dir, "claimed",
                          round=int(os.path.basename(path)[:-len(".md")]),
                          pid=os.getpid())
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
    if len(raw) > AMEND_MAX:
        raise AsksError("the amendment is %d bytes; the limit is %d"
                        % (len(raw), AMEND_MAX), code=_EXIT_USAGE)
    return raw.decode("utf-8", "replace")


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
    if run_ended(run_dir, pointer):
        raise AsksError("the run has ended: %s" % run_id)
    if os.path.lexists(os.path.join(run_dir, INBOX_DIR, CLOSED_SENTINEL)):
        raise AsksError("the run's inbox is closed: %s" % run_id)
    amend_dir = os.path.join(run_dir, AMEND_DIR)
    if _is_link(amend_dir) or not os.path.isdir(amend_dir):
        raise AsksError("no amend folder at %s (is the loop running with "
                        "messaging on?)" % amend_dir)
    text = _read_amend_source(file_spec)
    try:
        task = taskfile.parse(text)
    except ValueError as e:
        sys.stderr.write("%s\n" % e)           # the parser's own message
        return _EXIT_USAGE
    if any(item.kind == "test" for item in task.items) and not _has_test_cmd(amend_dir):
        raise AsksError("the run has no test command (amend/meta.json): a "
                        "'-- check: test' item could never be checked",
                        code=_EXIT_USAGE)
    safe_write(os.path.join(amend_dir, PENDING_FILE), text.encode("utf-8"))
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
