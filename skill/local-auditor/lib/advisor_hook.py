"""The PreToolUse hook of qwen-agent --plan: holds the session's first file write once.

Claude Code runs this before every Edit, Write, MultiEdit and NotebookEdit of a --plan
run (qwen-agent.sh writes the settings file that names it). The first time, it builds a
digest of what the session said and did so far (never its thinking, never file contents), asks a
stronger model with no tools, and denies THAT write with the advice as the reason the
local model reads. Every other case lets the write through, and every failure does too:
the hook never breaks or stalls a session.

  advisor_hook.py --state DIR --model M --task FILE --record FILE [--log FILE]
                  [--digest FILE] [--claude BIN] [--timeout SECS]   (hook JSON on stdin)

DIR is the run's private plan directory: advisor_mcp.ask keeps its call counter there
(`count`), and a counter of 1 means the one plan call is spent, so a second write, a
retry round or a failed call is never held again.
"""
import argparse
import json
import os
import re
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import advisor_mcp  # noqa: E402

DIGEST_MAX = 40000                 # characters of digest sent
TASK_MAX = 4000                    # characters of the task text sent
PLAN_LIMIT = DIGEST_MAX + TASK_MAX + 1000       # ask()'s context limit for this call (headers)
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WITHHELD = "<secret-looking path withheld>"
LAG_NOTE = "(the transcript may lag the held write by one turn: the last message may be missing)"
SAID_LAST, SAID_EARLIER = 4000, 1000
SAFEGUARD_SKIP = "refused by Claude's safeguards"

PLAN_Q = (
    "Below: the task and a digest of an agent's work up to its first file edit, which is "
    "held for your advice. The digest lists its visible messages in order, ending with its "
    "PLAN briefing, one line per tool call (what it opened, searched or ran, with the "
    "outcome; no file contents), and the edit it is about to make. Reply in at most 350 words as three numbered "
    "lists. 1 APPROACH: is the intended approach right; name any misreading of the task "
    "or of the code, finding 1 first. 2 CHECK NEXT: what it did not look at and should, "
    "as concrete tool uses (a file to read, a pattern to grep, a test to run). 3 RISKS: "
    "what most likely makes the checks fail, ending with 'RUN: <one check to do before "
    "finishing>'. If the trail is too thin to judge, say what it must look at first.")

PLAN_REASON = """This is not an error. A stronger model reviewed your plan before your first edit (held once).

ADVICE:
{advice}

DO THIS NOW.
1. Number each point above.
2. Put this block first in your reply, one line per point:
ADVICE RESPONSE:
1. DONE <what you will change or check>
2. REJECTED <path:line or output that contradicts it>
3. Do the "CHECK NEXT" tool uses before you edit.
4. Re-issue your edit, revised if the advice requires it. Later edits are not held.
5. Memory of the task or a library is not evidence for REJECTED."""

GATE_REASON = """Held once more (this is not an error): you re-issued the edit without acting on the advice.

DO THIS NOW.
{todo}
Then re-issue the edit. It will not be held again."""
GATE_TODO = {"respond": "- Write the ADVICE RESPONSE block: one line per advice point, DONE or REJECTED with evidence.",
             "check": "- Run at least one CHECK NEXT tool use from the advice (a Read, a Grep or a qwen-test run)."}


# ------------------------------------------------------------------ transcript reading

def _rows(path):
    """Parsed JSONL objects of a transcript; corrupt or truncated lines are skipped."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _blocks(row):
    """The content blocks of a transcript row (a plain string becomes one text block)."""
    msg = row.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _wait_for(transcript, tool_use_id, seconds):
    """True once the transcript holds an assistant tool_use block with this id.

    Claude Code may run the hook before it has written the turn that holds the held call
    (the transcript "may lag the in-memory conversation"), so the hook polls for its own
    call. With no id there is nothing to wait for."""
    if not tool_use_id:
        return True
    deadline = time.time() + max(0.0, seconds)
    while True:
        try:
            for row in _rows(transcript):
                if any(b.get("type") == "tool_use" and b.get("id") == tool_use_id
                       for b in _blocks(row)):
                    return True
        except OSError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.1)


# ------------------------------------------------------------------ one line per tool call

def _flat(s, n):
    """One line, whitespace runs collapsed, capped at n characters."""
    return " ".join(str(s).split())[:n]


def _first_line(text, n, skip_exit=False):
    for ln in str(text).splitlines():
        if skip_exit and re.match(r"^Exit code \d+\s*$", ln.strip()):
            continue
        if ln.strip():
            return ln.strip()[:n]
    return ""


def _result_text(block):
    """The text of a tool_result block: a string, or a list of text blocks."""
    if not isinstance(block, dict):
        return ""
    c = block.get("content")
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _rel(path, cwd):
    """The path relative to cwd with forward slashes (absolute if it is outside cwd)."""
    p = str(path or "")
    if not p:
        return ""
    if os.path.isabs(p) or re.match(r"^[A-Za-z]:[\\/]", p):
        try:
            r = os.path.relpath(p, cwd or os.getcwd())
        except ValueError:                  # another drive on Windows
            r = p
        if not r.startswith(".."):
            p = r
    return p.replace("\\", "/")


def _shown(path, cwd):
    """(text, secret): the path as the digest shows it, or the withheld marker."""
    rel = _rel(path, cwd)
    if advisor_mcp.looks_secret(rel) or advisor_mcp.looks_secret(str(path or "")):
        return WITHHELD, True
    return rel, False


def _held_path(tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    return str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")


def _count_lines(text):
    return len([ln for ln in str(text).splitlines() if ln.strip()])


def _action_line(tool_use, tool_result, cwd):
    """`Read src/x.py -> 40 lines`: what one tool call opened, searched or ran, and the
    outcome. File contents never appear; a secret-looking path is withheld with no outcome."""
    name = str(tool_use.get("name") or "?")
    ti = tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {}
    text = _result_text(tool_result)
    err = bool(isinstance(tool_result, dict) and tool_result.get("is_error"))
    if tool_result is None:
        out = "(no result)"
    elif err:
        out = "ERROR: " + _first_line(text, 100, skip_exit=True)
    else:
        out = None
    if name == "Read":
        shown, secret = _shown(ti.get("file_path"), cwd)
        if secret:
            return "Read " + shown
        head = "Read " + shown
        off, lim = ti.get("offset"), ti.get("limit")
        if isinstance(off, int) and isinstance(lim, int):
            head += " lines %d-%d" % (off, off + lim - 1)
        elif isinstance(off, int):
            head += " from line %d" % off
        elif isinstance(lim, int):
            head += " lines 1-%d" % lim
        return "%s -> %s" % (head, out if out is not None else "%d lines" % len(text.splitlines()))
    if name == "Glob":
        head = 'Glob "%s"' % _flat(ti.get("pattern", ""), 60)
        if ti.get("path"):
            shown, secret = _shown(ti.get("path"), cwd)
            if secret:
                return "Glob " + shown
            head += " in " + shown
        if out is None:
            out = "0 files" if text.strip().startswith("No files found") or not text.strip() \
                else "%d files" % _count_lines(text)
        return "%s -> %s" % (head, out)
    if name == "Grep":
        head = 'Grep "%s"' % _flat(ti.get("pattern", ""), 60)
        if ti.get("path"):
            shown, secret = _shown(ti.get("path"), cwd)
            if secret:
                return "Grep " + shown
            head += " in " + shown
        if out is None:
            body = [ln for ln in text.splitlines() if ln.strip()
                    and not re.match(r"^(Found \d+ |No matches found|No files found)", ln.strip())]
            unit = "files" if ti.get("output_mode", "files_with_matches") == "files_with_matches" \
                else "hits"
            out = "%d %s" % (len(body), unit)
        return "%s -> %s" % (head, out)
    if name == "Bash":
        cmd = str(ti.get("command", ""))
        if any(advisor_mcp.looks_secret(tok) for tok in re.split(r"[\s\"'|;&<>()=]+", cmd)):
            return "Bash " + WITHHELD
        m = re.match(r"^\s*qwen-test\b\s*(.*)$", cmd, re.S)
        if m and not re.search(r"[;&|<>`\n]|\$\(", cmd):     # a chained command is not a test run
            head = "qwen-test " + _flat(m.group(1), 60)
            if out is None:
                out = "PASS"
            elif out.startswith("ERROR: "):
                out = "FAIL: " + _first_line(text, 120, skip_exit=True)
            return "%s -> %s" % (head.rstrip(), out)
        head = 'Bash "%s"' % _flat(cmd, 60)
        if out is not None and err and re.search(
                r"denied|not allowed|requires approval|permission", text[:300], re.I):
            out = "DENIED"
        elif out is None or out.startswith("ERROR: "):
            # command output is often file content (cat, grep, diff), so only its size is shown
            n = _count_lines(text)
            out = ("ERROR: " if err else "") + ("%d lines" % n if n else "(no output)")
        return "%s -> %s" % (head, out)
    # any other tool: Edit/Write (only seen when a later digest is built), Task, WebFetch...
    path = _held_path(ti)
    if path:
        shown, secret = _shown(path, cwd)
        if secret:
            return "%s %s" % (name, shown)
        what = shown
    else:
        what = _flat(ti.get("description") or next((v for v in ti.values()
                                                   if isinstance(v, str)), ""), 60)
    if out is None:
        out = _first_line(text, 100) or "(no output)"
    return "%s %s -> %s" % (name, what, out) if what else "%s -> %s" % (name, out)


# ------------------------------------------------------------------ the digest

def _cut_to(text, n):
    if len(text) <= n:
        return text
    return "%s [... %d characters cut]" % (text[:n].rstrip(), len(text) - n)


def _entries(rows, cwd, held):
    """[[kind, text, full text]] in conversation order: prompt, said, action. Stops at the
    held call. Subagent (sidechain) rows and thinking blocks are skipped: the session's
    reasoning is never sent; its visible PLAN briefing stands in for it. tool_result rows only complete the
    action lines of their tool_use."""
    rows = [r for r in rows if not r.get("isSidechain")]
    results, stop_id = {}, held.get("tool_use_id")
    for r in rows:
        for b in _blocks(r):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                results[b["tool_use_id"]] = b
    out, prompts = [], 0
    for r in rows:
        kind = r.get("type")
        for b in _blocks(r):
            t = b.get("type")
            if kind == "user" and t == "text" and not r.get("isMeta"):
                txt = str(b.get("text", "")).strip()
                if txt and not txt.startswith("<"):         # not a <system-reminder> or hook echo
                    line = ("[prompt: the task]" if prompts == 0
                            else "[prompt: a later message from the harness]")
                    out.append(["prompt", line, line])
                    prompts += 1
            elif kind == "assistant" and t == "text":
                if str(b.get("text", "")).strip():
                    out.append(["said", str(b["text"]).strip(), str(b["text"]).strip()])
            elif kind == "assistant" and t == "tool_use":
                if stop_id and b.get("id") == stop_id:
                    return out
                line = _action_line(b, results.get(b.get("id")), cwd)
                out.append(["action", line, line])
    return out


def _render(entries, tail):
    lines, run = [], 0
    for kind, text, _full in entries:
        line = ("SAID: " if kind == "said" else "") + text
        if kind == "action" and lines and lines[-1] == line:
            run += 1
            continue
        if run:
            lines[-1] += " (x%d)" % (run + 1)
            run = 0
        lines.append(line)
    if run:
        lines[-1] += " (x%d)" % (run + 1)
    return "\n".join(lines + tail)


def build_digest(rows, cwd, held, limit=DIGEST_MAX):
    """The digest the plan call sees: SAID / one action line per tool call, in order, ending
    with `about to <tool> <path>`; thinking is never included. `held` is the hook input:
    tool_name, tool_input, tool_use_id, and may_lag (True when the transcript was still short
    of the held call). Over `limit`, in this order and only as far as needed: every SAID
    shrinks (the last, normally the PLAN briefing, keeps 4000, earlier ones 1000); action
    lines are never cut. Last resort: the oldest lines are dropped."""
    entries = _entries(rows, cwd, held)
    shown, _ = _shown(_held_path(held.get("tool_input")), cwd)
    tail = ([LAG_NOTE] if held.get("may_lag") else []) + \
           ["about to %s %s" % (held.get("tool_name") or "write", shown)]

    def size():
        return len(_render(entries, tail))

    if size() <= limit:
        return _render(entries, tail)

    said = [e for e in entries if e[0] == "said"]
    for e in said:
        e[1] = _cut_to(e[2], SAID_LAST if e is said[-1] else SAID_EARLIER)
    dropped, marker = 0, ["action", "", ""]
    while size() > limit:
        idx = next((k for k, e in enumerate(entries) if e is not marker and e[0] != "action"),
                   next((k for k, e in enumerate(entries) if e is not marker), None))
        if idx is None:
            break
        del entries[idx]
        dropped += 1
        marker[1] = marker[2] = "[... %d earlier lines omitted]" % dropped
        if not any(e is marker for e in entries):
            entries.insert(0, marker)
    return _render(entries, tail)


def digest_was_cut(digest):
    return "characters cut]" in digest or "earlier lines omitted]" in digest


# ------------------------------------------------------------------ the hook

def _append(path, rec):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass                                    # the record is a convenience, never a failure


def _spend(state):
    """Mark the one plan call spent (ask() does when it gets as far as launching claude)."""
    if advisor_mcp._count(state) < 1:
        try:
            with open(os.path.join(state, "count"), "w", encoding="utf-8") as fh:
                fh.write("1")
        except OSError:
            pass


def _last_cost(state):
    try:
        with open(os.path.join(state, "calls.jsonl"), encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        return json.loads(lines[-1]).get("cost_usd") if lines else None
    except (OSError, ValueError):
        return None


def _read_task(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
    except (OSError, TypeError):
        return "(the task text was not available)"
    return _cut_to(text, TASK_MAX)


def main(argv=None, stdin=None, stdout=None):
    for s, kw in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                  (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"})):
        if hasattr(s, "reconfigure"):
            s.reconfigure(**kw)
    ap = argparse.ArgumentParser(prog="advisor_hook.py")
    ap.add_argument("--state", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--task", default="")
    ap.add_argument("--record", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--digest", default="")
    ap.add_argument("--claude", default="")
    ap.add_argument("--timeout", type=int, default=600)
    try:
        a = ap.parse_args(argv)
    except SystemExit:                          # a bad command line exits 2, which would block the write
        return 0
    try:
        return _decide(a, sys.stdin if stdin is None else stdin,
                       sys.stdout if stdout is None else stdout)
    except Exception:                           # any other error: the write goes through, the call is spent
        _spend(a.state)
        return 0


def _decide(a, stdin, stdout):
    try:
        data = json.loads(stdin.read())
    except ValueError:
        return 0
    if not isinstance(data, dict) or data.get("agent_id"):
        return 0                                # a subagent's write: never held, never spends
    tool = data.get("tool_name")
    if tool not in WRITE_TOOLS:
        return 0
    if advisor_mcp._count(a.state) >= 1:        # the one call is spent: at most one gate hold
        return _gate(a, data, stdout)
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    cwd = data.get("cwd") or os.getcwd()
    shown, _ = _shown(_held_path(tool_input), cwd)
    rec = {"trigger": "plan", "round": advisor_mcp._int(os.environ.get("QWEN_PLAN_ROUND"), 1),
           "called": False, "skipped_reason": None, "cost_usd": None, "tool": tool,
           "path": shown, "digest_chars": 0, "cut": False, "advice": None}
    transcript = data.get("transcript_path") or ""
    if not transcript or not os.path.isfile(transcript):
        rec["skipped_reason"] = "no transcript"
        _spend(a.state)
        _append(a.record, rec)
        return 0
    try:
        lag = float(os.environ.get("QWEN_PLAN_LAG_WAIT") or 3)
    except ValueError:
        lag = 3.0
    found = _wait_for(transcript, data.get("tool_use_id"), lag)
    try:
        rows = list(_rows(transcript))
    except OSError:
        rows = []
    if not rows:
        rec["skipped_reason"] = "no transcript"
        _spend(a.state)
        _append(a.record, rec)
        return 0
    held = {"tool_name": tool, "tool_input": tool_input,
            "tool_use_id": data.get("tool_use_id"), "may_lag": not found}
    digest = build_digest(rows, cwd, held, DIGEST_MAX)
    digest = digest.encode("utf-8", "replace").decode("utf-8")   # a lone surrogate is not UTF-8: replace it, as stdio does
    rec.update(digest_chars=len(digest), cut=digest_was_cut(digest), may_lag=not found)
    if a.digest:
        try:
            with open(a.digest, "w", encoding="utf-8") as fh:
                fh.write(digest + "\n")
        except OSError:
            pass
    context = "DIGEST OF THE AGENT'S WORK SO FAR:\n%s\n\nTHE TASK:\n%s" % (digest, _read_task(a.task))
    env = dict(os.environ)
    env.update({"QA_ADVISOR_MODEL": a.model, "QA_ADVISOR_STATE": a.state,
                "QA_ADVISOR_MAX_CALLS": "1", "QA_ADVISOR_TIMEOUT": str(a.timeout),
                "QA_ADVISOR_LOG": a.log})
    if a.claude:
        env["QA_ADVISOR_CLAUDE"] = a.claude
    try:
        text = advisor_mcp.ask(PLAN_Q, context, None, env, cwd, limit=PLAN_LIMIT)
    except Exception as e:                      # Refused, Unavailable, anything: allow the write
        _spend(a.state)                         # a failed call spends the budget: never block twice
        rec["skipped_reason"] = (SAFEGUARD_SKIP if "safeguards flagged" in str(e)
                                 else "%s: %s" % (type(e).__name__, e))
        rec["cost_usd"] = _last_cost(a.state)
        _append(a.record, rec)
        return 0
    advice = text.split("\n", 1)[1] if text.startswith("ADVICE (") and "\n" in text else text
    rec.update(called=True, advice=advice, cost_usd=_last_cost(a.state))
    _append(a.record, rec)
    _gate_save(a.state, {"advised": data.get("tool_use_id") or "", "held": 0, "released": False})
    stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": PLAN_REASON.format(advice=advice)},
        "systemMessage": "plan: %s advised before the first write" % a.model}) + "\n")
    stdout.flush()
    return 0



def _gate_load(state):
    try:
        with open(os.path.join(state, "gate.json"), encoding="utf-8") as fh:
            g = json.load(fh)
        return g if isinstance(g, dict) else None
    except (OSError, ValueError):
        return None


def _gate_save(state, g):
    try:
        with open(os.path.join(state, "gate.json"), "w", encoding="utf-8") as fh:
            json.dump(g, fh)
    except OSError:
        pass


def _after_advice(rows, advised_id):
    """(responded, checked) from the main session's rows after the advice was delivered:
    an ADVICE RESPONSE block in visible text, and at least one tool use that is not a write."""
    responded = checked = seen = False
    for r in rows:
        if r.get("isSidechain"):
            continue
        for b in _blocks(r):
            t = b.get("type")
            if not seen:
                seen = t == "tool_result" and b.get("tool_use_id") == advised_id
                continue
            if r.get("type") == "assistant" and t == "text" and "ADVICE RESPONSE" in str(b.get("text", "")):
                responded = True
            elif r.get("type") == "assistant" and t == "tool_use" and b.get("name") not in WRITE_TOOLS:
                checked = True
    return responded, checked


def _gate(a, data, stdout):
    """After the plan advice: hold the re-issued write once more if the session neither
    answered the advice nor ran a single check. No model call; at most one hold; any
    doubt (no gate state, no transcript, advice not found) lets the write through."""
    g = _gate_load(a.state)
    if not g or g.get("released") or not g.get("advised"):
        return 0
    transcript = data.get("transcript_path") or ""
    try:
        rows = list(_rows(transcript)) if os.path.isfile(transcript) else []
    except OSError:
        rows = []
    responded, checked = _after_advice(rows, g["advised"])
    missing = [k for k, ok in (("respond", responded), ("check", checked)) if not ok]
    rec = {"trigger": "plan-gate", "responded": responded, "checked": checked,
           "held": bool(missing) and not g.get("held") and bool(rows)}
    if not rec["held"]:
        g["released"] = True
        _gate_save(a.state, g)
        _append(a.record, rec)
        return 0
    g["held"] = 1
    _gate_save(a.state, g)
    _append(a.record, rec)
    stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": GATE_REASON.format(
                                   todo="\n".join(GATE_TODO[k] for k in missing))},
        "systemMessage": "plan: edit held once more until the advice is acted on"}) + "\n")
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
