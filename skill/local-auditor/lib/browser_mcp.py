"""The Playwright MCP server entry: the one place that knows how the browser server starts.

qwen-agent --browser writes it to <browser run folder>/mcp.json through the CLI below,
and qwen-swarm's wf.claude_check(browser=True) builds the same entry in-process, so a
local tester and its Claude confirmer drive the same browser the same way.

  build(output_dir, headed=False, env=None) -> {"mcpServers": {"playwright": {...}}}
  text(cfg)                                  -> the file's exact text (indent 2, "\\n")
  python -m lib.browser_mcp --output-dir DIR [--headed]    the JSON on stdout
  python lib/browser_mcp.py --output-dir DIR [--headed]    the same (qwen-agent's form:
                                             no `lib` lookup in the caller's directory)

The command: QWEN_PLAYWRIGHT_MCP when it is not blank -- a whole command line, split on
spaces, tabs and newlines with no pathname expansion ("node /x/*.js" stays one literal
word), the first word the command and the rest its leading args -- else
`npx -y --prefer-offline @playwright/mcp@0.0.83`. On Windows that default is started
through `cmd /c`: native Windows Claude Code cannot spawn npx (it is npx.cmd) as a stdio
MCP server, the connection just closes. Windows means os.name == "nt", or cygpath on the
env's PATH -- the Git Bash rule qwen-agent has always used, so a Cygwin-style Python
behind Git Bash gets the same form.
Then the browser switches: --headless unless headed, --isolated, --output-dir (the
caller passes the NATIVE spelling: claude starts the server natively), image responses
allowed, a 1280x900 viewport.
headed copies the caller's display into the server's env: the X display when there is
one (DISPLAY, plus XAUTHORITY), else the Wayland socket (WAYLAND_DISPLAY, plus
XDG_RUNTIME_DIR, where it lives). Never an empty DISPLAY key on a Wayland session: it
would only send the server looking for an X display.
"""
import argparse
import json
import os
import re
import shutil
import sys

PACKAGE = "@playwright/mcp@0.0.83"
DEFAULT = ("npx", "-y", "--prefer-offline", PACKAGE)
VIEWPORT = "1280,900"
OS_NAME = os.name          # module-level so a test can force the Windows branch ("nt")


def _windows(env):
    if OS_NAME == "nt":
        return True
    return shutil.which("cygpath", path=env.get("PATH") or os.defpath) is not None


def command(env):
    """(command, leading args) of the server, before the browser switches."""
    raw = env.get("QWEN_PLAYWRIGHT_MCP") or ""
    if raw.strip():
        words = [w for w in re.split(r"[ \t\n]+", raw) if w]
    else:
        words = (["cmd", "/c"] if _windows(env) else []) + list(DEFAULT)
    return words[0], words[1:]


def _display(env):
    out = {}
    if env.get("DISPLAY"):
        out["DISPLAY"] = env["DISPLAY"]
        if env.get("XAUTHORITY"):
            out["XAUTHORITY"] = env["XAUTHORITY"]
    elif env.get("WAYLAND_DISPLAY"):
        out["WAYLAND_DISPLAY"] = env["WAYLAND_DISPLAY"]
        if env.get("XDG_RUNTIME_DIR"):
            out["XDG_RUNTIME_DIR"] = env["XDG_RUNTIME_DIR"]
    return out


def build(output_dir, headed=False, env=None):
    """The MCP config holding the one Playwright server; env defaults to os.environ."""
    env = os.environ if env is None else env
    cmd, args = command(env)
    if not headed:
        args.append("--headless")
    args += ["--isolated", "--output-dir", str(output_dir),
             "--image-responses", "allow", "--viewport-size", VIEWPORT]
    server = {"command": cmd, "args": args}
    if headed:
        server["env"] = _display(env)
    return {"mcpServers": {"playwright": server}}


def text(cfg):
    """The config file's text, byte for byte what qwen-agent has always written."""
    return json.dumps(cfg, indent=2) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="browser_mcp.py")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--headed", action="store_true")
    o = ap.parse_args(argv)
    data = text(build(o.output_dir, o.headed)).encode("utf-8")
    # bytes, not print(): a text stdout on Windows would turn every "\n" into "\r\n"
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        sys.stdout.write(data.decode("utf-8"))
    else:
        out.write(data)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
