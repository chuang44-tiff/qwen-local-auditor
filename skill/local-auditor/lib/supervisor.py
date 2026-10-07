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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import checks, decisions, probe, taskfile, testrun  # noqa: E402
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

# --review-round: one extra round, resumed in the same session, after the checks first pass.
REVIEW_ROUND = """Review round: every check passes. Before the task is final, try to break your change.
Go back over each thing you changed and each checklist item. For each one, look for the
input, state or code path that would make it wrong -- empty, huge or odd inputs, paths with
spaces, non-UTF-8 bytes, platform differences, an interrupted or resumed run -- and check it
with a test (`qwen-test [SELECTOR]`) or by reading the code again. Fix what fails. The
harness runs every check again after this round. Reply as before: a DEVIATION block for
each deliberate departure, then the files you changed."""


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


def call_agent(agent, repo, prompt_path, session, passthrough, role="coder", test=True,
               probe_here=False):
    # --shallow goes on the command line, not only into the environment below:
    # qwen-agent reads its config file AFTER the environment, so a QWEN_DEPTH=deep
    # sitting in the config would otherwise re-imply depth (a review round, a
    # probe) into every round and into the audit. A typed flag beats env and
    # config alike. The rounds carry the depth the loop decomposed once as typed
    # tokens in the passthrough; nothing else may add to that.
    argv = list(agent) + ["--shallow", "--json", "--warn-denials", "-q", "-r", role]
    if test:
        argv.append("--test")
    if probe_here:
        argv.append("--probe-here")     # repo is the run's probe sandbox: a whole shell there
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

    Every agent call runs with QWEN_DEPTH=shallow (and a typed --shallow on its
    command line, which beats a config file): the loop decomposed depth once
    (qwen-agent.sh hands the rounds their switches as typed tokens), and a round
    that re-implied depth would compound it -- review-round a review round,
    nudge on top of a nudge -- until every round is a deep run.
    """
    posix = os.name == "posix"
    env = dict(os.environ)
    env["QWEN_DEPTH"] = "shallow"
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                         env=env,
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
# A real audit cites more than the one number it was logged under:
# "D2 (also D4, D6, D8, D9)". What matters is that the field STARTS with a
# D-number (only "*" emphasis or backticks around it allowed); the rest is
# prose, and only the first number gets the range check.
_LOGGED_D = re.compile(r"^[*`]*D(\d+)(?:\W|$)", re.I)
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
        logged = _LOGGED_D.match(f.get("logged", "").strip())
        if not logged or not 1 <= int(logged.group(1)) <= n_logged:
            out.append(f.get("code") or "(unlocated)")
    return out


# Options that would let the read-only audit change files or widen its tools.
_MUTATING_FLAGS = {"--write": 0, "--all-tools": 0, "--unrestricted": 0,
                   "-t": 1, "--tools": 1, "--toolset": 1, "--permission-mode": 1,
                   # The depth switches shape the coder's rounds; the audit stays the plain
                   # extraction its CONTRADICTION parser expects.
                   "--role-variant": 1, "--subagents-nudge": 0, "--subagents-push": 0}


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
            agent_err="", notes=(), tools=None, transcript_found=False, switches=(), depth=None):
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
        "start commit: %s" % start,
        *(["depth: %s" % depth] if depth else []),
        *(["switches: %s" % ", ".join(switches)] if switches else []), "",
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


def _add_note(path, note):
    """Append one line to report.md's `## Notes` section, opening that section when the
    report was written without notes. A cleanup that fails after the report exists is
    still worth reporting, and report.md is what the caller reads; the note goes before
    the final `full diff:` line, exactly where _report writes the others."""
    if not path:
        return
    with contextlib.suppress(OSError):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        at = next((i for i, ln in enumerate(lines)
                   if ln.startswith("full diff: ")), len(lines))
        lines[at:at] = [note] if "## Notes" in lines else ["", "## Notes", note]
        _write(path, "\n".join(lines) + "\n")


def _switches(o):
    """The depth switches of this run, as report.md lists them (none: no line)."""
    out = []
    if getattr(o, "probe", False):
        out.append("probe")
    if getattr(o, "review_round", False):
        out.append("review_round")
    pt = list(o.passthrough)
    for i, a in enumerate(pt):
        if a == "--role-variant" and i + 1 < len(pt):
            out.append("role_variant=%s" % pt[i + 1])
        elif a.startswith("--role-variant="):
            out.append("role_variant=%s" % a.split("=", 1)[1])
        elif a == "--subagents-nudge":
            out.append("subagents_nudge")
        elif a == "--subagents-push":
            out.append("subagents_push")
    return out


def run(o):
    """The until-done loop with the environment hygiene around it.

    _run pops the GIT_* steering variables and sets GIT_OPTIONAL_LOCKS under
    --probe; os.environ must come back exactly as it was found -- set the ones
    that were there, delete the ones that were not. A supervisor used
    in-process (the test suite) that left the optional locks off would change
    every later git call in the process, and could hide a git call that writes
    the user's index. The __main__ interrupt path
    os._exits past this finally; a hard exit leaves whatever the run set, but
    that process' environment dies with it.
    """
    names = probe.GIT_ENV_VARS + ("GIT_OPTIONAL_LOCKS",)
    saved = {n: os.environ[n] for n in names if n in os.environ}
    try:
        return _run(o)
    finally:
        for _steer in names:
            if _steer in saved:
                os.environ[_steer] = saved[_steer]
            else:
                os.environ.pop(_steer, None)


def _run(o):
    if o.probe:
        # An inherited GIT_* aims every git call this process and every child makes --
        # probe.make's clone, the sandbox rounds, the checks alike -- at a repository
        # other than the one the arguments name: with GIT_INDEX_FILE=<the user's
        # .git/index> (a pre-commit hook keeps it in the environment), the sandbox's
        # writes would land in the user's index. qwen-agent.sh unsets the same six before
        # exec'ing this; this is the belt for that braces -- a supervisor started
        # directly gets the same hygiene, before the first git call.
        for _steer in probe.GIT_ENV_VARS:
            os.environ.pop(_steer, None)
        # What git calls may still name the USER's repo under --probe are the read-only
        # ones the sandbox build needs: this process's `rev-parse --show-toplevel` and
        # probe.make's diff-index/ls-files. They take no index lock even so: with the
        # optional locks off, git can never touch index.lock beside the user's tree.
        os.environ["GIT_OPTIONAL_LOCKS"] = "0"
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
    report_path = None                # the report.md of this run, once written (what a
                                      # late cleanup failure adds its note to)
    rounds = tokens = 0
    denied = []
    agent_err = ""
    notes = []
    patch_written = False               # probe.patch is out: the work no longer needs the sandbox
    prev_tools, tool_totals, transcript_found, tools_session = {}, {}, False, None
    results, log, start = [], [], ""
    user_repo, sb = repo, None          # --probe: every round works in sb, never in user_repo

    def finish_probe():
        """Write RUN/probe.patch from the sandbox once; returns its path (None: no sandbox).
        Whatever probe.write_patch raises propagates -- a refused diff leaves patch_written
        False, and that is what keeps the sandbox from being removed."""
        nonlocal patch_written
        if sb is None:
            return None
        path = os.path.join(run_dir, "probe.patch")
        if not patch_written:
            # Written to a fresh temp file and moved into place, never skipped
            # because a file already sits at the target: the session has a shell
            # in the sandbox, and `../../probe.patch` from it IS this path -- a
            # planted file must never be handed back as the run's patch.
            fd, tmp = tempfile.mkstemp(dir=run_dir, prefix="probe.patch.")
            os.close(fd)
            try:
                probe.write_patch(sb, tmp)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            patch_written = True
            # The apply hint must paste into a shell as it stands (shlex.quote), as
            # qwen-agent.sh quotes its own -- a state or repo path with a space or a '
            # would break the bare spelling.
            notes.append("probe patch: %s (not applied; to apply: git -C %s apply %s)"
                         % (path, shlex.quote(str(user_repo)), shlex.quote(path)))
            if o.keep_sandbox:
                notes.append("sandbox kept: %s" % sb)
        return path

    def drop_sandbox():
        # The sandbox holds the run's only copy of the work until probe.patch is out of
        # it: never remove it when the patch could not be written. A probe.make that died
        # halfway left no sandbox (sb None) but still left the half-made clone and the
        # marker under run_dir -- and there is nothing to keep there, --keep-sandbox
        # included. A sweep that fails (raises, or refuses) leaves a sandbox behind that
        # nobody chose to keep: return its path so the caller says so -- probe.remove
        # swallows the individual rmdir failures itself, so an exception OR a False
        # answer both mean "something is still there".
        if o.probe and run_dir is not None and (sb is None
                                                or (patch_written and not o.keep_sandbox)):
            try:
                gone = probe.remove(run_dir)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                gone = False
            if not gone:
                return str(sb if sb is not None else run_dir)
        return None
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
        # A probe run copies the uncommitted state into its sandbox and writes nothing back.
        # Under --probe the status call is skipped entirely, not just its answer: `git
        # status` refreshes stat info and WRITES .git/index back. GIT_OPTIONAL_LOCKS=0
        # does stop that opportunistic refresh -- but the user's tree is read-only for a
        # probe run (the sandbox is meant to carry the dirt), so never calling status is
        # the primary guard and no part of this run may depend on that env var. Same for
        # the start sha: under --probe it is
        # re-read from the sandbox below, so no git call here touches the user's repo
        # (probe.make's own reads are the only ones that may).
        if not o.probe and _git(repo, "status", "--porcelain").strip() and not o.allow_dirty:
            print("until-done: the working tree has uncommitted changes; commit them or pass --allow-dirty",
                  file=sys.stderr)
            return EXIT_DIRTY
        start = "" if o.probe else _git(repo, "rev-parse", "HEAD").strip()
        run_dir = os.path.join(sd, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        base, k = run_dir, 1
        while os.path.exists(run_dir):
            k += 1
            run_dir = "%s-%d" % (base, k)
        os.makedirs(run_dir)
        if o.probe:
            # probe.check() and probe.remove() only accept a run folder carrying create()'s
            # marker; this run owns run_dir, so drop it in before the sandbox under it.
            _write(os.path.join(run_dir, probe.RUN_MARKER), probe.RUN_MARKER_WORD + "\n")
            try:
                sb, _ = probe.make(repo, repo, run_dir)
            except Exception as exc:
                # A half-made clone must not outlive the run (drop_sandbox sweeps it:
                # sb is still None), and the failure must leave a report. Write it
                # BEFORE the sweep -- probe.remove empties a run_dir that holds nothing
                # else, and the report would have nowhere to go. There is no sandbox to
                # diff, so the report's git calls run against run_dir: under --probe the
                # user's tree takes no git call of this process at all.
                # (KeyboardInterrupt and SystemExit are BaseExceptions: they keep their
                # own paths -- the interrupted report and 130 below.)
                print("until-done: --probe: %s" % exc, file=sys.stderr)
                path = _report(run_dir, reason="error: %s: %s" % (type(exc).__name__, exc),
                               code=EXIT_HARNESS, rounds=rounds, session=session,
                               tokens=tokens, results=results, log=log, start=start,
                               repo=run_dir, denied=denied, switches=_switches(o),
                               depth=getattr(o, "depth", None))
                report_path = path
                print("report: %s" % path)
                return EXIT_HARNESS
            repo = str(sb)
            start = _git(repo, "rev-parse", "HEAD").strip()
        _write(os.path.join(run_dir, "task.md"), task_text)
        log_path = os.path.join(run_dir, "decisions.jsonl")
        t0, prev, garbage, feedback = time.time(), None, 0, None
        reviewed = False                # --review-round: the one review round has been asked for
        timeout_run = 0                 # consecutive unusable rounds that were timeouts
        reason, code = "round limit reached", EXIT_PARTIAL
        for rounds in range(1, o.max_rounds + 1):
            prompt = feedback if (feedback and session) else FIRST.format(
                task=task_text, log=decisions.render(decisions.load(log_path)))
            ppath = os.path.join(run_dir, "round-%d.prompt.md" % rounds)
            _write(ppath, prompt)
            sig_before = _tree_sig(repo)
            r = call_agent(o.agent, repo, ppath, session, o.passthrough, probe_here=o.probe)
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
                    notes += ["audit reply (unusable): %s" % p for p in audit_replies]
                    break
            now = frozenset(["item%d" % c.index for c in failing] + ["dev:%s" % u for u in unlogged])
            if not now:
                if o.review_round and not reviewed:
                    if session:
                        reviewed = True
                        spent = ((o.budget_tokens and tokens >= o.budget_tokens) or
                                 (o.budget_seconds and time.time() - t0 >= o.budget_seconds))
                        if rounds < o.max_rounds and not spent:
                            # One more round in the same session; the checks (and the audit)
                            # run again after it, and it counts toward --max-rounds.
                            notes.append("review round: round %d" % (rounds + 1))
                            prev, feedback = None, REVIEW_ROUND
                            continue
                        notes.append("review round skipped: the checks passed with no round or "
                                     "budget left for it")
                    else:
                        # Nothing to resume: say so instead of letting "done" read as reviewed.
                        notes.append("review round skipped: no session id")
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
                                "and the agent changed nothing"), EXIT_NOPROGRESS
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
        patch = finish_probe()
        path = _report(run_dir, reason=reason, code=code, rounds=rounds, session=session,
                       tokens=tokens, results=results, log=log, start=start, repo=repo,
                       denied=denied, agent_err=agent_err, notes=notes,
                       tools=tool_totals, transcript_found=transcript_found,
                       switches=_switches(o), depth=getattr(o, "depth", None))
        report_path = path
        print("until-done: %s after %d round(s); session %s" % (reason, rounds, session or "-"))
        if patch:
            print("patch: %s" % patch)
        if sb is not None and o.keep_sandbox:
            print("sandbox kept: %s" % sb)     # as --keep-sandbox's help promises
        print("report: %s" % path)
        return code
    except KeyboardInterrupt:
        patch = None
        if run_dir:
            with contextlib.suppress(Exception):
                # an interrupted probe run still hands back its work; ANY failure here
                # (a refused diff included) leaves patch_written False, so the sandbox
                # is kept below rather than removed with the work still only in it
                patch = finish_probe()
            if patch is None and sb is not None:
                print("sandbox kept: %s" % sb, file=sys.stderr)
                # The stderr line dies with a closed terminal; the report is what
                # the reader finds, and it must name the kept sandbox too.
                notes.append("sandbox kept: %s" % sb)
            path = _report(run_dir, reason="interrupted", code=EXIT_INTERRUPTED, rounds=rounds,
                           session=session, tokens=tokens, results=results, log=log, start=start,
                           repo=run_dir if (o.probe and sb is None) else repo,
                           denied=denied, notes=notes,
                           tools=tool_totals, transcript_found=transcript_found,
                           switches=_switches(o), depth=getattr(o, "depth", None))
            report_path = path
            # A dead terminal (the window closed under the run) must not turn
            # the interrupt into exit 1: an OSError escaping this handler would
            # skip the 130 below, so every print here swallows it.
            with contextlib.suppress(OSError):
                if patch:
                    print("patch: %s" % patch)
                    if o.keep_sandbox and sb is not None:
                        print("sandbox kept: %s" % sb)     # as --keep-sandbox's help promises
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
            # os._exit skips the finally below, so the sweep and its leftover
            # report happen here. The note goes in before the print: a dead
            # terminal takes the print, not the report.
            with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
                leftover = drop_sandbox()
                if leftover is not None:
                    _add_note(report_path, "sandbox not removed: %s" % leftover)
                    print("sandbox not removed: %s" % leftover, file=sys.stderr)
            if mine:
                shutil.rmtree(lock, ignore_errors=True)
            os._exit(EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    except Exception as exc:
        # Without --probe the work sits in the user's tree and survives a crash; under
        # --probe the sandbox is its only copy, so an error must not throw it away: try
        # the patch first, and when it cannot be written (a refused diff, a dead agent
        # that deleted the .base) KEEP the sandbox and say so on stderr. Then report and
        # exit 8 -- no traceback either way.
        if not o.probe or run_dir is None:
            raise
        patch = None
        with contextlib.suppress(Exception):
            patch = finish_probe()        # may raise again: the same refused diff
        if patch is None and sb is not None:
            print("sandbox kept: %s" % sb, file=sys.stderr)
            # Name the kept sandbox in the report too: it holds the run's work.
            notes.append("sandbox kept: %s" % sb)
        path = _report(run_dir, reason="error: %s: %s" % (type(exc).__name__, exc),
                       code=EXIT_HARNESS, rounds=rounds, session=session, tokens=tokens,
                       results=results, log=log, start=start,
                       repo=repo if sb is not None else run_dir,
                       denied=denied, agent_err=agent_err, notes=notes,
                       tools=tool_totals, transcript_found=transcript_found,
                       switches=_switches(o), depth=getattr(o, "depth", None))
        report_path = path
        with contextlib.suppress(OSError):
            if patch:
                print("patch: %s" % patch)
            print("report: %s" % path)
        return EXIT_HARNESS
    finally:
        # A sandbox sweep that raises must not skip the lock removal: the next run
        # would be locked out (exit 14) by a run that already ended. A sweep that
        # cannot finish leaves a sandbox nobody chose to keep: say so -- stderr and
        # the report, with the exit code unchanged (this is a leftover, not a failure
        # of the run itself).
        with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
            leftover = drop_sandbox()
            if leftover is not None:
                _add_note(report_path, "sandbox not removed: %s" % leftover)
                print("sandbox not removed: %s" % leftover, file=sys.stderr)
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
    ap.add_argument("--review-round", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--keep-sandbox", action="store_true")
    # The depth mode the shell computed for this loop (report.md records it). The
    # shell decomposes depth ONCE: every agent call this runs gets a typed
    # --shallow and QWEN_DEPTH=shallow (see _run_agent), so a round never implies
    # a depth switch of its own.
    ap.add_argument("--depth", choices=("default", "deep", "shallow"), default=None)
    try:
        o = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    o.passthrough = passthrough
    return run(o)


if __name__ == "__main__":
    sys.exit(main())
