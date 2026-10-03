"""qwen-agent --until-done: keep a local coding session going until the HARNESS says done.

Done is decided here, deterministically: every checklist check is run by this
process after every round. The model cannot tick an item, edit the checklist
(it only ever sees a copy that lives outside the repo), or end the run by
claiming success. A round that leaves work undone resumes the SAME Claude Code
session with a precise "not done" prompt, so the server's prefix cache stays warm.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import checks, decisions, taskfile, testrun  # noqa: E402
from lib.builders import history  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_APIERR, EXIT_HARNESS = 0, 2, 4, 8
EXIT_PARTIAL, EXIT_NOPROGRESS, EXIT_DIRTY, EXIT_LOCKED, EXIT_INTERRUPTED = 11, 12, 13, 14, 130
AGENT_USAGE, AGENT_PREFLIGHT, AGENT_APIERR, AGENT_TIMEOUT, AGENT_EMPTY, AGENT_HARNESS = 2, 3, 4, 5, 6, 8

FIRST = """You are working on the task below until every checklist item's check passes.

The harness, not you, decides when you are done. After you stop it runs every
check itself; if anything still fails it resumes this session and tells you what.
- Run tests with `qwen-test [SELECTOR]`; it is your only shell command.
- You cannot tick or edit checklist items. If you believe an item is wrong, say so
  in a DEVIATION block and leave it.
- Record every deliberate departure from the spec as a DEVIATION block (format in
  your role instructions). An unrecorded departure keeps the task open.

# The task

{task}

# Decisions recorded so far

{log}
"""

NOT_DONE = """Not done. The harness ran every check after your last reply:

{fails}
{unlogged}
Fix these, or record a DEVIATION block for anything you changed on purpose. Continue."""


def state_root():
    explicit = os.environ.get("QWEN_AGENT_STATE")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(xdg, "qwen-agent", "runs")


def repo_state_dir(repo):
    # realpath, not abspath: a checkout reached through a symlink must share the
    # state dir (and so the lock) of the same checkout reached directly.
    return os.path.join(state_root(), hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:8])


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          encoding="utf-8", errors="replace").stdout


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _tree_sig(repo):
    """A hash of the working tree's state: tracked changes plus untracked file contents."""
    h = hashlib.sha1(_git(repo, "diff", "HEAD").encode("utf-8", "replace"))
    for rel in _new_files(repo):
        h.update(rel.encode())
        try:
            with open(os.path.join(repo, rel), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()


def _backoff():
    raw = os.environ.get("QWEN_SUPERVISOR_BACKOFF", "30,60,120")
    vals = [int(x) for x in raw.split(",") if x.strip()]
    if any(v < 0 for v in vals):
        raise ValueError("negative backoff")
    return vals


def call_agent(agent, repo, prompt_path, session, passthrough, role="coder", test=True):
    argv = list(agent) + ["--json", "--warn-denials", "-q", "-r", role]
    if test:
        argv.append("--test")
    argv += ["-C", repo, "-f", prompt_path]
    if session:
        argv += ["--resume", session]
    argv += list(passthrough)
    for delay in [0] + _backoff():
        time.sleep(delay)
        rc, out, err = _run_agent(argv)
        # 3 (preflight failed -- the server went away mid-run) is the same failure
        # as 4 (API error) wearing a different exit code: back off and ask again.
        if rc not in (AGENT_PREFLIGHT, AGENT_APIERR):
            break
    try:
        rec = json.loads(out) if out.strip() else {}
    except ValueError:
        rec = {}
    u = rec.get("usage") or {}
    return {"rc": rc, "session": rec.get("session_id") or "",
            "result": rec.get("result") if isinstance(rec.get("result"), str) else "",
            "tokens": int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0),
            "denied": [d.get("tool_name") or "?" for d in (rec.get("permission_denials") or [])
                       if isinstance(d, dict)],
            "stderr": err}


def _run_agent(argv):
    """(rc, stdout, stderr) of one agent call. Ctrl-C lets the agent clean up first.

    subprocess.run would SIGKILL the agent on KeyboardInterrupt, before its own
    signal handler could remove the qwen-test worktree. Instead the agent is
    asked to stop (SIGTERM; on POSIX it runs in its own session, so this is the
    only signal it gets) and given STOP_GRACE seconds to clean up before a kill.
    """
    posix = os.name == "posix"
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                         **({"start_new_session": True} if posix else {}))
    try:
        out, err = p.communicate()
    except KeyboardInterrupt:
        _stop_agent(p, posix)
        raise
    return p.returncode, out or "", err or ""


STOP_GRACE = 20


def _stop_agent(p, posix):
    if p.poll() is None and posix:
        with contextlib.suppress(OSError):
            p.send_signal(signal.SIGTERM)
    # On Windows the console's Ctrl-C already reached the agent: just wait.
    deadline = time.time() + STOP_GRACE
    while True:
        try:
            # communicate, not wait: it drains the pipes, so a chatty agent
            # cannot block on a full pipe while it cleans up.
            p.communicate(timeout=max(0.0, deadline - time.time()))
            return
        except KeyboardInterrupt:
            continue        # a second Ctrl-C does not cut the cleanup short
        except subprocess.TimeoutExpired:
            p.kill()
            with contextlib.suppress(Exception):
                p.communicate(timeout=5)
            return


AUDIT = """Compare the SPEC with the DIFF and list every place the diff does something the
spec says should be different. This is extraction: quote the spec, cite the line.
For each one, say which decision-log entry (D-number) records it, or NONE.

Reply with one block per contradiction, exactly:

## CONTRADICTION
SPEC: <quoted spec text>
CODE: <path:line in the new code>
LOGGED: <D-number, or NONE>

or, if there are none, the single line: NO CONTRADICTIONS

# SPEC

{task}

# DECISION LOG

{log}

# DIFF (against the starting commit)

```diff
{diff}
```
"""
_CONTRA = re.compile(r"^##\s*CONTRADICTION\s*:?\s*$", re.M | re.I)
_AFIELD = re.compile(r"^[ \t>*_-]*\**(SPEC|CODE|LOGGED)\**:[ \t]*\**[ \t]*(.*?)[ \t\r]*$", re.M | re.I)
MAX_AUDIT_DIFF = 120_000


def _bare(line):
    """A line without markdown emphasis, surrounding space or trailing . ! : -- upper-cased."""
    return re.sub(r"[*_`]", "", line).strip().rstrip(".!:").strip().upper()


def parse_audit(text, n_logged):
    """Unlogged contradiction locations from an audit reply; None if it is unparseable."""
    text = (text or "").replace("```", "")
    heads = list(_CONTRA.finditer(text))
    if not heads:
        return [] if any(_bare(ln) == "NO CONTRADICTIONS" for ln in text.splitlines()) else None
    out = []
    for i, m in enumerate(heads):
        seg = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        f = {k.lower(): v for k, v in _AFIELD.findall(seg)}
        logged = re.fullmatch(r"D(\d+)", f.get("logged", "").strip(), re.I)
        if not logged or not 1 <= int(logged.group(1)) <= n_logged:
            out.append(f.get("code") or "(unlocated)")
    return out


# Options that would let the read-only audit change files or widen its tools.
_MUTATING_FLAGS = {"--write": 0, "--all-tools": 0, "--unrestricted": 0,
                   "-t": 1, "--tools": 1, "--toolset": 1, "--permission-mode": 1}


def _read_only_passthrough(passthrough):
    """The coder's passthrough minus every mutation flag (and its value)."""
    out, skip = [], 0
    for a in passthrough:
        if skip:
            skip -= 1
            continue
        if a in _MUTATING_FLAGS:
            skip = _MUTATING_FLAGS[a]
            continue
        if a.startswith("--") and a.split("=", 1)[0] in _MUTATING_FLAGS and "=" in a:
            continue
        out.append(a)
    return out


def deviation_audit(agent, repo, run_dir, task_text, log, start, passthrough):
    """Unlogged spec contradictions in the diff: ([CODE locations] or None if the reply
    was unusable twice, tokens spent, saved reply files, rc of the last call, its stderr).
    The audit's agent call is a call like any other: rc 2/3/4 end it at once -- the run
    stops with exit 2 (usage) or 4 (server), as a coder round would, and the one retry is only for unparseable
    replies (a timed-out reply counts as unusable and still gets it)."""
    diff = _git(repo, "diff", start)
    for rel in _new_files(repo):
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
                diff += "\n+++ new file %s\n%s" % (rel, fh.read())
        except OSError:
            continue
    if len(diff) > MAX_AUDIT_DIFF:
        diff = diff[:MAX_AUDIT_DIFF] + "\n[diff truncated]"
    prompt = AUDIT.format(task=task_text, log=decisions.render(log), diff=diff)
    tokens, replies = 0, []
    rc, err = 0, ""
    for _attempt in range(2):          # an unparseable reply gets one fresh retry
        stamp = int(time.time() * 1000)
        while os.path.exists(os.path.join(run_dir, "audit-%d.prompt.md" % stamp)):
            stamp += 1
        ppath = os.path.join(run_dir, "audit-%d.prompt.md" % stamp)
        rpath = os.path.join(run_dir, "audit-%d.reply.md" % stamp)
        _write(ppath, prompt)
        r = call_agent(agent, repo, ppath, None, _read_only_passthrough(passthrough),
                       role="auditor", test=False)
        rc, err = r["rc"], r["stderr"]
        tokens += r["tokens"]
        _write(rpath, r["result"] or "")
        replies.append(rpath)
        if rc in (AGENT_USAGE, AGENT_PREFLIGHT, AGENT_APIERR):
            # Refused or dead: no retry can make this call parseable.
            return None, tokens, replies, rc, err
        # A timed-out call has no final result to parse; count it as unusable.
        got = None if rc == AGENT_TIMEOUT else parse_audit(r["result"], len(log))
        if got is not None:
            return got, tokens, replies, rc, err
    return None, tokens, replies, rc, err


FEEDBACK_DETAIL_BYTES = 12000     # all failing checks' output excerpts together


def _fails_text(failing):
    """What the coder is told: each failing item, its evidence line, and an excerpt of the
    check's output (so it can see why, not only that), bounded in total."""
    out, left = [], FEEDBACK_DETAIL_BYTES
    for c in failing:
        out.append("FAIL item %d: %s\n  %s" % (c.index, c.text, c.evidence))
        detail = (getattr(c, "detail", "") or "").strip()
        if detail and left > 0:
            block = "  output:\n" + "\n".join("    " + l for l in detail.splitlines())
            block = block.encode("utf-8")[:left].decode("utf-8", "ignore")
            left -= len(block.encode("utf-8"))
            out.append(block)
    return "\n".join(out)


def _new_files(repo):
    out = _git(repo, "ls-files", "-z", "-o", "--exclude-standard")
    return sorted(x for x in out.split("\0") if x)


def _tool_totals(repo, session):
    """{tool: cumulative calls} in the session transcript -- <session id>.jsonl in
    history.transcript_dir(repo) (which honours QWEN_TRANSCRIPT_DIR), counting the
    tool_use blocks of assistant messages. None when no transcript is readable:
    that is a reporting gap, never a run failure."""
    if not session:
        return None
    try:
        rows = list(history._rows(os.path.join(history.transcript_dir(repo),
                                               "%s.jsonl" % session)))
    except OSError:
        return None
    counts = {}
    for row in rows:
        if row.get("type") != "assistant":
            continue
        msg = row.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = str(b.get("name") or "?")
                counts[name] = counts.get(name, 0) + 1
    return counts


def _report(run_dir, *, reason, code, rounds, session, tokens, results, log, start, repo, denied=(),
            agent_err="", notes=(), tools=None, transcript_found=False):
    diff = _git(repo, "diff", start)
    new = _new_files(repo)
    for rel in new:
        # --no-index exits 1 when the files differ; that is the normal case here.
        diff += subprocess.run(["git", "-C", repo, "diff", "--no-index", "--", os.devnull, rel],
                               capture_output=True, encoding="utf-8", errors="replace").stdout
    _write(os.path.join(run_dir, "diff.patch"), diff)
    rows = ["| %d | %s | %s | %s |" % (r.index, r.text.replace("|", "/"), r.status,
                                       " ".join(r.evidence.split()).replace("|", "/")[:160]) for r in results]
    # Per-tool totals over the run, most-used first (names break ties: the order
    # is stable for a human re-reading the report). Without a readable transcript
    # the section says so instead of pretending nothing was used.
    tool_lines = (["| tool | calls |", "|---|---|"] +
                  ["| %s | %d |" % (name, cnt) for name, cnt in
                   sorted((tools or {}).items(), key=lambda kv: (-kv[1], kv[0]))]
                  ) if transcript_found else ["(no transcript found)"]
    text = "\n".join([
        "# until-done report", "",
        "stop: %s (exit %d)" % (reason, code),
        "rounds: %d" % rounds,
        "session: %s" % (session or "(none)"),
        "tokens: %d" % tokens,
        "denied tool calls: %d%s" % (len(denied), " (%s)" % ", ".join(sorted(set(denied))) if denied else ""),
        "start commit: %s" % start, "",
        "## Checklist", "", "| # | item | status | evidence |", "|---|---|---|---|", *rows, "",
        "## Tool use", "", *tool_lines, "",
        "## Decision log", "", decisions.render(log), "",
        "## Diff", "", "```", _git(repo, "diff", "--stat", start).rstrip(), "```",
        *(["new files:"] + ["  %s" % f for f in new] if new else []),
        # What the agent itself complained about when it refused to run at all.
        *(["", "## Agent stderr", "```", *agent_err.splitlines()[:20], "```"] if agent_err else []),
        *(["", "## Notes", *notes] if notes else []),
        "full diff: %s" % os.path.join(run_dir, "diff.patch"), ""])
    path = os.path.join(run_dir, "report.md")
    _write(path, text)
    return path


def run(o):
    try:
        repo = testrun.toplevel(o.repo)
    except RuntimeError as exc:
        print("until-done: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    try:
        with open(o.task, encoding="utf-8") as fh:
            task_text = fh.read()
        task = taskfile.parse(task_text)
    except (OSError, ValueError) as exc:
        print("until-done: task file: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    if "--web" in o.passthrough or os.environ.get("QWEN_WEB") == "1":
        # Every round runs with -q and its stderr is captured, so qwen-agent's own warning
        # never reaches the person who started the run: say it once here.
        print("WARNING: --web with --until-done: the checks can be gamed by fetching upstream "
              "answers (WebFetch is enabled in every round)", file=sys.stderr)
    test_cmd = os.environ.get("QWEN_TEST_CMD", "")
    if any(i.kind == "test" for i in task.items) and not test_cmd.strip():
        print("until-done: the checklist has test checks but QWEN_TEST_CMD is not set", file=sys.stderr)
        return EXIT_USAGE
    try:
        timeout = int(os.environ.get("QWEN_TEST_TIMEOUT") or testrun.DEFAULT_TIMEOUT)
        _backoff()
    except ValueError:
        print("until-done: QWEN_TEST_TIMEOUT and QWEN_SUPERVISOR_BACKOFF must be integers "
              "(backoff: a comma list)", file=sys.stderr)
        return EXIT_USAGE
    real_state, real_repo = os.path.realpath(state_root()), os.path.realpath(repo)
    if real_state == real_repo or real_state.startswith(real_repo + os.sep):
        print("until-done: the state directory %s is inside the repo; set QWEN_AGENT_STATE elsewhere"
              % real_state, file=sys.stderr)
        return EXIT_USAGE

    sd = repo_state_dir(repo)
    os.makedirs(sd, exist_ok=True)
    lock = os.path.join(sd, "lock")
    # The lock is created INSIDE the try/finally that removes it: with the mkdir
    # before the try, a signal landing between creating the lock and entering
    # the body left the lock held for good -- every later run then exits 14.
    # `mine` keeps a run that found the lock already held from deleting the
    # holder's lock on its way out.
    mine = False
    run_dir = session = None
    rounds = tokens = 0
    denied = []
    agent_err = ""
    notes = []
    prev_tools, tool_totals, transcript_found, tools_session = {}, {}, False, None
    results, log, start = [], [], ""
    try:
        # The handler in _stop_on_signal is not enough on its own: a signal
        # landing between the mkdir and `mine = True` would run the cleanup
        # with mine still False and leave the lock held for good. The stop
        # signals wait in the kernel across that window and are delivered
        # after the assignment, where cleanup knows the lock is ours.
        try:
            mask = _block_stop_signals()
            try:
                os.mkdir(lock)
                mine = True
            finally:
                _unblock_stop_signals(mask)
        except FileExistsError:
            print("until-done: another run holds %s (if no run is active -- a previous one was "
                  "killed with SIGKILL or the machine went down -- remove that directory)" % lock,
                  file=sys.stderr)
            return EXIT_LOCKED
        if _git(repo, "status", "--porcelain").strip() and not o.allow_dirty:
            print("until-done: the working tree has uncommitted changes; commit them or pass --allow-dirty",
                  file=sys.stderr)
            return EXIT_DIRTY
        start = _git(repo, "rev-parse", "HEAD").strip()
        run_dir = os.path.join(sd, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        base, k = run_dir, 1
        while os.path.exists(run_dir):
            k += 1
            run_dir = "%s-%d" % (base, k)
        os.makedirs(run_dir)
        _write(os.path.join(run_dir, "task.md"), task_text)
        log_path = os.path.join(run_dir, "decisions.jsonl")
        t0, prev, garbage, feedback = time.time(), None, 0, None
        timeout_run = 0                 # consecutive unusable rounds that were timeouts
        reason, code = "round limit reached", EXIT_PARTIAL
        for rounds in range(1, o.max_rounds + 1):
            prompt = feedback if (feedback and session) else FIRST.format(
                task=task_text, log=decisions.render(decisions.load(log_path)))
            ppath = os.path.join(run_dir, "round-%d.prompt.md" % rounds)
            _write(ppath, prompt)
            sig_before = _tree_sig(repo)
            r = call_agent(o.agent, repo, ppath, session, o.passthrough)
            # The tree as the agent left it, taken once and before any check runs:
            # a `cmd` check runs in the live repo, so one that writes a file would
            # otherwise make a round that changed nothing look like progress.
            sig_round = _tree_sig(repo)
            # The session transcript accumulates, so this round's calls are the
            # difference from the previous round's totals. No readable transcript
            # is empty counts, never a failure.
            # A round that timed out returns no session id; it is still the same session.
            sid = r["session"] or session
            totals = _tool_totals(repo, sid)
            if sid and sid != tools_session:
                # A new session (a resume that came back with a new id) has its own
                # transcript: count it from zero, not against the old session's totals.
                prev_tools, tools_session = {}, sid
            if totals is None:
                per_round = {}
            else:
                transcript_found = True
                per_round = {name: cnt - prev_tools.get(name, 0) for name, cnt in totals.items()
                             if cnt > prev_tools.get(name, 0)}
                prev_tools = totals
                for name, cnt in per_round.items():
                    tool_totals[name] = tool_totals.get(name, 0) + cnt
            r["tools"] = per_round
            _write(os.path.join(run_dir, "round-%d.json" % rounds), json.dumps(r, indent=1))
            denied += r["denied"]
            if r["rc"] in (AGENT_PREFLIGHT, AGENT_APIERR):
                reason, code = "server error after retries", EXIT_APIERR
                break
            tokens += r["tokens"]
            session = r["session"] or session
            if r["rc"] == AGENT_USAGE:
                # The agent refused to run at all; no retry or round can help.
                reason, code, agent_err = "agent usage error", EXIT_USAGE, r["stderr"]
                break
            # An empty reply is not a failed round: the model may have edited files and
            # simply ended without text. Done is decided by the checks, so they run
            # either way; the round is "unusable" only if it also changed nothing.
            empty = r["rc"] in (AGENT_EMPTY, AGENT_HARNESS, AGENT_TIMEOUT) or not r["result"].strip()
            unusable = empty and sig_round == sig_before
            if not unusable:
                garbage = 0
                timeout_run = 0
            decisions.append(log_path, decisions.parse(r["result"]), session=session,
                             commit=_git(repo, "rev-parse", "HEAD").strip())
            log = decisions.load(log_path)
            try:
                results = checks.run_checks(task.items, repo, test_cmd=test_cmd, timeout=timeout)
            except RuntimeError as exc:
                reason, code = "check runner failed: %s" % exc, EXIT_HARNESS
                break
            failing = [c for c in results if c.status == "FAIL"]
            unlogged = []
            if not failing and not o.no_deviation_audit:
                unlogged, t, audit_replies, audit_rc, audit_err = deviation_audit(
                    o.agent, repo, run_dir, task_text, log, start, o.passthrough)
                tokens += t
                if audit_rc in (AGENT_PREFLIGHT, AGENT_APIERR):
                    # The audit call failed like a coder round would: the server
                    # error ends the run with its own code, not a hand to a human.
                    reason, code = "server error after retries (deviation audit)", EXIT_APIERR
                    break
                if audit_rc == AGENT_USAGE:
                    # The agent refused to run at all; no retry or round can help.
                    reason, code, agent_err = ("agent usage error (deviation audit)",
                                               EXIT_USAGE, audit_err)
                    break
                if unlogged is None:
                    # The coder cannot fix an unreadable audit; hand the diff to a human.
                    why = "timed out" if audit_rc == AGENT_TIMEOUT else "unusable"
                    reason, code = ("checks pass; deviation audit %s "
                                    "\u2014 review the diff manually" % why), EXIT_PARTIAL
                    notes = ["audit reply (unusable): %s" % p for p in audit_replies]
                    break
            now = frozenset(["item%d" % c.index for c in failing] + ["dev:%s" % u for u in unlogged])
            if not now:
                reason, code = "done", EXIT_OK
                break
            if unusable:
                garbage += 1
                timeout_run = timeout_run + 1 if r["rc"] == AGENT_TIMEOUT else 0
                if garbage >= 2:
                    reason, code = ("two rounds timed out with no change" if timeout_run >= 2
                                    else "two unusable replies in a row"), EXIT_HARNESS
                    break
                feedback = ("Your last round ran out of time. Continue where you left off."
                            if r["rc"] == AGENT_TIMEOUT else
                            "Your last reply was empty or unusable. Continue the task.")
                continue
            # Progress = a different failing set OR a tree the AGENT changed. A model
            # still editing toward one stubborn item is working, not stuck. `stalled`
            # is what a writing check forces: its file sits in this round's starting
            # tree, so consecutive signatures differ while the agent does nothing.
            sig = (now, sig_round)
            stalled = prev is not None and now == prev[0] and sig_round == sig_before
            if sig == prev or stalled:
                reason, code = ("no progress: the same checks failed two rounds in a row "
                                "with no change to the tree"), EXIT_NOPROGRESS
                break
            prev = sig
            if (o.budget_tokens and tokens >= o.budget_tokens) or \
               (o.budget_seconds and time.time() - t0 >= o.budget_seconds):
                reason, code = "budget exhausted", EXIT_PARTIAL
                break
            feedback = NOT_DONE.format(
                fails=_fails_text(failing),
                unlogged=("Spec deviations with no DEVIATION entry:\n"
                          + "\n".join("  %s" % u for u in unlogged) + "\n") if unlogged else "")
            if r["denied"]:
                # Repeat the fence where the model will read it: the denials it
                # just hit were attempts at commands that can never run. This
                # round's count only -- the running total would be noise.
                feedback += ("\n%d Bash calls were denied last round. Only qwen-test runs;"
                             " use Read, Grep and Glob for files." % len(r["denied"]))
        path = _report(run_dir, reason=reason, code=code, rounds=rounds, session=session,
                       tokens=tokens, results=results, log=log, start=start, repo=repo,
                       denied=denied, agent_err=agent_err, notes=notes,
                       tools=tool_totals, transcript_found=transcript_found)
        print("until-done: %s after %d round(s); session %s" % (reason, rounds, session or "-"))
        print("report: %s" % path)
        return code
    except KeyboardInterrupt:
        if run_dir:
            path = _report(run_dir, reason="interrupted", code=EXIT_INTERRUPTED, rounds=rounds,
                           session=session, tokens=tokens, results=results, log=log, start=start, repo=repo, denied=denied,
                           tools=tool_totals, transcript_found=transcript_found)
            # A dead terminal (the window closed under the run) must not turn
            # the interrupt into exit 1: an OSError escaping this handler would
            # skip the 130 below, so every print here swallows it.
            with contextlib.suppress(OSError):
                print("report: %s" % path)
        with contextlib.suppress(OSError):
            if session:
                print('until-done: interrupted; continue by hand with: qwen-agent --resume %s "<prompt>"'
                      % session, file=sys.stderr)
            else:
                print("until-done: interrupted before any session started", file=sys.stderr)
        # A dead terminal survived the prints above, but CPython gets one
        # more chance at shutdown: flushing the dead stream there fails
        # again and the interpreter exits 120, contradicting the report's
        # 130. Flush what can still be flushed, then leave without any
        # shutdown at all. os._exit skips the finally below, so the lock
        # goes first; in-process callers must keep their interpreter, so
        # they still get the plain return.
        with contextlib.suppress(OSError):
            sys.stdout.flush()
            sys.stderr.flush()
        if __name__ == "__main__":
            if mine:
                shutil.rmtree(lock, ignore_errors=True)
            os._exit(EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    finally:
        if mine:
            shutil.rmtree(lock, ignore_errors=True)


def _stop_on_signal(signum, frame):
    """Route SIGINT/SIGHUP/SIGTERM through the Ctrl-C path (KeyboardInterrupt).

    The agent runs in its own session, so it never sees the terminal's hangup:
    dying here without stopping it would leave it running -- orphaned, worktree
    held, repo lock held. Raising KeyboardInterrupt makes _run_agent call
    _stop_agent, run() write the partial report, and the finally release the
    lock, exiting 130 like any other interrupt.

    Once the interrupt path has started, further signals are ignored first:
    a second one landing on the cleanup path raises where no handler is
    prepared -- the report half-written, the lock half-removed, the exit code
    no longer 130. Ignoring lets the cleanup finish and still exits 130.
    """
    for _sig in (getattr(signal, n, None) for n in ("SIGTERM", "SIGHUP", "SIGINT")):
        if _sig is not None:
            with contextlib.suppress(ValueError):
                signal.signal(_sig, signal.SIG_IGN)
    raise KeyboardInterrupt


def _block_stop_signals():
    """Park SIGINT/SIGTERM/SIGHUP in the kernel; return the mask to restore.

    Handlers cannot protect a window as short as "mkdir the lock, mark it
    ours": the interrupt they raise may land on either side of the
    assignment. Blocked signals cannot be raised at all; the kernel holds
    them until _unblock_stop_signals, by which point the lock is ours and
    the cleanup path is complete. POSIX only -- where pthread_sigmask is
    missing (Windows) this is a no-op and the window stays as it was.
    """
    if not hasattr(signal, "pthread_sigmask"):
        return None
    sigs = {s for s in (getattr(signal, n, None) for n in ("SIGINT", "SIGTERM", "SIGHUP"))
            if s is not None}
    return signal.pthread_sigmask(signal.SIG_BLOCK, sigs)


def _unblock_stop_signals(mask):
    if mask is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # The platform may have none, some or all; install whatever exists. SIGINT
    # goes through the same handler so the first Ctrl-C also disarms the rest.
    # (Not the main thread -- e.g. an embedding caller -- simply keeps default
    # dispositions rather than failing the run.)
    for _sig in (getattr(signal, n, None) for n in ("SIGTERM", "SIGHUP", "SIGINT")):
        if _sig is not None:
            with contextlib.suppress(ValueError):
                signal.signal(_sig, _stop_on_signal)
    passthrough = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="qwen-agent --until-done")
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--agent", action="append", required=True)
    ap.add_argument("--max-rounds", type=int, default=8)
    ap.add_argument("--budget-tokens", type=int, default=0)
    ap.add_argument("--budget-seconds", type=int, default=0)
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--no-deviation-audit", action="store_true")
    try:
        o = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    o.passthrough = passthrough
    return run(o)


if __name__ == "__main__":
    sys.exit(main())
