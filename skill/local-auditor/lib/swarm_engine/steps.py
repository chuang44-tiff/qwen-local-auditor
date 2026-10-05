"""Built-in mechanical steps for swarm workflows: the work an LLM loses track of.

Pure functions (no agent, no clock, no environment): URL normalisation and dedup, claim
dedup and caps, vote slots and tallies, JSON extraction, and the text helpers the built-in
workflows' parsers share. Moved verbatim out of research.py when it became a shim; a workflow reaches
them as wf.steps.NAME or `from lib.swarm_engine import steps`. Call them as steps.NAME(...) --
never `from lib.swarm_engine.steps import NAME` -- so a patched module attribute is the one used.
"""
import re
import urllib.parse

from lib import swarm

_DROP_PARAMS = ("fbclid", "gclid", "ref")
_SCORE_WORDS = {"low": 2, "medium": 3, "high": 4}
_FRACTION = re.compile(r"^(\d+(?:\.\d+)?)\s*/\s*5$")
_ESCAPES = re.compile(r"%([0-9a-fA-F]{2})")
_URL_WS = re.compile(r"\s")
_MAX_URL = 2000

extract_json = swarm.extract_json     # the last ```json block of a text, parsed; ValueError
tally = swarm.tally                   # majority of the votes requested; a missing vote is unclear


def slug(question):
    s = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    return s[:40].strip("-") or "question"


def _text(v):
    return v if isinstance(v, str) else ""


def _clip(v, n=300):
    return " ".join(_text(v).split())[:n]


def _clean_url(v):
    """A url is only ever whitespace-stripped, never clipped. "" for a url that is not
    a str, holds whitespace inside or is longer than _MAX_URL characters."""
    u = _text(v).strip()
    return "" if len(u) > _MAX_URL or _URL_WS.search(u) else u


def normalize_url(u):
    p = urllib.parse.urlsplit(u.strip())       # ValueError on a malformed url: the caller drops it
    scheme = p.scheme.lower()
    host = p.hostname
    if host is None:
        netloc = p.netloc.lower()
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = host.lower()
        netloc = host
        try:
            port = p.port
        except ValueError:
            port = None
        if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
            netloc = "%s:%d" % (netloc, port)
        if "@" in p.netloc:
            netloc = p.netloc[:p.netloc.rfind("@") + 1].lower() + netloc
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in _DROP_PARAMS]
    path = (p.path.rstrip("/") or "/") if p.path else "/"
    path = _ESCAPES.sub(lambda m: "%" + m.group(1).upper(), path)
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(query), ""))


def merge_urls(rows, cap):
    groups, order = {}, []
    for i, r in enumerate(rows):
        key = normalize_url(r["url"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"url": _clean_url(r["url"]), "title": _clip(r.get("title")),
                               "why": _clip(r.get("why")),
                               "relevance": r["relevance"], "angles": set(), "first": i}
            order.append(key)
        g["relevance"] = max(g["relevance"], r["relevance"])
        g["angles"].add(r["angle"])
    ranked = sorted((groups[k] for k in order), key=lambda g: (-g["relevance"], -len(g["angles"]), g["first"]))
    out = []
    for n, g in enumerate(ranked[:cap], 1):
        out.append({"id": "S%d" % n, "url": g["url"], "title": g["title"], "why": g["why"],
                    "relevance": g["relevance"], "angles": sorted(g["angles"])})
    return out


def merge_claims(rows, cap):
    best, order = {}, []
    for si, r in enumerate(rows):
        for ci, c in enumerate(r.get("claims") or []):
            if not isinstance(c, dict) or not isinstance(c.get("claim"), str) \
                    or not isinstance(c.get("snippet"), str):
                continue
            text = " ".join(c["claim"].split())
            snip = " ".join(c["snippet"].split())
            if not text or not snip:
                continue
            imp = _score(c.get("importance"))
            key = text.lower()
            cand = {"claim": _clip(text, 500), "snippet": _clip(snip, 500),
                    "importance": 3 if imp is None else imp,
                    "source": r["source"], "url": _clean_url(r.get("url", "")), "_rank": (si, ci)}
            if key not in best:
                order.append(key)
                best[key] = cand
            elif cand["importance"] > best[key]["importance"]:
                best[key] = cand
    ranked = sorted((best[k] for k in order), key=lambda c: (-c["importance"], c["_rank"]))
    out = []
    for n, c in enumerate(ranked[:cap], 1):
        c = dict(c, id="C%d" % n)
        del c["_rank"]
        out.append(c)
    return out


def _score(v):
    """1..5 from an int, float, numeric string, "n/5" or low/medium/high; else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    if isinstance(v, str):
        s = v.strip()
        m = _FRACTION.match(s)
        if m:
            s = m.group(1)
        try:
            v = float(s)
        except ValueError:
            return _SCORE_WORDS.get(s.lower())
    try:
        return min(5, max(1, int(round(v))))
    except (OverflowError, ValueError):
        return None


def vote_slots(claims, voters):
    """Claim-major (claim, k) slots, k = 0..voters-1: dealt round-robin over at least
    `voters` agents, no agent ever holds two slots of one claim."""
    return [(c, v) for c in claims for v in range(voters)]


# public names for the text helpers the built-in workflows' parsers share
as_text, clip, clean_url, score = _text, _clip, _clean_url, _score
