"""The decision log: every deliberate departure from the spec, with its reason.

The coder emits DEVIATION blocks in its reply; the SUPERVISOR parses and stores
them (the model never writes the log file), so an entry always carries the
session and commit it came from.
"""
import json
import os
import re
import time

_HEAD = re.compile(r"^##\s*DEVIATION\s*:?\s*$", re.M | re.I)
_FIELD = re.compile(r"^[ \t>*_-]*\**(SPEC|DID|WHY|EVIDENCE)\**:[ \t]*\**[ \t]*(.*?)[ \t\r]*$", re.M | re.I)


def parse(text):
    heads = list(_HEAD.finditer(text or ""))
    out = []
    for i, m in enumerate(heads):
        seg = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        seg = re.split(r"^##\s", seg.replace("```", ""), maxsplit=1, flags=re.M)[0]
        f = {k.lower(): v for k, v in _FIELD.findall(seg)}
        if f.get("spec") and f.get("did"):
            out.append({"spec": f["spec"], "did": f["did"],
                        "why": f.get("why", ""), "evidence": f.get("evidence", "")})
    return out


def load(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for l in fh:
            if l.strip():
                try:
                    out.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
    return out


def append(path, entries, *, session, commit):
    loaded = load(path)
    start = max((e["n"] for e in loaded), default=0)
    stored = []
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        # Any non-empty file not ending in a newline, parsed entries or not: a log
        # whose only content is a half-written line would otherwise absorb the first
        # record appended to it and lose both.
        if os.path.getsize(path) > 0:
            with open(path, "rb") as check:
                check.seek(-1, 2)
                if check.read(1) != b"\n":
                    fh.write("\n")
        for k, e in enumerate(entries, start + 1):
            rec = dict(e, n=k, session=session or "", commit=commit or "",
                       time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            fh.write(json.dumps(rec) + "\n")
            stored.append(rec)
    return stored


def render(entries):
    if not entries:
        return "(none recorded)"
    return "\n\n".join("D%d\nSPEC: %s\nDID: %s\nWHY: %s\nEVIDENCE: %s"
                       % (e["n"], e["spec"], e["did"], e.get("why", ""), e.get("evidence", ""))
                       for e in entries)
