"""history: an item is one file of interest; its evidence is what the CURRENT
project's Claude Code transcripts recorded about it.

Only <claude dir>/projects/<this project>/*.jsonl is read, and a file whose real
path leaves that directory is refused. The local model never sees raw
transcripts -- only the extracted decision moments, each with a session and
timestamp header so it can be cited.
"""
import json
import os
import re

from .base import Built, item_budget

DEFAULT_BRIEF = "history"
REQUIRED_ARGS = {"files": "--arg files=PATH[,PATH...] (relative to --repo)"}
_REASON = re.compile(r"\b(because|since|so that|instead|deviat|changed|chose|decid)", re.I)
_TESTISH = re.compile(r"\b(qwen-test|pytest|npm test|cargo test|go test|make test|tox)\b")
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def transcript_dir(repo):
    override = os.environ.get("QWEN_TRANSCRIPT_DIR")
    if override:
        return override
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    # realpath, not abspath: a symlinked checkout is the same project as its real path.
    return os.path.join(root, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(repo)))


def sessions(d):
    if not os.path.isdir(d):
        return []
    real = os.path.realpath(d)
    out = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name.endswith(".jsonl") and os.path.dirname(os.path.realpath(p)) == real:
            out.append(p)
    return out


def _flat(s, n):
    """One line, CR/LF and runs of whitespace collapsed, capped at n chars."""
    return " ".join(str(s).split())[:n]


def _blocks(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _text_of(block):
    c = block.get("content") if block.get("type") == "tool_result" else block.get("text")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _mentions(base, text):
    """Does the text name this file? A whole name only, never a substring: the
    character before must not extend the name ("internet.py" is not "net.py") and
    neither must the character after ("net.py2" and "net.py.bak" are not "net.py"). A
    path prefix ("src/net.py") and punctuation ("`net.py`", "net.py:", a sentence-ending
    "net.py.") still count."""
    return re.search(r"(?<![A-Za-z0-9_.-])%s(?![A-Za-z0-9_])(?!\.[A-Za-z0-9_])" % re.escape(base),
                     text)


def _rows(path):
    """Parsed JSONL objects; corrupt or truncated lines are skipped."""
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


def events_for(path_rel, transcripts):
    rel = path_rel.replace("\\", "/")
    base = os.path.basename(rel)
    out = []
    for t in transcripts:
        pending, touched, buf = {}, False, []
        for row in _rows(t):
            sid = str(row.get("sessionId") or "")[:8]
            tag = "[session:%s %s] " % (sid, row.get("timestamp", "?"))
            for b in _blocks(row.get("message")):
                kind = b.get("type")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if row.get("type") == "user" and kind == "text" and _mentions(base, _text_of(b)):
                    buf.append(tag + "USER: " + _flat(_text_of(b), 400))
                elif kind == "tool_use" and b.get("name") in _EDIT_TOOLS:
                    fp = str(inp.get("file_path", "")).replace("\\", "/")
                    if fp.endswith("/" + rel) or fp == rel:
                        touched = True
                        snippet = "%s -> %s" % (inp.get("old_string", ""), inp.get("new_string", ""))
                        buf.append(tag + "EDIT %s: %s %s" % (b["name"], rel, _flat(snippet, 300)))
                elif kind == "tool_use" and b.get("name") == "Bash" and \
                        _TESTISH.search(str(inp.get("command", ""))):
                    pending[b.get("id")] = (tag, _flat(inp.get("command", ""), 200))
                elif kind == "tool_result" and b.get("tool_use_id") in pending:
                    ptag, cmd = pending.pop(b["tool_use_id"])
                    res = _text_of(b).strip().splitlines()
                    buf.append(ptag + "TEST RUN: `%s` -> %s" % (cmd, _flat(res[0] if res else "", 300)))
                elif row.get("type") == "assistant" and kind == "text":
                    txt = _text_of(b)
                    if _mentions(base, txt) and _REASON.search(txt):
                        buf.append(tag + "REASON: " + _flat(txt, 400))
        # a session counts only if it edited the file or a user/assistant line named it
        if touched or any(("] USER: " in e) or ("] REASON: " in e) for e in buf):
            out += buf
    return out


def enumerate_items(args):
    repo = args.get("repo", "")
    files = [f.strip() for f in str(args["files"]).split(",") if f.strip()]
    tdir = transcript_dir(repo)
    return [{"path": f, "repo": repo, "tdir": tdir} for f in files]


def build(item):
    label = item["path"]
    ts = sessions(item["tdir"])
    if not ts:
        return Built("", "", 0, "no transcript folder for this project (%s)" % item["tdir"], None, label)
    ev = events_for(label, ts)
    if not ev:
        return Built("", "", 0, "no transcript evidence mentions this file", None, label)
    body = "File of interest: %s\nWhat did past sessions decide about it, and why?" % label
    context = "\n".join(["# Transcript evidence (extracted, chronological)", ""] + ev + [""])
    budget = item_budget()
    if len(context) > budget:
        context = context[:budget] + "\n[TRUNCATED at item budget]\n"
    return Built(body, context, len(body) + len(context), None, None, label)
