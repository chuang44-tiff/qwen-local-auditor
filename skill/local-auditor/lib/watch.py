"""The watch page, part 1: a run registry, the events.jsonl fold, and the stdlib viewer.

A launcher (Task 2) registers a run before starting it and finishes it on the way out:

    run_id = watch.register("swarm", title, run_path=run, timeout=t, start_server=True)
    ...
    watch.finish(run_id, exit_code)

Pointers live in `$XDG_CACHE_HOME/qwen-agent/watch/<id>.json` (one per run, `run_id_for`
makes the id stable across a resume). `serve()` runs a small HTTP server on
127.0.0.1:8790 (QWEN_WATCH_HOST/QWEN_WATCH_PORT, environment first, then the config
file -- a config line in the `NAME="${NAME:-default}"` form reads as its default) that
answers /health for the liveness probe, /api/runs (`{"runs": [...], "page": version}`)
and /api/run/<id> (the run plus its `"page"`) for the page, and / for
lib/watch_page.html itself -- served with its `__PAGE_VERSION__` placeholder replaced
by the page's version, the first 12 hex digits of the sha256 of the file's bytes
counted BEFORE the replacement, computed once per process (at server start, or on the
first page read): a tab keeps running the code it first loaded, so it compares the
version it loaded with the answers' `page` and reloads itself ONCE when they differ
(sessionStorage keeps that from becoming a loop; the page's side of it, below).
`python lib/watch.py url <id>` prints the link to paste (`http://HOST:PORT/#<id>`). QWEN_WATCH_HOST may be a COMMA-SEPARATED LIST: serve
binds every address on the same port, in one process, one server thread per address, and
the url, the /health probe and server.json name the FIRST one. The viewer STAYS UP:
`serve` records itself in `<watch_dir>/server.json` ({pid, host, port, start}) and waits
until `python lib/watch.py stop` SIGTERMs it (an `idle_exit` above 0 is the old
self-exit, off by default). `python lib/watch.py dismiss <id>` removes an ended or
killed run's pointer now instead of at the prune.

BEST EFFORT: every watch call in a launcher is wrapped so a failure prints at most one
`watch: ...` warning line to stderr and never changes the run's exit code. QWEN_WATCH=0
disables registration and server start (environment first, then the config file);
registration belongs to ONE process: register() sets QWEN_WATCH_CHILD=1 plus
QWEN_WATCH_PARENT=<own pid>, and a later register() refuses unless its own pid is the
registered owner (so a swarm agent's own subprocesses never re-register, while a second
register() in the owning process itself still works).

THE FOLD: fold_events turns the run's events into the page's data -- the current phase
plus one `phases` entry per phase_start (its seat usage: wall, busy, usage, tail, filled
in once the phase finishes), and per-unit timings (started, ended, and the unit_start's
timeout budget) so the run view can draw a per-agent time bar (a unit's `progress` over
its own timeout) and a table of finished phases' usage. _fold_for reads the file
INCREMENTALLY: a lock-guarded module cache keeps, per events file, its identity
(st_ino/st_dev), the bytes folded so far and the raw fold state before _finish_phases,
so each poll folds only what the emitter wrote since the last one and an overnight run
stays cheap; a changed identity, a file shorter than the offset or no entry re-reads
from 0, and every derived number lands on a deep copy.

ACTIVITY: a hook (lib/watch_hook.py) running inside the session appends one line per tool
call to the file QWEN_WATCH_ACTIVITY names -- RUN/activity/<unit>.jsonl for a swarm unit,
RUN/activity.jsonl for a coder loop, <watch_dir>/<id>.activity.jsonl for a single-shot
session. activity_fold turns those lines into each agent's current tool and trimmed input,
how long it has been on it, its last ten finished calls and the thinking/tools split.

WHAT THE PAGE MAY SHOW: names, roles, states, timings, goal or title, exit, event
why/attention text, and -- from the activity lines -- the tool name with the trimmed INPUT
the hook recorded (a path, a pattern, the start of a command). Never a tool's OUTPUT. The
until-done loop's between-rounds `step` is likewise fixed words: the step's name, its
stamp, and the checklist's SIZE while testing -- never what a check printed. The
viewer never reads round-*.json or round-*.prompt.md, transcripts or prompt bodies
(filenames may be counted); report.md is read for its `stop:` line only, and only over the
first 20 lines.
"""
import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import signal
import socketserver
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "qwen-watch"
KINDS = ("swarm", "research", "until-done", "agent")
DEFAULT_HOST = "127.0.0.1"                   # never bind 0.0.0.0 by default
DEFAULT_PORT = 8790
EVENTS_FILE = "events.jsonl"
ACTIVITY_FILE = "activity.jsonl"              # an until-done run's own hook file
ACTIVITY_DIR = "activity"                     # a swarm run's per-unit hook files, in here
ACTIVITY_SUFFIX = ".activity.jsonl"           # a single-shot session's file, beside its
                                              # pointer (never *.json: never a pointer)
SERVER_FILE = "server.json"                    # who holds the port: what `stop` reads
RECENT_KEEP = 15                              # fold_events keeps the last N events
AGENT_HIDE_AFTER = 600                        # s an ended agent run stays in /api/runs
PRUNE_AFTER = 3600                            # s after which a pointer file is deleted
HEALTH_TIMEOUT = 1.0                          # s to wait for /health when probing
ROUND_FILE = re.compile(r"^round-.*\.prompt\.md$")   # counted by NAME, never opened
ID_RE = re.compile(r"^[0-9a-f]{12}$")
UNIT_STATES = ("queued", "running", "done", "cached", "dropped", "not run")
ACTIVITY_KEEP = 10                            # finished calls one fold hands back
WAIT_AFTER = 15.0                             # s a tool may run before it reads as waiting
UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")   # lib/swarm.py's own name rule:
                                              # a hand-built file cannot aim the reader
                                              # outside RUN/activity/
CHECK_STATES = ("PASS", "FAIL", "UNVERIFIED")
ACTIVITY_SMALL = ("state", "cls", "tool", "target", "since", "ctx", "win")


# ---------------------------------------------------------------- the small places

def watch_dir():
    """$XDG_CACHE_HOME/qwen-agent/watch (XDG_CACHE_HOME falls back to ~/.cache)."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "qwen-agent", "watch")


def _config_file():
    """The config file the shells source, resolved by THEIR rule (QWEN_CONFIG, else
    $XDG_CONFIG_HOME/qwen-agent/config, else ~/.config/qwen-agent/config -- exactly
    qwen-agent.sh's QA_CONFIG): the viewer reads host/port from the same file the tool
    does, never a divergent one. Tests pin QWEN_CONFIG to a nonexistent path the way
    test_cli.py does, so the developer's own settings stay out of the suite."""
    p = os.environ.get("QWEN_CONFIG")
    if p:
        return p
    cfg_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(cfg_home, "qwen-agent", "config")


def _setting(name, default=None):
    """A setting from the environment, else from the config file, else the default.
    The config file is read the way the shells that source it would resolve the line:
    `NAME="${NAME:-value}"` (quoted or not) means VALUE when NAME is not in the
    environment -- and it is not, the environment was checked first. A bare
    `NAME=value` line reads as its (quote-stripped) value. When NAME is assigned on
    several lines the LAST assignment wins, exactly as `source` would leave it."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open(_config_file(), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return default
    found = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) == name:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            d = re.match(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}]*)\}$", value)
            if d:                                  # the config.example form: the
                value = d.group(1)                 # default IS the value here
            found = value or default               # keep looking: the LAST line wins
    return default if found is None else found


def _host_list(value=None):
    """Every address the viewer serves: QWEN_WATCH_HOST (or `value`) split on commas,
    whitespace trimmed, empties dropped. A plain address gives a one-address list."""
    raw = _setting("QWEN_WATCH_HOST", DEFAULT_HOST) if value is None else str(value)
    return [h.strip() for h in raw.split(",") if h.strip()]


def watch_host():
    """The canonical address: the FIRST of QWEN_WATCH_HOST's list. The url, the /health
    probe and server.json name this one; serve() binds every address of the list."""
    hosts = _host_list()
    return hosts[0] if hosts else DEFAULT_HOST


def watch_port():
    return int(_setting("QWEN_WATCH_PORT", str(DEFAULT_PORT)))


def run_id_for(path=None):
    """The run's 12-hex-char id: stable over one run_path (a resume reuses the run --
    the path form mixes in nothing but the realpath, so it stays recomputable), random
    without one (a bare agent session has no folder to hash): the pid, the clock AND
    os.urandom(8), because a coarse Windows clock hands two register() calls in one
    process the SAME time.time() and the second pointer would overwrite the first."""
    if path:
        seed = os.path.realpath(str(path))
    else:
        seed = "%d-%f-%s" % (os.getpid(), time.time(), os.urandom(8).hex())
    return hashlib.sha1(seed.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _warn(message):
    """The one warning line a failed watch call may print (the launcher wraps every
    call; nothing here may be able to fail a run any louder than this)."""
    try:
        sys.stderr.write("watch: %s\n" % message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _pointer_path(run_id):
    return os.path.join(watch_dir(), "%s.json" % run_id)


def activity_path(run_id):
    """A single-shot session's activity file, beside its pointer: the file its
    watch_hook.py appends one line per tool call to (QWEN_WATCH_ACTIVITY names it to the
    hook). The suffix keeps it out of `*.json`, so it is never mistaken for a pointer."""
    return os.path.join(watch_dir(), "%s%s" % (run_id, ACTIVITY_SUFFIX))


def _drop_activity(run_id):
    """Remove a run's activity file with its pointer (the prune and `dismiss`): a file
    nobody will ever read again must not sit in the registry forever."""
    if isinstance(run_id, str) and ID_RE.fullmatch(run_id):
        with contextlib.suppress(OSError):
            os.unlink(activity_path(run_id))


def _server_path():
    return os.path.join(watch_dir(), SERVER_FILE)


def server_info():
    """What the running viewer recorded about itself -- {pid, host, port, start} -- or
    None when nothing is recorded (no viewer up, or a file that does not read as a
    JSON object)."""
    info = _read_json(_server_path())
    return info if isinstance(info, dict) else None


def url(run_id=None):
    """The page's url for a run: http://HOST:PORT/#<id> -- the slash before the hash, so
    the link loads the page and the hash then selects the run -- and http://HOST:PORT/
    with no id (host/port per watch_host/watch_port, the FIRST address of the list)."""
    return "http://%s:%d%s" % (watch_host(), watch_port(),
                               ("/#" + run_id) if run_id else "/")


def _read_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    """Write whole: a reader must never see a half pointer (same trick as events.py:
    build the file beside the target, os.replace it over the target)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    data = (json.dumps(obj, indent=1) + "\n").encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _alive(pid):
    """True unless this machine can say the pid is gone. os.kill(pid, 0); off POSIX, or
    on an error other than ProcessLookupError (no permission, an exotic platform), the
    process counts as alive: a watcher must not call a live run killed."""
    if os.name != "posix" or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):            # a tampered pid no kill can even take
        return True                             # (OverflowError is not an OSError)
    return True


# ---------------------------------------------------------------- registry

def register(kind, title, *, run_path=None, pid=None, timeout=None, max_rounds=None,
             start_server=True):
    """Register this run and return its id, or None when watch is switched off
    (QWEN_WATCH=0 -- environment first, then the config file) or when this process is
    not the owner of a registration: a QWEN_WATCH_PARENT of another pid, or the legacy
    QWEN_WATCH_CHILD=1 without a parent pid of our own, means we are a registered run's
    CHILD and must never register one of our own. A second register() inside the
    owning process itself succeeds: a run may register twice (the switch below says
    who owns it). Writes the pointer atomically and keeps the original start on a
    resume; starts the viewer detached when nothing answers /health yet. A port held
    by another service, or a spawn that cannot run, costs one warning line: the run
    continues and its pointer is on disk for whenever a viewer does come up."""
    if _setting("QWEN_WATCH") == "0":
        return None
    own = str(os.getpid())
    parent = os.environ.get("QWEN_WATCH_PARENT")
    if (parent is not None and parent != own
            or os.environ.get("QWEN_WATCH_CHILD") == "1" and parent != own):
        return None                                # a registered run's child, not the owner
    if kind not in KINDS:
        raise ValueError("watch kind must be one of %s, not %r" % (", ".join(KINDS), kind))
    run = str(run_path) if run_path else None
    run_id = run_id_for(run)
    pointer_file = _pointer_path(run_id)
    start = time.time()
    old = _read_json(pointer_file)
    if isinstance(old, dict) and isinstance(old.get("start"), (int, float)):
        start = old["start"]                       # resume: the page keeps seeing when it began
    pointer = {"id": run_id, "kind": kind, "title": title, "run_path": run,
               "pid": os.getpid() if pid is None else pid, "start": start,
               "timeout": timeout, "max_rounds": max_rounds, "end": None, "exit": None}
    _write_json(pointer_file, pointer)
    os.environ["QWEN_WATCH_CHILD"] = "1"           # this process owns the registration:
    os.environ["QWEN_WATCH_PARENT"] = own          # its children never re-register
    if start_server and _setting("QWEN_WATCH_NOSERVE") != "1":
        try:
            answer = _health()
            if answer is None:                      # nothing listening: bring it up
                try:
                    _spawn_server()
                except OSError as e:
                    _warn("the viewer did not start (%s); the run goes on without it" % e)
            elif answer is False:                   # something else holds the port
                _warn("port %d answers but is not %s; the run goes on without the page"
                      % (watch_port(), APP))
        except Exception as e:                      # a broken QWEN_WATCH_PORT config
            _warn("the viewer check failed (%s); the run goes on without it" % e)
    return run_id


def finish(run_id, exit_code):
    """Record this run's end and exit in its pointer. Unknown or None ids (a run that
    was never registered, or one while watch was switched off) are a silent no-op."""
    if not run_id or not ID_RE.fullmatch(run_id):
        return
    pointer = _read_json(_pointer_path(run_id))
    if not isinstance(pointer, dict):
        return
    pointer["end"] = time.time()
    pointer["exit"] = exit_code
    _write_json(_pointer_path(run_id), pointer)


def _health():
    """True when our viewer answers /health, False when something else answers there,
    None when nothing does. Anything that answers at HTTP level -- a 404, an HTML body,
    JSON that is not ours -- is 'not the watch server': spawning onto it only buries a
    second server's port in a detached child's silence, so the caller warns instead."""
    url = "http://%s:%d/health" % (watch_host(), watch_port())
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError:
        return False                                # an answer IS an answer: other server
    except (urllib.error.URLError, OSError, ValueError):
        return None                                 # refused or timed out: nothing listens
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False                                # it answered, and it is not us
    return isinstance(parsed, dict) and parsed.get("app") == APP


def _spawn_server():
    """Start `python lib/watch.py serve` detached: it outlives this launcher's exit,
    and raises OSError (the caller's one warning) when it cannot start at all."""
    argv = [sys.executable, os.path.abspath(__file__), "serve",
            "--host", ",".join(_host_list()), "--port", str(watch_port())]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(argv, **kwargs)


# ---------------------------------------------------------------- the events fold

def _num(value):
    """A stamp or a budget as a number: a finite non-negative int/float, or None for
    anything else (a bool, a string, a NaN, a negative clock skew)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(value):
        return None
    return value


def _empty_fold():
    return {"workflow": None, "goal": None, "phase": None, "phases": [], "units": {},
            "attention": [], "recent": [], "ended": False, "exit": None, "_end_t": None}


def _touch_unit(fold, name, role):
    """The record of one unit, created as queued when the stream mentions it first.
    Besides the state fields the page shows, it keeps the fold's own timings:
    started (the unit_start's t), finished_t (the t of the unit_done or unit_dropped
    that gave the outcome -- cached ones included: a cached unit did no work but its
    outcome has a moment all the same), ended (the t of the unit_done or unit_dropped
    of a unit that WORKED, not set for a cached one) and timeout (unit_start's seconds
    budget), which the phase usage and the per-agent bar read."""
    if not isinstance(name, str) or not name:
        return None
    unit = fold["units"].get(name)
    if unit is None:
        unit = fold["units"][name] = {"role": None, "state": "queued",
                                      "seconds": None, "why": "",
                                      "started": None, "ended": None,
                                      "finished_t": None, "timeout": None}
    if isinstance(role, str) and role:
        unit["role"] = role
    return unit


def fold_events(lines):
    """Fold RUN/events.jsonl into what the page shows:
    {workflow, goal, phase: {label, n, seats, units: [...]}, phases: [...],
     units: {name: {role, state, seconds, why, started, ended, finished_t, timeout}},
     attention: [...], recent: [...], ended, exit}.
    phases is one entry per phase_start -- {label, n, seats, start, end, wall, busy,
    usage, tail, units} -- with the seat-usage numbers filled in for the phases that
    finished (_finish_phases); end is None while a phase is still running.
    A line that does not parse (a truncated last line, garbage) is skipped, never
    raised. A second run_start (a --resume) resets the fold: only events after the
    last one count. recent holds the last 15 events."""
    fold = _fold_into(_empty_fold(), lines)
    _finish_phases(fold)
    return fold


def _fold_into(fold, lines):
    """Fold lines ON TOP of an existing fold state: what fold_events does from the
    empty fold, and what the incremental reader (_fold_for) does from the cached
    one, so the two can never drift apart. A run_start resets the fold mid-stream
    -- the caller must keep what this RETURNS (the reset rebinds; it does not edit
    the argument in place)."""
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue                                # unreadable or half-written: gone
        if not isinstance(rec, dict) or not isinstance(rec.get("kind"), str):
            continue
        kind = rec["kind"]
        if kind == "run_start":
            fold = _empty_fold()
            fold["workflow"] = rec.get("workflow")
            fold["goal"] = rec.get("goal")
        fold["recent"].append(rec)
        if len(fold["recent"]) > RECENT_KEEP:
            fold["recent"].pop(0)
        if kind == "phase_start":
            names = []
            items = rec.get("units")
            if not isinstance(items, (list, tuple)):
                items = []                      # a corrupt units (an int, a string:
            for item in items:                  # anything not a list) folds as empty
                name, role = ((item.get("name"), item.get("role"))
                              if isinstance(item, dict) else (item, None))
                unit = _touch_unit(fold, name, role)
                if unit is None or name in names:
                    continue
                unit.update(state="queued", seconds=None, why="", started=None,
                            ended=None, finished_t=None, timeout=None)  # a re-listed unit
                names.append(name)                                      # starts clean
            n = rec.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                n = len(names)
            seats = rec.get("seats")
            if not isinstance(seats, (int, float)) or isinstance(seats, bool):
                seats = None
            fold["phase"] = {"label": rec.get("label"), "n": n, "seats": seats,
                             "units": names}
            fold["phases"].append({"label": rec.get("label"), "n": n, "seats": seats,
                                   "start": _num(rec.get("t")), "end": None, "wall": None,
                                   "busy": None, "usage": None, "tail": None,
                                   "units": list(names)})
        elif kind == "unit_start":
            unit = _touch_unit(fold, rec.get("unit"), rec.get("role"))
            if unit is not None:
                unit["state"] = "running"
                started = _num(rec.get("t"))
                if started is not None:
                    unit["started"] = started
                timeout = _num(rec.get("timeout"))
                unit["timeout"] = timeout if timeout else None  # 0 is not a budget
        elif kind == "unit_done":
            unit = _touch_unit(fold, rec.get("unit"), rec.get("role"))
            if unit is not None:
                unit["state"] = "cached" if rec.get("cached") else "done"
                seconds = rec.get("seconds")
                unit["seconds"] = (seconds if isinstance(seconds, (int, float))
                                   and not isinstance(seconds, bool)
                                   and seconds >= 0 and math.isfinite(seconds)
                                   else None)  # NaN/Infinity/negative: not a timing;
                                                # json.loads accepts NaN, a page does not
                unit["why"] = rec["why"] if isinstance(rec.get("why"), str) else ""
                unit["finished_t"] = _num(rec.get("t"))  # every outcome is stamped,
                if not rec.get("cached"):                 # cached ones too: the
                    unit["ended"] = _num(rec.get("t"))    # no-roster progress filter
                                                          # needs a moment to test
        elif kind == "unit_dropped":
            unit = _touch_unit(fold, rec.get("unit"), rec.get("role"))
            if unit is not None:
                unit["state"] = "dropped"
                unit["why"] = rec["why"] if isinstance(rec.get("why"), str) else ""
                unit["ended"] = unit["finished_t"] = _num(rec.get("t"))
        elif kind == "attention":
            fold["attention"].append({"item": rec.get("item"), "reason": rec.get("reason"),
                                      "detail": rec.get("detail"), "t": rec.get("t")})
        elif kind == "run_end":
            fold["ended"] = True
            fold["exit"] = rec.get("exit")
            fold["_end_t"] = _num(rec.get("t"))
    return fold


def _finish_phases(fold):
    """Fill in end/wall/busy/usage/tail for every phase that has finished.
    A unit belongs to the latest phase_start that LISTED it; a unit no roster listed
    (the units of a truncated or clipped phase_start, which names nobody) belongs to the
    latest phase that began at or before its unit_start. A phase is finished when a
    later phase_start followed it, when the run ended, or when all n of its units have
    reported an outcome; until then its end (and everything derived from it) is None --
    the run view's usage table is a table of finished phases, and a running phase does
    not belong in it. The formulas: wall = end - start; busy = the sum of each working
    unit's (end - unit_start t), cached units counting 0 (they never started);
    usage = busy / (min(seats, n) x wall), None when wall is 0;
    tail = end - the last unit_start t."""
    phases = fold["phases"]
    if not phases:
        return
    units = fold["units"]
    owner = {}                                     # unit name -> phase index
    for i, ph in enumerate(phases):
        for name in ph["units"]:
            owner[name] = i                        # the LATEST lister wins
    for name, unit in units.items():
        if name in owner:
            continue
        started = _num(unit.get("started"))
        if started is None:
            continue
        for i, ph in enumerate(phases):            # unlisted: the phase it ran inside
            start = ph["start"]
            if isinstance(start, (int, float)) and not isinstance(start, bool) \
                    and started >= start:
                owner[name] = i
    last = len(phases) - 1
    for i, ph in enumerate(phases):
        mine = [u for name, u in units.items() if owner.get(name) == i]
        if not (i != last or fold["ended"]
               or (isinstance(ph["n"], int) and ph["n"] > 0
                   and sum(1 for u in mine
                           if u["state"] in ("done", "cached", "dropped")) >= ph["n"])):
            continue                               # still running: end stays None
        ends = [_num(u.get("ended")) for u in mine if u["state"] != "cached"]
        ends = [e for e in ends if e is not None]
        starts = [_num(u.get("started")) for u in mine]
        starts = [s for s in starts if s is not None]
        if ends:
            ph["end"] = max(ends)
        elif i != last:
            ph["end"] = phases[i + 1]["start"]     # no unit reported: it gave over then
        else:
            ph["end"] = fold["_end_t"] if fold["_end_t"] is not None else ph["start"]
        if _num(ph["end"]) is None or _num(ph["start"]) is None or ph["end"] < ph["start"]:
            continue                               # no trustworthy clock: no numbers
        ph["wall"] = ph["end"] - ph["start"]
        busy = 0.0
        for u in mine:
            if u["state"] == "cached":
                continue                           # a cached unit did no work: 0
            s, e = _num(u.get("started")), _num(u.get("ended"))
            if s is not None and e is not None and e >= s:
                busy += e - s
        ph["busy"] = busy
        seats = ph["seats"]
        if not isinstance(seats, (int, float)) or isinstance(seats, bool) \
                or seats < 1 or not math.isfinite(seats):
            seats = 1                              # unknown seats: one (as the eta does)
        n = ph["n"] if isinstance(ph["n"], int) and ph["n"] > 0 else 1
        ph["usage"] = busy / (min(seats, n) * ph["wall"]) if ph["wall"] > 0 else None
        ph["tail"] = ph["end"] - max(starts) if starts else None


FOLD_CACHE_MAX = 256                              # events files remembered at once

_fold_cache = {}                                  # events path -> {ino, dev, off, fold}
_fold_lock = threading.Lock()


def _fold_for(pointer):
    """The fold of the run's events.jsonl; an empty fold when the run has none (yet,
    or at all: an agent session writes no events file). Read INCREMENTALLY, the design
    spec's byte offset per run: the module cache keeps, per events file, its identity
    (st_ino, st_dev), the bytes folded so far and the raw fold state before
    _finish_phases, and each call reads and folds only the bytes written since the
    last one -- an overnight run costs a poll its new lines, not its whole history.
    Only complete lines fold (up to the last \\n); a trailing partial line (the emitter
    is mid-write) stays unread until its newline arrives. The file is re-read from 0
    when its identity changed, when it is shorter than the offset, or when there is no
    cache entry; a file that cannot be stat'ed or opened drops its entry and folds as
    empty. A run_start in the new bytes (a resume) resets the fold, exactly as a full
    fold would. The viewer is threaded, so the cache is held under a lock, and
    _finish_phases -- like every derivation -- runs on a deep copy: the cached state
    is never mutated. The cache is bounded: gone files drop their entries and no more
    than FOLD_CACHE_MAX are kept -- past the cap, the entry that has been in the cache
    longest (by its last re-read from 0) leaves first. The result equals a full
    fold_events of the same bytes."""
    run_path = pointer.get("run_path") if isinstance(pointer, dict) else None
    if not run_path:
        return _empty_fold()
    path = os.path.join(str(run_path), EVENTS_FILE)
    try:
        st = os.stat(path)
    except OSError:                               # gone (or never there): the fold is
        with _fold_lock:                          # empty and the entry goes with it
            _fold_cache.pop(path, None)
        return _empty_fold()
    with _fold_lock:
        entry = _fold_cache.get(path)
        if (entry is None or entry["ino"] != st.st_ino or entry["dev"] != st.st_dev
                or st.st_size < entry["off"]):
            _fold_cache.pop(path, None)           # re-read from 0: a fresh state, and
            entry = {"ino": st.st_ino, "dev": st.st_dev,      # a fresh spot at the
                     "off": 0, "fold": _empty_fold()}          # cache's fresh end
            _fold_cache[path] = entry
            while len(_fold_cache) > FOLD_CACHE_MAX:
                _fold_cache.pop(next(iter(_fold_cache)))
        try:
            with open(path, "rb") as fh:
                fh.seek(entry["off"])
                data = fh.read()
        except OSError:
            _fold_cache.pop(path, None)
            return _empty_fold()
        cut = data.rfind(b"\n")                   # fold the complete lines only; what
        if cut < 0:                               # follows the last newline is half
            data = b""                            # a line: it stays past the offset
        else:
            entry["off"] += cut + 1
            data = data[:cut + 1]
        entry["fold"] = _fold_into(entry["fold"],
                                   data.decode("utf-8", "replace").splitlines())
        fresh = copy.deepcopy(entry["fold"])      # the caller derives on a copy: the
    _finish_phases(fresh)                         # cached raw state stays untouched
    return fresh


def _phase_progress(fold, units):
    """(progress, eta) for a swarm or research run off the current phase: finished
    (done, cached or dropped) over n; median seconds of the phase's finished non-cached
    units times the remaining units over the seats -- eta waits for the first such unit.
    A phase whose roster never arrived (a truncated or clipped phase_start, which names
    nobody) has no names to count among: its finished set is the distinct units that
    reported unit_done or unit_dropped since that phase_start -- each unit once however
    many times it tried -- and only the units that started are ever listed there
    (queued is n minus the started ones: nothing else knows the other names)."""
    phase = fold.get("phase")
    if not phase:
        return None, None
    n = phase.get("n")
    if not isinstance(n, int) or n <= 0:
        return None, None
    names = phase.get("units") or []
    if names:
        finished = sum(1 for name in names
                       if units.get(name, {}).get("state") in ("done", "cached", "dropped"))
        finished = min(finished, n)
        seconds = [units[name]["seconds"] for name in names
                   if units.get(name, {}).get("state") == "done"
                   and isinstance(units[name].get("seconds"), (int, float))]
    else:                                   # no roster: the units announce themselves
        start = _num(fold["phases"][-1]["start"]) if fold.get("phases") else None
        finished = 0
        seconds = []
        for unit in units.values():
            if unit.get("state") not in ("done", "cached", "dropped"):
                continue
            # the outcome's stamp: finished_t exists for every outcome (a cached
            # unit_done has no ended -- it did no work -- but its t is stamped all the
            # same), so a unit that reported under an EARLIER phase never counts here
            ended = _num(unit.get("finished_t"))
            if ended is None:
                ended = _num(unit.get("ended"))
            if start is not None and ended is not None and ended < start:
                continue                    # finished under an earlier phase, not this one
            finished += 1
            if unit.get("state") == "done" and isinstance(unit.get("seconds"), (int, float)):
                seconds.append(unit["seconds"])
        finished = min(finished, n)
    progress = finished / n
    if not seconds:
        return progress, None
    seats = phase.get("seats")
    if not isinstance(seats, (int, float)) or seats < 1 or not math.isfinite(seats):
        seats = 1
    eta = statistics.median(seconds) * (n - finished) / seats
    return progress, eta


def _unit_progress(unit, now):
    """The per-agent bar for one unit: a running unit is (now - started) over the
    seconds budget its unit_start carried (capped at 1 -- the bar sits full while the
    watchdog has still not cut the unit off); any unit that reported an outcome is its
    seconds over that budget -- done, cached, or dropped, whose unit_done left its
    real seconds behind (a dropped unit spent them too); None when there is no timeout
    to divide by or no time to hang a bar on (queued, or a unit that never ran)."""
    timeout = unit.get("timeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or timeout <= 0 or not math.isfinite(timeout):
        return None
    state = unit.get("state")
    if state == "running":
        started = _num(unit.get("started"))
        if started is None:
            return None
        return min(max((now - started) / timeout, 0.0), 1.0)
    seconds = unit.get("seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) \
            and seconds >= 0 and math.isfinite(seconds):
        return seconds / timeout
    return None


def _rounds(run_path):
    """How many rounds an until-done run has written: the COUNT of the
    round-*.prompt.md filenames, whose contents the page may never show."""
    if not run_path:
        return 0
    try:
        names = os.listdir(str(run_path))
    except OSError:
        return 0
    return sum(1 for name in names if ROUND_FILE.match(name))


def _report_stop(run_path):
    """report.md's `stop:` line once the report exists, read over the first 20 lines
    only -- the page may show why the loop stopped, not what it wrote."""
    if not run_path:
        return None
    try:
        with open(os.path.join(str(run_path), "report.md"),
                  encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    break
                if line.strip().startswith("stop:"):
                    return line.strip()
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- agent activity
# What lib/watch_hook.py wrote, folded into what one agent's card shows. A hook records
# the tool's name and its trimmed INPUT and nothing else, so nothing else can come here.

def _read_json_lines(path):
    """Every JSON object of a lines file, oldest first. A line that does not parse -- a
    half-written tail most of all, since the hook appends while the reader reads -- is
    skipped, never raised, and a file that cannot be read is no lines at all."""
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


def _tool_class(tool, target):
    """Which of the page's verbs a call belongs to: a shell command, a Python or test
    run, a read, an edit, the web, an MCP tool, a delegation -- or none of them. A Bash
    call that runs Python or pytest is a test run, which the page words differently from
    a bare command, so the first word (and pytest anywhere in the line) decides."""
    if not tool:
        return "other"
    if tool == "Bash":
        line = target.strip().lower() if target else ""
        first = line.split(" ", 1)[0]
        if first in ("python", "python3", "pytest", "pip", "uv") or "pytest" in line:
            return "python"
        return "shell"
    if tool in ("Read", "Grep", "Glob"):
        return "read"
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "edit"
    if tool in ("WebFetch", "WebSearch"):
        return "web"
    if tool.startswith("mcp__"):
        return "mcp"
    if tool in ("Task", "Agent"):
        return "delegate"
    return "other"


def activity_fold(path, now):
    """Fold one agent's activity file into what its card shows, or None when there is
    nothing to fold (no file, no readable line, no event among them):

      {state, cls, tool, target, since, calls, thinking_s, tools_s, ctx, win}

    `state` is "thinking" (the model is composing: the last call answered and none is
    open), "tool" (a call is running, under WAIT_AFTER s of it), "waiting" (the same call
    has been running WAIT_AFTER s or longer) or "done" (the agent stopped). `since` is
    when that state began; `cls`, `tool` and `target` name what is running now. `calls`
    is the last ACTIVITY_KEEP finished calls, each {tool, target, seconds, ok, cls}; the
    ones closed without a post carry `abandoned: true` and the `pre_t` of their
    unanswered ask as well; `thinking_s` and `tools_s` split the time between composing
    and working. `ctx` and `win` are the latest context reading and the window it
    belongs to, from the hook's own lines.

    An answer that never came does not pin the card: a `pre` still open when a NEW `pre`
    arrives AND at least one `post` has come in after that older call started got no
    reply -- the agent moved on -- and is closed as ABANDONED (it joins `calls` with
    `seconds: None`, `ok: false`, `abandoned: true` and the `pre_t` of its own `pre`,
    and adds nothing to `tools_s`). A post that arrives LATE reclaims the abandoned call
    it belongs to -- once no open `pre` of the same tool AND target wins it, and before
    the looser same-tool / any-tool pairings are tried: its `seconds` are the post's t
    minus the kept `pre_t` (billed to `tools_s` like any finished call), its `ok` is the
    post's and `abandoned` is cleared -- an unrelated open `pre` is never mispaired with
    it. A subagent call (Task/Agent) is exempt from the abandon
    rule: the tool calls running inside it are its own doing, so it stays open until
    its post or the stop -- and a `stop` line closes every call still open, subagent
    calls included, their round being over.
    """
    events = []
    ctx = win = None
    if not path:
        return None                                 # no file named: nothing to fold, no error
    for rec in _read_json_lines(path):
        ev = rec.get("ev")
        if ev not in ("pre", "post", "stop"):
            continue
        t = _num(rec.get("t"))
        if t is None:
            continue
        for name in ("ctx", "win"):                 # the LAST line that carried one wins
            value = rec.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                if name == "ctx":
                    ctx = value
                else:
                    win = value
        tool, target = rec.get("tool"), rec.get("target")
        events.append({"ev": ev, "t": t,
                       "tool": tool if isinstance(tool, str) and tool else None,
                       "target": target if isinstance(target, str) and target else None,
                       "ok": rec.get("ok") is not False})
    if not events:
        return None
    open_calls, calls, tools_s = [], [], 0.0
    last_post = None                                # event index of the newest post seen
    abandoned_at = {}                               # (tool, target) -> indexes into calls
                                                    # of its ABANDONED entries, oldest
                                                    # first: the index of the newest is
                                                    # the LAST -- the reclaim below is a
                                                    # lookup, not a scan of `calls` (the
                                                    # fold re-runs whole on every poll)

    def abandon(oc):                                # a call that never got its answer
        calls.append({"tool": oc["tool"], "target": oc["target"], "seconds": None,
                      "ok": False, "abandoned": True, "pre_t": oc["t"],
                      "cls": _tool_class(oc["tool"], oc["target"])})
        abandoned_at.setdefault((oc["tool"], oc["target"]), []).append(len(calls) - 1)

    for seq, e in enumerate(events):
        if e["ev"] == "pre":
            # The agent moved on: a call still open from BEFORE the last answer got no
            # reply -- hook missed, tool denied, crash -- and is closed as abandoned. A
            # subagent call is exempt: the calls running inside it open new pres while
            # it is itself still open. Two pres back to back abandon nothing (nothing
            # was answered between them), so parallel calls keep their own durations.
            if last_post is not None:
                for oc in [c for c in open_calls
                           if c["seq"] < last_post and c["tool"] not in ("Task", "Agent")]:
                    open_calls.remove(oc)
                    abandon(oc)
            e["seq"] = seq
            open_calls.append(e)                    # open until its post closes it
            continue
        if e["ev"] == "stop":
            for oc in open_calls:                   # the round is over: every still-open
                abandon(oc)                         # call is closed, subagent calls in
            open_calls = []                         # included -- nothing runs any more
            continue
        if e["ev"] != "post":
            continue
        last_post = seq
        # Pair an answer with its own ask: the most recent open pre of the SAME tool
        # and the SAME target first (two parallel Reads of different files keep their
        # own durations). When no OPEN pre carries the answer's tool AND target, its
        # ask may already have been closed as abandoned: a post that arrives LATE
        # reclaims that call -- seconds from its kept pre_t, ok from the post, no
        # longer abandoned, billed to tools_s -- rather than mispairing with an
        # unrelated open pre. Only then the most recent open pre of the same tool (an
        # answer that came back under another target is still that tool's), else -- as
        # today -- the most recent open pre of any tool at all.
        i = next((j for j in range(len(open_calls) - 1, -1, -1)
                  if open_calls[j]["tool"] == e["tool"]
                  and open_calls[j]["target"] == e["target"]), None)
        if i is None:
            stack = abandoned_at.get((e["tool"], e["target"]))
            if stack:                               # the most recent abandoned entry
                k = stack.pop()                     # of this tool and target (popped:
                if not stack:                       # a second late post must not
                    del abandoned_at[(e["tool"], e["target"])]   # reclaim it twice)
                oc = calls[k]                       # re-open the call this answer is for
                span = e["t"] - oc["pre_t"]
                if span >= 0 and math.isfinite(span):
                    oc["seconds"], tools_s = span, tools_s + span
                oc["ok"] = e["ok"]
                del oc["abandoned"]
                continue
            i = next((j for j in range(len(open_calls) - 1, -1, -1)
                      if open_calls[j]["tool"] == e["tool"]), None)
        if i is None and open_calls:
            i = len(open_calls) - 1
        pre = open_calls.pop(i) if i is not None else None   # an answer with no ask
        seconds = None
        if pre is not None:
            span = e["t"] - pre["t"]
            if span >= 0 and math.isfinite(span):
                seconds, tools_s = span, tools_s + span
            tool = e["tool"] or pre["tool"]
            target = e["target"] or pre["target"]
        else:
            tool, target = e["tool"], e["target"]
        calls.append({"tool": tool, "target": target, "seconds": seconds,
                      "ok": e["ok"], "cls": _tool_class(tool, target)})
    last, live = events[-1], (open_calls[-1] if open_calls else None)
    if last["ev"] == "stop":
        state, since, tool, target = "done", last["t"], None, None
    elif live is not None:
        state = "tool" if now - live["t"] < WAIT_AFTER else "waiting"
        since, tool, target = live["t"], live["tool"], live["target"]
    else:
        state, since, tool, target = "thinking", last["t"], None, None
    return {"state": state,
            "cls": _tool_class(tool, target) if state in ("tool", "waiting") else None,
            "tool": tool, "target": target, "since": since,
            "calls": calls[-ACTIVITY_KEEP:],
            "thinking_s": max(0.0, (now if state == "thinking" else last["t"])
                              - events[0]["t"] - tools_s),
            "tools_s": tools_s, "ctx": ctx, "win": win}


def _finished_activity(folded):
    """What a finished unit keeps of its activity: the calls it ran and how its time went
    -- not a tool it is no longer using."""
    if not folded:
        return None
    return {"state": "done", "calls": folded["calls"],
            "thinking_s": folded["thinking_s"], "tools_s": folded["tools_s"]}


def _small_activity(folded):
    """The overview's share of an activity fold: enough for one status line and the
    context meter, without the call list the run view folds again."""
    if not folded:
        return None
    return {key: folded[key] for key in ACTIVITY_SMALL}


def _activity_file(pointer):
    """Where a run's own hook lines land: RUN/activity.jsonl for an until-done loop, the
    session's file beside its pointer for a single-shot agent. None for a kind that has
    no file of its own -- a swarm's units each append to their own, below."""
    kind = pointer.get("kind") if isinstance(pointer, dict) else None
    if kind == "until-done" and pointer.get("run_path"):
        return os.path.join(str(pointer["run_path"]), ACTIVITY_FILE)
    if kind == "agent" and isinstance(pointer.get("id"), str) \
            and ID_RE.fullmatch(pointer["id"]):
        return activity_path(pointer["id"])
    return None


def _unit_activity_file(run_path, name):
    """RUN/activity/<unit>.jsonl, when the name is one lib/swarm.py would ever have
    accepted: the fold must not be aimed outside the folder by a hand-built event."""
    if not run_path or not isinstance(name, str) or not UNIT_NAME.fullmatch(name):
        return None
    return os.path.join(str(run_path), ACTIVITY_DIR, "%s.jsonl" % name)


def _checks_view(rec):
    """A `checks` event as the page wants it: the counts, and each item's text (the
    emitter trimmed it to 100 characters) with the state it ended in."""
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    listed = rec.get("items")
    items = []
    for item in listed if isinstance(listed, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        status = item.get("status")
        items.append({"text": text[:100] if isinstance(text, str) else "",
                      "status": status if isinstance(status, str) and status in CHECK_STATES
                      else "UNVERIFIED"})
    return {"round": count(rec.get("round")), "passed": count(rec.get("passed")),
            "failed": count(rec.get("failed")),
            "unverified": count(rec.get("unverified")), "total": count(rec.get("total")),
            "items": items}


# The loop's own six kinds, in order of a round: what the page reads its step from.
STEP_KINDS = ("round_start", "checks_start", "checks", "round_end", "audit_start", "audit_end")
STEP_NAMES = {"round_start": "coding", "checks_start": "testing", "audit_start": "reviewing"}


def _loop_view(run_path):
    """(round, checks, step) of an until-done run, off RUN/events.jsonl: the latest
    round_start (its `n`, timeout, started and review flag) and the latest checks event,
    plus the step the loop is on -- what the LATEST of the loop's six kinds (round_start,
    checks_start, checks, round_end, audit_start, audit_end) says: {"name": "coding" for
    a round_start, "testing" for a checks_start (carrying its "total"), "reviewing" for an
    audit_start, "between" for anything else; "since": the event's t}. None when the loop
    wrote no such line. Never off a round-*.json -- those hold the agent's own reply,
    which belongs to the report and not to the page."""
    round_, checks, step = None, None, None
    if not run_path:
        return None, None, None
    for rec in reversed(_read_json_lines(os.path.join(str(run_path), EVENTS_FILE))):
        kind = rec.get("kind")
        if step is None and kind in STEP_KINDS:
            step = {"name": STEP_NAMES.get(kind, "between"), "since": _num(rec.get("t"))}
            if kind == "checks_start":
                total = rec.get("total")
                step["total"] = (total if isinstance(total, int) and not isinstance(total, bool)
                                 else None)
        if round_ is None and kind == "round_start":
            n = rec.get("round")
            round_ = {"n": n if isinstance(n, int) and not isinstance(n, bool) else None,
                      "timeout": _num(rec.get("timeout")), "started": _num(rec.get("t")),
                      "review": rec.get("review") is True}
        if checks is None and kind == "checks":
            checks = _checks_view(rec)
        if round_ is not None and checks is not None and step is not None:
            break
    return round_, checks, step


def run_state(pointer, now):
    """The pointer plus state, elapsed, progress and eta (and the folded units, so a
    killed run's page can show its unfinished units as not run). state: ended when the
    pointer carries an end, killed when its pid is dead and end is null, running
    otherwise. progress is a 0..1 float or None, eta seconds or None. Agent progress is
    elapsed over the timeout; until-done progress is the round-file count over
    max_rounds; swarm and research follow the current phase (_phase_progress)."""
    st = dict(pointer) if isinstance(pointer, dict) else {}
    fold = _fold_for(st)
    end = st.get("end")
    pid = st.get("pid")
    if end is not None:
        state = "ended"
    elif pid is not None and not _alive(pid):
        state = "killed"
    else:
        state = "running"
    start = st.get("start")
    elapsed = None
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        when = end if isinstance(end, (int, float)) and not isinstance(end, bool) else now
        elapsed = max(0.0, when - start)
    units = fold["units"]
    if state == "killed":
        # a process killed outright leaves its running and queued units as they were:
        # they never ran, and the page must not keep a spinner turning on them.
        units = {name: (dict(unit, state="not run")
                        if unit["state"] in ("queued", "running") else unit)
                 for name, unit in units.items()}
    progress, eta = None, None
    kind = st.get("kind")
    if kind in ("swarm", "research"):
        progress, eta = _phase_progress(fold, units)
    elif kind == "agent":
        timeout = st.get("timeout")
        if (elapsed is not None and isinstance(timeout, (int, float))
                and not isinstance(timeout, bool) and timeout > 0):
            progress = min(elapsed / timeout, 1.0)
    elif kind == "until-done":
        max_rounds = st.get("max_rounds")
        if (isinstance(max_rounds, (int, float)) and not isinstance(max_rounds, bool)
                and max_rounds > 0):
            progress = min(_rounds(st.get("run_path")) / max_rounds, 1.0)
    st.update({"state": state, "elapsed": elapsed, "progress": progress, "eta": eta,
               "units": units, "phase": fold["phase"],          # the overview's label
               "attention_count": len(fold["attention"])})       # and its warning chip
    return st


# ---------------------------------------------------------------- the viewer

def run_detail(run_id, now):
    """One run's page data: run_state plus the fold (workflow, goal, phase, phases,
    attention, recent, ended), a per-unit `progress` for the agent rows' bars
    (_unit_progress), and, for an until-done run, the rounds count and report.md's
    stop: line. Besides those: `activity` on the run itself for a single-shot session or
    a coder loop, `activity` on each unit whose hook file exists for a swarm or research
    run, and an until-done run's `round`, `checks` and `step`, read off the loop's own
    events (the step is None once the run has ended).
    None when no such pointer exists."""
    if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
        return None
    pointer = _read_json(_pointer_path(run_id))
    if not isinstance(pointer, dict):
        return None
    st = run_state(pointer, now)
    fold = _fold_for(pointer)
    for key in ("workflow", "goal", "phase", "phases", "attention", "recent", "ended"):
        st[key] = fold[key]
    for unit in st["units"].values():
        unit["progress"] = _unit_progress(unit, now)
    if st.get("exit") is None:
        # the pointer's exit (finish) wins; a run killed before finish still shows
        # what its run_end event said
        st["exit"] = fold["exit"]
    if st.get("kind") == "until-done":
        st["rounds"] = _rounds(st.get("run_path"))
        st["report_stop"] = _report_stop(st.get("run_path"))
        st["round"], st["checks"], step = _loop_view(st.get("run_path"))
        # what the loop is doing right now; a run that has ended has no "right now",
        # so its step is None -- the page must not keep a testing spinner turning
        st["step"] = None if st["state"] == "ended" else step
    run_file = _activity_file(st)
    if run_file:
        # what the single-shot session or the coder loop is doing right now
        st["activity"] = activity_fold(run_file, now)
    elif st.get("kind") in ("swarm", "research") and st.get("run_path"):
        # likewise each unit that has a file: live while it runs, its calls once it is done
        for name, unit in st["units"].items():
            path = _unit_activity_file(st["run_path"], name)
            folded = activity_fold(path, now) if path else None
            if folded is not None:
                unit["activity"] = (folded if unit["state"] == "running"
                                    else _finished_activity(folded))
    return st


def _last_activity(pointer):
    """When a killed run was last heard of: its last event, else when it started;
    None when neither says (a pointer with no start is kept, not pruned)."""
    if not isinstance(pointer, dict):
        return None
    stamps = [rec["t"] for rec in _fold_for(pointer)["recent"]
              if isinstance(rec, dict) and isinstance(rec.get("t"), (int, float))]
    start = pointer.get("start")
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        stamps.append(start)
    return max(stamps) if stamps else None


def list_runs(now):
    """run_state for every pointer, newest first: an ended agent run whose end is more
    than AGENT_HIDE_AFTER s ago is hidden, and a pointer whose end (or, when killed,
    last activity) is more than PRUNE_AFTER s ago is deleted here -- its activity file
    with it, so the registry holds nothing a gone run left behind. For the overview's
    agent and coder-loop cards it carries the small `activity` (state, cls, tool, target,
    since, ctx, win) and, for a coder loop, round.n and the checks' passed/total."""
    try:
        names = os.listdir(watch_dir())
    except OSError:
        return []
    runs = []
    for name in names:
        if not name.endswith(".json"):
            continue
        pointer_file = os.path.join(watch_dir(), name)
        pointer = _read_json(pointer_file)
        if not isinstance(pointer, dict) or not isinstance(pointer.get("id"), str) \
                or not ID_RE.fullmatch(pointer["id"]):
            continue                            # same id contract as run_detail
        st = run_state(pointer, now)
        if st["state"] == "ended":
            when = pointer.get("end") if isinstance(pointer.get("end"), (int, float)) else None
        elif st["state"] == "killed":
            when = _last_activity(pointer)
        else:
            when = None
        if when is not None and now - when > PRUNE_AFTER:
            with contextlib.suppress(OSError):
                os.unlink(pointer_file)
            _drop_activity(pointer["id"])        # its activity file goes with it
            continue
        if (st["state"] == "ended" and pointer.get("kind") == "agent"
                and isinstance(pointer.get("end"), (int, float))
                and now - pointer["end"] > AGENT_HIDE_AFTER):
            continue
        if st.get("kind") == "until-done":
            # the overview's "Round N of M" and the stop reason: /api/runs carries what
            # run_detail computes, so the page reads r.rounds/r.report_stop from here
            st["rounds"] = _rounds(st.get("run_path"))
            st["report_stop"] = _report_stop(st.get("run_path"))
            round_, checks, step = _loop_view(st.get("run_path"))
            st["round"] = None if round_ is None else {"n": round_["n"]}
            st["checks"] = (None if checks is None
                            else {"passed": checks["passed"], "total": checks["total"]})
            # the card's between-rounds status line: name and stamp only, no total --
            # an ended run has no "right now" and exposes no step
            st["step"] = (None if step is None or st["state"] == "ended"
                          else {"name": step["name"], "since": step["since"]})
        run_file = _activity_file(st)
        if run_file:
            # one status line and the context meter per agent or coder loop, no call list
            st["activity"] = _small_activity(activity_fold(run_file, now))
        runs.append(st)
    runs.sort(key=lambda st: st.get("start") if isinstance(st.get("start"),
                                                            (int, float)) else 0,
              reverse=True)
    return runs


NOT_FOUND = b"watch: not found\n"


# ---------------------------------------------------------------- the page version
# A tab keeps running the page code it first loaded: after the viewer restarts on newer
# code the tab polls the new API with old JavaScript and the new states never show. The
# version below is what lets the tab notice -- the served page and every /api answer
# carry the same hash, and the page reloads itself once when they differ.

PAGE_FILE = "watch_page.html"
PAGE_PLACEHOLDER = b"__PAGE_VERSION__"        # the page says var PAGE_VERSION="...";
PAGE_VERSION_HEX = 12                         # leading hex digits of the file's sha256

_page_lock = threading.Lock()
_page_state = {}                              # {"raw": bytes, "version": str}, read once


def _read_page():
    """(the page file's bytes, its version) read ONCE and kept for the life of the
    process: the version is the first 12 hex digits of the sha256 of those bytes,
    counted BEFORE the `__PAGE_VERSION__` replacement (the file on disk still carries
    the placeholder), and serving the same bytes and the same version every time is
    what makes the tab's comparison trustworthy -- the page's own PAGE_VERSION and the
    `page` the API answers share ONE read. Raises OSError when the file cannot be read
    (the caller's 404); a failed read is not remembered: a page that appears later
    serves."""
    with _page_lock:
        if not _page_state:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   PAGE_FILE), "rb") as fh:
                raw = fh.read()
            _page_state["raw"] = raw
            _page_state["version"] = hashlib.sha256(raw).hexdigest()[:PAGE_VERSION_HEX]
        return _page_state["raw"], _page_state["version"]


def page_version():
    """The page's version string: see _read_page. Computed once per process -- serve()
    asks when the server starts, the routes ask on the first page read -- and stable
    after that, which is the whole point: a tab left open across a restart sees the new
    hash in the API answers, reloads itself, and not again for it within the guard
    window. "" when the page file cannot be read: the page must not reload itself on a
    version that is not a version."""
    try:
        return _read_page()[1]
    except OSError:
        return ""


class _Handler(BaseHTTPRequestHandler):
    """The viewer's routes: GET /health, /api/runs (the runs list wrapped, with the
    page's version beside it), /api/run/<id> (the run plus the same `"page"`) and /
    (the page itself, its placeholder replaced at serve time); any other path is 404,
    and so is every method we declare except GET (a verb the stdlib does not know is
    its own 501). Quiet by default: a watcher that shouts at every request is a
    watcher the user closes."""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)              # a polling page that closes the tab
                                                # mid-read is not a server-side fault

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode("utf-8"), "application/json")

    def _missing(self):
        self._send(404, NOT_FOUND, "text/plain; charset=utf-8")

    def do_GET(self):
        self.server.last_request = time.time()
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._json({"app": APP})
        elif path == "/api/runs":
            # the bare list wrapped the way the page reads it, with the version beside
            # it: the tab compares `page` against the PAGE_VERSION it loaded (render)
            self._json({"runs": list_runs(time.time()), "page": page_version()})
        elif path.startswith("/api/run/"):
            st = run_detail(path[len("/api/run/"):], time.time())
            if st is None:
                self._missing()
            else:
                st["page"] = page_version()       # a fresh dict per call (run_detail)
                self._json(st)
        elif path == "/":
            self._page()
        else:
            self._missing()

    def _page(self):
        try:
            raw, version = _read_page()
        except OSError:
            self._missing()
            return
        self._send(200, raw.replace(PAGE_PLACEHOLDER, version.encode("ascii")),
                   "text/html; charset=utf-8")

    def do_HEAD(self):
        self._missing()

    def do_POST(self):
        self._missing()

    def do_PUT(self):
        self._missing()

    def do_PATCH(self):
        self._missing()

    def do_DELETE(self):
        self._missing()

    def do_OPTIONS(self):
        self._missing()


class _ViewerServer(ThreadingHTTPServer):
    """The viewer's server, with the name lookup taken out. HTTPServer.server_bind
    calls socket.getfqdn(host) -- a reverse DNS lookup that can hang a long time on
    macOS runners (and on any machine with slow DNS): the viewer never came up, no
    server.json was ever written, and the launcher's health probe waited forever.
    This subclass binds through the plain socketserver and names itself from the
    address it was GIVEN; getfqdn is never asked."""

    def server_bind(self):
        host = self.server_address[0]                  # the host string as GIVEN,
        socketserver.TCPServer.server_bind(self)       # before bind rewrites it
        self.server_name = host                        # a name, never a lookup
        self.server_port = self.server_address[1]      # the port ACTUALLY bound (port 0)


def make_server(host, port, idle_exit=1800):
    """The viewer's HTTP server, bound and ready (serve_forever() it; tests bind port
    0 and read server_address for the port they got)."""
    httpd = _ViewerServer((host, int(port)), _Handler)
    httpd.daemon_threads = True
    httpd.last_request = time.time()
    return httpd


def _record_server(host, port):
    """server.json: this viewer's {pid, host, port, start}, the one place `stop` looks.
    Written whole (an os.replace) so a reader never sees half of it; a watch dir that
    cannot be written costs the record only -- the viewer still serves."""
    with contextlib.suppress(OSError):
        _write_json(_server_path(), {"pid": os.getpid(), "host": host, "port": int(port),
                                     "start": time.time()})


def _clear_server_record():
    """Remove OUR server.json on the way out. A record another viewer wrote since (a
    different pid) is left exactly as it is: that one is still running and still must be
    findable by `stop`."""
    info = server_info()
    if isinstance(info, dict) and info.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            os.unlink(_server_path())


def _serve_quietly(httpd):
    """One extra address's serve_forever, in its own thread: a server that dies takes
    only its address with it -- the other addresses keep answering."""
    with contextlib.suppress(Exception):
        httpd.serve_forever()


def serve(host, port, idle_exit=0):
    """Run the viewer until it is stopped, on EVERY address `host` names: a comma
    list binds them all on the same port, in this ONE process -- the first bound
    address is served by this thread, each further one by its own server thread. An
    address that cannot bind (not ours to assign) costs one `watch: ...` warning and
    the others still serve; if NONE binds, serve returns 1 without recording anything.
    `idle_exit` 0 (the default) means NEVER exit on idle -- the page is there when the
    user comes back, and `watch.py stop` ends it. An `idle_exit` above 0 is the old
    self-exit, for a caller that wants it (it watches the LAST request across all the
    addresses). On start the server records itself in <watch_dir>/server.json -- the
    FIRST bound address, the one the url and the /health probe name -- and removes that
    on a clean exit (a SIGTERM from `stop` ends the process before any cleanup, which
    is why `stop` removes the file itself). Returns the exit code: 0, or 1 when
    nothing bound."""
    hosts = _host_list(host)
    if not hosts:
        _warn("no address to serve on")
        return 1
    servers = []
    for h in hosts:
        try:
            servers.append(make_server(h, port, idle_exit=idle_exit))
        except OSError as e:
            _warn("%s:%s cannot be bound (%s); the other addresses still serve"
                  % (h, port, e.strerror or e))
    if not servers:
        return 1
    first = servers[0]
    page_version()      # the page's version is computed once, at server start: the
    # served page and every later API answer carry this one, restart or no restart
    _record_server(first.server_address[0], first.server_address[1])
    # port 0: record the port we ACTUALLY got
    for extra in servers[1:]:
        threading.Thread(target=_serve_quietly, args=(extra,), daemon=True).start()
    if idle_exit and idle_exit > 0:
        def _close_when_idle():
            while True:
                time.sleep(min(60.0, max(1.0, idle_exit / 4.0)))
                if all(time.time() - s.last_request > idle_exit for s in servers):
                    for s in servers:               # a failed shutdown retries on the
                        with contextlib.suppress(Exception):
                            s.shutdown()            # next tick: the exit must not die
                    return
        threading.Thread(target=_close_when_idle, daemon=True).start()
    try:
        first.serve_forever()
    finally:
        for s in servers:                           # the extra threads end with theirs
            with contextlib.suppress(Exception):    # (a stub server has no shutdown)
                s.shutdown()
            with contextlib.suppress(Exception):
                s.server_close()
        _clear_server_record()
    return 0


def stop():
    """`python lib/watch.py stop`: end the running viewer -- SIGTERM the pid server.json
    names and remove that file. One line says what happened: `watch: stopped` when a
    process was signalled, `watch: not running` when nothing was there to stop -- no
    record, a record that does not read, a pid already gone, or a pid that is not ours to
    signal. `stop` removes server.json in every one of those cases: a record this command
    cannot act on is stale by definition, and leaving it would tell the next register to
    leave a viewer alone that nobody can stop. Returns the exit code: 0 either way --
    `stop` failing to find a viewer is not a failure of the command that asked."""
    path = _server_path()
    info = server_info()
    pid = info.get("pid") if isinstance(info, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        with contextlib.suppress(OSError):
            os.unlink(path)
        print("watch: not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OverflowError):
        # OverflowError: a record whose "pid" is not a pid at all (a hand-edited file),
        # which is as gone as a process that already exited.
        with contextlib.suppress(OSError):
            os.unlink(path)                       # gone without a chance to clean up
        print("watch: not running")
        return 0
    except OSError:
        # A pid that is not ours to signal (EPERM: a pid reused by another user's process,
        # or a hand-written record), or a platform that signals no one. Nothing this
        # command may end, so the record is as stale as a dead viewer's and goes too: a
        # server.json naming a process we can neither stop nor own is the one file the
        # next register must not have to work around.
        with contextlib.suppress(OSError):
            os.unlink(path)
        print("watch: not running")
        return 0
    with contextlib.suppress(OSError):
        os.unlink(path)
    print("watch: stopped")
    return 0


def dismiss(run_id):
    """`python lib/watch.py dismiss <id>`: clear a finished run's card NOW -- delete
    its pointer file when the run is ended or killed, instead of waiting out the prune.
    A run that is still running keeps its pointer and its card (and says so, exit 1),
    and an unknown or invalid id is refused the same way. The run's activity file goes
    with the pointer: a card nobody can see again leaves no lines behind. One line on
    stdout, like `stop`; returns the exit code."""
    if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
        print("watch: no such run %s" % run_id)
        return 1
    path = _pointer_path(run_id)
    pointer = _read_json(path)
    if not isinstance(pointer, dict):
        print("watch: no such run %s" % run_id)
        return 1
    if run_state(pointer, time.time())["state"] == "running":
        print("watch: %s is still running" % run_id)
        return 1
    with contextlib.suppress(OSError):
        os.unlink(path)
    _drop_activity(run_id)
    print("watch: dismissed %s" % run_id)
    return 0


def main(argv=None):
    """python lib/watch.py serve [--host H] [--port P]   (defaults: every address in
    QWEN_WATCH_HOST -- a comma list -- on 127.0.0.1:8790, env or config);
    python lib/watch.py url [<id>]  prints http://HOST:PORT/#<id> (the first address);
    python lib/watch.py stop  ends the running viewer;
    python lib/watch.py dismiss <id>  removes a finished run's pointer now."""
    ap = argparse.ArgumentParser(prog="watch.py",
                                 description="the qwen-agent watch page viewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the viewer until `stop` ends it")
    p_serve.add_argument("--host", default=None,
                         help="default QWEN_WATCH_HOST or 127.0.0.1; a comma-separated"
                              " list binds every address on the same port")
    p_serve.add_argument("--port", type=int, default=None,
                         help="default QWEN_WATCH_PORT or 8790")
    p_url = sub.add_parser("url", help="print the page's url")
    p_url.add_argument("id", nargs="?", default=None)
    sub.add_parser("stop", help="stop the running viewer")
    p_dismiss = sub.add_parser("dismiss", help="remove a finished run's pointer now")
    p_dismiss.add_argument("id")
    o = ap.parse_args(argv)
    if o.cmd == "stop":
        # no host or port read: `stop` goes to server.json's pid, and a junk
        # QWEN_WATCH_PORT must not keep a viewer from being stopped
        return stop()
    if o.cmd == "dismiss":
        # likewise: the pointer file is all `dismiss` touches, and a junk
        # QWEN_WATCH_PORT must not keep a finished card from being cleared
        return dismiss(o.id)
    if o.cmd == "serve":
        # --host/--port belong to this subcommand only (they are undefined on the
        # others' Namespaces); the bare defaults are the QWEN_WATCH_HOST list (every
        # address binds it on the same port) and QWEN_WATCH_PORT
        host = o.host or ",".join(_host_list())
        port = o.port if o.port is not None else watch_port()
        return serve(host, port)
    print(url(o.id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
