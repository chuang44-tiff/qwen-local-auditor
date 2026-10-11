"""The `search` and `page` tools for qwen-deep-research: a stdlib MCP server (stdio, JSON-RPC 2.0).

Claude Code's own WebSearch is a server-side tool that local servers reject, so the
research workers search through this server instead. Backends: a self-hosted SearXNG
(QWEN_SEARCH_URL, JSON output enabled) or the Brave Search API (QWEN_SEARCH_KEY).
The key is read from the environment only; it is never written to a file.

`page` is the fetch side of the same server: it fetches a url once per run (the text is
cached under QWEN_PAGE_CACHE when the engine sets it) and returns only the passages that
match what the agent is checking -- no model call, where WebFetch runs one per page.
A hostile page must not be able to hang the server or use it to reach the machine, so:
html-to-text is one linear pass (an unclosed tag or comment costs nothing extra); each
hop resolves its host once and connects only to the addresses that check accepted, in
order (DNS rebinding cannot answer public to the check and loopback to the connect);
every non-global address is refused, IPv4 embedded inside IPv6 included (NAT64 and SIIT
prefixes too); proxy environment variables are ignored
(the fetch drives http.client directly, which never reads them -- no urllib opener to
proxy through); and PAGE_DEADLINE seconds cap one whole fetch, chunked read included --
enforced per hop by a watchdog that shuts the socket when the deadline passes, because
headers dripped a byte at a time outrun every per-read timeout (each header line's own
recv gets the full remaining time).
QWEN_PAGE_ALLOW_HOSTS is a comma-separated host allow-list that skips the address
refusals: FOR TESTS ONLY (the page tests serve a local http.server on loopback and need
past the loopback refusal; with it set, serve() says so on stderr at startup); never set
it for a real run -- it opens exactly the door the refusals shut.

  search_mcp.py                 serve MCP on stdin/stdout
  search_mcp.py --query Q [--n N]   one search, JSON rows on stdout (exit 3 on failure)
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

TIMEOUT = 20
PAGE_DEADLINE = 30      # one whole page fetch -- connect, headers and body together --
                        # gets this many seconds (TIMEOUT alone only bounds one read)
PAGE_CHUNK = 64 * 1024  # the page body is read this many bytes at a time
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
PAGE_TOOL = {
    "name": "page",
    "description": "Fetch a web page and return only the parts that bear on your question: "
                   "its title plus up to 5 passages of about 600 characters around each "
                   "case-insensitive match of `find` (with no find, or no match, the first "
                   "3000 characters of the page). Plain text, no model call; a url already "
                   "fetched this run is served from the cache. Use WebFetch to read a page "
                   "whole or when page returns an error.",
    "inputSchema": {"type": "object",
                    "properties": {"url": {"type": "string",
                                           "description": "the page url (http or https)"},
                                   "find": {"type": "string",
                                            "description": "what to look for: a quoted "
                                                           "snippet, a number, a name"}},
                    "required": ["url"]},
}
PAGE_ACCEPT = "text/html"
PAGE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0.0.0 Safari/537.36")
PAGE_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
MAX_REDIRECTS = 3
MAX_PASSAGES = 5
PASSAGE_CHARS = 600
START_CHARS = 3000


class SearchError(Exception):
    pass


class PageError(Exception):
    """A refused or failed page fetch; the .status field (when set) is the HTTP code."""


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


_DROP_TAGS = {"script", "style", "noscript"}
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "dd", "details", "div",
               "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
               "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
               "option", "p", "pre", "section", "select", "summary", "table", "tbody",
               "td", "tfoot", "th", "thead", "tr", "ul"}


_TAG_NAME = re.compile(r"[a-zA-Z][^\t\n\r\f />]*")
_MARKUP_OPEN = re.compile(r"<(?:[/!?]|[a-zA-Z])")   # HTML: '<' opens markup only
                                                     # before a letter, '/', '!' or '?'
# The lowercased copy the raw-text skips search. str.translate with an A-Z -> a-z table
# cannot change the string's length; str.lower() can ("I" with a dot becomes two
# characters), and an offset taken from a re-cased copy would index the original at the
# wrong place -- or past its end.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def html_to_text(html):
    """html to plain text in ONE left-to-right scan, so the time is linear in the input
    (the stdlib parser's was quadratic on hostile pages: "<a " * 20000 took ~25 s, and
    the server is single-threaded). script/style/noscript content is skipped raw to its
    matching close -- an unclosed block, and an unclosed comment, run to the end of the
    input --; comments are dropped; other tags vanish, block tags leaving a line break;
    a '<' that no letter, '/', '!' or '?' follows is page text, not markup; entities
    unescape; the <title> text is collected apart -- it names the page, it is not part
    of the page's text. Every '<', '>' and '-->' is looked at a bounded number of
    times: the two cursors (the scan position and the next '>') only move forward, and
    the raw-text skips search an ASCII-lowercased copy -- the same length as the
    original, which str.lower() does not promise -- forward only."""
    parts, title_parts = [], []
    in_title = False
    low = html.translate(_ASCII_LOWER)
    i, n = 0, len(html)
    gt = html.find(">")                      # the next '>' at or after the scan point
    while i < n:
        j = html.find("<", i)
        if j < 0:                            # trailing text: emit it and stop
            (title_parts if in_title else parts).append(html[i:])
            break
        if j > i:                            # the text between the tags
            (title_parts if in_title else parts).append(html[i:j])
        if html.startswith("<!--", j):
            k = html.find("-->", j + 4)
            if k < 0:
                break                        # unclosed comment: drops to the end
            i = k + 3
            continue
        if not _MARKUP_OPEN.match(html, j):  # '<' before anything else: it is text,
            (title_parts if in_title else parts).append("<")   # not a tag to swallow
            i = j + 1
            continue
        if gt < j:                           # re-aim the '>' cursor forward, never back
            gt = html.find(">", j)
        if gt < 0:                           # a tag left unterminated: it is plain text
            parts.append(html[j:])
            break
        closed = html[j + 1] == "/"
        m = _TAG_NAME.match(html, j + 2 if closed else j + 1)
        name = m.group(0).lower() if m else ""
        if name in _DROP_TAGS:               # raw text until the matching close
            k = low.find("</" + name, gt)
            if k < 0:
                break                        # unclosed block: drops to the end
            k = low.find(">", k)
            i = n if k < 0 else k + 1        # a close left unterminated ends the page
            continue
        if name == "title":
            in_title = not closed
        elif name in _BLOCK_TAGS:            # a block tag, open or close, is a line break
            parts.append("\n")
        i = gt + 1
    text = unescape("".join(parts))
    lines = (" ".join(line.split()) for line in text.split("\n"))
    text = "\n".join(ln for ln in lines if ln)      # every block boundary is exactly one
    return " ".join(unescape("".join(title_parts)).split()), text   # newline: blanks collapse


def _media_charset(content_type):
    parts = (content_type or "").split(";")
    media, charset = parts[0].strip().lower(), ""
    for p in parts[1:]:
        key, _, value = p.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("'\"")
    return media, charset


def _decode(body, charset):
    try:
        return body.decode(charset or "utf-8", "replace")
    except LookupError:                      # a header naming no codec the machine knows
        return body.decode("utf-8", "replace")
    except (ValueError, UnicodeError):       # a codec that exists but takes these bytes
        return body.decode("utf-8", "replace")   # no way at all (e.g. charset=idna)


_NOT_GLOBAL = "resolves to %s, which is not a public address"
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")   # deprecated, but ipaddress still calls
                                                  # it global: refuse it by name
# The two translation prefixes whose last four bytes are a plain IPv4, as PACKED bytes:
# the well-known NAT64 64:ff9b::/96, and the SIIT IPv4-translated ::ffff:0:0/96 as the
# spelling "::ffff:0:a.b.c.d" puts it -- a dotted tail carries the LAST 32 bits, so its
# ff:ff sits one group below where the pure-hex "::ffff:0:0" parses to (a network object
# for that literal covers the mapped range, and misses exactly the translated addresses).
_NAT64_PREFIX = b"\x00\x64\xff\x9b" + b"\x00" * 8
_SIIT_PREFIX = b"\x00" * 8 + b"\xff\xff" + b"\x00" * 2


def _not_public(ip):
    """True for every address a page fetch must never connect to. Not-global covers
    loopback, link-local, the RFC 1918 ranges, the shared-address space the carriers
    hand out (the /10 Tailscale and some cloud metadata live in), reserved and the
    unspecified ones; the explicit multicast, unspecified and site-local cases cover
    what one interpreter's is_global happens to leave out."""
    return (not ip.is_global or ip.is_multicast or ip.is_unspecified
            or (ip.version == 6 and ip in _SITE_LOCAL))


def _embedded_ipv4(ip):
    """The IPv4 inside an IPv6 address that carries one: IPv4-mapped (::ffff:a.b.c.d),
    6to4 (2002:a.b.c.d::), the deprecated IPv4-compatible (::a.b.c.d) form, and the
    last four bytes of either translation prefix -- the well-known NAT64 64:ff9b::/96
    and the SIIT IPv4-translated ::ffff:0:0/96 -- whose addresses is_global calls
    global on more than one interpreter, so only the embedded IPv4 can say what they
    really reach. An address reached through the outer IPv6 reaches its embedded IPv4
    as it is, so an embedded one is refused on exactly the same grounds as a plain one."""
    if ip.version != 6:
        return ()
    p = ip.packed
    if p[:10] == b"\x00" * 10 and p[10:12] == b"\xff\xff":
        return (ipaddress.IPv4Address(p[12:]),)              # ::ffff:a.b.c.d
    if p[:2] == b"\x20\x02":
        return (ipaddress.IPv4Address(p[2:6]),)              # 2002:a.b.c.d::
    if p[:12] == b"\x00" * 12 and p != b"\x00" * 16:
        return (ipaddress.IPv4Address(p[12:]),)              # ::a.b.c.d
    if p[:12] in (_NAT64_PREFIX, _SIIT_PREFIX):
        return (ipaddress.IPv4Address(p[12:]),)              # 64:ff9b::/96, ::ffff:0:0/96
    return ()


def _checked_hop(url, env):
    """The scheme-and-address gate every page fetch passes (again on every redirect
    hop): http/https only, and every address the host resolves to must be global -- a
    local LLM server on 127.0.0.1, a LAN or CGNAT peer, or any IPv6 that merely hides
    one of those, is refused. The host is resolved exactly ONCE here and EVERY address
    it answers -- de-duplicated, in order -- is returned for the connect to pin:
    resolving twice (gate, then connect) is the DNS-rebinding window, and only these
    checked addresses are ever connected to, in this order, until one connects.
    Returns (parts, port, checked_ips)."""
    try:
        parts = urllib.parse.urlsplit(url)   # raises ValueError on a mangled netloc,
        host = parts.hostname                # e.g. an unbalanced "[" -- refuse, never raise
    except ValueError:
        raise PageError("the url could not be parsed as given")
    if parts.scheme not in ("http", "https"):
        raise PageError("only http and https urls can be fetched")
    if not host:
        raise PageError("the url names no host")
    try:
        port = parts.port                    # ValueError again on a port out of range
    except ValueError:
        raise PageError("the url names no valid port")
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    allow = host.lower() in {h.strip().lower() for h in
                             (env.get("QWEN_PAGE_ALLOW_HOSTS") or "").split(",") if h.strip()}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise PageError("%s does not resolve" % host)
    checked = []
    for info in infos:
        raw = info[4][0]
        plain = raw.split("%")[0]            # an IPv6 scope id is not part of the address
        try:
            ip = ipaddress.ip_address(plain)
        except ValueError:
            ip = None
        if not allow and (ip is None or _not_public(ip)
                          or any(_not_public(v4) for v4 in _embedded_ipv4(ip))):
            raise PageError("%s %s" % (host, _NOT_GLOBAL % raw))
        if plain not in checked:             # keep every answer, in order, but do not
            checked.append(plain)            # connect to the same one twice
    if not checked:
        raise PageError("%s does not resolve" % host)
    return parts, port, checked


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """connect() goes to the checked addresses -- every answer _checked_hop resolved,
    in the order it returned them -- instead of letting the socket layer resolve host
    again: a name with TTL 0 can answer public to the check and 127.0.0.1 to the
    connect (DNS rebinding), so the answers that were checked are the only ones ever
    connected to. An answer that refuses or times out moves to the next checked one,
    each attempt bounded to the time the hop's deadline still leaves. `host` is
    untouched: it is still the Host header, and for https the certificate name. When
    _fetch_page sets hop_deadline, making the connection is also what arms the hop's
    watchdog: before this method returns there is no fd a shutdown could reach, and
    every connect attempt is already bounded by the timeout create_connection is
    given."""
    checked_ips = None
    hop_deadline = None                    # set by _fetch_page; None arms nothing
    watchdog = None                        # this hop's timer and fired event, set
    fired = None                           # and started together by an armed connect()
    watch_sock = None                      # the socket the watchdog shuts once conn.sock
                                           # is gone: a will-close getresponse clears
                                           # conn.sock while the response keeps reading
                                           # the same fd through its own file object

    def connect(self):
        last_error = None
        for ip in self.checked_ips:
            left = self.timeout
            if self.hop_deadline is not None:
                left = min(left, self.hop_deadline - time.monotonic())
                if left <= 0:              # the attempts so far used the hop's time
                    raise _deadline_error()
            try:
                self.sock = socket.create_connection((ip, self.port), timeout=left)
            except OSError as e:           # refused/unroutable: on to the next checked
                last_error = e             # answer (a timeout lands past the deadline)
            else:
                break
        else:
            raise last_error               # none connected: the last answer's failure
        self.watch_sock = self.sock        # dated from the connect, so the fd stays the
                                           # watchdog's to shut even after a will-close
                                           # getresponse clears conn.sock (its response
                                           # keeps the fd open through its file object)
        if self.hop_deadline is not None:
            self.watchdog, self.fired = _hop_watchdog(self, self.hop_deadline)
            self.watchdog.start()


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    """Same pinning over TLS: wrap_socket with server_hostname=host keeps the
    certificate verified against the original host name. The TLS handshake is bounded
    by the socket timeout the connection carries, not by the watchdog the parent's
    connect() armed: once wrap_socket takes the fd over, a shutdown on the socket
    conn.sock names reaches nothing of it."""
    default_port = 443

    def connect(self):
        _PinnedHTTPConnection.connect(self)
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def _page_error(msg, status="error"):
    err = PageError(msg)
    err.status = status
    return err


def _deadline_error():
    return _page_error("the page did not arrive within %d seconds" % PAGE_DEADLINE)


def _read_page_body(sock, resp, deadline):
    """The body in PAGE_CHUNK pieces: the socket timeout is cut to the time the deadline
    still leaves before every read, so a server dripping one byte per TIMEOUT-less gap
    cannot hold the tool past PAGE_DEADLINE the way it could with one fixed per-recv
    timeout. Once read1 has returned the last declared byte it closes the connection
    itself, so the loop stops the moment the response reports it is done -- touching
    that socket after that point is a read on a closed descriptor. A connection that
    dies mid-body simply ends the body where it stands (the caller turns a body the
    watchdog ended into the deadline error instead). At most MAX_BYTES + 1 bytes are
    taken; a timed-out read raises through the caller's deadline handler."""
    buf = bytearray()
    while len(buf) <= MAX_BYTES:
        if resp.fp is None:                    # read1 closed the connection once the
            break                              # declared length arrived: body complete
        left = deadline - time.monotonic()
        if left <= 0:
            raise _deadline_error()
        sock.settimeout(left)
        chunk = resp.read1(PAGE_CHUNK)
        if not chunk:                          # died mid-body: take what arrived
            break
        buf += chunk
    return bytes(buf[:MAX_BYTES + 1])


def _hop_watchdog(conn, deadline):
    """The per-recv timeout cannot bound the header phase: http.client reads the status
    line and every header line with its own recv, each allowed the full remaining time,
    so a server dripping one header byte per gap holds the single-threaded server for
    hours before http.client's own 64 KB line / 100 header ceilings bite. The daemon
    timer returned here ends any read or send still in flight when the deadline passes
    -- shutdown(SHUT_RDWR) on the connection's socket -- and sets the returned event
    first, so the hop's except paths can call such an ended read the deadline, not a
    broken connection. _PinnedHTTPConnection calls this as its connect() finishes, so
    the interval -- and with it the deadline -- is dated from the moment the connection
    is made. A response that will close clears conn.sock as getresponse hands the socket
    to it, but the response keeps the same fd open through its own file object, so
    fire() shuts conn.sock while it stands and the socket stored at connect() once it
    is gone; a socket that vanished mid-fire leaves nothing to kill (OSError ignored);
    where a blocked read ends with an error instead of the EOF Linux's shutdown gives
    it, the event is what names it. The TLS handshake is not this timer's to bound:
    wrap_socket takes the fd out from under conn.sock, so the handshake ends on the
    socket timeout the connection carries -- and a shutdown landing on a half-wrapped
    socket surfaces as a ValueError ("Read on closed or unwrapped SSL socket") that
    the hop maps through the fired event."""
    fired = threading.Event()

    def fire():
        fired.set()
        try:
            sock = conn.sock if conn.sock is not None else conn.watch_sock
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), fire)
    timer.daemon = True                        # never the reason the process stays alive
    return timer, fired


def _fetch_page(url, env):
    """GET the url by driving http.client directly -- the page fetch has no urllib
    opener at all, so nothing behind it re-resolves the host (the pinned connection is
    what connects) and http_proxy/https_proxy, which only urllib reads, are bypassed as
    a matter of route, not of configuration. Follows at most MAX_REDIRECTS redirects,
    re-checking scheme, address and resolution on every hop, and PAGE_DEADLINE seconds
    cover the whole fetch (all hops, connect, headers and body); returns
    (content_type, body, status). The body may exceed MAX_BYTES by one byte: the caller
    refuses an oversized page without reading it whole."""
    deadline = time.monotonic() + PAGE_DEADLINE
    target, hops = url, 0
    while True:
        parts, port, checked_ips = _checked_hop(target, env)
        request_target = parts.path or "/"
        if parts.query:
            request_target += "?" + parts.query
        left = deadline - time.monotonic()
        if left <= 0:
            raise _deadline_error()
        cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
        conn = cls(parts.hostname, port, timeout=left)
        conn.checked_ips = checked_ips      # connect() tries these, in order
        conn.hop_deadline = deadline        # connect() starts the watchdog it arms
        resp = None
        try:
            conn.request("GET", request_target, headers={"Accept": PAGE_ACCEPT,
                                                         "User-Agent": PAGE_UA})
            left = deadline - time.monotonic()
            if left <= 0:
                raise _deadline_error()
            # getresponse() hands the socket to the response when the server asked to
            # close (conn.sock becomes None then), so the socket is taken before it:
            # settimeout reaches the same socket either way, and fire() shuts this one
            # once conn.sock is cleared
            sock = conn.sock
            conn.watch_sock = sock
            sock.settimeout(left)                                  # for the header read
            resp = conn.getresponse()
            loc = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and loc:
                if hops >= MAX_REDIRECTS:
                    raise _page_error("the page redirected more than %d times" % MAX_REDIRECTS,
                                      resp.status)
                resp.close()                     # release the hop being left behind (and its
                hops += 1                        # unread body) before asking the next one
                target = urllib.parse.urljoin(target, loc)
                continue
            if not 200 <= resp.status < 300:
                raise _page_error("the page returned HTTP %d" % resp.status, resp.status)
            # the fetch sends no Accept-Encoding and decodes nothing: a body any of
            # whose content codings is not identity would be cached and shown as bytes
            # no reader can use -- every header counts, and a comma list splits into
            # the codings it names, so "identity" first cannot hide "gzip" after it.
            # The error stays short: 40 characters per coding, 200 in all -- a hostile
            # 100 x 64 KB header set must not reach the tool's answer whole
            codings = [e.strip() for head in resp.headers.get_all("Content-Encoding", ())
                       for e in head.split(",") if e.strip()]
            enc = ", ".join(c[:40] for c in codings if c.lower() != "identity")[:200]
            if enc:
                raise _page_error("the page was sent compressed (%s)" % enc, resp.status)
            body = _read_page_body(sock, resp, deadline)
            if conn.fired.is_set():              # the watchdog ended the body read where
                raise _deadline_error()          # it stood: truncated bytes are the deadline
            return resp.getheader("Content-Type") or "", body, resp.status
        except TimeoutError:                     # socket.timeout IS TimeoutError: any read,
            raise _deadline_error()              # send or connect past the deadline gets here
        except http.client.HTTPException as e:   # a connect-phase failure has no watchdog
            if conn.fired is not None and conn.fired.is_set():   # the watchdog ended this
                raise _deadline_error()          # read: the page died on the deadline
            raise PageError("the page fetch failed: %s" % type(e).__name__)
        except OSError as e:                     # a refused or vanished connection
            if conn.fired is not None and conn.fired.is_set():   # ...and the shutdown that
                raise _deadline_error()          # ended it is the deadline's doing
            raise PageError("the page is unreachable: %s" % e)
        except (ValueError, UnicodeError):       # a url or redirect target that cannot be
            if conn.fired is not None and conn.fired.is_set():   # sent -- or a shutdown
                raise _deadline_error()          # landed mid-TLS-read: "Read on closed
            raise PageError("the url could not be requested as given")   # or unwrapped
                                                 # SSL socket" is ValueError, and the
                                                 # fired event is what names it
        except PageError:
            raise                                # the refusals this hop raises itself
        except Exception as e:                   # the tool answers in text, never raises:
            if conn.fired is not None and conn.fired.is_set():   # whatever the watchdog
                raise _deadline_error()          # ended was the deadline, whatever the
            raise PageError("the page fetch failed: %s" % type(e).__name__)   # error's own
                                                 # text might be
        finally:                               # the watchdog was armed and started by
            if conn.watchdog is not None:      # connect(); a connect that never finished
                conn.watchdog.cancel()         # left nothing to disarm. Joined -- only if
                if conn.watchdog.is_alive():   # a thread really exists (an interrupt can
                    conn.watchdog.join()       # land between the arm and the start) -- so
                                               # no timer outlives the hop and its shutdown
                                               # cannot race the close below
            if resp is not None:               # also on a body read the deadline killed:
                resp.close()                   # the response never outlives the hop
            conn.close()


def _cache_dir(env):
    d = (env.get("QWEN_PAGE_CACHE") or "").strip()
    return d or None


def _page_log(cache_dir, key, state, status, nbytes):
    """One tab-separated line -- time, sha256, hit|miss|error, status, bytes -- appended
    to <dir>/log.tsv with O_APPEND. With no cache dir named there is nothing to log to;
    a log that cannot be written never sinks the fetch it describes."""
    if cache_dir is None:
        return
    line = "%d\t%s\t%s\t%s\t%d\n" % (time.time(), key, state, status, nbytes)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        fd = os.open(os.path.join(cache_dir, "log.tsv"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _read_cache(cache_dir, key):
    if cache_dir is None:
        return None
    try:
        with open(os.path.join(cache_dir, key + ".txt"), "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _write_cache(cache_dir, key, stored):
    # A temp file in the same folder, then os.replace: a concurrent reader sees the old
    # file or the new one, never half of either. (The same shape as swarm_engine.api's
    # atomic_write, inlined because this server is run by path and imports no lib code.)
    path = os.path.join(cache_dir, key + ".txt")
    tmp = os.path.join(cache_dir, ".%s.%d.tmp" % (key, os.getpid()))
    try:
        with open(tmp, "wb") as fh:
            fh.write(stored.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _passages(text, find):
    """Up to MAX_PASSAGES windows of PASSAGE_CHARS around case-insensitive matches of
    find, earliest first; a match inside a window already taken adds no second window.
    Case folds ASCII-only (the same length-preserving table html_to_text searches
    with): str.lower() can change the text's length, and a window cut at an offset
    from its copy would miss the match it was meant to centre on."""
    low, needle = text.translate(_ASCII_LOWER), find.translate(_ASCII_LOWER)
    out, pos = [], 0
    while len(out) < MAX_PASSAGES:
        i = low.find(needle, pos)
        if i < 0:
            break
        start = max(0, min(i - PASSAGE_CHARS // 2, max(0, len(text) - PASSAGE_CHARS)))
        end = min(len(text), start + PASSAGE_CHARS)
        if out and start < out[-1][1]:
            # this match's window overlaps the one just taken; the next candidate must
            # be past it, and strictly past this match, so the loop always advances
            pos = max(out[-1][1] + PASSAGE_CHARS // 2, i + 1)
            continue
        out.append((start, end, text[start:end]))
        pos = max(i + 1, end)
    return [t for _, _, t in out]


def _format_page(title, text, find):
    head = "Title: %s\n\n" % (title or "(no title)")
    needle = " ".join(find.split()) if isinstance(find, str) else ""
    if needle:
        hits = _passages(text, needle)
        if hits:
            return (head + 'Passages matching "%s" (%d shown, ~%d characters each, of a '
                    "%d-character page):\n\n" % (needle, len(hits), PASSAGE_CHARS, len(text))
                    + "\n\n".join(hits))
        lead = 'No match for "%s": ' % needle
    else:
        lead = "No find given: "
    tail = "" if len(text) <= START_CHARS else \
        "\n(the page is %d characters; these are its first %d)" % (len(text), START_CHARS)
    return head + lead + "the first %d characters of the page:\n\n%s%s" % (
        START_CHARS, text[:START_CHARS], tail)


def _page(url, find, env, key, cache_dir):
    stored = _read_cache(cache_dir, key)
    if stored is not None:
        title, _, text = stored.partition("\n")
        _page_log(cache_dir, key, "hit", "cache", len(stored.encode("utf-8")))
        return _format_page(title, text, find)
    # every failure path raises PageError, and _page_answer logs the error line once
    content_type, body, status = _fetch_page(url, env)
    if len(body) > MAX_BYTES:
        raise _page_error("the page is larger than 2 MB", status)
    media, charset = _media_charset(content_type)
    if media not in PAGE_TYPES:
        raise _page_error("%s is not a text page (text/html, text/plain or "
                          "application/xhtml+xml only)" % (media or "no content type"), status)
    raw = _decode(body, charset)
    if media == "text/plain":
        title, text = "", raw
    else:
        title, text = html_to_text(raw)
    stored = title + "\n" + text
    if cache_dir is not None:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            _write_cache(cache_dir, key, stored)
        except OSError:
            pass                             # an unwritable cache never fails the fetch
    _page_log(cache_dir, key, "miss", status, len(stored.encode("utf-8")))
    return _format_page(title, text, find)


def page(url, find=None, env=None):
    """The `page` tool: fetch a page once per run and return its title plus the
    passages around `find` -- plain text, no model call. Never raises: a refusal or a
    failed fetch comes back as the error text, which names WebFetch as the fallback."""
    return _page_answer(url, find, env)[0]


def _page_answer(url, find, env=None):
    """(text, ok) -- ok is False when the text is an error text."""
    env = os.environ if env is None else env
    # the key hashes the url as it will actually be fetched, so a padded spelling of a
    # page and the plain one share one cache entry
    target = url.strip() if isinstance(url, str) else ""
    key = hashlib.sha256(target.encode("utf-8", "replace")).hexdigest()
    cache_dir = _cache_dir(env)
    if not target:
        _page_log(cache_dir, key, "error", "error", 0)
        return _page_text_error("no url given"), False
    try:
        return _page(target, find if isinstance(find, str) else None, env, key, cache_dir), True
    except PageError as e:
        _page_log(cache_dir, key, "error", getattr(e, "status", "error"), 0)
        return _page_text_error(str(e)), False


def _page_text_error(msg):
    return "page: %s. You may use WebFetch instead." % msg


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
        return _reply(mid, {"tools": [TOOL, PAGE_TOOL]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if name == "search":
            try:
                rows = search(args.get("query"), args.get("n", 8), env=env)
            except SearchError as e:
                return _reply(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            text = json.dumps(rows, ensure_ascii=False, indent=1) if rows else "no results"
            return _reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        if name == "page":
            text, ok = _page_answer(args.get("url"), args.get("find"), env)
            return _reply(mid, {"content": [{"type": "text", "text": text}], "isError": not ok})
        return _reply(mid, error={"code": -32602, "message": "unknown tool %r" % name})
    return _reply(mid, error={"code": -32601, "message": "method not found: %s" % method})


def serve(stdin, stdout, env=None):
    env = os.environ if env is None else env
    allow = (env.get("QWEN_PAGE_ALLOW_HOSTS") or "").strip()
    if allow:
        # the one line that the test-only allow-list is in force: it says exactly what
        # it turns off and for whom, because in a real run it is the hole, not a feature
        print("search_mcp: QWEN_PAGE_ALLOW_HOSTS=%s: the page tool's address refusals are "
              "off for those hosts -- for tests only, never for a real run." % allow,
              file=sys.stderr, flush=True)
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
