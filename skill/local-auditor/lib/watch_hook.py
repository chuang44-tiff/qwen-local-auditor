"""The activity hook of the watch page: one line per tool call, INPUTS only.

Claude Code runs this for PreToolUse, PostToolUse, PostToolUseFailure and Stop of a run
the page knows about (qwen-agent.sh writes the settings file that names it and points
QWEN_WATCH_ACTIVITY at the file to append to). One JSON line lands there per event:

  {"t":<unix s>, "ev":"pre"|"post"|"stop", "tool":NAME|null, "target":TRIMMED|null,
   "ok":true|false, "ctx":TOKENS, "win":TOKENS}        (ctx and win only when known)

A failed call fires PostToolUseFailure, not PostToolUse: its payload is written as the
call's post -- that is what answers the `pre` line -- with `ok: false`, whatever its error
flags say. Without that event mapped, a failing call's pre would never get a post and the
page would read the tool as running forever.

`target` is a trimmed INPUT -- the start of a command, a path, a search pattern, a URL's
host, a query, a subagent type -- never what a tool answered with, and never a prompt, a
transcript line or model text: the page shows what the agent asked for, nothing else. The
transcript is opened for ONE number only (the token count of its last assistant message,
the four usage counters added up); its text is never copied anywhere.

BEST EFFORT by contract, like every watch component: no QWEN_WATCH_ACTIVITY, a file that
cannot be written, junk on stdin or an unreadable transcript costs the run nothing. The
hook prints nothing and always exits 0 -- a watcher that breaks a session is a watcher
someone uninstalls, and a session must never fail because its page is broken.
"""
import json
import os
import sys
import time
from urllib.parse import urlsplit

MAX_LINE = 1024                     # bytes; a fold reader must never meet a monster line
TAIL_BYTES = 256 * 1024             # the transcript's tail the ctx scan reads
CMD_MAX = 80                        # characters of a Bash command kept
PATTERN_MAX = 60                    # characters of a Grep/Glob pattern or a search query
MCP_MAX = 60                        # characters of an mcp__ tool's own name, its target
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "output_tokens")
EVENTS = {"PreToolUse": "pre", "PostToolUse": "post",
          "PostToolUseFailure": "post", "Stop": "stop", "SubagentStop": "stop"}
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")


def _text(value):
    """The payload's strings, or None: a tool_input that is not a dict, a pattern that
    is a number, a path that is null -- all read as no target rather than as a crash."""
    return value if isinstance(value, str) and value else None


def _one_line(value, limit):
    """A command or a query for one line: newlines folded to spaces, capped at limit."""
    return value.replace("\r", " ").replace("\n", " ")[:limit]


def _relative(path, cwd):
    """The path as the page should name it: inside the session's directory it comes back
    relative to it, anywhere else (and for any path outside it) it stays as it was."""
    if not cwd or not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:                            # another drive's absolute path
        return path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return path                               # escapes the cwd: say where it really is
    return rel


def target_for(tool, inputs, cwd):
    """The trimmed INPUT of one tool call, by tool: what the agent asked for, chosen per
    tool kind and capped. A tool whose name starts with mcp__ has no input shape this
    hook knows, so its target is the tool NAME, capped: which MCP tool is running is
    the whole news. Anything else without an input worth naming -- a tool this hook
    does not know -- gets None, and so does a call whose input is missing."""
    if isinstance(tool, str) and tool.startswith("mcp__"):
        return tool[:MCP_MAX]
    if not isinstance(inputs, dict):
        return None
    if tool == "Bash":
        cmd = _text(inputs.get("command"))
        return _one_line(cmd, CMD_MAX) if cmd else None
    if tool in FILE_TOOLS:
        path = _text(inputs.get("file_path")) or _text(inputs.get("notebook_path"))
        return _relative(path, cwd) if path else None
    if tool in ("Grep", "Glob"):
        pattern = _text(inputs.get("pattern"))
        return pattern[:PATTERN_MAX] if pattern else None
    if tool == "WebFetch":
        url = _text(inputs.get("url"))
        if not url:
            return None
        try:
            host = urlsplit(url).hostname
        except ValueError:                         # a url so malformed urllib refuses it
            return None
        return host or None
    if tool == "WebSearch":
        query = _text(inputs.get("query"))
        return query[:PATTERN_MAX] if query else None
    if tool in ("Task", "Agent"):
        return _text(inputs.get("subagent_type"))
    return None


def ctx_from_transcript(path):
    """The context the session is using, from the newest assistant record of its
    transcript that HAS a usage dict: the four usage counters added up, or None when
    there is no such number to read -- an assistant record without a usage dict is
    passed over and the older ones keep being searched. Only the tail of the file is
    taken (a session's transcript grows without bound and the newest message is all
    that is wanted), and only its integers are kept: the message text is never
    returned, kept or copied."""
    if not isinstance(path, str) or not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
            data = fh.read()
    except OSError:
        return None
    for raw in reversed(data.decode("utf-8", "replace").splitlines()):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:                 # the tail begins mid-line: that line is junk
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        total, known = 0, False
        for key in USAGE_KEYS:
            n = usage.get(key)
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                total += n
                known = True
        return total if known else None
    return None


def _window():
    """The model's context window, as the launcher that sized it exported it. Not a
    number -- not set, not the served model's -- means the page shows no meter."""
    try:
        win = int(os.environ.get("QWEN_WATCH_CTX_WINDOW", ""))
    except ValueError:
        return None
    return win if win > 0 else None


def _fit(rec):
    """The record as one line, under MAX_LINE bytes: `target` is the only field that can
    grow, so it gives first, one character at a time. None when even an empty target
    cannot fit (a tool name long enough to do that is not a tool call worth a line)."""
    while True:
        line = json.dumps(rec, separators=(",", ":"))
        if len(line.encode("utf-8")) <= MAX_LINE:
            return line
        target = rec.get("target")
        if not isinstance(target, str) or not target:
            return None
        rec["target"] = target[:-1]


def line_for(payload, now, win):
    """The one line this payload is worth, or None when it is worth none: an event the
    page does not track, a payload that is not the object Claude Code sends. `ok` comes
    from the error FLAGS of a PostToolUse payload and from nothing else -- a tool whose
    answer mentions an error is not an error. A PostToolUseFailure payload is the one
    exception the event itself decides: a call Claude Code reports as failed gets
    `ok: false` whatever its flags say (and it is still that call's post -- the line
    that answers its `pre`)."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    ev = EVENTS.get(event)
    if ev is None:
        return None
    tool = _text(payload.get("tool_name"))
    inputs = payload.get("tool_input")
    target = target_for(tool, inputs, _text(payload.get("cwd"))) if ev != "stop" else None
    ok = True
    if ev == "post":
        if event == "PostToolUseFailure":
            ok = False                               # failed is failed, flags or no flags
        else:
            flag = payload.get("is_error")
            if flag is None:
                flag = payload.get("error")
            if flag is None:
                response = payload.get("tool_response")
                if isinstance(response, dict):
                    flag = response.get("is_error")   # the flag only, never the content
            ok = not bool(flag)
    rec = {"t": now, "ev": ev, "tool": tool, "target": target, "ok": ok}
    ctx = ctx_from_transcript(payload.get("transcript_path"))
    if ctx is not None:
        rec["ctx"] = ctx
    if win is not None:
        rec["win"] = win
    return _fit(rec)


def main():
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        return 0
    path = os.environ.get("QWEN_WATCH_ACTIVITY")
    if not path:
        return 0
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return 0                                    # not the JSON Claude Code sends: gone
    try:
        line = line_for(payload, time.time(), _window())
        if line is None:
            return 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        return 0                                    # unwritable file, a broken fold: no
    return 0                                        # matter what, this hook exits 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
