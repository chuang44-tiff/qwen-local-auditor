"""A scripted stand-in for qwen-agent, for supervisor tests.

$FAKE_AGENT_SCRIPT is a JSON list; call N uses entry N-1 (the last entry repeats):
  {"rc": 0, "result": "...", "session_id": "s1", "tokens": 100, "stderr": "...",
   "write": {"relative/path": "content", ...}, "denied": ["Bash"], "sleep": 30,
   "tools": ["Read", "Task", ...], "advisor": [{...}, ...]}
With "tools", and QWEN_TRANSCRIPT_DIR set, one assistant tool_use line per listed
tool is appended to $QWEN_TRANSCRIPT_DIR/<session_id>.jsonl (the file accumulates,
like a resumed Claude Code session's transcript).
With "run_write", the same {rel/path: content} mapping is written under
dirname($QWEN_INBOX) -- the loop's RUN folder, NOT the round's repo -- creating
parent dirs (a test plants asks/lease.json there). Needs a messaged round
(QWEN_INBOX set); without one it is a loud KeyError.
With "export_run" (true), the RUN path (dirname($QWEN_INBOX)) is appended to
$FAKE_AGENT_RECORD.run as one JSON line, so a test learns where the run folder is
without reading the watch pointer.
With "advisor", one JSON line per entry is appended to the call's --advisor-state
directory's calls.jsonl -- what lib/advisor_mcp.py writes for every ask call it
serves, so a test can hand the supervisor the call log of a run that asked.
With "advisor_count" (an int), that number is written to the --advisor-state directory's
`count` file: the agent has spent that many asks.
With "plant_question" ({"question": str, "options": [str, ...]}), what the ask_main
tool of lib/msg_mcp.py writes when the model asks: the queued record
$QWEN_MSG_DIR/out/new/<id>.json (body {"id","t","kind":"question","question",
"options"}, the id from notes.new_note_id()) and the $QWEN_MSG_DIR/open-question.json
stamp naming it ({"q_id","main_seq":0}); the id is also appended as one JSON line to
$FAKE_AGENT_RECORD.qids. It runs BEFORE "sleep", so a round can ask and then wait.
Needs a messaged round of a loop with a msg dir (QWEN_MSG_DIR set); without one it is
a loud KeyError.
With "run_write_then_sleep" (true), the "run_write"/"export_run" work is done BEFORE
the "sleep" instead of after it -- a round that plants a lease (or anything else
under RUN) and then waits leaves it for the loop to find while it sleeps.
With "record_env": [names], one JSON line {name: os.environ.get(name)} is appended
to $FAKE_AGENT_RECORD.env -- what the loop handed this round's environment (the argv
record file itself is not touched, so calls() stays a pure argv log).
With "advisor_lock" (true/false), one JSON line {"found": .., "left": ..} is appended
to $FAKE_AGENT_RECORD.locks: "found" is whether that state directory held the
advisor's lock when the call started, and true also leaves one behind (what a round
killed mid-ask does), so a test sees the lock clear between rounds rather than never
having been there.
With "plan_record" (a dict), that dict is appended to the call's --plan-state directory's
plan.jsonl as one JSON line -- what lib/advisor_hook.py writes when the hook fires -- so a
loop test covers the supervisor's side without running claude. With "plan_count" (an int),
that number is written to the directory's `count` file (what advisor_mcp.ask writes before it
launches claude: a hook killed mid-call leaves a count and no record). A call that has either
key but no --plan-state is a loud ValueError. With "plan_files" ({name: text}), those files
are written into the directory too (the hook's plan.md and plan-digest.md logs).
Every call that has --plan-state also appends {"round", "state", "task", "task_text"} to
$FAKE_AGENT_RECORD.plan: the QWEN_PLAN_ROUND it was given, the directory, whether the
supervisor had put task.md in it by then, and that copy's text (None when there is none).
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


def _do_run_write():
    """The "run_write"/"export_run" work, factored so the sleep can be ordered
    either side of it ("run_write_then_sleep")."""
    # RUN is dirname($QWEN_INBOX): the loop's folder, which only a messaged round
    # is given an inbox inside of. No QWEN_INBOX is a loud KeyError, on purpose.
    rundir = os.path.dirname(os.environ["QWEN_INBOX"])
    for rel, content in (step.get("run_write") or {}).items():
        path = os.path.join(rundir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    if step.get("export_run"):
        with open(os.environ["FAKE_AGENT_RECORD"] + ".run", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(rundir) + "\n")


if step.get("record_env"):
    # What the loop handed this round's environment (the qla_msg vars, spec 12.7):
    # one JSON line per call to a SIDE file -- the argv record stays a pure argv log.
    with open(os.environ["FAKE_AGENT_RECORD"] + ".env", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({name: os.environ.get(name)
                             for name in step["record_env"]}) + "\n")


def _plant_question():
    """The "plant_question" work: what the ask_main tool of lib/msg_mcp.py writes
    when the model asks (spec 11.3) -- the queued question record and the
    open-question stamp naming it. No QWEN_MSG_DIR is a loud KeyError, on purpose,
    like run_write without QWEN_INBOX."""
    _lib_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "skill", "local-auditor")
    if _lib_root not in sys.path:
        sys.path.insert(0, _lib_root)
    from lib import notes                    # noqa: E402  (the path insert above)
    _spec = step["plant_question"]
    q_id = notes.new_note_id()
    _body = {"id": q_id, "t": round(time.time(), 3), "kind": "question",
             "question": _spec.get("question", ""),
             "options": _spec.get("options", [])}
    _mdir = os.environ["QWEN_MSG_DIR"]
    _new = os.path.join(_mdir, "out", "new")
    os.makedirs(_new, exist_ok=True)
    with open(os.path.join(_new, "%s.json" % q_id), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_body) + "\n")
    with open(os.path.join(_mdir, "open-question.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"q_id": q_id, "main_seq": 0}) + "\n")
    with open(os.environ["FAKE_AGENT_RECORD"] + ".qids", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(q_id) + "\n")


if step.get("run_write_then_sleep"):
    # The RUN files land BEFORE anything else of this round (a planted lease must
    # be live for a relay pass during the sleep, and before the question so a pass
    # can never see a question with no lease and drop it as no_listener); then the
    # question, then the wait. Without the key the order is the old one.
    if step.get("run_write") or step.get("export_run"):
        _do_run_write()
if step.get("plant_question"):                # before the sleep: ask, then wait
    _plant_question()
if step.get("run_write_then_sleep"):
    if step.get("sleep"):
        time.sleep(step["sleep"])
elif step.get("sleep"):          # holds the round open so a test can interrupt it
    time.sleep(step["sleep"])
repo = argv[argv.index("-C") + 1]
for rel, content in (step.get("write") or {}).items():
    with open(os.path.join(repo, rel), "w", encoding="utf-8") as fh:
        fh.write(content)
if (step.get("run_write") or step.get("export_run")) and not step.get("run_write_then_sleep"):
    _do_run_write()
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
if step.get("advisor_count") is not None:
    with open(os.path.join(argv[argv.index("--advisor-state") + 1], "count"), "w",
              encoding="utf-8") as fh:
        fh.write(str(step["advisor_count"]))
if "--plan-state" in argv:
    pdir = argv[argv.index("--plan-state") + 1]
    tpath = os.path.join(pdir, "task.md")
    has_task = os.path.isfile(tpath)
    task_text = None
    if has_task:
        with open(tpath, encoding="utf-8") as fh:
            task_text = fh.read()
    with open(os.environ["FAKE_AGENT_RECORD"] + ".plan", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"round": os.environ.get("QWEN_PLAN_ROUND"), "state": pdir,
                             "task": has_task, "task_text": task_text}) + "\n")
if step.get("plan_record") is not None or step.get("plan_count") or step.get("plan_files"):
    pdir = argv[argv.index("--plan-state") + 1]
    if step.get("plan_record") is not None:
        with open(os.path.join(pdir, "plan.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(step["plan_record"]) + "\n")
    if step.get("plan_count"):
        with open(os.path.join(pdir, "count"), "w", encoding="utf-8") as fh:
            fh.write(str(step["plan_count"]))
    for name, text in (step.get("plan_files") or {}).items():
        with open(os.path.join(pdir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
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
