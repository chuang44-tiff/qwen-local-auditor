"""The `ask` tool for qwen-agent --advisor: a stdlib MCP server (stdio, JSON-RPC 2.0).

The local model asks a stronger Claude model one question and gets advice back as a
tool result. Each call is one `claude -p` run on the user's own claude login, with no
tools, no settings and one turn, so one call is one request to the advisor model.
Every failure is a tool result that says so: the session never breaks on the advisor.

The environment comes from the MCP config qwen-agent writes:
  QA_ADVISOR_MODEL      the advisor model (opus, sonnet, or a model id)
  QA_ADVISOR_STATE      the run's private directory: call counter, lock, calls.jsonl
  QA_ADVISOR_MAX_CALLS  calls per run (default 4), shared by every session of the run
  QA_ADVISOR_TIMEOUT    seconds per call (default 600)
  QA_ADVISOR_LOG        markdown file each question and answer is appended to
  QA_ADVISOR_CLAUDE     the claude binary (default: claude on PATH)

  advisor_mcp.py                                  serve MCP on stdin/stdout
  advisor_mcp.py --ask Q [--context C] [--path P]  one call; the answer on stdout (exit 3 if unavailable)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

MAX_CONTEXT = 12000
MAX_PATHS = 5
MAX_FILE = 40000
MAX_FILES_TOTAL = 120000
BRIEF_HEAD = (
    "You are advising another AI agent that is auditing or changing code. You cannot run "
    "anything or see anything beyond what is below. Answer the question directly: say which "
    "option you would take and why, what evidence would change your mind, and the one check "
    "the agent should run next. Be brief (under 400 words). If the question cannot be "
    "answered from what is given, say exactly what is missing.")
TOOL = {
    "name": "ask",
    "description": (
        "Ask a stronger model for advice on ONE decision you cannot settle with a probe: "
        "contradictory evidence, a claim you are unsure whether to keep or drop, a design "
        "choice with real trade-offs. It sees only what you send: a self-contained question, "
        "the evidence, and up to 5 files (paths inside the working directory). It cannot find "
        "bugs for you or read the codebase. Verify any claim in its answer before relying on it. "
        "Calls are limited per run."),
    "inputSchema": {"type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "one self-contained question"},
                        "context": {"type": "string",
                                    "description": "what you know, tried and observed (at most 12000 characters)"},
                        "paths": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PATHS,
                                  "description": "files to attach, relative to the working directory"}},
                    "required": ["question"]},
}


class Refused(Exception):
    """A request the model can fix and send again. Spends no call."""


class Unavailable(Exception):
    """The advisor could not answer. The session goes on without it."""


def clean_env(env):
    """The caller's environment minus every Qwen redirect: the child uses the claude login."""
    def drop(k):
        return (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_")
                or k in ("CLAUDE_EFFORT", "AWS_BEARER_TOKEN_BEDROCK"))
    return {k: v for k, v in env.items() if not drop(k)}


def _int(v, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def read_paths(paths, root):
    """([(path, text, truncated)], [note]) for the files inside root; the rest are notes."""
    if paths is None:
        return [], []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise Refused("paths must be a list of file paths")
    if len(paths) > MAX_PATHS:
        raise Refused("attach at most %d files (got %d)" % (MAX_PATHS, len(paths)))
    root_r = os.path.realpath(root)
    files, notes, total = [], [], 0
    for p in paths:
        try:
            full = os.path.realpath(os.path.join(root_r, p))
            inside = os.path.commonpath([full, root_r]) == root_r
        except ValueError:                       # another drive on Windows, or a NUL in the path
            inside = False
        if not inside:
            notes.append("%s: outside the working directory, not attached" % p)
            continue
        if not os.path.isfile(full):
            notes.append("%s: not a file, not attached" % p)
            continue
        try:
            with open(full, "rb") as fh:
                data = fh.read(MAX_FILE + 1)
        except OSError:
            notes.append("%s: unreadable, not attached" % p)
            continue
        text = data[:MAX_FILE].decode("utf-8", "replace")
        if total + len(text) > MAX_FILES_TOTAL:
            notes.append("%s: over the %d KB total, not attached" % (p, MAX_FILES_TOTAL // 1000))
            continue
        total += len(text)
        files.append((p, text, len(data) > MAX_FILE))
    return files, notes


def build_brief(question, context, files):
    parts = [BRIEF_HEAD, "", "QUESTION: " + question.strip(), "", "CONTEXT: " + (context.strip() or "(none)")]
    for p, text, cut in files:
        parts += ["", "FILE %s (%d characters%s):" % (p, len(text), ", truncated" if cut else ""), text]
    return "\n".join(parts) + "\n"


def _lock(state, wait):
    lk = os.path.join(state, "lock")
    t0 = time.time()
    while True:
        try:
            os.mkdir(lk)
            return lk
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lk) > wait + 60:    # a crashed holder
                    os.rmdir(lk)
                    continue
            except OSError:
                pass
            if time.time() - t0 > wait:
                raise Unavailable("another advisor call is still running")
            time.sleep(0.5)


def _count(state):
    try:
        with open(os.path.join(state, "count"), encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _record(state, rec):
    with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _run(claude, model, brief, timeout, env):
    """(answer, model actually used, cost) from one clean claude -p run."""
    argv = [claude, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence",
            "--output-format", "json", "--max-turns", "1"]
    cwd = tempfile.mkdtemp(prefix="qla-advisor-")
    try:
        p = subprocess.run(argv, input=brief, cwd=cwd, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Unavailable("no answer within %ds" % timeout)
    except OSError as e:
        raise Unavailable("could not start claude (%s)" % (e.strerror or e))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    obj = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                obj = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(obj, dict):
        raise Unavailable("claude gave no readable answer (exit %d)" % p.returncode)
    res = obj.get("result")
    if obj.get("is_error") or not isinstance(res, str) or not res.strip():
        why = (res if isinstance(res, str) else "") or str(obj.get("subtype") or "empty answer")
        raise Unavailable("claude reported an error: %s" % why.strip()[:200])
    used = ",".join(sorted((obj.get("modelUsage") or {}).keys())) or model
    cost = obj.get("total_cost_usd")
    return res.strip(), used, cost if isinstance(cost, (int, float)) else None


def _log(path, n, budget, model, question, context, notes, answer):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Advisor call %d of %d (%s)\n\n**Question:** %s\n\n**Context:**\n\n%s\n\n"
                     % (n, budget, model, question.strip(), context.strip() or "(none)"))
            if notes:
                fh.write("**Not attached:** %s\n\n" % "; ".join(notes))
            fh.write("**Advice:**\n\n%s\n\n" % answer)
    except OSError:
        pass                                     # the log is a convenience, never a failure


def ask(question, context, paths, env, root):
    if not isinstance(question, str) or not question.strip():
        raise Refused("the question is empty: send one self-contained question")
    context = context if isinstance(context, str) else ""
    if len(context) > MAX_CONTEXT:
        raise Refused("the context is %d characters; the limit is %d. Shorten it to the "
                      "evidence that matters." % (len(context), MAX_CONTEXT))
    files, notes = read_paths(paths, root)
    state = env.get("QA_ADVISOR_STATE")
    if not state or not os.path.isdir(state):
        raise Unavailable("no advisor state directory (start the run with qwen-agent --advisor)")
    model = env.get("QA_ADVISOR_MODEL") or "opus"
    budget = _int(env.get("QA_ADVISOR_MAX_CALLS"), 4)
    timeout = _int(env.get("QA_ADVISOR_TIMEOUT"), 600)
    # which() even for a configured name: Windows CreateProcess finds neither claude.cmd nor
    # a bare name the way a shell does.
    named = env.get("QA_ADVISOR_CLAUDE") or "claude"
    claude = shutil.which(named, path=env.get("PATH")) or named
    brief = build_brief(question, context, files)
    lk = _lock(state, timeout)
    try:
        n = _count(state)
        if n >= budget:
            raise Unavailable("the budget of %d advisor calls for this run is spent" % budget)
        with open(os.path.join(state, "count"), "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))
        t0 = time.time()
        try:
            answer, used, cost = _run(claude, model, brief, timeout, clean_env(env))
        except Unavailable as e:
            _record(state, {"n": n + 1, "model": model, "seconds": round(time.time() - t0, 1),
                            "cost_usd": None, "unavailable": str(e)})
            raise
        _record(state, {"n": n + 1, "model": used, "seconds": round(time.time() - t0, 1),
                        "cost_usd": cost, "unavailable": None})
    finally:
        try:
            os.rmdir(lk)
        except OSError:
            pass
    _log(env.get("QA_ADVISOR_LOG"), n + 1, budget, used, question, context, notes, answer)
    tail = ("\n\nNot attached: " + "; ".join(notes)) if notes else ""
    return "ADVICE (%s, call %d of %d):\n%s%s" % (used, n + 1, budget, answer, tail)


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _text(mid, text, is_error):
    return _reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def handle(msg, env=None, root=None):
    env = os.environ if env is None else env
    root = os.getcwd() if root is None else root
    if not isinstance(msg, dict):
        return _reply(None, error={"code": -32600, "message": "invalid request"})
    mid, method = msg.get("id"), msg.get("method")
    if mid is None or method is None:
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _reply(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "qla-advisor", "version": "1"}})
    if method == "ping":
        return _reply(mid, {})
    if method == "tools/list":
        return _reply(mid, {"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != "ask":
            return _reply(mid, error={"code": -32602, "message": "unknown tool %r" % params.get("name")})
        a = params.get("arguments")
        a = a if isinstance(a, dict) else {}
        try:
            return _text(mid, ask(a.get("question"), a.get("context"), a.get("paths"), env, root), False)
        except Refused as e:
            return _text(mid, "REFUSED: %s" % e, True)
        except Unavailable as e:
            return _text(mid, "ADVISOR UNAVAILABLE: %s. Decide on the evidence you have." % e, False)
    return _reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def serve(stdin, stdout, env=None, root=None):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            reply = _reply(None, error={"code": -32700, "message": "parse error"})
        else:
            try:
                reply = handle(msg, env, root)
            except Exception:
                mid = msg.get("id") if isinstance(msg, dict) else None
                reply = _reply(mid, error={"code": -32603, "message": "internal error"})
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv=None):
    for s, kw in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                  (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"}),
                  (sys.stderr, {"encoding": "utf-8"})):
        if hasattr(s, "reconfigure"):
            s.reconfigure(**kw)
    ap = argparse.ArgumentParser(prog="advisor_mcp.py")
    ap.add_argument("--ask")
    ap.add_argument("--context", default="")
    ap.add_argument("--path", action="append")
    o = ap.parse_args(argv)
    if o.ask is None:
        serve(sys.stdin, sys.stdout)
        return 0
    try:
        print(ask(o.ask, o.context, o.path, os.environ, os.getcwd()))
    except (Refused, Unavailable) as e:
        print("advisor: %s" % e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
