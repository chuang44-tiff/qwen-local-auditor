"""qwen-swarm: run a workflow (a workflow.json manifest plus a workflow.py script) on a
swarm of local-model Claude Code sessions.

The engine owns everything a workflow must not: the CLI, the run folder, config.json,
resume, knob resolution, preflight, totals.json, error.log, the manifest summary and the
exit codes. A workflow's run(wf) only makes calls on the Workflow object (lib/swarm_engine/api.py).

Exit codes (every workflow): 0 ok; 2 usage or manifest error; 3 preflight failed;
4 report written but units were dropped, the deadline left items unrun, or the workflow
finished without its goal; 5 nothing usable (no report); 8 internal error (an exception
in the engine or in workflow.py: the traceback goes to <run>/error.log); 130 interrupted.
"""
import argparse
import contextlib
import copy
import datetime
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from lib import search_mcp, swarm  # noqa: E402
from lib.swarm_engine import api, events, fences, manifest, runlock, steps  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_PREFLIGHT, EXIT_PARTIAL, EXIT_EMPTY = 0, 2, 3, 4, 5
EXIT_HARNESS, EXIT_INTERRUPTED = 8, 130
BUILTIN = pathlib.Path(__file__).resolve().parents[1] / "workflows"
DEFAULT_MAX_AGENTS, DEFAULT_MAX_ITEMS, DEFAULT_SEATS = 8, 10, 4
# research is the released qwen-deep-research: its config keys, goal file, default run
# folder and QWEN_DR_* environment names are kept so its run folders and habits still work
LEGACY = {"research": {"goal_key": "question", "goal_file": "question.md",
                       "out_root": "deep-research", "env": ("QWEN_SWARM_", "QWEN_DR_")}}
DEFAULT_PROFILE = {"goal_key": "goal", "goal_file": "goal.md", "out_root": None,
                   "env": ("QWEN_SWARM_",)}
RESUMABLE = "--seats, --web-seats, --timeout, --retries, --rounds, --hours, --effort, " \
            "--role-effort, --deep, --shallow and --keep-sandboxes"
# the --resume error keeps each command's own wording: deep-research's text is the one
# it released with, which predates the engine-only --rounds and --keep-sandboxes
RESUME_MSG = {
    None: "--resume takes the goal and settings from the run folder; only %s may change" % RESUMABLE,
    "deep-research": "--resume takes the question and settings from the run folder; only --seats, "
                     "--web-seats, --timeout, --retries, --hours, --effort and --role-effort "
                     "may change",
}
USAGE = {
    None: "usage: qwen-swarm WORKFLOW GOAL [--depth NAME] [--set KNOB=VALUE] [--target DIR] "
          "[--max-agents N] [--max-items N] [--seats N] [--web-seats N] [--timeout N] "
          "[--retries N] [--rounds N|until] [--hours H] [--effort LEVEL] "
          "[--role-effort ROLE=LEVEL[,ROLE=LEVEL...]] [--deep ROLE[,ROLE...]|all] "
          "[--shallow ROLE[,ROLE...]|all] [--out DIR] "
          "[--keep-sandboxes] | WORKFLOW --stdin | --resume RUN_DIR | --check WORKFLOW | --preflight [WORKFLOW] "
          "| --record-verdict RUN_DIR --id ID --verdict VERDICT --evidence TEXT [--evidence TEXT ...] "
          "| --list",
    "deep-research": "usage: qwen-deep-research QUESTION "
                     "[--depth quick|standard|deep|overnight] [--max-agents N] [--max-items N] "
                     "[--seats N] [--web-seats N] [--timeout N] [--retries N] [--hours H] "
                     "[--effort LEVEL] [--role-effort ROLE=LEVEL[,ROLE=LEVEL...]] [--out DIR] "
                     "| --resume RUN_DIR | --check",
}
PROG = "qwen-swarm"
_MODULES = {}


class Usage(Exception):
    """A usage error: the message goes to stderr, the exit code is 2."""


def err(msg):
    print("%s: %s" % (PROG, msg), file=sys.stderr)


# ---------------------------------------------------------------- workflows
def builtin_names():
    if not BUILTIN.is_dir():
        return []
    return sorted(p.name for p in BUILTIN.iterdir() if (p / "workflow.json").is_file())


def resolve_workflow(spec):
    """A built-in name, or (when spec holds a path separator or starts with '.') a folder."""
    if "/" in spec or "\\" in spec or spec.startswith("."):
        folder = pathlib.Path(spec).resolve()
        if not (folder / "workflow.json").is_file():
            raise Usage("%s is not a workflow folder (no workflow.json)" % spec)
        return folder
    if spec in builtin_names():
        return BUILTIN / spec
    raise Usage("no built-in workflow named %r (built-ins: %s; a folder path needs a / in it)"
                % (spec, ", ".join(builtin_names()) or "none"))


def load_module(folder):
    """workflow.py of `folder`, imported once per process (one module object per path)."""
    path = (pathlib.Path(folder) / "workflow.py").resolve()
    key = str(path)
    if key not in _MODULES:
        name = "qwen_swarm_wf_%s" % hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError("cannot load %s" % path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[key] = mod
    return _MODULES[key]


def profile(m):
    """The legacy profile belongs to the built-in research workflow only: a user folder
    whose manifest says "research" gets the default one, not QWEN_DR_* and question.md."""
    if m.name in LEGACY and (BUILTIN / m.name).resolve() == pathlib.Path(m.folder).resolve():
        return LEGACY[m.name]
    return DEFAULT_PROFILE


def _same_workflow_dir(spec, stored):
    """True when a --resume positional that is not a built-in name resolves to the run's
    stored workflow_dir: the positional must match the run's workflow."""
    if not isinstance(stored, str) or not stored or spec in builtin_names():
        return False
    return str(pathlib.Path(spec).resolve()) == str(pathlib.Path(stored).resolve())


# ---------------------------------------------------------------- environment
def env_raw(prefixes, name):
    """(variable name, value) of the first non-empty PREFIX+name, or (None, None)."""
    for p in prefixes:
        v = (os.environ.get(p + name) or "").strip()
        if v:
            return p + name, v
    return None, None


def env_int(prefixes, name, default):
    _, v = env_raw(prefixes, name)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def env_float(prefixes, name, default):
    """ValueError on junk: the caller reports it as a usage error."""
    _, v = env_raw(prefixes, name)
    return default if v is None else float(v)


def env_min_int(prefixes, name, minimum, default):
    var, v = env_raw(prefixes, name)
    if v is None:
        return default
    try:
        n = int(v)
    except ValueError:
        n = minimum - 1
    if n < minimum:
        raise Usage("%s must be an integer of at least %d (got %r)" % (var, minimum, v))
    return n


def utc_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(
        timespec="seconds")


def _hours_fits(h):
    """True when h is a positive number of hours datetime can build a deadline from:
    1e300 and inf parse as floats but would crash utc_iso -- that is a usage error."""
    if not math.isfinite(h) or not h > 0:
        return False
    try:
        utc_iso(time.time() + h * 3600)
    except (ValueError, OverflowError, OSError):
        return False
    return True


def deadline_epoch(cfg):
    d = cfg.get("deadline")
    if not isinstance(d, str) or not d:
        return None
    try:
        dt = datetime.datetime.fromisoformat(d)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def role_efforts(text, roles):
    out = {}
    for pair in text.split(","):
        role, sep, level = pair.partition("=")
        if not sep or role not in roles or not level:
            raise Usage("--role-effort: %r is not ROLE=LEVEL with ROLE one of %s"
                        % (pair, ", ".join(roles)))
        out[role] = level
    return out


def _roles_of(flag, text, roles):
    """ROLE[,ROLE...] or all for a depth flag: the sorted role names it names."""
    names = [r.strip() for r in text.split(",") if r.strip()]
    if names == ["all"]:
        return sorted(roles)
    if not names or any(n not in roles for n in names):
        raise Usage("%s: %r is not ROLE[,ROLE...] or all, with ROLE one of %s"
                    % (flag, text, ", ".join(roles)))
    return sorted(set(names))


def deep_roles(text, roles):
    """--deep ROLE[,ROLE...] or all: the sorted role names that get every depth switch."""
    return _roles_of("--deep", text, roles)


def shallow_roles(text, roles):
    """--shallow ROLE[,ROLE...] or all: the roles that opt out of depth (the default)."""
    return _roles_of("--shallow", text, roles)


def load_json(path):
    """Bytes I/O with utf-8/surrogateescape: argv bytes (a goal, a --set value) that are
    not valid UTF-8 survive a run-folder round trip byte-exact; valid UTF-8 is identical."""
    try:
        return json.loads(pathlib.Path(path).read_bytes().decode("utf-8", "surrogateescape"))
    except (OSError, ValueError):
        return None


def save_json(path, data):
    pathlib.Path(path).write_bytes(
        json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8", "surrogateescape"))


# ---------------------------------------------------------------- preflight
def preflight(agent):
    """The model server is reachable and serves the model (qwen-agent --preflight-only)."""
    try:
        p = subprocess.run(list(agent) + ["--preflight-only", "-q"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError) as e:
        err("preflight: model: cannot run the agent: %s" % e)
        return False
    if p.returncode != 0:
        err("preflight: model: %s" % ((p.stderr.strip().splitlines() or ["qwen-agent exit %d" % p.returncode])[0]))
        return False
    return True


def search_preflight(prefixes):
    """One real search succeeds ($QWEN_SWARM_SKIP_SEARCH_CHECK=1 / QWEN_DR_* skips it)."""
    if env_raw(prefixes, "SKIP_SEARCH_CHECK")[1] == "1":
        return True
    try:
        search_mcp.search("test", 1)
    except search_mcp.SearchError as e:
        err("preflight: search: %s" % e)
        return False
    return True


# ---------------------------------------------------------------- arguments
def parse_args(argv, compat=None):
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("words", nargs="*")
    ap.add_argument("--agent", action="append", required=True)
    ap.add_argument("--stdin", action="store_true")
    if compat == "deep-research":
        # the released qwen-deep-research validated --depth with argparse choices: an
        # unknown depth gets that argparse error and its own usage line, not "must be one of"
        try:
            choices = sorted(manifest.load(BUILTIN / "research").presets)
        except manifest.ManifestError:
            choices = None
        ap.add_argument("--depth", choices=choices)
    else:
        ap.add_argument("--depth")
    ap.add_argument("--set", action="append", default=[], dest="sets")
    ap.add_argument("--max-agents", type=int)
    ap.add_argument("--max-items", type=int)
    ap.add_argument("--seats", type=int)
    ap.add_argument("--web-seats", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--retries", type=int)
    ap.add_argument("--rounds")
    ap.add_argument("--hours", type=float)
    ap.add_argument("--effort")
    ap.add_argument("--role-effort", dest="role_effort")
    if compat is None:
        # depth per role: --deep forces the switches on (qwen-agent --review-round
        # --subagents-nudge), --shallow opts a role out of the default; the released
        # qwen-deep-research takes neither
        ap.add_argument("--deep")
        ap.add_argument("--shallow")
        # the main session's verdict on one row of a finished or running run
        ap.add_argument("--record-verdict", metavar="RUN_DIR")
        ap.add_argument("--id", dest="verdict_id")
        ap.add_argument("--verdict")
        ap.add_argument("--evidence", action="append")
    ap.add_argument("--out")
    ap.add_argument("--target")
    ap.add_argument("--keep-sandboxes", action="store_true")
    ap.add_argument("--resume")
    ap.add_argument("--check", metavar="WORKFLOW")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--list", action="store_true")
    return ap.parse_args(argv)


def _rounds_arg(text):
    try:
        return manifest.parse_engine("rounds", text)
    except ValueError:
        raise Usage("--rounds must be an integer of at least 1 or 'until' (got %r)" % text)


# ---------------------------------------------------------------- main
def main(argv=None, compat=None):
    global PROG
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--compat"] and len(argv) > 1:
        compat, argv = argv[1], argv[2:]
    if compat is None:
        PROG = "qwen-swarm"
    elif compat == "deep-research":
        PROG = "qwen-deep-research"
        # the released qwen-deep-research's --check is the preflight; qwen-swarm's --check validates a workflow
        argv = ["--preflight" if a == "--check" else a for a in argv]
    else:
        # an unknown --compat value is a usage error, not silently plain qwen-swarm
        err("unknown --compat value %r (expected deep-research)" % compat)
        return EXIT_USAGE
    try:
        o = parse_args(argv, compat)
    except SystemExit:
        err(USAGE[compat])
        return EXIT_USAGE
    try:
        return _main(o, compat)
    except Usage as e:
        err(str(e))
        return EXIT_USAGE
    except Exception as e:
        # no unexpected exception may escape as a raw traceback: every internal error is
        # exit 8 (KeyboardInterrupt and SystemExit are BaseExceptions, never caught)
        return _internal_error(e, getattr(o, "run_dir", None))


def _main(o, compat):
    if o.list:
        for name in builtin_names():
            try:
                print("%s\t%s" % (name, manifest.load(BUILTIN / name).description))
            except manifest.ManifestError as e:
                print("%s\t(invalid: %s)" % (name, e))
        return EXIT_OK
    if o.check:
        from lib.swarm_engine import check
        return check.check_workflow(o.check, err)
    if getattr(o, "record_verdict", None) is not None:
        return record_verdict(o)
    if getattr(o, "verdict_id", None) is not None or getattr(o, "verdict", None) is not None \
            or getattr(o, "evidence", None):
        raise Usage("--id, --verdict and --evidence go with --record-verdict RUN_DIR")
    words = list(o.words)
    if o.preflight and not words and not o.resume and not compat:
        # --preflight [WORKFLOW]: with no workflow only the model check runs and its
        # outcome is the exit (0 ok, 3 failed); no run folder is created. preflight
        # returns a bool: map it to the exit, like the WORKFLOW path does.
        if preflight(o.agent):
            print("ok: model reachable")
            return EXIT_OK
        return EXIT_PREFLIGHT
    if compat:
        wf_spec = "research"
    elif o.resume:
        wf_spec = words.pop(0) if words else None
    else:
        if not words:
            raise Usage("no workflow given; built-ins: %s (qwen-swarm --list)"
                        % ", ".join(builtin_names()))
        wf_spec = words.pop(0)
    if len(words) > 1:
        raise Usage("one goal only (quote it); got %d words: %r" % (len(words), words))
    if o.stdin and words:
        if not compat:
            raise Usage("--stdin and a goal argument are exclusive")
        del words[:]        # the released command: --stdin wins and the question word is ignored
    if o.effort is not None and not o.effort:
        raise Usage("--effort needs a level")
    if o.hours is not None and not _hours_fits(o.hours):
        raise Usage("--hours must be a positive number of hours")
    if o.retries is not None and o.retries < 0:
        raise Usage("--retries must be at least 0")
    rounds_flag = _rounds_arg(o.rounds) if o.rounds is not None else None
    if o.resume:
        return _resume(o, compat, wf_spec, words)
    folder = resolve_workflow(wf_spec)
    m = _load_manifest(folder)
    prof = profile(m)
    env = prof["env"]
    max_unit = env_min_int(env, "MAX_UNIT_SECONDS", 1, swarm.MAX_UNIT_SECONDS)
    efforts = role_efforts(o.role_effort, tuple(m.roles)) if o.role_effort is not None else {}
    deep = deep_roles(o.deep, tuple(m.roles)) if getattr(o, "deep", None) is not None else None
    shallow = (shallow_roles(o.shallow, tuple(m.roles))
               if getattr(o, "shallow", None) is not None else None)
    seats, web_seats = _seats(o, env)
    if o.preflight:
        ok = preflight(o.agent)
        searched = ok and fences.needs_search(m)
        if ok and searched:
            ok = search_preflight(env)
        if not ok:
            return EXIT_PREFLIGHT
        print("ok: model and search reachable" if searched else "ok: model reachable")
        return EXIT_OK
    try:
        goal = sys.stdin.read() if o.stdin else (words[0] if words else "")
    except KeyboardInterrupt:
        err("interrupted")
        return EXIT_INTERRUPTED
    goal = goal.strip()
    if not goal:
        # the released command's message for its own goal; the engine names the manifest's goal
        raise Usage("no question given" if compat else "no goal given (%s)" % m.goal)
    depth = o.depth or m.default_depth
    if depth not in m.presets:
        raise Usage("--depth must be one of %s (got %r)" % (", ".join(m.presets), depth))
    preset = m.presets[depth]
    try:
        sets = manifest.parse_sets(m, o.sets)
    except manifest.ManifestError as e:
        raise Usage(str(e))
    knobs = {k: sets.get(k, preset[k]) for k in m.knobs}
    max_agents = o.max_agents if o.max_agents is not None else env_int(env, "MAX_AGENTS", DEFAULT_MAX_AGENTS)
    if max_agents < 1:
        raise Usage("--max-agents must be at least 1")
    max_items = o.max_items if o.max_items is not None else env_int(env, "MAX_ITEMS", DEFAULT_MAX_ITEMS)
    if max_items < 1:
        raise Usage("--max-items must be at least 1")
    timeout = _first(o.timeout, sets.get("budget"), _env_opt_int(env, "TIMEOUT"), preset["budget"])
    retries = _first(o.retries, sets.get("retries"), _env_opt_int(env, "RETRIES"), preset["retries"])
    if retries < 0:
        raise Usage("--retries must be at least 0")
    rounds = _first(rounds_flag, sets.get("rounds"), None, preset["rounds"])
    hours = o.hours if o.hours is not None else sets.get("hours")
    if hours is None:
        try:
            hours = env_float(env, "HOURS", preset.get("hours"))
        except ValueError:
            hours = -1.0
    if hours is not None and not _hours_fits(hours):
        raise Usage("--hours must be a positive number of hours (check --hours and %sHOURS)" % env[-1])
    if rounds == "until" and hours is None:
        raise Usage("--rounds until needs --hours: an open-ended run must have a deadline")
    target = _target(o, m)
    prof_key = prof["goal_key"]
    cfg = {prof_key: goal, "depth": depth}
    cfg.update(knobs)
    cfg.update({"max_agents": max_agents, "max_items": max_items, "timeout_per_item": timeout,
                "retries": retries, "effort": o.effort, "role_effort": efforts, "hours": hours,
                "deadline": None if hours is None else utc_iso(time.time() + hours * 3600),
                "workflow": m.name, "rounds": rounds,
                "target": None if target is None else str(target),
                "workflow_dir": None if folder.parent == BUILTIN else str(folder)})
    if deep is not None:
        cfg["deep"] = deep              # only when given: a run without it keeps its config
    if shallow is not None:
        cfg["shallow"] = shallow        # the depth opt-out per role, same rule
    try:
        mod = load_module(folder)
    except Exception as e:
        err("internal error: workflow.py does not import: %s: %s" % (type(e).__name__, e))
        return EXIT_HARNESS
    validate = getattr(mod, "validate", None)
    msg = None
    if callable(validate):
        try:
            msg = validate(dict(cfg))
        except Exception as e:
            err("internal error: workflow.py validate(): %s: %s" % (type(e).__name__, e))
            return EXIT_HARNESS
    if msg:
        raise Usage(msg)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = pathlib.Path(prof["out_root"]) if prof["out_root"] else pathlib.Path("swarm") / m.name
    run = (pathlib.Path(o.out) if o.out else root / ("%s-%s" % (stamp, steps.slug(goal)))).resolve()
    _check_run_outside_target(run, target)
    o.run_dir = run                                     # where error.log goes if we crash
    if (run / "config.json").exists():
        raise Usage("--out %s already holds a run; use --resume %s or another --out" % (run, run))
    return _post_release(run, mod, _start(o, m, mod, cfg, run, seats, web_seats, max_unit,
                                          new=True))


def _first(*values):
    return next(v for v in values if v is not None)


def _env_opt_int(env, name):
    _, v = env_raw(env, name)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _seats(o, env):
    seats = o.seats if o.seats is not None else env_int(env, "SEATS", DEFAULT_SEATS)
    if seats < 1:
        raise Usage("--seats must be at least 1")
    web_seats = o.web_seats if o.web_seats is not None else env_int(env, "WEB_SEATS", seats)
    if web_seats < 1 or web_seats > seats:
        raise Usage("--web-seats must be between 1 and --seats (%d)" % seats)
    return seats, web_seats


def _target(o, m):
    if m.target == "none":
        if o.target:
            raise Usage("workflow %s takes no --target" % m.name)
        return None
    if not o.target:
        raise Usage("workflow %s needs --target DIR (the codebase it works on)" % m.name)
    t = pathlib.Path(o.target).resolve()
    if not t.is_dir():
        raise Usage("--target %s is not a directory" % o.target)
    return t


def _check_run_outside_target(run, target):
    """Refuse (exit 2) when the run folder and --target nest into each other. A run
    folder inside the target would write the run's prompts, patches, logs and sandboxes
    into the user's tree (and a copy-mode sandbox would copy the target into a sandbox
    folder inside itself); a target inside the run folder nests the same way."""
    if target is None:
        return
    run, target = pathlib.Path(run).resolve(), pathlib.Path(target).resolve()
    if run == target or target in run.parents or run in target.parents:
        raise Usage("the run folder %s and --target %s hold one inside the other: "
                    "use --out DIR outside the target" % (run, target))


def _load_manifest(folder):
    try:
        return manifest.load(folder)
    except manifest.ManifestError as e:
        raise Usage("%s: %s" % (pathlib.Path(folder) / "workflow.json", e))


def _resume(o, compat, wf_spec, words):
    if words or o.stdin or o.depth or o.max_agents is not None or o.max_items is not None \
            or o.out or o.sets or o.target or o.preflight:
        raise Usage(RESUME_MSG.get(compat) or RESUME_MSG[None])
    run = pathlib.Path(o.resume).resolve()
    o.run_dir = run                                     # where error.log goes if we crash
    cfg = load_json(run / "config.json")
    if cfg is None:
        raise Usage("--resume: no readable config.json in %s" % run)
    if not isinstance(cfg, dict):
        raise Usage("--resume: config.json is not a run configuration")
    name = cfg.get("workflow") or "research"      # run folders from the released command predate the key
    if compat and name != "research":
        raise Usage("--resume: %s is a %s run, not a research run (use qwen-swarm)" % (run, name))
    if wf_spec is not None and wf_spec != name and \
            not _same_workflow_dir(wf_spec, cfg.get("workflow_dir")):
        raise Usage("--resume: %s is a %s run, not %s" % (run, name, wf_spec))
    folder = pathlib.Path(cfg["workflow_dir"]) if cfg.get("workflow_dir") else BUILTIN / name
    m = _load_manifest(folder)
    prof = profile(m)
    env = prof["env"]
    max_unit = env_min_int(env, "MAX_UNIT_SECONDS", 1, swarm.MAX_UNIT_SECONDS)
    efforts = role_efforts(o.role_effort, tuple(m.roles)) if o.role_effort is not None else {}
    seats, web_seats = _seats(o, env)
    if "timeout_per_item" not in cfg and "timeout" in cfg:
        cfg["timeout_per_item"] = cfg["timeout"]          # older configs called it "timeout"
    cfg.setdefault("retries", 0)
    cfg.setdefault("max_items", DEFAULT_MAX_ITEMS)
    cfg.setdefault("rounds", 1)
    for k in [prof["goal_key"], "depth", "max_agents", "timeout_per_item"]:
        if k not in cfg:
            raise Usage("--resume: config.json in %s has no '%s' key" % (run, k))
    fill = _fill_knobs(m, cfg, run)
    if o.timeout is not None:
        cfg["timeout_per_item"] = o.timeout
    if o.retries is not None:
        cfg["retries"] = o.retries
    if o.rounds is not None:
        cfg["rounds"] = _rounds_arg(o.rounds)
    if o.effort is not None:
        cfg["effort"] = o.effort
    if o.role_effort is not None:
        stored = cfg.get("role_effort")
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored.update(efforts)
        cfg["role_effort"] = stored
    if getattr(o, "deep", None) is not None:
        stored = cfg.get("deep")
        stored = set(stored) if isinstance(stored, list) else set()
        cfg["deep"] = sorted(stored | set(deep_roles(o.deep, tuple(m.roles))))
    if getattr(o, "shallow", None) is not None:
        stored = cfg.get("shallow")
        stored = set(stored) if isinstance(stored, list) else set()
        cfg["shallow"] = sorted(stored | set(shallow_roles(o.shallow, tuple(m.roles))))
    if o.hours is not None:
        cfg["hours"] = o.hours
        cfg["deadline"] = utc_iso(time.time() + o.hours * 3600)
    if cfg["rounds"] == "until" and deadline_epoch(cfg) is None:
        raise Usage("--rounds until needs --hours: an open-ended run must have a deadline")
    target = pathlib.Path(cfg["target"]) if cfg.get("target") else None
    if m.target == "required" and (target is None or not target.is_dir()):
        raise Usage("--resume: the run's --target %s is gone" % cfg.get("target"))
    _check_run_outside_target(run, target)
    try:
        mod = load_module(folder)
    except Exception as e:
        err("internal error: workflow.py does not import: %s: %s" % (type(e).__name__, e))
        return EXIT_HARNESS
    # the run lock (runlock.py) before config.json is touched: a live runner owns that
    # file, and of two resumes racing for one folder only one passes the O_EXCL create
    _clear_stale(run)                                 # a killed runner's lock
    try:
        runlock.acquire(run)
    except runlock.RunLive as e:
        raise Usage("--resume: %s" % e)
    for line in fill:
        err(line)                                   # said only now: the lock is ours
    try:
        # the workflow loaded, so the merged settings can be written: a resume that fails
        # on the workflow leaves config.json exactly as the interrupted run left it
        save_json(run / "config.json", cfg)
    except BaseException:
        runlock.release(run)
        raise
    return _post_release(run, mod, _start(o, m, mod, cfg, run, seats, web_seats, max_unit,
                                          new=False, locked=True))


def _fill_knobs(m, cfg, run):
    """A declared knob missing from config.json -- the run was made before the workflow
    declared it -- takes the value of the run's own depth preset. The lines saying so
    are RETURNED, not printed: --resume prints them only once it holds the run lock, so
    a resume refused by a live runner says nothing. A run whose depth is no longer a
    preset cannot be filled: usage error."""
    missing = [k for k in m.knobs if k not in cfg]
    if not missing:
        return []
    preset = m.presets.get(cfg["depth"]) if isinstance(cfg["depth"], str) else None
    if preset is None:
        raise Usage("--resume: config.json in %s has no '%s' key, and its depth %r is not "
                    "a preset of %s to take it from" % (run, missing[0], cfg["depth"], m.name))
    for k in missing:
        cfg[k] = preset[k]
    return ["config.json has no %r; using preset value %r" % (k, preset[k]) for k in missing]


def _notice(mod, cfg, run):
    """The workflow's notice(cfg) -> str | None, printed in the run-start summary on a
    fresh run and on every resume: what the run will do that the user should know
    before any agent starts (e.g. data leaving the machine). The hook sees a deep copy:
    editing it cannot change the run's config. A return that is neither str nor None is
    ignored, with one line in run.log."""
    hook = getattr(mod, "notice", None)
    if not callable(hook):
        return
    text = hook(copy.deepcopy(cfg))
    if text is not None and not isinstance(text, str):
        with open(run / "run.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write("\t".join(["-", "workflow", "-", "0", "0",
                                "notice returned %s, ignored" % type(text).__name__]) + "\n")
        return
    for line in str(text or "").splitlines():
        if line.strip():
            err("  notice: %s" % line)


def summary(m, cfg):
    """The resolved manifest summary: what the run will do, before any agent starts."""
    knobs = {k: cfg[k] for k in m.knobs}
    knobs.update({"budget": cfg["timeout_per_item"], "retries": cfg.get("retries"),
                  "rounds": cfg.get("rounds", 1), "hours": cfg.get("hours")})
    return {"workflow": m.name, "depth": cfg["depth"],
            "roles": {r.name: r.fence for r in m.roles.values()},
            "knobs": knobs, "target": cfg.get("target"),
            "target_dirty": bool(cfg.get("target_dirty"))}


def print_summary(s):
    err("workflow %s (depth %s)" % (s["workflow"], s["depth"]))
    err("  roles: %s" % " ".join("%s=%s" % kv for kv in s["roles"].items()))
    err("  knobs: %s" % " ".join("%s=%s" % (k, json.dumps(v)) for k, v in s["knobs"].items()))
    if s["target"]:
        err("  target: %s%s" % (s["target"], " (uncommitted changes are NOT in the sandboxes)"
                                if s["target_dirty"] else ""))


def _internal_error(e, run=None):
    """Report an unexpected exception as exit 8: the message to stderr, the traceback to
    <run>/error.log when a run folder exists to hold it (stderr alone otherwise)."""
    err("internal error: %s: %s" % (type(e).__name__, e))
    if run is not None:
        run = pathlib.Path(run)
        if run.is_dir():
            with contextlib.suppress(OSError):
                with open(run / "error.log", "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
                err("see %s" % (run / "error.log"))
    return EXIT_HARNESS


def _clear_stale(run):
    """runlock.clear_stale with its wait mapped: TimeoutError means another process is
    mid-clear (RUN/.lock.clear held past the wait) -- a "try again" usage error, not
    an internal error."""
    try:
        runlock.clear_stale(run, err)
    except TimeoutError as e:
        raise Usage("another process is clearing the lock at %s; try again"
                    % (pathlib.Path(run) / runlock.LOCK)) from e


def _start(o, m, mod, cfg, run, seats, web_seats, max_unit, new, locked=False):
    """locked=True: the caller (--resume) already holds RUN/.lock; a fresh run takes it
    here, once the folder exists. Whoever took it, it is released on every exit, and
    only a holder may touch the run's files on the way out: a runner refused the lock
    leaves the live run exactly as it found it."""
    target = None
    # once run_start is written, the finally writes run_end with rc, the exit this call
    # returns: every exit of a run that started (0, 2 when run() itself raises Usage,
    # 4, 5, 8, 130); the exits before run_start (2 and 3) never get that far
    started, rc, wf = False, EXIT_HARNESS, None
    try:
        # inside the try, so a raise here still reaches the finally that releases a
        # lock a --resume was already holding on entry
        prof = profile(m)
        env = prof["env"]
        target = pathlib.Path(cfg["target"]) if cfg.get("target") else None
        if not preflight(o.agent):
            return EXIT_PREFLIGHT
        if fences.needs_search(m) and not search_preflight(env):
            return EXIT_PREFLIGHT
        run.mkdir(parents=True, exist_ok=True)
        if not locked:
            _clear_stale(run)                         # a killed runner's lock
            try:
                runlock.acquire(run)
            except runlock.RunLive as e:
                err(str(e))                           # another runner holds the folder
                return EXIT_USAGE
            locked = True
        # the first stderr line after the preflight: a session that started the run in
        # the background learns where to watch (RUN/events.jsonl) before anything runs
        err("run folder: %s" % run)
        events.emit(run, "run_start", workflow=m.name, goal=cfg[prof["goal_key"]],
                    run=str(run), resumed=not new, depth=cfg["depth"],
                    knobs={k: cfg[k] for k in m.knobs})
        started = True
        if target is not None:
            from lib.swarm_engine import sandbox
            sandbox.cleanup(run, target)               # leftovers of a killed run
            cfg["target_dirty"] = sandbox.is_dirty(target)
        cfg["summary"] = summary(m, cfg)
        if new:
            (run / prof["goal_file"]).write_bytes(
                (cfg[prof["goal_key"]] + "\n").encode("utf-8", "surrogateescape"))
        save_json(run / "config.json", cfg)
        mcp = fences.mcp_config(run) if fences.needs_search(m) else None
        print_summary(cfg["summary"])
        _notice(mod, cfg, run)
        sw = swarm.Swarm(o.agent, run, seats=seats, timeout=cfg["timeout_per_item"],
                         backoff=env_int(env, "BACKOFF", 30), max_unit_seconds=max_unit,
                         deadline=deadline_epoch(cfg))
        swarm.install_stop_signals()
        start = time.time()
        wf = api.Workflow(m, cfg, run, sw, goal=cfg[prof["goal_key"]], mcp=mcp,
                          web_seats=web_seats, start=start, keep_sandboxes=o.keep_sandboxes)
        mod.run(wf)
        rc = finish(wf, cfg, run, start)
        return rc
    except api.Empty as e:
        err(str(e))
        print(run)
        rc = EXIT_EMPTY
        return rc
    except KeyboardInterrupt:
        err("interrupted; resume with --resume %s" % run)
        rc = EXIT_INTERRUPTED
        return rc
    except Usage:
        if started:
            rc = EXIT_USAGE                         # run_end.exit matches the exit 2
        raise                                       # the user's error, not the engine's
    except Exception as e:
        rc = _internal_error(e, run)
        return rc
    finally:
        # only the holder of the lock cleans up: sandbox.cleanup deletes everything
        # under <run>/sandboxes, and a start refused by RunLive must never wipe the
        # live run's sandboxes through it
        if locked and target is not None and not o.keep_sandboxes and run.exists():
            from lib.swarm_engine import sandbox
            with contextlib.suppress(Exception):
                sandbox.cleanup(run, target)
        if started:
            report = wf.report_path if wf is not None and rc in (EXIT_OK, EXIT_PARTIAL) else None
            events.emit(run, "run_end", exit=rc, report=None if report is None else str(report))
        if locked:
            with contextlib.suppress(OSError):
                runlock.release(run)


def finish(wf, cfg, run, start):
    """The engine's end of every run: report path, totals, stderr notes, exit code."""
    if wf.report_path is None:
        err("the workflow finished without writing a report")
        print(run)
        return EXIT_EMPTY
    totals = wf.last_totals if wf.last_totals is not None else wf.totals()
    if wf.multi_round:
        totals = dict(totals, stop_reason=wf.stop_reason or "rounds")
        save_json(run / "totals.json", totals)
    print(wf.report_path)
    if wf.not_run:
        hours = cfg.get("hours")
        x = hours if isinstance(hours, (int, float)) and not isinstance(hours, bool) \
            and hours > 0 else max(0.0, (time.time() - start) / 3600.0)
        xh = "%g" % x
        err("deadline reached after %sh; %d items not run; --resume %s --hours %s continues"
            % (xh, wf.not_run, run, xh))
    if wf.dropped:
        err("%d agent(s) dropped; see run.log; rerun them with --resume %s" % (wf.dropped, run))
    if wf.unmet:
        err("finished without its goal: %s" % wf.unmet)
    return exit_for(wf.dropped, wf.not_run, wf.unmet)


def exit_for(dropped, not_run, unmet):
    """The one rule for a run that wrote its report: EXIT_PARTIAL (4) when any unit was
    dropped, the deadline left items unrun, or the goal was not met; else EXIT_OK.
    Shared by finish() and by a re-render of a finished run (--record-verdict)."""
    return EXIT_PARTIAL if (dropped or not_run or unmet) else EXIT_OK


# ---------------------------------------------------------------- verdicts
def _write_json(path, data):
    """`path` replaced atomically (api.atomic_write) with `data` as wf.save writes JSON: the
    same bytes, so a re-rendered artifact is indistinguishable from a live-written one."""
    api.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1)
                     .encode("utf-8", "surrogateescape"))


def _write_report(path, markdown):
    """`path` replaced atomically with the bytes wf.report writes (newlines as os.linesep)."""
    api.atomic_write(path, markdown.replace("\n", os.linesep).encode("utf-8"))


def rerender(run, mod):
    """Re-render a finished run of a workflow that takes verdicts (its module defines
    read_verdicts, apply_verdicts and render) from its `final` artifact and every valid
    verdict file, under runlock.render_lock so parallel callers take turns (TimeoutError
    when another holder never lets go): results.json, report.md and final.json (its
    verdicts_applied) are each replaced atomically, final.json last, so an interrupted
    re-render is simply redone; totals.json is left alone. Returns (exit code, final,
    unmet), or (None, None, None) when the run has no final.json -- it never reached its
    final rows."""
    run = pathlib.Path(run)
    with runlock.render_lock(run):
        final = load_json(run / "final.json")
        if not isinstance(final, dict):
            return None, None, None
        invalid = []
        verdicts = mod.read_verdicts(run, invalid=invalid)
        final["verdicts_applied"] = sorted([vid, v["mtime_ns"]] for vid, v in verdicts.items())
        final["invalid_verdicts"] = [list(x) for x in invalid]
        rows, unmet = mod.apply_verdicts(final, verdicts)
        _write_json(run / "results.json", rows)
        _write_report(run / "report.md", mod.render(final, rows))
        _write_json(run / "final.json", final)
    return exit_for(final.get("dropped") or 0, final.get("not_run") or 0, unmet), final, unmet


def _exit_line(code, final, unmet):
    """Why a re-rendered run exits as it does: a verdict clears neither a drop nor a NOT RUN."""
    why = []
    if final.get("dropped"):
        why.append("%d agent(s) dropped (a verdict does not clear a drop)" % final["dropped"])
    if final.get("not_run"):
        why.append("%d item(s) not run (a verdict does not clear that; --resume runs them)"
                   % final["not_run"])
    if unmet:
        why.append("rows that still count as failures remain")
    return "exit %d: %s" % (code, "; ".join(why) or "nothing left that counts as a failure")


def _post_release(run, mod, code):
    """After a run ended (its `finally` has written run_end and released RUN/.lock): when the
    workflow takes verdicts and finished with a `final` artifact, re-list RUN/verdicts/; a
    verdict not in final.verdicts_applied (by id and mtime_ns) was written after the final
    rows were built, so the run is re-rendered with it and that exit is the process's. A
    --record-verdict call that saw the lock gone re-renders too; the render lock serializes
    the two, and either way no verdict is lost. run_end.exit stays the run's own exit; the
    docs tell sessions to gate on the process exit. A render lock that stays busy means a
    verdict was NOT applied: the process exits 4 (never 0) and says to run the same
    --record-verdict command again. Any other fault reading the verdicts or re-rendering
    is one err() note and the run's own exit."""
    if code not in (EXIT_OK, EXIT_PARTIAL) or not callable(getattr(mod, "apply_verdicts", None)):
        return code
    try:
        final = load_json(pathlib.Path(run) / "final.json")
        if not isinstance(final, dict):
            return code
        applied = {(x[0], x[1]) for x in final.get("verdicts_applied") or []
                   if isinstance(x, list) and len(x) == 2}
        current = {(vid, v["mtime_ns"]) for vid, v in mod.read_verdicts(run).items()}
    except Exception as e:
        err("could not read the run's session verdicts after the run: %s: %s; the run's "
            "exit stands" % (type(e).__name__, e))
        return code
    if current <= applied:
        return code
    try:
        new, final, unmet = rerender(run, mod)
    except TimeoutError as e:
        err("a session verdict was recorded as the run ended but %s; run `qwen-swarm "
            "--record-verdict %s ...` again to apply it" % (e, run))
        return EXIT_PARTIAL            # a verdict did not land: never report a clean pass
    except Exception as e:
        err("could not apply a session verdict recorded as the run ended: %s: %s; the run's "
            "exit stands; run `qwen-swarm --record-verdict %s ...` once the fault is fixed"
            % (type(e).__name__, e, run))
        return code
    if new is None:
        return code
    err("applied %d session verdict(s) recorded as the run ended; %s"
        % (len(current - applied), _exit_line(new, final, unmet)))
    return new


def record_verdict(o):
    """qwen-swarm --record-verdict RUN --id ID --verdict V --evidence TEXT [...]: the main
    session's final say on one row. The workflow must take verdicts (its module defines
    apply_verdicts; it names them in VERDICTS and may refuse an id through
    verdict_problem(run_dir, id)). The verdict is written to RUN/verdicts/<ID>.json
    atomically -- a later call for the same id replaces it. A live run (RUN/.lock held)
    applies it itself: exit 0. A run that did not finish has no `final` artifact: exit 5,
    the file waits for --resume. Otherwise the run is re-rendered under the render lock and
    the exit is the re-rendered one. This command never writes events."""
    if o.words or o.resume or o.stdin or o.sets or o.out or o.target or o.depth or o.preflight:
        raise Usage("--record-verdict takes RUN_DIR --id ID --verdict VERDICT --evidence TEXT "
                    "[--evidence TEXT ...] and nothing else")
    run = pathlib.Path(o.record_verdict).resolve()
    cfg = load_json(run / "config.json")
    if not isinstance(cfg, dict):
        raise Usage("--record-verdict: %s is not a run folder (no readable config.json)" % run)
    name = cfg.get("workflow") or "research"
    folder = pathlib.Path(cfg["workflow_dir"]) if cfg.get("workflow_dir") else BUILTIN / name
    try:
        mod = load_module(folder)
    except Exception as e:
        raise Usage("--record-verdict: the run's workflow %s does not load: %s: %s"
                    % (name, type(e).__name__, e))
    if not callable(getattr(mod, "apply_verdicts", None)):
        raise Usage("--record-verdict: %s is a %s run; that workflow takes no verdicts"
                    % (run, name))
    choices = tuple(getattr(mod, "VERDICTS", ()))
    if not o.verdict_id or not api._KEY.fullmatch(o.verdict_id):
        raise Usage("--record-verdict needs --id ID (a row id of the run)")
    if o.verdict not in choices:
        raise Usage("--verdict must be one of %s (got %r)" % (", ".join(choices), o.verdict))
    evidence = [e for e in (o.evidence or []) if e.strip()]
    if not evidence:
        raise Usage("--record-verdict needs at least one --evidence TEXT")
    problem = getattr(mod, "verdict_problem", None)
    why = problem(run, o.verdict_id) if callable(problem) else None
    if why:
        raise Usage("--record-verdict: %s" % why)
    path = run / "verdicts" / ("%s.json" % o.verdict_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, {"id": o.verdict_id, "verdict": o.verdict, "evidence": evidence,
                       "by": "session", "t": time.time()})           # atomic: api.atomic_write
    if runlock.is_live(run):
        print("recorded; the running workflow will apply it")
        return EXIT_OK
    try:
        runlock.clear_stale(run, err)      # a killed runner's lock, "removed stale lock (pid N)"
        code, final, unmet = rerender(run, mod)
    except TimeoutError as e:              # .lock.clear or .render.lock held by someone else
        err("verdict saved to %s, but it was not applied: %s; run the same command again"
            % (path, e))
        return EXIT_HARNESS
    if code is None:
        err("run did not finish; verdict saved, applied on --resume %s" % run)
        return EXIT_EMPTY
    err(_exit_line(code, final, unmet))
    print(run / "report.md")
    return code


if __name__ == "__main__":
    sys.exit(main())
