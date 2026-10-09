"""RUN/events.jsonl: the run's progress as one JSON object per line, for a watcher.

A swarm runs headless; a session that launched it learns what happens by following this
file (tail -F, or the Monitor tool) instead of re-reading run.log. Only the runner
process writes it -- its worker threads included -- so a process-wide lock plus ONE
os.write of each whole line (O_APPEND) keeps lines whole. A line is under 4 KB: long
string fields are cut until it fits. Readers must still skip a line that does not parse
or has no trailing newline (a reader can catch a write in flight).

Every line has "kind" and "t" (unix time). The engine's own kinds are ENGINE_KINDS:
  run_start    workflow, goal, run, resumed, depth, knobs   (runner._start)
  unit_done    unit, role, ok, cached, seconds, why (only when not ok)   (swarm.py)
  unit_dropped unit, role, why                              (swarm.py)
  claude_call  name, item, state, seconds, cost_usd         (claude_check)
  claude_probe name, state, why                             (claude_check)
  run_end      exit, report (path or null)                  (runner._start, every exit
                                                            once the run holds its lock)
A workflow adds its own kinds through wf.event(kind, **fields); "attention" (fields
item, reason, detail) means "the main session should look at this".
Writing is best effort: an event that cannot be written never fails the run.
"""
import json
import os
import pathlib
import threading
import time

FILE = "events.jsonl"
ENGINE_KINDS = frozenset({"run_start", "unit_done", "unit_dropped", "claude_call",
                          "claude_probe", "run_end"})
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


def emit(run_dir, kind, **fields):
    """Append one event to RUN/events.jsonl: {"kind", "t", **fields}."""
    if "t" in fields:
        raise ValueError("an event field may not be named 't' (the engine's timestamp)")
    rec = {"kind": kind, "t": round(time.time(), 3)}
    rec.update(fields)
    line, n = _line(rec), 1024
    while len(line) >= MAX_LINE and n >= 16:
        line, n = _line(_clip(rec, n)), n // 2
    if len(line) >= MAX_LINE:
        # too many fields to fit even cut short: say so rather than write a long line
        line = _line({"kind": kind, "t": rec["t"], "truncated": True})
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
