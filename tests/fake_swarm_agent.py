"""A stand-in for qwen-agent in swarm/research tests. Thread-safe: no shared counter.

Reads its own argv: --role-file R, -f PROMPT, -C CWD, optional --resume SID.
Behaviour comes from $FAKE_SWARM_DIR/<role-stem>.py if present: a python file defining
answer(prompt: str, resumed: bool) -> (rc, text). Otherwise it answers '```json\n[]\n```'.
Concurrency is observed with marker files in $FAKE_SWARM_DIR/live/: every agent appends
"<role-stem> <count>" (the count of markers it saw, itself included) as one line to
$FAKE_SWARM_DIR/counts.
Every call appends its argv as a JSON line to $FAKE_SWARM_DIR/calls.jsonl.
With --preflight-only in argv it exits int($FAKE_PREFLIGHT_RC, default 0) before anything
else, printing "fake preflight failed" to stderr when non-zero (the research preflight hook).
$FAKE_SWARM_RC_RECORD ("6,7"): also print the JSON record when the behaviour returns one
of those non-zero rcs (with "result": "" for 6). Without a record, stdout stays empty:
a swarm that finds no session_id there has nothing to repair.
"""
import json
import os
import pathlib
import signal
import sys
import time
import uuid

argv = sys.argv[1:]
if "--preflight-only" in argv:
    rc = int(os.environ.get("FAKE_PREFLIGHT_RC", "0"))
    if rc:
        sys.stderr.write("fake preflight failed\n")
    sys.exit(rc)
d = pathlib.Path(os.environ["FAKE_SWARM_DIR"])


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def append_line(path, line):
    # Several fake agents append to the same file at once. On Windows concurrent
    # appends can overwrite each other, so take an exclusive lock file first.
    lock = str(path) + ".lock"
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):   # Windows: a lock being deleted refuses create
            if time.time() > deadline:
                raise
            time.sleep(0.005)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    finally:
        os.close(fd)
        os.unlink(lock)


append_line(d / "calls.jsonl", json.dumps(argv))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))
live = d / "live"
live.mkdir(exist_ok=True)
me = live / uuid.uuid4().hex
me.write_text("x")
try:
    role = pathlib.Path(opt("--role-file")).stem
    append_line(d / "counts", "%s %d" % (role, len([x for x in live.iterdir() if not x.name.endswith(".lock")])))
    time.sleep(float(os.environ.get("FAKE_SWARM_SLEEP", "0.2")))
    prompt = pathlib.Path(opt("-f")).read_text(encoding="utf-8")
    beh = d / ("%s.py" % role)
    rc, text = 0, "```json\n[]\n```"
    if beh.exists():
        ns = {}
        exec(beh.read_text(encoding="utf-8"), ns)
        rc, text = ns["answer"](prompt, opt("--resume") is not None)
finally:
    me.unlink()
if rc == 0 or str(rc) in os.environ.get("FAKE_SWARM_RC_RECORD", "").split(","):
    print(json.dumps({"type": "result", "is_error": False, "result": "" if rc == 6 else text,
                      "session_id": "sess-" + uuid.uuid4().hex[:8],
                      "usage": {"input_tokens": 100, "output_tokens": 10}}))
else:
    sys.stderr.write("fake failure rc=%d\n" % rc)
sys.exit(rc)
