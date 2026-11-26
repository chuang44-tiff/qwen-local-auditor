"""The swarm run's message relay (phase 2, spec 11.4/12.4/12.5): ONE
``RunRelay`` per run reads every running unit's ``out/new/`` and moves what
the units sent onward -- a ``share`` becomes an admitted finding that reaches
the sender's running siblings of the same phase as notes, a ``question`` goes
to ``asks.admit_question``. Built and tested on its own; Task 8b wires it
into ``lib/swarm.py`` and ``lib/swarm_engine/api.py``.

The relay is the ONLY writer of share notes: each note is queued with
``kind="share"`` and ``sender="unit <name>"``, and the sender is stamped from
the DIRECTORY the file was read in -- any ``from`` or sender-like field
inside the file is ignored, so a unit cannot put words in a sibling's mouth
(the spec's forged-file defence). The note text is the finding itself; the
body carries the sender's ``role`` and ``paths``, and the renderer
(``notes.render_note``, Task 1) is the one that quotes it.

Everything hangs off one ``threading.Lock`` (spec 11.4: one lock over the
findings list and the started set, so a unit gets each finding exactly once
and registration cannot race a relay iteration):

* ADMISSION, per sender per phase: at most ``share_max()``
  (``QWEN_SHARE_MAX``, default 3, clamped 0..5, non-integer -> 3) distinct
  findings; a finding is admitted only when it is 1..1500 characters after
  strip, its paths are at most 10 strings of at most 200 characters, and
  neither the finding nor any path imitates a note header
  (``notes.imitates_header``). An admitted finding lands as one line in
  ``RUN/msg/_phases/<pid>/findings.jsonl`` (``msgdir.findings_append``) with
  {"id","sender","role","t","text","paths"}; the id is
  ``notes.new_note_id()``-shaped. Drops log ``share_dropped`` with a reason:
  ``not_opted`` (the unit has no phase), ``phase_ended`` (its phase is
  closed), ``invalid``, ``sender_cap``, ``duplicate`` (the sha256 of the
  stripped finding is already this sender's in this phase).
* DELIVERY, per receiver ATTEMPT: at most ``share_in_max()``
  (``QWEN_SHARE_IN_MAX``, default 10, clamped 0..15, non-integer -> 10)
  share notes; a receiver at its budget logs ``share_dropped`` with reason
  ``receiver_cap`` and ``to``; a receiver whose inbox refuses (NotesError,
  closed) is skipped in silence. Every note that does land logs
  ``share_queued`` (``finding`` id and ``to`` receiver, no text) -- that log
  is who received what, and ``shared_findings_lines`` builds report.md's
  section from it. Only RUNNING units of the phase receive; a
  late starter gets the phase's findings as backlog at ``register``, and a
  retry attempt gets the backlog again under a fresh budget, skipping every
  finding still pending in its inbox ``new/`` (the relay tracks its own
  ``note_id -> finding id`` map) or already queued in THIS attempt.
* Questions are addressed ``"<run_id>/<unit>"``. Without ``asks_ready`` the
  question is dropped ``no_listener``; otherwise ``asks.admit_question``
  decides, and a ``run_limit``/``no_listener`` refusal answers the unit with
  a harness note (``open_question`` sends nothing). The ``close()`` drain
  drops what is left as ``run_ended`` instead of admitting it (spec 12.3).

``start_phase(pid)`` (re)loads a phase from disk -- its findings, every
sender's admitted count and its seen-text hashes -- so a resumed run keeps
its budgets and its backlog (``--resume`` replays ``findings.jsonl``).

The relay's own log is ``RUN/msg/relay.jsonl``: one JSON line per event
{"t","ev",...} under 512 bytes (the ``asks.log_event`` rule: fields dropped
biggest-first, then the ev truncated), 0600, ONE os.write on an O_APPEND fd.
Lines carry ids and reasons only -- ``unit``/``sender``, ``to``, ``pid``,
``finding`` (an id), ``attempt`` ("a<k>"), ``reason`` -- NEVER a finding's or
a question's text; the full text lives only in ``findings.jsonl`` and in the
notes. ``emit(kind, **fields)`` (Task 8b passes the engine's event writer)
reports one admitted share as ``emit("share", unit=, role=, share_id=,
to_count=, len=)`` -- no text either.

``start()`` runs a daemon thread calling ``poll_once()`` every
``QWEN_RELAY_POLL`` seconds (float, default 1.0 -- for tests);
``close()`` stops and joins it (5 s), then drains once more with questions
dropped, and is idempotent.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import asks, msgdir, notes  # noqa: E402
from lib.notes import DIR_MODE, FILE_MODE, _is_link  # noqa: E402

SHARE_MAX_DEFAULT = 3          # QWEN_SHARE_MAX ...
SHARE_MAX_BOUND = 5            # ... clamped to 0..5
SHARE_IN_MAX_DEFAULT = 10      # QWEN_SHARE_IN_MAX ...
SHARE_IN_MAX_BOUND = 15        # ... clamped to 0..15
FINDING_MAX_CHARS = 1500       # one finding, after strip
POLL_DEFAULT = 1.0             # seconds between polls (QWEN_RELAY_POLL)
CLOSE_JOIN = 5.0               # seconds close() gives the thread to stop
LOG_MAX = 512                  # bytes of one relay log line, newline included
RELAY_LOG = "relay.jsonl"

HARNESS_REFUSAL = ("your question was not sent (%s); decide yourself and"
                   " say what you assumed.")
RELAY_DROPPED = ("run_limit", "no_listener")   # the refusals that answer back


def _now_t():
    """The one stamp format (the notes.py convention): a Unix epoch float
    rounded to 3 decimals."""
    return round(time.time(), 3)


def _env_int(name, default, bound):
    """The one env-cap reader: the named variable as an int clamped to
    0..bound; unset or not an integer is the default."""
    try:
        value = int(os.environ.get(name))
    except (TypeError, ValueError):
        return default
    return max(0, min(bound, value))


def share_max() -> int:
    """Findings admitted per sender per phase (spec 12.5): QWEN_SHARE_MAX,
    default 3, clamped 0..5, non-integer -> 3. Read at every admission so a
    caller (or a test) may retune it between polls."""
    return _env_int("QWEN_SHARE_MAX", SHARE_MAX_DEFAULT, SHARE_MAX_BOUND)


def share_in_max() -> int:
    """Share notes delivered per receiver attempt (spec 12.5):
    QWEN_SHARE_IN_MAX, default 10, clamped 0..15, non-integer -> 10."""
    return _env_int("QWEN_SHARE_IN_MAX", SHARE_IN_MAX_DEFAULT,
                    SHARE_IN_MAX_BOUND)


def relay_poll() -> float:
    """Seconds between the thread's polls: QWEN_RELAY_POLL as a float (the
    tests set 0.1); unset, non-numeric or non-positive is 1.0."""
    try:
        value = float(os.environ.get("QWEN_RELAY_POLL"))
    except (TypeError, ValueError):
        return POLL_DEFAULT
    return value if value > 0 else POLL_DEFAULT


def _digest(text):
    """The duplicate key: sha256 of the finding after strip."""
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


def _event_line(rec):
    return (json.dumps(rec, ensure_ascii=False, default=str)
            + "\n").encode("utf-8", "replace")


class RunRelay:
    """The relay of one swarm run. ``run_dir`` is RUN, ``run_id`` the watch
    id the addresses are built from, ``asks_ready`` whether a main session
    can listen for this run at all (Task 8b: the run made its asks layout);
    ``emit(kind, **fields)`` is the optional event callback."""

    def __init__(self, run_dir, run_id, asks_ready, emit=None):
        self._run_dir = run_dir
        self._run_id = run_id
        # the engine reads asks_ready to gate the ask_main grant (spec 12.5's
        # activation matrix): the run made its asks layout AND a main session
        # can listen for it.
        self.asks_ready = bool(asks_ready)
        self._asks_ready = self.asks_ready
        self._emit = emit
        self._lock = threading.Lock()
        self._units = {}      # unit -> {unit, role, inbox, M, pid, attempt,
                              #          running, rescan, budget, delivered,
                              #          note_ids}
        self._phases = {}     # pid -> {"findings": [rec], "closed": bool}
        self._sender_state = {}   # (pid, sender) -> {"count": int, "seen": set}
        self._stop = None
        self._thread = None
        self._closed = False

    # ------------------------------------------------------------- phases
    def start_phase(self, pid) -> None:
        """Open phase ``pid``: its findings so far are reloaded from
        ``msgdir.findings_read`` together with every sender's admitted count
        and seen-text hashes, so a resumed run keeps its budgets and its
        backlog, and an already-admitted finding is never recounted or
        duplicated. Idempotent: a reopen re-reads the disk, which the relay
        always appends to before it touches memory."""
        with self._lock:
            self._phases.pop(pid, None)
            for key in [k for k in self._sender_state if k[0] == pid]:
                del self._sender_state[key]      # counts come from disk again
            self._phase(pid)

    def _phase(self, pid):
        """The phase's state, loaded from disk on first touch (register may
        meet a phase start_phase has not opened)."""
        state = self._phases.get(pid)
        if state is not None:
            return state
        findings = []
        for rec in msgdir.findings_read(self._run_dir, pid):
            if not isinstance(rec.get("id"), str) \
                    or not isinstance(rec.get("sender"), str) \
                    or not isinstance(rec.get("text"), str):
                continue                      # junk lines replay nothing
            findings.append(rec)
        state = {"findings": findings, "closed": False}
        self._phases[pid] = state
        for f in findings:
            st = self._sender(pid, f["sender"])
            st["count"] += 1
            st["seen"].add(_digest(f["text"]))
        return state

    def _sender(self, pid, sender):
        key = (pid, sender)
        st = self._sender_state.get(key)
        if st is None:
            st = {"count": 0, "seen": set()}
            self._sender_state[key] = st
        return st

    # ------------------------------------------------------------- units
    def register(self, unit, role, inbox, pid=None) -> None:
        """The unit is running (the runner calls this before ``unit_start``):
        attempt 1, fresh delivery budget, its msg dir
        ``msgdir.unit_msgdir(run_dir, unit)``. With ``pid`` (an opted phase)
        the unit joins the phase and every finding of the phase from ANOTHER
        sender is queued to its inbox at once -- the backlog, within its
        attempt budget."""
        with self._lock:
            u = {"unit": unit, "role": role, "inbox": inbox,
                 "M": msgdir.unit_msgdir(self._run_dir, unit),
                 "pid": pid, "attempt": 1, "running": True, "rescan": False,
                 "budget": 0, "delivered": set(), "note_ids": {}}
            self._units[unit] = u
            if pid is not None:
                # the backlog is queued whether or not the phase has since
                # closed: only a share READ from a closed phase is phase_ended
                self._replay(u, self._phase(pid))

    def retry(self, unit, attempt) -> None:
        """A new attempt of a running unit: fresh delivery budget (a higher
        attempt number resets it; a repeat for the CURRENT attempt is
        idempotent), the unit runs again, and every finding of its phase from
        another sender that is neither pending in its inbox ``new/`` nor
        already queued in THIS attempt is re-queued (spec 12.5)."""
        with self._lock:
            u = self._units.get(unit)
            if u is None:
                return
            attempt = int(attempt)
            if attempt != u["attempt"]:
                u["attempt"] = attempt
                u["budget"] = 0
                u["delivered"] = set()
            u["running"] = True
            u["rescan"] = False
            if u["pid"] is None:
                return
            self._replay(u, self._phase(u["pid"]))

    def unit_done(self, unit) -> None:
        """The unit no longer receives; one last pass over its ``out/new/``
        happens first, so what it shared on its way out still reaches its
        siblings (the next ``poll_once`` gives it one more pass, which is
        normally empty)."""
        with self._lock:
            u = self._units.get(unit)
            if u is None:
                return
            u["running"] = False
            self._poll_unit(u, drop_questions=False, mark=False)

    # ------------------------------------------------------------- delivery
    def _replay(self, u, state):
        for f in state["findings"]:
            if f["sender"] != u["unit"]:
                self._queue(u, f)

    def _pending(self, u):
        """The finding ids of our own notes still pending in the unit's inbox
        ``new/`` (our note_id -> finding id map against the files there)."""
        present = {note_id for _, note_id in notes.list_new(u["inbox"])}
        return {fid for nid, fid in u["note_ids"].items() if nid in present}

    def _queue(self, u, f):
        """One finding into one receiver's inbox, under its attempt budget:
        True when the note landed (logged ``share_queued``, ids only). Skipped
        (False, silently) when the finding is already queued in this attempt,
        still pending in ``new/``, or the inbox refuses it; budget spent logs
        ``receiver_cap`` naming both sides."""
        fid = f["id"]
        if fid in u["delivered"] or fid in self._pending(u):
            return False
        if u["budget"] >= share_in_max():
            self._log("share_dropped", unit=f["sender"], to=u["unit"],
                      pid=u["pid"], finding=fid, attempt=_attempt(u),
                      reason="receiver_cap")
            return False
        try:
            note_id = notes.queue_note(
                u["inbox"], "%s/%s" % (self._run_id, u["unit"]), f["text"],
                sender="unit %s" % f["sender"], kind="share",
                role=f.get("role"), paths=f.get("paths"))
        except notes.NotesError:
            return False                    # refused or closed inbox: silence
        u["budget"] += 1
        u["delivered"].add(fid)
        u["note_ids"][note_id] = fid
        self._log("share_queued", unit=f["sender"], to=u["unit"], pid=u["pid"],
                  finding=fid, attempt=_attempt(u))
        return True

    # ------------------------------------------------------------- the poll
    def poll_once(self) -> None:
        """One pass over every registered unit that runs or finished since
        the last pass: every id in ``msgdir.list_out(M)`` is taken and
        dispatched; None (taken by another taker, unreadable) is skipped. An
        exception inside one record's handling is caught, logged
        ``relay_error`` with no text, and the poll goes on."""
        with self._lock:
            self._poll_all(drop_questions=False)

    def _poll_all(self, drop_questions):
        for name in sorted(self._units):
            u = self._units[name]
            if u["running"] or not u["rescan"]:
                self._poll_unit(u, drop_questions)

    def _poll_unit(self, u, drop_questions, mark=True):
        for note_id in msgdir.list_out(u["M"]):
            try:
                rec = msgdir.take_out(u["M"], note_id)
                if rec is None:
                    continue
                kind = rec.get("kind")
                if kind == "share":
                    self._handle_share(u, rec)
                elif kind == "question":
                    self._handle_question(u, rec, drop_questions)
                else:
                    self._log("out_dropped", unit=u["unit"], finding=note_id,
                              reason="unknown_kind")
            except Exception as exc:            # one broken record never
                self._log("relay_error", unit=u["unit"],   # costs the rest of
                          finding=note_id,                  # the pass
                          reason=type(exc).__name__)
        if mark and not u["running"]:
            u["rescan"] = True

    def _handle_share(self, u, rec):
        """One share file from the unit's own outbox: the sender is the
        DIRECTORY's unit (the file's own from/sender fields are ignored), so
        nobody can forge a sibling into speaking."""
        unit = u["unit"]
        pid = u["pid"]
        rec_id = rec.get("id")
        if pid is None:
            self._log("share_dropped", unit=unit, finding=rec_id,
                      attempt=_attempt(u), reason="not_opted")
            return
        state = self._phase(pid)
        if state["closed"]:
            self._log("share_dropped", unit=unit, pid=pid, finding=rec_id,
                      attempt=_attempt(u), reason="phase_ended")
            return
        finding = rec.get("finding")
        paths = rec.get("paths")
        if (not isinstance(finding, str)
                or not isinstance(paths, list)
                or len(paths) > notes.MAX_PATHS
                or any(not isinstance(p, str)
                       or len(p) > notes.MAX_PATH_CHARS for p in paths)
                or not 1 <= len(finding.strip()) <= FINDING_MAX_CHARS
                or notes.imitates_header(finding)
                or any(notes.imitates_header(p) for p in paths)):
            self._log("share_dropped", unit=unit, pid=pid, finding=rec_id,
                      attempt=_attempt(u), reason="invalid")
            return
        text = finding.strip()
        st = self._sender(pid, unit)
        if st["count"] >= share_max():
            self._log("share_dropped", unit=unit, pid=pid, finding=rec_id,
                      attempt=_attempt(u), reason="sender_cap")
            return
        digest = _digest(text)
        if digest in st["seen"]:
            self._log("share_dropped", unit=unit, pid=pid, finding=rec_id,
                      attempt=_attempt(u), reason="duplicate")
            return
        finding_id = notes.new_note_id()
        f = {"id": finding_id, "sender": unit, "role": u["role"],
             "t": _now_t(), "text": text, "paths": list(paths)}
        msgdir.findings_append(self._run_dir, pid, f)   # disk first, then
        state["findings"].append(f)                      # memory mirrors it
        st["count"] += 1
        st["seen"].add(digest)
        self._log("share_admitted", unit=unit, sender=unit, pid=pid,
                  finding=finding_id, attempt=_attempt(u))
        to_count = 0
        for name in sorted(self._units):
            other = self._units[name]
            if name == unit or other["pid"] != pid or not other["running"]:
                continue                                 # siblings, running only
            if self._queue(other, f):
                to_count += 1
        if self._emit is not None:
            try:
                self._emit("share", unit=unit, role=u["role"],
                           share_id=finding_id, to_count=to_count,
                           len=len(text))
            except Exception:                            # the callback is a
                pass                                       # guest: never fatal

    def _handle_question(self, u, rec, drop_questions):
        address = "%s/%s" % (self._run_id, u["unit"])
        if drop_questions:                     # only close()'s final drain
            asks.drop_question(self._run_dir, address, u["M"], rec,
                               "run_ended")    # passes True; every poll_once
            return                             # (then or later) admits
        if not self._asks_ready:
            asks.drop_question(self._run_dir, address, u["M"], rec,
                               "no_listener")
            return
        status, reason = asks.admit_question(self._run_dir, address, u["M"],
                                             rec, asks.ask_main_max())
        if status == "dropped" and reason in RELAY_DROPPED:
            notes.queue_note(u["inbox"], address, HARNESS_REFUSAL % reason,
                             sender="harness", kind="harness")
        # open_question (and any other reason) sends nothing: the asker was
        # told at tool time to keep working (spec 12.4).

    # ------------------------------------------------------------- phases end
    def end_phase(self, pid) -> None:
        """One last ``poll_once`` (a share planted before this point is
        admitted by it), then the phase is closed: a share read from a unit
        of a closed phase is ``share_dropped: phase_ended``."""
        with self._lock:
            self._poll_all(drop_questions=False)
            self._phase(pid)["closed"] = True

    # ------------------------------------------------------------- the thread
    def start(self) -> None:
        """The daemon thread: ``poll_once`` every QWEN_RELAY_POLL seconds.
        Called once per run by the engine; a second call is a no-op."""
        with self._lock:
            if self._thread is not None or self._closed:
                return
            stop = threading.Event()
            self._stop = stop
            thread = threading.Thread(target=self._run, args=(stop,),
                                      name="share-relay", daemon=True)
            self._thread = thread
            thread.start()      # under the lock: a concurrent close() never
                                # sees a claimed-but-never-started thread

    def _run(self, stop):
        interval = relay_poll()
        while not stop.wait(interval):
            try:
                self.poll_once()
            except Exception:                   # a relay never dies holding
                pass                            # the run's messaging

    def close(self) -> None:
        """Stop the thread (join 5 s) and drain once more in the calling
        thread, where every question is dropped ``run_ended`` instead of
        admitted (spec 12.3). Idempotent: the first caller claims the close.
        The drop mode belongs to that final drain alone: a ``poll_once`` run
        at any other time -- including the thread's last in-flight iteration
        and any call made after ``close()`` -- keeps ``poll_once``'s contract
        and admits. A question the in-flight poll admits is an admitted
        question: unclaimed asks stay in ``asks/new/``, as in spec 12.3."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stop, thread = self._stop, self._thread
            self._stop = self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=CLOSE_JOIN)
        with self._lock:
            for name in sorted(self._units):
                self._poll_unit(self._units[name], drop_questions=True)

    # ------------------------------------------------------------- the log
    def _log(self, ev, **fields):
        """One relay line {"t","ev",...} at RUN/msg/relay.jsonl: under 512
        bytes (the asks.log_event rule -- biggest field dropped first, then
        the ev cut), 0600, ONE os.write on an O_APPEND fd, O_NOFOLLOW. Only
        the named fields ride: unit/sender, to, pid, finding (an id),
        attempt, reason -- never a text. A symlinked msg/ or log is refused
        in silence; the log is never the run's problem."""
        rec = {"t": _now_t(), "ev": ev}
        for key in ("unit", "sender", "to", "pid", "finding", "attempt",
                    "reason"):
            value = fields.get(key)
            if value is not None:
                rec[key] = value
        line = _event_line(rec)
        if len(line) >= LOG_MAX:
            for key in sorted(fields, key=lambda k: -len(str(fields[k]))):
                if key in rec and key != "ev":
                    del rec[key]
                    line = _event_line(rec)
                    if len(line) < LOG_MAX:
                        break
        if len(line) >= LOG_MAX:               # only ev is left too long: cut
            ev_str = str(ev)                   # it to the longest fitting
            lo, hi = 0, len(ev_str)            # prefix (the bare line fits)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(_event_line({"t": rec["t"], "ev": ev_str[:mid]})) < LOG_MAX:
                    lo = mid
                else:
                    hi = mid - 1
            rec["ev"] = ev_str[:lo]
            line = _event_line(rec)
        msg = os.path.join(self._run_dir, msgdir.MSG_DIR)
        path = os.path.join(msg, RELAY_LOG)
        if _is_link(msg) or _is_link(path):
            return
        try:
            os.makedirs(msg, mode=DIR_MODE, exist_ok=True)
            if os.name == "posix":
                with contextlib.suppress(OSError):
                    os.chmod(msg, DIR_MODE)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                         | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError:
            pass


def _attempt(u):
    """The attempt tag the log uses: ``a<k>``."""
    return "a%d" % u["attempt"]


# ------------------------------------------------------------- the report
def _relay_records(run_dir):
    """The relay log's lines as records (an unreadable or missing log reads as
    none; a torn line is skipped, never raised): the report's reader."""
    path = os.path.join(run_dir, msgdir.MSG_DIR, RELAY_LOG)
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


def shared_findings_lines(run_dir):
    """report.md's ``## Shared findings`` entries (spec 11.4): one per admitted
    finding, in ``findings.jsonl`` order over every phase folder (the
    ``_phases/<pid>`` dirs sorted by their ``p<seq>`` number), the receivers
    read off the relay's own ``share_queued`` lines -- never from the notes and
    never with any text::

        - 13:45:02 unit a (prober), phase p1, to b, c
        > the finding, every line quoted, control characters stripped
        Paths: src/a.py, src/b.py

    The finding is quoted the way the renderer quotes a share note (the
    ``> `` prefix on every line of the stripped text); a ``Paths:`` line
    follows when the finding names any paths. A finding nobody got reads
    ``to nobody``. Junk findings records (no id, sender or text) replay
    nothing. [] when the run admitted nothing (the section is then absent)."""
    root = os.path.join(run_dir, msgdir.MSG_DIR, msgdir.PHASES_DIR)
    if _is_link(root) or not os.path.isdir(root):
        return []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    pids = [n for n in names if not _is_link(os.path.join(root, n))
            and os.path.isdir(os.path.join(root, n))]

    def _pid_sort(n):
        return (0, int(n[1:]), "") if n[:1] == "p" and n[1:].isdigit() else (1, 0, n)
    pids.sort(key=_pid_sort)
    queued = {}
    for rec in _relay_records(run_dir):
        if rec.get("ev") == "share_queued" and isinstance(rec.get("finding"), str) \
                and isinstance(rec.get("to"), str):
            receivers = queued.setdefault(rec["finding"], [])
            if rec["to"] not in receivers:
                receivers.append(rec["to"])
    lines = []
    for pid in pids:
        for f in msgdir.findings_read(run_dir, pid):
            fid, sender, text = f.get("id"), f.get("sender"), f.get("text")
            if not isinstance(fid, str) or not isinstance(sender, str) \
                    or not isinstance(text, str):
                continue                      # junk lines land in no report
            role = f.get("role")
            role = role if isinstance(role, str) else ""
            receivers = queued.get(fid) or []
            lines.append("- %s unit %s (%s), phase %s, to %s"
                         % (notes.fmt_clock(f.get("t")), sender, role, pid,
                            ", ".join(receivers) if receivers else "nobody"))
            lines.extend("> " + one
                         for one in notes._strip_controls(text).split("\n"))
            paths = f.get("paths")
            paths = [notes._strip_controls(p) for p in paths if isinstance(p, str)] \
                if isinstance(paths, (list, tuple)) else []
            if paths:
                lines.append("Paths: %s" % ", ".join(paths))
    return lines
