"""The `search` tool for qwen-deep-research: a stdlib MCP server (stdio, JSON-RPC 2.0).

Claude Code's own WebSearch is a server-side tool that local servers reject, so the
research workers search through this server instead. Backends: a self-hosted SearXNG
(QWEN_SEARCH_URL, JSON output enabled) or the Brave Search API (QWEN_SEARCH_KEY).
The key is read from the environment only; it is never written to a file.

  search_mcp.py                 serve MCP on stdin/stdout
  search_mcp.py --query Q [--n N]   one search, JSON rows on stdout (exit 3 on failure)
"""
import argparse
import http.client
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
MAX_N = 20
MAX_BYTES = 2_000_000
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TOOL = {
    "name": "search",
    "description": "Search the web. Returns a JSON list of {title, url, snippet}. "
                   "Use several specific queries rather than one broad one.",
    "inputSchema": {"type": "object",
                    "properties": {"query": {"type": "string", "description": "the search query"},
                                   "n": {"type": "integer", "minimum": 1, "maximum": MAX_N,
                                         "description": "how many results (default 8)"}},
                    "required": ["query"]},
}


class SearchError(Exception):
    pass


def _checked_url(url):
    if not url.startswith(("http://", "https://")):
        raise SearchError("QWEN_SEARCH_URL must start with http:// or https://")
    return url


def _checked_key(key):
    # Anything outside printable ASCII would corrupt or be rejected by the header; the
    # message must not quote the key itself.
    if any(c < "\x21" or c > "\x7e" for c in key):
        raise SearchError("QWEN_SEARCH_KEY contains characters that cannot be sent in a header")
    return key


def backend_from_env(env=None):
    env = os.environ if env is None else env
    name = (env.get("QWEN_SEARCH_BACKEND") or "").strip().lower()
    url = (env.get("QWEN_SEARCH_URL") or "").strip().rstrip("/")
    key = (env.get("QWEN_SEARCH_KEY") or "").strip()
    if not name:
        name = "searxng" if url else ("brave" if key else "")
    if not name:
        raise SearchError("no search backend: set QWEN_SEARCH_URL (SearXNG) or QWEN_SEARCH_KEY (Brave)")
    if name == "searxng":
        if not url:
            raise SearchError("QWEN_SEARCH_BACKEND=searxng needs QWEN_SEARCH_URL (e.g. http://localhost:8888)")
        return ("searxng", _checked_url(url), "")
    if name == "brave":
        if not key:
            raise SearchError("QWEN_SEARCH_BACKEND=brave needs QWEN_SEARCH_KEY")
        _checked_key(key)
        return ("brave", _checked_url((env.get("QWEN_SEARCH_BRAVE_URL") or BRAVE_URL).rstrip("/")), key)
    raise SearchError("unknown QWEN_SEARCH_BACKEND %r (searxng|brave)" % name)


def _get(url, headers):
    h = {"Accept": "application/json", "User-Agent": "qwen-deep-research"}
    h.update(headers)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=TIMEOUT) as r:
            body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SearchError("search backend reply is larger than 2 MB")
        return json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        hint = " (SearXNG: add json to search.formats in settings.yml)" if e.code == 403 else ""
        raise SearchError("search backend returned HTTP %d%s" % (e.code, hint))
    except http.client.HTTPException as e:
        raise SearchError("search backend unreachable: %s" % type(e).__name__)
    except (urllib.error.URLError, OSError) as e:
        raise SearchError("search backend unreachable: %s" % getattr(e, "reason", e))
    except (ValueError, RecursionError):
        raise SearchError("search backend returned something that is not JSON")


def _clamped_n(n):
    """Only finite integer-like numbers count as n; anything else (str, bool, inf, nan, None) is 8."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return 8
    if isinstance(n, float):
        if not math.isfinite(n) or n != int(n):
            return 8
        n = int(n)
    return max(1, min(n, MAX_N))


def search(query, n=8, backend=None, env=None):
    query = (query or "").strip() if isinstance(query, str) else ""
    if not query:
        raise SearchError("empty query")
    n = _clamped_n(n)
    kind, base, key = backend or backend_from_env(env)
    try:
        if kind == "searxng":
            qs = urllib.parse.urlencode({"q": query, "format": "json"})
        else:
            qs = urllib.parse.urlencode({"q": query, "count": n})
    except UnicodeEncodeError:
        raise SearchError("query is not valid text")
    if kind == "searxng":
        data = _get(base + "/search?" + qs, {})
    else:
        data = _get(base + "?" + qs, {"X-Subscription-Token": key})
    if not isinstance(data, dict):                       # a bare list, a string, a number...
        raise SearchError("search backend returned an unexpected JSON shape")
    if kind == "searxng":
        items = data.get("results")
    else:
        web = data.get("web")
        items = web.get("results") if isinstance(web, dict) else None
    if not isinstance(items, list):
        items = []
    raw = [(r.get("title"), r.get("url"), r.get("content" if kind == "searxng" else "description"))
           for r in items if isinstance(r, dict)]
    rows = [{"title": t if isinstance(t, str) else "", "url": u,
             "snippet": s if isinstance(s, str) else ""} for t, u, s in raw
            if isinstance(u, str) and u.startswith(("http://", "https://"))]
    return rows[:n]


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def handle(msg, env=None):
    if not isinstance(msg, dict):
        return _reply(None, error={"code": -32600, "message": "invalid request"})
    mid, method = msg.get("id"), msg.get("method")
    if mid is None or method is None:
        return None                       # a notification or a client response: no reply
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _reply(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "qwen-search", "version": "1"}})
    if method == "ping":
        return _reply(mid, {})
    if method == "tools/list":
        return _reply(mid, {"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != "search":
            return _reply(mid, error={"code": -32602, "message": "unknown tool %r" % params.get("name")})
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        try:
            rows = search(args.get("query"), args.get("n", 8), env=env)
        except SearchError as e:
            return _reply(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        text = json.dumps(rows, ensure_ascii=False, indent=1) if rows else "no results"
        return _reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    return _reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def serve(stdin, stdout, env=None):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):   # RecursionError: absurdly deep nesting
            reply = _reply(None, error={"code": -32700, "message": "parse error"})
        else:
            try:
                reply = handle(msg, env)
            except Exception:
                # The server lives for the worker's whole session: one poisoned message must not
                # kill it. The text says nothing, because exception text could carry the key.
                mid = msg.get("id") if isinstance(msg, dict) else None
                reply = _reply(mid, error={"code": -32603, "message": "internal error"})
        if reply is not None:
            # ensure_ascii=True: a lone surrogate echoed in an error text must not fail the write.
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv=None):
    for s, kw in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                  (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"}),
                  (sys.stderr, {"encoding": "utf-8"})):
        if hasattr(s, "reconfigure"):
            s.reconfigure(**kw)
    ap = argparse.ArgumentParser(prog="search_mcp.py")
    ap.add_argument("--query")
    ap.add_argument("--n", type=int, default=8)
    o = ap.parse_args(argv)
    if o.query is None:
        serve(sys.stdin, sys.stdout)
        return 0
    try:
        print(json.dumps(search(o.query, o.n), ensure_ascii=False, indent=1))
    except SearchError as e:
        print("search: %s" % e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
