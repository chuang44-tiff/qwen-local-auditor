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
import threading
import time

from lib import swarm
from lib.swarm_engine import events, fences, manifest, staging, steps

MIN_UNIT_TIMEOUT = 300
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNIT_ID = re.compile(r"[A-Za-z0-9._-]+")   # an item id fan_out(unit_ids=True) names a unit by


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


def atomic_write(path, data):
    """Write `data` (bytes) to `path` through a temp file in the same folder and
    os.replace: a reader -- another process, or a crash halfway -- sees the old file or
    the new one, never a torn one. The temp name carries the pid and thread so two
    writers never share it; it starts with "." so no "*.json" glob ever lists it."""
    path = pathlib.Path(path)
    tmp = path.with_name(".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        self.claude_calls = 0             # wf.claude_check calls made this invocation
        self.claude_cost_usd = 0.0        # and what claude reported they cost
        self._in_loop = False
        self._converged = None
        totals = self._load_path(self.run_dir / "totals.json")
        self._baseline = totals if isinstance(totals, dict) else {}
        self._target_fp = None
        self._stage_shas = {}             # fixtures folder -> its staging.digest, once per run

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

    def _role_list(self, key):
        """A stored --deep/--shallow role list. Only a list counts: a hand-edited string
        in config.json must not match role names as substrings."""
        names = self.cfg.get(key)
        return names if isinstance(names, list) else ()

    def _deep(self, role):
        """--shallow ROLE forces the opt-out, --deep ROLE (all switches) forces all, and
        otherwise the manifest's role "deep" (default: all); a tuple of names. A
        fence-none role has no tools to delegate with, so it gets the subagent
        switches only when its own "deep" list names them."""
        if role in self._role_list("shallow"):
            return ()
        r = self._role(role)
        switches = manifest.DEEP_SWITCHES if role in self._role_list("deep") else r.deep
        if r.fence == "none":
            listed = r.deep if r.deep_listed else ()
            if not any(s.startswith("subagents") for s in listed):
                switches = tuple(s for s in switches if not s.startswith("subagents"))
        return switches

    def _timeout(self, role, items):
        per_item = self.cfg["timeout_per_item"]
        return max(MIN_UNIT_TIMEOUT, int(math.ceil(self._role(role).budget_weight * items * per_item)))

    def _seats(self, role):
        return self.web_seats if self._role(role).fence in fences.WEB_FENCES else None

    def _unit_name(self, name, k, wave=1, item=None):
        """name-k (wave 1), name-wW-k (later waves) -- or name-<item> when `item` (an item
        id, fan_out(unit_ids=True)) is given; r<round>- in front from round 2 on."""
        if item is not None:
            base = "%s-%s" % (name, item)
        else:
            base = "%s-%d" % (name, k) if wave == 1 else "%s-w%d-%d" % (name, wave, k)
        return ("r%d-" % self.round if self.round > 1 else "") + base

    def browser_dir(self, unit_name):
        """<run>/browser/<unit_name>: the evidence root of one browser-fenced unit, the
        path _unit hands its session as QWEN_BROWSER_DIR. qwen-agent --browser makes a
        fresh timestamped folder inside it for every call it runs -- a repair round or a
        retry gets one of its own -- and writes that call's screenshots and page snapshots
        there, so a report that names this folder points at every session the unit ran. The
        engine only names the path: qwen-agent creates it, and nothing here removes it
        afterwards -- it is the evidence."""
        return self.run_dir / "browser" / str(unit_name)

    def stage_dir(self, unit_name):
        """<run>/agents/<unit_name>/fixtures: where a unit started with stage=DIR finds its
        copy of DIR -- inside the unit's own working directory, the one place a browser
        upload is accepted from. A prompt names files by this native absolute path."""
        return self.sw.agents_dir / str(unit_name) / staging.FOLDER

    def stage_digest(self, path):
        """staging.digest(path), walked once per run (per Workflow) and then reused: the
        cache key of every staged unit and a workflow's own record of the folder agree.
        The run folder is `skip`: a fixtures folder holding it must not be walked into it
        (a workflow refuses that nesting; the skip is the same rule held twice)."""
        key = str(path)
        if key not in self._stage_shas:
            self._stage_shas[key] = staging.digest(key, skip=str(self.run_dir))
        return self._stage_shas[key]

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

    def event(self, kind, **fields):
        """A workflow event in RUN/events.jsonl (events.py): {"kind", "t", **fields}.
        The engine's kinds (events.ENGINE_KINDS) are refused: a watcher must be able to
        trust that run_end means the run ended. Convention: "attention" with item,
        reason, detail = the main session should look at this. Not part of wf.calls
        (--check compares calls, not events)."""
        if not isinstance(kind, str) or not kind or kind in events.ENGINE_KINDS:
            raise ValueError("wf.event kind must be a non-empty name other than the "
                             "engine's own (%s); got %r" % (", ".join(sorted(events.ENGINE_KINDS)), kind))
        events.emit(self.run_dir, kind, **fields)

    # ------------------------------------------------------------ units
    def _unit(self, name, role, prompt, timeout, parse, batch, cache=True, always=False,
              stage=None, repair=None):
        r = self._role(role)
        f = fences.unit_fields(r.fence, self.mcp)
        u = swarm.Unit(name=name, role_file=r.file, prompt=prompt, parse=None, timeout=timeout,
                       retries=self.cfg.get("retries") or 0, effort=self._effort(role),
                       ignore_deadline=always, deep=self._deep(role), **f)
        u.cache = cache
        u.repair_text = repair
        if r.fence == "read":
            u.cwd = self.target
            u.key_extra = "target:%s" % self._fingerprint()
        if r.fence in fences.BROWSER_FENCES:
            # one evidence root per unit, inside the run folder: what the unit's session
            # is given is QWEN_BROWSER_DIR, and wf.browser_dir(unit) names the same path
            u.env = {"QWEN_BROWSER_DIR": str(self.browser_dir(u.name))}
        if r.fence == "sandbox":
            self._sandbox_unit(u)
            u.parse = self._sandbox_parse(u, parse, batch)
        else:
            u.parse = parse
        if stage is not None:
            self._stage_unit(u, r.fence, stage)
        self.calls.append(("unit", name, role, _sha(prompt)))
        return u

    def _stage_unit(self, u, fence, stage):
        """stage=DIR: the unit's setup replaces agents/<unit>/fixtures/ with a copy of DIR
        (Unit.setup runs once per unit, so its retries and repair round find the same
        copy), and DIR's digest joins its cache key, so a changed fixture re-runs it. Only
        a unit working in its own agents/<unit> folder can be staged: a read unit works in
        the user's --target and a sandbox unit in a copy of it, and neither is ours to
        write a fixtures folder into. A --check dry run copies nothing."""
        if fence in fences.TARGET_FENCES:
            raise ValueError("stage= needs a role that works in its own folder; fence %r "
                             "works in --target" % fence)
        u.key_extra = "stage:%s" % self.stage_digest(stage)
        if self.check is None:
            src, dest = str(stage), str(self.stage_dir(u.name))
            u.setup = lambda: staging.copy(src, dest)

    def agent(self, name, role, prompt, parse, *, cache=True, always=False, item="goal",
              stage=None):
        """One agent; returns parse(text) (parse(text, None, patch) for a sandbox role),
        or None when the unit failed or the deadline kept it from starting -- then
        wf.last_unit says which ("ok", "why", "deadline"). always=True runs it past the
        deadline; cache=False re-runs it on every resume. stage=DIR copies DIR into the
        unit's agents/<unit>/fixtures/ when it starts; `prompt` may then be a callable,
        prompt(staged) -> str, handed wf.stage_dir(unit) as a native absolute path."""
        uname = self._unit_name(name, 1)
        fence = self._role(role).fence
        wrapped = parse if fence != "sandbox" else (lambda text, batch, patch: parse(text, batch, patch))
        if stage is not None and callable(prompt):
            prompt = prompt(str(self.stage_dir(uname)))
        u = self._unit(uname, role, prompt, self._timeout(role, 2), wrapped, None, cache, always,
                       stage)
        (r,) = self._phase([u], role)
        self.last_unit = r
        if r["ok"]:
            return r["data"]
        if r.get("deadline"):
            self._log_deadline(uname, role, item)
            self.not_run += 1
        return None

    def fan_out(self, name, role, items, prompt, parse, *, item_id=None, max_items=None,
                stage=None, repair=None, unit_ids=False):
        """Deal `items` over at most --max-agents agents in waves of --max-agents x
        --max-items (max_items= lowers the per-agent cap for this call). prompt(batch) ->
        str; parse(text, batch) -> list of rows (parse(text, batch, patch) for a sandbox
        role). item_id(item) names an item in run.log's deadline lines (default: its
        "id"). stage=DIR copies DIR into every unit's agents/<unit>/fixtures/ when it
        starts, and prompt is then called as prompt(batch, staged): `staged` is that
        unit's wf.stage_dir as a native absolute path, for the prompt to name files by.
        repair=TEXT replaces the swarm's REPAIR text for these units' repair round
        ("{why}" in it is replaced by parse's reason). unit_ids=True deals ONE item per
        unit and names each unit <name>-<item id> instead of by its place in the deal, so
        dropping an item from the list (one already settled) moves no other unit's name,
        files or cache; the ids must be [A-Za-z0-9._-]+, not end in '.' (Windows strips
        it, so 'a.' and 'a' would collide) and differ by more than case (they name
        files, and Windows and macOS fold case), else ValueError."""
        ma, mi = self.cfg["max_agents"], self.cfg["max_items"]
        if max_items is not None:
            mi = max(1, min(mi, max_items))
        ident = item_id or _default_id
        if unit_ids:
            mi = 1
            ids = [ident(it) for it in items]
            bad = [i for i in ids if not (isinstance(i, str) and _UNIT_ID.fullmatch(i))]
            if bad:
                raise ValueError("unit_ids=True needs item ids made of [A-Za-z0-9._-] "
                                 "(got %r)" % (bad[0],))
            dotted = [i for i in ids if i.endswith(".")]
            if dotted:
                raise ValueError("unit_ids=True needs item ids that do not end in '.' "
                                 "(Windows strips it, so %r and %r would collide)"
                                 % (dotted[0], dotted[0][:-1]))
            if len({i.lower() for i in ids}) != len(ids):
                raise ValueError("unit_ids=True needs item ids that differ by more than case "
                                 "(they name unit files)")

        def uname(k, w, batch):
            return self._unit_name(name, k, w, ident(batch[0]) if unit_ids else None)
        rows, units_res, dropped, not_run = [], [], [], []
        for w, chunk in _waves(list(items), ma, mi):
            batches = list(swarm.deal(chunk, ma))
            if self._past_deadline():
                for k, batch in enumerate(batches, 1):
                    for it in batch:
                        self._log_deadline(uname(k, w, batch), role, ident(it))
                not_run += chunk
                continue
            units = []
            for k, batch in enumerate(batches, 1):
                p = self._bind(parse, batch, self._role(role).fence == "sandbox")
                un = uname(k, w, batch)
                text = prompt(batch) if stage is None else prompt(batch, str(self.stage_dir(un)))
                units.append(self._unit(un, role, text, self._timeout(role, len(batch)), p,
                                        batch, stage=stage, repair=repair))
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

    def claude_check(self, name, items, prompt, parse, *, model, max_calls, budget_usd=2.0,
                     browser=False, stage=None, read_dirs=(), item_id=None, timeout=600,
                     max_turns=40):
        """Ask Claude -- one `claude -p` per item on the user's own claude login -- to
        check each item: prompt(item) -> str, parse(text, item) -> data or ValueError.
        Returns one {"item", "state", "data", "why"} per item, in order; state is ok
        (data = parse's output), failed (this item only), unavailable (claude cannot be
        reached: not found, logged out, offline -- no further item is tried), over_cap
        (past max_calls calls made; cache hits are free) or deadline. Only ok answers
        are cached (artifact claude-<name>), so a resume asks again for the rest.
        browser=True gives the session the Playwright browser; stage=DIR is copied into
        its cwd as fixtures/; read_dirs are readable too. See swarm_engine/claude_check.py."""
        from lib.swarm_engine import claude_check
        return claude_check.run(self, name, items, prompt, parse, model=model,
                                max_calls=max_calls, budget_usd=budget_usd, browser=browser,
                                stage=stage, read_dirs=read_dirs, item_id=item_id,
                                timeout=timeout, max_turns=max_turns)

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
        # Atomic (atomic_write): qwen-swarm --record-verdict re-reads artifacts of a
        # run that may still be writing them.
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1)
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
        # the bytes a text-mode write_text produced (newlines as os.linesep), written
        # atomically: a re-render must never leave a half-written report behind
        data = markdown.replace("\n", os.linesep).encode("utf-8")
        atomic_write(path, data)
        if self._in_loop and self.multi_round:
            atomic_write(self.run_dir / ("report-round-%d.md" % self.round), data)
        self.report_path = path
        return path

    def totals(self):
        """The run's cumulative totals (every invocation so far plus this one), written
        to totals.json: agents_run, tokens, seconds, invocations, and claude_calls and
        claude_cost_usd once wf.claude_check has made a call."""
        wall = int(time.time() - self.start)

        def added(key, value):
            prev = self._baseline.get(key)
            prev = prev if isinstance(prev, int) and not isinstance(prev, bool) else 0
            return prev + value

        cum = {k: added(k, v) for k, v in (("agents_run", self.sw.agents_run),
                                           ("tokens", self.sw.tokens),
                                           ("seconds", wall), ("invocations", 1))}
        # wf.claude_check's calls: the keys appear only once a run has made one, so a
        # run that never asks Claude keeps the totals.json it always had
        if self.claude_calls or "claude_calls" in self._baseline:
            cum["claude_calls"] = added("claude_calls", self.claude_calls)
            prev = self._baseline.get("claude_cost_usd")
            prev = prev if isinstance(prev, (int, float)) and not isinstance(prev, bool) else 0.0
            cum["claude_cost_usd"] = round(prev + self.claude_cost_usd, 4)
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
