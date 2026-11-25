"""One agent's message folder (phase 2, spec 12.2/12.5/11.4): what the agent
sends, what it published in answer, and what the driver marked delivered.

Layout (every path under the agent's msg dir M -- ``RUN/msg/units/<unit>`` for
a swarm unit, ``RUN/msg`` for an until-done loop, ``<watch_dir>/<run_id>.msg``
for a single-shot)::

    M/out/new/<id>.json        what the agent queued to send: kind share or
                               question, body {"id","t","kind",**payload}, the
                               id from notes.new_note_id()
    M/out/taken/<id>.json      taken by the relay (an unreadable file moves
                               here too, so it is never read again)
    M/replies/<id>.json        the reply to ask <id>, published once, body
                               {"note_id","t","source":"reply","text"}
    M/replies/.<id>.<pid>.tmp  the tmp a publish is landing; stale ones swept
    M/replies/.<id>.lock       the Windows publish lock (no reliable links)
    M/seen/ask-<id>            the driver delivered ask <id> to this agent
    M/seen/main-count          decimal: main notes delivered (spec 11.3)
    M/open-question.json       {"q_id","main_seq"} while one ask_main is open
    M/share-state.json         {"count","seen_texts"}: the share tool's state

Findings are run-level, one file per opted phase:
``RUN/msg/_phases/<pid>/findings.jsonl``, one JSON line per admitted share,
appended by the relay. Unit dirs live under ``RUN/msg/units/`` so no unit name
can collide with the ``_phases`` folder (spec 12.5).

Storage only: the MCP server (lib/msg_mcp.py), the driver wiring and the
relays come in later tasks and are this module's only callers.

Directories are 0700 and files 0600 on POSIX, like notes.py and asks.py.
Writes go through safe_write (a unique temp name in the destination's own
directory, O_EXCL|O_NOFOLLOW, then os.replace retried on the Windows
PermissionError the way notes.move_note retries) and reads through safe_read
(O_NOFOLLOW, our-uid check on posix, a size limit). Every path refuses a
symlinked M or subdirectory; MsgError is the only error this module raises --
readers answer None/False/0/[] for anything absent, unreadable or unparsable
rather than raising, so a missing folder is never the caller's crash.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import stat
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.notes import (DIR_MODE, FILE_MODE, NOTE_FILE_RE, NOTE_ID_RE,  # noqa: E402
                       _is_link, move_note, new_note_id)

# ----------------------------------------------------------------- constants
MSG_DIR = "msg"
UNITS_DIR = "units"
PHASES_DIR = "_phases"
OUT_DIR, NEW, TAKEN = "out", "new", "taken"
REPLIES_DIR = "replies"
SEEN_DIR = "seen"
MAIN_COUNT_FILE = "main-count"
OPEN_QUESTION_FILE = "open-question.json"
SHARE_STATE_FILE = "share-state.json"
FINDINGS_FILE = "findings.jsonl"
OUT_KINDS = ("share", "question")
SHARE_SOURCE = "reply"

_MAX_OUT = 256 * 1024        # an out/new file we will even read
_MAX_REPLY = 256 * 1024      # a reply file
_MAX_STATE = 256 * 1024      # open-question.json or share-state.json
_MAX_COUNT = 32              # the decimal inside seen/main-count

_TMP_SEQ = itertools.count()

_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) \
    | getattr(os, "O_NOFOLLOW", 0)


class MsgError(Exception):
    """The one refusal of this module: a path a symlink blocks, a malformed
    id, a payload of the wrong kind, a folder that cannot be built. Readers
    never raise it -- they answer None/False/0/[] -- only writers do."""


def _now_t():
    """The one stamp format (the notes.py convention): a Unix epoch float
    rounded to 3 decimals."""
    return round(time.time(), 3)


def _chmod_dir(path):
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, DIR_MODE)


def _discard(path):
    with contextlib.suppress(OSError):
        os.unlink(path)


def _check_note_id(note_id):
    if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
        raise MsgError("malformed note id: %r" % (note_id,))


# ----------------------------------------------------------------- safe file IO
def safe_write(path, data):
    """Land bytes at `path` atomically and never through a link: a unique temp
    name in the destination's own directory (pid plus a per-process counter),
    O_EXCL|O_NOFOLLOW at 0600, then os.replace retried on the Windows
    PermissionError of a still-open handle (the asks.safe_write/notes.move_note
    pattern). MsgError for a symlinked path or parent and for any failure."""
    parent = os.path.dirname(path) or "."
    if _is_link(path):
        raise MsgError("refusing to write through a symlink: %s" % path)
    if _is_link(parent):
        raise MsgError("refusing to write into a symlinked directory: %s" % parent)
    tmp = os.path.join(parent, ".%s.%d.%d.tmp" % (os.path.basename(path),
                                                  os.getpid(), next(_TMP_SEQ)))
    try:
        fd = os.open(tmp, _FLAGS, FILE_MODE)
    except OSError as e:
        raise MsgError("cannot create %s (%s)" % (tmp, e)) from e
    try:
        os.write(fd, data)
    except OSError as e:
        _discard(tmp)
        raise MsgError("cannot write %s (%s)" % (tmp, e)) from e
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
        except OSError as e:                    # not the Windows handle case:
            _discard(tmp)                       # a planted directory in the way,
            raise MsgError("cannot land %s (%s)" % (path, e)) from e   # say so
    try:
        os.replace(tmp, path)
    except OSError as e:
        _discard(tmp)
        raise MsgError("cannot land %s (%s)" % (path, e)) from e


def safe_read(path, limit):
    """Read at most `limit` bytes, refusing anything a reader must not trust
    (the asks.safe_read pattern): the open carries O_NOFOLLOW where the
    platform has it, on posix the fstat uid must be ours, a longer file is not
    read at all, and a symlinked file or parent is refused. MsgError names the
    refusal; callers that only read catch it and answer "not there"."""
    if _is_link(path):
        raise MsgError("refusing to read through a symlink: %s" % path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise MsgError("cannot read %s (%s)" % (path, e)) from e
    try:
        info = os.fstat(fd)
        if os.name == "posix" and info.st_uid != os.getuid():
            raise MsgError("file is owned by uid %d, not us: %s"
                           % (info.st_uid, path))
        if info.st_size > limit:
            raise MsgError("%s is %d bytes; the limit is %d"
                           % (path, info.st_size, limit))
        data = os.read(fd, limit + 1)
    except OSError as e:                          # a MsgError is not an OSError:
        raise MsgError("cannot read %s (%s)"                       # the refusals above
                       % (path, e)) from e                        # ride through
    finally:
        os.close(fd)
    if len(data) > limit:
        raise MsgError("%s grew past its %d-byte limit while being read"
                       % (path, limit))
    return data


def _json_bytes(rec):
    """The one JSON encoding: one object on one \n-terminated line, never
    ASCII-escaped; a non-serialisable value falls back to str (the log rule)."""
    return (json.dumps(rec, ensure_ascii=False, default=str)
            + "\n").encode("utf-8", "replace")


def _read_json_dict(path, limit):
    """The parsed dict at `path`, or None: absent, symlinked, oversized,
    unreadable, unparsable or not an object -- any of them reads as None."""
    try:
        data = safe_read(path, limit)
    except MsgError:
        return None
    try:
        rec = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


# ----------------------------------------------------------------- the layout
def init_msgdir(M: str) -> None:
    """Create M and its four folders -- out/new, out/taken, replies, seen --
    at 0700, idempotent like notes.init_inbox. MsgError when M or any folder
    (including the intermediate out/) is a symlink -- checked BEFORE anything
    is created, so a planted link is refused, never walked through -- or when
    a folder cannot be built."""
    parts = (M, os.path.join(M, OUT_DIR), os.path.join(M, OUT_DIR, NEW),
             os.path.join(M, OUT_DIR, TAKEN), os.path.join(M, REPLIES_DIR),
             os.path.join(M, SEEN_DIR))
    for path in parts:
        if _is_link(path):
            raise MsgError("msg folder is a symlink: %s" % path)
    try:
        for path in parts:
            os.makedirs(path, mode=DIR_MODE, exist_ok=True)
            _chmod_dir(path)
    except OSError as e:
        raise MsgError("cannot build msg dir %s (%s)" % (M, e)) from e


def unit_msgdir(run_dir: str, unit: str) -> str:
    """The msg dir of one swarm unit: RUN/msg/units/<unit> (spec 12.5 -- under
    units/, so no unit name collides with the _phases folder)."""
    return os.path.join(run_dir, MSG_DIR, UNITS_DIR, unit)


def phase_dir(run_dir: str, pid: str) -> str:
    """The folder of one opted phase: RUN/msg/_phases/<pid> (spec 12.5)."""
    return os.path.join(run_dir, MSG_DIR, PHASES_DIR, pid)


def loop_msgdir(run_dir: str) -> str:
    """The msg dir of an until-done loop: RUN/msg, the loop as one agent."""
    return os.path.join(run_dir, MSG_DIR)


def single_msgdir(watch_dir: str, run_id: str) -> str:
    """The msg dir of a single-shot run: <watch_dir>/<run_id>.msg, beside its
    .inbox (it goes when watch._drop_activity drops the activity)."""
    return os.path.join(watch_dir, "%s.msg" % run_id)


# ----------------------------------------------------------------- the out queue
def write_out(M, kind: str, payload: dict) -> str:
    """Queue one thing the agent sends: kind "share" or "question" (anything
    else is MsgError), file out/new/<id>.json with body {"id","t","kind",
    **payload} (the payload's own keys ride beside them; "id", "t" and "kind"
    are always ours, a payload key of the same name never overrides them),
    landed by safe_write. Returns the id."""
    if kind not in OUT_KINDS:
        raise MsgError("out kind %r is not one of %s"
                       % (kind, " or ".join(OUT_KINDS)))
    if not isinstance(payload, dict):
        raise MsgError("out payload must be a dict, not %s" % type(payload).__name__)
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    new_dir = os.path.join(M, OUT_DIR, NEW)
    if _is_link(new_dir):
        raise MsgError("out folder is a symlink: %s" % new_dir)
    if not os.path.isdir(new_dir):
        raise MsgError("no out/new folder at %s (call init_msgdir first)" % new_dir)
    note_id = new_note_id()
    body = {**payload, "id": note_id, "t": _now_t(), "kind": kind}
    safe_write(os.path.join(new_dir, "%s.json" % note_id), _json_bytes(body))
    return note_id


def list_out(M) -> list[str]:
    """The pending ids in out/new/, sorted by name: only files whose whole
    name matches the note grammar (\\d+-[0-9a-f]{6}\\.json) -- a leftover .tmp
    is none of our business -- and symlinks are skipped, never listed."""
    new_dir = os.path.join(M, OUT_DIR, NEW)
    if _is_link(new_dir):
        return []
    try:
        names = os.listdir(new_dir)
    except OSError:
        return []
    out = []
    for name in sorted(names):
        if not NOTE_FILE_RE.fullmatch(name):
            continue
        path = os.path.join(new_dir, name)
        if _is_link(path) or not os.path.isfile(path):
            continue
        out.append(name[:-len(".json")])
    return out


def take_out(M, note_id: str) -> dict | None:
    """Claim one queued item: read the body, then move the file out of new/
    into taken/ (os.replace with the move_note retry). None when it is gone
    -- the loser of a race against another taker -- or unreadable; an
    unreadable or unparsable file is moved to taken/ too, so nobody reads it
    again. A file whose body was read but that cannot be moved away is a
    broken layout, not a race lost: MsgError, never a quiet None that leaves
    the item pending for the caller to spin on."""
    _check_note_id(note_id)
    new_dir = os.path.join(M, OUT_DIR, NEW)
    path = os.path.join(new_dir, "%s.json" % note_id)
    dst = os.path.join(M, OUT_DIR, TAKEN, "%s.json" % note_id)
    body = None
    if not _is_link(new_dir):
        body = _read_json_dict(path, _MAX_OUT)
    if body is None:
        with contextlib.suppress(OSError):
            move_note(path, dst)        # unreadable: move it aside all the same
        return None
    try:
        moved = move_note(path, dst)
    except FileNotFoundError:
        # move_note retries only the Windows PermissionError, so ENOENT rides
        # out of it as a raise -- from a sibling's replace landing first (the
        # lost race) OR from a missing destination parent. Both are told apart
        # below by whether our source is still here, never assumed lost.
        moved = False
    if not moved:
        if not os.path.lexists(path):
            return None                  # gone: the lost race, answered None
        raise MsgError("cannot move %s into taken/" % path)
    return body


# ----------------------------------------------------------------- seen markers
def mark_ask_seen(M, note_id: str) -> None:
    """Create the empty marker M/seen/ask-<note_id>: the driver stamps it in
    forward_note BEFORE the ask is queued to stdin (spec 12.2), so a reply
    tool call can only answer an ask that was really delivered. Idempotent.
    MsgError for a malformed id, a symlink on the way, or a marker that
    cannot be written (the driver sweeps the note undelivered on that)."""
    _check_note_id(note_id)
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    seen = os.path.join(M, SEEN_DIR)
    if _is_link(seen):
        raise MsgError("seen folder is a symlink: %s" % seen)
    path = os.path.join(seen, "ask-%s" % note_id)
    if _is_link(path):
        raise MsgError("refusing to mark through a symlink: %s" % path)
    try:
        fd = os.open(path, _FLAGS, FILE_MODE)
    except FileExistsError:
        return                                    # already marked: the race is a
    except OSError as e:                          # no-op, not a failure
        raise MsgError("cannot mark %s (%s)" % (path, e)) from e
    os.close(fd)


def ask_seen(M, note_id: str) -> bool:
    """True when the marker of ask <note_id> exists -- the reply tool's proof
    that this agent was actually delivered that ask (spec 12.2). A malformed
    id or an unreadable marker is False, never a raise."""
    if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
        return False
    path = os.path.join(M, SEEN_DIR, "ask-%s" % note_id)
    return not _is_link(path) and os.path.isfile(path)


# ----------------------------------------------------------------- main count
def main_count(M) -> int:
    """Delivered main notes so far (seen/main-count, a decimal written by temp
    + rename, spec 11.3): the number an open question closes against. Missing
    or unreadable or unparseable reads as 0."""
    try:
        data = safe_read(os.path.join(M, SEEN_DIR, MAIN_COUNT_FILE), _MAX_COUNT)
    except MsgError:
        return 0
    try:
        return int(data.decode("utf-8", "replace").strip())
    except ValueError:
        return 0


def bump_main_count(M) -> int:
    """One more delivered main note (the driver's _deliver echo and the
    until-done drain call this, spec 12.2): main_count + 1, landed temp +
    rename, and the new number back."""
    count = main_count(M) + 1
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    safe_write(os.path.join(M, SEEN_DIR, MAIN_COUNT_FILE),
               str(count).encode("ascii"))
    return count


# ----------------------------------------------------------------- open question
def open_question(M) -> dict | None:
    """The agent's open ask_main question: the parsed {"q_id","main_seq"} from
    M/open-question.json while the main-note count has not passed main_seq
    (main_count(M) <= main_seq, spec 11.3) -- once a main note lands after the
    question, the answer is on its way as a note and the question is closed.
    None when absent, unreadable, malformed or closed."""
    rec = _read_json_dict(os.path.join(M, OPEN_QUESTION_FILE), _MAX_STATE)
    if rec is None:
        return None
    q_id = rec.get("q_id")
    main_seq = rec.get("main_seq")
    if not isinstance(q_id, str) or not NOTE_ID_RE.fullmatch(q_id):
        return None
    if isinstance(main_seq, bool) or not isinstance(main_seq, int):
        return None
    if main_count(M) <= main_seq:
        return rec
    return None


def set_open_question(M, q_id: str, main_seq: int) -> None:
    """Record the question this agent just queued (the msg_mcp server calls it
    with the id write_out returned and the main_count at call time, spec 12.4):
    {"q_id","main_seq"} at M/open-question.json, temp + rename. MsgError for a
    malformed id, a non-int seq, or a write the layout refuses."""
    _check_note_id(q_id)
    if isinstance(main_seq, bool) or not isinstance(main_seq, int):
        raise MsgError("main_seq must be an int, not %r" % (main_seq,))
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    safe_write(os.path.join(M, OPEN_QUESTION_FILE),
               _json_bytes({"q_id": q_id, "main_seq": main_seq}))


def clear_open_question(M) -> None:
    """The question is no longer open (the relay dropped it, so the agent may
    ask again, spec 12.5): the file goes -- a symlinked one goes too (unlink
    removes the link, never its target) -- and no error ever rides out of
    housekeeping."""
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(M, OPEN_QUESTION_FILE))


# ----------------------------------------------------------------- replies
def publish_reply(M, note_id: str, text: str) -> bool:
    """Publish the one reply to ask <note_id> (spec 12.2): write the whole
    body {"note_id","t","source","text"} to replies/.<note_id>.<pid>.tmp and
    fsync it, then os.link it onto replies/<note_id>.json -- the link fails
    with FileExistsError when a reply already exists (the loser of a race or
    a retried tool call), and readers never see a partial file. True when we
    published, False when it was already answered; the tmp always goes. On
    Windows (no reliable hard links) the claim is instead an O_EXCL lock file
    replies/.<note_id>.lock plus a rename onto a name checked absent.
    MsgError only for a malformed id or a folder/write the layout refuses."""
    _check_note_id(note_id)
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    replies = os.path.join(M, REPLIES_DIR)
    if _is_link(replies):
        raise MsgError("replies folder is a symlink: %s" % replies)
    if not os.path.isdir(replies):
        raise MsgError("no replies folder at %s (call init_msgdir first)" % replies)
    tmp = os.path.join(replies, ".%s.%d.tmp" % (note_id, os.getpid()))
    final = os.path.join(replies, "%s.json" % note_id)
    body = {"note_id": note_id, "t": _now_t(), "source": SHARE_SOURCE, "text": text}
    try:
        fd = os.open(tmp, _FLAGS, FILE_MODE)
    except FileExistsError:
        # a stale tmp (this pid's earlier crash, pid reuse, or a planted
        # name) is NOT the "already answered" verdict -- that verdict be-
        # longs to the link below alone. The spec's write-the-tmp step
        # simply clears the way and writes.
        _discard(tmp)
        try:
            fd = os.open(tmp, _FLAGS, FILE_MODE)
        except OSError as e:
            raise MsgError("cannot create %s (%s)" % (tmp, e)) from e
    except OSError as e:
        raise MsgError("cannot create %s (%s)" % (tmp, e)) from e
    try:
        os.write(fd, _json_bytes(body))
        os.fsync(fd)
    except OSError as e:
        _discard(tmp)
        raise MsgError("cannot write %s (%s)" % (tmp, e)) from e
    finally:
        os.close(fd)
    if os.name == "nt":
        # no hard links we may rely on: the O_EXCL lock beside the final name
        # is the claim, the rename lands what the lock holder wrote (spec 12.2)
        lock = os.path.join(replies, ".%s.lock" % note_id)
        try:
            fd = os.open(lock, _FLAGS, FILE_MODE)
        except FileExistsError:
            _discard(tmp)
            return False
        except OSError as e:
            _discard(tmp)
            raise MsgError("cannot lock %s (%s)" % (lock, e)) from e
        os.close(fd)
        try:
            if os.path.lexists(final):
                return False                       # already answered
            try:
                os.rename(tmp, final)
            except OSError as e:
                raise MsgError("cannot publish %s (%s)" % (final, e)) from e
            return True
        finally:
            _discard(tmp)                          # gone by the rename, if it worked
            _discard(lock)                         # the lock is held, not kept
    try:
        os.link(tmp, final)
    except FileExistsError:
        _discard(tmp)
        return False
    except OSError as e:
        _discard(tmp)
        raise MsgError("cannot publish %s (%s)" % (final, e)) from e
    _discard(tmp)
    return True


def read_reply(M, note_id: str) -> dict | None:
    """The parsed reply body ({"note_id","t","source","text"}) for ask
    <note_id>, or None: absent, symlinked, oversized, unparsable -- --ask
    polls this, so it answers, never raises."""
    if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
        return None
    return _read_json_dict(os.path.join(M, REPLIES_DIR, "%s.json" % note_id),
                           _MAX_REPLY)


def sweep_stale_tmp(M, older_than: float = 3600) -> int:
    """Remove replies/.*.tmp files older than `older_than` seconds (the next
    reply call and run end sweep, spec 12.2); a crashed publish left one, a
    running one is younger than the bound. Returns the number removed; only
    our own regular .tmp files are ever touched, and any other problem leaves
    the file for the next sweep."""
    replies = os.path.join(M, REPLIES_DIR)
    if _is_link(replies):
        return 0
    try:
        names = os.listdir(replies)
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for name in names:
        if not (name.startswith(".") and name.endswith(".tmp")):
            continue
        path = os.path.join(replies, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or now - info.st_mtime <= older_than:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed += 1
    return removed


# ----------------------------------------------------------------- share state
def share_state(M) -> dict:
    """The agent's share admission state (M/share-state.json): the tool's own
    per-phase counts and duplicate hashes, kept on disk so a retry or a
    review round that starts a new server sees the same state (spec 11.4).
    Absent or unparsable reads as the fresh default."""
    rec = _read_json_dict(os.path.join(M, SHARE_STATE_FILE), _MAX_STATE)
    if rec is None:
        return {"count": {}, "seen_texts": {}}
    return rec


def save_share_state(M, state: dict) -> None:
    """Land the share state (temp + os.replace, 0600). MsgError for a non-dict
    or a write the layout refuses."""
    if not isinstance(state, dict):
        raise MsgError("share state must be a dict, not %s" % type(state).__name__)
    if _is_link(M):
        raise MsgError("msg dir is a symlink: %s" % M)
    safe_write(os.path.join(M, SHARE_STATE_FILE), _json_bytes(state))


# ----------------------------------------------------------------- findings
def findings_append(run_dir: str, pid: str, rec: dict) -> None:
    """Append one admitted finding as one JSON line to
    RUN/msg/_phases/<pid>/findings.jsonl (the relay's persistence, spec 11.4),
    creating the phase folder at 0700: ONE os.write on an append fd (the
    notes.append_jsonl rule), so two relays never interleave half a line.
    MsgError for a non-dict or a path the layout refuses."""
    if not isinstance(rec, dict):
        raise MsgError("a findings record must be a dict, not %s"
                       % type(rec).__name__)
    folder = phase_dir(run_dir, pid)
    for path in (os.path.join(run_dir, MSG_DIR),
                 os.path.join(run_dir, MSG_DIR, PHASES_DIR), folder):
        if _is_link(path):
            raise MsgError("phase folder is a symlink: %s" % path)
    try:
        for path in (os.path.join(run_dir, MSG_DIR),
                     os.path.join(run_dir, MSG_DIR, PHASES_DIR), folder):
            os.makedirs(path, mode=DIR_MODE, exist_ok=True)
            _chmod_dir(path)
    except OSError as e:
        raise MsgError("cannot build phase folder %s (%s)" % (folder, e)) from e
    line = _json_bytes(rec)
    try:
        fd = os.open(os.path.join(folder, FINDINGS_FILE),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                     FILE_MODE)
    except OSError as e:
        raise MsgError("cannot open the findings log (%s)" % e) from e
    try:
        os.write(fd, line)
    except OSError as e:
        raise MsgError("cannot append to the findings log (%s)" % e) from e
    finally:
        os.close(fd)


def findings_read(run_dir: str, pid: str) -> list[dict]:
    """The phase's findings in append order (the backlog a retry or a resume
    replays, spec 11.4): a missing file or folder is [], a line that does not
    parse (the tail a reader caught mid-append) or is not an object is
    skipped, never raised."""
    folder = phase_dir(run_dir, pid)
    path = os.path.join(folder, FINDINGS_FILE)
    if _is_link(folder) or _is_link(path):
        return []
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    out = []
    for raw in data.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
