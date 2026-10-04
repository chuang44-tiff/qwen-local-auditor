"""A seat-limited swarm of qwen-agent sessions.

Work is dealt to at most max_agents agents per phase (deal); never more than `seats`
qwen-agent processes are alive at once; each agent answers with one fenced JSON block,
gets one repair round (resuming its own session) when that block does not parse, and
leaves everything in the run folder so an interrupted run resumes where it stopped.
A unit whose last failure was a timeout (exit 5), a server error (exit 3 or 4) that
survived its backoff, an empty result (exit 6, including on its repair call), or an
answer still unusable after its repair round is run again up to `retries` times, each
try with double the previous timeout (every timeout capped at MAX_UNIT_SECONDS); any
other exit drops the unit at once. Every spawn's token usage counts, including a
re-spawn after a server error. An absolute `deadline` stops new units from starting:
a unit whose session would begin after it is returned as never started (not dropped)
and the caller records its work.
Role-agnostic: research.py (web) is the first user.
On Windows stop_all can only kill the direct child process; Ctrl-C reaches the agents
through the shared console.
"""
import contextlib
import hashlib
import json
import os
import pathlib
import queue
import re
import signal
import subprocess
import threading
import time

AGENT_PREFLIGHT, AGENT_APIERR = 3, 4
AGENT_TIMEOUT = 5
# The ceiling on any unit's --timeout, retry doublings included: a unit never runs
# longer than this. research.py reads QWEN_DR_MAX_UNIT_SECONDS into the Swarm.
MAX_UNIT_SECONDS = 14400
STOP_GRACE = 20
REPAIR = ("Your last answer could not be used: {why}\n\n"
          "Reply again with ONLY the corrected answer as one ```json fenced block, "
          "following the format you were given.")
_FENCE_JSON = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S | re.I)
_FENCE_ANY = re.compile(r"```[^\n]*\r?\n(.*?)```", re.S)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def deal(items, max_agents):
    if max_agents < 1:
        raise ValueError("max_agents must be at least 1")
    a = min(len(items), max_agents)
    return [items[i::a] for i in range(a)]


def extract_json(text):
    text = text or ""
    blocks = _FENCE_JSON.findall(text) or _FENCE_ANY.findall(text)
    candidate = blocks[-1] if blocks else text.strip()
    if not candidate.strip():
        raise ValueError("no JSON block found")
    try:
        return json.loads(candidate)
    except (ValueError, RecursionError) as e:
        # Answers must be JSON: no Python-literal fallback, and json.loads itself
        # blows its stack (RecursionError) on deeply nested input. Either way the
        # answer is simply unparseable.
        raise ValueError("the JSON block does not parse (%s)" % e)


def tally(verdicts, voters):
    need = 1 if voters == 1 else voters // 2 + 1
    if verdicts.count("refuted") >= need:
        return "refuted"
    if verdicts.count("supported") >= need:
        return "supported"
    return "unclear"


def _term_group(p):
    """Ask one live process to stop (SIGTERM to its group; POSIX only, like stop_all)."""
    if p.poll() is None and os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGTERM)


def _kill_group(p):
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
    else:
        p.kill()


def _stop_handler(signum, frame):
    raise KeyboardInterrupt


def install_stop_signals():
    """Make SIGTERM and SIGHUP stop the swarm the same way Ctrl-C does; main thread only.

    Returns the previous handlers as {signum: handler} ({} off the main thread); callers
    that want the old behaviour back may restore them with signal.signal(signum, handler).
    """
    if threading.current_thread() is not threading.main_thread():
        return {}
    prev = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            prev[sig] = signal.signal(sig, _stop_handler)
    return prev


class Unit:
    def __init__(self, name, role_file, prompt, toolset="none", grants="", web=False,
                 mcp_config=None, parse=extract_json, cache=True, timeout=None, retries=0,
                 effort=None, ignore_deadline=False):
        # fullmatch, not match: "a\n" must not pass on the strength of the trailing $.
        if not _SAFE_NAME.fullmatch(name or ""):
            raise ValueError("unit name must match %s (got %r)" % (_SAFE_NAME.pattern, name))
        if (name or "").endswith(".repair"):
            raise ValueError("unit name must not end in '.repair' (it would collide with "
                             "another unit's repair files): %r" % name)
        self.name, self.role_file, self.prompt = name, role_file, prompt
        self.toolset, self.grants, self.web = toolset, grants, web
        self.mcp_config, self.parse, self.cache = mcp_config, parse, cache
        self.timeout = timeout      # seconds for this one unit; None = the Swarm's timeout
        self.retries = retries      # extra attempts for a failed unit (timeout, server error,
                                    # an empty result, or an answer still unusable after its
                                    # repair round)
        self.effort = effort        # qwen-agent -e LEVEL; None = qwen-agent's own default
        # the one unit a passed deadline must not stop: the caller always runs it
        self.ignore_deadline = ignore_deadline


class Swarm:
    def __init__(self, agent_cmd, run_dir, seats, timeout, backoff=30,
                 max_unit_seconds=MAX_UNIT_SECONDS, deadline=None):
        if seats < 1:
            raise ValueError("seats must be at least 1")
        self.agent_cmd, self.run_dir = list(agent_cmd), pathlib.Path(run_dir)
        self.seats, self.timeout, self.backoff = seats, timeout, backoff
        self.max_unit_seconds = max_unit_seconds    # no unit's --timeout ever exceeds this
        # absolute epoch seconds after which no new unit starts (None = no deadline);
        # running units finish, never-started ones carry "deadline" in their result
        self.deadline = deadline
        self.agents_dir = self.run_dir / "agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.dropped = self.agents_run = self.tokens = 0
        self._lock = threading.Lock()
        self._live = set()
        self._stopping = threading.Event()

    # ------------------------------------------------------------ one process
    def _argv(self, u, prompt_path, timeout, resume=None):
        # the caller owns the budget: a unit carries its own timeout, and a retry doubles it
        argv = self.agent_cmd + ["--json", "-q", "--warn-denials",
                                 "--role-file", str(u.role_file),
                                 "--toolset", u.toolset or "none",
                                 "--permission-mode", "dontAsk", "--timeout", str(timeout),
                                 "-C", str(self.agents_dir / u.name), "-f", str(prompt_path)]
        if u.effort:
            argv += ["-e", u.effort]
        if u.grants:
            argv += ["-t", u.grants]
        if u.web:
            argv.append("--web")
        if u.mcp_config:
            argv += ["--mcp-config", str(u.mcp_config)]
        if resume:
            argv += ["--resume", resume]
        return argv

    def _spawn(self, argv):
        posix = os.name == "posix"
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                             **({"start_new_session": True} if posix else {}))
        with self._lock:
            self._live.add(p)
            stopping = self._stopping.is_set()
        if stopping:
            # stop_all snapshotted _live before we registered; never leave an orphan,
            # and kill it outright: nobody is left to escalate a SIGTERM in this race.
            _kill_group(p)
        try:
            out, err = p.communicate()
        finally:
            with self._lock:
                self._live.discard(p)
        return p.returncode, out or "", err or ""

    def _call(self, argv):
        # both spawns' usage counts when an exit 3/4 is re-spawned after the backoff:
        # the first spawn spent those tokens, whether or not its answer survived
        rc, out, err, tokens = -1, "", "", 0
        for attempt in (0, 1):
            if self._stopping.is_set():
                # the same well-formed record shape a real call returns: the caller
                # reads rec["tokens"]/rec["result"] without guarding for a bare string
                return -1, {"result": "", "session": "", "tokens": tokens}, "interrupted"
            rc, out, err = self._spawn(argv)
            try:
                rec = json.loads(out) if out.strip() else {}
            except ValueError:
                rec = {}
            if not isinstance(rec, dict):
                rec = {}
            u = rec.get("usage") or {}
            tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
            if rc not in (AGENT_PREFLIGHT, AGENT_APIERR) or attempt == 1:
                break
            time.sleep(self.backoff)
        result = rec.get("result") if isinstance(rec.get("result"), str) else ""
        return rc, {"result": result, "session": rec.get("session_id") or "", "tokens": tokens}, err

    def stop_all(self):
        self._stopping.set()
        with self._lock:
            live = list(self._live)
        deadline = time.time() + STOP_GRACE
        try:
            for p in live:
                _term_group(p)
            for p in live:
                try:
                    p.wait(timeout=max(0.0, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    _kill_group(p)      # the whole group, not just the leader
        except KeyboardInterrupt:
            # A second Ctrl-C must not skip the kill.
            for p in live:
                _kill_group(p)
            raise
        if os.name == "posix":
            # A grandchild that outlived its leader must not survive the stop either.
            for p in live:
                with contextlib.suppress(OSError):
                    os.killpg(p.pid, signal.SIGKILL)

    # ------------------------------------------------------------ one unit
    def _log(self, u, rc, tokens, secs, status):
        line = "\t".join([u.name, pathlib.Path(u.role_file).stem, str(rc), str(tokens),
                          "%.0f" % secs, status])
        with self._lock:
            with open(self.run_dir / "run.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _key(self, u):
        # Over the role file's CONTENTS, not its stem: editing a role must invalidate
        # the cached answer, and so must widening a unit's tools or web access. The
        # mcp_config file's contents too -- pointing a unit at a different (or changed)
        # server list changes what its answer could mean. The effort too: an answer
        # earned at one reasoning effort is not the answer asked for at another.
        role_text = pathlib.Path(u.role_file).read_text(encoding="utf-8")
        mcp_text = "" if u.mcp_config is None else pathlib.Path(u.mcp_config).read_text(encoding="utf-8")
        blob = "%s\n%s\n%s\n%s\n%s\n%s\n%s" % (role_text, u.prompt, u.toolset, u.grants,
                                               u.web, mcp_text, u.effort or "")
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cached(self, u):
        """Return (found, data); found is True only when the cached key still matches."""
        try:
            rec = json.loads((self.agents_dir / ("%s.json" % u.name)).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False, None
        if not isinstance(rec, dict) or "data" not in rec or rec.get("key") != self._key(u):
            return False, None
        return True, rec["data"]

    def run_unit(self, u):
        res = {"name": u.name, "ok": False, "data": None, "why": "", "tokens": 0,
               "seconds": 0.0, "cached": False, "deadline": False}
        if u.cache:
            found, data = self._cached(u)
            if found:
                # a finished unit is never retried: its answer is already in hand
                res.update(ok=True, data=data, cached=True)
                self._log(u, 0, 0, 0, "cached")
                return res
        if self.deadline is not None and not u.ignore_deadline and time.time() >= self.deadline:
            # the run's deadline has passed: this unit never starts -- no session, no
            # files, not a drop. The caller records its work and logs the deadline line
            # (it knows the items); the phase's file stays unwritten so a resume can run it.
            res.update(why="deadline reached before this unit started", deadline=True)
            return res
        start = time.time()
        rc, tokens, status = -1, 0, "ok"
        # the last attempt's start and token boundary: the unit's final log line reports
        # that attempt alone, so the token column of run.log sums to self.tokens
        t_start, tokens_before = start, 0
        timeout = min(self.timeout if u.timeout is None else u.timeout, self.max_unit_seconds)
        try:
            (self.agents_dir / u.name).mkdir(parents=True, exist_ok=True)
            prompt_path = self.agents_dir / ("%s.prompt.md" % u.name)
            prompt_path.write_text(u.prompt, encoding="utf-8")
            why, data, attempt, retryable = "", None, 0, False
            while True:
                for suffix in (".out", ".repair.out"):    # each attempt starts from clean files
                    with contextlib.suppress(OSError):
                        os.unlink(self.agents_dir / ("%s%s" % (u.name, suffix)))
                t_start, tokens_before = time.time(), tokens
                rc, rec, err = self._call(self._argv(u, prompt_path, timeout))
                tokens += rec["tokens"]
                status = "ok"
                with self._lock:
                    self.agents_run += 1
                if rc not in (0, 6):       # 6 = ran clean but the result was empty
                    why = "qwen-agent exit %d: %s" % (rc, (err.strip().splitlines() or [""])[0])
                    # only a timeout or a server error that survived the backoff inside
                    # the call can retrying fix: any other exit (usage, denied, signal)
                    # is the same answer the next time
                    retryable = rc == AGENT_TIMEOUT or rc in (AGENT_PREFLIGHT, AGENT_APIERR)
                else:
                    (self.agents_dir / ("%s.out" % u.name)).write_text(rec["result"], encoding="utf-8")
                    try:
                        if rc == 6:
                            raise ValueError("qwen-agent ran clean but returned an empty result")
                        data = u.parse(rec["result"])
                    except ValueError as e:
                        if not rec["session"]:
                            why = "answer unusable and no session to repair: %s" % e
                            retryable = True
                        else:
                            repair = self.agents_dir / ("%s.repair.md" % u.name)
                            repair.write_text(REPAIR.format(why=e), encoding="utf-8")
                            # the repair keeps this attempt's timeout: it is a
                            # continuation of the attempt, not a new one
                            rc, rec2, err = self._call(self._argv(u, repair, timeout, resume=rec["session"]))
                            tokens += rec2["tokens"]   # on any rc, count the repair record's usage
                            if rc == 0:
                                (self.agents_dir / ("%s.repair.out" % u.name)).write_text(
                                    rec2["result"], encoding="utf-8")
                                try:
                                    data, status = u.parse(rec2["result"]), "repaired"
                                except ValueError as e2:
                                    why = "answer still unusable after one repair: %s" % e2
                                    retryable = True
                            else:
                                why = "qwen-agent exit %d on repair: %s" % (rc, (err.strip().splitlines() or [""])[0])
                                # exit 6 = the repair answered empty: same unusable answer
                                # as a failed parse, so it buys a retry like one does
                                retryable = (rc == AGENT_TIMEOUT or rc in (AGENT_PREFLIGHT,
                                                                           AGENT_APIERR, 6))
                # only a timeout, a server error that survived its backoff, an empty
                # result or an answer still unusable after its repair round buys another
                # try — never any other exit, never when the swarm is stopping, and
                # never more than u.retries of them, each with double the timeout (up
                # to the cap): a model that needed 5 minutes may need 10
                if not why or self._stopping.is_set() or not retryable or attempt >= u.retries:
                    break
                # a retry is new work: past the run's deadline the unit stays dropped
                # (a repair call, by contrast, continues the attempt and still runs)
                if self.deadline is not None and not u.ignore_deadline and time.time() >= self.deadline:
                    break
                attempt += 1
                timeout = min(timeout * 2, self.max_unit_seconds)
                # every retried attempt gets its own line, reporting its own tokens and
                # seconds; only the last failure is a drop
                self._log(u, rc, tokens - tokens_before, time.time() - t_start,
                          "retry %d: %s" % (attempt, why))
                why, data, retryable = "", None, False
            if why:
                res["why"] = why
                if self._stopping.is_set():
                    # the swarm was told to stop: this unit is interrupted, not dropped —
                    # it never got the chance to finish, and it must not inflate the
                    # exit-4 "agents dropped" count of a run that was stopped, not failing
                    status = "interrupted"
                else:
                    with self._lock:
                        self.dropped += 1
            else:
                out_path = self.agents_dir / ("%s.json" % u.name)
                tmp_path = self.agents_dir / ("%s.json.tmp" % u.name)
                tmp_path.write_text(json.dumps({"data": data, "key": self._key(u)},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp_path, out_path)
                res.update(ok=True, data=data)
        except Exception as e:      # a crashing parse still leaves the unit logged below
            res["why"] = "internal error: %s" % type(e).__name__
            raise
        finally:
            # Tokens already spent by this unit reach self.tokens on every exit path,
            # including one where u.parse raises a non-ValueError. Its log line reports
            # only the last attempt — the earlier ones wrote their own retry lines, so
            # the token column of run.log sums to the Swarm's tokens total.
            secs = time.time() - start
            res.update(tokens=tokens, seconds=secs)
            with self._lock:
                self.tokens += tokens
            self._log(u, rc, tokens - tokens_before, time.time() - t_start,
                      "interrupted" if status == "interrupted"
                      else status if not res["why"] else "dropped: %s" % res["why"])
        return res

    # ------------------------------------------------------------ one phase
    def run_phase(self, units, seats=None):
        """Run every unit; at most `seats` at once. None = the Swarm's seats; a value
        above them is capped to the Swarm's seats."""
        if self._stopping.is_set():
            raise KeyboardInterrupt
        seats = self.seats if seats is None else min(seats, self.seats)
        names = [u.name for u in units]
        if len(set(names)) != len(names):
            raise ValueError("two units in one phase share a name: they would overwrite "
                             "each other's files")
        results = [None] * len(units)
        q = queue.Queue()
        for i, u in enumerate(units):
            q.put((i, u))

        def worker():
            while not self._stopping.is_set():
                try:
                    i, u = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    results[i] = self.run_unit(u)
                except Exception as e:      # a crashing parse must not kill the worker
                    res = {"name": u.name, "ok": False, "data": None,
                           "why": "internal error: %s" % type(e).__name__,
                           "tokens": 0, "seconds": 0.0, "cached": False, "deadline": False}
                    with self._lock:
                        self.dropped += 1
                    # run_unit's finally already logged this unit's line with its tokens.
                    results[i] = res

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(seats, len(units)))]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(0.2)       # short joins keep Ctrl-C deliverable
        except KeyboardInterrupt:
            self.stop_all()
            raise
        if self._stopping.is_set():
            raise KeyboardInterrupt   # stopped after stop_all: results are not complete
        return results
