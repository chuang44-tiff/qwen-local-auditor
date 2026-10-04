"""search_mcp: backend selection, SearXNG/Brave adapters over real HTTP, MCP stdio."""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import pathlib
import urllib.parse

import pytest

import lib.search_mcp as sm

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "local-auditor" / "lib" / "search_mcp.py"


class _H(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        _H.seen.append((u.path, q, dict(self.headers)))
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
    t = threading.Thread(target=srv.serve_forever, daemon=True)
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
    assert [t["name"] for t in tools] == ["search"]
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
