"""wf.claude_check: ask Claude, through the user's own claude login, to check a list of
items -- one `claude -p` per item, one after another.

The workflow says what to ask (prompt(item) -> str) and how to read the answer
(parse(text, item) -> data, or ValueError). Everything else is the engine's, the same
for every workflow:

- The call. QWEN_CLAUDE_BIN (else claude) resolved on PATH for every call (Windows
  CreateProcess finds neither claude.cmd nor a bare name the way a shell does), in
  advisor_mcp.clean_env -- the login, never a Qwen redirect -- with a fixed read-only
  flag set; the prompt goes on stdin (--tools, --add-dir and --allowedTools take lists
  and would swallow a trailing prompt) through a pipe, never the runner's own stdin.
  The cwd is RUN/agents/<name>-<id>/; `stage` is copied into it as fixtures/
  (wf.stage_dir) by staging.copy, the same walk, link rule and size limit as a
  fan_out unit's fixtures; Read reaches only the cwd and read_dirs.
- browser=True adds lib/browser_mcp's Playwright server (its output dir
  RUN/browser/<name>-<id>/ is readable too, for the screenshots), every Playwright tool
  but three granted -- browser_evaluate and the browser_network_requests list included,
  scripted probes are the point -- and those three (browser_run_code_unsafe,
  browser_install, browser_network_request) passed as --disallowedTools so the model
  never sees them.
- The process. Its own process group (a new session on POSIX, CREATE_NEW_PROCESS_GROUP
  on Windows), killed whole on timeout or KeyboardInterrupt (killpg / taskkill /T /F,
  sandbox._kill) so no npx, Playwright or Chromium outlives it; the interrupt then
  propagates as the runner expects.
- Exit 126/127 means claude never ran only when the output holds no parseable envelope
  (not found, or being replaced by an update): the binary is re-resolved and the call
  retried after each QWEN_EXEC_RETRY_BACKOFF delay (default "10 30" seconds; set but blank
  is no retry at all), as qwen-agent's run_claude does. A 126/127 whose stdout does hold an
  envelope is handled like any other exit: the answer is read, nothing is retried.
- The probe: once per run() invocation, just before the first item that really needs a
  call (a cache hit, a --check dry run, an over_cap or deadline item never triggers it),
  probe(env) asks cheaply -- `claude auth status` (local state, no network, no tokens)
  and one HEAD to QWEN_CLAUDE_PROBE_URL -- whether claude can be used at all. It answers
  (state, why, kind): state "available", "unavailable" or "unknown", and, for an
  "unavailable", a kind of "login", "network" or "binary". A login or a network answer
  ends the run like the breaker does -- every remaining item "unavailable" with that
  reason, one run.log line, one claude_probe event, and not one call made. A "binary" one
  does not pre-trip anything: a claude mid-auto-update fails 126/127 for exactly a moment,
  which is what the retry above decides, so the run logs one line and makes its call as
  if the probe had said nothing (no event). "available" and "unknown" (a machine the
  probe cannot read) proceed exactly as before.
  QWEN_CLAUDE_PROBE=off skips the probe. The in-call breaker below stays as the backstop.
- The breaker trips on claude's OWN availability failures only: 126/127 after the
  retries with nothing run; AUTH_DOWN -- a login/auth failure of the CLI ("not logged
  in", a bad key, an expired oauth token, a bare API Error: 401/403) -- in an error
  envelope's text or in unreadable output; NET_DOWN -- the CLI failing to reach the
  API ("connection error", ECONNREFUSED, getaddrinfo, fetch failed) -- where nothing
  parsed at all (empty stdout, or not JSON), NEVER in the text of an is_error envelope;
  or output that does not parse on the FIRST call. A parseable envelope whose
  CLI-level fields say the API call never got a response trips it too -- the real
  offline run exits 1 with a parseable envelope ("API Error: ... ECONNREFUSED"), so
  NET_DOWN's nothing-parsed ground never holds for it: terminal_reason "api_error"
  triggers on its own (no is_error needed), or is_error with a result starting
  "API Error" and duration_api_ms 0, in both cases after the per-item subtypes; the
  envelope's result is the reason. A usage limit is per-item
  (by design): an api_error with api_error_status 429 or rate/usage-limit text stays
  "failed". Words like 403, credential or
  ERR_CONNECTION_REFUSED inside a result envelope come from the PAGE under test, and
  an error envelope's subtype (error_max_turns, the budget) is read before AUTH_DOWN.
  Every remaining item after the breaker is "unavailable" with that reason, without a
  call. A failure of one item -- the turn limit, the timeout, is_error, the budget, an
  answer parse rejects -- is "failed" and the next item is still tried.
- max_calls counts calls made (cache hits are free); items past it are "over_cap".
  Once the run's deadline has passed the remaining items are "deadline" (a run.log
  line each, as fan_out logs an item that never started). Neither is cached, and
  neither touches wf.not_run: the workflow maps every state to its own verdict.
- The cache: artifact claude-<name> {item id: {"key", "data"}}, "ok" results only,
  saved after EACH call so an interrupt loses nothing, AFTER the call is recorded --
  a paid call is never lost to an answer that will not serialize into the artifact:
  it stays ok, uncached, and run.log says so. Data enters and leaves the cache as a
  copy.deepcopy, so a caller mutating result["data"] cannot corrupt what is cached.
  The key is the sha256 of the JSON of its parts [prompt, model, browser, the staged
  files' contents] (wf.stage_digest: staging.digest, walked once per run), so no
  two field splits share a key; a resume after logging in or with a larger cap asks
  again for everything that was not ok. Prompts must not carry timestamped paths (the
  cwd is stable by construction).
- The records, one per call made -- written before that call's cache save:
  RUN/claude/calls.jsonl, a run.log line (role claude-check), a claude_call event,
  and wf.claude_calls / wf.claude_cost_usd, which wf.totals() adds to totals.json.

Under --check (wf.check is set) nothing starts: the dry run's answer(role, prompt) is
asked with role "claude-check" and parsed; no answer, or one parse rejects, is
"unavailable". The calls still join wf.calls, so the determinism check covers them.
"""
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from lib import advisor_mcp, browser_mcp
from lib.swarm_engine import events, sandbox, staging

ROLE = "claude-check"
EXEC_FAILED = (126, 127)
DEFAULT_BACKOFF = (10, 30)
BROWSER_ALLOWED = ("browser_navigate", "browser_navigate_back", "browser_snapshot",
                   "browser_find", "browser_click", "browser_type", "browser_fill_form",
                   "browser_press_key", "browser_select_option", "browser_hover",
                   "browser_drag", "browser_drop", "browser_file_upload",
                   "browser_handle_dialog", "browser_tabs", "browser_resize",
                   "browser_emulate_media", "browser_wait_for", "browser_take_screenshot",
                   "browser_console_messages", "browser_network_requests",
                   "browser_evaluate", "browser_close")
BROWSER_DENIED = ("browser_run_code_unsafe", "browser_install", "browser_network_request")
# The CLI's own availability failures -- never the words the PAGE under test produces.
# AUTH_DOWN: the login is gone or rejected ("not logged in", a bad key, an expired
# oauth token, a bare API Error: 401/403 line). Matched on an error envelope's text or
# on unreadable output. NET_DOWN: no route to the API. Matched ONLY where nothing
# parsed at all (empty stdout, or not JSON -- the CLI itself failed); the same words
# inside a result envelope ("net::ERR_CONNECTION_REFUSED", "navigation returned 403",
# "credential field not found") are the page talking and stay per-item.
AUTH_DOWN = re.compile(
    r"not logged in|invalid api key|please run /login|"
    r"oauth token (?:has )?expired|oauth token|api error:? ?(?:401|403)|"
    r"authentication_error", re.I)
NET_DOWN = re.compile(
    r"connection error|connection refused|econnrefused|enotfound|eai_again|econnreset|"
    r"getaddrinfo|fetch failed|network is unreachable|unable to connect to api", re.I)
# The exclusion on an envelope api_error: a usage limit is the ITEM's own (by design), not a
# claude availability failure. Looked at ONLY inside the terminal_reason/"API Error" guard.
USAGE_LIMIT = re.compile(r"rate limit|usage limit", re.I)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
PROBE_URL = "https://api.anthropic.com/"        # HEAD to it answers 404 in ~0.1 s online
PROBE_LOGIN_TIMEOUT = 15                        # seconds for `claude auth status`
# What an "unavailable" probe rests on. PROBE_BINARY is the one the run does NOT pre-trip
# on: a claude mid-auto-update is briefly not found or not executable -- exit 126/127,
# exactly what QWEN_EXEC_RETRY_BACKOFF's retry inside the call exists to ride out, so the
# call decides that one. A gone login or an unreachable API ends the run before a call.
PROBE_BINARY, PROBE_LOGIN, PROBE_NETWORK = "binary", "login", "network"


def safe_id(item_id):
    """An item id as a folder-name part: anything but [A-Za-z0-9._-] becomes '_'."""
    return _UNSAFE.sub("_", item_id) or "_"


def backoff(env):
    """The retry delays for exit 126/127: QWEN_EXEC_RETRY_BACKOFF ("10 30"), whole
    seconds >= 0. Unset is the default; SET but blank -- empty, or only whitespace -- is
    NO retry, (), as qwen-agent's ${VAR-10 30} reads it; anything unreadable, or a
    negative delay, is the default."""
    raw = env.get("QWEN_EXEC_RETRY_BACKOFF")
    if raw is None:
        return DEFAULT_BACKOFF
    try:
        vals = tuple(int(x) for x in raw.split())
    except ValueError:
        return DEFAULT_BACKOFF
    return vals if all(v >= 0 for v in vals) else DEFAULT_BACKOFF


def resolve(env):
    """The claude to start: QWEN_CLAUDE_BIN, else claude, looked up with shutil.which on
    the env's PATH -- on Windows that applies PATHEXT, so npm's claude.cmd is found;
    CreateProcess would not find it from a bare name. Unfound: the name as given (the
    spawn then fails 127 and the retry re-resolves)."""
    named = env.get("QWEN_CLAUDE_BIN") or "claude"
    return shutil.which(named, path=env.get("PATH")) or named


def flags(model, max_turns, budget_usd, read_dirs, mcp_config=None):
    """claude's argv after the binary. The prompt is NOT here: it goes on stdin."""
    argv = ["-p", "--model", model, "--output-format", "json", "--max-turns", str(max_turns),
            "--max-budget-usd", "%g" % budget_usd, "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--restricted", "--tools", "Read"]
    for d in read_dirs:
        argv += ["--add-dir", str(d)]
    allowed = ["Read"]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
        allowed += ["mcp__playwright__%s" % t for t in BROWSER_ALLOWED]
        argv += ["--disallowedTools", ",".join("mcp__playwright__%s" % t for t in BROWSER_DENIED)]
    argv += ["--allowedTools", ",".join(allowed)]
    return argv


def spawn(argv, cwd, env, text, timeout):
    """(rc, stdout, stderr, timed_out) of one run in its own process group. A binary
    that cannot be started is rc 127 (not found) or 126 (not executable), the codes a
    shell gives. On timeout or KeyboardInterrupt the whole group is killed; the
    interrupt is re-raised."""
    group = ({"start_new_session": True} if os.name == "posix"
             else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)})
    try:
        p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", **group)
    except FileNotFoundError:
        return 127, "", "", False
    except OSError:
        return 126, "", "", False
    try:
        out, err = p.communicate(text, timeout=timeout)
    except subprocess.TimeoutExpired:
        sandbox._kill(p)
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:          # a grandchild that left the group
            out, err = "", ""
        return p.returncode, out or "", err or "", True
    except BaseException:                          # KeyboardInterrupt, SIGTERM/SIGHUP
        sandbox._kill(p)
        with contextlib.suppress(Exception):
            p.wait(timeout=10)
        raise
    return p.returncode, out or "", err or "", False


def envelope(stdout):
    """The --output-format json result object: the last stdout line that parses as a
    JSON object, else None."""
    for line in reversed((stdout or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _brief(text, n=200):
    return " ".join(str(text).split())[:n]


def _probe_bypassed(env, host):
    """True when no_proxy/NO_PROXY in `env` exempts `host` from the proxy: an entry equal
    to the host, or the domain the host sits under, or * for everything. (urllib's own
    proxy_bypass_environment reads os.environ, not the env we were given.)"""
    host = (host or "").lower()
    for pat in (env.get("no_proxy") or env.get("NO_PROXY") or "").replace(" ", ",").split(","):
        pat = pat.strip().lower().lstrip(".")
        if pat == "*" or (pat and (host == pat or host.endswith("." + pat))):
            return True
    return False


def probe(env, timeout=5):
    """The cheap answer (~0.2 s, no tokens) to "can claude be used at all", before the
    first paid call does the expensive answering: (state, why, kind), state one of
    "available", "unavailable" or "unknown" -- never a false offline verdict, "unknown"
    means decide by the real call exactly as today. For an "unavailable", `kind` says what
    it rests on -- PROBE_BINARY (the binary itself: not found, or not executable),
    PROBE_LOGIN (the login) or PROBE_NETWORK (no route to the API); "" for any other
    state. Whether an "unavailable" ends the run is the caller's call by kind: a binary is
    the brief mid-auto-update failure the in-call retry rides out. The login comes from
    `claude auth status` (local state, no network); the route to the API from one HEAD,
    where ANY HTTP answer -- a 404 included -- counts as reachable. The proxy settings IN
    `env` are honoured (no_proxy bypasses). QWEN_CLAUDE_PROBE=off: "unknown", skip the
    probe (tests, or a network the probe gets wrong); QWEN_CLAUDE_PROBE_URL: where the HEAD
    goes. The probe never raises: an error of its own is one more "unknown"."""
    try:
        if (env.get("QWEN_CLAUDE_PROBE") or os.environ.get("QWEN_CLAUDE_PROBE")) == "off":
            return "unknown", "probe off", ""
        login, exe = None, resolve(env)
        try:
            p = subprocess.run([exe, "auth", "status"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=PROBE_LOGIN_TIMEOUT)
        except (FileNotFoundError, PermissionError):
            return ("unavailable", "claude not found or not executable (%s)" % exe,
                    PROBE_BINARY)
        except subprocess.TimeoutExpired:
            pass                                   # undecided: let the network speak
        else:
            if p.returncode in EXEC_FAILED:        # the codes a shell gives for exactly this
                return ("unavailable", "claude not found or not executable (%s)" % exe,
                        PROBE_BINARY)
            obj = envelope(p.stdout)
            if isinstance(obj, dict) and isinstance(obj.get("loggedIn"), bool):
                if not obj["loggedIn"]:
                    return ("unavailable", "claude is not logged in (run: claude /login)",
                            PROBE_LOGIN)
                login = True                       # any other output: login undecided
        url = env.get("QWEN_CLAUDE_PROBE_URL") or PROBE_URL
        host = urllib.parse.urlsplit(url).hostname or url
        proxies = {}
        for scheme in ("http", "https"):
            v = env.get(scheme + "_proxy") or env.get(scheme.upper() + "_PROXY")
            if v and not _probe_bypassed(env, host):
                proxies[scheme] = v
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        try:
            resp = opener.open(urllib.request.Request(url, method="HEAD"), timeout=timeout)
        except urllib.error.HTTPError:             # an answer IS reachability: 404, 401, 5xx
            resp = None
        except (urllib.error.URLError, OSError) as e:      # refused, no route, timed out
            return ("unavailable", "cannot reach %s: %s"
                    % (host, _brief(getattr(e, "reason", None) or e)), PROBE_NETWORK)
        if resp is not None:
            resp.close()
        return ("available", "", "") if login else ("unknown", "", "")
    except Exception as e:
        return "unknown", "probe error: %s" % _brief(e), ""


def call(wf, name, iid, text, item, parse, *, model, budget_usd, browser, stage, read_dirs,
         timeout, max_turns, env, first):
    """One item's call: (state, data, why, rec); rec = rc, seconds, cost_usd, num_turns,
    tokens. state is ok, failed or unavailable (the caller trips its breaker on that)."""
    unit = "%s-%s" % (name, safe_id(iid))
    cwd = wf.sw.agents_dir / unit
    cwd.mkdir(parents=True, exist_ok=True)
    if stage is not None:                      # a fresh copy per call: nothing left over
        staging.copy(str(stage), str(wf.stage_dir(unit)))
    dirs = [os.path.abspath(str(d)) for d in read_dirs]
    mcp = None
    if browser:
        bdir = wf.browser_dir(unit)
        bdir.mkdir(parents=True, exist_ok=True)
        mcp = bdir / "mcp.json"
        mcp.write_bytes(browser_mcp.text(browser_mcp.build(str(bdir))).encode("utf-8"))
        dirs.append(str(bdir))
    tail = flags(model, max_turns, budget_usd, dirs, mcp)
    rec = {"rc": None, "seconds": 0.0, "cost_usd": None, "num_turns": None, "tokens": 0}
    delays = list(backoff(os.environ))
    t0 = time.time()
    while True:
        rc, out, err, timed_out = spawn([resolve(env)] + tail, cwd, env, text, timeout)
        obj = envelope(out)
        if rc in EXEC_FAILED and delays and obj is None:   # only when nothing ran
            d = delays.pop(0)
            wf.log("%s: claude could not be executed (exit %d); retrying in %ds" % (unit, rc, d))
            time.sleep(d)
            continue
        break
    rec["rc"], rec["seconds"] = rc, round(time.time() - t0, 1)
    if rc in EXEC_FAILED and obj is None:
        return ("unavailable", None, "claude could not be executed (exit %d): not found, or "
                "being replaced by an update (QWEN_CLAUDE_BIN or PATH)" % rc, rec)
    if timed_out:
        return "failed", None, "no answer within %ds" % timeout, rec
    if obj is None:                             # the CLI itself failed: no envelope at all
        seen = _brief((out or "") + " " + (err or "")) or "no output"
        blob = (out or "") + "\n" + (err or "")
        if first or AUTH_DOWN.search(blob) or NET_DOWN.search(blob):
            return "unavailable", None, "claude gave no readable answer (exit %s): %s" % (rc, seen), rec
        return "failed", None, "claude gave no readable answer (exit %s): %s" % (rc, seen), rec
    cost, turns = obj.get("total_cost_usd"), obj.get("num_turns")
    rec["cost_usd"] = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    rec["num_turns"] = turns if isinstance(turns, int) and not isinstance(turns, bool) else None
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    with contextlib.suppress(TypeError, ValueError):
        rec["tokens"] = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    res, sub = obj.get("result"), str(obj.get("subtype") or "")
    msg = res.strip() if isinstance(res, str) and res.strip() else (sub or "empty answer")
    # the per-item subtypes first: a turn- or budget-capped run failed this item only
    if "max_turns" in sub:
        return "failed", None, "stopped at the %d-turn limit" % max_turns, rec
    if "budget" in sub:
        return "failed", None, "stopped at the $%g budget" % budget_usd, rec
    # A real offline run exits 1 with a PARSEABLE envelope (result "API Error: ...
    # ECONNREFUSED"), so NET_DOWN's nothing-parsed ground never holds. The CLI's own
    # fields -- which page text cannot set -- say the CLI failed: terminal_reason
    # "api_error" triggers on its own, without is_error; an "API Error" result only
    # when duration_api_ms is 0 (no API response at all). A usage limit is the
    # item's own (by design): 429 or rate/usage-limit text stays failed.
    api_error = obj.get("terminal_reason") == "api_error" or (
        obj.get("is_error") and str(res).startswith("API Error")
        and obj.get("duration_api_ms") == 0)
    if api_error and obj.get("api_error_status") != 429 and not USAGE_LIMIT.search(msg):
        return "unavailable", None, "claude is not available: %s" % _brief(msg), rec
    if obj.get("is_error") or sub not in ("", "success") or not isinstance(res, str):
        # then the CLI's own login failure; NET_DOWN is never looked at here --
        # inside an envelope those words are the page's.
        if AUTH_DOWN.search(msg):
            return "unavailable", None, "claude is not available: %s" % _brief(msg), rec
        return "failed", None, "claude reported an error: %s" % _brief(msg), rec
    try:
        data = parse(res, item)
    except ValueError as e:
        return "failed", None, "unusable answer: %s" % _brief(e), rec
    return "ok", data, "", rec


def record(wf, name, iid, model, state, why, rec):
    """The call's line in RUN/claude/calls.jsonl and run.log, its claude_call event, and
    the counts totals.json adds up."""
    d = wf.run_dir / "claude"
    d.mkdir(parents=True, exist_ok=True)
    line = {"name": name, "item": iid, "model": model, "seconds": rec["seconds"],
            "cost_usd": rec["cost_usd"], "num_turns": rec["num_turns"], "state": state,
            "why": why}
    with open(d / "calls.jsonl", "a", encoding="utf-8", errors="replace") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    wf._log_line(["%s-%s" % (name, safe_id(iid)), ROLE,
                  "-" if rec["rc"] is None else str(rec["rc"]), str(rec["tokens"]),
                  "%.0f" % rec["seconds"], state + (": " + " ".join(why.split()) if why else "")])
    wf.claude_calls += 1
    if rec["cost_usd"] is not None:
        wf.claude_cost_usd += rec["cost_usd"]
    events.emit(wf.run_dir, "claude_call", name=name, item=iid, state=state,
                seconds=rec["seconds"], cost_usd=rec["cost_usd"])


def _dry(wf, text, item, parse):
    answer = getattr(wf.sw, "answer", None)
    if not answer:
        return "unavailable", None, "check: no claude in a dry run"
    try:
        return "ok", parse(answer(ROLE, text), item), ""
    except ValueError as e:
        return "unavailable", None, "check: the dry-run answer does not parse: %s" % _brief(e)


def _result(item, state, data, why):
    return {"item": item, "state": state, "data": data, "why": why}


def run(wf, name, items, prompt, parse, *, model, max_calls, budget_usd=2.0, browser=False,
        stage=None, read_dirs=(), item_id=None, timeout=600, max_turns=40):
    """What wf.claude_check does; one {"item", "state", "data", "why"} per item, in order."""
    from lib.swarm_engine import api
    key = "claude-%s" % name
    wf._check_key(key)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("claude_check needs a model (opus, sonnet or a model id)")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 0:
        raise ValueError("claude_check max_calls must be a whole number >= 0 (got %r)" % (max_calls,))
    ident = item_id or api._default_id
    items = list(items)
    ids = [str(ident(it)) for it in items]
    seen = set()
    for iid in ids:
        if safe_id(iid).lower() in seen:
            raise ValueError("claude_check: two items share the id %r (case and unsafe "
                             "characters aside)" % iid)
        seen.add(safe_id(iid).lower())
    if stage is not None:
        if not os.path.isdir(str(stage)):
            raise ValueError("claude_check stage=%r is not a folder" % (str(stage),))
        total = staging.size(str(stage))
        if total > staging.MAX_BYTES:
            raise ValueError("claude_check stage=%r holds %.1f MB, over the limit of %d MB"
                             % (str(stage), total / 1048576.0, staging.MAX_BYTES // 1048576))
    stage_sha = wf.stage_digest(str(stage)) if stage is not None else ""   # staging's walk
    env = advisor_mcp.clean_env(os.environ)
    cache = wf.load(key)
    cache = cache if isinstance(cache, dict) else {}
    opts = dict(model=model, budget_usd=budget_usd, browser=browser, stage=stage,
                read_dirs=tuple(read_dirs), timeout=timeout, max_turns=max_turns, env=env)
    out, made, down = [], 0, None
    probed = False
    for it, iid in zip(items, ids):
        text = prompt(it)
        k = hashlib.sha256(json.dumps([text, model, bool(browser), stage_sha])
                           .encode("utf-8")).hexdigest()
        wf.calls.append(("claude", "%s-%s" % (name, iid), ROLE, k))
        hit = cache.get(iid)
        if isinstance(hit, dict) and hit.get("key") == k and "data" in hit:
            out.append(_result(it, "ok", copy.deepcopy(hit["data"]), "cached"))
        elif down is not None:
            out.append(_result(it, "unavailable", None, down))
        elif wf._past_deadline():
            wf._log_deadline("%s-%s" % (name, safe_id(iid)), ROLE, iid)
            out.append(_result(it, "deadline", None, "the run's deadline passed before this call"))
        elif made >= max_calls:
            out.append(_result(it, "over_cap", None, "over the cap of %d calls" % max_calls))
        elif wf.check is not None:
            made += 1
            out.append(_result(it, *_dry(wf, text, it, parse)))
        else:
            if not probed:                             # the first real call of this run()
                probed = True                          # invocation: probe asks first, once
                p_state, p_why, p_kind = probe(env)
                # A "binary" unavailable pre-trips nothing: a claude mid-auto-update is
                # 126/127 for exactly a moment, which the retry inside call() exists to
                # ride out -- so the call decides that one, with no breaker and no event.
                if p_state == "unavailable" and p_kind == PROBE_BINARY:
                    wf.log("claude-check: the probe could not run claude (%s); the call "
                           "and its retry decide" % p_why)
                elif p_state == "unavailable":         # the login or the route to the API
                    down = "claude is not available: " + p_why
                    wf.log("claude-check: probe says claude is not available (%s); "
                           "no calls made" % p_why)
                    events.emit(wf.run_dir, "claude_probe", name=name, state="unavailable",
                                why=p_why)             # wf.event refuses the engine's kinds
            if down is not None:                       # the probe ended the run without a call
                out.append(_result(it, "unavailable", None, down))
                continue
            made += 1
            state, data, why, rec = call(wf, name, iid, text, it, parse, first=(made == 1), **opts)
            if state == "unavailable":
                down = why
            record(wf, name, iid, model, state, why, rec)   # first: a paid call is never lost
            if state == "ok":
                had, was = iid in cache, cache.get(iid)     # an entry under an earlier key?
                try:                              # the copy too: a lock in the data
                    cache[iid] = {"key": k, "data": copy.deepcopy(data)}
                    wf.save(key, cache)
                except (TypeError, ValueError):             # not JSON-serializable
                    if had:
                        cache[iid] = was                    # that paid answer stays cached
                    else:
                        cache.pop(iid, None)
                    wf.log("claude-check: the answer for %s was not cached "
                           "(not JSON-serializable)" % iid)
            out.append(_result(it, state, data, why))
    return out
