"""The `qla_msg` MCP server (spec 12.4): share, reply and ask_main.

One stdlib server over stdio with the lib/advisor_mcp.py protocol shape:
newline-delimited JSON-RPC 2.0, methods `initialize`, `ping`, `tools/list`
and `tools/call`, no answer to a notification (no `id`), -32700 for a line
that is not JSON, -32601 for an unknown method, -32603 for a handler that
raised, and a request that is not an object -32600. A tool never raises at
the session and never blocks on another process: a refusal the model can fix
comes back as an `isError: true` text result, and the two ask_main refusals
that are NOT the model's fault (no listener, an open question already there)
come back as ordinary text. A tool name the agent does not hold is a JSON-RPC
error -32602 `unknown tool`.

Identity comes only from the environment the launcher sets -- no tool argument
names a sender or a target, and the server writes only inside its own message
dir, through lib/msgdir.py (never the inbox, never another agent's dir):

  QWEN_MSG_DIR     the agent's own msg dir (lib/msgdir.py). Required: unset or
                   not a directory, `tools/list` is empty and every call errors.
  QWEN_MSG_TOOLS   comma list of the tools this agent gets: share, reply,
                   ask_main. Any other name in it is ignored.
  QWEN_MSG_SELF    the agent's address, for log lines only.
  QWEN_MSG_PHASE   share: the phase id the findings of this call belong to.
  QWEN_ASKS_DIR    ask_main: the run's asks/ dir, read only (the lease check).
  QWEN_SHARE_MAX   share: admitted findings per sender per phase (default 3,
                   clamped to 0..5).

Every tools/call sweeps stale replies/*.tmp first (best effort, errors
ignored); accepted calls append one text-free line to M/log.jsonl.

  python lib/msg_mcp.py        serve MCP on stdin/stdout
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import asks, msgdir  # noqa: E402
from lib.notes import (MAX_PATH_CHARS, MAX_PATHS, append_jsonl,  # noqa: E402
                       imitates_header, path_on_one_line)

SERVER_NAME = "qla_msg"
TOOL_ORDER = ("share", "reply", "ask_main")

SHARE_MAX_CHARS = 1500        # the finding, after strip
REPLY_MAX_CHARS = 4000        # the answer, after strip
QUESTION_MAX_CHARS = 800      # the question, after strip
OPTIONS_MAX = 4               # ask_main: at most 4 ...
OPTION_CHARS_MAX = 80         # ... single-line options of at most 80 chars
SHARE_MAX_DEFAULT = 3         # QWEN_SHARE_MAX ...
SHARE_MAX_CAP = 5             # ... clamped to 0..5

SHARE_QUEUED = "finding queued for the other units of this phase"
REPLY_SENT = "answer sent"
ALREADY_ANSWERED = "already answered"
NOT_DELIVERED = "no question with that id was delivered to you"
NO_HEADER = "a finding may not imitate a note header"
NO_LISTENER = "no main session is listening; decide yourself and say what you assumed"
OPEN_QUESTION = "you already have an open question; keep working"
QUESTION_QUEUED = ("question queued; an answer, if any, arrives as a note. "
                   "Keep working; do not wait for it.")

TOOL_SHARE = {
    "name": "share",
    "description": (
        "Share one verified finding with the other units of your phase, with the "
        "evidence, when it changes what they should do: a bug you proved, a wrong "
        "assumption, a constraint you hit. `finding` is the claim plus its evidence "
        "(at most 1500 characters); `paths` names the files it concerns. Not for "
        "progress reports -- you get a few admitted findings per phase."),
    "inputSchema": {"type": "object",
                    "properties": {
                        "finding": {"type": "string",
                                    "description": "the claim and its evidence, at most 1500 characters"},
                        "paths": {"type": "array", "items": {"type": "string"},
                                  "maxItems": MAX_PATHS,
                                  "description": "repo-relative path[:line] strings, at most "
                                                 "10 of at most 200 characters"}},
                    "required": ["finding"]},
}
TOOL_REPLY = {
    "name": "reply",
    "description": (
        "Answer a question note from the main session by its note id (the note's "
        "header names the id and tells you to use this tool). Send the full answer "
        "in `text` (at most 4000 characters). Only a question actually delivered "
        "to you can be answered, and each one only once."),
    "inputSchema": {"type": "object",
                    "properties": {
                        "note_id": {"type": "string",
                                    "description": "the id of the delivered question note"},
                        "text": {"type": "string",
                                 "description": "the answer, at most 4000 characters"}},
                    "required": ["note_id", "text"]},
}
TOOL_ASK_MAIN = {
    "name": "ask_main",
    "description": (
        "Ask the main session (the user's agent) one question only it can answer: "
        "intent or scope, or a decision you cannot settle from your own evidence. "
        "Never progress. With options you offer the choices. The answer, if one "
        "comes, arrives later as a note -- keep working and do not wait for it; "
        "while one question of yours is open you cannot send another."),
    "inputSchema": {"type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "the question, at most 800 characters"},
                        "options": {"type": "array", "items": {"type": "string"},
                                    "maxItems": OPTIONS_MAX,
                                    "description": "the choices, at most 4 single-line "
                                                   "strings of at most 80 characters"}},
                    "required": ["question"]},
}
TOOL_BY_NAME = {t["name"]: t for t in (TOOL_SHARE, TOOL_REPLY, TOOL_ASK_MAIN)}


class Refused(Exception):
    """A tool refusal the model can fix: an isError tool result, never a raise
    at the session."""


def _int(v, default):
    """int(v), the default for anything that is not a number (or absent)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _msg_dir(env):
    """The agent's msg dir from QWEN_MSG_DIR, or None when it is absent or not
    a directory -- the one state every tool needs and none of them supplies."""
    M = env.get("QWEN_MSG_DIR")
    if not M or not isinstance(M, str) or not os.path.isdir(M):
        return None
    return M


def enabled_tools(env):
    """The tools this server lists and accepts: QWEN_MSG_TOOLS narrowed to the
    three we know, and nothing at all without a usable QWEN_MSG_DIR."""
    if _msg_dir(env) is None:
        return []
    wanted = {w.strip() for w in (env.get("QWEN_MSG_TOOLS") or "").split(",")}
    return [name for name in TOOL_ORDER if name in wanted]


def _log(env, M, ev, fields):
    """One text-free line in M/log.jsonl (the note text never comes here);
    best effort -- a log that cannot be written is none of the caller's
    business, like every other log in this skill."""
    try:
        rec = {"t": round(time.time(), 3), "ev": ev, "self": env.get("QWEN_MSG_SELF") or ""}
        rec.update(fields)
        append_jsonl(os.path.join(M, "log.jsonl"), rec)
    except Exception:
        pass


# ----------------------------------------------------------------- the tools
def _tool_share(args, env, M):
    """One admitted finding for the phase, or an isError refusal naming the
    limit it crossed; a refusal spends no share and writes nothing."""
    finding = args.get("finding")
    if not isinstance(finding, str) or not finding.strip():
        raise Refused("the finding is empty: send 1..%d characters" % SHARE_MAX_CHARS)
    finding = finding.strip()
    if len(finding) > SHARE_MAX_CHARS:
        raise Refused("the finding is %d characters; the limit is %d"
                      % (len(finding), SHARE_MAX_CHARS))
    paths = args.get("paths")
    if paths is None:
        paths = []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise Refused("paths must be a list of strings")
    if len(paths) > MAX_PATHS:
        raise Refused("attach at most %d paths (got %d)" % (MAX_PATHS, len(paths)))
    for p in paths:
        if not p:
            # an empty name is no path to attach (and str.splitlines() of ""
            # would fail path_on_one_line's one-line rule for the wrong reason)
            raise Refused("a path may not be empty")
        if len(p) > MAX_PATH_CHARS:
            raise Refused("a path is %d characters; the limit is %d"
                          % (len(p), MAX_PATH_CHARS))
        if not path_on_one_line(p):
            raise Refused("a path may not hold a line break or a control character")
    if imitates_header(finding) or any(imitates_header(p) for p in paths):
        raise Refused(NO_HEADER)
    phase = env.get("QWEN_MSG_PHASE") or ""
    limit = max(0, min(SHARE_MAX_CAP, _int(env.get("QWEN_SHARE_MAX"), SHARE_MAX_DEFAULT)))
    state = msgdir.share_state(M)
    counts = state.get("count")
    counts = counts if isinstance(counts, dict) else {}
    seen_map = state.get("seen_texts")
    seen_map = seen_map if isinstance(seen_map, dict) else {}
    count = counts.get(phase)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = 0
    if count >= limit:
        raise Refused("share limit reached (%d); keep working" % limit)
    seen = seen_map.get(phase)
    seen = seen if isinstance(seen, list) else []
    digest = hashlib.sha256(finding.encode("utf-8")).hexdigest()
    if digest in seen:
        raise Refused("you already shared this finding")
    # the state first (spec 11.4): the count and the duplicate hash are this
    # sender's durable budget, and the share becomes visible only once they are
    # on disk -- a crash after write_out must never leave a finding in the
    # outbox that its sender's state does not know about. A state-save failure
    # is an error that writes nothing; a write_out failure after the save
    # reports the error as today.
    counts[phase] = count + 1
    seen_map[phase] = seen + [digest]
    msgdir.save_share_state(M, {"count": counts, "seen_texts": seen_map})
    note_id = msgdir.write_out(M, "share",
                               {"phase": phase, "finding": finding, "paths": paths})
    _log(env, M, "share", {"share_id": note_id, "phase": phase,
                           "len": len(finding), "paths": len(paths)})
    return SHARE_QUEUED


def _tool_reply(args, env, M):
    """The one answer to a question this agent was actually delivered (the
    seen marker of the driver, spec 12.2), published once."""
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused("the answer is empty: send 1..%d characters" % REPLY_MAX_CHARS)
    text = text.strip()
    if len(text) > REPLY_MAX_CHARS:
        raise Refused("the answer is %d characters; the limit is %d"
                      % (len(text), REPLY_MAX_CHARS))
    note_id = args.get("note_id")
    if not msgdir.ask_seen(M, note_id):
        raise Refused(NOT_DELIVERED)
    if not msgdir.publish_reply(M, note_id, text):
        raise Refused(ALREADY_ANSWERED)
    _log(env, M, "reply", {"note_id": note_id, "len": len(text)})
    return REPLY_SENT


def _tool_ask_main(args, env, M):
    """A question only the main session can answer, queued for the relay --
    or the plain text that tells the model to decide yourself (no listener,
    or one of the agent's own questions still open): never an error."""
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        raise Refused("the question is empty: send 1..%d characters" % QUESTION_MAX_CHARS)
    question = question.strip()
    if len(question) > QUESTION_MAX_CHARS:
        raise Refused("the question is %d characters; the limit is %d"
                      % (len(question), QUESTION_MAX_CHARS))
    options = args.get("options")
    if options is None:
        options = []
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise Refused("options must be a list of strings")
    if len(options) > OPTIONS_MAX:
        raise Refused("give at most %d options (got %d)" % (OPTIONS_MAX, len(options)))
    for o in options:
        if len(o.splitlines()) > 1 or o.endswith(("\n", "\r")):
            raise Refused("an option must be a single line")
        if len(o) > OPTION_CHARS_MAX:
            raise Refused("an option is %d characters; the limit is %d"
                          % (len(o), OPTION_CHARS_MAX))
    if imitates_header(question) or any(imitates_header(o) for o in options):
        raise Refused(NO_HEADER)
    asks_dir = env.get("QWEN_ASKS_DIR")
    if not asks_dir or not isinstance(asks_dir, str):
        return NO_LISTENER
    try:
        live = asks.lease_live(os.path.dirname(asks_dir))    # read only, spec 12.4
    except Exception:
        live = False
    if not live:
        return NO_LISTENER
    if msgdir.open_question(M) is not None:
        return OPEN_QUESTION
    q_id = msgdir.write_out(M, "question", {"question": question, "options": options})
    msgdir.set_open_question(M, q_id, msgdir.main_count(M))
    _log(env, M, "ask_main", {"question_id": q_id, "len": len(question),
                              "options": len(options)})
    return QUESTION_QUEUED


HANDLERS = {"share": _tool_share, "reply": _tool_reply, "ask_main": _tool_ask_main}


# ----------------------------------------------------------------- the protocol
def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _text(mid, text, is_error):
    return _reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def _call(mid, params, env):
    """One tools/call: the stale-tmp sweep first (best effort), the dispatch
    table by name, and a tool the agent does not hold as -32602 `unknown tool`."""
    M = _msg_dir(env)
    if M is not None:
        try:
            msgdir.sweep_stale_tmp(M)
        except Exception:
            pass                                 # the sweep is best effort, spec 12.2
    name = params.get("name")
    if name not in enabled_tools(env):
        return _reply(mid, error={"code": -32602, "message": "unknown tool"})
    args = params.get("arguments")
    args = args if isinstance(args, dict) else {}
    try:
        return _text(mid, HANDLERS[name](args, env, M), False)
    except Refused as e:
        return _text(mid, str(e), True)
    except msgdir.MsgError as e:                 # the layout refused a write: say so
        return _text(mid, "could not record that (%s); keep working" % e, True)


def handle(msg, env=None):
    env = os.environ if env is None else env
    if not isinstance(msg, dict):
        return _reply(None, error={"code": -32600, "message": "invalid request"})
    mid, method = msg.get("id"), msg.get("method")
    if mid is None or method is None:
        return None                              # a notification: no answer
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _reply(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": SERVER_NAME, "version": "1"}})
    if method == "ping":
        return _reply(mid, {})
    if method == "tools/list":
        return _reply(mid, {"tools": [TOOL_BY_NAME[n] for n in enabled_tools(env)]})
    if method == "tools/call":
        return _call(mid, params, env)
    return _reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def serve(stdin, stdout, env=None):
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
                reply = handle(msg, env)
            except Exception:
                mid = msg.get("id") if isinstance(msg, dict) else None
                reply = _reply(mid, error={"code": -32603, "message": "internal error"})
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main():
    for s, kw in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                  (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"}),
                  (sys.stderr, {"encoding": "utf-8"})):
        if hasattr(s, "reconfigure"):
            s.reconfigure(**kw)
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
