"""Notes for running local agents: addresses, inbox files, the log, the sender CLI.

Phase 1 lets the main session steer ONE running agent: a note is a plain user message
the agent sees at its next model call. This module owns everything note-shaped; the
driver (lib/stream_driver.py) forwards what lands here, and phases 2 and 3 reuse it.

Addresses (spec 4.1) are `<run id>` or `<run id>/<unit>`:  the run id is a 12-hex watch
id, the unit name starts alphanumeric (like lib/swarm.py's _SAFE_NAME and lib/watch.py's
UNIT_NAME) and allows [A-Za-z0-9._-] for up to 64 characters, so `.` and `..` cannot
match. `--send` resolves an address through the watch registry pointer
($XDG_CACHE_HOME/qwen-agent/watch/<id>.json, lib/watch.py) to the agent's inbox:

  <watch_dir>/<id>.inbox/          a single-shot or sweep-batch run (kind "agent")
  <RUN>/inbox/                     an until-done loop (kind "until-done")
  <RUN>/inbox/<unit>/              one swarm or research unit (kind "swarm"/"research")

Inbox layout (spec 4.2): `new/` pending, `delivered/`, `undelivered/`, `notes.jsonl`
(the log for THIS agent only), `.closed` (sentinel: no more notes accepted). A note is
one file `new/<id>.json`, `id = <t_ns>-<rand6 hex>` (the stem sorts by time), body
{"id","t","from","to","kind","text"} plus "role"/"paths" for kind "share" and optional
"reply_to"/"hop" (reserved; ignored). `kind` is one of KINDS (note|ask|share|harness);
a body without one is a phase 1 "note", and `from` pairs with it ("main" for note and
ask, "harness" for harness, "unit <name>" for share). The ONE text the model sees is
built by render_note (the stream driver and the between-rounds drain both call it), and
imitates_header is the tool-side guard against forged headers in model-written fields.
`t` is a Unix epoch float rounded to 3 decimals everywhere, like
swarm_engine/events.py. Files land by tmp + os.replace; readers match only
^\\d+-[0-9a-f]{6}\\.json$; reads AND writes refuse symlinks (islink checks plus
O_NOFOLLOW where available); directories are 0700 and files 0600 on POSIX.

The log line is {"t","id","to","from","state","reason"?,"len"} with state in
queued | delivered | deferred | undelivered, appended with ONE os.write on an
O_WRONLY|O_CREAT|O_APPEND|O_BINARY fd (mode 0600), each line under 4 KiB -- the note
text is never in the log. `close_inbox(inbox, reason)` is the owner's end-of-run sweep
(4.7): idempotent, it writes `.closed`, moves every pending `new/` file to
`undelivered/` and logs each `undelivered: <reason>`; it is the only place `.closed`
is written. `take_pending(inbox, reason)` is the supervisor's between-rounds drain
(K5): validated pending notes in time order, moved to `delivered/` and logged
`delivered: <reason>`.

Text limits: non-empty after strip, at most 8,000 Unicode characters and 32 KiB UTF-8.

CLI (spec 4.4/K9 and 11.2; qwen-agent.sh dispatches `--send`/`--notes`/`--ask`/
`--answer` here before preflight):

  python lib/notes.py send ADDRESS [--wait SECONDS] [-f FILE | -f -] [--] TEXT
  python lib/notes.py notes ADDRESS
  python lib/notes.py ask ADDRESS [--wait SECONDS] [-f FILE | -f -] [--] TEXT
  python lib/notes.py answer ADDRESS NOTE_ID [--wait SECONDS]

`send` prints the note id on stdout and exits 0; exit 2 is usage (bad grammar, empty
or oversized text, bad option combination), exit 3 is unavailable (no such run, run or
unit not running, closed inbox, dead pid). `--wait SECONDS` (1..3600) prints the id and
the outcome on the next line: `delivered at HH:MM:SS` exit 0, `still queued` exit 10,
`undelivered: <reason>` exit 11, `deferred to the next round` exit 12; it returns early
with 11 when the target pid dies (`undelivered: pid_dead`) or `.closed` appears (it
re-reads the log until the sweep's line lands and reports that reason; a claimed-but-
never-swept inbox reports `undelivered: closed`). `notes` prints one row per note
(latest state) as `HH:MM:SS  <state>[: reason]  <id>  <preview>`, `no notes` for none,
exit 0 for a run whose pointer exists (running or ended), exit 3 for none; the preview
is the first 80 characters with control characters shown as spaces.

`ask` (spec 11.2) is `send` with kind `ask`: the same grammar and the same
refusals, but it queues `kind="ask"`, prints `asked <id>`, and --wait SECONDS (1..3600;
absent means 600) polls the agent's msg dir (single-shot `<watch_dir>/<id>.msg`,
until-done `<RUN>/msg`, swarm/research unit `<RUN>/msg/units/<unit>`) once a second
for its `reply` tool answer: `answer from <address> (HH:MM:SS):` then the text (over
4000 characters is printed cut, with a last line `... (cut at 4000 characters)`),
exit 0; `the agent ended without answering` on stderr and 11 when the inbox closes
with no reply; `no answer yet: <id>` on stderr and 10 when the wait runs out.
`answer ADDRESS NOTE_ID [--wait SECONDS]` (0..3600, absent means 0: one look) fetches
a reply later -- even after the run ended (resolve with require_running=False: no
running, pid or closed checks): an id with neither an inbox note (in `delivered/`,
`new/` or `undelivered/`) nor a reply is `no such ask: <id>` and 3; a reply prints as
above and exits 0; no reply waits like --ask (closed 11, wait over 10).
"""
import contextlib
import json
import os
import re
import sys
import time
import unicodedata

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import watch  # noqa: E402

# ---------------------------------------------------------------- grammar and limits
ADDRESS_RE = re.compile(r"^[0-9a-f]{12}(/[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
NOTE_FILE_RE = re.compile(r"^\d+-[0-9a-f]{6}\.json$")
NOTE_ID_RE = re.compile(r"^\d+-[0-9a-f]{6}$")
KINDS = ("note", "ask", "share", "harness")          # every note names exactly one
SHARE_FROM_RE = re.compile(r"^unit [A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_PATHS = 10                                       # a share carries at most 10 ...
MAX_PATH_CHARS = 200                                 # ... paths of at most 200 chars
HEADER_PREFIXES = ("[Note from", "[Question from", "[Shared finding from")
MAX_CHARS = 8000                       # Unicode characters of one note's text
MAX_BYTES = 32 * 1024                  # and its UTF-8 size
MAX_LINE = 4096                        # bytes, newline included: every log line stays under it
DIR_MODE = 0o700
FILE_MODE = 0o600
NEW, DELIVERED, UNDELIVERED = "new", "delivered", "undelivered"
CLOSED = ".closed"
LOG_FILE = "notes.jsonl"
STATES = ("queued", "delivered", "deferred", "undelivered")
SENDER = "main"                        # phase 1: every note comes from the main session

EXIT_OK, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 2, 3
EXIT_STILL_QUEUED, EXIT_UNDELIVERED, EXIT_DEFERRED = 10, 11, 12

ASK_WAIT_DEFAULT = 600                       # --ask with no --wait: ten minutes
ASK_POLL = 1                                 # seconds between read_reply polls (4.3)
REPLY_MAX = 4000                             # answer text printed whole, else cut (4.3)

_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) \
    | getattr(os, "O_NOFOLLOW", 0)


class NotesError(Exception):
    """A refusal the CLI turns into its exit code: 2 usage, 3 unavailable."""

    def __init__(self, message, code=EXIT_UNAVAILABLE):
        super().__init__(message)
        self.code = code


def _now_t():
    """The one stamp format (spec 4.2): a Unix epoch float rounded to 3 decimals."""
    return round(time.time(), 3)


def _is_link(path):
    with contextlib.suppress(OSError):
        return os.path.islink(path)
    return False


def _reject_link(path, what):
    if _is_link(path):
        raise NotesError("%s is a symlink: %s" % (what, path), EXIT_UNAVAILABLE)


def _chmod_dir(path):
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, DIR_MODE)


# ---------------------------------------------------------------- ids, times, text
def new_note_id():
    """<t_ns>-<rand6 hex>: the stem sorts by time, the random suffix separates two
    notes queued in the same nanosecond tick (a coarse clock on Windows)."""
    return "%d-%s" % (time.time_ns(), os.urandom(3).hex())


def fmt_clock(t):
    """HH:MM:SS in local time, the format both the forwarded header and --wait use.
    A non-numeric stamp (a hand-edited log line) reads as now, never a TypeError."""
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t))


def validate_text(text):
    """A note's text as --send must accept it: a string, non-empty after strip, at
    most MAX_CHARS Unicode characters and MAX_BYTES UTF-8 bytes. NotesError code 2."""
    if not isinstance(text, str):
        raise NotesError("note text must be a string", EXIT_USAGE)
    if not text.strip():
        raise NotesError("note text is empty after strip", EXIT_USAGE)
    if len(text) > MAX_CHARS:
        raise NotesError("note text is %d characters; the limit is %d"
                         % (len(text), MAX_CHARS), EXIT_USAGE)
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise NotesError("note text is %d UTF-8 bytes; the limit is %d"
                         % (size, MAX_BYTES), EXIT_USAGE)
    return text


def preview(text):
    """The --notes/report.md preview (K5/P8): first 80 characters, newlines and other
    control characters shown as spaces."""
    if not isinstance(text, str):
        return ""
    return "".join(ch if " " <= ch and ch != "\x7f" else " " for ch in text[:80])


# ---------------------------------------------------------------- the log
def _line_bytes(rec):
    # errors="replace": a field may carry a lone surrogate (a non-UTF-8 argv byte)
    return (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")


def _fit_line(rec):
    """The whole line as bytes, cut until it fits under MAX_LINE (the events.py rule):
    long string fields lose their tails, and a record that will not fit even cut short
    becomes a minimal one rather than a long one."""
    line = _line_bytes(rec)
    if len(line) < MAX_LINE:
        return line
    rec = dict(rec)
    for key in ("reason", "to", "from", "id"):
        value = rec.get(key)
        if isinstance(value, str) and len(value) > 8:
            rec[key] = value[:8] + "...[cut]"
            line = _line_bytes(rec)
            if len(line) < MAX_LINE:
                return line
    return _line_bytes({"t": rec.get("t"), "id": rec.get("id"), "to": "main",
                        "from": "main", "state": rec.get("state", "undelivered"),
                        "len": 0, "truncated": True})


def append_jsonl(path, rec):
    """Append one record as one whole line: ONE os.write on an append fd, created 0600,
    O_NOFOLLOW where the platform has it (a log file is never followed through a
    symlink). Best effort at the caller's level, honest at ours: an OSError means the
    line did not land."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd = os.open(path, _FLAGS, FILE_MODE)
    try:
        os.write(fd, _fit_line(rec))
    finally:
        os.close(fd)


def append_log(inbox, rec):
    """Append one line to the agent's own notes.jsonl."""
    append_jsonl(os.path.join(inbox, LOG_FILE), rec)


def log_note(inbox, note_id, to_addr, sender, state, reason=None, text_len=None):
    """One log line: {"t","id","to","from","state","reason"?,"len"}. The text is never
    here; `len` is len(text) in Unicode characters."""
    rec = {"t": _now_t(), "id": note_id, "to": to_addr, "from": sender,
           "state": state, "len": int(text_len or 0)}
    if reason:
        rec["reason"] = str(reason)
    if rec["state"] not in STATES:
        raise NotesError("unknown note state %r" % state, EXIT_USAGE)
    append_log(inbox, rec)
    return rec


def read_log(inbox):
    """Every readable log line, oldest first; a line that does not parse (the tail a
    reader caught mid-append) is skipped, never raised."""
    try:
        with open(os.path.join(inbox, LOG_FILE), "rb") as fh:
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


def latest_states(inbox):
    """({id: last line that named it}, [ids in first-seen (time) order]): the log is
    an append-only trail, so the LAST line for an id is its state."""
    states, order = {}, []
    for rec in read_log(inbox):
        note_id = rec.get("id")
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            continue
        if note_id not in states:
            order.append(note_id)
        states[note_id] = rec
    return states, order


# ---------------------------------------------------------------- note files
def validate_note_body(body, stem):
    """A note file as the reader must accept it: a JSON object, `id` matching the name
    stem, `text` a string within the limits, and a consistent kind/from pairing --
    `kind` one of KINDS (a body without one is a phase 1 "note"), `from` "main" for
    note and ask, "harness" for harness, "unit <name>" (SHARE_FROM_RE) for share; a
    share's `role` matches ROLE_RE and its `paths`, when present, are at most
    MAX_PATHS strings of at most MAX_PATH_CHARS characters; a `reply_to`, when
    present, is a note id. Raises NotesError on anything else (the caller sweeps the
    file invalid_note)."""
    if not isinstance(body, dict):
        raise NotesError("note is not a JSON object", EXIT_UNAVAILABLE)
    if body.get("id") != stem:
        raise NotesError("note id %r does not match its file name %r"
                         % (body.get("id"), stem), EXIT_UNAVAILABLE)
    if not NOTE_ID_RE.fullmatch(stem):
        raise NotesError("note id %r is malformed" % stem, EXIT_UNAVAILABLE)
    try:
        validate_text(body.get("text"))
    except NotesError as e:
        raise NotesError(str(e), EXIT_UNAVAILABLE) from e
    kind = body.get("kind")
    if kind is None:
        kind = "note"                                # a phase 1 file is a plain note
    elif kind not in KINDS:
        raise NotesError("note kind %r is not one of %s"
                         % (kind, ", ".join(KINDS)), EXIT_UNAVAILABLE)
    sender = body.get("from")
    if kind in ("note", "ask"):
        if sender != "main":
            raise NotesError("%s note from %r: the sender must be \"main\""
                             % (kind, sender), EXIT_UNAVAILABLE)
    elif kind == "harness":
        if sender != "harness":
            raise NotesError("harness note from %r: the sender must be \"harness\""
                             % (sender,), EXIT_UNAVAILABLE)
    else:                                            # share: a unit's finding
        if not isinstance(sender, str) or not SHARE_FROM_RE.fullmatch(sender):
            raise NotesError("share note from %r: the sender must be a unit "
                             "(\"unit <name>\")" % (sender,), EXIT_UNAVAILABLE)
        role = body.get("role")
        if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
            raise NotesError("share role %r is malformed" % (role,), EXIT_UNAVAILABLE)
        paths = body.get("paths")
        if paths is not None:
            if not isinstance(paths, list) or len(paths) > MAX_PATHS:
                raise NotesError("share paths must be a list of at most %d strings"
                                 % MAX_PATHS, EXIT_UNAVAILABLE)
            for path in paths:
                if not isinstance(path, str) or len(path) > MAX_PATH_CHARS:
                    raise NotesError("share path %r is not a string of at most %d "
                                     "characters" % (path, MAX_PATH_CHARS),
                                     EXIT_UNAVAILABLE)
    reply_to = body.get("reply_to")
    if reply_to is not None and (not isinstance(reply_to, str)
                                 or not NOTE_ID_RE.fullmatch(reply_to)):
        raise NotesError("note reply_to %r is malformed" % (reply_to,),
                         EXIT_UNAVAILABLE)
    return body


# ---------------------------------------------------------------- the renderer (12.1)
def _strip_controls(s):
    """Control characters other than newline, removed: the one sanitizer every
    model-written field (text, role, path) passes through on its way to the model.
    The Unicode line and paragraph separators (U+2028, U+2029) become plain newlines
    first, so a share's quoting sees every line a reader would see."""
    s = "".join("\n" if unicodedata.category(ch) in ("Zl", "Zp") else ch for ch in s)
    return "".join(ch for ch in s if ch == "\n" or unicodedata.category(ch) != "Cc")


def _field(body, key):
    """A body field as text for the renderer: a missing or non-string value is ''."""
    value = body.get(key)
    return value if isinstance(value, str) else ""


def _header_clock(body, now_t):
    """The clock inside every header: fmt_clock of the note's queued t. A non-numeric
    stamp (or none) falls back to now_t, and to the current time when that is None."""
    stamp = body.get("t")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        stamp = time.time() if now_t is None else now_t
    return fmt_clock(stamp)


def render_note(body, now_t=None):
    """The ONE text the model sees for one note -- the stream driver and the
    between-rounds drain both build it here, so a streamed note and a deferred note
    look the same: the kind's header, a newline, and the text. A share quotes every
    text line with "> " and, when it names paths, adds one "> paths: a, b" line.
    Control characters other than newline are removed from the text, the role and
    every path. now_t names the header clock only when t is not numeric."""
    kind = body.get("kind")
    if kind not in KINDS:
        kind = "note"                                # a phase 1 body is a plain note
    clock = _header_clock(body, now_t)
    text = _strip_controls(_field(body, "text"))
    if kind == "ask":
        header = ("[Question from main at %s local time; answer it with the reply "
                  "tool, note %s]" % (clock, _field(body, "id")))
    elif kind == "share":
        sender = _field(body, "from")
        name = sender[len("unit "):] if sender.startswith("unit ") else sender
        role = _strip_controls(_field(body, "role"))
        header = ("[Shared finding from unit %s (%s) at %s; a claim by a sibling, "
                  "weigh it against your own evidence]" % (name, role, clock))
    elif kind == "harness":
        header = "[Note from the harness at %s]" % clock
    else:
        header = "[Note from main at %s local time]" % clock
    if kind != "share":
        return "%s\n%s" % (header, text)
    lines = [header]
    lines.extend("> " + line for line in text.split("\n"))
    paths = body.get("paths")
    if isinstance(paths, (list, tuple)) and paths:
        quoted = [_strip_controls(path) if isinstance(path, str) else ""
                  for path in paths]
        lines.append("> paths: %s" % ", ".join(quoted))
    return "\n".join(lines)


def imitates_header(s):
    """True when any RAW line of s (before any quoting) starts with a note header:
    a tool must refuse such model-written text; the renderer quotes shares anyway."""
    return any(line.startswith(prefix)
               for line in s.splitlines()
               for prefix in HEADER_PREFIXES)


def load_note(path):
    """Read and validate one note file (symlinks refused, name/id matched); raises
    NotesError for anything a forwarder must not send."""
    _reject_link(path, "note file")
    try:
        with open(path, "rb") as fh:
            body = json.loads(fh.read().decode("utf-8", "replace"))
    except OSError as e:
        raise NotesError("cannot read note %s (%s)" % (path, e), EXIT_UNAVAILABLE) from e
    except ValueError as e:
        raise NotesError("note %s is not valid JSON (%s)" % (path, e), EXIT_UNAVAILABLE) from e
    return validate_note_body(body, os.path.basename(path)[:-len(".json")])


def move_note(src, dst, retries=4):
    """os.replace a note between state folders, retried with short backoff on the
    Windows PermissionError a still-open handle causes; False when it never moved."""
    delay = 0.05
    for _ in range(retries):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
    try:
        os.replace(src, dst)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- the inbox
def init_inbox(inbox):
    """Create an inbox and its three state folders (0700), and remove a stale `.closed`
    an interrupted earlier run left behind (P3). Every owner uses this; it is
    idempotent. Symlinks (the inbox itself or any state folder) are refused."""
    _reject_link(inbox, "inbox")
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError as e:
        raise NotesError("cannot create inbox %s (%s)" % (inbox, e), EXIT_UNAVAILABLE) from e
    _chmod_dir(inbox)
    for name in (NEW, DELIVERED, UNDELIVERED):
        path = os.path.join(inbox, name)
        _reject_link(path, "inbox folder")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            raise NotesError("cannot create %s (%s)" % (path, e), EXIT_UNAVAILABLE) from e
        _chmod_dir(path)
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(inbox, CLOSED))
    return inbox


def is_closed(inbox):
    """True once the owner swept the inbox: no more notes are accepted. lexists: even
    a dangling or symlinked sentinel counts -- nothing is ever written through it."""
    return os.path.lexists(os.path.join(inbox, CLOSED))


def queue_note(inbox, to_addr, text, sender=SENDER, reply_to=None, hop=None,
               kind="note", role=None, paths=None):
    """Queue one note: validate the text, write new/<id>.json.tmp with O_EXCL|O_NOFOLLOW
    and os.replace it into place, log `queued`. Returns the id. Refused (NotesError 3)
    when the inbox has no `new/` (its owner never made one) or is closed. The body
    carries its `kind`; `role` and `paths` ride only on a share (spec 12.1)."""
    new_dir = os.path.join(inbox, NEW)
    _reject_link(inbox, "inbox")
    _reject_link(new_dir, "inbox folder")
    if not os.path.isdir(new_dir):
        raise NotesError("no inbox at %s" % inbox, EXIT_UNAVAILABLE)
    if is_closed(inbox):
        raise NotesError("inbox closed: %s" % inbox, EXIT_UNAVAILABLE)
    validate_text(text)
    note_id = new_note_id()
    body = {"id": note_id, "t": _now_t(), "from": sender, "to": to_addr,
            "kind": kind, "text": text}
    if kind == "share":
        body["role"] = role
        body["paths"] = paths
    if reply_to is not None:
        body["reply_to"] = reply_to
    if hop is not None:
        body["hop"] = hop
    final = os.path.join(new_dir, "%s.json" % note_id)
    tmp = "%s.tmp" % final
    data = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8", "replace")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, final)
    log_note(inbox, note_id, to_addr, sender, "queued", text_len=len(text))
    return note_id


def list_new(inbox):
    """Pending note files in time order: names matching ^\\d+-[0-9a-f]{6}\\.json$ only
    (a leftover .tmp is none of our business), symlinks skipped silently -- a link is
    never followed or forwarded, and never touched either."""
    new_dir = os.path.join(inbox, NEW)
    if _is_link(new_dir):
        return []                                    # reads refuse symlinks: never
                                                     # list (or sweep) through a link
    try:
        names = sorted(os.listdir(new_dir))
    except OSError:
        return []
    out = []
    for name in names:
        if not NOTE_FILE_RE.fullmatch(name):
            continue
        path = os.path.join(new_dir, name)
        if _is_link(path):
            continue
        if os.path.isfile(path):
            out.append((path, name[:-len(".json")]))
    return out


def pending_count(inbox):
    """How many pending note files an inbox holds right now (close_policy reads it)."""
    return len(list_new(inbox))


def note_meta(path):
    """A note file's (to, len) for its log line, read leniently: an unreadable or
    broken file logs as ("", 0) -- the log says what it can, never a stack trace."""
    try:
        with open(path, "rb") as fh:
            body = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return "", 0
    if not isinstance(body, dict):
        return "", 0
    to_addr = body.get("to") if isinstance(body.get("to"), str) else ""
    text_len = len(body["text"]) if isinstance(body.get("text"), str) else 0
    return to_addr, text_len


def sweep_undelivered(inbox, note_id, reason, path):
    """One note out of `new/` and into `undelivered/`, logged with its reason: the
    move and the log line together, so no caller half-does it."""
    to_addr, text_len = note_meta(path)
    dst = os.path.join(inbox, UNDELIVERED, "%s.json" % note_id)
    moved = move_note(path, dst)
    log_note(inbox, note_id, to_addr, SENDER, "undelivered", reason=reason,
             text_len=text_len)
    return moved


def note_text(inbox, note_id):
    """A note's text for the --notes preview, read from new/, delivered/ or
    undelivered/ -- whatever folder still holds it. '' when no readable file does."""
    if not NOTE_ID_RE.fullmatch(note_id or ""):
        return ""
    for folder in (NEW, DELIVERED, UNDELIVERED):
        path = os.path.join(inbox, folder, "%s.json" % note_id)
        if _is_link(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                body = json.loads(fh.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        if isinstance(body, dict) and isinstance(body.get("text"), str):
            return body["text"]
    return ""


def close_inbox(inbox, reason):
    """The owner's end-of-run sweep (4.7) -- the ONE place `.closed` is written.
    Idempotent: the exclusive create of `.closed` is the claim, so the second caller
    (any reason) sweeps nothing and logs nothing. The claim happens BEFORE the sweep,
    so a note sent between the driver's exit and here is swept, never left queued.
    Returns the ids it moved to undelivered/."""
    if not os.path.isdir(inbox) or _is_link(inbox):
        return []                                    # writes refuse symlinks: an inbox
                                                     # that is a link is never swept
    closed_path = os.path.join(inbox, CLOSED)
    try:
        fd = os.open(closed_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
    except FileExistsError:
        return []
    os.close(fd)
    swept = []
    for path, note_id in list_new(inbox):
        dst = os.path.join(inbox, UNDELIVERED, "%s.json" % note_id)
        if not move_note(path, dst):
            continue
        to_addr, text_len = "", 0
        try:
            with open(dst, "rb") as fh:
                body = json.loads(fh.read().decode("utf-8", "replace"))
            if isinstance(body, dict):
                to_addr = body.get("to") if isinstance(body.get("to"), str) else ""
                if isinstance(body.get("text"), str):
                    text_len = len(body["text"])
        except (OSError, ValueError):
            pass
        log_note(inbox, note_id, to_addr, SENDER, "undelivered",
                 reason=reason, text_len=text_len)
        swept.append(note_id)
    return swept


def take_pending(inbox, reason):
    """The supervisor's between-rounds drain (K5): every valid pending note in time
    order, moved to delivered/ and logged `delivered: <reason>`; an invalid file goes
    to undelivered/ with reason invalid_note instead. Returns the note bodies."""
    taken = []
    for path, note_id in list_new(inbox):
        try:
            body = load_note(path)
        except NotesError:
            _move_invalid(inbox, path, note_id)
            continue
        dst = os.path.join(inbox, DELIVERED, "%s.json" % note_id)
        if not move_note(path, dst):
            continue
        to_addr = body.get("to") if isinstance(body.get("to"), str) else ""
        log_note(inbox, note_id, to_addr, body.get("from") or SENDER, "delivered",
                 reason=reason, text_len=len(body["text"]))
        taken.append(body)
    return taken


def _move_invalid(inbox, path, note_id):
    """One unreadable file out of new/ and into undelivered/, logged invalid_note."""
    dst = os.path.join(inbox, UNDELIVERED, "%s.json" % note_id)
    with contextlib.suppress(OSError):
        move_note(path, dst)
    log_note(inbox, note_id, "", SENDER, "undelivered", reason="invalid_note", text_len=0)
    return dst


# ---------------------------------------------------------------- addresses (4.1)
def split_address(address):
    """(<run id>, <unit or None>) -- only for a syntactically valid address.
    NOTE_FILE_RE and friends stay private; this is the grammar's own splitter."""
    run_id, _, unit = address.partition("/")
    return run_id, (unit or None)


def valid_address(address):
    # fullmatch: the grammar is a whole-string validator -- re.match's "$" also
    # accepts a trailing "\n", and an address is never allowed newline garbage.
    return isinstance(address, str) and bool(ADDRESS_RE.fullmatch(address))


def _kernel32():
    """kernel32 for pid_alive, with the signatures of the calls it makes declared so a
    64-bit HANDLE is not truncated to a 32-bit int (restype defaults to c_int!). The
    use_last_error flag is what feeds ctypes.get_last_error() after a failed call. This
    is also the seam a test monkeypatches to drive the nt branch off Windows."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # noqa: N806 (API type)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def pid_alive(pid):
    """POSIX os.kill(pid, 0); Windows OpenProcess(SYNCHRONIZE) + WaitForSingleObject(h,
    0) via ctypes -- watch._alive returns True off POSIX, so the sender must judge for
    itself. The process object is signalled when the process exits: WAIT_TIMEOUT
    (0x102) says it runs, WAIT_OBJECT_0 (0) says it is gone, and WAIT_FAILED
    (0xFFFFFFFF) proves nothing; an OpenProcess NULL whose last error is 5
    (ERROR_ACCESS_DENIED) names a process we may not open, and only 87
    (ERROR_INVALID_PARAMETER) proves the pid away. Anything we cannot prove gone
    counts as alive."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True                                  # not a pid we may judge: watch's rule
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (OSError, OverflowError):             # no permission, exotic platform
            return True
        return True
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = _kernel32()                   # the test seam, never ctypes.windll
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
            if not handle:                           # NULL: the last error tells how far
                last_error = ctypes.get_last_error()
                if last_error == 5:                  # ERROR_ACCESS_DENIED: it exists
                    return True
                return last_error != 87              # 87 INVALID_PARAMETER: gone; else unknown
            try:
                result = kernel32.WaitForSingleObject(handle, 0)
                if result == 0x00000102:             # WAIT_TIMEOUT: still running
                    return True
                if result == 0:                      # WAIT_OBJECT_0: signalled, exited
                    return False
                return True                          # WAIT_FAILED or a surprise: not proved
            finally:
                kernel32.CloseHandle(handle)         # whenever a handle was opened
        except Exception:                            # any ctypes trouble: not proven gone
            return True
    return True


def _pointer(run_id):
    """The registry pointer for a run id, or None: read through lib/watch.py's own
    path rule so the address book stays one directory, not two."""
    path = os.path.join(watch.watch_dir(), "%s.json" % run_id)
    try:
        with open(path, "rb") as fh:
            pointer = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    return pointer if isinstance(pointer, dict) else None


def _unit_states(pointer):
    """{unit name: latest folded state} of a swarm/research run, off its events.jsonl
    through lib/watch.py's fold (unit_start says running; unit_done/cached/dropped
    say otherwise; a queued unit never spawned is not running either)."""
    run_path = pointer.get("run_path")
    if not isinstance(run_path, str) or not run_path:
        return {}
    try:
        with open(os.path.join(run_path, "events.jsonl"), "rb") as fh:
            data = fh.read()
    except OSError:
        return {}
    fold = watch.fold_events(data.decode("utf-8", "replace").splitlines())
    units = fold.get("units") or {}
    return {name: unit.get("state") for name, unit in units.items()
            if isinstance(unit, dict)}


def resolve_address(address, require_running=True):
    """An address to an inbox directory, resolved through the watch registry.
    Returns (inbox, pointer, unit). NotesError 2: malformed grammar, a swarm/research
    run id without /<unit> (the message lists the running units), a /<unit> on a run
    with no units. NotesError 3: no such run, run not running, unit not running
    (queued, finished, cached or skipped), closed inbox, dead pid. --notes passes
    require_running=False: the pointer existing is enough (the run may have ended)."""
    if not valid_address(address):
        raise NotesError("malformed address: %r" % address, EXIT_USAGE)
    run_id, unit = split_address(address)
    pointer = _pointer(run_id)
    if pointer is None:
        raise NotesError("no such run: %s" % run_id, EXIT_UNAVAILABLE)
    kind = pointer.get("kind")
    run_path = pointer.get("run_path")
    if kind == "agent":
        if unit:
            raise NotesError("run %s has no units (single-shot): %s" % (run_id, address),
                             EXIT_USAGE)
        inbox = os.path.join(watch.watch_dir(), "%s.inbox" % run_id)
    elif kind == "until-done":
        if unit:
            raise NotesError("run %s has no units (until-done): %s" % (run_id, address),
                             EXIT_USAGE)
        if not isinstance(run_path, str) or not run_path:
            raise NotesError("run %s has no run folder" % run_id, EXIT_UNAVAILABLE)
        inbox = os.path.join(run_path, "inbox")
    elif kind in ("swarm", "research"):
        units = _unit_states(pointer)
        if not unit:
            running = sorted(name for name, state in units.items() if state == "running")
            raise NotesError("run %s is a %s: address a running unit as <id>/<unit> "
                             "(running units: %s)" % (run_id, kind, ", ".join(running) or "none"),
                             EXIT_USAGE)
        if not units:
            raise NotesError("run %s has no units" % run_id, EXIT_USAGE)
        if unit not in units:
            raise NotesError("no such unit: %s" % address, EXIT_UNAVAILABLE)
        if require_running and units[unit] != "running":
            raise NotesError("unit %s is not running (state: %s): %s"
                             % (unit, units[unit], address), EXIT_UNAVAILABLE)
        if not isinstance(run_path, str) or not run_path:
            raise NotesError("run %s has no run folder" % run_id, EXIT_UNAVAILABLE)
        inbox = os.path.join(run_path, "inbox", unit)
    else:
        raise NotesError("unknown run kind %r for %s" % (kind, run_id), EXIT_UNAVAILABLE)
    if require_running:
        if pointer.get("end") is not None:
            raise NotesError("run not running (it ended): %s" % address, EXIT_UNAVAILABLE)
        if not pid_alive(pointer.get("pid")):
            raise NotesError("the run's pid %r is dead: %s" % (pointer.get("pid"), address),
                             EXIT_UNAVAILABLE)
        if not os.path.isdir(inbox):
            raise NotesError("no inbox for %s at %s" % (address, inbox), EXIT_UNAVAILABLE)
        if is_closed(inbox):
            raise NotesError("inbox closed: %s" % address, EXIT_UNAVAILABLE)
    return inbox, pointer, unit


# ---------------------------------------------------------------- the CLI (4.4, K9, P8)
USAGE_SEND = ("usage: qwen-agent --send ADDRESS [--wait SECONDS] "
              "[-f FILE | -f -] [--] TEXT")
USAGE_NOTES = "usage: qwen-agent --notes ADDRESS"
USAGE_ASK = ("usage: qwen-agent --ask ADDRESS [--wait SECONDS] "
             "[-f FILE | -f -] [--] TEXT")
USAGE_ANSWER = "usage: qwen-agent --answer ADDRESS NOTE_ID [--wait SECONDS]"


def _read_text_source(spec):
    """-f's operand: a file path, or `-` for stdin. Read bytes decoded leniently; the
    text itself is stored verbatim (validated after, never stripped)."""
    if spec == "-":
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") \
            else sys.stdin.read().encode("utf-8", "replace")
    else:
        try:
            with open(spec, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise NotesError("cannot read -f %s (%s)" % (spec, e), EXIT_USAGE) from e
    return data.decode("utf-8", "replace")


def _parse_send(argv):
    """The K9 grammar, hand-rolled so every wrong shape exits 2 with a reason:
    ADDRESS, then options (--wait SECONDS, -f FILE|-), then at most one TEXT (a `--`
    lets TEXT start with a dash, and options must come before it)."""
    wait = None
    file_spec = None
    positional = []
    rest = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if rest:
            positional.append(token)
        elif token == "--":
            rest = True
        elif token == "--wait":
            if len(positional) > 1:              # options must come before TEXT (K9)
                raise NotesError("--wait must come before TEXT", EXIT_USAGE)
            if i + 1 >= len(argv) or wait is not None:
                raise NotesError("--wait needs its seconds, once", EXIT_USAGE)
            try:
                wait = int(argv[i + 1])
            except ValueError:
                raise NotesError("--wait SECONDS must be an integer", EXIT_USAGE) from None
            if not 1 <= wait <= 3600:
                raise NotesError("--wait SECONDS must be 1..3600", EXIT_USAGE)
            i += 1
        elif token.startswith("--wait="):
            return _parse_send(argv[:i] + ["--wait", token[len("--wait="):]] + argv[i + 1:])
        elif token == "-f" or token.startswith("-f") and len(token) > 2:
            if len(positional) > 1:              # options must come before TEXT (K9)
                raise NotesError("-f must come before TEXT", EXIT_USAGE)
            if file_spec is not None:
                raise NotesError("-f may be given only once", EXIT_USAGE)
            if token == "-f":
                if i + 1 >= len(argv):
                    raise NotesError("-f needs FILE or -", EXIT_USAGE)
                file_spec = argv[i + 1]
                i += 1
            else:
                file_spec = token[2:]
        elif token.startswith("-") and len(token) > 1:
            raise NotesError("unknown option: %s (options come before TEXT)" % token,
                             EXIT_USAGE)
        else:
            positional.append(token)
        i += 1
    if not 1 <= len(positional) <= 2:
        raise NotesError("ADDRESS and at most one TEXT after the options "
                         "(got %d positional(s))" % len(positional), EXIT_USAGE)
    address = positional[0]
    if len(positional) == 2:
        if file_spec is not None:
            raise NotesError("exactly one of TEXT or -f, never both", EXIT_USAGE)
        text = positional[1]
    elif file_spec is not None:
        text = _read_text_source(file_spec)
    else:
        raise NotesError("no note text: give TEXT or -f FILE | -f -", EXIT_USAGE)
    return address, wait, text


def _wait_for_note(inbox, pointer, note_id, seconds):
    """--wait: poll the agent's log for this id until the deadline; early exits for a
    dead target pid and for a closing sweep (P8)."""
    deadline = time.time() + seconds
    while True:
        states, _ = latest_states(inbox)
        outcome = _outcome_of(states.get(note_id), inbox, note_id)
        if outcome is not None:
            return outcome
        if is_closed(inbox):
            # The owner writes the sweep's lines in the same breath as claiming
            # (close_inbox): re-read until one lands, so --wait reports the sweep's
            # reason (P8). Only a claimed-but-never-swept sentinel ends as "closed".
            sweep_deadline = min(time.time() + 2.0, deadline)
            while True:
                states, _ = latest_states(inbox)
                outcome = _outcome_of(states.get(note_id), inbox, note_id)
                if outcome is not None:
                    return outcome
                if time.time() >= sweep_deadline:
                    print("undelivered: closed")
                    return EXIT_UNDELIVERED
                time.sleep(0.05)
        if not pid_alive(pointer.get("pid")):
            print("undelivered: pid_dead")
            return EXIT_UNDELIVERED
        if time.time() >= deadline:
            print("still queued")
            return EXIT_STILL_QUEUED
        time.sleep(0.2)


def _outcome_of(rec, inbox, note_id):
    """The (print, exit) pair for one log line, or None while the note sits queued."""
    if not rec:
        return None
    state = rec.get("state")
    if state == "delivered":
        print("delivered at %s" % fmt_clock(rec.get("t") or time.time()))
        return EXIT_OK
    if state == "deferred":
        print("deferred to the next round")
        return EXIT_DEFERRED
    if state == "undelivered":
        print("undelivered: %s" % (rec.get("reason") or "unknown"))
        return EXIT_UNDELIVERED
    return None


def cmd_send(argv):
    address, wait, text = _parse_send(argv)
    if not valid_address(address):
        raise NotesError("malformed address: %r" % address, EXIT_USAGE)
    inbox, pointer, _unit = resolve_address(address, require_running=True)
    note_id = queue_note(inbox, address, text)
    print(note_id)
    sys.stdout.flush()
    if wait is None:
        return EXIT_OK
    return _wait_for_note(inbox, pointer, note_id, wait)


def cmd_notes(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        raise NotesError(USAGE_NOTES, EXIT_USAGE)
    address = argv[0]
    inbox, _pointer, _unit = resolve_address(address, require_running=False)
    states, order = latest_states(inbox)
    if not order:
        print("no notes")
        return EXIT_OK
    for note_id in order:
        rec = states[note_id]
        state = rec.get("state") if rec.get("state") in STATES else "undelivered"
        reason = rec.get("reason")
        stamp = fmt_clock(rec.get("t") or time.time())
        label = "%s: %s" % (state, reason) if reason else state
        print("%s  %s  %s  %s" % (stamp, label, note_id, preview(note_text(inbox, note_id))))
    return EXIT_OK


# ------------------------------------------------------------ ask and answer (11.2)
def _msgdir():
    """The msgdir module, imported on first use: lib/msgdir.py imports this module
    at its top, so a module-level import would be a cycle (the repo's deferred-
    import pattern, cf. lib/advisor_mcp.py's watch import inside its handler)."""
    from lib import msgdir
    return msgdir


def msg_dir_for(pointer, unit):
    """The agent's message dir from its registry pointer (spec 4.1/11.2): the
    single-shot's `<watch_dir>/<id>.msg` beside its pointer and inbox, the
    until-done loop's `RUN/msg` (one per loop, shared by its rounds), a swarm or
    research unit's `RUN/msg/units/<unit>`. resolve_address has already vouched
    for the pointer's kind and run_path; only the agent kind's own id is checked."""
    kind = pointer.get("kind")
    if kind == "agent":
        run_id = pointer.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise NotesError("pointer has no run id", EXIT_UNAVAILABLE)
        return _msgdir().single_msgdir(watch.watch_dir(), run_id)
    if kind == "until-done":
        return _msgdir().loop_msgdir(pointer.get("run_path"))
    return _msgdir().unit_msgdir(pointer.get("run_path"), unit)


def _print_reply(address, reply):
    """One answer on stdout (spec 4.3): `answer from <address> (HH:MM:SS):` from the
    reply's own stamp, then the text; more than REPLY_MAX characters is printed cut,
    the cut notice as the last line."""
    print("answer from %s (%s):" % (address, fmt_clock(reply.get("t") or time.time())))
    text = reply.get("text")
    if not isinstance(text, str):
        text = ""
    # the model's text lands in the caller's terminal: no escape sequences
    text = _strip_controls(text)
    if len(text) > REPLY_MAX:
        print(text[:REPLY_MAX])
        print("... (cut at %d characters)" % REPLY_MAX)
    else:
        print(text)


def _ask_note_exists(inbox, note_id):
    """Does the inbox hold a note file with that id in `delivered/`, `new/` or
    `undelivered/` (the driver moves it as it delivers)? --answer's unknown-id
    check (11.2); a symlink is never opened and never counts."""
    if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
        return False
    for state in (DELIVERED, NEW, UNDELIVERED):
        path = os.path.join(inbox, state, "%s.json" % note_id)
        if not _is_link(path) and os.path.isfile(path):
            return True
    return False


def _wait_for_reply(msg_dir, inbox, address, note_id, seconds):
    """The answer wait (spec 4.3/11.2): poll the agent's msg dir for its reply every
    ASK_POLL seconds. A reply prints (0); the inbox `.closed` with no reply is
    `the agent ended without answering` (11); the wait running out is
    `no answer yet: <id>` (10). seconds == 0 is exactly one look: reply, closed,
    over -- it never sleeps."""
    msgdir = _msgdir()
    deadline = time.time() + seconds
    while True:
        reply = msgdir.read_reply(msg_dir, note_id)
        if reply is not None:
            _print_reply(address, reply)
            return EXIT_OK
        if is_closed(inbox):
            sys.stderr.write("the agent ended without answering\n")
            return EXIT_UNDELIVERED
        if time.time() >= deadline:
            sys.stderr.write("no answer yet: %s\n" % note_id)
            return EXIT_STILL_QUEUED
        time.sleep(ASK_POLL)


def cmd_ask(argv):
    """The `--ask` mode (11.2): `send`'s grammar and refusals (require_running=True),
    but the note is kind `ask` and the wait is for the agent's `reply` tool answer
    in its msg dir -- default 600 s, no `--wait` given."""
    address, wait, text = _parse_send(argv)
    if not valid_address(address):
        raise NotesError("malformed address: %r" % address, EXIT_USAGE)
    inbox, pointer, unit = resolve_address(address, require_running=True)
    note_id = queue_note(inbox, address, text, kind="ask")
    print("asked %s" % note_id)
    sys.stdout.flush()
    return _wait_for_reply(msg_dir_for(pointer, unit), inbox, address, note_id,
                           ASK_WAIT_DEFAULT if wait is None else wait)


def _parse_answer(argv):
    """The `--answer` grammar, hand-rolled like _parse_send: ADDRESS, NOTE_ID and an
    optional --wait SECONDS (0..3600; absent means 0 -- one look, no wait). Every
    wrong shape is a NotesError code 2."""
    wait = None
    positional = []
    rest = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if rest:
            positional.append(token)
        elif token == "--":
            rest = True
        elif token == "--wait":
            if wait is not None or i + 1 >= len(argv):
                raise NotesError("--wait needs its seconds, once", EXIT_USAGE)
            try:
                wait = int(argv[i + 1])
            except ValueError:
                raise NotesError("--wait SECONDS must be an integer", EXIT_USAGE) from None
            if not 0 <= wait <= 3600:
                raise NotesError("--wait SECONDS must be 0..3600", EXIT_USAGE)
            i += 1
        elif token.startswith("--wait="):
            return _parse_answer(argv[:i] + ["--wait", token[len("--wait="):]] + argv[i + 1:])
        elif token.startswith("-") and len(token) > 1:
            raise NotesError("unknown option: %s" % token, EXIT_USAGE)
        else:
            positional.append(token)
        i += 1
    if len(positional) != 2:
        raise NotesError("%s (got %d positional(s))" % (USAGE_ANSWER, len(positional)),
                         EXIT_USAGE)
    return positional[0], positional[1], 0 if wait is None else wait


def cmd_answer(argv):
    """The `--answer` mode (11.2): fetch the reply to an earlier ask, even after the
    run ended -- resolve with require_running=False (no running, pid or closed
    checks). Unknown id (no note of it in the inbox and no reply) is 3; a reply
    prints (0); no reply waits like --ask (closed 11, over 10)."""
    address, note_id, wait = _parse_answer(argv)
    if not valid_address(address):
        raise NotesError("malformed address: %r" % address, EXIT_USAGE)
    inbox, pointer, unit = resolve_address(address, require_running=False)
    msg_dir = msg_dir_for(pointer, unit)
    reply = _msgdir().read_reply(msg_dir, note_id)
    if reply is not None:
        _print_reply(address, reply)
        return EXIT_OK
    if not _ask_note_exists(inbox, note_id):
        sys.stderr.write("no such ask: %s\n" % note_id)
        return EXIT_UNAVAILABLE
    return _wait_for_reply(msg_dir, inbox, address, note_id, wait)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt":                        # bash reads our lines: never a \r (K8)
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.reconfigure(newline="\n")
    if argv and argv[0] in ("send", "--send"):
        handler = cmd_send
    elif argv and argv[0] in ("notes", "--notes"):
        handler = cmd_notes
    elif argv and argv[0] in ("ask", "--ask"):
        handler = cmd_ask
    elif argv and argv[0] in ("answer", "--answer"):
        handler = cmd_answer
    else:
        sys.stderr.write("%s\n%s\n%s\n%s\n"
                         % (USAGE_SEND, USAGE_NOTES, USAGE_ASK, USAGE_ANSWER))
        return EXIT_USAGE
    try:
        return handler(argv[1:])
    except NotesError as e:
        sys.stderr.write("notes: %s\n" % e)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
