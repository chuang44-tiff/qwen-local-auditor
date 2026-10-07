"""Load and validate a workflow folder's workflow.json (the manifest a user approves).

Every rule raises ManifestError with a message that names the offending field; the
runner turns it into a usage error (exit 2). Unknown keys are rejected: a typo must
never be silently ignored.
"""
import json
import math
import pathlib
import re

FENCES = ("none", "search", "web", "read", "sandbox")
KNOB_TYPES = ("int", "float", "str", "bool")
ENGINE_KNOBS = ("budget", "retries", "rounds", "hours")
TOP_KEYS = ("name", "description", "goal", "target", "roles", "knobs", "presets",
            "default_depth")
ROLE_KEYS = ("file", "fence", "budget_weight", "effort", "deep")
# qwen-agent depth switches a role may ask for ("deep": true = all of them), in the order
# qwen-agent is given them: --review-round, --subagents-nudge.
DEEP_SWITCHES = ("review_round", "subagents")
# config.json keys a knob would shadow
RESERVED = ENGINE_KNOBS + ("workflow", "workflow_dir", "goal", "question", "depth",
                           "max_agents", "max_items", "timeout_per_item", "effort",
                           "role_effort", "deadline", "target", "summary", "seats",
                           "web_seats", "timeout", "deep")
_NAME = re.compile(r"[a-z][a-z0-9-]*")
_IDENT = re.compile(r"[a-z][a-z0-9_]*")
_EFFORT = re.compile(r"[A-Za-z0-9_-]+")
_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


class ManifestError(ValueError):
    pass


class Role:
    def __init__(self, name, file, fence, budget_weight, effort, deep=()):
        self.name, self.file, self.fence = name, file, fence
        self.budget_weight, self.effort = budget_weight, effort
        self.deep = tuple(deep)         # a subset of DEEP_SWITCHES, in that order


class Manifest:
    def __init__(self, folder, name, description, goal, target, roles, knobs, presets,
                 default_depth):
        self.folder, self.name, self.description, self.goal = folder, name, description, goal
        self.target, self.roles, self.knobs = target, roles, knobs
        self.presets, self.default_depth = presets, default_depth


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (_is_int(v) or isinstance(v, float)) and math.isfinite(v)


def check_value(kind, v):
    """Whether v is a valid value of knob type `kind` (int means int >= 0)."""
    if kind == "int":
        return _is_int(v) and v >= 0
    if kind == "float":
        return _is_num(v)
    if kind == "str":
        return isinstance(v, str)
    if kind == "bool":
        return isinstance(v, bool)
    raise ValueError("unknown knob type %r" % kind)


def _check_engine(depth, preset):
    where = "presets.%s" % depth
    if not (_is_int(preset.get("budget")) and preset["budget"] >= 1):
        raise ManifestError("%s.budget must be an integer of at least 1" % where)
    if not (_is_int(preset.get("retries")) and preset["retries"] >= 0):
        raise ManifestError("%s.retries must be an integer of at least 0" % where)
    rounds = preset.get("rounds")
    if not (rounds == "until" or (_is_int(rounds) and rounds >= 1)):
        raise ManifestError("%s.rounds must be an integer of at least 1 or \"until\"" % where)
    if "hours" in preset and not (_is_num(preset["hours"]) and preset["hours"] > 0):
        raise ManifestError("%s.hours must be a number above 0" % where)
    if rounds == "until" and "hours" not in preset:
        raise ManifestError("%s.hours is required when rounds is \"until\"" % where)


def _deep(where, value):
    """A role's "deep" field as a tuple of DEEP_SWITCHES: true = all, false = none, or a list."""
    if value is True:
        return DEEP_SWITCHES
    if value is False:
        return ()
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            # A list it already is: the complaint is its items, and the message must
            # say that -- "must be true, false or a list" reads as nonsense for [1].
            raise ManifestError("%s.deep: a list must hold switch names as strings (got %r)"
                                % (where, value))
    else:
        raise ManifestError("%s.deep must be true, false or a list of %s (got %r)"
                            % (where, ", ".join(DEEP_SWITCHES), value))
    if "probe" in value:
        raise ManifestError("%s.deep: \"probe\" is not a manifest option: a sandbox role already "
                            "has a shell, and the other fences have no tree to probe" % where)
    for v in value:
        if v not in DEEP_SWITCHES:
            raise ManifestError("%s.deep: unknown switch %r (allowed: %s)"
                                % (where, v, ", ".join(DEEP_SWITCHES)))
    return tuple(d for d in DEEP_SWITCHES if d in value)


def validate(data, folder):
    """A Manifest from the parsed JSON `data` of the workflow folder `folder`."""
    folder = pathlib.Path(folder).resolve()
    if not isinstance(data, dict):
        raise ManifestError("workflow.json must hold one JSON object")
    for k in data:
        if k not in TOP_KEYS:
            raise ManifestError("unknown key %r (allowed: %s)" % (k, ", ".join(TOP_KEYS)))
    for k in TOP_KEYS:
        if k not in data:
            raise ManifestError("missing key %r" % k)
    name = data["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ManifestError("name must match [a-z][a-z0-9-]* (got %r)" % (name,))
    for k in ("description", "goal"):
        if not isinstance(data[k], str) or not data[k].strip():
            raise ManifestError("%s must be a non-empty string" % k)
    if data["target"] not in ("none", "required"):
        raise ManifestError("target must be \"none\" or \"required\" (got %r)" % (data["target"],))
    roles_in = data["roles"]
    if not isinstance(roles_in, dict) or not roles_in:
        raise ManifestError("roles must be a non-empty object")
    roles = {}
    for rname, spec in roles_in.items():
        where = "roles.%s" % rname
        if not _IDENT.fullmatch(rname):
            raise ManifestError("%s: a role name must match [a-z][a-z0-9_]*" % where)
        if not isinstance(spec, dict):
            raise ManifestError("%s must be an object" % where)
        for k in spec:
            if k not in ROLE_KEYS:
                raise ManifestError("%s: unknown key %r (allowed: %s)" % (where, k, ", ".join(ROLE_KEYS)))
        rel = spec.get("file")
        if not isinstance(rel, str) or not rel:
            raise ManifestError("%s.file must be a relative path inside the workflow folder" % where)
        p = pathlib.PurePosixPath(rel.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", rel):
            raise ManifestError("%s.file must stay inside the workflow folder (got %r)" % (where, rel))
        path = (folder / pathlib.Path(*p.parts)).resolve()
        # resolve() followed symlinks; a link to outside the (already resolved)
        # folder is an escape even though the lexical check above passed.
        try:
            path.relative_to(folder)
        except ValueError:
            raise ManifestError("%s.file must stay inside the workflow folder (got %r)" % (where, rel))
        if not path.is_file():
            raise ManifestError("%s.file: no such file: %s" % (where, rel))
        fence = spec.get("fence")
        if fence not in FENCES:
            raise ManifestError("%s.fence must be one of %s (got %r)" % (where, ", ".join(FENCES), fence))
        if fence in ("read", "sandbox") and data["target"] != "required":
            raise ManifestError("%s.fence %r needs \"target\": \"required\"" % (where, fence))
        weight = spec.get("budget_weight", 1)
        if not (_is_num(weight) and weight >= 1):
            raise ManifestError("%s.budget_weight must be a number of at least 1" % where)
        effort = spec.get("effort")
        if effort is not None and not (isinstance(effort, str) and _EFFORT.fullmatch(effort)):
            raise ManifestError("%s.effort must be a level name such as low or high" % where)
        roles[rname] = Role(rname, path, fence, weight, effort, _deep(where, spec.get("deep", False)))
    knobs = data["knobs"]
    if not isinstance(knobs, dict):
        raise ManifestError("knobs must be an object of name: type")
    for k, kind in knobs.items():
        if not _IDENT.fullmatch(k) or k in RESERVED:
            raise ManifestError("knobs.%s: a knob name must match [a-z][a-z0-9_]* and not be "
                                "one of %s" % (k, ", ".join(RESERVED)))
        if kind not in KNOB_TYPES:
            raise ManifestError("knobs.%s must be one of %s (got %r)" % (k, ", ".join(KNOB_TYPES), kind))
    presets = data["presets"]
    if not isinstance(presets, dict) or not presets:
        raise ManifestError("presets must be a non-empty object")
    for depth, preset in presets.items():
        where = "presets.%s" % depth
        if not _IDENT.fullmatch(depth):
            raise ManifestError("%s: a preset name must match [a-z][a-z0-9_]*" % where)
        if not isinstance(preset, dict):
            raise ManifestError("%s must be an object" % where)
        for k in preset:
            if k not in knobs and k not in ENGINE_KNOBS:
                raise ManifestError("%s: %r is neither a declared knob nor one of %s"
                                    % (where, k, ", ".join(ENGINE_KNOBS)))
        for k, kind in knobs.items():
            if k not in preset:
                raise ManifestError("%s must set knob %r" % (where, k))
            if not check_value(kind, preset[k]):
                raise ManifestError("%s.%s must be a %s%s (got %r)" % (
                    where, k, kind, " >= 0" if kind == "int" else "", preset[k]))
        _check_engine(depth, preset)
    if not isinstance(data["default_depth"], str) or data["default_depth"] not in presets:
        raise ManifestError("default_depth must name a preset (one of %s)" % ", ".join(presets))
    return Manifest(folder, name, data["description"], data["goal"], data["target"], roles,
                    dict(knobs), {d: dict(p) for d, p in presets.items()}, data["default_depth"])


def load(folder):
    """The validated Manifest of `folder`/workflow.json; ManifestError on any problem."""
    path = pathlib.Path(folder) / "workflow.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ManifestError("cannot read %s: %s" % (path, e))
    except ValueError as e:
        raise ManifestError("%s is not valid JSON: %s" % (path, e))
    return validate(data, folder)


def parse_value(kind, text):
    """A --set VALUE parsed as knob type `kind`; ValueError when it does not parse."""
    if kind == "str":
        return text
    if kind == "bool":
        t = text.strip().lower()
        if t in _TRUE:
            return True
        if t in _FALSE:
            return False
        raise ValueError("expected true or false")
    if kind == "int":
        v = int(text.strip())
        if v < 0:
            raise ValueError("expected an integer of at least 0")
        return v
    if kind == "float":
        v = float(text.strip())
        if not math.isfinite(v):
            raise ValueError("expected a finite number")
        return v
    raise ValueError("unknown knob type %r" % kind)


def parse_engine(name, text):
    """A --set VALUE for an engine knob; ValueError when it does not parse."""
    t = text.strip()
    if name == "rounds":
        if t == "until":
            return "until"
        v = int(t)
        if v < 1:
            raise ValueError("expected an integer of at least 1 or until")
        return v
    if name == "hours":
        v = float(t)
        if not (math.isfinite(v) and v > 0):
            raise ValueError("expected a number above 0")
        return v
    v = int(t)
    if v < (1 if name == "budget" else 0):
        raise ValueError("expected an integer of at least %d" % (1 if name == "budget" else 0))
    return v


def parse_sets(m, pairs):
    """{knob: value} from --set KNOB=VALUE strings; ManifestError names the bad one."""
    out = {}
    for pair in pairs:
        name, sep, text = pair.partition("=")
        if not sep or not name:
            raise ManifestError("--set %r is not KNOB=VALUE" % pair)
        try:
            if name in m.knobs:
                out[name] = parse_value(m.knobs[name], text)
            elif name in ENGINE_KNOBS:
                out[name] = parse_engine(name, text)
            else:
                raise ManifestError("--set %s: no such knob (knobs: %s)" % (
                    name, ", ".join(list(m.knobs) + list(ENGINE_KNOBS))))
        except ValueError as e:
            if isinstance(e, ManifestError):
                raise
            raise ManifestError("--set %s=%s: %s" % (name, text, e))
    return out
