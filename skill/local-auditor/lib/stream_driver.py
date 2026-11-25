"""The driver between qwen-agent.sh and claude: streamed input, notes in, one record out.

`qwen-agent.sh` builds this instead of running claude directly (spec 4.3):

  ... "$QA_PY" lib/stream_driver.py --inbox DIR --prompt-file F --raw RAW
      [--defer-on-end] [--bash BASH] -- <claude argv without the trailing -- PROMPT>

The driver adds `--input-format stream-json --output-format stream-json --verbose
--replay-user-messages`, writes the prompt (the prompt file's whole text) as the first
user line, and forwards the agent's inbox notes (`lib/notes.py`) as further user lines
with the `[Note from <from> at HH:MM:SS local time]` header (K10: the time the note was
QUEUED). A forwarded note counts as delivered when its `isReplay` echo comes back; the
echoes are matched FIFO and the first one (the initial prompt) is skipped (K6). Tool
results (`type == "user"` without `isReplay`) are ignored.

Threads, not select (Windows-safe): a reader drains claude's stdout continuously and
keeps ONLY the allowlist -- `result` events, `system/init` (the session id) and the
replayed `user` events; every thinking event, assistant block and tool block is
discarded unread and never written anywhere. A writer drains a queue into claude's
stdin; a BrokenPipeError marks unacknowledged notes `undelivered: crashed`. The main
thread runs the state machine plus the inbox poll on one Condition.

Close rule (P1): `turn_in_flight` starts True; writing a note or reading its echo sets
it True; a `result` clears it ONLY when the writer queue (`pending`) and the waiting-for
echo set (`unacked`) are both empty; stdin closes when it is False and `new/`, `pending`
and `unacked` are all empty -- the whole test lives in `close_policy(state) -> bool` so
phases 1.5/2 can keep an agent waiting for a reply. Per-run cap QWEN_NOTE_MAX_PER_RUN
(default 20): notes beyond it move to `undelivered/` with reason `cap`. QWEN_NOTE_GRACE
(default 0) makes the DRAIN wait that many seconds first; QWEN_NOTE_POLL (default 1 s)
is the test-visible poll interval.

The final `$RAW` record (one JSON line, `type: "result"`) is written ONLY when at least
one `result` arrived: the LAST result's fields, with `num_turns`, `duration_ms` and the
numeric `usage` keys summed per turn, `total_cost_usd` the last (cumulative), denials
concatenated and de-duplicated only among entries whose `tool_use_id` is a non-empty
string, the FIRST errored turn's error fields, and the additive `stream: true`,
`notes_delivered` and `note_ids`. Crash rules: no `result` ever -> `$RAW` is left EMPTY
(a spawn failure leaves it untouched); turn in flight after an earlier result -> the
record carries `is_error: true` and `subtype: "error_during_turn"`. Exit codes (P4/P7):
127/126 for a failed spawn or a claude that exited 126/127 having written ZERO stdout
bytes (so run_claude's retry path works); 1 for 126/127 after some output or 0/124/137
before any result; every other code propagates (negative -> 128 + (-rc)). On
SIGTERM/SIGINT (and in the abnormal finally) claude's whole process group gets
SIGTERM, then SIGKILL after at most 2 s (POSIX killpg under start_new_session; a
Windows Job Object with KILL_ON_JOB_CLOSE), the reader joins with a 1 s cap, and the
driver exits 128 + signum.

`--defer-on-end` (until-done rounds, P2): notes still in `new/` at exit are logged
`deferred` and LEFT there; without the flag the driver leaves `new/` alone. `--dry-run`
prints the `# stream:` line above the argv and starts nothing.
"""
import argparse
import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import notes  # noqa: E402

STREAM_FLAGS = ["--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--replay-user-messages"]
DEFAULT_NOTE_MAX_PER_RUN = 20
DEFAULT_POLL = 1.0
EXIT_NOFILE, EXIT_NOEXEC = 127, 126
KILL_WAIT = 2.0                    # s from the group SIGTERM to the group SIGKILL
READER_JOIN = 1.0                  # s the reader gets to finish draining


def close_policy(state):
    """True when stdin may close (P1): the latest written message got its `result`,
    nothing is queued for the writer, nothing waits for its echo, and `new/` is empty.
    Phases 1.5/2 extend this to keep an agent alive between turns."""
    return (not state["turn_in_flight"] and not state["pending"]
            and not state["unacked"] and not state["new"])


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_results(results, notes_delivered, note_ids):
    """The final record, built from the per-turn results exactly as 4.3 specifies:
    the LAST result's fields, then summed per-turn figures, the cumulative cost as-is,
    de-duplicated denials, the first errored turn's error fields, and the trio."""
    last = results[-1]
    merged = dict(last)
    for key in ("num_turns", "duration_ms"):
        total = None
        for rec in results:
            if _numeric(rec.get(key)):
                total = (total or 0) + rec[key]
        if total is not None:
            merged[key] = total
    summed, last_usage = {}, last.get("usage") if isinstance(last.get("usage"), dict) else {}
    for rec in results:
        usage = rec.get("usage")
        if isinstance(usage, dict):
            for name, value in usage.items():
                if _numeric(value):
                    summed[name] = summed.get(name, 0) + value
    final_usage = {}
    for name, value in last_usage.items():
        final_usage[name] = summed[name] if name in summed else value   # nested dicts:
    for name, value in summed.items():                                   # last's, as-is
        final_usage.setdefault(name, value)
    if isinstance(last.get("usage"), dict):
        merged["usage"] = final_usage
    denials, seen_ids = [], set()
    for rec in results:
        for entry in rec.get("permission_denials") or []:
            if isinstance(entry, dict):
                tool_use_id = entry.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    if tool_use_id in seen_ids:
                        continue
                    seen_ids.add(tool_use_id)
            denials.append(entry)
    merged["permission_denials"] = denials
    errored = next((rec for rec in results if rec.get("is_error")), None)
    if errored is not None:
        merged["is_error"] = True
        for key in ("subtype", "api_error_status", "terminal_reason"):
            if key in errored:
                merged[key] = errored[key]
    merged["stream"] = True
    merged["notes_delivered"] = notes_delivered
    merged["note_ids"] = list(note_ids)
    return merged


def _resolve_claude(argv, bash):
    """K8/P9: on POSIX exec CLAUDE_ARGV[0] as given (an unexecutable file stays exit
    126 -- spawn_permission_error covers it). Only on Windows: a path-qualified name
    is checked with os.path.isfile, otherwise shutil.which then a scan of PATH for an
    extensionless file, and a `#!` script runs through $BASH."""
    exe = argv[0]
    if os.name != "nt":
        return list(argv)
    found = None
    if os.path.isabs(exe) or os.sep in exe or (os.altsep and os.altsep in exe):
        if os.path.isfile(exe):
            found = exe
    else:
        found = shutil.which(exe)
        if not found:
            for folder in os.environ.get("PATH", "").split(os.pathsep):
                candidate = os.path.join(folder, exe)
                if os.path.isfile(candidate):
                    found = candidate
                    break
    if not found:
        raise FileNotFoundError("claude not found: %s" % exe)
    try:
        with open(found, "rb") as fh:
            shebang = fh.read(2) == b"#!"
    except OSError:
        shebang = False
    if shebang:
        import pathlib
        return [(bash or "bash"), pathlib.PurePath(found).as_posix()] + list(argv[1:])
    return [found] + list(argv[1:])


if os.name == "nt":                                          # ctypes only on Windows
    import ctypes

    class _BasicLimit(ctypes.Structure):                     # ntobjapi.h, packed=False
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _Job:
        """A Job Object that kills its members when its handle closes: the driver's
        death -- or its _kill_group -- takes claude's whole subtree with it."""

        def __init__(self):
            self.handle = None
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.CreateJobObjectW(None, None)
                if not handle:
                    return
                info = _ExtendedLimit()
                info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(
                        handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                        ctypes.byref(info), ctypes.sizeof(info)):
                    kernel32.CloseHandle(handle)
                    return
                self.handle = handle
            except Exception:
                self.handle = None

        def assign(self, proc):
            if not self.handle:
                return
            with contextlib.suppress(Exception):
                ctypes.windll.kernel32.AssignProcessToJobObject(
                    self.handle, ctypes.c_void_p(int(proc.handle)))

        def kill(self):
            if self.handle:
                with contextlib.suppress(Exception):
                    ctypes.windll.kernel32.CloseHandle(self.handle)
                self.handle = None


class Driver:
    def __init__(self, inbox, prompt, raw_path, defer_on_end=False, bash=None):
        self.inbox = str(inbox)
        self.prompt = prompt
        self.raw_path = str(raw_path)
        self.defer_on_end = bool(defer_on_end)
        self.bash = bash
        self.cap = self._env_int("QWEN_NOTE_MAX_PER_RUN", DEFAULT_NOTE_MAX_PER_RUN)
        self.poll_interval = self._env_float("QWEN_NOTE_POLL", DEFAULT_POLL)
        self.grace = self._env_float("QWEN_NOTE_GRACE", 0.0)
        self.activity = os.environ.get("QWEN_WATCH_ACTIVITY")
        self.cond = threading.Condition()
        self.pending = {}                    # note id -> entry handed to the writer
        self.unacked = {}                    # note id -> entry written, echo awaited
        self.eof_seen = False                # the reader drained stdout to EOF
        self.wqueue = queue.Queue()
        self.results = []                    # appended by the reader, applied by the main
        self._results_applied = 0            # thread (P1: a result clears the flag only
        self.echoes = []                     # when pending AND unacked are empty)
        self.session_id = None
        self.stdout_bytes = 0
        self.turn_in_flight = True           # P1: the prompt is in flight from the start
        self.forwarded = 0
        self.seen_ids = set()
        self.acked_ids = []
        self.signum = None
        self.spawned = False
        self.proc = None
        self.job = None
        self.reader = None
        self.writer = None
        self._first_echo_done = False
        self._graced = False
        self._task_warned = False
        self._wake = 0                       # generation: bumped under the lock at every
                                             # state change -- a Condition.notify that
                                             # lands between a check and wait_for() is
                                             # otherwise lost, and the driver would
                                             # sleep out the whole poll interval with
                                             # work outstanding (P1: writer moved the
                                             # note after close_policy read `pending`)

    @staticmethod
    def _env_int(name, default):
        try:
            return max(0, int(os.environ.get(name, "")))
        except ValueError:
            return default

    @staticmethod
    def _env_float(name, default):
        try:
            value = float(os.environ.get(name, ""))
        except ValueError:
            return default
        return value if value >= 0 else default

    # ---------------------------------------------------------------- spawn
    def _spawn(self, claude_argv):
        argv = _resolve_claude(claude_argv, self.bash) + STREAM_FLAGS
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": None,
                  "close_fds": True}
        if os.name == "posix":
            kwargs["start_new_session"] = True     # our own group: killpg takes the subtree
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(argv, **kwargs)
        if os.name == "nt":
            self.job = _Job()
            self.job.assign(self.proc)
        self.spawned = True

    # ---------------------------------------------------------------- threads
    def _wake_locked(self):
        """Call while holding self.cond: the state changed -- make sure any thread
        waiting (or about to) on the condition wakes for it, no matter the timing."""
        self._wake += 1
        self.cond.notify_all()

    def _reader_loop(self):
        try:
            for raw in self.proc.stdout:           # binary lines, no length cap
                self.stdout_bytes += len(raw)
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "result":
                    with self.cond:
                        self.results.append(event)
                        self._wake_locked()
                elif kind == "system" and event.get("subtype") == "init":
                    session = event.get("session_id")
                    if isinstance(session, str):
                        self.session_id = session
                elif kind == "user" and event.get("isReplay") is True:
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    with self.cond:
                        self.echoes.append(content if isinstance(content, str) else None)
                        self._wake_locked()
                elif isinstance(kind, str) and kind.startswith("task_"):
                    if not self._task_warned:          # O3: one line, then carry on
                        self._task_warned = True
                        self._warn("unexpected %s event (O3): ignored" % kind)
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.eof_seen = True
                self._wake_locked()

    def _writer_loop(self):
        while True:
            item = self.wqueue.get()
            if item is None:
                return
            note_id, line = item              # note_id None: the prompt, not a note
            try:
                self.proc.stdin.write(line.encode("utf-8", "replace") + b"\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._fail_queued(note_id)
                return
            if note_id is not None:
                with self.cond:
                    entry = self.pending.pop(note_id, None)
                    self.unacked[note_id] = entry or {"content": line}
                    self.turn_in_flight = True  # P1: a written note is a turn in flight
                    self._wake_locked()

    def _fail_queued(self, failed_id):
        """The stdin died: this note and every queued one is undelivered: crashed.
        The prompt (id None) is not a note: it fails nothing."""
        doomed = [failed_id] if failed_id is not None else []
        while True:
            try:
                rest = self.wqueue.get_nowait()
            except queue.Empty:
                break
            if rest is not None and rest[0] is not None:
                doomed.append(rest[0])
        with self.cond:
            for note_id in doomed:
                entry = self.pending.pop(note_id, None)
                self._undeliver(note_id, "crashed", entry)
            self._wake_locked()

    # ---------------------------------------------------------------- notes
    def _warn(self, message):
        sys.stderr.write("stream_driver: %s\n" % message)
        sys.stderr.flush()

    def _note_header_time(self, body):
        """The driver's time source for the header: the note's queued t when numeric,
        else right now (K10). Passed to render_note as now_t, which consults it only
        when t is not numeric."""
        stamp = body.get("t")
        return stamp if _numeric(stamp) else time.time()

    def forward_note(self, body, path, note_id):
        """Queue one validated note: the model-facing text is built ONLY by
        notes.render_note (one renderer for every kind, so a streamed note and a
        deferred one look the same); the stdin line carries nothing else. No other
        body key is forwarded."""
        content = notes.render_note(body, now_t=self._note_header_time(body))
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                          ensure_ascii=False)
        entry = {"content": content, "path": path, "body": body, "line": line}
        with self.cond:
            self.pending[note_id] = entry
            self.seen_ids.add(note_id)
            self.forwarded += 1
            self.wqueue.put((note_id, line))
            self._wake_locked()
        return entry

    def _forward_pass(self):
        """One synchronous scan of new/ in time order (the poll and the DRAIN scan):
        forward what is valid, move what is not, cap what is beyond the budget."""
        touched = 0
        for path, note_id in notes.list_new(self.inbox):
            if note_id in self.seen_ids:
                continue
            if self.forwarded >= self.cap:
                notes.sweep_undelivered(self.inbox, note_id, "cap", path)
                self.seen_ids.add(note_id)
                touched += 1
                continue
            try:
                body = notes.load_note(path)
            except notes.NotesError:
                notes.sweep_undelivered(self.inbox, note_id, "invalid_note", path)
                self.seen_ids.add(note_id)
                touched += 1
                continue
            self.forward_note(body, path, note_id)
            touched += 1
        return touched

    def _process_echoes(self):
        """K6 FIFO matching: the first echo is the prompt (skipped); each later echo
        acknowledges the oldest unacknowledged note."""
        while True:
            with self.cond:
                if not self.echoes:
                    return
                if not self._first_echo_done:
                    self._first_echo_done = True
                    content = self.echoes.pop(0)
                    if content is not None and content != self.prompt:
                        self._warn("prompt echo does not match what was written")
                    continue
                if not self.unacked:
                    if self.pending:
                        return                     # the writer is mid-write: retry
                    self._warn("echo with nothing unacknowledged: ignored")
                    self.echoes.pop(0)
                    continue
                note_id = next(iter(self.unacked))
                entry = self.unacked.pop(note_id)
                content = self.echoes.pop(0)
                if content is not None and content != entry.get("content"):
                    self._warn("echo content mismatch for %s: acknowledging FIFO anyway"
                               % note_id)
                self.turn_in_flight = True          # P1: an echo is a turn in flight too
                self.acked_ids.append(note_id)
            self._deliver(note_id, entry)

    def _deliver(self, note_id, entry):
        """The delivered log line, the activity line and the move to delivered/."""
        body = entry.get("body") or {}
        to_addr = body.get("to") if isinstance(body.get("to"), str) else ""
        sender = body.get("from") if isinstance(body.get("from"), str) else "main"
        text_len = len(body.get("text") or "")
        try:
            notes.log_note(self.inbox, note_id, to_addr, sender, "delivered",
                           text_len=text_len)
        except OSError as e:
            # A log that cannot be written (a symlinked notes.jsonl, a full disk) must
            # not undo a real delivery: warn, and let the move and the record proceed.
            self._warn("delivered log failed for %s (%s)" % (note_id, e))
        if self.activity:
            with contextlib.suppress(OSError):
                notes.append_jsonl(self.activity, {"t": notes._now_t(), "ev": "note",
                                                  "id": note_id, "from": sender,
                                                  "len": text_len})
        path = entry.get("path")
        if path and os.path.exists(path):
            dst = os.path.join(self.inbox, notes.DELIVERED, "%s.json" % note_id)
            if not notes.move_note(path, dst):
                self._warn("could not move %s to delivered/ (delivered once, seen)" % note_id)

    def _undeliver(self, note_id, reason, entry):
        """A note the driver touched but never saw echoed: log it and move the file."""
        if entry is None:
            entry = {}
        path = entry.get("path")
        if path:
            dst = os.path.join(self.inbox, notes.UNDELIVERED, "%s.json" % note_id)
            notes.move_note(path, dst)
            to_addr, text_len = notes.note_meta(dst) if os.path.exists(dst) else ("", 0)
        else:
            to_addr, text_len = "", 0
        body = entry.get("body") or {}
        sender = body.get("from") if isinstance(body.get("from"), str) else "main"
        try:
            notes.log_note(self.inbox, note_id, to_addr, sender, "undelivered",
                           reason=reason, text_len=text_len)
        except OSError as e:
            self._warn("undelivered log failed for %s (%s)" % (note_id, e))

    def _finalize_notes(self):
        """At exit the driver handles only the notes it touched (K4): forwarded but not
        echoed -> undelivered: crashed. With --defer-on-end, untouched new/ notes are
        logged deferred and LEFT; without it, new/ is left alone entirely."""
        with self.cond:
            stuck = dict(self.unacked)
            queued = dict(self.pending)
            self.unacked.clear()
            self.pending.clear()
        for note_id, entry in list(queued.items()) + list(stuck.items()):
            self._undeliver(note_id, "crashed", entry)
        if self.defer_on_end:
            for path, note_id in notes.list_new(self.inbox):
                if note_id in self.seen_ids:
                    continue
                to_addr, text_len = notes.note_meta(path)
                try:
                    notes.log_note(self.inbox, note_id, to_addr, "main", "deferred",
                                   text_len=text_len)
                except OSError as e:
                    self._warn("deferred log failed for %s (%s)" % (note_id, e))

    # ---------------------------------------------------------------- close & kill
    def _apply_results(self):
        """P1: each result the reader appended clears `turn_in_flight` ONLY when the
        writer queue and the echo-waiting set are both empty at that moment."""
        with self.cond:
            while self._results_applied < len(self.results):
                self._results_applied += 1
                if not self.pending and not self.unacked:
                    self.turn_in_flight = False

    def _state(self):
        with self.cond:
            return {"turn_in_flight": self.turn_in_flight,
                    "pending": bool(self.pending), "unacked": bool(self.unacked),
                    "new": notes.pending_count(self.inbox)}

    def _machine(self):
        """The P1 state machine: RUNNING_TURN <-> DRAIN <-> CLOSING."""
        prompt_line = json.dumps({"type": "user",
                                  "message": {"role": "user", "content": self.prompt}},
                                 ensure_ascii=False)
        self.wqueue.put((None, prompt_line))                  # the prompt: first in, always
        while True:
            if self.signum is not None:
                return
            with self.cond:
                seen = self._wake            # snapshot BEFORE consuming: any event the
                                             # reader/writer/signals raise after it makes
                                             # the wait_for below return at once, so a
                                             # notify can never land in the gap between
                                             # the checks and the sleep
            self._process_echoes()                       # acks first: P1 reads the flags
            self._apply_results()                        # at the moment the result lands
            if self._forward_pass():
                continue
            if close_policy(self._state()):
                if self.grace > 0 and not self._graced:
                    self._graced = True
                    with self.cond:
                        self.cond.wait_for(lambda: self._wake != seen
                                           or self.signum is not None, self.grace)
                    continue
                return
            self._graced = False
            if self.proc.poll() is not None or self.eof_seen:
                # A closed stdout can never emit another result or echo, so the turn
                # is over: returning lets run() close stdin (_close_stdin), which also
                # unblocks a child whose own exit waits on its stdin (the Windows
                # hang: claude finished writing, then stalled in os._exit until stdin
                # closed -- and stdin only closed after _machine returned). eof_seen is
                # set under self.cond; reading the bool plainly is fine.
                self.reader.join(2.0)                          # drain the last lines first:
                return                                         # late results and echoes
            with self.cond:
                self.cond.wait_for(lambda: self._wake != seen
                                   or self.signum is not None, self.poll_interval)

    def _close_stdin(self):
        """CLOSING: drain the writer, then close claude's stdin; the reader keeps
        draining stdout to EOF and merges any late result (it is a plain append)."""
        self.wqueue.put(None)
        if self.writer is not None:
            self.writer.join(5.0)
        with self.cond:
            leftover = dict(self.pending)
            self.pending.clear()
        for note_id, entry in leftover.items():               # writer died with them queued
            self._undeliver(note_id, "crashed", entry)
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()

    def _kill_group(self):
        """POSIX: killpg SIGTERM, at most 2 s, then killpg SIGKILL (claude owns its own
        session, so the pgid is its pid: the WHOLE subtree goes, spawn_child decoys
        included). Windows: close the Job Object handle (KILL_ON_JOB_CLOSE)."""
        if self.proc is None:
            return
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            deadline = time.time() + KILL_WAIT
            while time.time() < deadline and self.proc.poll() is None:
                time.sleep(0.05)
            if self.proc.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        else:
            if self.job is not None:
                self.job.kill()
            with contextlib.suppress(Exception):
                self.proc.kill()
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=2)

    def _on_signal(self, signum, _frame):
        if self.signum is None:
            self.signum = signum
            self._kill_group()
            with self.cond:
                self._wake_locked()

    # ---------------------------------------------------------------- raw & exit
    def _write_raw(self, text):
        tmp = "%s.tmp.%d" % (self.raw_path, os.getpid())
        folder = os.path.dirname(self.raw_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(text.encode("utf-8", "replace"))
        os.replace(tmp, self.raw_path)

    def _finalize_raw(self):
        """Write $RAW per the crash rules: a record only when a result arrived; an
        EMPTY file when claude ran but never produced one; nothing before the spawn."""
        if not self.spawned:
            return
        if self.results:
            in_flight = self.turn_in_flight
            rec = merge_results(self.results, len(self.acked_ids), self.acked_ids)
            if in_flight:
                rec["is_error"] = True
                rec["subtype"] = "error_during_turn"
            self._write_raw(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        else:
            self._write_raw("")

    def _exit_code(self):
        rc = self.proc.returncode if self.proc is not None else 1
        if rc is None:
            rc = 1
        code = 128 + (-rc) if rc < 0 else rc
        if self.results:
            return code                       # claude's own code, clean or not
        if code in (EXIT_NOEXEC, EXIT_NOFILE) and self.stdout_bytes == 0:
            return code                       # K7: never ran -> the retry path's codes
        if code in (EXIT_NOEXEC, EXIT_NOFILE, 0, 124, 137):
            return 1                          # P4: ran, produced nothing usable
        return code

    # ---------------------------------------------------------------- run
    def run(self, claude_argv):
        try:                                          # handlers BEFORE the spawn: a
            signal.signal(signal.SIGTERM, self._on_signal)   # SIGTERM that lands during
            signal.signal(signal.SIGINT, self._on_signal)    # the spawn must still take
        except (ValueError, OSError, AttributeError, RuntimeError):
            pass                                      # claude down (killpg, or the
                                                      # machine's signum check + finally)
        try:
            self._spawn(claude_argv)
        except FileNotFoundError as e:
            self._warn("%s" % e)
            return EXIT_NOFILE                 # $RAW untouched: today's retry path works
        except PermissionError as e:
            self._warn("cannot execute claude (%s)" % e)
            return EXIT_NOEXEC
        except OSError as e:
            self._warn("cannot start claude (%s)" % e)
            return EXIT_NOEXEC
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.writer = threading.Thread(target=self._writer_loop, daemon=True)
        self.reader.start()
        self.writer.start()
        try:
            self._machine()
            if self.signum is None:
                self._close_stdin()
                with contextlib.suppress(Exception):
                    # Bounded: a child that still will not exit after its stdin closed
                    # must not hang us either -- the timeout (or any wait error) falls
                    # through to the finally's _kill_group().
                    self.proc.wait(timeout=KILL_WAIT * 5)
        except Exception as e:                      # abnormal finally: never leak claude
            self._warn("internal error (%s): stopping claude" % e)
        finally:
            if self.proc.poll() is None:
                self._kill_group()
            if self.reader is not None:
                self.reader.join(READER_JOIN)
            if self.writer is not None:
                self.writer.join(READER_JOIN)
        try:
            self._finalize_notes()
        except OSError as e:                       # a hostile/cooperating-less inbox must
            self._warn("note finalization failed (%s)" % e)   # never skip the finished
        try:                                       # claude's record -- $RAW is the
            self._finalize_raw()                   # contract run_claude reads
        except OSError as e:
            self._warn("cannot write $RAW %s (%s)" % (self.raw_path, e))
        if self.signum is not None:
            return 128 + self.signum
        return self._exit_code()


def main(argv=None):
    if os.name == "nt":                            # bash reads our lines: never a \r (K8)
        for stream in (sys.stdout, sys.stderr):    # --dry-run on stdout, _warn on stderr
            with contextlib.suppress(Exception):
                stream.reconfigure(newline="\n")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        sys.stderr.write("usage: stream_driver.py --inbox DIR --prompt-file F --raw RAW "
                         "[--defer-on-end] [--bash BASH] -- <claude argv...>\n")
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser(prog="stream_driver.py", add_help=False)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--defer-on-end", action="store_true")
    parser.add_argument("--bash")
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv[:split])
    claude_argv = argv[split + 1:]
    if os.name == "nt":                            # bash hands us \r on every line (K8)
        claude_argv = [a.rstrip("\r") for a in claude_argv]
    if not claude_argv:
        sys.stderr.write("stream_driver: no claude argv after --\n")
        return 2
    if opts.dry_run:
        deferred = " --defer-on-end" if opts.defer_on_end else ""
        sys.stdout.write("# stream: lib/stream_driver.py --inbox %s --prompt-file %s "
                         "--raw %s%s -- %s\n" % (opts.inbox, getattr(opts, "prompt_file"),
                                                 opts.raw, deferred,
                                                 " ".join(claude_argv + STREAM_FLAGS)))
        return 0
    try:
        with open(getattr(opts, "prompt_file"), "rb") as fh:
            prompt = fh.read().decode("utf-8", "replace")
    except OSError as e:
        sys.stderr.write("stream_driver: cannot read the prompt file (%s)\n" % e)
        return 2
    driver = Driver(opts.inbox, prompt, opts.raw, defer_on_end=opts.defer_on_end,
                    bash=getattr(opts, "bash", None))
    return driver.run(claude_argv)


if __name__ == "__main__":
    sys.exit(main())
