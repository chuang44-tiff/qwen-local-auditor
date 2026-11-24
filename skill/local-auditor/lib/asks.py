"""Asks for running until-done loops: the run-folder layout, safe IO, the lease,
the ask text, the claim and the asks log (stage 1.5).

An until-done round that fails in the same way twice asks the MAIN session instead
of flailing on: the loop writes an ask, the main session answers with a note
(``qwen-agent --send``) or a task amendment (``qwen-agent --amend``). This module
owns everything ask-shaped under the run folder; no supervisor or CLI wiring yet.

Run-folder layout (spec 4.1)::

    <RUN>/asks/new/<n>.md       the asks, written by the failing round
    <RUN>/asks/claimed/<n>.md   the ask one answerer holds (claimed by rename)
    <RUN>/asks/lease.json       {"pid","until","t"}: who may ask right now
    <RUN>/asks/log.jsonl        the asks event log, one JSON line per event
    <RUN>/amend/applied/        amendments the loop applied
    <RUN>/amend/rejected/       amendments the loop rejected
    <RUN>/amend/meta.json       {"has_test_cmd": <bool>}

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
is refused in silence -- the log is never a reason to fail the round that wrote it.
"""
import contextlib
import itertools
import json
import os
import re
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.notes import DIR_MODE, FILE_MODE, _is_link  # noqa: E402

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
REASONS = ("noop", "parse_error", "test_without_cmd", "symlink", "uid",
           "read_error", "too_large")

ASKS_DIR = "asks"
AMEND_DIR = "amend"
NEW, CLAIMED = "new", "claimed"
APPLIED, REJECTED = "applied", "rejected"
LEASE_FILE = "lease.json"
LOG_FILE = "log.jsonl"
META_FILE = "meta.json"

_EXIT_UNAVAILABLE = 3      # the notes.py exit-code convention this module joins
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
    answer = [
        "## How to answer",
        "qwen-agent --send %s -f - <<'EOF'" % run_id,
        "Answer this ask: the text you send is delivered before the next round.",
        "qwen-agent --amend %s -f - <<'EOF'" % run_id,
        "Amend the task: the loop applies or rejects it between rounds.",
    ]

    def assemble(cut_fails, cut):
        head = [
            "# Ask %d for %s (run %s)" % (n, cut["title"], run_id),
            "repo: %s%s" % (cut["repo"],
                            " (probe sandbox: read only)" if probe else ""),
            "task: %s" % cut["task"],
            "round: %d of %d" % (n, max_rounds),
            "next round: started at %s" % cut["next"],
            "start commit: %s" % cut["start"],
            "changed files:",
            cut["diff"],
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
    asks/ directory is refused in silence: the log is never the round's problem."""
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
    fd = os.open(os.path.join(asks_dir, LOG_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                 FILE_MODE)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
