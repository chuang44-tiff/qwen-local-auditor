"""Named fences: the only way a workflow chooses an agent's tools and working directory.

Each fence maps to the qwen-agent flags hardened for the released qwen-deep-research. Every unit also gets
--permission-mode dontAsk and --warn-denials from swarm.Swarm. The flag values are part
of each unit's cache key (Swarm._key hashes toolset, grants, web and the mcp.json text, and
--browser for a browser-fenced unit only: every other fence's key is exactly as it was).

| fence   | toolset                        | grants (-t)             | --web | --mcp-config | -C           |
|---------|--------------------------------|-------------------------|-------|--------------|--------------|
| none    | none                           | -                       | no    | no           | agents/<unit>|
| browser | none (--browser)               | -                       | no    | its own      | agents/<unit>|
| browser-probe | none (--browser-eval)    | -                       | no    | its own      | agents/<unit>|
| search  | none                           | mcp__search__search     | no    | yes          | agents/<unit>|
| web     | none                           | mcp__search__search,mcp__search__page | yes | yes        | agents/<unit>|
| read    | Read,Glob,Grep                 | Read,Glob,Grep          | no    | no           | --target     |
| sandbox | Read,Edit,Write,Bash,Glob,Grep | the same six            | no    | no           | a sandbox    |

The `browser` row is `none` plus qwen-agent --browser: qwen-agent writes its own ONE-server
Playwright MCP config and grants the mcp__playwright__ tools itself, so a browser unit
carries no --mcp-config of ours to begin with (qwen-agent refuses the pair). Its
QWEN_BROWSER_DIR is set per unit by api.Workflow to <run>/browser/<unit>: qwen-agent makes a
fresh timestamped folder inside that root and its screenshots and page snapshots land there,
so a unit's evidence stays inside the run folder, beside the unit that made it. It is not a
web fence: a UI suite runs against local URLs, so browser units run at --seats and need no
search preflight.

`browser-probe` is `browser` plus qwen-agent --browser-eval: the session may also run
scripted probes (browser_evaluate) and read single network requests -- what a confirmer
needs to check a verdict directly instead of by eye. Everything said above about `browser`
holds for it; --browser-eval joins the unit's cache key, so its answers never pass for a
plain browser unit's.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

SEARCH_TOOL = "mcp__search__search"
PAGE_TOOL = "mcp__search__page"             # the web fence's second search-server tool
READ_TOOLS = "Read,Glob,Grep"
SANDBOX_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"
WEB_FENCES = ("search", "web")         # run at --web-seats; need mcp.json and a search preflight
BROWSER_FENCES = ("browser", "browser-probe")   # qwen-agent --browser; QWEN_BROWSER_DIR per unit
TARGET_FENCES = ("read", "sandbox")    # need --target
SEARCH_SERVER = pathlib.Path(__file__).resolve().parents[1] / "search_mcp.py"
DESCRIBE = {"none": "no tools", "browser": "a real browser (Playwright)",
            "browser-probe": "a real browser (Playwright) + scripted probes (browser_evaluate)",
            "search": "search",
            "web": "search + page + WebFetch",
            "read": "Read/Glob/Grep in --target", "sandbox": "edit + Bash in a sandbox copy"}


def unit_fields(fence, mcp=None):
    """swarm.Unit keyword arguments for `fence` (cwd is set by the caller for read and
    sandbox, and the browser fence's QWEN_BROWSER_DIR by api.Workflow). `mcp` is the run's
    mcp.json path, required for the web fences."""
    if fence == "none":
        return {"toolset": "none", "grants": "", "web": False, "mcp_config": None}
    if fence == "browser":
        # --browser: qwen-agent writes the Playwright MCP config itself and grants its
        # tools, so the unit asks for no toolset, no grants and no --mcp-config of ours
        # (qwen-agent refuses --browser beside a caller's --mcp-config).
        return {"toolset": "none", "grants": "", "web": False, "mcp_config": None,
                "browser": True}
    if fence == "browser-probe":
        # --browser plus --browser-eval (qwen-agent's own flag; it implies --browser, and
        # both are passed so the argv reads as what it is)
        return {"toolset": "none", "grants": "", "web": False, "mcp_config": None,
                "browser": True, "browser_eval": True}
    if fence in WEB_FENCES:
        if mcp is None:
            raise ValueError("fence %r needs the run's mcp.json" % fence)
        # `web` also gets the search server's `page` tool: fetch a known url once per run
        # and take back only the passages around what the unit is checking, no model call
        return {"toolset": "none",
                "grants": SEARCH_TOOL if fence == "search" else SEARCH_TOOL + "," + PAGE_TOOL,
                "web": fence == "web", "mcp_config": mcp}
    if fence == "read":
        return {"toolset": READ_TOOLS, "grants": READ_TOOLS, "web": False, "mcp_config": None}
    if fence == "sandbox":
        return {"toolset": SANDBOX_TOOLS, "grants": SANDBOX_TOOLS, "web": False,
                "mcp_config": None}
    raise ValueError("unknown fence %r" % fence)


def can_write(fence):
    """True when the fence's toolset includes Edit or Write (today only `sandbox`): the
    roles that can hold a plan-mode first edit. Derived from unit_fields, not a list."""
    tools = set(unit_fields(fence, mcp="-")["toolset"].split(","))
    return bool(tools & {"Edit", "Write"})


def needs_search(manifest):
    return any(r.fence in WEB_FENCES for r in manifest.roles.values())


# --------------------------------------------------------------------------- confinement
# A unit whose grants include Bash (today: the `sandbox` fence) runs under Claude Code's
# Bash sandbox with scoped edit grants and deny rules, so it cannot forge notes, message
# files or asks for other units, the main session or the harness (spec 13). When the host
# cannot confine, the unit still gets the scoped grants and the deny rules, and the run's
# report says why. The sandbox always allows writes to TMPDIR, so a run folder inside
# TMPDIR buys no confinement and is reported as unconfined.

_DENY_DIRS = ("inbox", "msg", "asks", "agents")   # the run's coordination files


def rule_path(path):
    """The path part of a confinement rule: what sits between `Edit(` and `/**)`.
    POSIX: the realpath with the rule grammar's absolute `//` prefix
    (`/srv/run` -> `//srv/run`). Windows (os.name == "nt"): the launcher's
    rule_path() form -- forward slashes, the lowercase drive letter without its
    colon, the `//` prefix (`C:\\work\\run` -> `//c/work/run`)."""
    p = os.path.realpath(str(path))
    if os.name == "nt":
        p = p.replace("\\", "/")
        if len(p) > 2 and p[0].isalpha() and p[1] == ":":
            p = p[0].lower() + p[2:]
    return "//" + p.lstrip("/")


def confine_settings(run_dir, unit, sandbox=True):
    """The Claude Code settings block a Bash unit's session merges into its ONE --settings
    file (qwen-agent --settings-extra). Every deny rule is an `Edit(<rule_path(R)>/...)`
    absolute rule for one of the run's coordination dirs, so a confined unit edits none of
    them (`Write(...)` path rules do not match -- Edit covers both tools). sandbox=False
    drops the sandbox block: the deny rules and, with them, the run of a host that cannot
    confine; failIfUnavailable makes a sandbox that cannot come up a refusal, never an
    unsandboxed session (`unit` names whose unit the block belongs to -- the scoped grant
    for its sandboxes/<unit> folder comes from scoped_grants)."""
    R = rule_path(run_dir)
    out = {"permissions": {"deny": ["Edit(%s/%s/**)" % (R, d) for d in _DENY_DIRS]}}
    if sandbox:
        out = {"sandbox": {"enabled": True, "failIfUnavailable": True,
                           "allowUnsandboxedCommands": False,
                           "autoAllowBashIfSandboxed": True},
               "permissions": out["permissions"]}
    return out


def scoped_grants(grants, run_dir, unit):
    """`grants` (a comma list) with every bare `Edit` and `Write` entry replaced by the
    ONE rule `Edit(<rule_path(R)>/sandboxes/<unit>/**)` at the first of their places;
    every other entry keeps its order. A list without Edit/Write is returned unchanged.
    One Edit rule covers the Write tool too (spec 13). The scoping happens on every
    platform, Windows included (spec 13: deny rules and scoped grants only there): a
    bare grant fails OPEN -- the Write tool then reaches anywhere no deny rule names --
    while a rule spelled wrong fails CLOSED, so the rule is always the safer entry."""
    rule = "Edit(%s/sandboxes/%s/**)" % (rule_path(run_dir), unit)
    out = []
    replaced = False
    for entry in (grants or "").split(","):
        if entry in ("Edit", "Write"):
            if not replaced:
                out.append(rule)
                replaced = True
        else:
            out.append(entry)
    return ",".join(out) if replaced else grants


def _probe_run(argv):
    """Run one sandboxed command with a 10 s leash: None when it ran, else the reason --
    `sandbox probe failed: ` plus its first stderr line (200 chars at most). Never
    `claude -p`: the probe is the sandbox itself, not a model call."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=10)
    except Exception as e:            # the program vanished between PATH and exec, or hung
        return "sandbox probe failed: %s" % ((str(e).splitlines() or [""])[0][:200])
    if p.returncode != 0:
        first = (p.stderr or b"").decode("utf-8", "replace").splitlines()
        return "sandbox probe failed: %s" % ((first or [""])[0][:200])
    return None


def confine_probe(run_dir):
    """Can this host confine a Bash unit of a run at `run_dir`? None = yes; else the
    reason, checked in this order: no Bash sandbox on Windows (deny rules and scoped
    grants only there), a run folder inside the temp folder (the sandbox always lets a
    unit write there, so a note file written through TMPDIR is no longer confined), and
    on Linux the bwrap/socat availability plus one sandboxed `true`. Called once per
    run, before the first confined unit (api.Workflow); any other platform gets no
    sandbox."""
    if os.name == "nt":
        return "no bash sandbox on this platform"
    R = os.path.realpath(str(run_dir))
    tmp = os.path.realpath(tempfile.gettempdir())
    # rstrip keeps the test right for the degenerate TMPDIR=/ (every run folder IS
    # under it); a realpath'd temp dir otherwise carries no trailing separator
    if R == tmp or R.startswith(tmp.rstrip(os.sep) + os.sep):
        return "run folder under TMPDIR"
    if sys.platform.startswith("linux"):
        # bwrap is the sandbox, socat the proxy Claude Code's sandbox needs for any
        # network-visible command: one missing, and the functional probe below would
        # only say what this line already knows (this host's bwrap has failed under
        # AppArmor userns limits before, which is why the probe runs one command).
        # --unshare-net rides the probe because the network namespace is the step
        # that has failed on hosts with userns limits: probing without it would
        # call a host confining that cannot actually isolate the unit's network.
        if shutil.which("bwrap") is None or shutil.which("socat") is None:
            return "sandbox probe failed: bwrap or socat missing"
        return _probe_run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
                           "--unshare-net", "true"])
    if sys.platform == "darwin":
        return _probe_run(["sandbox-exec", "-p", "(version 1)(allow default)", "true"])
    return "no bash sandbox on this platform"


def mcp_config(run):
    """Write <run>/mcp.json for the search MCP server and return its path. Only the
    non-secret backend settings go in; QWEN_SEARCH_KEY reaches the server by environment
    inheritance and is never written to the run folder. (Moved verbatim out of research.py.)"""
    env = {"PYTHONUTF8": "1"}
    for k in ("QWEN_SEARCH_BACKEND", "QWEN_SEARCH_URL", "QWEN_SEARCH_BRAVE_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    cfg = {"mcpServers": {"search": {"type": "stdio", "command": sys.executable,
                                     "args": [str(SEARCH_SERVER)], "env": env}}}
    path = pathlib.Path(run) / "mcp.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
