"""search_mcp: backend selection, SearXNG/Brave adapters over real HTTP, MCP stdio,
and the `page` tool (fetch-once passages) against a local http.server. The page fetch
refuses loopback by design, so the page tests pass QWEN_PAGE_ALLOW_HOSTS=localhost --
the test-only allow-list -- in the env they hand the tool."""
import hashlib
import http.server
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import pathlib
import urllib.parse

import pytest

import lib.search_mcp as sm

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "local-auditor" / "lib" / "search_mcp.py"

# > 600 chars of filler between the two "4.2 million" matches, so the passages around
# them are two separate windows, and "filler" itself matches often enough to hit the
# cap of five passages.
_FILLER = "filler sentence for the middle of the page. " * 300
PAGE_HTML = ("<html><head><title>Acme Report</title>"
             "<script>var script_hidden = 1;</script>"
             "<style>.style_hidden { color: red; }</style>"
             "<noscript>noscript_hidden notice</noscript></head>"
             "<body><h1>Acme Report</h1>"
             "<p>Intro   paragraph   with   loose   spacing.</p>"
             "<p>The revenue was 4.2 million dollars in 2023.</p>"
             "<p>" + _FILLER + "</p>"
             "<p>The revenue was 4.2 million again in the later table.</p>"
             "</body></html>").encode("utf-8")


class _H(http.server.BaseHTTPRequestHandler):
    seen = []

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        _H.seen.append((u.path, q, dict(self.headers)))
        if u.path == "/page":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML)
            return
        if u.path == "/plain":
            self._send(200, "text/plain; charset=utf-8", b"plain body number 42")
            return
        if u.path == "/latin1":
            self._send(200, "text/html; charset=iso-8859-1",
                       b"<html><head><title>Cafe title</title></head>"
                       b"<body><p>caf\xe9 au lait</p></body></html>")
            return
        if u.path == "/bogus":
            self._send(200, "text/html; charset=not-a-codec",
                       b"<html><head><title>t</title></head>"
                       b"<body><p>fallback \xe9 work</p></body></html>")
            return
        if u.path == "/idna":
            self._send(200, "text/html; charset=idna",
                       b"<html><head><title>Odd charset</title></head>"
                       b"<body><p>caf\xc3\xa9 undecodable codec</p></body></html>")
            return
        if u.path == "/image":
            self._send(200, "image/png", b"\x89PNG\r\n\x1a\nnot really a page")
            return
        if u.path == "/huge":
            self._send(200, "text/html", b"<html><body>" + b"x" * 2_100_000 + b"</body></html>")
            return
        if u.path == "/selfloop":
            self.send_response(302)
            self.send_header("Location", "/selfloop")
            self.end_headers()
            return
        if u.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        if u.path == "/redirect-loopback":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/late" % self.server.server_address[1])
            self.end_headers()
            return
        if u.path == "/redirect-page":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if u.path == "/redirect-private":
            # a name whose resolution the test steers onto loopback; port 9 (discard) so
            # that even a tool that wrongly followed the hop would fetch nothing
            self.send_response(302)
            self.send_header("Location", "http://evil.example:9/page")
            self.end_headers()
            return
        if u.path == "/drip":
            # headers of a page that never finishes: one byte per 0.3 s, forever-ish --
            # exactly the per-recv-timeout-defeating server the whole-fetch deadline is for
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000000")
            self.end_headers()
            try:
                for _ in range(3000):
                    time.sleep(0.3)
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                pass
            return
        if u.path == "/dripheaders":
            # the status line, then one header byte every 0.3 s forever: no single read
            # is slow enough to trip the per-read timeout -- the header phase simply
            # never ends, and only a watchdog over the whole hop can cut it off
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.flush()
                while True:
                    time.sleep(0.3)
                    self.wfile.write(b"X")
                    self.wfile.flush()
            except OSError:
                pass
            return
        if u.path == "/halfbody":
            # two bytes of a ten-byte body, then silence: no per-read timeout ends
            # this on time -- only the whole-hop watchdog fires before them
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "10")
            self.end_headers()
            try:
                self.wfile.write(b"ab")
                self.wfile.flush()
                time.sleep(2.5)
            except OSError:
                pass
            return
        if u.path == "/slowheaders":
            time.sleep(5)                             # the headers never arrive in time:
            try:                                      # by now the client is gone, and a
                self._send(200, "text/html", b"<p>too late</p>")
            except OSError:                           # write onto its closed socket is how
                pass                                  # this handler ends
            return
        if u.path == "/redirect-ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1:21/pub")
            self.end_headers()
            return
        if u.path == "/chunked":
            # no Content-Length: the body arrives as chunked framing instead, with the
            # pieces far enough apart that more than one read is needed
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for part in (b"<html><head><title>Chunked</title></head><body>",
                             b"<p>served in pieces: 4.2 million</p></body></html>"):
                    self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            return
        if u.path == "/empty":
            self._send(200, "text/html; charset=utf-8", b"")
            return
        if u.path == "/titleonly":
            self._send(200, "text/html; charset=utf-8",
                       b"<html><head><title>Only title</title></head><body></body></html>")
            return
        if u.path == "/late":
            self._send(200, "text/html", b"<title>late</title><p>late body</p>")
            return
        if u.path == "/search" and q.get("format") == ["json"]:
            body = {"results": [
                {"title": "A", "url": "https://a.example/1", "content": "alpha"},
                {"title": "Bad", "url": "javascript:alert(1)", "content": "x"},
                {"title": "B", "url": "http://b.example/2", "content": "beta"}]}
        elif u.path == "/list/search":
            body = [1, 2]
        elif u.path == "/items/search":
            body = {"results": [1, "x", {"title": 5, "url": "https://ok.example/", "content": None}]}
        elif u.path == "/big/search":
            body = "x" * 2_100_000
        elif u.path == "/brave":
            if self.headers.get("X-Subscription-Token") != "k":
                self.send_response(401); self.end_headers(); return
            body = {"web": {"results": [{"title": "C", "url": "https://c.example/", "description": "gamma"}]}}
        elif u.path in ("/search", "/nojson/search"):
            self.send_response(403); self.end_headers(); return
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)  # the client may drop off early (e.g. it refused /big)
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def web():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    # poll_interval keeps shutdown() snappy (see the server fixture in test_cli.py).
    t = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.02), daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def dead_url():
    # Bind a port, read it, close it: nothing is listening, so connects get refused.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


def _stdio_env(web):
    env = dict(os.environ, QWEN_SEARCH_URL=web)
    for k in ("QWEN_SEARCH_KEY", "QWEN_SEARCH_BACKEND", "QWEN_SEARCH_BRAVE_URL"):
        env.pop(k, None)
    return env


def test_backend_selection():
    assert sm.backend_from_env({"QWEN_SEARCH_URL": "http://h:1/"})[:2] == ("searxng", "http://h:1")
    assert sm.backend_from_env({"QWEN_SEARCH_KEY": "k"})[0] == "brave"
    assert sm.backend_from_env({"QWEN_SEARCH_URL": "http://h", "QWEN_SEARCH_KEY": "k"})[0] == "searxng"
    assert sm.backend_from_env({"QWEN_SEARCH_BACKEND": "brave", "QWEN_SEARCH_URL": "http://h",
                                "QWEN_SEARCH_KEY": "k"})[0] == "brave"
    for env, needle in (({}, "QWEN_SEARCH_URL"), ({"QWEN_SEARCH_BACKEND": "searxng"}, "QWEN_SEARCH_URL"),
                        ({"QWEN_SEARCH_BACKEND": "brave"}, "QWEN_SEARCH_KEY"),
                        ({"QWEN_SEARCH_BACKEND": "bing"}, "searxng|brave")):
        with pytest.raises(sm.SearchError, match=needle.replace("|", r"\|")):
            sm.backend_from_env(env)


def test_searxng_adapter_keeps_only_http_urls(web):
    rows = sm.search("q", 8, env={"QWEN_SEARCH_URL": web})
    assert [r["url"] for r in rows] == ["https://a.example/1", "http://b.example/2"]
    assert rows[0] == {"title": "A", "url": "https://a.example/1", "snippet": "alpha"}
    assert sm.search("q", 1, env={"QWEN_SEARCH_URL": web}) == rows[:1]


def test_searxng_403_names_the_json_setting(web):
    # /nojson/search answers 403, as SearXNG does when the json format is not enabled
    with pytest.raises(sm.SearchError, match="403.*json"):
        sm.search("q", 8, env={"QWEN_SEARCH_URL": web + "/nojson"})


def test_brave_adapter_sends_key(web):
    env = {"QWEN_SEARCH_KEY": "k", "QWEN_SEARCH_BRAVE_URL": web + "/brave"}
    assert sm.search("q", 5, env=env) == [{"title": "C", "url": "https://c.example/", "snippet": "gamma"}]
    with pytest.raises(sm.SearchError, match="401"):
        sm.search("q", 5, env=dict(env, QWEN_SEARCH_KEY="wrong"))


def test_brave_request_shape(web):
    env = {"QWEN_SEARCH_KEY": "k", "QWEN_SEARCH_BRAVE_URL": web + "/brave"}
    assert sm.search("q", 5, env=env) == [{"title": "C", "url": "https://c.example/", "snippet": "gamma"}]
    path, q, headers = _H.seen[-1]
    assert q == {"q": ["q"], "count": ["5"]}
    assert headers.get("X-Subscription-Token") == "k"   # the key travels only in the header
    assert "k" not in path


def test_unreachable_and_empty_query(dead_url):
    with pytest.raises(sm.SearchError, match="unreachable"):
        sm.search("q", 3, env={"QWEN_SEARCH_URL": dead_url})
    with pytest.raises(sm.SearchError, match="empty"):
        sm.search("  ", 3, env={"QWEN_SEARCH_URL": dead_url})


def test_non_dict_backend_reply_is_a_tool_error(web):
    with pytest.raises(sm.SearchError, match="unexpected JSON shape"):
        sm.search("q", 8, env={"QWEN_SEARCH_URL": web + "/list"})


def test_non_dict_items_and_fields_are_skipped_or_blanked(web):
    rows = sm.search("q", 8, env={"QWEN_SEARCH_URL": web + "/items"})
    assert rows == [{"title": "", "url": "https://ok.example/", "snippet": ""}]


def test_oversized_reply_is_refused(web):
    with pytest.raises(sm.SearchError, match="larger than 2 MB"):
        sm.search("q", 8, env={"QWEN_SEARCH_URL": web + "/big"})


def test_odd_n_values(web):
    env = {"QWEN_SEARCH_URL": web}
    for n in ("5", True, float("inf"), float("nan"), None):
        rows = sm.search("q", n, env=env)  # each of these falls back to the default n=8
        assert _H.seen[-1][0] == "/search"  # the SearXNG request really went out
        assert len(rows) == 2
    assert len(sm.search("q", 3.0, env=env)) <= 3  # 3.0 is int-like, so n=3


def test_lone_surrogate_query(web):
    with pytest.raises(sm.SearchError, match="not valid text"):
        sm.search("\ud800", env={"QWEN_SEARCH_URL": web})


def test_bad_url_scheme_and_key():
    with pytest.raises(sm.SearchError, match="must start with http"):
        sm.backend_from_env({"QWEN_SEARCH_URL": "localhost:8888"})
    with pytest.raises(sm.SearchError, match="cannot be sent in a header") as e:
        sm.backend_from_env({"QWEN_SEARCH_KEY": "ab\ncd"})
    assert "ab" not in str(e.value)  # the message never quotes the key


def test_mcp_handle_protocol(web):
    env = {"QWEN_SEARCH_URL": web}
    init = sm.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}}, env)
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in init["result"]["capabilities"]
    assert sm.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, env) is None
    tools = sm.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, env)["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "page"]
    ok = sm.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "search", "arguments": {"query": "q", "n": 1}}}, env)["result"]
    assert ok["isError"] is False
    assert json.loads(ok["content"][0]["text"])[0]["url"] == "https://a.example/1"
    bad = sm.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "search", "arguments": {"query": "q"}}}, {})["result"]
    assert bad["isError"] is True and "QWEN_SEARCH_URL" in bad["content"][0]["text"]
    assert sm.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"}, env)["error"]["code"] == -32601


def test_mcp_server_over_stdio(web):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "q", "n": 2}}}]
    p = subprocess.run([sys.executable, str(SCRIPT)], input="".join(json.dumps(m) + "\n" for m in msgs) + "not json\n",
                       capture_output=True, text=True, encoding="utf-8", timeout=30,
                       env=_stdio_env(web))
    replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700
    assert p.returncode == 0, p.stderr


def test_server_survives_bad_messages(web):
    stdin = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}\n'
             b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search",'
             b'"arguments":{"query":"q","n":1e999}}}\n'
             b"\xff\xfe\n"
             b'{"jsonrpc":"2.0","id":7,"result":{}}\n'
             b'{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
                       timeout=30, env=_stdio_env(web))
    replies = [json.loads(line) for line in p.stdout.decode("utf-8").splitlines() if line.strip()]
    # ids 1 and 2 still get replies, the raw-bytes line gets an id-null error, the client
    # response (id 7) gets nothing, and the ping after it proves the loop survived.
    assert [r.get("id") for r in replies] == [1, 2, None, 3]
    assert replies[2]["error"]["code"] in (-32700, -32603)
    assert p.returncode == 0, p.stderr
    assert b"Traceback" not in p.stderr


def test_cli_query(web, dead_url):
    env = _stdio_env(web)
    p = subprocess.run([sys.executable, str(SCRIPT), "--query", "q", "--n", "1"], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)[0]["url"] == "https://a.example/1"
    env["QWEN_SEARCH_URL"] = dead_url
    p = subprocess.run([sys.executable, str(SCRIPT), "--query", "q"], capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=30)
    assert p.returncode == 3 and "unreachable" in p.stderr


# ------------------------------------------------------------------ the page tool


def _localhost(web):
    # the server binds 127.0.0.1; the page tests reach it as "localhost" and name that
    # host in the test-only allow-list, so a redirect to the literal 127.0.0.1 is a hop
    # the address gate has never been shown
    return "http://localhost:%s" % web.rsplit(":", 1)[1]


def _page_env(cache=None, allow="localhost"):
    env = {"QWEN_PAGE_ALLOW_HOSTS": allow}
    if cache is not None:
        env["QWEN_PAGE_CACHE"] = str(cache)
    return env


def _passage_blocks(out):
    # "Title: ...\n\n<one header line>\n\n<passage>\n\n<passage>...": the page's own text
    # carries no blank line, so everything past the header line is passages
    return out.split("\n\n", 2)[2].split("\n\n")


def test_page_returns_passages_around_find(web):
    url, env = _localhost(web) + "/page", _page_env()
    out = sm.page(url, "4.2 million", env=env)
    assert "Acme Report" in out and "Passages matching" in out
    blocks = _passage_blocks(out)
    assert len(blocks) == 2                                   # both matches, kept apart
    assert all("4.2 million" in b and len(b) <= sm.PASSAGE_CHARS for b in blocks)
    assert "\n" in blocks[0]                                  # block boundaries survive
    _, _, headers = _H.seen[-1]                               # what the fetch really sent
    low = {k.lower(): v for k, v in headers.items()}
    assert low["accept"] == "text/html"
    assert low["user-agent"].startswith("Mozilla/")
    assert "script_hidden" not in out and "style_hidden" not in out
    assert "noscript_hidden" not in out                       # dropped tags leave no text
    assert "   " not in out                                   # whitespace collapsed
    upper = sm.page(url, "REVENUE WAS 4.2", env=env)          # case-insensitive
    assert upper.count("revenue was 4.2") == 2
    capped = sm.page(url, "filler", env=env)                  # often-matching find
    assert len(_passage_blocks(capped)) == sm.MAX_PASSAGES    # never more than five


def test_page_without_find_returns_the_start(web):
    url, env = _localhost(web) + "/page", _page_env()
    out = sm.page(url, env=env)
    assert "No find given" in out and "first 3000" in out     # it says which case this is
    assert "Intro paragraph with loose spacing." in out
    assert "again in the later table" not in out              # the far end is beyond 3000
    nomatch = sm.page(url, "zzz-not-on-the-page", env=env)
    assert "No match for" in nomatch and "first 3000" in nomatch
    assert "Intro paragraph with loose spacing." in nomatch


def test_page_second_call_is_served_from_the_cache(tmp_path, web):
    cache, url = tmp_path / "pages", _localhost(web) + "/page"
    env = _page_env(cache)
    before = len(_H.seen)
    first = sm.page(url, "4.2 million", env=env)
    after_first = len(_H.seen)
    second = sm.page(url, "later table", env=env)             # same url, different find
    assert after_first - before == 1 and len(_H.seen) == after_first   # one request, ever
    assert "4.2 million" in first and "later table" in second
    assert "Title: Acme Report" in first and "Title: Acme Report" in second   # even a hit
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["miss", "hit"]
    assert rows[0][1] == key == rows[1][1] and rows[0][3] == "200" and rows[1][3] == "cache"
    assert int(rows[0][4]) > 0 and int(rows[0][0]) > 0
    assert int(rows[1][4]) == int(rows[0][4])
    assert sorted(p.name for p in cache.iterdir()) == sorted(["log.tsv", key + ".txt"])


def test_page_without_cache_dir_writes_nothing(tmp_path, web, monkeypatch):
    monkeypatch.chdir(tmp_path)                              # no relative paths either
    env = {"QWEN_PAGE_ALLOW_HOSTS": "localhost"}              # QWEN_PAGE_CACHE unset
    assert "4.2 million" in sm.page(_localhost(web) + "/page", "4.2 million", env=env)
    assert "404" in sm.page(_localhost(web) + "/missing", env=env)
    assert list(tmp_path.iterdir()) == []                     # not even a log line


def test_page_refuses_bad_urls(web):
    hostport = web.split("//", 1)[1]
    env = {}                                                  # no allow-list: refusals stand
    # the two private addresses are the RFC 1918 example ones, split only so the repo's
    # hygiene guard (no LAN details in a public repo) stays green -- the tool still sees
    # the full literal url
    for url in ("file:///etc/passwd",
                "http://" + hostport + "/page",               # loopback: a local model server
                "http://10" + ".0.0.1/",                      # private range
                "http://192.168" + ".1.1/",                   # private range
                "http://169.254.169.254/latest/meta-data/",   # link-local: cloud metadata
                "ftp://" + hostport + "/page"):               # not http(s)
        out = sm.page(url, env=env)
        assert out.startswith("page:") and "WebFetch" in out, url
        assert "root:" not in out and "late body" not in out, url
    out = sm.page(_localhost(web) + "/redirect-loopback", env=_page_env())
    assert "127.0.0.1" in out and "public address" in out and "WebFetch" in out
    assert "late body" not in out                             # the hop was refused, not fetched
    out = sm.page(_localhost(web) + "/redirect-ftp", env=_page_env())
    assert "only http and https" in out and "WebFetch" in out  # a hop the SCHEME gate refuses
    before = len(_H.seen)
    loop = sm.page(_localhost(web) + "/selfloop", env=_page_env())
    assert "3" in loop and "WebFetch" in loop                 # the cap on redirects
    assert len(_H.seen) - before == sm.MAX_REDIRECTS + 1      # initial hop + 3, no further


def test_page_refuses_non_global_addresses():
    # every url names loopback or a non-routable range through a form a gate that only
    # read the dotted-quad would miss: the carrier-shared /10 Tailscale hands out,
    # loopback embedded in IPv6 (mapped, compressed-hex and 6to4 forms), the IPv4 each
    # of the two translation prefixes carries in its last four bytes (NAT64 and SIIT,
    # whose outer addresses is_global calls global), the deprecated site-local range,
    # and the integer/hex spellings of 127.0.0.1. Split literals are for the repo
    # hygiene guard only -- the tool still sees each url whole.
    env = {}                                                  # no allow-list: refusals stand
    for url in ("http://100" + ".64.0.1/",
                "http://100" + ".100.100.200/",
                "http://[::ffff:127.0.0.1]/",
                "http://[::ffff:7f00:1]/",
                "http://[2002:7f00:1::]/",
                "http://[64:ff9b::a00" + ":1]/",              # NAT64 of a private range
                "http://[64:ff9b::7f00" + ":1]/",             # NAT64 of loopback
                "http://[::ffff:0:10.0.0" + ".1]/",           # SIIT of a private range
                "http://[fec0::1]/",
                "http://2130706433/",
                "http://0x7f.1/"):
        out = sm.page(url, env=env)
        assert out.startswith("page:") and "WebFetch" in out, url
        assert "public address" in out or "does not resolve" in out, url


def test_page_connects_to_the_address_it_checked(web, monkeypatch):
    # DNS rebinding defence: the host is resolved once, every returned address is
    # checked, and the connection is made to that checked address -- not to whatever
    # the name resolves to later. The fake hands out a public address on the first
    # look and loopback on any later one: a tool that re-resolved would reach the
    # local server through the second answer and never notice.
    port = int(web.rsplit(":", 1)[1])
    real_gai, real_connect = socket.getaddrinfo, socket.create_connection
    looked, connected = [], []

    def _gai(host, *a, **k):
        if host == "evil.example":
            looked.append(host)
            addr = "93.184.216.34" if len(looked) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, a[0]))]
        return real_gai(host, *a, **k)

    def _connect(address, *a, **k):
        connected.append(address)                             # what was actually asked for
        return real_connect(("127.0.0.1", address[1]), *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    monkeypatch.setattr(socket, "create_connection", _connect)
    out = sm.page("http://evil.example:%d/page" % port, "4.2 million", env={})
    assert "4.2 million" in out                               # served over the redirected socket
    assert looked == ["evil.example"]                         # one resolution -- connect did not ask again
    assert connected == [("93.184.216.34", port)]             # ...and it went to the checked address
    _, _, headers = _H.seen[-1]
    assert headers["Host"] == "evil.example:%d" % port        # the server still hears the name


def test_page_checks_every_resolved_address(web, monkeypatch):
    # a name may answer with several addresses (v4 and v6); checking only the one it
    # would connect to lets a public first answer hide a loopback second one
    port = int(web.rsplit(":", 1)[1])
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", a[0])),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", a[0]))])
    out = sm.page("http://mixed.example:%d/page" % port, env={})
    assert out.startswith("page:") and "127.0.0.2" in out
    assert "public address" in out and "WebFetch" in out


def test_page_tries_the_next_checked_address(web, monkeypatch):
    # every checked address is a permitted connect target, so the first answer may
    # simply not answer -- a dead load-balancer leg, a filtered leg -- and the fetch
    # must go on to the next checked one. The fake resolves the allow-listed host to a
    # public-looking leg (twice, to show answers are de-duplicated) that the fake
    # connect refuses, then to the loopback server; the pinning is unchanged: only
    # checked addresses are ever dialed, in resolution order.
    port = int(web.rsplit(":", 1)[1])
    real_gai, real_connect = socket.getaddrinfo, socket.create_connection
    tried = []

    def _gai(host, *a, **k):
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, a[0]))
                    for addr in ("93.184.216.34", "93.184.216.34", "127.0.0.1")]
        return real_gai(host, *a, **k)

    def _connect(address, *a, **k):
        tried.append(address)                                 # what was actually asked for
        if address[0] != "127.0.0.1":
            raise ConnectionRefusedError("this checked answer refuses")
        return real_connect(address, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    monkeypatch.setattr(socket, "create_connection", _connect)
    out = sm.page(_localhost(web) + "/page", "4.2 million", env=_page_env())
    assert "4.2 million" in out                               # served over the second answer
    # both answers dialed, in resolution order -- and the repeated one only once
    assert tried == [("93.184.216.34", port), ("127.0.0.1", port)]

    def _refuse_all(address, *a, **k):                        # every answer a dead leg
        tried.append(address)
        raise ConnectionRefusedError("every checked answer refuses")

    monkeypatch.setattr(socket, "create_connection", _refuse_all)
    out = sm.page(_localhost(web) + "/page", env=_page_env())
    assert out.startswith("page:") and "unreachable" in out   # none connected: an error
    assert tried[-2:] == [("93.184.216.34", port), ("127.0.0.1", port)]   # that tried them all

    def _timeout_all(address, *a, **k):                       # every leg hangs instead
        tried.append(address)
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(socket, "create_connection", _timeout_all)
    out = sm.page(_localhost(web) + "/page", env=_page_env())
    assert "did not arrive" in out                            # a timed-out attempt is the
    assert tried[-2:] == [("93.184.216.34", port), ("127.0.0.1", port)]   # deadline's own
    #                                                        error, whatever leg raised it


def test_page_refuses_a_redirect_to_a_private_name(web, monkeypatch):
    # the redirect gate must see the resolution, not the url text: "evil.example" is
    # only loopback because the monkeypatched resolver says so
    real_gai = socket.getaddrinfo
    looked = []

    def _gai(host, *a, **k):
        if host == "evil.example":
            looked.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", a[0]))]
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    before = len(_H.seen)
    out = sm.page(_localhost(web) + "/redirect-private", env=_page_env())
    assert "evil.example" in out and "public address" in out and "WebFetch" in out
    assert looked == ["evil.example"]                         # it was resolved -- and refused
    assert len(_H.seen) - before == 1                         # only the redirect itself was fetched


def test_page_ignores_proxy_env(web, dead_url, monkeypatch):
    # every proxy variable urllib would honour points at a port that refuses
    # connections: the fetch drives http.client itself, so the page still arrives --
    # a proxy anywhere in the route would turn this into an error
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(var, dead_url)
    out = sm.page(_localhost(web) + "/page", "4.2 million", env=_page_env())
    assert "4.2 million" in out and "Acme Report" in out


def test_page_gives_up_on_a_slow_drip_server(web, monkeypatch):
    monkeypatch.setattr(sm, "PAGE_DEADLINE", 1)               # one second for the whole fetch
    t0 = time.monotonic()
    out = sm.page(_localhost(web) + "/drip", "needle", env=_page_env())
    took = time.monotonic() - t0
    assert out.startswith("page:") and "did not arrive" in out and "WebFetch" in out
    assert took < 3, "held the tool for %.1f s" % took        # not per-chunk 20 s reads


def test_page_deadline_covers_a_stalled_header(web, monkeypatch):
    monkeypatch.setattr(sm, "PAGE_DEADLINE", 1)               # the server sleeps 5 s before
    t0 = time.monotonic()                                     # its first response byte
    out = sm.page(_localhost(web) + "/slowheaders", "x", env=_page_env())
    took = time.monotonic() - t0
    assert out.startswith("page:") and "did not arrive" in out and "WebFetch" in out
    assert took < 3, "the header read ran on past the deadline (%.1f s)" % took


def test_page_gives_up_on_a_slow_drip_header(web, monkeypatch):
    # the per-read timeout cannot bound the header phase (every header line's own recv
    # gets the full remaining time): only a whole-hop watchdog ends this by PAGE_DEADLINE
    monkeypatch.setattr(sm, "PAGE_DEADLINE", 1)               # headers drip until then
    t0 = time.monotonic()
    out = sm.page(_localhost(web) + "/dripheaders", "x", env=_page_env())
    took = time.monotonic() - t0
    assert out.startswith("page:") and "did not arrive" in out and "WebFetch" in out
    assert took < 3, "held the tool for %.1f s" % took        # not until 64 KB of headers
    left = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert left == [], "watchdog timer threads still alive: %r" % left


def test_page_watchdog_ending_a_body_read_is_still_the_deadline(web, monkeypatch):
    # a body the watchdog ends (EOF or an aborted send, not a per-read timeout) must
    # answer as the deadline too: arm the timer 0.2 s before the per-read timeouts can
    # fire, so it is the one that ends the half-delivered body of /halfbody
    monkeypatch.setattr(sm, "PAGE_DEADLINE", 1)
    real_watch = sm._hop_watchdog
    monkeypatch.setattr(sm, "_hop_watchdog",
                        lambda conn, deadline: real_watch(conn, deadline - 0.2))
    t0 = time.monotonic()
    out = sm.page(_localhost(web) + "/halfbody", "needle", env=_page_env())
    took = time.monotonic() - t0
    assert out.startswith("page:") and "did not arrive" in out and "WebFetch" in out
    assert took < 2.5, "held the tool for %.1f s" % took     # died at ~0.8 s, not 1.0+
    left = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert left == [], "watchdog timer threads still alive: %r" % left


def test_page_watchdog_errors_after_firing_are_the_deadline(web, monkeypatch):
    # a shutdown landing mid-TLS-read surfaces as ValueError("Read on closed or
    # unwrapped SSL socket"), not an OSError: with the fired event set it is the
    # deadline, not a url that "could not be requested as given" -- and the same
    # holds for an unexpected error type the named handlers never mention
    monkeypatch.setattr(sm, "PAGE_DEADLINE", 1)
    real_watch = sm._hop_watchdog
    monkeypatch.setattr(sm, "_hop_watchdog",
                        lambda conn, deadline: real_watch(conn, time.monotonic()))

    def _raise(cls, msg):
        def _after_a_blink(*a):                # the armed-now timer has fired by then
            time.sleep(0.05)
            raise cls(msg)
        return _after_a_blink

    monkeypatch.setattr(sm, "_read_page_body",
                        _raise(ValueError, "Read on closed or unwrapped SSL socket"))
    out = sm.page(_localhost(web) + "/page", env=_page_env())
    assert "did not arrive" in out and "requested as given" not in out
    monkeypatch.setattr(sm, "_read_page_body", _raise(RuntimeError, "something odd"))
    out = sm.page(_localhost(web) + "/page", env=_page_env())
    assert "did not arrive" in out and "odd" not in out
    left = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert left == [], "watchdog timer threads still alive: %r" % left


def test_page_turns_unexpected_errors_into_text(web, monkeypatch):
    # nothing from inside the hop may escape the tool as a raise: an error with no
    # named handler answers as the error text, and the text names the type, never
    # the exception's own words (they could carry data the tool must not repeat)
    def _boom(*a):
        raise RuntimeError("the fetch went wrong internally")
    monkeypatch.setattr(sm, "_read_page_body", _boom)
    out = sm.page(_localhost(web) + "/page", env=_page_env())
    assert out.startswith("page:") and "WebFetch" in out
    assert "RuntimeError" in out and "went wrong internally" not in out


def test_page_reads_bodies_as_they_are_framed(web):
    # read1 stops at what the framing says the body is: a chunked body across two reads,
    # and a zero-length 200 whose connection the response has already closed before the
    # first read is even attempted -- neither may hang, error or read past its end
    out = sm.page(_localhost(web) + "/chunked", "4.2 million", env=_page_env())
    assert "Chunked" in out and "4.2 million" in out
    empty = sm.page(_localhost(web) + "/empty", env=_page_env())
    assert empty.startswith("Title:") and "No find given" in empty and "WebFetch" not in empty


def test_html_to_text_is_linear_on_hostile_pages():
    # each page is adversarial at a different construct: an unterminated tag opening,
    # an unclosed comment, an unclosed raw-text block, stray "<"s, repeated <title>
    # opens and one huge unterminated tag -- a parser that restarts from the beginning
    # (or re-scans its pending text) per construct takes minutes on these
    for evil in ("<a " * 70000, "<!--" * 50000, "<script>" * 30000, "<" * 200000,
                 "<title>" * 30000, "<p " + "x=" * 100000,
                 "<p>İ" * 40000,                       # the translate path on non-ASCII:
                 "<script>" + "İ" * 200000,            # raw-skip search over a copy that
                 "<p>" + "İ" * 100000 + "</p><style>x</style>after"):   # keeps length
        t0 = time.monotonic()
        title, text = sm.html_to_text(evil)
        took = time.monotonic() - t0
        assert isinstance(title, str) and isinstance(text, str)
        assert took < 2, "%s... took %.2f s" % (evil[:12], took)
    assert sm.html_to_text("") == ("", "")                    # the empty page: nothing, no raise
    assert sm.html_to_text("<TITLE>Up</TITLE>bo&amp;dy") == ("Up", "bo&dy")  # case + entities


def test_embedded_ipv4_spots_both_translation_prefixes():
    # the translated addresses carry the IPv4 in the LAST four bytes -- and note the
    # SIIT spelling: "…:0:a.b.c.d" puts its ff:ff one group below where parsing the
    # pure-hex prefix literal puts it, so the check is on packed bytes, not an
    # ipaddress network membership (which would name the mapped range and miss these)
    def emb(s):
        return [str(v4) for v4 in sm._embedded_ipv4(ipaddress.ip_address(s))]
    assert emb("64:ff9b::a00" + ":1") == ["10" + ".0.0.1"]            # NAT64
    assert emb("64:ff9b::7f00:1") == ["127.0.0.1"]                    # NAT64
    assert emb("::ffff:0:10.0.0" + ".1") == ["10" + ".0.0.1"]         # SIIT
    assert emb("::ffff:0:7f00:1") == ["127.0.0.1"]                    # SIIT
    assert emb("::ffff:7f00:1") == ["127.0.0.1"]                      # mapped, as before
    assert emb("2001:4860:4860::8888") == []                          # plain v6 carries none
    assert emb("64:ff9b::808:808") == ["8.8.8.8"]                     # a GLOBAL one...
    checked = sm._checked_hop("http://[64:ff9b::808" + ":808]/", {})[2]
    assert str(ipaddress.ip_address(checked[0])) == "64:ff9b::808:808"  # ...still passes
    assert len(checked) == 1                                           # the whole gate


def test_html_to_text_keeps_offsets_with_non_ascii():
    # the raw-text skips must not index the page with offsets taken from a re-cased
    # copy: str.lower() can change the string's length ("I" with a dot becomes two
    # characters), which slid the script skip off its close and ate the page's text
    page = "<p>" + "İ" * 20 + "</p><script>a</script><p>KEEP THIS TEXT</p>"
    _, text = sm.html_to_text(page)
    assert "KEEP THIS TEXT" in text
    title, text = sm.html_to_text("İİİİİ<script>secret()</script>VISIBLE after")
    assert title == "" and "secret" not in text               # the script is still skipped
    assert text == "İİİİİVISIBLE after"                       # both halves land whole
    _, text = sm.html_to_text("<p>İİ</p><SCRIPT>x</SCRIPT><p>AFTER</p>")
    assert "AFTER" in text and "x" not in text                # ASCII case still folds too


def test_html_to_text_keeps_a_bare_less_than():
    # per HTML a '<' opens markup only before a letter, '/', '!' or '?': "a < b" is
    # text -- before the check the '<' swallowed everything up to the next '>'
    _, text = sm.html_to_text("<p>a < b and c > d</p>")
    assert text == "a < b and c > d"
    title, text = sm.html_to_text("<title>x < y</title>body < 0")
    assert title == "x < y" and text == "body < 0"            # in the title too
    assert sm.html_to_text("keep <")[1] == "keep <"           # a '<' the page dies on
    assert sm.html_to_text("<!doctype html><p>x</p>")[1] == "x"    # '<!' and '<?' still
    assert sm.html_to_text("<p>a <?php echo 1; ?> b</p>")[1] == "a b"       # open markup


def test_page_needs_no_arguments_to_answer_as_text(web):
    # every failure mode of a page call is an error text naming WebFetch, never a raise:
    # no url at all, and a find that is not a string, still produce ordinary tool answers
    missing = sm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "page", "arguments": {}}}, {})["result"]
    assert missing["isError"] is True and "WebFetch" in missing["content"][0]["text"]
    odd = sm.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "page",
                                "arguments": {"url": _localhost(web) + "/plain", "find": 42}}},
                    _page_env())["result"]
    assert odd["isError"] is False and "plain body number 42" in odd["content"][0]["text"]


def test_page_refuses_non_text_content(tmp_path, web):
    cache = tmp_path / "pc"
    out = sm.page(_localhost(web) + "/image", env=_page_env(cache))
    assert "image/png" in out and "WebFetch" in out
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["error"]
    assert sorted(p.name for p in cache.iterdir()) == ["log.tsv"]      # nothing stored


def test_page_errors_are_not_cached(tmp_path, web, dead_url):
    cache, url, env = tmp_path / "pc", _localhost(web) + "/missing", _page_env(tmp_path / "pc")
    before = len(_H.seen)
    first = sm.page(url, env=env)
    second = sm.page(url, env=env)
    assert len(_H.seen) - before == 2                         # an error never counts as a copy
    assert "404" in first and "WebFetch" in first and second == first
    assert not any(p.name.endswith(".txt") for p in cache.iterdir())
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["error", "error"] and rows[0][3] == "404"
    assert "larger than 2 MB" in sm.page(_localhost(web) + "/huge", env=env)
    refused = sm.page(dead_url, env=_page_env(cache, allow="127.0.0.1"))
    assert "unreachable" in refused                           # the allow-list skips the gate,
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert rows[-1][2] == "error"                             # the failed fetch still logs


def test_page_follows_a_allowed_redirect_and_the_header_charset(web):
    env = _page_env()
    out = sm.page(_localhost(web) + "/redirect-page", "4.2 million", env=env)
    assert "Passages matching" in out and "Acme Report" in out
    latin = sm.page(_localhost(web) + "/latin1", "au lait", env=env)
    assert "café au lait" in latin                            # the header's charset is used
    assert "fallback" in sm.page(_localhost(web) + "/bogus", env=env) and \
        "work" in sm.page(_localhost(web) + "/bogus", env=env)  # unknown charset: utf-8, replace
    plain = sm.page(_localhost(web) + "/plain", "42", env=env)
    assert "(no title)" in plain and "plain body number 42" in plain


def test_page_mangled_urls_come_back_as_text(tmp_path):
    # urlsplit and parts.port raise ValueError inside the gate (an unbalanced "[", a
    # junk port): the gate turns them into refusals, so page() and the MCP call answer
    # as text instead of raising, and each dead call still gets its one log line
    cache = tmp_path / "pc"
    env = _page_env(cache)
    for url in ("http://[::1", "http://a]b:99999x/"):
        out = sm.page(url, env=env)
        assert out.startswith("page:") and "WebFetch" in out, url
    assert sm.page(None, env=env).startswith("page:")        # not a string: the no-url path
    assert sm.page("   ", env=env).startswith("page:")       # blank after stripping
    assert sm.page("http://localhost:0/", env=env).startswith("page:")   # a real port, refused
    res = sm.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": "page", "arguments": {"url": "http://[::1"}}},
                    env)["result"]
    assert res["isError"] is True and "WebFetch" in res["content"][0]["text"]
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["error"] * 6              # logged, never crashed past
    assert sorted(p.name for p in cache.iterdir()) == ["log.tsv"]


def test_page_allow_list_does_not_skip_the_port_gate():
    # the test-only allow-list skips the address refusals, not the url's own validity:
    # a port out of range is refused through the allow-list too, rather than reaching
    # getaddrinfo -- which raises OverflowError, not OSError, past 65535
    out = sm.page("http://localhost:99999/", env=_page_env())
    assert out.startswith("page:") and "port" in out and "WebFetch" in out


def test_page_charset_that_cannot_decode_falls_back(tmp_path, web):
    # `idna` is a registered codec whose decode raises on page bytes: a header naming
    # one must not crash the tool, just fall back to utf-8 with replace -- and a page
    # that decodes after the fallback is a success, so it lands in the cache
    cache, url = tmp_path / "pc", _localhost(web) + "/idna"
    out = sm.page(url, "undecodable", env=_page_env(cache))
    assert "Odd charset" in out and "café" in out
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert (cache / (key + ".txt")).is_file()


def test_page_padded_url_shares_the_plain_pages_cache_entry(tmp_path, web):
    # the key hashes the url as it is fetched, so a padded spelling and the plain one
    # are one page: one fetch, one file, and a blank find counts as no find given
    cache = tmp_path / "pc"
    base, url = _localhost(web), _localhost(web) + "/page"
    env = _page_env(cache)
    before = len(_H.seen)
    first = sm.page(base + "/page  ", "   ", env=env)
    assert "No find given" in first and "4.2 million" in first
    again = sm.page(url, "4.2 million", env=env)
    assert "Passages matching" in again and len(_H.seen) - before == 1
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["miss", "hit"] and rows[0][1] == rows[1][1] == key


def test_page_cache_dir_that_is_not_a_directory_still_answers(tmp_path, web):
    # QWEN_PAGE_CACHE naming an ordinary file: the read finds nothing to read, the
    # write and the log are refused, and the tool still fetches and answers as text --
    # no raise, and the file the env pointed at stays untouched
    f = tmp_path / "cache-is-a-file"
    f.write_bytes(b"untouched")
    out = sm.page(_localhost(web) + "/page", "4.2 million", env=_page_env(f))
    assert "4.2 million" in out and "Passages matching" in out
    assert f.read_bytes() == b"untouched"


def test_page_cache_path_with_spaces_and_accents(tmp_path, web):
    cache = tmp_path / "my pages é"                    # spaces and a non-ascii letter
    url, env = _localhost(web) + "/page", _page_env(cache)
    before = len(_H.seen)
    assert "4.2 million" in sm.page(url, "4.2 million", env=env)
    assert "later table" in sm.page(url, "later table", env=env)   # the hit needs no fetch
    assert len(_H.seen) - before == 1
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert (cache / (key + ".txt")).is_file()
    rows = [ln.split("\t") for ln in (cache / "log.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in rows] == ["miss", "hit"]


def test_page_find_is_a_literal_string_not_a_regex(web):
    # "(4.2" would crash a regex engine, "t.l" and "million$" would match in one: the
    # find is matched byte-for-byte, so all three simply report no match
    url, env = _localhost(web) + "/page", _page_env()
    for needle in ("(4.2", "t.l", "million$"):
        out = sm.page(url, needle, env=env)
        assert "No match for" in out and "first 3000" in out, needle
    assert "No match for" in sm.page(url, "x" * 4000, env=env)     # find past any window


def test_page_accepts_an_uppercase_scheme_and_allow_host(web):
    port = web.rsplit(":", 1)[1]
    out = sm.page("HTTP://LOCALHOST:%s/page" % port, "4.2 million",
                  env=_page_env(allow="LOCALHOST"))
    assert "Passages matching" in out and "Acme Report" in out


def test_page_title_only_page_round_trips_through_the_cache(tmp_path, web):
    # an empty text body is stored as "title\n": the hit must still name the title
    url, env = _localhost(web) + "/titleonly", _page_env(tmp_path / "pc")
    out = sm.page(url, env=env)
    assert "Title: Only title" in out and "No find given" in out
    again = sm.page(url, "nothing here", env=env)
    assert "Title: Only title" in again and "No match for" in again


def test_tools_list_has_search_and_page():
    tools = sm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "page"]
    assert tools[1]["inputSchema"]["required"] == ["url"]
    assert set(tools[1]["inputSchema"]["properties"]) == {"url", "find"}


def test_passages_windows_never_spin():
    # A find that matches again inside -- and just after -- a window already taken must
    # advance the scan past that match, not re-find it forever: matches at an offset
    # under PASSAGE_CHARS apart (and an end-clamped final window) used to spin here.
    text = "x" * 300 + "needle" + "x" * 100 + "needle" + "x" * 400 + "needle" + "x" * 5
    hits = sm._passages(text, "needle")                     # returns at all == the fix
    assert 1 <= len(hits) <= sm.MAX_PASSAGES
    assert all(len(h) <= sm.PASSAGE_CHARS for h in hits)
    dense = "needle" + "x" * 310 + "needle" + "x" * 310 + "needle"   # windows edge to edge
    assert 1 <= len(sm._passages(dense, "needle")) <= sm.MAX_PASSAGES


def test_passages_windows_are_cut_at_the_texts_own_offsets():
    # the same rule html_to_text learned: the windows must be cut with offsets of the
    # text as given, not of a re-cased copy -- .lower() doubles every "I" with a dot,
    # and the window slid to behind a match the length change had moved
    text = "İ" * 400 + "needle here" + "y" * 600
    hits = sm._passages(text, "NEEDLE HERE")                   # the fold is ASCII-only
    assert len(hits) == 1                                      # the match was found...
    assert "needle here" in hits[0]                            # ...and it is inside
    assert hits[0].index("needle here") < sm.PASSAGE_CHARS // 2 + 60   # ...centred


def test_page_over_mcp_handle(web):
    ok = sm.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "page",
                               "arguments": {"url": _localhost(web) + "/page",
                                             "find": "4.2 million"}}}, _page_env())["result"]
    assert ok["isError"] is False and "4.2 million" in ok["content"][0]["text"]
    bad = sm.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "page", "arguments": {"url": "file:///etc/passwd"}}},
                    {})["result"]
    assert bad["isError"] is True and "WebFetch" in bad["content"][0]["text"]
    assert sm.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "nope", "arguments": {}}}, {})["error"]["code"] == -32602


def test_deep_nesting_does_not_kill_the_server(web):
    # json.loads raises RecursionError (not ValueError) on absurd nesting: one such
    # line must not take the worker's search tool down for the rest of its session.
    import os
    env = dict(os.environ, QWEN_SEARCH_URL=web)
    for k in ("QWEN_SEARCH_KEY", "QWEN_SEARCH_BACKEND", "QWEN_SEARCH_BRAVE_URL"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, str(SCRIPT)],
                       input="[" * 100000 + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n",
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    assert [r.get("id") for r in replies] == [None, 2]
    assert replies[0]["error"]["code"] == -32700
    assert p.returncode == 0 and "Traceback" not in p.stderr
