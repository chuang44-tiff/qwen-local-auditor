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
| web     | none                           | mcp__search__search     | yes   | yes          | agents/<unit>|
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
import sys

SEARCH_TOOL = "mcp__search__search"
READ_TOOLS = "Read,Glob,Grep"
SANDBOX_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"
WEB_FENCES = ("search", "web")         # run at --web-seats; need mcp.json and a search preflight
BROWSER_FENCES = ("browser", "browser-probe")   # qwen-agent --browser; QWEN_BROWSER_DIR per unit
TARGET_FENCES = ("read", "sandbox")    # need --target
SEARCH_SERVER = pathlib.Path(__file__).resolve().parents[1] / "search_mcp.py"
DESCRIBE = {"none": "no tools", "browser": "a real browser (Playwright)",
            "browser-probe": "a real browser (Playwright) + scripted probes (browser_evaluate)",
            "search": "search",
            "web": "search + WebFetch",
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
        return {"toolset": "none", "grants": SEARCH_TOOL, "web": fence == "web",
                "mcp_config": mcp}
    if fence == "read":
        return {"toolset": READ_TOOLS, "grants": READ_TOOLS, "web": False, "mcp_config": None}
    if fence == "sandbox":
        return {"toolset": SANDBOX_TOOLS, "grants": SANDBOX_TOOLS, "web": False,
                "mcp_config": None}
    raise ValueError("unknown fence %r" % fence)


def needs_search(manifest):
    return any(r.fence in WEB_FENCES for r in manifest.roles.values())


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
