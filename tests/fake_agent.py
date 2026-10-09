"""A scripted stand-in for qwen-agent, for supervisor tests.

$FAKE_AGENT_SCRIPT is a JSON list; call N uses entry N-1 (the last entry repeats):
  {"rc": 0, "result": "...", "session_id": "s1", "tokens": 100, "stderr": "...",
   "write": {"relative/path": "content", ...}, "denied": ["Bash"], "sleep": 30,
   "tools": ["Read", "Task", ...], "advisor": [{...}, ...]}
With "tools", and QWEN_TRANSCRIPT_DIR set, one assistant tool_use line per listed
tool is appended to $QWEN_TRANSCRIPT_DIR/<session_id>.jsonl (the file accumulates,
like a resumed Claude Code session's transcript).
With "advisor", one JSON line per entry is appended to the call's --advisor-state
directory's calls.jsonl -- what lib/advisor_mcp.py writes for every ask call it
serves, so a test can hand the supervisor the call log of a run that asked.
With "advisor_lock" (true/false), one JSON line {"found": .., "left": ..} is appended
to $FAKE_AGENT_RECORD.locks: "found" is whether that state directory held the
advisor's lock when the call started, and true also leaves one behind (what a round
killed mid-ask does), so a test sees the lock clear between rounds rather than never
having been there.
Every call's argv is appended to $FAKE_AGENT_RECORD as one JSON line.
"""
import json
import os
import sys
import time

argv = sys.argv[1:]
with open(os.environ["FAKE_AGENT_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
script = json.load(open(os.environ["FAKE_AGENT_SCRIPT"], encoding="utf-8"))
counter = os.environ["FAKE_AGENT_RECORD"] + ".n"
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
step = script[min(n, len(script) - 1)]
if step.get("sleep"):          # holds the round open so a test can interrupt it
    time.sleep(step["sleep"])
repo = argv[argv.index("-C") + 1]
for rel, content in (step.get("write") or {}).items():
    with open(os.path.join(repo, rel), "w", encoding="utf-8") as fh:
        fh.write(content)
if step.get("advisor"):
    # A round that asks lands its records in the ONE state dir the supervisor gave it.
    # No --advisor-state on this call means the script asked for calls the round could
    # not have made; index() raising ValueError is the loud answer to that.
    with open(os.path.join(argv[argv.index("--advisor-state") + 1], "calls.jsonl"),
              "a", encoding="utf-8") as fh:
        for rec in step["advisor"]:
            fh.write(json.dumps(rec) + "\n")
if "advisor_lock" in step:
    # The advisor's lock in the ONE state dir this call was given: what the round found
    # when it started, and what it leaves behind when the script says it was killed
    # mid-ask. Both halves land in one line so a test can tell "the supervisor cleared
    # the stale lock" from "no lock was ever made" -- and no --advisor-state here is a
    # loud ValueError, as above.
    lk = os.path.join(argv[argv.index("--advisor-state") + 1], "lock")
    found = os.path.isdir(lk)
    if step["advisor_lock"]:
        os.mkdir(lk)
    with open(os.environ["FAKE_AGENT_RECORD"] + ".locks", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"found": found, "left": os.path.isdir(lk)}) + "\n")
tdir = os.environ.get("QWEN_TRANSCRIPT_DIR")
if tdir and step.get("tools"):
    os.makedirs(tdir, exist_ok=True)
    sid = step.get("session_id", "s1")
    with open(os.path.join(tdir, "%s.jsonl" % sid), "a", encoding="utf-8") as fh:
        for k, tool in enumerate(step["tools"]):
            fh.write(json.dumps({"type": "assistant",
                                 "message": {"role": "assistant", "content": [
                                     {"type": "tool_use", "name": tool,
                                      "id": "toolu_%d_%d" % (n, k), "input": {}}]}},
                                ) + "\n")
rc = step.get("rc", 0)
if step.get("stderr"):
    sys.stderr.write(step["stderr"])
if rc in (0, 5, 6, 7):
    # 6 = ran clean but ended without text (the JSON record still carries the session).
    # 5 = round timeout: the round produced no final result, but the session it
    # reached is still resumable, so the JSON record carries it.
    t = step.get("tokens", 100)
    print(json.dumps({"type": "result", "is_error": False, "result": step.get("result", "" if rc == 6 else "ok"),
                      "session_id": step.get("session_id", "s1"),
                      "usage": {"input_tokens": t, "output_tokens": 0},
                      "permission_denials": [{"tool_name": n, "tool_input": {}} for n in step.get("denied", [])]}, indent=2))
sys.exit(rc)
