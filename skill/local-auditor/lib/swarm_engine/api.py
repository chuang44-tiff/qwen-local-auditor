"""The Workflow object a workflow script's run(wf) receives.

A workflow script states what to do; this object does it the way the released
qwen-deep-research did: units are named, dealt, waved, timed, fenced, cached and logged
exactly as its pipeline did, so a script only decides which calls to make. Scripts must not read the clock, randomness or
the environment: resume replays run(wf) from the start and relies on the same calls in
the same order (wf.rounds() owns time).
"""
import hashlib
import json
import math
import os
import pathlib
import re
import time

from lib import swarm
from lib.swarm_engine import fences, steps

MIN_UNIT_TIMEOUT = 300
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Empty(Exception):
    """wf.fail(): the workflow has nothing usable to report (exit 5)."""


class Result:
    """What wf.fan_out returns. rows: every parsed row of the units that succeeded, in
    deal order; dropped_items: items whose unit failed; not_run_items: items the deadline
    kept from starting (logged as 'deadline' in run.log, never a drop); units: the
    swarm's per-unit result dicts (name, ok, data, why, deadline, ...)."""

    def __init__(self, rows, dropped_items, not_run_items, units):
        self.rows, self.dropped_items = rows, dropped_items
        self.not_run_items, self.units = not_run_items, units

    @property
    def ok(self):
        """Every unit succeeded and no item was left unrun."""
        return not self.not_run_items and all(u["ok"] for u in self.units)


class Votes(dict):
    """What wf.vote returns: {claim_id: verdict}, plus .cast {claim_id: [vote rows]},
    .requested {claim_id: votes requested} and .result (the fan_out Result)."""


class Steps:
    """wf.steps: the steps module's functions, looked up at call time (so a patched
    module attribute is the one used), plus the bound run_cmd."""

    def __init__(self, wf):
        self._wf = wf

    def __getattr__(self, name):
        return getattr(steps, name)

    def run_cmd(self, cmd, patch=None, timeout=600):
        return self._wf._run_cmd(cmd, patch, timeout)


def _waves(items, ma, mi):
    """Consecutive in-order chunks of ma * mi items: (wave number, chunk)."""
    cap = ma * mi
    for w, i in enumerate(range(0, len(items), cap), 1):
        yield w, items[i:i + cap]


def _default_id(item):
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return str(item)


def _sha(text):
    # surrogateescape: a patch's bytes that are not valid UTF-8 ride through the engine
    # as lone surrogates (sandbox.diff), and valid text encodes byte-for-byte as before.
    return hashlib.sha256((text or "").encode("utf-8", "surrogateescape")).hexdigest()


class Workflow:
    def __init__(self, manifest, cfg, run, sw, *, goal, mcp=None, web_seats=None,
                 start=None, keep_sandboxes=False, check=None):
        self.manifest, self.cfg, self.run_dir, self.sw = manifest, cfg, pathlib.Path(run), sw
        self.goal = goal
        self.target = pathlib.Path(cfg["target"]) if cfg.get("target") else None
        self.mcp, self.web_seats = mcp, web_seats
        self.start = time.time() if start is None else start
        self.keep_sandboxes = keep_sandboxes
        self.check = check                # None, or the --check fake (answers and run_cmd)
        self.steps = Steps(self)
        self.round = 1
        self.not_run = 0                  # items the deadline kept from starting, all phases
        self.unmet = None                 # wf.goal_unmet(reason): finished without its goal
        self.report_path = None
        self.last_unit = None             # the swarm result dict of the last wf.agent call
        self.last_totals = None
        self.stop_reason = None
        self.calls = []                   # (kind, name, role, sha) per call: --check compares
        self._in_loop = False
        self._converged = None
        totals = self._load_path(self.run_dir / "totals.json")
        self._baseline = totals if isinstance(totals, dict) else {}
        self._target_fp = None

    # ------------------------------------------------------------ settings
    def knob(self, name):
        """A declared knob, or an engine knob: budget, retries, rounds, hours."""
        if name in self.manifest.knobs:
            return self.cfg[name]
        engine = {"budget": "timeout_per_item", "retries": "retries", "rounds": "rounds",
                  "hours": "hours"}
        if name in engine:
            return self.cfg.get(engine[name])
        raise KeyError("no knob %r in workflow %s" % (name, self.manifest.name))

    @property
    def multi_round(self):
        return self.cfg.get("rounds", 1) != 1

    @property
    def dropped(self):
        return self.sw.dropped

    def _role(self, role):
        try:
            return self.manifest.roles[role]
        except KeyError:
            raise ValueError("no role %r in workflow %s" % (role, self.manifest.name)) from None

    def _effort(self, role):
        """--role-effort beats --effort beats the manifest's role effort; else None."""
        per_role = self.cfg.get("role_effort") or {}
        return per_role.get(role) or self.cfg.get("effort") or self._role(role).effort or None

    def _timeout(self, role, items):
        per_item = self.cfg["timeout_per_item"]
        return max(MIN_UNIT_TIMEOUT, int(math.ceil(self._role(role).budget_weight * items * per_item)))

    def _seats(self, role):
        return self.web_seats if self._role(role).fence in fences.WEB_FENCES else None

    def _unit_name(self, name, k, wave=1):
        base = "%s-%d" % (name, k) if wave == 1 else "%s-w%d-%d" % (name, wave, k)
        return ("r%d-" % self.round if self.round > 1 else "") + base

    def _past_deadline(self):
        return self.sw.deadline is not None and time.time() >= self.sw.deadline

    # ------------------------------------------------------------ run.log
    def _log_line(self, cols):
        # errors="replace": a workflow line may quote a patch carrying a non-UTF-8 byte
        # (a lone surrogate), and run.log must hold the line rather than raise.
        with open(self.run_dir / "run.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write("\t".join(cols) + "\n")

    def _log_deadline(self, name, role, item):
        """One run.log line for an item whose unit never started after the deadline."""
        self._log_line([name, role, "-", "0", "0", "deadline: %s not started" % item])

    def log(self, msg):
        """A workflow line in run.log (name '-', role 'workflow')."""
        self._log_line(["-", "workflow", "-", "0", "0", " ".join(str(msg).split())])

    # ------------------------------------------------------------ units
    def _unit(self, name, role, prompt, timeout, parse, batch, cache=True, always=False):
        r = self._role(role)
        f = fences.unit_fields(r.fence, self.mcp)
        u = swarm.Unit(name=name, role_file=r.file, prompt=prompt, parse=None, timeout=timeout,
                       retries=self.cfg.get("retries") or 0, effort=self._effort(role),
                       ignore_deadline=always, **f)
        u.cache = cache
        if r.fence == "read":
            u.cwd = self.target
            u.key_extra = "target:%s" % self._fingerprint()
        if r.fence == "sandbox":
            self._sandbox_unit(u)
            u.parse = self._sandbox_parse(u, parse, batch)
        else:
            u.parse = parse
        self.calls.append(("unit", name, role, _sha(prompt)))
        return u

    def agent(self, name, role, prompt, parse, *, cache=True, always=False, item="goal"):
        """One agent; returns parse(text) (parse(text, None, patch) for a sandbox role),
        or None when the unit failed or the deadline kept it from starting -- then
        wf.last_unit says which ("ok", "why", "deadline"). always=True runs it past the
        deadline; cache=False re-runs it on every resume."""
        uname = self._unit_name(name, 1)
        fence = self._role(role).fence
        wrapped = parse if fence != "sandbox" else (lambda text, batch, patch: parse(text, batch, patch))
        u = self._unit(uname, role, prompt, self._timeout(role, 2), wrapped, None, cache, always)
        (r,) = self._phase([u], role)
        self.last_unit = r
        if r["ok"]:
            return r["data"]
        if r.get("deadline"):
            self._log_deadline(uname, role, item)
            self.not_run += 1
        return None

    def fan_out(self, name, role, items, prompt, parse, *, item_id=None, max_items=None):
        """Deal `items` over at most --max-agents agents in waves of --max-agents x
        --max-items (max_items= lowers the per-agent cap for this call). prompt(batch) ->
        str; parse(text, batch) -> list of rows (parse(text, batch, patch) for a sandbox
        role). item_id(item) names an item in run.log's deadline lines (default: its
        "id")."""
        ma, mi = self.cfg["max_agents"], self.cfg["max_items"]
        if max_items is not None:
            mi = max(1, min(mi, max_items))
        ident = item_id or _default_id
        rows, units_res, dropped, not_run = [], [], [], []
        for w, chunk in _waves(list(items), ma, mi):
            batches = list(swarm.deal(chunk, ma))
            if self._past_deadline():
                for k, batch in enumerate(batches, 1):
                    for it in batch:
                        self._log_deadline(self._unit_name(name, k, w), role, ident(it))
                not_run += chunk
                continue
            units = []
            for k, batch in enumerate(batches, 1):
                p = self._bind(parse, batch, self._role(role).fence == "sandbox")
                units.append(self._unit(self._unit_name(name, k, w), role, prompt(batch),
                                        self._timeout(role, len(batch)), p, batch))
            wave_res = self._phase(units, role)
            units_res += wave_res
            for r, batch in zip(wave_res, batches):
                if r["ok"]:
                    rows += r["data"]
                elif r.get("deadline"):
                    for it in batch:
                        self._log_deadline(r["name"], role, ident(it))
                    not_run += batch
                else:
                    dropped += batch
        self.not_run += len(not_run)
        return Result(rows, dropped, not_run, units_res)

    @staticmethod
    def _bind(parse, batch, sandbox):
        if sandbox:
            return lambda text, _batch, patch: parse(text, batch, patch)
        return lambda text: parse(text, batch)

    def vote(self, name, role, claims, voters, prompt, parse, *, claim_id=None):
        """Claim-major vote slots (claim, k) fanned out; parse(text, batch) returns rows
        with "claim" (an id) and "verdict" (supported, refuted or unclear). A row naming a
        claim this vote does not know, or carrying no string "verdict", is ignored. Returns
        Votes {claim_id: majority-of-voters verdict} with .cast, .requested, .result."""
        cid = claim_id or (lambda c: c["id"])
        if voters > self.cfg["max_agents"]:
            raise ValueError("%d voters need --max-agents of at least %d" % (voters, voters))
        res = self.fan_out(name, role, steps.vote_slots(claims, voters), prompt, parse,
                           item_id=lambda s: cid(s[0]))
        cast = {cid(c): [] for c in claims}
        for v in res.rows:
            # a row naming a claim this vote never asked about, one with no "verdict" at
            # all and one whose verdict is not a string are all ignored the same way: the
            # claim keeps the votes it did get (which may still be too few: unclear).
            if v.get("claim") in cast and isinstance(v.get("verdict"), str):
                cast[v["claim"]].append(v)
        out = Votes((k, steps.tally([v["verdict"] for v in vs], voters)) for k, vs in cast.items())
        out.cast, out.requested, out.result = cast, {k: voters for k in cast}, res
        return out

    def _phase(self, units, role):
        return self.sw.run_phase(units, seats=self._seats(role))

    # ------------------------------------------------------------ artifacts
    def _dir(self):
        if self._in_loop and self.round > 1:
            d = self.run_dir / ("round-%d" % self.round)
            d.mkdir(parents=True, exist_ok=True)
            return d
        return self.run_dir

    @staticmethod
    def _check_key(key):
        if not _KEY.fullmatch(key or ""):
            raise ValueError("artifact key must match %s (got %r)" % (_KEY.pattern, key))

    @staticmethod
    def _load_path(path):
        # bytes in, decoded with surrogateescape: an artifact may hold a patch whose
        # bytes are not valid UTF-8, which this reads back as the same str it was written
        # from (and invalid JSON is still just an unreadable artifact).
        try:
            return json.loads(path.read_bytes().decode("utf-8", "surrogateescape"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save_path(path, data):
        # written as bytes so a patch's non-UTF-8 bytes survive instead of raising; bytes
        # also skip the newline translation a text write would do, so a patch keeps its
        # own line endings (on POSIX the bytes are exactly what write_text wrote).
        path.write_bytes(json.dumps(data, ensure_ascii=False, indent=1)
                         .encode("utf-8", "surrogateescape"))

    def save(self, key, data):
        """<key>.json in the run folder (round-<r>/ from round 2 of a rounds loop)."""
        self._check_key(key)
        self._save_path(self._dir() / ("%s.json" % key), data)

    def load(self, key):
        """The saved <key>.json, or None when it is missing or unreadable."""
        self._check_key(key)
        return self._load_path(self._dir() / ("%s.json" % key))

    def exists(self, key):
        self._check_key(key)
        return (self._dir() / ("%s.json" % key)).exists()

    def forget(self, *keys):
        """Delete saved artifacts (a phase re-ran, so its later phases are stale)."""
        for key in keys:
            self._check_key(key)
            try:
                (self._dir() / ("%s.json" % key)).unlink()
            except OSError:
                pass

    def write(self, relpath, text):
        """A text file at relpath inside the run folder (e.g. patches/1.diff). Written
        as bytes through utf-8/surrogateescape, so a patch's exact bytes survive."""
        p = pathlib.PurePosixPath(str(relpath).replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.parts or re.match(r"^[A-Za-z]:", str(relpath)):
            raise ValueError("wf.write needs a relative path inside the run folder (got %r)" % (relpath,))
        path = self.run_dir.joinpath(*p.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8", "surrogateescape"))
        return path

    def report(self, markdown):
        """Write report.md (and report-round-<r>.md inside the rounds loop of a
        multi-round run). The runner prints report.md's path as the last stdout line."""
        path = self.run_dir / "report.md"
        path.write_text(markdown, encoding="utf-8")
        if self._in_loop and self.multi_round:
            (self.run_dir / ("report-round-%d.md" % self.round)).write_text(markdown, encoding="utf-8")
        self.report_path = path
        return path

    def totals(self):
        """The run's cumulative totals (every invocation so far plus this one), written
        to totals.json: agents_run, tokens, seconds, invocations."""
        wall = int(time.time() - self.start)

        def added(key, value):
            prev = self._baseline.get(key)
            prev = prev if isinstance(prev, int) and not isinstance(prev, bool) else 0
            return prev + value

        cum = {k: added(k, v) for k, v in (("agents_run", self.sw.agents_run),
                                           ("tokens", self.sw.tokens),
                                           ("seconds", wall), ("invocations", 1))}
        self._save_path(self.run_dir / "totals.json", cum)
        self.last_totals = cum
        return cum

    def fail(self, message):
        """Stop: nothing usable (exit 5; the run folder's path is the last stdout line)."""
        raise Empty(message)

    def goal_unmet(self, reason):
        """The workflow finished without its goal (exit 4 once a report is written)."""
        if self.unmet is None:
            self.unmet = str(reason)

    # ------------------------------------------------------------ rounds
    def rounds(self):
        """Yield round numbers 1, 2, ... Stops before round r > 1 when r exceeds the
        rounds knob, when wf.converged() was called in round r-1, or when the run's
        deadline has passed. Round 1 always runs. Multi-round runs record started rounds
        in rounds.json, so a resume re-enters an interrupted round instead of re-deciding
        it against the clock."""
        cap = self.cfg.get("rounds", 1)
        multi = self.multi_round
        started = []
        if multi:
            data = self._load_path(self.run_dir / "rounds.json")
            started = [r for r in data if isinstance(r, int)] if isinstance(data, list) else []
        r = 0
        self._converged = None
        try:
            while True:
                r += 1
                if r > 1 and self._converged is not None:
                    self.stop_reason = "converged: %s" % self._converged
                    return
                if cap != "until" and r > cap:
                    self.stop_reason = "rounds"
                    return
                if r > 1 and r not in started and self._past_deadline():
                    self.stop_reason = "deadline" if self.not_run else "hours"
                    return
                self._converged = None
                self.round, self._in_loop = r, True
                if multi and r not in started:
                    started.append(r)
                    self._save_path(self.run_dir / "rounds.json", started)
                yield r
        finally:
            self.round, self._in_loop = 1, False

    def converged(self, reason):
        """End the rounds loop after the current round (the first reason wins)."""
        if self._converged is None:
            self._converged = str(reason)

    # ------------------------------------------------------------ sandboxes and commands
    def _fingerprint(self):
        if self._target_fp is None:
            if self.check is not None or self.target is None:
                self._target_fp = "none"
            else:
                from lib.swarm_engine import sandbox
                self._target_fp = sandbox.fingerprint(self.target)
        return self._target_fp

    def _sandbox_unit(self, u):
        """Point a sandbox-fenced unit at its own throwaway copy of --target."""
        path = self.run_dir / "sandboxes" / u.name
        u.cwd = path
        u.key_extra = "target:%s" % self._fingerprint()
        if self.check is not None:
            return
        from lib.swarm_engine import sandbox
        target, keep = self.target, self.keep_sandboxes
        u.setup = lambda: sandbox.create(target, path)
        u.teardown = (lambda: None) if keep else (lambda: sandbox.remove(target, path))

    def _sandbox_parse(self, u, parse, batch):
        """parse(text, batch, patch) with the sandbox's diff (new files included), also
        saved to agents/<unit>/patch.diff (as bytes: the patch's exact bytes must
        survive, so it is encoded with surrogateescape)."""
        def p(text):
            if self.check is not None:
                patch = self.check.patch(pathlib.Path(u.role_file).stem, u.prompt)
            else:
                from lib.swarm_engine import sandbox
                patch = sandbox.diff(u.cwd)
                (self.sw.agents_dir / u.name).mkdir(parents=True, exist_ok=True)
                (self.sw.agents_dir / u.name / "patch.diff").write_bytes(
                    patch.encode("utf-8", "surrogateescape"))
            return parse(text, batch, patch)
        return p

    def _run_cmd(self, cmd, patch, timeout):
        """steps.run_cmd: `bash -c cmd` in a fresh sandbox of --target with `patch`
        applied first; cached by (cmd, patch, target fingerprint)."""
        if self.target is None and self.check is None:
            raise ValueError("run_cmd needs a workflow with \"target\": \"required\"")
        key = _sha("%s\n%s\n%s" % (cmd, _sha(patch or ""), self._fingerprint()))
        self.calls.append(("cmd", "cmd-%s" % key[:12], "-", key))
        if self.check is not None:
            return dict(self.check.run_cmd(cmd, patch))
        cache = self.run_dir / "cmds" / ("%s.json" % key)
        hit = self._load_path(cache)
        if isinstance(hit, dict) and {"applied", "rc", "timed_out", "output_tail"} <= set(hit):
            return hit
        from lib.swarm_engine import sandbox
        t0 = time.time()
        res = sandbox.run_cmd(self.target, self.run_dir / "sandboxes" / ("cmd-%s" % key[:12]),
                              cmd, patch=patch, timeout=timeout)
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        self._save_path(tmp, res)
        os.replace(tmp, cache)
        status = ("patch did not apply" if not res["applied"] else
                  "timed out" if res["timed_out"] else "ok")
        self._log_line(["cmd-%s" % key[:12], "run_cmd", str(res["rc"]), "0",
                        "%.0f" % (time.time() - t0), status])
        return dict(res)
