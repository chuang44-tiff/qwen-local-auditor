"""RUN/events.jsonl: the run's progress as one JSON object per line, for a watcher.

A swarm runs headless; a session that launched it learns what happens by following this
file (tail -F, or the Monitor tool) instead of re-reading run.log. Only the runner
process writes it -- its worker threads included -- so a process-wide lock plus ONE
os.write of each whole line (O_APPEND) keeps lines whole. A line is under 4 KB: long
string fields are cut until it fits. Readers must still skip a line that does not parse
or has no trailing newline (a reader can catch a write in flight).

Every line has "kind" and "t" (unix time). The engine's own kinds are ENGINE_KINDS:
  run_start    workflow, goal, run, resumed, depth, knobs   (runner._start)
  phase_start  label, n, seats, units                        (the engine's run_phase's,
               before the phase's first unit spawns; units is one {unit, name, role} per
               unit -- the watch fold reads an item by "name", so a unit names itself both
               ways. A roster too wide for one line goes out WITHOUT the roster, as
               {label, n, seats, truncated}: emit_phase_start, and why)
  unit_start   unit, role, timeout                            (swarm.run_unit, only when
               the unit really spawns: never for a cache hit or a stopped-by-deadline one.
               timeout is the seconds budget that call is spawned with -- the halved one
               for a review-round unit -- so the page's per-agent bar divides by it)
  unit_done    unit, role, ok, cached, seconds, why (only when not ok)   (swarm.py)
  unit_dropped unit, role, why                              (swarm.py)
  claude_call  name, item, state, seconds, cost_usd         (claude_check)
  claude_probe name, state, why                             (claude_check)
  run_end      exit, report (path or null)                  (runner._start, every exit
                                                            once the run holds its lock)
A workflow adds its own kinds through wf.event(kind, **fields); "attention" (fields
item, reason, detail) means "the main session should look at this".
The watch page (lib/watch.py) folds run_start, phase_start, unit_start, unit_done,
unit_dropped, attention and run_end into what it shows; the phase's label is the workflow's
own words for what the phase does (a workflow passes it to wf.agent/wf.fan_out/wf.vote as
label=, and a phase without one is "phase N").
Writing is best effort: an event that cannot be written never fails the run.
"""
import json
import os
import pathlib
import threading
import time

FILE = "events.jsonl"
ENGINE_KINDS = frozenset({"run_start", "phase_start", "unit_start", "unit_done",
                          "unit_dropped", "claude_call", "claude_probe", "run_end"})
MAX_LINE = 4096                       # bytes, newline included: every line stays below it
_LOCK = threading.Lock()
_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)


def _clip(value, n):
    """Strings longer than n characters cut to n plus a marker, inside dicts and lists."""
    if isinstance(value, str):
        return value if len(value) <= n else value[:n] + "...[cut]"
    if isinstance(value, dict):
        return {k: _clip(v, n) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v, n) for v in value]
    return value


def _line(rec):
    # errors="replace": a field may carry a lone surrogate (a non-UTF-8 argv byte)
    return (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")


def emit_phase_start(run_dir, label, n, seats, units):
    """The phase_start line the engine's two run_phase implementations owe the page
    (swarm.py and check.py): {label, n, seats, units}, `units` one {unit, name, role} per
    unit -- a unit names itself two ways because the watch fold reads a phase's roster by
    "name" while unit_start and unit_done name it by "unit".

    A roster too wide for one line is announced WITHOUT it but WITH its size: {label, n,
    seats, truncated}. The two shapes that look cheaper both say something false. Letting
    append() clip the line cuts the unit names inside it, and a cut name stops matching the
    unit_start and unit_done lines, so the page would keep a unit that ran as one that never
    started, plus a phantom queued one beside it. And a part-roster with n the phase's true
    size strands the progress bar: _phase_progress (lib/watch.py) counts finished units among
    the names it was given, so it would sit at roster/n however many units finished. Without
    a roster the page still has n, and the units announce themselves from their own events:
    progress counts the distinct units that report done or dropped since this line, and only
    the ones that started are listed (lib/watch.py). The size is measured on the exact bytes
    append() will write, same t throughout, so the clip ladder can never be reached with a
    name still left to cut."""
    t = round(time.time(), 3)
    rec = {"kind": "phase_start", "t": t, "label": label, "n": n, "seats": seats,
           "units": list(units)}
    if len(_line(rec)) >= MAX_LINE:
        rec = {"kind": "phase_start", "t": t, "label": label, "n": n, "seats": seats,
               "truncated": True}
    append(run_dir, rec)


def emit(run_dir, kind, **fields):
    """Append one event to RUN/events.jsonl: {"kind", "t", **fields}."""
    if "t" in fields:
        raise ValueError("an event field may not be named 't' (the engine's timestamp)")
    rec = {"kind": kind, "t": round(time.time(), 3)}
    rec.update(fields)
    append(run_dir, rec)


def append(run_dir, rec):
    """Append one already-built record as one whole line: strings are cut until the line
    fits under MAX_LINE bytes, and a record that will not fit even cut short becomes a short
    `truncated` line rather than a long one. A field a reader matches on -- a unit's name --
    must be measured by its writer before it gets here: see emit_phase_start."""
    line, n = _line(rec), 1024
    while len(line) >= MAX_LINE and n >= 16:
        line, n = _line(_clip(rec, n)), n // 2
    if len(line) >= MAX_LINE:
        # too many fields to fit even cut short: say so rather than write a long line
        line = _line({"kind": rec.get("kind"), "t": rec.get("t"), "truncated": True})
    path = pathlib.Path(run_dir) / FILE
    with _LOCK:
        try:
            fd = os.open(str(path), _FLAGS, 0o644)
        except OSError:
            return
        try:
            os.write(fd, line)
        except OSError:
            pass
        finally:
            os.close(fd)
