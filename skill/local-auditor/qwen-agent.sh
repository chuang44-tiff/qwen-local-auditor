#!/usr/bin/env bash
# qwen-agent — run a headless Claude Code session against a locally served model.
#
# Claude Code cannot route an individual subagent to a different provider
# (ANTHROPIC_BASE_URL is session-wide), so we spawn a separate headless process
# instead. Any server that speaks the Anthropic Messages API (/v1/messages) and
# lists its models at /v1/models can be the target.
#
# Run `qwen-agent --help` for usage.  Exit codes are documented there and are
# deliberately distinct so a caller can tell failure modes apart.

set -uo pipefail

# A caller's QA_META/QA_META_* are unset before anything else, so they can reach
# nothing: `extract` reads them from the environment, and an inherited QA_META=1
# would bolt a qwen_agent key onto a run that used no switch while the QA_META_*
# values would describe switches this run never made. export_meta (below) is the
# only code that sets them, and only for the child it is about to parse. The
# ${!PREFIX@} expansion lists QA_META itself plus every QA_META_*, and works in
# bash 3.2 even under POSIXLY_CORRECT -- the compgen + process substitution it
# replaces does not. Variable names hold no whitespace, so the unquoted expansion
# splits into exactly the names it lists (and runs this loop in THIS shell).
# shellcheck disable=SC2068,SC2086  # deliberate word list: one word per variable name
for _qa_v in ${!QA_META@}; do unset "$_qa_v"; done
unset _qa_v

_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SKILL_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
unset _src _dir

QA_VERSION="3.0"
QA_SELF="qwen-agent"           # the forwarder execs qwen-agent.sh; users type qwen-agent

# ---------------------------------------------------------------- exit codes
QA_OK=0            # success
QA_USAGE=2         # bad flags / no prompt
QA_PREFLIGHT=3     # server unreachable, or model not served there
QA_APIERR=4        # the endpoint returned an API error
QA_TIMEOUT=5       # wall-clock timeout tripped
QA_EMPTY=6         # ran clean but produced no usable text
QA_DENIED=7        # a tool call was blocked by the permission system
QA_HARNESS=8       # claude binary missing, or its output was unparseable
QA_SCENARIO_FAIL=9 # --scenarios: at least one scripted scenario ended FAIL or BLOCKED

# Parent-session variables NOT to pass down to the headless child:
#  - the parent's control channel and session identity;
#  - provider routing and credentials (Bedrock / Vertex / Foundry, a real API key,
#    custom headers), which would send the prompt -- code excerpts included --
#    somewhere other than the local server;
#  - model and effort overrides, which are set explicitly for the child below.
SCRUB_LIST='CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_BRIDGE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_EFFORT CLAUDE_CODE_EFFORT_LEVEL CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY AWS_BEARER_TOKEN_BEDROCK ANTHROPIC_API_KEY ANTHROPIC_CUSTOM_HEADERS ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL CLAUDE_CODE_SUBAGENT_MODEL'
SCRUB_ARGS=""
for _v in $SCRUB_LIST; do SCRUB_ARGS="$SCRUB_ARGS -u $_v"; done
unset _v

# ------------------------------------------------------------- machine config
# Endpoint/model/interpreter differ per machine, so they live in a config file
# rather than in this script -- keeping the script itself portable and letting a
# host be re-pointed without editing (and re-diffing) 900 lines of shell.
QA_CONFIG="${QWEN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/config}"
if [ -f "$QA_CONFIG" ] && [ -r "$QA_CONFIG" ]; then
  # CRs stripped first: a config saved by a Windows editor would otherwise leave
  # a trailing \r on every value (and a URL that silently never connects).
  eval "$(tr -d '\r' < "$QA_CONFIG")"
fi
# Native Windows Python defaults to cp1252 for files and pipes; model output is UTF-8.
export PYTHONUTF8=1

# ------------------------------------------------------------------ defaults
BASE="${QWEN_BASE_URL:-http://127.0.0.1:8000}"
# Empty MODEL = use the served model when exactly one is served (checked in
# preflight). Set it when the server hosts several.
MODEL="${QWEN_MODEL:-}"
# Empty CTX = read the model's max_model_len from /v1/models when the server
# reports one (vLLM does); otherwise claude's own default stays in place.
CTX="${QWEN_CTX:-}"
# A local server rejects an oversized request with a 400; the client packs input
# up to CTX minus its output reservation and was observed going ONE token over
# (230145 vs a 230144 budget on a 262144 window). AUTOCOMPACT makes long runs
# summarise-and-continue instead of dying at that wall. 'default' = 3/4 of CTX
# when CTX is known, so the boundary is never the thing under test.
AUTOCOMPACT="${QWEN_AUTOCOMPACT:-default}"
# Effort is passed through to claude --effort. Some local chat templates accept
# only a subset of levels (one Qwen template rejects 'high' with a 400): list the
# accepted ones in QWEN_EFFORT_ALLOWED to fail fast instead of mid-run.
EFFORT="${QWEN_EFFORT:-medium}"
EFFORT_ALLOWED="${QWEN_EFFORT_ALLOWED:-}"
TIMEOUT="${QWEN_TIMEOUT:-1800}"
TIMEOUT_EXPLICIT=0                       # --timeout typed, or QWEN_TIMEOUT (env or config) set
[ -n "${QWEN_TIMEOUT:-}" ] && TIMEOUT_EXPLICIT=1
TIMEOUT_BIN=""                           # resolved below (env QWEN_TIMEOUT_BIN; 'none' = built-in watchdog)
CLAUDE_BIN="${QWEN_CLAUDE_BIN:-claude}"
SETTING_SOURCES="${QWEN_SETTING_SOURCES:-}"
QA_PY=""                                 # resolved below (env QWEN_PYTHON to pin it)
ROLE_DIR="${QWEN_ROLE_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/roles}"

# --allowed-tools GRANTS permission; it does NOT restrict the toolset, so these
# only matter when Bash is actually in the toolset (--write / --all-tools).
# Deliberately no sed and no find: `sed -i` and `find -delete`/`-exec` are
# arbitrary mutation, and granting them by default made the "read-only" default
# write-capable.
GRANTS_DEFAULT='Bash(ls:*),Bash(grep:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Read,Glob,Grep'
GRANTS_WRITE="$GRANTS_DEFAULT,Edit,Write,MultiEdit,NotebookEdit"

# --tools IS the real restriction: it removes every built-in not named here.
TOOLSET_READONLY='Read,Glob,Grep'
TOOLSET_WRITE='Read,Edit,Write,Glob,Grep'

TOOLS=""            # --allowed-tools : GRANTS permission, does not restrict
TOOLS_EXPLICIT=0
TOOLSET=""          # --tools         : RESTRICTS the available built-in set
TOOLSET_EXPLICIT=0
DISALLOWED=""       # --disallowedTools: HIDES tools from the model, grant or no grant
READ_ONLY_FLAG=0    # --read-only given (refused with --test)
WRITE_MODE=0       # --write     : allow Edit/Write
ALL_TOOLS=0         # --all-tools : no restriction at all (dangerous)
WEB_MODE=0          # --web       : web access is opt-in (adds WebFetch only, never WebSearch)
case "${QWEN_WEB:-}" in 1) WEB_MODE=1 ;; esac
SUBAGENTS=0         # --subagents : Task tool (default depth sets it) (each subagent is one more concurrent request)
case "${QWEN_SUBAGENTS:-}" in 1) SUBAGENTS=1 ;; esac
BROWSER=0           # --browser : a real browser through the Playwright MCP server (role tester implies it)
HEADED=0            # --headed      : --browser with a visible window (implies --browser)
BROWSER_EVAL=0      # --browser-eval: add mcp__playwright__browser_evaluate (implies --browser)
BROWSER_DIR=""      # the browser run folder: made just before claude starts, kept after
                    # the run -- it is the evidence
BROWSER_ROOT=""     # its parent (QWEN_BROWSER_DIR or the cache dir), resolved against the
                    # caller HERE, before the -C chdir; created with the folder itself
SCENARIOS=""        # --scenarios FILE: the scripted UI suite (implies --browser and -r tester)
SCENARIOS_GIVEN=""  # FILE as the caller typed it, for the --json record
SCEN_RESULTS=""     # the results.json written into the browser run folder,
SCEN_PASS="" SCEN_FAIL="" SCEN_BLOCKED=""   # and the per-status counts of its summary
RECORD=""           # --record DIR: after the suite is scored, resume the session ONCE to
                    # write one deterministic replay script per scenario, and keep in DIR
                    # the scripts whose replay reproduces the verdict the run recorded
REPLAY=""           # --replay DIR: run DIR's recorded scripts with NO model call at all
STRICT_MCP=0
STRICT_MCP_EXPLICIT=0
MCP_CONFIG=""       # --mcp-config: load ONLY the MCP servers named in this file
TOOLSET_NONE=0      # --toolset none: pass --tools "" (no built-in tool at all)
NO_TIMEOUT=0
FORCE=0
OUT=""
BG=0
ROLE=""
ROLE_FILE=""
EXTRA_SYS=""
PROMPT=""
PROMPT_SET=0
PROMPT_FILE=""
READ_STDIN=0
WORKDIR=""
ADD_DIRS=()
JSON_OUT=0
DRY_RUN=0
QUIET=0
AUTO_MODEL=0
case "${QWEN_AUTO_MODEL:-0}" in 1|true|yes) AUTO_MODEL=1 ;; esac
NO_PREFLIGHT=0
case "${QWEN_PREFLIGHT:-1}" in 0|false|no) NO_PREFLIGHT=1 ;; esac
PREFLIGHT_ONLY=0
WARN_DENIALS=0
PERM_MODE=""
PERM_MODE_EXPLICIT=0  # --permission-mode given by the caller (refused with --test)
RESUME_ID=""
INTERACTIVE=0       # --interactive : hand the keyboard to a person: exec claude with no -p and no fence
TEST_MODE=0         # --test : grant qwen-test, give the run a throwaway worktree
TEST_REPO=""        # --test-repo : the repo whose tests run (default: the -C dir)
TEST_WT=""
ROLE_VARIANT=""     # --role-variant NAME : the NAME variant of the built-in -r role (auditor/coder: deep)
NUDGE=0             # --subagents-nudge   : --subagents plus a section on when and how to delegate
PUSH=0              # --subagents-push    : --subagents plus a section that makes delegation
                    #                       mandatory; it REPLACES the nudge text
REVIEW_ROUND=0      # --review-round      : resume the finished session once with REVIEW_PROMPT
PROBE=0             # --probe        : run in a throwaway sandbox of the project, with a shell
PROBE_EXPLICIT=0    # --probe given by the caller (not the one --deep implies)
PROBE_HERE=0        # --probe-here   : the -C directory already is a probe sandbox
KEEP_SANDBOX=0      # --keep-sandbox : do not remove the --probe sandbox at exit
PROBING=0           # PROBE or PROBE_HERE (set after parsing)
PROBE_RUN=""        # the probe run folder, the sandbox, the session's directory in it,
PROBE_SB=""         # and the directory the sandbox copies (all from lib/probe.py create)
PROBE_CWD=""
PROBE_SRC=""
PROBE_PATCH=""      # the patch file a --probe --write run wrote
DEEP=0              # --deep : all four switches above (V, R, PUSH, P)
SHALLOW=0           # --shallow : no depth switch is implied; only the typed ones apply
DEPTH_MODE=""       # computed after parsing: shallow | deep (--deep typed) | default (implied)
SHORT_ANSWER_WARNING=0  # the final answer was short after a long session (set in emit)

# --------------------------------------------------------------------- help
usage() {
  cat <<EOF
$QA_SELF v$QA_VERSION — headless Claude Code session on a locally served model.

USAGE
  $QA_SELF [options] <prompt>...
  $QA_SELF [options] -f prompt.md
  cat task.md | $QA_SELF [options] --stdin
  $QA_SELF --interactive [-C DIR]   an interactive session for a person

PROMPT INPUT (exactly one)
  <prompt>...          Positional. Multiple words are joined with a space.
                       Quotes, newlines, backticks and \$ are passed through
                       verbatim — nothing is eval'd. A prompt starting with '-'
                       is safe (an internal '--' separator is used).
  -f, --prompt-file F  Read the prompt from file F ('-' means stdin).
      --stdin          Read the prompt from stdin.

ROLE / SYSTEM PROMPT
  -r, --role NAME      Prepend a role. Built-ins: auditor, coder, mechanic,
                       plain, tester (browser testing; implies --browser).
                       Also resolves \$QWEN_ROLE_DIR/NAME.md (or .txt), then a
                       literal file path. Roles are APPENDED to Claude Code's
                       own system prompt, never replacing it (replacing it
                       breaks tool use).
      --role-file F    Use file F as the role text.
  -s, --system TEXT    Extra system-prompt text, appended after the role.
      --list-roles     Print resolvable role names and exit.

MODEL / ENDPOINT
  -m, --model NAME     Served model name. Default: the served model, when the
                       server serves exactly one.  (env QWEN_MODEL)
  -b, --base URL       Base URL, no /v1 suffix. Default $BASE
                       (env QWEN_BASE_URL)
  -e, --effort LEVEL   Passed to claude --effort. Default medium. (env QWEN_EFFORT)
                       Some chat templates reject some levels: set e.g.
                       QWEN_EFFORT_ALLOWED="low medium xhigh" to refuse anything
                       else before a model call is made.
      --ctx N          Context window. Default: the model's max_model_len from
                       /v1/models when reported, else claude's own default.
                       (env QWEN_CTX)
      --autocompact N  Auto-compact window, passed to claude as --autocompact.
                       'auto', or 100000-1000000 and below the context window.
                       Default: 3/4 of the window when it is known, so a long
                       run compacts and continues instead of hitting the
                       server's hard limit (a 400 that kills the run outright).
                       (env QWEN_AUTOCOMPACT)
      --no-autocompact Do not pass --autocompact at all (claude's own default).
      --auto-model     If the configured model is not served but exactly one
                       other model is, use that one instead of failing.
      --no-preflight   Skip the /v1/models reachability check.
      --preflight-only Run the checks and exit (0 = usable; 2, 3 or 8 = not). No
                       model call. Also runs when QWEN_PREFLIGHT=0.

CONFIG FILE
  \$QWEN_CONFIG, default \$XDG_CONFIG_HOME/qwen-agent/config (else
  ~/.config/qwen-agent/config). Sourced as shell before the defaults are
  applied, so it is the right place for the per-machine endpoint:
      QWEN_BASE_URL="\${QWEN_BASE_URL:-http://192.0.2.10:8000}"
      QWEN_MODEL="\${QWEN_MODEL:-my-served-model}"
      QWEN_PYTHON="\${QWEN_PYTHON:-python}"
  \$QWEN_PYTHON pins the interpreter used to parse the JSON result. It is
  probed by EXECUTION, not by PATH presence -- on Windows a bare 'python3' is
  usually a Store stub that is on PATH but cannot run anything.

EXECUTION  (the DEFAULT never writes your tree — mutation must be asked for; see SAFETY)
      --interactive    Open an INTERACTIVE Claude Code session (no -p) on this
                       server for the person at the keyboard, in the -C directory
                       (default: the current one). Preflight, model, context and
                       effort resolve exactly as for a headless run, and so does
                       the child environment (every model alias, the window, the
                       effort, the scrubbed parent-session variables). NO fence
                       flags are passed at all — no --tools, --allowed-tools,
                       --restricted, --permission-mode, --output-format,
                       --append-system-prompt or --strict-mcp-config: the person
                       answers Claude Code's own permission prompts. --dry-run
                       prints the command line and the redacted environment
                       instead of running. Refused with a prompt, -f/--stdin,
                       --until-done, --test, --write, --all-tools/--unrestricted,
                       --toolset, --read-only, --strict-mcp, -t/--tools, --web,
                       --subagents, --json, -o, -w, --resume, -r/--role,
                       --role-file and -s/--system — each belongs to a headless
                       run and would be silently dropped here. --timeout does not
                       apply: the person ends the session. Launch one from inside
                       Claude Code with qwen-cc.
      --write          Let the run modify files: --toolset '$TOOLSET_WRITE',
                       and --permission-mode acceptEdits unless you set one.
                       Still no Bash. Role 'mechanic' implies this.
      --test           Let the run execute the project's tests through qwen-test
                       (the ONLY shell command granted -- a --test run also gets
                       no implied --probe: the sandbox would delete the
                       reproduction test with itself, so the full shell there is
                       only what a typed --probe brings). The test command is
                       QWEN_TEST_CMD from the config; the model only picks which
                       tests. Runs in a throwaway git worktree. Read-only runs may
                       write ONLY inside that worktree (reproduction tests, listed
                       under '## REPRO FILES' in the result). Always passes
                       claude --restricted (user/project settings files are
                       ignored, file tools confined to the working dirs) and
                       --permission-mode dontAsk; write/coder runs still edit
                       because --allowed-tools grants the edit tools. Needs a
                       claude that has --restricted. Not with -w, --all-tools,
                       --toolset, --read-only, -t/--tools or any
                       --permission-mode. The
                       tests run the repo's code as you: use it only on code
                       you would run.
      --test-repo DIR  Repo whose tests run (default: the -C directory).
      --all-tools      No toolset restriction at all: every built-in, including
                       Bash and Write, plus any configured MCP servers. This is
                       the widest setting; a warning is printed. Costs many
                       more input tokens per run (every tool schema is sent).
      --web            Web access is opt-in: adds WebFetch to --tools and to
                       --allowed-tools (every role, and to the fixed --test
                       grant list; --all-tools needs none of this). Off by
                       default. Never WebSearch: it is a server-side tool that
                       local servers (vLLM) reject with a 400
                       "body.tools.0.input_schema Field required"; search needs
                       an MCP server. With --test a warning is printed: tests
                       and checks can be gamed by fetching upstream answers.
                       (env QWEN_WEB=1)
      --subagents      Adds the Task tool (already on under default depth), so the model can
                       hand broad reading and searching to a subagent and keep
                       its own context small. A subagent runs on the same model
                       with the same tool limits, and is one more concurrent
                       request: leave it off on a small GPU. Depth turns it on --
                       every session gets it (opt-in again with --shallow).
                       (env QWEN_SUBAGENTS=1)
      --toolset LIST   Passed to claude as --tools — the REAL restriction: it
                       removes every built-in tool you do not name. Overrides
                       --write/--all-tools. Default: '$TOOLSET_READONLY'.
                       'none' removes every built-in tool.
      --read-only      Explicit form of the default (--toolset
                       '$TOOLSET_READONLY' --strict-mcp).
  -t, --tools LIST     Passed to claude as --allowed-tools. This GRANTS
                       permission for tools that ARE in the toolset; it does
                       not restrict anything and it is NOT a sandbox. Only
                       meaningful together with --write or --all-tools.
                       Default grants include read-only Bash (ls, grep, cat,
                       head, tail, wc); they take effect only in a run whose
                       toolset has Bash (--toolset ...,Bash or --all-tools).
      --strict-mcp     Add --strict-mcp-config, dropping configured MCP servers
                       (--toolset governs built-ins only; MCP tools survive it).
                       On by default; --all-tools turns it off.
      --mcp-config FILE  Load only the MCP servers in FILE (implies --strict-mcp), even with --all-tools.
                       Grant their tools with -t, e.g. -t mcp__search__search.
      --permission-mode M   Passed through to claude (e.g. acceptEdits, plan).
  -C, --cd DIR         chdir here before running (tool access is rooted at cwd).
  -D, --add-dir DIR    Extra readable directory. Repeatable.
      --timeout SECS   Wall clock limit. Default: 3600 whenever depth is on
                       (the default), 1800 with --shallow; a typed --timeout
                       or QWEN_TIMEOUT always wins.  (env QWEN_TIMEOUT)
                       0 is refused: it would mean "no timeout". Uses GNU
                       timeout (or gtimeout) when present, else a built-in
                       watchdog; stock macOS ships neither. QWEN_TIMEOUT_BIN
                       pins one, and 'none' forces the watchdog.
      --no-timeout     Really run with no wall-clock limit. Say it out loud.

OUTPUT
  -o, --out FILE       Write the result to FILE instead of stdout. Relative to the
                       current directory, not to --cd.
  -w, --detach         Run in the background; print the output path and exit.
                       Implies -o; a unique name is generated if none is given.
                       Writes FILE, FILE.err and FILE.status alongside. FILE
                       does not exist until the run finishes — poll for a
                       non-empty FILE.status, not for FILE.
      --force          With -w -o FILE, overwrite an existing FILE/.status
                       instead of refusing (two jobs sharing one -o clobber
                       each other's sidecars).
      --json           Emit Claude Code's full JSON result record, not just text.
      --resume ID      Continue Claude Code session ID (passed to claude --resume).
                       The session id of every run is printed on the status line.
      --warn-denials   Treat tool-permission denials as a warning, not a failure.
  -q, --quiet          Suppress the stderr status line.
      --dry-run        Print the exact command that would run, then exit.
  -h, --help           This text.
  -V, --version        Print version.

UNTIL DONE  (a coding task with a checklist, checked by the harness)
      --until-done TASK  Work on TASK (a task file with '- [ ] text -- check: ...'
                       items) until every check passes. Each round resumes the
                       same session with what still fails. Implies -r coder --test.
      --max-rounds N   Default 8.
      --budget-tokens N / --budget-seconds N   Stop early when spent.
      --allow-dirty    Start even with uncommitted changes.
      --no-deviation-audit  Skip the read-only spec-vs-diff audit after checks pass.

DEPTH  (the default, not the extra: unless --shallow is given every direct run gets
      the switches below that FIT it, and --json records which ones under qwen_agent.
      V goes to a role that has a deep variant; R and the delegation push (see
      --subagents-push) go to every session; P goes to a read-only run.
      Parts that do not fit are silently dropped, and an implied
      part never causes a refusal -- a typed switch keeps every refusal it has ever
      had. A depth run also defaults --timeout to 3600 (1800 with --shallow).
      QWEN_DEPTH=shallow is the environment form of --shallow.)
      --shallow        No implied depth: only the switches you type. This is the
                       quick-question mode: one read-only call. With --deep: exit 2
                       (depth is already the default -- type what you want, or
                       nothing). --shallow plus a typed part = only that part.
      --role-variant deep  The deep variant of -r auditor or -r coder: the auditor
                       maps what the code must guarantee, tries to break each
                       guarantee and records evidence for every verdict; the
                       coder adds an edge-case pass after the checks. Implied
                       for exactly these two roles. Other roles, --role-file and
                       other variant names: exit 2 when typed.
      --subagents-nudge  --subagents plus a section on when to delegate
                       (independent probes, big or many files, long logs), what
                       to hand a subagent, and to verify what it reports. Typed,
                       and what qwen-sweep batches and the swarm role default
                       give; --deep and the implied default use --subagents-push
                       instead. The delegation section -- nudge or push -- is
                       implied on every session (not --interactive, --resume,
                       --preflight-only or an explicit --toolset none; --dry-run
                       shows it, because it is part of the argv a real run
                       execs).
      --subagents-push  --subagents plus a section that makes delegation part of
                       the task: split the work into independent areas, keep one,
                       hand every other area to a subagent one at a time with a
                       self-contained brief, verify each subagent's claims, and
                       close with a DELEGATION section. It REPLACES the nudge
                       text when both apply (push wins). Implied by --deep and
                       by the default depth; refused with --interactive.
      --review-round   When the session has ended cleanly, resume it once with a
                       fixed prompt: try to break what you just did or reported,
                       check each claim, revise. The revised answer is the result.
                       If the review round fails -- or returns a stub under 300
                       characters while the first answer had 1000 or more -- the
                       first answer stands (a WARNING on stderr). Each of the two
                       calls gets the full --timeout. With --until-done: one
                       review round after the checks first pass, then every check
                       runs again; it counts toward --max-rounds. Implied on
                       every session (same exclusions as --subagents-nudge).
      --probe          Run in a throwaway sandbox of the project -- the git work
                       tree holding -C (or --test-repo DIR), uncommitted and
                       untracked files included -- with a shell: Bash, Edit and
                       Write, --permission-mode dontAsk, claude --restricted.
                       Your tree is only read. A --write run (coder, mechanic)
                       reports its edits as a patch and applies nothing:
                       FILE.patch next to -o FILE, else a new
                       qwen-agent-XXXXXXXX.patch in QWEN_OUTDIR when you set
                       it, else in the probe directory itself -- never in the
                       tree being probed; the path is printed. The sandbox is
                       removed at exit. Sandboxes (and that default patch)
                       live under QWEN_PROBE_DIR (default
                       \$XDG_CACHE_HOME/qwen-agent/probes) and isolate against
                       accidents, not against a hostile model. Not with
                       --interactive, -w, --resume, --all-tools, --toolset,
                       --read-only, -t/--tools, --permission-mode or -D -- and
                       not implied for such a run, for a --test run (its
                       reproduction test lives in the worktree, not in a sandbox
                       that ends with the run) or wherever the sandbox cannot be
                       built either: an implied --probe steps aside exactly
                       where a typed one refuses (one note on stderr, the run
                       goes on unsandboxed), and for any other read-only run it
                       is the default.
                       With --until-done the whole loop runs in one sandbox (the
                       checks too), the patch is RUN/probe.patch (printed as
                       'patch: PATH' before 'report: PATH'), and a dirty tree
                       needs no --allow-dirty.
      --keep-sandbox   With --probe: keep the sandbox and print its path.
      --probe-here     The -C directory is inside a probe sandbox (one kept with
                       --keep-sandbox): the --probe fence, nothing created or
                       removed, no patch. Use it to --resume a probe session.
      --deep           All four TYPED at once: --probe --role-variant deep
                       --review-round --subagents-push, with every typed
                       refusal intact (the implied default drops what does not
                       fit instead of refusing -- deep's own --probe still steps
                       aside on a sandbox that cannot be built, like the implied
                       one, unless the run writes: then the refusal stands).
                       Needs -r auditor or -r coder, or --until-done.
                       With --shallow: exit 2.

BROWSER  (opt-in: give the run a real browser through the Playwright MCP server)
      --browser        Load exactly ONE MCP server, "playwright", which qwen-agent
                       writes (npx -y --prefer-offline @playwright/mcp@0.0.83,
                       headless, isolated; on Git Bash 'cmd /c npx -y
                       --prefer-offline @playwright/mcp@0.0.83' -- native Claude
                       Code cannot spawn npx directly) into a fresh browser run
                       folder and passes with --mcp-config --strict-mcp-config: no
                       other MCP server loads. The mcp__playwright__ browser tools
                       join the run's --allowed-tools list on top of whatever
                       toolset the run already has. Screenshots and page snapshots
                       land in that folder; its path is printed on stderr and the
                       folder is kept after the run -- it is the evidence. The
                       folder is made only when the run actually starts: a refused
                       run, a failed preflight and --dry-run leave none (--dry-run
                       shows '<browser run folder>/mcp.json' as the config path).
                       The browser opens any URL, the internet included,
                       independently of --web; with --test a warning is printed:
                       tests and checks can be gamed by browsing upstream answers.
                       The server offers more tools than are granted; the ones no
                       run gets (browser_run_code_unsafe, browser_install, and
                       browser_evaluate and browser_network_request without
                       --browser-eval) are passed as --disallowedTools too, so
                       the model never sees them.
                       Refused with --mcp-config, --until-done and --interactive;
                       allowed with --probe and --write. Role 'tester' implies
                       this. (env QWEN_BROWSER_DIR, QWEN_PLAYWRIGHT_MCP)
      --headed         --browser with a visible window instead of headless. Needs
                       DISPLAY or WAYLAND_DISPLAY set, else exit 2; DISPLAY (and
                       XAUTHORITY when set), else WAYLAND_DISPLAY (and
                       XDG_RUNTIME_DIR when set), is passed to the browser.
                       Implies --browser.
      --browser-eval   --browser plus the two source-reaching browser tools:
                       mcp__playwright__browser_evaluate (JavaScript run inside
                       the page, the one browser tool that can do anything the
                       page can) and mcp__playwright__browser_network_request
                       (one request WITH its response body). Both off by
                       default -- and hidden from the model's tools until asked
                       for. Implies --browser.
      --scenarios FILE Scripted UI suite for the tester: implies --browser and
                       -r tester. The file (format: reference/qwen-agent.md,
                       "Scripted UI suites") is validated FIRST -- a parse error
                       is exit 2 with the line named and claude never started.
                       The prompt IS the suite's task text built by
                       lib/scenarios.py, so a prompt argument, -f, --stdin,
                       --until-done, --interactive and another -r are all
                       refused (exit 2). After the run the answer is scored
                       against the suite: <browser run folder>/results.json and
                       summary.md are written, the summary table and
                       'results: PATH' print on stderr (the answer keeps stdout
                       or -o), and the exit is $QA_SCENARIO_FAIL when any
                       scenario is FAIL or BLOCKED -- unless the run itself
                       failed, whose code then wins.
      --record DIR     With --scenarios: turn the suite into a replayable recording.
                       After the scoring, the session is resumed ONCE with a fixed
                       prompt that writes one deterministic Playwright script per
                       scenario (<browser run folder>/replay/<id>.mjs; for that call
                       alone the session may write inside that replay folder, and
                       nowhere else). Every script is then replayed and kept only
                       when it answers what the run answered: PASS replays PASS, FAIL
                       replays FAIL. A BLOCKED or unreported scenario gets no script,
                       and a script that disagrees is rejected with its reason. The
                       kept scripts are copied into DIR with a manifest.json; the
                       table 'recorded: N of M scenarios (rejected: ids)' and the
                       manifest path print on stderr. Needs node and the playwright
                       package, like --replay. Replay the result: --replay DIR.
                       The scripts are model-written JavaScript: --record runs
                       them with node, as you, unsandboxed and with your network,
                       before deciding which to keep. Read DIR before relying on it.
      --replay DIR     Run DIR's recorded scripts with NO model call at all -- no
                       preflight, no claude: seconds, and deterministic for the
                       same page state (a changed app can change the result).
                       The scripts run with node, as you, unsandboxed and with
                       your network: replay only folders you recorded or have read.
                       Each script's sha256 is verified against the manifest first (a
                       script that changed since recording is ERROR and never runs),
                       then node runs it (120 s each) and its 'RESULT <id> ...' line
                       is the verdict: PASS, FAIL (with the expectation that did not
                       hold) or ERROR (it crashed, printed no RESULT line, timed
                       out, or printed its own error as a FAIL). The summary table
                       and 'PASS n / FAIL n / ERROR n' print on stderr, with the
                       scenarios the recording kept no script for listed as NOT
                       RECORDED; exit 0 when every kept script
                       PASSED, 9 when any is FAIL or ERROR or none was kept. Every model-related flag (-r, -f, a prompt,
                       --scenarios, --until-done, --interactive, --deep, a model, a
                       timeout) is refused with exit 2 — a replay asks nothing. -b
                       and -q are accepted: -b URL is the base the scripts open.
                       Needs node on PATH and the playwright package.

ENVIRONMENT  (also settable in the config file; flags win)
  QWEN_BASE_URL, QWEN_MODEL, QWEN_CTX, QWEN_AUTOCOMPACT, QWEN_EFFORT, QWEN_TIMEOUT
                       Defaults for the flags above.
  QWEN_DEPTH           deep (the default: every direct run gets the depth switches
                       that fit it) or shallow (no implied switch -- only what you
                       type, the --shallow mode). Any other value: exit 2.
  QWEN_API_KEY         Sent as the auth token, and on the preflight request.
  QWEN_CUSTOM_HEADERS  Passed to claude as ANTHROPIC_CUSTOM_HEADERS (gateways).
  QWEN_EFFORT_ALLOWED  Effort levels the server accepts; others are refused up
                       front. QWEN_EFFORT=default omits --effort entirely.
                       With --effort (or QWEN_EFFORT) the level is also set in
                       the child environment as CLAUDE_CODE_EFFORT_LEVEL, so
                       Claude Code's own internal model calls use it too.
  QWEN_WEB=1           Same as --web: adds WebFetch. Off by default.
  QWEN_SUBAGENTS=1     Same as --subagents: adds Task. Depth turns it on with
                       the delegation push (--subagents-push) on every session;
                       set it for a --shallow run.
  QWEN_PREFLIGHT=0     Skip the /v1/models check (QWEN_MODEL is then required).
  QWEN_AUTO_MODEL=1    Same as --auto-model.
  QWEN_SETTING_SOURCES Passed to claude --setting-sources (e.g. project,local) so
                       personal ~/.claude settings cannot change results. Not
                       with --test (--restricted already ignores settings files).
  QWEN_PYTHON          Interpreter for result parsing: Python 3.8+, probed by
                       running it.
  QWEN_CLAUDE_BIN      The claude executable. Default: claude.
  QWEN_TIMEOUT_BIN     A GNU timeout to use, or 'none' for the built-in watchdog.
  QWEN_ROLE_DIR        Extra roles as NAME.md or NAME.txt.
  QWEN_BROWSER_DIR     Where --browser run folders go; a relative value is
                       relative to the caller's directory (like QWEN_OUTDIR).
                       Default: \$XDG_CACHE_HOME/qwen-agent/browser (else
                       ~/.cache/qwen-agent/browser).
  QWEN_PLAYWRIGHT_MCP  Replaces the browser MCP server command --browser writes
                       into its config (default 'npx -y --prefer-offline
                       @playwright/mcp@0.0.83'; on Git Bash the built-in default
                       is 'cmd /c npx -y --prefer-offline @playwright/mcp@0.0.83'
                       and this variable overrides that too). Split on whitespace
                       with no pathname expansion: the first word is the command,
                       the rest its leading args; a '*' stays literal.
  QWEN_PLAYWRIGHT_NODE_PATH
                       A node_modules DIRECTORY holding the playwright package,
                       for --replay (and --record, which replays once to validate
                       what it records; node must be on PATH). Unset, the package
                       is looked for in the npx cache under "\$(npm config get
                       cache)/_npx/*/node_modules"; found nowhere, the replay
                       stops with the install hint instead of skipping scripts.
  QWEN_REPLAY_BASE     The URL a recorded script opens, overriding the base in its
                       manifest — point the same scripts at another deployment.
                       Set by --replay --base; a recorded script's own default is
                       the base URL of the suite that was recorded.
  QWEN_OUTDIR          Where -w puts generated output files. Default: cwd.
                       A --probe patch with no -o goes here only when you set
                       it; unset, it goes to the probe directory instead.
  QWEN_CONFIG          The config file.

  The child never inherits the parent session's control channel, provider
  routing (Bedrock, Vertex, Foundry), ANTHROPIC_API_KEY, custom headers, or model
  overrides: every model alias is pointed at the served model.

EXIT CODES
  $QA_OK  success
  $QA_USAGE  usage error (bad flag, missing or doubled prompt)
  $QA_PREFLIGHT  preflight failed (server unreachable, or model not served)
  $QA_APIERR  API error from the endpoint (status is reported on stderr)
  $QA_TIMEOUT  timed out after --timeout seconds
  $QA_EMPTY  ran clean but returned no usable text
  $QA_DENIED  a tool call was blocked by the permission system (see --warn-denials)
  $QA_HARNESS  harness failure (claude missing, or unparseable output)
  $QA_SCENARIO_FAIL  --scenarios: at least one scripted scenario ended FAIL or BLOCKED;
                     --replay: at least one recorded script ended FAIL or ERROR, or the
                     folder kept no script at all (the same verdicts taken again exit
                     the same way)
  11  --until-done stopped at the round limit or a budget; also checks pass but the
      deviation audit was unusable twice -- review the diff manually (partial; report written)
  12  --until-done made no progress (same checks failed two rounds in a row)
  13  --until-done: working tree dirty at start (use --allow-dirty)
  14  --until-done: another run holds this repo's lock

EXAMPLES
  $QA_SELF "which files in this dir are shell scripts?"
  $QA_SELF -r auditor -o findings.md "audit ./scripts for unquoted vars"
  $QA_SELF -w -r auditor -f audit-task.md          # detached, unique out file
  $QA_SELF --json "count TODOs" | jq -r .usage.input_tokens
  $QA_SELF -r mechanic "add a trailing newline to every .sh that lacks one"
  $QA_SELF --write --toolset 'Read,Edit,Glob,Grep' "retitle every heading"
  $QA_SELF --interactive -C ./proj      # an interactive session on the local model
  $QA_SELF --scenarios cart.md --record ./cart-replay   # suite run, verdicts recorded
  $QA_SELF --replay ./cart-replay       # the same verdicts again, with no model at all

SAFETY
  A bare run never writes your files. Under default depth, inside a git repo,
  it gets a shell (Bash), Edit, Write and Task subagents in a throwaway sandbox
  copy of the tree (the implied --probe) -- a copy, not a jail: the shell runs
  as you, with your network. --shallow (or QWEN_DEPTH=shallow) or --read-only
  keeps the strict fence: --tools 'Read,Glob,Grep' --strict-mcp-config, a
  schema-level restriction under which the model has no Bash and no Write tool
  at all. Writing to your tree requires --write, --all-tools, or an explicit
  --toolset naming Edit/Write/Bash. --allowed-tools alone never restricts
  anything. --test adds Bash for qwen-test only; the tests it runs are arbitrary
  code. Web tools are opt-in: --web adds only WebFetch (never WebSearch, which
  local servers reject anyway), --browser a real browser. --record and --replay
  run model-written JavaScript with node, as you, unsandboxed.
EOF
}

die()  { printf '%s: %s\n' "$QA_SELF" "$*" >&2; }
note() { [ "$QUIET" -eq 1 ] || printf '%s: %s\n' "$QA_SELF" "$*" >&2; }
# A fresh private file for one helper's stderr, for the steps that run before the
# run's own temp directory exists. The caller exits on failure: `x="$(err_file)" || exit`.
err_file() {
  mktemp "${TMPDIR:-/tmp}/qwen-agent-err.XXXXXXXX" 2>/dev/null \
    || { die "cannot create a temporary file in ${TMPDIR:-/tmp}"; return 1; }
}
# Git Bash: argument conversion is switched off for the child (see CHILD_ENV), so
# a path that must reach a native program is converted explicitly. No-op elsewhere.
# An empty path stays empty (cygpath would complain on stderr). cygpath refuses a
# path that runs through a regular file (".../file/sub": Not a directory) and then
# prints nothing -- which once turned an unusable QWEN_PROBE_DIR into an empty one,
# i.e. the default probe directory. Such a path is converted parent first, so it
# reaches the native program still pointing where the caller said, and fails there.
native_path() {
  local p="$1" n parent
  if [ -z "$p" ] || ! command -v cygpath >/dev/null 2>&1; then printf '%s' "$p"; return 0; fi
  if n="$(cygpath -w "$p" 2>/dev/null)" && [ -n "$n" ]; then printf '%s' "$n"; return 0; fi
  while [ "${p%/}" != "$p" ] && [ "$p" != / ]; do p="${p%/}"; done
  case "$p" in */*) ;; *) printf '%s' "$p"; return 0 ;; esac
  parent="${p%/*}"; [ -n "$parent" ] || parent=/
  [ "$parent" != "$p" ] || { printf '%s' "$p"; return 0; }
  n="$(native_path "$parent")"
  case "$n" in *\\*) printf '%s\\%s' "${n%\\}" "${p##*/}" ;; *) printf '%s/%s' "${n%/}" "${p##*/}" ;; esac
}
# The drive-letter spelling with forward slashes (C:/x/y) under Git Bash: what a
# printed path or command must use when it sits beside paths git or Python print,
# so a pasted line names every file the same way. No-op elsewhere.
mixed_path() {
  local n
  if [ -n "$1" ] && command -v cygpath >/dev/null 2>&1 && n="$(cygpath -m "$1" 2>/dev/null)" \
     && [ -n "$n" ]; then printf '%s' "$n"; else printf '%s' "$1"; fi
}
# Single-quote a path for the shell: what a printed command must carry so it pastes
# correctly wherever it lands -- wrap in single quotes and write each embedded ' as
# '\''. Pattern and replacement are variables so nothing has to be parsed as quoting
# inside the ${...} (bash 3.2 included, and shellcheck parses it as written).
sq() { local q="'" esc="'\\''"; printf "'%s'" "${1//$q/$esc}"; }
# An absolute path as a permission-rule path ("//path"). Claude Code matches rules
# against POSIX-form paths and normalizes a Windows drive path C:\x\y to /c/x/y, so
# on Git Bash the (native, from Python) path is converted to exactly that form.
# cygpath -m, not -u: -u maps %TEMP% back to /tmp through the mount table, which
# is not the form Claude Code compares against.
rule_path() {
  local p="$1" d
  if command -v cygpath >/dev/null 2>&1 && [ "$p" != "<worktree>" ]; then
    p="$(cygpath -m "$p")"
    case "$p" in
      [A-Za-z]:/*) d="$(printf '%s' "${p%%:*}" | tr '[:upper:]' '[:lower:]')"; p="/$d${p#?:}" ;;
    esac
  fi
  printf '//%s' "${p#/}"
}

# --------------------------------------------------------------- interpreter
# The JSON parsing below needs a REAL python. Presence on PATH is not enough:
# on Windows, `python3` is normally a Microsoft Store launcher stub that prints
# an install advert and exits 49, which made the preflight report "not JSON" and
# hid the actual cause. So each candidate is EXECUTED before it is trusted.
resolve_py() {
  local c
  for c in "${QWEN_PYTHON:-}" python3 python python3.exe python.exe; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import json,shlex,sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || continue
    QA_PY="$c"
    return 0
  done
  return 1
}

# ------------------------------------------------------------------- roles
builtin_role() {
  case "$1" in
    plain) printf '' ;;
    auditor)
      cat <<'ROLE_EOF'
You are operating as a CODE AUDITOR. Rules:
- Ground every claim in a file you actually read. Cite it as path:line.
- Never speculate about code you did not open. If you could not verify
  something, say "UNVERIFIED" and say exactly what you would need.
- Report problems in severity order: correctness bugs first, then security,
  then robustness, then style. Skip anything you cannot demonstrate.
- For each finding give: location, what is wrong, and a concrete failing case
  (specific inputs or state that produce the wrong result).
- Do not rewrite the code unless asked. Do not praise. Do not pad.
- If you find nothing real, say so plainly. A short honest audit beats a long
  speculative one.
ROLE_EOF
      ;;
    coder)
      cat <<'ROLE_EOF'
You are a CODER working to a written task with a checklist. Rules:
- The harness, not you, decides when you are done: it runs every checklist
  check itself after you stop. Do not claim an item is done; make its check pass.
- Run tests with `qwen-test [SELECTOR]` (a test id/path or -k EXPR). It is the
  only shell command you have. Read its first line: TEST <id> PASSED|FAILED|...
- Change only what the task needs. Preserve surrounding style.
- When you deliberately depart from the spec, end your reply with one block per
  departure, exactly:
  ## DEVIATION
  SPEC: <the spec text or section you departed from>
  DID: <what you did instead>
  WHY: <the reason>
  EVIDENCE: <the TEST line or path:line that forced it>
- Finish with a terse list of the files you changed.
- Before you stop, compare your actual output with every explicit requirement in the spec
  (names, headers, exact messages, output shape). Read the files you wrote.
- Do not add leniency the spec did not ask for (trimming, normalising, accepting malformed
  input), and do not special-case the examples.
ROLE_EOF
      ;;
    mechanic)
      cat <<'ROLE_EOF'
You are performing a MECHANICAL task. Rules:
- Do exactly what was asked, across every place it applies, and nothing else.
- No refactoring, renaming, reformatting or "while I was in there" changes.
- Preserve surrounding style, indentation and comments exactly.
- Work from what the files actually contain; verify before you edit.
- If an instance is ambiguous, skip it and list it at the end under
  "SKIPPED — needs a decision" rather than guessing.
- Finish with a terse list of every location you changed.
ROLE_EOF
      ;;
    tester)
      cat <<'ROLE_EOF'
You are operating as a BROWSER TESTER. You drive a real browser through the
Playwright tools. Method: open the page and take an accessibility snapshot;
exercise every control and flow named in the task as a user would, including
empty, invalid and repeated input; chain actions into sequences (do something,
reset or undo it, then do it again) and check that the state after the sequence
is what a user would expect, not only what the screen shows right after each
click; after each action take a snapshot and compare what the page shows with
what a user would expect; take a screenshot whenever the evidence is visual
(layout, images, canvas, colours) and look at it: call browser_take_screenshot
WITHOUT a filename so the image comes back to you; a filename only saves the
file (open a saved one with Read if you need to see it again). Check the layout
at a narrow phone width too (browser_resize to 400x800, then back), and repeat
a key flow with any dark or alternate theme the app offers. Report each
defect with:
steps to reproduce, expected, actual, and evidence (the snapshot lines or the
screenshot file name). Then list what you tested that worked. Only report what
you observed in the browser; if a tool failed or an image was not visible to
you, say so. Test as a user would, from the outside: do not open, fetch or quote
the application's source code (scripts, styles, server files) unless the task asks
you to, and never base a verdict on reading code instead of on what the page does.
ROLE_EOF
      ;;
    *) return 1 ;;
  esac
}

# Deep variants of two built-ins, reached only through --role-variant (so -r and
# --list-roles are unchanged). coder-deep is the coder text plus an edge-case pass:
# the supervisor relies on the coder's DEVIATION format, so it is kept verbatim.
builtin_variant() {
  case "$1-$2" in
    auditor-deep)
      cat <<'ROLE_EOF'
You are operating as a CODE AUDITOR doing a DEEP audit. Work in this order:
1. Map what the code must guarantee: its inputs and their edge cases,
   concurrency and ordering, error paths, platform differences (Windows,
   macOS, Linux) and security boundaries. Write that list down first.
1b. Coverage: list every public entry point (function, method, CLI option,
   file format) of the code you were asked to audit. Every entry point needs
   at least one failure mode on your list, or a one-line reason why it cannot
   fail in a way a user would notice.
2. For EACH failure mode on the list, try to trigger it: read the code path
   that handles it and, when you have a shell, write and run a probe or a
   test that exercises it.
3. Record the evidence for every verdict: path:line for code you read, the
   exact command and its output for anything you ran.
4. Give a verdict per failure mode: FAIL (with the concrete failing case:
   the inputs or state and the wrong result), PASS (with the evidence that
   rules the failure out), or UNVERIFIED (with what you would need).
5. Default to FAIL when evidence is missing for a guarantee that matters.
6. Finish check: before you answer, go back over the list from steps 1 and 1b.
   Any failure mode you have not tried to trigger, try now. Your answer ends
   with a COVERAGE section: each entry point, and per failure mode
   FAIL / PASS / UNVERIFIED.
Rules:
- Ground every claim in a file you actually read or a command you actually
  ran. There is no length limit, but every claim carries its evidence.
- List unverified suspicions separately, under the heading UNVERIFIED
  SUSPICIONS, never mixed in with the findings you demonstrated.
- Report findings in severity order: correctness bugs first, then security,
  then robustness, then style.
- Do not rewrite the code unless asked. Do not praise.
ROLE_EOF
      ;;
    coder-deep)
      builtin_role coder
      cat <<'ROLE_EOF'

When the checks pass, do an edge-case pass before you stop:
- List the edge cases the task implies: empty, huge and odd inputs; paths
  with spaces; non-UTF-8 bytes; Windows and macOS differences; a run that is
  interrupted or resumed halfway.
- For each plausible one, write a probe or a test and run it.
- Fix what fails, inside what the task asks for.
- End your reply with a section headed EDGE CASES: one line per case,
  saying how you checked it (the test id or the command) and what you
  changed, or that you did not check it and why.

Finish check: before you answer, list the edge cases you identified; for each
one name the probe or test that exercised it and its result. An edge case
without a probe is either probed now or listed under NOT CHECKED with the
reason.
ROLE_EOF
      ;;
    *) return 1 ;;
  esac
}

# The one fixed prompt of --review-round. It asks for the WHOLE answer again, in the
# format the first answer had to follow, because the reviewed answer replaces it.
REVIEW_PROMPT='Review round: before your answer is final, try to break it. Go back over every claim you made and every change you made. For each one, look for the input, state or code path that would make it wrong, and check it: read the code again and, where you have a shell, run a probe or a test. Correct or drop anything that does not survive, and add anything you missed. List, under REVIEW, each earlier claim or change you re-checked, the check you ran, and what changed (kept, corrected, dropped). Look in particular for what you did not cover the first time: a part of the code you did not open, an input class you did not try. Then reply with your complete revised answer. It replaces your previous answer, so repeat everything that still stands, in exactly the format your previous answer had to follow.'
# With --subagents-push the review round also delegates: appended to REVIEW_PROMPT
# only when PUSH is on, so a nudge-only review round keeps the prompt above verbatim.
REVIEW_PUSH_TEXT=' Hand the re-verification of your three most important claims to a subagent, and compare its result with yours.'

# The one fixed prompt of --record, sent to the session that just ran the suite. The
# script contract it states -- one RESULT line on stdout, exit 0 after it, exit 1 on a
# script error, the base URL from QWEN_REPLAY_BASE -- is what lib/scenarios.py replays
# and parses, so the two must not drift: that file documents the same contract. The
# @FOLDER@ placeholder is replaced with the run's browser folder (the one directory the
# recording may write in) because the session knows its own evidence only by that path.
RECORD_PROMPT="Now turn each scenario you ran into a deterministic replay script. For each scenario id write the file @FOLDER@/replay/<id>.mjs: a self-contained Node ES module that imports { chromium } from 'playwright', launches it headless, opens the page fresh, performs the scenario's steps exactly with stable selectors (roles, labels, visible text; never coordinates), checks EVERY expectation with an explicit assertion, and prints exactly one line: 'RESULT <id> PASS' or 'RESULT <id> FAIL: <the expectation that did not hold>'. Read the base URL from the environment variable QWEN_REPLAY_BASE (default: the suite's base URL). Handle dialogs explicitly. Exit 0 after printing the line; exit 1 only on a script error. Never catch your own errors to print FAIL: a step that cannot be performed must let the exception propagate; FAIL is only for an expectation that was checked and did not hold. Do not change anything else. Reply with the list of files written."

list_roles() {
  echo "built-in: auditor, coder, mechanic, plain, tester"
  if [ -d "$ROLE_DIR" ]; then
    echo "from $ROLE_DIR:"
    # Portable on purpose: no find -printf, no GNU sed alternation (BSD/macOS).
    local f b
    for f in "$ROLE_DIR"/*.md "$ROLE_DIR"/*.txt; do
      [ -f "$f" ] || continue
      b="${f##*/}"
      printf '  %s\n' "${b%.*}"
    done | sort
  else
    echo "(no role dir at $ROLE_DIR)"
  fi
}

resolve_role() {
  local name="$1" f
  if builtin_role "$name"; then return 0; fi
  for f in "$ROLE_DIR/$name.md" "$ROLE_DIR/$name.txt" "$name"; do
    if [ -f "$f" ] && [ -r "$f" ]; then cat "$f"; return 0; fi
  done
  return 1
}

# A role may pin a hard toolset. Explicit flags always win.
apply_role_defaults() {
  case "$1" in
    auditor)
      : # nothing to pin: the default is already read-only + strict MCP
      ;;
    mechanic|coder)
      # A mechanic has to be able to edit; still no Bash, still no MCP.
      if [ "$TOOLSET_EXPLICIT" -eq 0 ] && [ "$ALL_TOOLS" -eq 0 ]; then
        WRITE_MODE=1
      fi
      ;;
    tester)
      # A tester drives a real browser: it implies --browser. The MCP config, the
      # run folder and the grants are wired to BROWSER below, where this takes hold.
      BROWSER=1
      ;;
  esac
}

# ------------------------------------------------------------- arg parsing
need_arg() { [ "$2" -gt 0 ] || { die "option $1 requires a value (see --help)"; exit $QA_USAGE; }; }

ORIG_ARGS=("$@")
UNTIL_DONE=""
SUP_ARGS=()

while [ $# -gt 0 ]; do
  arg="$1"
  val=""
  # A valueless switch given an '=value' would split into the switch plus an orphan
  # value that reads like the prompt: refuse it here, where the message can name the
  # flag. The --interactive refusals below list the =-forms of their switches too;
  # the until-done refusals of the switches named here need no such repeat.
  case "$arg" in
    --probe=*|--probe-here=*|--keep-sandbox=*|--review-round=*|--deep=*|--subagents-nudge=*|\
    --subagents-push=*|--shallow=*|--browser=*|--headed=*|--browser-eval=*)
      die "option ${arg%%=*} takes no value (got '$arg')"; exit $QA_USAGE ;;
  esac
  # support --opt=value
  case "$arg" in
    --*=*) val="${arg#*=}"; arg="${arg%%=*}"
           [ -n "$val" ] || { die "option $arg was given an empty value"; exit $QA_USAGE; }
           set -- "$arg" "$val" "${@:2}" ;;
  esac
  case "$1" in
    -h|--help)            usage; exit 0 ;;
    -V|--version)         echo "$QA_SELF $QA_VERSION"; exit 0 ;;
    --list-roles)         list_roles; exit 0 ;;
    -o|--out)             need_arg "$1" $(($#-1)); OUT="$2"; shift 2 ;;
    -t|--tools)           need_arg "$1" $(($#-1)); TOOLS="$2"; TOOLS_EXPLICIT=1; shift 2 ;;
    --toolset)            need_arg "$1" $(($#-1)); TOOLSET="$2"; TOOLSET_EXPLICIT=1
                          # 'none' MEANS THE EMPTY TOOLSET; inside a list it would
                          # be silently dropped (or read as a tool literally named
                          # 'none'), so it only means something on its own.
                          case ",$TOOLSET," in
                            *,none,*)
                              [ "$TOOLSET" = none ] || { die "--toolset: 'none' must stand alone (got '$TOOLSET')"; exit $QA_USAGE; } ;;
                          esac
                          [ "$TOOLSET" = none ] && { TOOLSET=""; TOOLSET_NONE=1; }; shift 2 ;;
    --read-only)          TOOLSET="$TOOLSET_READONLY"; TOOLSET_EXPLICIT=1; READ_ONLY_FLAG=1
                          STRICT_MCP=1; STRICT_MCP_EXPLICIT=1; shift ;;
    --write)              WRITE_MODE=1; shift ;;
    --web)                WEB_MODE=1; shift ;;
    --subagents)          SUBAGENTS=1; shift ;;
    --subagents-nudge)    SUBAGENTS=1; NUDGE=1; shift ;;
    --subagents-push)     SUBAGENTS=1; PUSH=1; shift ;;
    --role-variant)       need_arg "$1" $(($#-1)); ROLE_VARIANT="$2"; shift 2 ;;
    --review-round)       REVIEW_ROUND=1; shift ;;
    --probe)              PROBE=1; PROBE_EXPLICIT=1; shift ;;
    --probe-here)         PROBE_HERE=1; shift ;;
    --keep-sandbox)       KEEP_SANDBOX=1; shift ;;
    --deep)               DEEP=1; PROBE=1; REVIEW_ROUND=1; ROLE_VARIANT="deep"; SUBAGENTS=1; PUSH=1
                          shift ;;
    --shallow)            SHALLOW=1; shift ;;
    --interactive)        INTERACTIVE=1; shift ;;
    --test)               TEST_MODE=1; shift ;;
    --test-repo)          need_arg "$1" $(($#-1)); TEST_REPO="$2"; shift 2 ;;
    --all-tools|--unrestricted) ALL_TOOLS=1; shift ;;
    --strict-mcp)         STRICT_MCP=1; STRICT_MCP_EXPLICIT=1; shift ;;
    --mcp-config)         need_arg "$1" $(($#-1))
                          # An empty value survives every later [ -n ] test as
                          # "not given": the run would look like an MCP run and
                          # load nothing. Refuse it where the message can name
                          # the flag.
                          [ -n "$2" ] || { die "--mcp-config needs a file path, not an empty string"; exit $QA_USAGE; }
                          MCP_CONFIG="$2"; shift 2 ;;
    --browser)            BROWSER=1; shift ;;
    --headed)             HEADED=1; BROWSER=1; shift ;;
    --browser-eval)       BROWSER_EVAL=1; BROWSER=1; shift ;;
    --scenarios)          need_arg "$1" $(($#-1))
                          # The same empty-value trap as --mcp-config: an empty
                          # path would survive every later [ -n "$SCENARIOS" ] as
                          # "no suite given" and the run would go out promptless.
                          [ -n "$2" ] || { die "--scenarios needs a file path, not an empty string"; exit $QA_USAGE; }
                          SCENARIOS="$2"; shift 2 ;;
    --record)             need_arg "$1" $(($#-1))
                          # The empty-value trap of --mcp-config again: an empty
                          # directory would survive every later [ -n "$RECORD" ] as
                          # "no --record given" and silently skip the recording.
                          [ -n "$2" ] || { die "--record needs a directory, not an empty string"; exit $QA_USAGE; }
                          RECORD="$2"; shift 2 ;;
    --replay)             need_arg "$1" $(($#-1))
                          [ -n "$2" ] || { die "--replay needs a directory, not an empty string"; exit $QA_USAGE; }
                          REPLAY="$2"; shift 2 ;;
    -e|--effort)          need_arg "$1" $(($#-1)); EFFORT="$2"; shift 2 ;;
    -m|--model)           need_arg "$1" $(($#-1)); MODEL="$2"; shift 2 ;;
    -b|--base)            need_arg "$1" $(($#-1)); BASE="$2"; shift 2 ;;
    --ctx)                need_arg "$1" $(($#-1)); CTX="$2"; shift 2 ;;
    --autocompact)        need_arg "$1" $(($#-1)); AUTOCOMPACT="$2"; shift 2 ;;
    --no-autocompact)     AUTOCOMPACT=""; shift ;;
    -r|--role)            need_arg "$1" $(($#-1)); ROLE="$2"; shift 2 ;;
    --role-file)          need_arg "$1" $(($#-1)); ROLE_FILE="$2"; shift 2 ;;
    -s|--system)          need_arg "$1" $(($#-1)); EXTRA_SYS="$2"; shift 2 ;;
    -f|--prompt-file)     need_arg "$1" $(($#-1)); PROMPT_FILE="$2"; shift 2 ;;
    --stdin)              READ_STDIN=1; shift ;;
    -C|--cd)              need_arg "$1" $(($#-1)); WORKDIR="$2"; shift 2 ;;
    -D|--add-dir)         need_arg "$1" $(($#-1)); ADD_DIRS+=("$2"); shift 2 ;;
    --permission-mode)    need_arg "$1" $(($#-1)); PERM_MODE="$2"; PERM_MODE_EXPLICIT=1; shift 2 ;;
    --timeout)            need_arg "$1" $(($#-1)); TIMEOUT="$2"; TIMEOUT_EXPLICIT=1; shift 2 ;;
    --no-timeout)         NO_TIMEOUT=1; shift ;;
    --force)              FORCE=1; shift ;;
    -w|--detach|--bg)     BG=1; shift ;;
    --json)               JSON_OUT=1; shift ;;
    --resume)             need_arg "$1" $(($#-1))
                          # A value starting with '-' is a forgotten quote or a missing id:
                          # claude would consume the next real flag as the session id (or be
                          # fed one as its prompt). An empty id resumes nothing and silences
                          # the run. Refuse both here, where the message can name --resume.
                          case "$2" in
                            ''|-*) die "--resume needs a session id, not '$2' (empty or option-like)";
                                   exit $QA_USAGE ;;
                          esac
                          RESUME_ID="$2"; shift 2 ;;
    --until-done)         need_arg "$1" $(($#-1)); UNTIL_DONE="$2"; shift 2 ;;
    --max-rounds|--budget-tokens|--budget-seconds)
                          need_arg "$1" $(($#-1)); SUP_ARGS+=("$1" "$2"); shift 2 ;;
    --allow-dirty|--no-deviation-audit)
                          SUP_ARGS+=("$1"); shift ;;
    --warn-denials)       WARN_DENIALS=1; shift ;;
    --auto-model)         AUTO_MODEL=1; shift ;;
    --no-preflight)       NO_PREFLIGHT=1; shift ;;
    --preflight-only)     PREFLIGHT_ONLY=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    -q|--quiet)           QUIET=1; shift ;;
    --)                   shift; PROMPT="$*"; PROMPT_SET=1; break ;;
    -*)                   die "unknown option: $1 (see --help)"; exit $QA_USAGE ;;
    *)                    PROMPT="$*"; PROMPT_SET=1; break ;;
  esac
done

# ---------------------------------------------------------------------- depth
# Depth is the DEFAULT: unless --shallow (or QWEN_DEPTH=shallow) is given, every run
# gets the depth switches that FIT it, and the ones that do not fit are silently
# dropped. Implied switches never cause a refusal; typed switches keep theirs.
#   V --role-variant deep : only roles that HAVE a deep variant (auditor, coder)
#   R --review-round and N --subagents-push : every session -- not --interactive,
#     not --resume, not --preflight-only, and not an explicit --toolset none (the
#     push replaces the softer --subagents-nudge text; sweep batches still nudge). A
#     --dry-run gets N (it shapes the argv the dry run must print truthfully) and
#     notes R; it starts no session and makes no second call.
#   P --probe : only for a read-only session that runs no --test (a --test run
#     keeps its worktree and its ## REPRO FILES; the sandbox would delete the
#     reproduction test with itself), and it is judged further down, after roles
#     are resolved (it must see what --write and the role decided).
# --until-done decomposes depth once, in its own block below, and the rounds it
# spawns run shallow -- implied depth there would compound, round by round.
DEPTH_ENV="${QWEN_DEPTH:-deep}"
case "$DEPTH_ENV" in
  deep|shallow) ;;
  *) [ -n "$REPLAY" ] && DEPTH_ENV=shallow        # a replay runs no model: depth is moot
     [ -n "$REPLAY" ] || { die "QWEN_DEPTH is '$DEPTH_ENV' (expected 'deep' or 'shallow')"; exit $QA_USAGE; } ;;
esac
if [ "$SHALLOW" -eq 1 ] && [ "$DEEP" -eq 1 ]; then
  die "--shallow and --deep together: depth is on by default (drop --shallow) or you asked"
  die "for all of it by name (drop --deep) -- pick one"
  exit $QA_USAGE
fi
if   [ "$SHALLOW" -eq 1 ];       then DEPTH_MODE="shallow"   # the flag wins over the env
elif [ "$DEEP" -eq 1 ];          then DEPTH_MODE="deep"
elif [ "$DEPTH_ENV" = shallow ]; then DEPTH_MODE="shallow"
else                                  DEPTH_MODE="default"
fi
# A deep session reads, probes, delegates and reviews: give it an hour, not the
# 30 minutes a single read-only answer needed. A typed --timeout or QWEN_TIMEOUT
# (env or config) always wins; --no-timeout needs no default at all.
if [ "$DEPTH_MODE" != shallow ] && [ "$TIMEOUT_EXPLICIT" -eq 0 ] && [ "$NO_TIMEOUT" -eq 0 ]; then
  TIMEOUT=3600
fi
if [ "$DEPTH_MODE" = default ] && [ -z "$UNTIL_DONE" ]; then
  # Not for a resume either: --resume continues the session that ran with the
  # plain role text, and swapping the deep variant in mid-session is wrong.
  if [ -z "$ROLE_VARIANT" ] && [ -z "$ROLE_FILE" ] && [ "$INTERACTIVE" -eq 0 ] \
     && [ -z "$RESUME_ID" ]; then
    case "$ROLE" in auditor|coder) ROLE_VARIANT=deep ;; esac
  fi
  if [ "$INTERACTIVE" -eq 0 ] && [ -z "$RESUME_ID" ] && [ "$PREFLIGHT_ONLY" -eq 0 ] \
     && [ "$TOOLSET_NONE" -eq 0 ]; then
    REVIEW_ROUND=1
    # A typed --subagents-nudge is the caller's choice of delegation text and stays;
    # otherwise depth brings the push.
    SUBAGENTS=1; [ "$NUDGE" -eq 1 ] || PUSH=1
  fi
fi

# ------------------------------------------------------- --interactive
# An interactive session is Claude Code as the person at the keyboard knows it:
# they answer its permission prompts themselves, and the session ends when they
# leave. Every flag that exists to fence a HEADLESS run (a tool policy, a fixed
# permission mode, a role prompt, one result file, a background job) either
# cannot apply here or would be silently dropped, so the combination is refused
# rather than quietly ignored. --dry-run is allowed: printing the command is not
# running it. Refused before the until-done block below, which would otherwise
# consume --until-done and its task file.
if [ "$INTERACTIVE" -eq 1 ]; then
  _ia_refuse() {
    die "--interactive cannot be combined with $1: an interactive session is driven by the person at the"
    die "keyboard, who answers Claude Code's own permission prompts (drop $1, or drop --interactive)"
    exit $QA_USAGE
  }
  [ "$PROMPT_SET" -eq 0 ] || _ia_refuse "a prompt"
  for _a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
    case "$_a" in
      -f|--prompt-file|--prompt-file=*)  _ia_refuse "--prompt-file" ;;
      --stdin)                           _ia_refuse "--stdin" ;;
      --until-done|--until-done=*)       _ia_refuse "--until-done" ;;
      --test|--test=*)                   _ia_refuse "--test" ;;
      --write)                           _ia_refuse "--write" ;;
      --all-tools|--all-tools=*|--unrestricted) _ia_refuse "$_a" ;;
      --toolset|--toolset=*)             _ia_refuse "$_a" ;;
      -t|--tools|--tools=*)              _ia_refuse "$_a" ;;
      --web|--web=*)                     _ia_refuse "--web" ;;
      --subagents|--subagents=*)         _ia_refuse "--subagents" ;;
      --subagents-nudge|--subagents-nudge=*) _ia_refuse "--subagents-nudge" ;;
      --subagents-push|--subagents-push=*)   _ia_refuse "--subagents-push" ;;
      --role-variant|--role-variant=*)   _ia_refuse "--role-variant" ;;
      --review-round)                    _ia_refuse "--review-round" ;;
      --probe|--probe=*|--probe-here|--probe-here=*|--keep-sandbox|--keep-sandbox=*|\
      --deep) _ia_refuse "$_a" ;;
      --json)                            _ia_refuse "--json" ;;
      -o|--out|--out=*)                  _ia_refuse "$_a" ;;
      -w|--detach|--bg)                  _ia_refuse "$_a" ;;
      --resume|--resume=*)               _ia_refuse "--resume" ;;
      -r|--role|--role=*)                _ia_refuse "$_a" ;;
      # A role or extra system text has nowhere to go: an interactive run passes
      # no --append-system-prompt, so it would be read and then dropped.
      --role-file|--role-file=*)         _ia_refuse "--role-file" ;;
      -s|--system|--system=*)            _ia_refuse "$_a" ;;
      # Same reason: both only shape the tool fence an interactive run does not
      # pass at all, so accepting them would advertise a restriction that is not there.
      --read-only)                       _ia_refuse "--read-only" ;;
      # Each only steers a headless or looped run; an interactive session would drop it.
      --permission-mode|--permission-mode=*) _ia_refuse "--permission-mode" ;;
      --test-repo|--test-repo=*)         _ia_refuse "--test-repo" ;;
      --max-rounds|--max-rounds=*|--budget-tokens|--budget-tokens=*|--budget-seconds|--budget-seconds=*|--allow-dirty|--no-deviation-audit)
                                         _ia_refuse "${_a%%=*}" ;;
      --warn-denials)                    _ia_refuse "--warn-denials" ;;
      --strict-mcp|--strict-mcp=*)       _ia_refuse "--strict-mcp" ;;
      # Same reason: it only steers the headless fence (a strict MCP load an
      # interactive session never gets), so it would be silently dropped.
      --mcp-config|--mcp-config=*)       _ia_refuse "--mcp-config" ;;
      # --browser writes its own MCP config and grants its tools as a headless run
      # does; an interactive session passes neither, so the browser would not come.
      --browser|--browser=*)             _ia_refuse "--browser" ;;
      --headed|--headed=*)               _ia_refuse "--headed" ;;
      --browser-eval|--browser-eval=*)   _ia_refuse "--browser-eval" ;;
    esac
  done
  unset _a
fi

# --------------------------------------------------------- --browser refusals
# Before the until-done block below (which execs the supervisor and never returns)
# and before anything is created: the loop's rounds are headless coder runs that get
# no browser, and --browser writes its own single-server MCP config -- a caller's
# --mcp-config would leave two claims on which servers load. --interactive is refused
# by the loop above. The --mcp-config check repeats below, after role resolution,
# where a 'tester' role turns --browser on beside a caller's --mcp-config too.
if [ "$BROWSER" -eq 1 ]; then
  [ -z "$MCP_CONFIG" ] || {
    die "--browser brings its own MCP config (one server: playwright): --mcp-config cannot be combined with --browser"
    exit $QA_USAGE; }
  [ -z "$UNTIL_DONE" ] || {
    die "--browser cannot be combined with --until-done: the loop's rounds are headless coder runs without a browser"
    exit $QA_USAGE; }
fi

# -------------------------------------------------------- --scenarios refusals
# The suite file supplies the prompt, the tester role and the browser, so every
# other source of a prompt or role is refused -- before the until-done block
# below (which would consume --until-done, exec the supervisor and never return)
# and before anything is created. --interactive: an interactive session has no
# headless answer to score, so the suite would run and go unscored; the refusal
# loop above does not list --scenarios because this block owns it.
if [ -n "$SCENARIOS" ]; then
  _sc_refuse() {
    die "--scenarios cannot be combined with $1: the scenario file supplies the prompt,"
    die "the tester role and the browser (drop $1, or drop --scenarios)"
    exit $QA_USAGE
  }
  [ "$INTERACTIVE" -eq 0 ] || _sc_refuse "--interactive"
  [ "$PROMPT_SET" -eq 0 ]  || _sc_refuse "a prompt argument"
  [ -z "$PROMPT_FILE" ]    || _sc_refuse "-f/--prompt-file"
  [ "$READ_STDIN" -eq 0 ]  || _sc_refuse "--stdin"
  [ -z "$UNTIL_DONE" ]     || _sc_refuse "--until-done"
  [ -z "$ROLE" ] || [ "$ROLE" = tester ] || _sc_refuse "-r $ROLE"
  BROWSER=1
  [ -n "$ROLE" ] || ROLE=tester
fi

# ---------------------------------------------------------- --record refusal
# --record is how a suite's verdicts become replayable: one deterministic script per
# scenario, replayed later by --replay. What a script has to reproduce is a scenario's
# verdict, and only a suite run produces those -- on its own --record records nothing.
if [ -n "$RECORD" ] && [ -z "$SCENARIOS" ]; then
  die "--record needs --scenarios FILE: the replay scripts record what the suite's"
  die "scenarios did, so there is nothing to record without a suite (drop --record)"
  exit $QA_USAGE
fi

# ------------------------------------------------------------- --replay
# A recorded suite replays with NO model at all: no preflight (nothing to ask a
# server), no role, no browser, no claude -- the scripts carry the verdicts a model
# recorded once, and node runs them in seconds. That is why this block stands above
# every model-shaped block below and exits here: any flag it was given that it does
# not act on (-r, -f, a prompt, --scenarios, --until-done, --interactive, --deep, a
# model or a timeout) would be silently dropped, and a run that drops the role or the
# suite the caller typed answers a question nobody asked. So only its own directory,
# the app's base URL and -q are accepted; everything else is refused, where the
# message can name the flag.
if [ -n "$REPLAY" ]; then
  _rp_refuse() {
    die "--replay cannot be combined with $1: --replay runs the recorded scripts with no"
    die "model call at all (drop $1, or drop --replay)"
    exit $QA_USAGE
  }
  [ "$PROMPT_SET" -eq 0 ] || _rp_refuse "a prompt argument"
  _rp_base=""
  _rp_val=""
  for _a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
    if [ -n "$_rp_val" ]; then                 # the value of a switch that takes one
      [ "$_rp_val" = base ] && _rp_base="$_a"
      _rp_val=""
      continue
    fi
    case "$_a" in
      --replay)                  _rp_val=dir ;;
      --replay=*)                : ;;
      -b|--base)                 _rp_val=base ;;
      --base=*)                  _rp_base="${_a#*=}" ;;
      -q|--quiet|-h|--help|-V|--version) : ;;
      --)                        break ;;       # the rest is a prompt, refused above
      *)                         _rp_refuse "$_a" ;;
    esac
  done
  unset _a _rp_val
  if ! resolve_py; then
    die "no working Python 3.8+ found (tried \$QWEN_PYTHON, python3, python): --replay"
    die "runs the scripts through lib/scenarios.py. Set QWEN_PYTHON, or put one in $QA_CONFIG."
    exit $QA_HARNESS
  fi
  # DIR names the CALLER's directory (like -o and --mcp-config), never -C's -- and
  # there is no -C here anyway: a replay touches no tree but its own temp directory.
  case "$REPLAY" in /*|[A-Za-z]:*) ;; *) REPLAY="$PWD/$REPLAY" ;; esac
  # The calls inline what skill_py (defined further down) does: this block runs before
  # that function exists, exactly as --scenarios validation does.
  _rp_py() { PYTHONPATH="$(native_path "$SKILL_DIR")" "$QA_PY" \
               "$(native_path "$SKILL_DIR/lib/scenarios.py")" "$@"; }
  _rp_err="$(err_file)" || exit $QA_HARNESS
  _rp_argv=(replay "$(native_path "$REPLAY")")
  [ -n "$_rp_base" ] && _rp_argv+=(--base "$_rp_base")
  note "replay: $(native_path "$REPLAY")"
  _rp_sum="$(_rp_py ${_rp_argv[@]+"${_rp_argv[@]}"} 2>"$_rp_err")"; _rp_rc=$?
  _rp_e="$(cat "$_rp_err" 2>/dev/null)"; rm -f "$_rp_err"
  case "$_rp_rc" in
    0)   : ;;
    9)   : ;;
    2)   die "--replay: ${_rp_e#scenarios.py: }"
         exit $QA_USAGE ;;
    *)   [ -z "$_rp_e" ] || die "--replay: ${_rp_e#scenarios.py: }"
         die "--replay: could not run the recorded scripts (exit $_rp_rc)"
         exit $QA_HARNESS ;;
  esac
  unset _rp_err _rp_argv
  # The table and its counts, on stderr like the scored suite's: the answer of a
  # replay is the verdict, and a replay has no answer but that. die, not note: -q
  # must not hide which scenarios did not hold.
  [ -z "$_rp_sum" ] || die "$_rp_sum"
  [ -z "$_rp_e" ] || die "--replay: ${_rp_e#scenarios.py: }"   # e.g. no script was kept
  exit "$_rp_rc"
fi

# ------------------------------------------------- probe git hygiene / --deep
# A caller-exported GIT_* points EVERY git call made from here on -- probe.py's
# gate, probe.make's clone, every until-done round and the session's Bash alike --
# at a repository other than the tree the arguments name: GIT_INDEX_FILE=<the
# user's .git/index> (as a pre-commit hook leaves it) had the sandbox's writes
# rewriting the user's index, and GIT_WORK_TREE=<a kept sandbox> answers that
# sandbox as the top level of `probe.py check` on the user's plain repo, so the
# fence (a whole shell) lands on the user's tree. Unset for this process and every
# child of it BEFORE the until-done exec below: that exec hands over to the
# supervisor, and the --probe setup further down is never reached on the loop path. This is PROBE hygiene, not depth hygiene: it
# fires only where a sandbox of this run is actually used -- a typed --probe,
# --probe-here or --deep here, an implied --probe at the place it applies below --
# and never for a plain (say --write) run, which has no sandbox and no reason to
# lose the caller's own git setup (a bare-repo dotfiles tree, a hook's GIT_*).
# The sandbox is where -C says it is; nothing inherited may move the gate or the
# session outside it.
if [ "$PROBE" -eq 1 ] || [ "$PROBE_HERE" -eq 1 ] || [ "$DEEP" -eq 1 ]; then
  unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
        GIT_ALTERNATE_OBJECT_DIRECTORIES
fi
# --deep sets --role-variant deep; an explicit different variant would win or lose
# by flag ORDER alone (in the loop the forwarded --role-variant deep came last), so
# refuse the combination where the message can name both. The caller's argv is
# scanned, not the parsed value, so either order is caught; after '--' the rest is
# the prompt, not flags.
if [ "$DEEP" -eq 1 ]; then
  _rv_next=0
  for _a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
    if [ "$_rv_next" -eq 1 ]; then
      _rv_next=0
      [ "$_a" = deep ] || {
        die "--deep sets --role-variant deep (got --role-variant $_a)"; exit $QA_USAGE; }
    elif [ "$_a" = --role-variant ]; then
      _rv_next=1
    else
      case "$_a" in
        --) break ;;
        --role-variant=*)
          [ "${_a#*=}" = deep ] || {
            die "--deep sets --role-variant deep (got $_a)"; exit $QA_USAGE; } ;;
      esac
    fi
  done
  unset _rv_next _a
fi
# --deep --probe-here resumes a --deep session IN its kept sandbox: --probe-here
# wins over --deep's implied --probe (the deep fence around the sandbox that
# already exists) instead of colliding with it as "--probe and --probe-here are
# exclusive", which left spelling out --review-round --subagents-nudge etc. by hand.
# Only the IMPLIED --probe steps aside: --deep --probe --probe-here types both
# directions at once, and stays the exclusivity refusal it always was.
if [ "$DEEP" -eq 1 ] && [ "$PROBE_HERE" -eq 1 ] && [ "$PROBE_EXPLICIT" -eq 0 ]; then
  PROBE=0
fi

# --------------------------------------------------------- until-done
if [ -n "$UNTIL_DONE" ]; then
  _ud_refuse() { die "--until-done takes its prompt from TASK and owns -f/--resume/-o/--json per round; drop $1"; exit $QA_USAGE; }
  [ "$PROMPT_SET" -eq 0 ] || _ud_refuse "the prompt"
  FWD=()
  _skip=0
  for a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
    if [ "$_skip" -eq 2 ]; then
      [ "$a" = coder ] || { die "--until-done always runs the coder role (got -r $a)"; exit $QA_USAGE; }
      _skip=0; continue
    fi
    if [ "$_skip" -eq 1 ]; then _skip=0; continue; fi
    case "$a" in
      # --role-file would replace the coder role every round runs with: a custom
      # file has no coder instructions, so the rounds silently turn read-only.
      -f|--prompt-file|--prompt-file=*|--stdin|--resume|--resume=*|-w|--detach|--bg|-o|--out|--out=*|--dry-run|--json|\
      --role-file|--role-file=*)
        _ud_refuse "$a" ;;
      # Every round runs --test, and --test builds the tool policy itself. These
      # collide with that fence, so refuse them here rather than at every
      # round's qwen-agent call. -t/--tools would replace the qwen-test-only
      # grants and --unrestricted removes the toolset outright; -D/--add-dir
      # widens the coder's file access (-D / reaches the whole disk).
      --toolset|--toolset=*|--read-only|--all-tools|--permission-mode|--permission-mode=*|\
      -t|--tools|--tools=*|--unrestricted|-D|--add-dir|--add-dir=*)
        _ud_refuse "$a" ;;
      # Every round is a headless run of the coder role; the MCP servers are the
      # harness's, not the caller's to retarget per loop.
      --mcp-config|--mcp-config=*)
        _ud_refuse "--mcp-config" ;;
      # One review round after the checks pass is the supervisor's, not each round's.
      --review-round) ;;
      # The supervisor owns the probe sandbox: one for the whole loop. Each round runs
      # in it with --probe-here, which is therefore not the caller's to pass.
      # --shallow says only WHAT depth the rounds get; the loop's own decomposition
      # already read it off DEPTH_MODE, so there is nothing to forward.
      --probe|--keep-sandbox|--deep|--shallow) ;;
      --probe-here)
        die "--probe-here belongs to the supervisor's own rounds; use --probe"; exit $QA_USAGE ;;
      -r|--role) _skip=2 ;;
      --role=coder|--test) ;;
      --role=*) die "--until-done always runs the coder role (got $a)"; exit $QA_USAGE ;;
      --until-done|-C|--cd|--max-rounds|--budget-tokens|--budget-seconds) _skip=1 ;;
      --until-done=*|--cd=*|--max-rounds=*|--budget-tokens=*|--budget-seconds=*) ;;
      --allow-dirty|--no-deviation-audit) ;;
      *) FWD+=("$a") ;;
    esac
  done
  unset _skip
  # --probe copies -C's tree into the one sandbox every round works in; --test-repo
  # would send each round's --probe-here to a repo that is not the sandbox, and
  # --probe-here refuses --test-repo, so every round died at the agent with
  # "agent usage error after 1 round(s)". Refuse it here, up front.
  if [ -n "$TEST_REPO" ] && { [ "$PROBE" -eq 1 ] || [ "$DEEP" -eq 1 ]; }; then
    if [ "$PROBE_EXPLICIT" -eq 1 ]; then
      die "--probe copies -C's tree; --test-repo is not supported with --until-done --probe"
    else
      die "--deep includes --probe, which copies -C's tree; --test-repo is not supported with --until-done --deep"
    fi
    exit $QA_USAGE
  fi
  # Default depth decomposes once, here: the coder rounds get V + R + N -- what
  # --deep forwards, minus --probe, which is the loop's sandbox and only ever
  # typed. The rounds themselves run with QWEN_DEPTH=shallow (the supervisor sets
  # it), so nothing re-implies depth round by round. A typed token outranks the
  # implied one: it is already in FWD, so the implied copy is skipped. (The rounds
  # run shallow by typed flag AND environment -- see supervisor call_agent.)
  if [ "$DEPTH_MODE" = default ]; then
    REVIEW_ROUND=1
    _ud_v=0; _ud_n=0
    for _ua in ${FWD[@]+"${FWD[@]}"}; do
      case "$_ua" in
        --role-variant|--role-variant=*) _ud_v=1 ;;
        # Either delegation token counts as typed: the implied push must not
        # stack on a nudge the caller already forwarded (push replaces nudge).
        --subagents-nudge|--subagents-push) _ud_n=1 ;;
      esac
    done
    [ "$_ud_v" -eq 1 ] || FWD+=(--role-variant deep)
    [ "$_ud_n" -eq 1 ] || FWD+=(--subagents-push)
    unset _ud_v _ud_n _ua
  fi
  # The rounds run shallow (the supervisor types --shallow and sets QWEN_DEPTH, so
  # neither the environment nor a config file can re-imply depth into a round), and
  # a shallow round would default its per-call timeout back to 1800. Under depth the
  # loop's rounds do the deep work: hand them the hour this run computed through
  # QWEN_TIMEOUT -- exported here, inherited by the exec'd supervisor and by every
  # round and audit call it makes, each of which reads it as its own default. (Not
  # a forwarded --timeout: that lands in the supervisor's passthrough, which the
  # loop pins to the depth switches alone, and the rounds would type a flag the
  # caller never did.) A typed --timeout is already in FWD, where a flag beats the
  # environment for every round; --no-timeout is forwarded as typed and gets no
  # default; a caller-set QWEN_TIMEOUT is the value this export carries anyway.
  if [ "$DEPTH_MODE" != shallow ] && [ "$TIMEOUT_EXPLICIT" -eq 0 ] && [ "$NO_TIMEOUT" -eq 0 ]; then
    export QWEN_TIMEOUT="$TIMEOUT"
  fi
  [ "$REVIEW_ROUND" -eq 1 ] && SUP_ARGS+=(--review-round)
  [ "$KEEP_SANDBOX" -eq 0 ] || [ "$PROBE" -eq 1 ] || { die "--keep-sandbox needs --probe: type --probe --keep-sandbox (the sandbox depth makes on its own is always removed)"; exit $QA_USAGE; }
  [ "$PROBE" -eq 1 ] && SUP_ARGS+=(--probe)
  [ "$KEEP_SANDBOX" -eq 1 ] && SUP_ARGS+=(--keep-sandbox)
  SUP_ARGS+=(--depth "$DEPTH_MODE")
  [ "$DEEP" -eq 1 ] && FWD+=(--role-variant deep --subagents-push)
  resolve_py || { die "no working Python 3.8+ found (set QWEN_PYTHON)"; exit $QA_HARNESS; }
  # The supervisor is exec'd, not sourced: it only sees EXPORTED variables. The
  # config eval above sets shell variables, so each QWEN_* the supervisor or the
  # test runner reads from the environment must be exported here. Guard unset
  # ones (set -u would make a bare "$NAME" an error; ${NAME:-} inside [ ] is fine).
  [ -n "${QWEN_TEST_CMD:-}" ] && export QWEN_TEST_CMD
  [ -n "${QWEN_TEST_TIMEOUT:-}" ] && export QWEN_TEST_TIMEOUT
  [ -n "${QWEN_TEST_MAX_BYTES:-}" ] && export QWEN_TEST_MAX_BYTES
  [ -n "${QWEN_TEST_WORKTREES:-}" ] && export QWEN_TEST_WORKTREES
  [ -n "${QWEN_AGENT_STATE:-}" ] && export QWEN_AGENT_STATE
  case "$UNTIL_DONE" in /*|[A-Za-z]:*) ;; *) UNTIL_DONE="$PWD/$UNTIL_DONE" ;; esac
  case "${WORKDIR:-.}" in /*|[A-Za-z]:*) SUP_REPO="${WORKDIR:-.}" ;; *) SUP_REPO="$PWD/$WORKDIR" ;; esac
  # bash (not $0's interpreter guess): the forwarder execs `bash qwen-agent.sh`, keep that.
  PYTHONPATH="$(native_path "$SKILL_DIR")" exec "$QA_PY" "$(native_path "$SKILL_DIR/lib/supervisor.py")" \
    --task "$(native_path "$UNTIL_DONE")" --repo "$(native_path "$SUP_REPO")" \
    --agent "${BASH:-bash}" --agent "$(native_path "$SKILL_DIR/qwen-agent.sh")" \
    ${SUP_ARGS[@]+"${SUP_ARGS[@]}"} -- ${FWD[@]+"${FWD[@]}"}
fi

# --------------------------------------------------------- validate inputs
if [ "$DEEP" -eq 1 ] && { [ -z "$ROLE" ] || [ -n "$ROLE_FILE" ]; }; then
  die "--deep includes --role-variant deep, which needs -r auditor or -r coder (and no --role-file)"
  exit $QA_USAGE
fi
if [ -n "$ROLE_VARIANT" ]; then
  [ -z "$ROLE_FILE" ] || { die "--role-variant cannot be combined with --role-file: variants exist only for the built-in auditor and coder roles"; exit $QA_USAGE; }
  [ -n "$ROLE" ] || { die "--role-variant needs a built-in role: -r auditor or -r coder"; exit $QA_USAGE; }
fi
if [ "$PROBE" -eq 1 ] && [ "$PROBE_HERE" -eq 1 ]; then
  die "--probe and --probe-here are exclusive: --probe makes a sandbox, --probe-here runs in one that exists"
  exit $QA_USAGE
fi
[ "$KEEP_SANDBOX" -eq 0 ] || [ "$PROBE" -eq 1 ] || { die "--keep-sandbox needs --probe: type --probe --keep-sandbox (the sandbox depth makes on its own is always removed)"; exit $QA_USAGE; }
if [ "$PROBE" -eq 1 ] || [ "$PROBE_HERE" -eq 1 ]; then
  PROBING=1
  # Name the flag the caller typed: a refusal --deep's implied --probe triggers is
  # about --deep, and "-w cannot be combined with --probe" would send the user to
  # a flag they never gave.
  if [ "$PROBE_HERE" -eq 1 ]; then _pf="--probe-here"
  elif [ "$DEEP" -eq 1 ]; then _pf="--deep (implies --probe)"
  else _pf="--probe"; fi
  _probe_refuse() { die "$_pf cannot be combined with $1: $2"; exit $QA_USAGE; }
  [ "$BG" -eq 0 ]                 || _probe_refuse "-w" "the sandbox must be removed by this process"
  [ "$ALL_TOOLS" -eq 0 ]          || _probe_refuse "--all-tools" "$_pf sets the toolset itself"
  [ "$TOOLSET_EXPLICIT" -eq 0 ]   || _probe_refuse "--toolset/--read-only" "$_pf sets the toolset itself"
  [ "$TOOLS_EXPLICIT" -eq 0 ]     || _probe_refuse "-t/--tools" "$_pf grants Bash, Edit and Write itself"
  [ "$PERM_MODE_EXPLICIT" -eq 0 ] || _probe_refuse "--permission-mode" "$_pf fixes the permission mode to dontAsk"
  # Only the CALLER's -D/--add-dir stands here: --browser's own entry, the run
  # folder, joins ADD_DIRS later, where that folder is made -- it is never the
  # user's tree, so this refusal is exempt for it by construction.
  [ "${#ADD_DIRS[@]}" -eq 0 ]     || _probe_refuse "-D/--add-dir" "edits are granted everywhere the session can reach, and that must stay the sandbox"
  [ "$PROBE" -eq 0 ] || [ -z "$RESUME_ID" ] || _probe_refuse "--resume" "a new sandbox has a new path and Claude Code finds a session by its directory; keep the first sandbox (--keep-sandbox) and resume in it with --probe-here -C <its path>"
  # --probe-here has no sandbox of this run to point qwen-test at: the session stands IN
  # the kept one, so its tests are that sandbox's. A caller-named --test-repo would send
  # qwen-test (Bash, dontAsk) back into the user's real tree.
  [ "$PROBE_HERE" -eq 0 ] || [ -z "$TEST_REPO" ] || {
    die "--probe-here runs the sandbox's own tests; --test-repo is not allowed"
    exit $QA_USAGE; }
fi
PROBE_SOURCE="$TEST_REPO"           # --test-repo names what --probe copies; read before --test defaults it
case "$EFFORT" in
  ''|*[!A-Za-z0-9_-]*) die "invalid --effort '$EFFORT'"; exit $QA_USAGE ;;
esac
if [ -n "$EFFORT_ALLOWED" ] && [ "$EFFORT" != default ]; then
  case " $(printf '%s' "$EFFORT_ALLOWED" | tr ',|' '  ') " in
    *" $EFFORT "*) ;;
    *) die "effort '$EFFORT' is not accepted by this server (QWEN_EFFORT_ALLOWED: $EFFORT_ALLOWED)"
       exit $QA_USAGE ;;
  esac
fi

if [ "$NO_TIMEOUT" -eq 1 ]; then
  TIMEOUT=""            # run claude directly, with no `timeout` wrapper
else
  case "$TIMEOUT" in
    ''|*[!0-9]*) die "--timeout must be a whole number of seconds, got '$TIMEOUT'"; exit $QA_USAGE ;;
    *[!0]*) ;;          # any non-zero digit somewhere => fine
    *) die "--timeout $TIMEOUT disables the timeout entirely (0 would mean no limit); use --no-timeout if that is really what you want"
       exit $QA_USAGE ;;
  esac
fi

# `timeout` is GNU coreutils: absent on stock macOS (gtimeout via Homebrew), and on
# Windows a different timeout.exe can shadow it. Only a GNU one is trusted; with
# none, run_claude falls back to a built-in watchdog.
if [ -n "$TIMEOUT" ]; then
  for _c in "${QWEN_TIMEOUT_BIN:-}" timeout gtimeout; do
    [ -n "$_c" ] || continue
    [ "$_c" = none ] && break
    command -v "$_c" >/dev/null 2>&1 || continue
    "$_c" --version 2>/dev/null | grep -qE 'GNU coreutils|uutils' || continue
    TIMEOUT_BIN="$_c"
    break
  done
  unset _c
fi
case "$CTX" in
  '') : ;;
  *[!0-9]*) die "--ctx must be a whole number, got '$CTX'"; exit $QA_USAGE ;;
esac
case "$AUTOCOMPACT" in
  ''|auto|default) : ;;
  *[!0-9]*) die "--autocompact takes 'auto', a whole number of tokens, or use --no-autocompact; got '$AUTOCOMPACT'"; exit $QA_USAGE ;;
  *) if [ "$AUTOCOMPACT" -lt 100000 ] || [ "$AUTOCOMPACT" -gt 1000000 ]; then
       die "--autocompact must be between 100000 and 1000000 tokens (claude's accepted range), got '$AUTOCOMPACT'"; exit $QA_USAGE
     fi ;;
esac

# The window may only be known once preflight has read /v1/models, so the
# "autocompact below the window" rule runs here AND again after resolution.
check_autocompact_vs_ctx() {
  case "$AUTOCOMPACT" in ''|auto|default) return 0 ;; esac
  [ -n "$CTX" ] || return 0
  if [ "$AUTOCOMPACT" -ge "$CTX" ]; then
    die "--autocompact ($AUTOCOMPACT) must be BELOW the context window ($CTX), or it can never fire before the server's hard limit"
    return 1
  fi
}
check_autocompact_vs_ctx || exit $QA_USAGE

# A RELATIVE --mcp-config NAMES THE CALLER'S FILE: the existence check below
# resolves it against the caller's directory, but claude would resolve the same
# relative string inside -C -- without this the directory under audit could swap
# in its own mcp.json and choose which MCP servers (arbitrary commands) start.
# Absolutize it the way -o is: relative to the caller, never to --cd.
case "$MCP_CONFIG" in ''|/*|[A-Za-z]:*) ;; *) MCP_CONFIG="$PWD/$MCP_CONFIG" ;; esac
# A typo'd --mcp-config must not become a run that silently loads the wrong MCP
# servers (or none); checking the file up front also makes the implied
# --strict-mcp safe: nothing configured can sneak in beside a missing file.
if [ -n "$MCP_CONFIG" ]; then
  [ -f "$MCP_CONFIG" ] || { die "--mcp-config: no such file: $MCP_CONFIG"; exit $QA_USAGE; }
  STRICT_MCP=1
fi

# ------------------------------------------------------- --scenarios validation
# The suite is checked BEFORE claude could ever start: a broken file is exit 2
# naming its line, and nothing runs. A RELATIVE FILE NAMES THE CALLER'S FILE --
# absolutized here against the caller's directory, like --mcp-config and -o,
# never against -C. The prompt is scenarios.py's own task text for the suite, so
# what the tester is handed and what the answer is later scored against cannot
# drift apart. resolve_py runs here (not at its usual place below) because a
# suite with no python to parse it must fail as a harness problem, once.
SCENARIOS_GIVEN="$SCENARIOS"
if [ -n "$SCENARIOS" ]; then
  case "$SCENARIOS" in /*|[A-Za-z]:*) ;; *) SCENARIOS="$PWD/$SCENARIOS" ;; esac
  [ -r "$SCENARIOS" ] || { die "--scenarios: cannot read the scenario file: $SCENARIOS_GIVEN"; exit $QA_USAGE; }
  if ! resolve_py; then
    die "no working Python 3.8+ found (tried \$QWEN_PYTHON, python3, python)."
    die "set QWEN_PYTHON to a real interpreter, or put one in $QA_CONFIG."
    exit $QA_HARNESS
  fi
  # The calls inline what skill_py (defined further down) does: this block runs
  # before that function exists.
  _sc_py() { PYTHONPATH="$(native_path "$SKILL_DIR")" "$QA_PY" \
               "$(native_path "$SKILL_DIR/lib/scenarios.py")" "$@"; }
  _sc_err="$(err_file)" || exit $QA_HARNESS
  if ! _sc_py check "$(native_path "$SCENARIOS")" >/dev/null 2>"$_sc_err"; then
    _e="$(cat "$_sc_err" 2>/dev/null)"; rm -f "$_sc_err"
    die "--scenarios: ${_e#scenarios.py: }"
    exit $QA_USAGE
  fi
  rm -f "$_sc_err"
  PROMPT="$(_sc_py prompt "$(native_path "$SCENARIOS")")"
fi

# Prompt sources are mutually exclusive.
nsrc=0
[ "$PROMPT_SET" -eq 1 ] && nsrc=$((nsrc+1))
[ -n "$PROMPT_FILE" ]   && nsrc=$((nsrc+1))
[ "$READ_STDIN" -eq 1 ] && nsrc=$((nsrc+1))
if [ "$nsrc" -gt 1 ]; then
  die "give the prompt exactly one way: positional, -f FILE, or --stdin"
  exit $QA_USAGE
fi

if [ -n "$PROMPT_FILE" ]; then
  if [ "$PROMPT_FILE" = "-" ]; then
    if [ -t 0 ]; then die "-f - given but stdin is a terminal"; exit $QA_USAGE; fi
    PROMPT="$(cat)"
  elif [ -r "$PROMPT_FILE" ]; then
    PROMPT="$(cat -- "$PROMPT_FILE")"
  else
    die "cannot read prompt file: $PROMPT_FILE"; exit $QA_USAGE
  fi
elif [ "$READ_STDIN" -eq 1 ]; then
  if [ -t 0 ]; then die "--stdin given but stdin is a terminal"; exit $QA_USAGE; fi
  PROMPT="$(cat)"
fi

# Trim whitespace-only prompts. --preflight-only never sends one, so it is exempt,
# and --interactive has none at all: the person at the keyboard types it.
if [ "$PREFLIGHT_ONLY" -eq 0 ] && [ "$INTERACTIVE" -eq 0 ]; then
  case "$PROMPT" in
    *[![:space:]]*) ;;
    *) die "no prompt given (see --help)"; exit $QA_USAGE ;;
  esac
fi

if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  die "claude binary not found: $CLAUDE_BIN (set QWEN_CLAUDE_BIN)"
  exit $QA_HARNESS
fi

if ! resolve_py; then
  die "no working Python 3.8+ found (tried \$QWEN_PYTHON, python3, python)."
  die "a python on PATH is not enough -- it must actually run 'import json'."
  die "set QWEN_PYTHON to a real interpreter, or put one in $QA_CONFIG."
  exit $QA_HARNESS
fi

# ------------------------------------------------------------- resolve role
SYSTEM=""
if [ -n "$ROLE_FILE" ]; then
  [ -r "$ROLE_FILE" ] || { die "cannot read role file: $ROLE_FILE"; exit $QA_USAGE; }
  SYSTEM="$(cat -- "$ROLE_FILE")"
elif [ -n "$ROLE" ] && [ -n "$ROLE_VARIANT" ]; then
  if ! SYSTEM="$(builtin_variant "$ROLE" "$ROLE_VARIANT")"; then
    die "role '$ROLE' has no '$ROLE_VARIANT' variant (built-in variants: auditor deep, coder deep)"
    exit $QA_USAGE
  fi
  apply_role_defaults "$ROLE"
elif [ -n "$ROLE" ]; then
  if ! SYSTEM="$(resolve_role "$ROLE")"; then
    die "unknown role '$ROLE'. Known roles:"
    list_roles >&2
    exit $QA_USAGE
  fi
  apply_role_defaults "$ROLE"
fi
if [ -n "$EXTRA_SYS" ]; then
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$EXTRA_SYS"; else SYSTEM="$EXTRA_SYS"; fi
fi

# The probe directory, resolved once, here, while this process still stands in the
# caller's directory (a relative QWEN_PROBE_DIR is the caller's, like QWEN_OUTDIR):
# the shell's checks, the default patch directory and probe.py all use this one path.
PROBE_ROOT="${QWEN_PROBE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/qwen-agent/probes}"
case "$PROBE_ROOT" in /*|[A-Za-z]:*) ;; *) PROBE_ROOT="$PWD/$PROBE_ROOT" ;; esac
# An existing directory in this shell's spelling with every symlink resolved (the
# drive form C:/x becomes /c/x under Git Bash); empty when it cannot be entered.
physical_dir() {
  local d="$1"
  command -v cygpath >/dev/null 2>&1 && d="$(cygpath -u "$d")"
  (cd -P -- "$d" 2>/dev/null && pwd -P)
}
# ------------------------------------------------------- implied --probe
# P is the one implied part that can only be judged once the ROLE is known (coder
# and mechanic default to write mode; tester implies --browser). Everything that
# makes a typed --probe refuse makes the implied one step aside instead -- the
# implied depth must never be the reason a run dies: the cheap pre-check below
# drops the obvious shapes, and a refusal probe.py still finds is taken as "no
# sandbox" at create (see the --probe setup: the implied P steps aside there with
# one note, never an exit 2). It is skipped for non-session modes too:
# --preflight-only can run in a plain directory (no git repo), and a sandbox needs
# one to copy -- and for a --test run: its reproduction test lives in the test
# worktree and comes back under ## REPRO FILES, while a sandbox would delete the
# test with itself. The probe's own refusals and setup above and below then
# proceed on PROBING exactly as for a typed --probe.
if [ "$DEPTH_MODE" = default ] && [ "$PROBE" -eq 0 ] && [ "$PROBE_HERE" -eq 0 ] \
   && [ "$WRITE_MODE" -eq 0 ] && [ "$INTERACTIVE" -eq 0 ] && [ "$TEST_MODE" -eq 0 ] \
   && [ "$PREFLIGHT_ONLY" -eq 0 ] && [ "$BROWSER" -eq 0 ] && [ "$BG" -eq 0 ] \
   && [ "$ALL_TOOLS" -eq 0 ] && [ "$TOOLSET_EXPLICIT" -eq 0 ] && [ "$TOOLS_EXPLICIT" -eq 0 ] \
   && [ "$PERM_MODE_EXPLICIT" -eq 0 ] && [ "${#ADD_DIRS[@]}" -eq 0 ] && [ -z "$RESUME_ID" ]; then
  # A sandbox must also be BUILDABLE here: it copies the git work tree holding the -C
  # directory and lives under the probe directory, and probe.py refuses when the tree
  # has no commit or the probe directory sits inside that tree. These are the cheap
  # shapes of those checks; probe.py keeps the fine ones, and a refusal there only
  # costs an implied probe its sandbox (it steps aside at create, below).
  # Paths are compared in one spelling: git names the top level C:/... under Git Bash
  # while the shell spells it /c/..., and a symlinked -C differs from both.
  _ip_src="${PROBE_SOURCE:-${WORKDIR:-$PWD}}"
  case "$_ip_src" in /*|[A-Za-z]:*) ;; *) _ip_src="$PWD/$_ip_src" ;; esac
  _ip_top="$(git -C "$_ip_src" rev-parse --show-toplevel 2>/dev/null)" || _ip_top=""
  [ -n "$_ip_top" ] && { git -C "$_ip_src" rev-parse --verify -q HEAD >/dev/null 2>&1 || _ip_top=""; }
  [ -n "$_ip_top" ] && _ip_top="$(physical_dir "$_ip_top")"
  if [ -n "$_ip_top" ]; then
    # the probe directory inside the tree it would copy: probe.py refuses it. The
    # directory may not exist yet; its nearest existing parent decides the spelling.
    _ip_root="$PROBE_ROOT"; _ip_rest=""
    while [ ! -d "$_ip_root" ] && [ "${_ip_root%/*}" != "$_ip_root" ] && [ -n "${_ip_root%/*}" ]; do
      _ip_rest="/${_ip_root##*/}$_ip_rest"; _ip_root="${_ip_root%/*}"
    done
    _ip_p="$(physical_dir "$_ip_root")"; _ip_root="${_ip_p:-$_ip_root}$_ip_rest"
    _ip_t="${_ip_top%/}"
    case "$_ip_root/" in "$_ip_t"/*) _ip_top="" ;; esac
  fi
  if [ -n "$_ip_top" ]; then
    PROBE=1; PROBING=1; _pf="--probe"
    # The sandbox is the plan from here, so the git hygiene of the block above
    # applies to this run too -- taken HERE, where the implied P applies, always
    # before probe.py create: sandbox.py's git calls inherit the environment
    # instead of stripping it, so an inherited GIT_DIR would steer the clone.
    # (A create that then refuses costs this run its GIT_* -- a half-steered git
    # context is never handed on to a session; the run itself goes on fine.)
    unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
          GIT_ALTERNATE_OBJECT_DIRECTORIES
  fi
  unset _ip_src _ip_top _ip_t _ip_root _ip_rest _ip_p
fi

# ------------------------------------------------------------- --browser setup
# Resolved after the role so -r tester can imply --browser. ONE MCP server, named
# "playwright", is written as a config into a fresh browser run folder and passed with
# --mcp-config, which implies --strict-mcp: no other MCP server loads. Nothing is
# CREATED here: the folder is made just before claude is started (below the preflight),
# after every refusal, validation and preflight has had its chance to exit -- a run
# that dies before claude starts leaves no browser folder behind, and --dry-run makes
# nothing at all. The folder is NEVER the caller's directory or the -C directory: it
# lives under QWEN_BROWSER_DIR (default $XDG_CACHE_HOME/qwen-agent/browser, else
# ~/.cache/...; a RELATIVE value resolves against the caller, like QWEN_OUTDIR), and
# its root is absolutized HERE -- before the -C chdir below -- while this process
# still stands in the caller's directory.
if [ "$BROWSER" -eq 1 ]; then
  [ -z "$MCP_CONFIG" ] || {
    die "--browser brings its own MCP config (one server: playwright): --mcp-config cannot be combined with --browser"
    exit $QA_USAGE; }
  if [ "$HEADED" -eq 1 ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    die "--headed needs a display (DISPLAY is not set)"
    exit $QA_USAGE
  fi
  BROWSER_ROOT="${QWEN_BROWSER_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/qwen-agent/browser}"
  case "$BROWSER_ROOT" in /*|[A-Za-z]:*) ;; *) BROWSER_ROOT="$PWD/$BROWSER_ROOT" ;; esac
  # --dry-run shows this in place of the real path: the run folder's name comes from
  # mktemp, so there is no real config path until a run that starts makes the folder.
  [ "$DRY_RUN" -eq 1 ] && MCP_CONFIG='<browser run folder>/mcp.json'
  STRICT_MCP=1
fi

# ------------------------------------------------------ effective tool policy
# The SAFE path is the one you get by typing nothing. Mutation is opt-in.
if [ "$TOOLSET_EXPLICIT" -eq 0 ]; then
  if [ "$ALL_TOOLS" -eq 1 ]; then
    TOOLSET=""
  elif [ "$WRITE_MODE" -eq 1 ]; then
    TOOLSET="$TOOLSET_WRITE"
  else
    TOOLSET="$TOOLSET_READONLY"
  fi
fi
if [ "$STRICT_MCP_EXPLICIT" -eq 0 ] && [ "$ALL_TOOLS" -eq 0 ]; then
  STRICT_MCP=1
fi
if [ "$TOOLS_EXPLICIT" -eq 0 ]; then
  # --toolset none leaves no built-in tool to grant: the default grant list would
  # advertise permissions for tools the run cannot have, so the grant stays empty.
  # --web/--subagents below still append their tools to it.
  if [ "$TOOLSET_NONE" -eq 1 ]; then TOOLS=""
  elif [ "$WRITE_MODE" -eq 1 ]; then TOOLS="$GRANTS_WRITE"; else TOOLS="$GRANTS_DEFAULT"; fi
fi
if [ "$TEST_MODE" -eq 1 ]; then
  # Never left to the settings files --restricted ignores, and never to a
  # caller-chosen mode: dontAsk asks nothing, so nothing beyond the grants made
  # below is ever applied. Write/coder runs still edit -- --allowed-tools hands
  # them Edit/Write/MultiEdit already granted; acceptEdits would auto-accept
  # every edit, granted or not. A --permission-mode of one's own collides with
  # the fence: refuse it here (before the write warning below can print it).
  [ "$PERM_MODE_EXPLICIT" -eq 1 ] && { die "--test cannot be combined with --permission-mode: --test fixes the permission mode to dontAsk (got '$PERM_MODE'); drop --permission-mode"; exit $QA_USAGE; }
  PERM_MODE="dontAsk"
elif [ "$PROBING" -eq 1 ]; then
  PERM_MODE="dontAsk"               # the grants below are the whole fence, as under --test
elif [ "$WRITE_MODE" -eq 1 ] && [ -z "$PERM_MODE" ]; then
  PERM_MODE="acceptEdits"
fi
# Warn on stderr whenever this run can mutate anything. Deliberately uses die()
# (not note()) so -q cannot hide it.
# An explicit --toolset OVERRIDES --all-tools (see the effective policy above),
# so with one given the every-tool claim would be false: take the branch that
# describes the toolset actually passed instead.
if [ "$ALL_TOOLS" -eq 1 ] && [ "$TOOLSET_EXPLICIT" -eq 0 ]; then
  die "WARNING: --all-tools — every built-in tool is available, including Bash and Write"
elif [ "$WRITE_MODE" -eq 1 ]; then
  # ${TOOLSET:-none}: --toolset none leaves the list empty, and an empty list in
  # the warning would read like a mistake rather than the point of 'none'.
  die "WARNING: write-enabled run (toolset '${TOOLSET:-none}', permission-mode '$PERM_MODE')"
elif [ "$TOOLSET_EXPLICIT" -eq 1 ]; then
  case ",$TOOLSET," in
    *,Bash,*|*,Write,*|*,Edit,*|*,MultiEdit,*|*,NotebookEdit,*)
      die "WARNING: --toolset '$TOOLSET' can modify files" ;;
  esac
fi

# -o and QWEN_OUTDIR are relative to where the CALLER is (like -f), never to --cd:
# output must not land inside the directory under audit by accident.
case "$OUT" in ''|/*|[A-Za-z]:*) ;; *) OUT="$PWD/$OUT" ;; esac
QWEN_OUTDIR_SET=0
[ -n "${QWEN_OUTDIR:-}" ] && QWEN_OUTDIR_SET=1
QWEN_OUTDIR="${QWEN_OUTDIR:-$PWD}"
case "$QWEN_OUTDIR" in /*|[A-Za-z]:*) ;; *) QWEN_OUTDIR="$PWD/$QWEN_OUTDIR" ;; esac
# A --probe run "only reads" the user's tree, and the usual caller stands INSIDE it:
# with QWEN_OUTDIR left at the cwd default, the default patch (--probe --write with
# no -o) would land in the tree it reports on -- and the next probe would copy it
# back as dirt. An unset QWEN_OUTDIR therefore sends the patch to the probe
# directory itself (create() makes it; probe.py refuses one inside the tree). An
# explicit QWEN_OUTDIR (env or config) is the caller's own choice and stays, exactly
# like -o inside the tree stays. Same caller-side absolutizing as the line above.
# The caller's default as it stood before a probe took it over: an implied --probe
# that then steps aside (create refuses) restores it, so the run left standing is
# the unsandboxed one the caller would have got under --shallow.
_qa_outdir_kept=""
if [ "$PROBE" -eq 1 ] && [ "$QWEN_OUTDIR_SET" -eq 0 ]; then
  _qa_outdir_kept="$QWEN_OUTDIR"
  QWEN_OUTDIR="$PROBE_ROOT"
fi
# --record DIR is the caller's directory too -- never the -C directory's, which is the
# tree under test and has no business holding the replay scripts of a suite run against
# it. Same caller-side absolutizing as -o, one line before the -C chdir.
case "$RECORD" in ''|/*|[A-Za-z]:*) ;; *) RECORD="$PWD/$RECORD" ;; esac

# ------------------------------------------------------------- working dir
if [ -n "$WORKDIR" ]; then
  [ -d "$WORKDIR" ] || { die "--cd: not a directory: $WORKDIR"; exit $QA_USAGE; }
  cd -- "$WORKDIR" || { die "--cd failed: $WORKDIR"; exit $QA_USAGE; }
fi

# shellcheck disable=SC2329  # invoked via trap
cleanup() {
  [ -n "${TMPD:-}" ] && rm -rf "$TMPD"
  [ -n "${TEST_WT:-}" ] && skill_py testrun.py --cleanup "$(native_path "$TEST_REPO")" "$(native_path "$TEST_WT")" >/dev/null 2>&1
  if [ -n "${PROBE_RUN:-}" ] && [ "$KEEP_SANDBOX" -eq 0 ]; then
    cd / 2>/dev/null                # Windows cannot remove the directory a process stands in
    skill_py probe.py remove "$(native_path "$PROBE_RUN")" >/dev/null 2>&1 \
      || die "sandbox not removed: $PROBE_RUN"
  fi
  return 0
}

# ------------------------------------------------------------- --test setup
skill_py() {  # run a lib/ module with this skill on PYTHONPATH
  PYTHONPATH="$(native_path "$SKILL_DIR")" "$QA_PY" "$(native_path "$SKILL_DIR/lib/$1")" "${@:2}"
}
if [ "$TEST_MODE" -eq 1 ]; then
  [ "$ALL_TOOLS" -eq 1 ] && { die "--test cannot be combined with --all-tools (--test is a fence, --all-tools removes it)"; exit $QA_USAGE; }
  [ "$BG" -eq 1 ] && { die "--test cannot be combined with -w (the worktree must be cleaned up by this process)"; exit $QA_USAGE; }
  # --test builds its own fence (toolset, grants, permission mode); a hand-made
  # toolset on top of it would silently widen or break that fence.
  [ "$READ_ONLY_FLAG" -eq 1 ] && { die "--test cannot be combined with --read-only: a --test run gains Bash (qwen-test only) and, when read-only, writes inside its worktree; drop --read-only"; exit $QA_USAGE; }
  [ "$TOOLSET_EXPLICIT" -eq 1 ] && { die "--test cannot be combined with --toolset: --test sets the toolset itself; drop --toolset"; exit $QA_USAGE; }
  # -t/--tools IS the --allowed-tools grant list, and --test keeps it to
  # qwen-test; an explicit one REPLACES those grants, so `--test -t 'Bash(*)'`
  # would hand an unrestricted shell to a dontAsk run. The fence cannot widen.
  [ "$TOOLS_EXPLICIT" -eq 1 ] && { die "--test cannot be combined with -t/--tools: --test grants qwen-test only and an explicit grant list replaces that (e.g. Bash(*) would be an unrestricted shell); drop -t/--tools"; exit $QA_USAGE; }
  # Whitespace-only counts as unset (like qwen-sweep's guard): it survives an
  # -n test but splits into nothing, and every qwen-test in the run would die.
  case "${QWEN_TEST_CMD:-}" in
    *[![:space:]]*) ;;
    *) die "--test needs a test command: set QWEN_TEST_CMD in $QA_CONFIG"; exit $QA_USAGE ;;
  esac
  # --restricted is what makes the fence independent of the user's and the repo's
  # Claude settings (it ignores their settings files and confines the file tools
  # to the working directories). An older claude without it cannot be fenced.
  "$CLAUDE_BIN" --help 2>/dev/null | grep -q -- '--restricted' \
    || { die "--test needs a Claude Code with --restricted; upgrade claude"; exit $QA_USAGE; }
  TEST_REPO="${TEST_REPO:-$PWD}"
  # The fence is invisible to the model: without this it burns turns on cat,
  # grep, find and git calls the permission system denies. Every role, every
  # --test run; appended after the role text and any -s text.
  # shellcheck disable=SC2016  # the backticks are literal prompt text, not command substitution
  _fence_note='Your only shell command is `qwen-test [SELECTOR]`, run with the Bash tool (it is a command, not a tool). Every other Bash command is denied and wastes a turn. Instead of cat/head/tail use Read; instead of grep/rg use Grep; instead of find/ls use Glob. You cannot run git, python, pip, env or which. To check a change, run its test with qwen-test.'
  if [ "$PROBING" -eq 1 ]; then :      # a probe run has a whole shell: the probe note says so
  elif [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_fence_note"; else SYSTEM="$_fence_note"; fi
  unset _fence_note
fi

# ------------------------------------------------------------- --probe setup
# An IMPLIED --probe (default depth, or --deep on a read-only run) never ends a run:
# when the sandbox cannot be had, it steps aside and the run goes on exactly as the
# --shallow run would, unsandboxed and read-only. A typed --probe keeps every
# refusal, and so does --deep on a writing run: there the sandbox is the promise that
# the edits come back as a patch and the tree stays untouched.
probe_is_implied() {
  [ "$PROBE" -eq 1 ] && [ "$PROBE_EXPLICIT" -eq 0 ] && [ "$PROBE_HERE" -eq 0 ] \
    && { [ "$DEEP" -eq 0 ] || [ "$WRITE_MODE" -eq 0 ]; }
}
# Drop the probe and its fence. Nothing beyond the permission mode and the
# QWEN_OUTDIR default is set from PROBING yet (the grants, the note and --restricted
# all come later, keyed off PROBING). A --test run fixes dontAsk itself, so its
# fence is left exactly as a --shallow --test run has it.
probe_step_aside() {
  PROBE=0; PROBING=0
  if [ "$TEST_MODE" -eq 0 ]; then PERM_MODE=""; fi
  [ -n "$_qa_outdir_kept" ] && QWEN_OUTDIR="$_qa_outdir_kept"
  note "depth: no sandbox for this run ($1); running without --probe"
}
if [ "$PROBING" -eq 1 ]; then
  # --restricted keeps the fence independent of the user's and the project's settings
  # files and confines the file tools to the working directories -- here the sandbox.
  if ! "$CLAUDE_BIN" --help 2>/dev/null | grep -q -- '--restricted'; then
    if probe_is_implied; then
      probe_step_aside "this claude has no --restricted"
    else
      die "$_pf needs a Claude Code with --restricted; upgrade claude"; exit $QA_USAGE
    fi
  fi
fi
if [ "$PROBE_HERE" -eq 1 ]; then
  # The marker lib/swarm_engine/sandbox.py writes beside every sandbox is not enough on
  # its own: `<toplevel>.base` is a text file anyone can write beside their own
  # checkout, and a plain tree, the user's above all, must never get a probe fence.
  # probe.py check verifies the whole footprint create() leaves (both markers, no symlink,
  # a `sandboxes/` parent, and a `.base` naming a commit inside) and prints the sandbox it
  # checked. The refusal comes before anything else this run would do in the tree.
  _errf="$(err_file)" || exit $QA_HARNESS
  _out="$(skill_py probe.py check "$(native_path "$PWD")" 2>"$_errf")"
  _rc=$?
  _e="$(cat "$_errf")"; rm -f "$_errf"
  if [ "$_rc" -ne 0 ]; then
    die "--probe-here: $PWD is not inside a probe sandbox (one kept with --probe --keep-sandbox); use --probe"
    [ -n "$_e" ] && die "--probe-here: $_e"
    exit $QA_USAGE
  fi
  PROBE_SB="$(printf '%s\n' "$_out" | tr -d '\r')"; PROBE_CWD="$PWD"
  # qwen-test runs the tests of the sandbox, never of the user's tree.
  _sbu="$PROBE_SB"
  command -v cygpath >/dev/null 2>&1 && _sbu="$(cygpath -u "$_sbu")"
  [ "$TEST_MODE" -eq 1 ] && TEST_REPO="$_sbu"
  unset _errf _out _rc _e _sbu
fi
if [ "$PROBE" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  QWEN_PROBE_DIR="$(native_path "$PROBE_ROOT")"; export QWEN_PROBE_DIR
  _errf="$(err_file)" || exit $QA_HARNESS
  _src=()
  [ -n "$PROBE_SOURCE" ] && _src=(--source "$(native_path "$PROBE_SOURCE")")
  _out="$(skill_py probe.py create --cwd "$(native_path "$PWD")" ${_src[@]+"${_src[@]}"} 2>"$_errf")"
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    _e="$(cat "$_errf")"; rm -f "$_errf"
    if probe_is_implied; then
      # The pre-check above cannot see every refusal probe.py makes (a -C inside an
      # ignored directory, a -C outside a --test-repo it is asked to copy), nor a
      # broken environment (an unwritable probe directory, a failed clone, a
      # symlink Windows will not make). Any of them only costs an implied probe
      # its sandbox.
      probe_step_aside "${_e:-probe.py create failed with exit $_rc}"
    else
      die "--probe: $_e"
      [ "$_rc" -eq 2 ] && exit $QA_USAGE
      exit $QA_HARNESS
    fi
  else
    rm -f "$_errf"
    _out="$(printf '%s\n' "$_out" | tr -d '\r')"
    PROBE_RUN="$(printf '%s\n' "$_out" | sed -n 1p)"
    PROBE_SB="$(printf '%s\n' "$_out" | sed -n 2p)"
    PROBE_CWD="$(printf '%s\n' "$_out" | sed -n 3p)"
    PROBE_SRC="$(printf '%s\n' "$_out" | sed -n 4p)"
    # The printed `git -C DIR apply PATCH` must carry an absolute DIR: a relative
    # --test-repo is the caller's string and probe.py prints it as given, but by the
    # time the patch is printed this process has cd'd into the sandbox. Still the
    # caller's directory here (-C already applied), so that is what a relative path
    # resolves against -- as it did for probe.py itself.
    case "$PROBE_SRC" in ''|/*|[A-Za-z]:*) ;; *) PROBE_SRC="$PWD/$PROBE_SRC" ;; esac
    # Registered at once: a failed preflight below must not leak the sandbox.
    trap cleanup EXIT
    _cd="$PROBE_CWD"; _sbu="$PROBE_SB"
    if command -v cygpath >/dev/null 2>&1; then _cd="$(cygpath -u "$_cd")"; _sbu="$(cygpath -u "$_sbu")"; fi
    cd -- "$_cd" || { die "--probe: cannot enter the sandbox $PROBE_CWD"; exit $QA_HARNESS; }
    # qwen-test runs the tests of the sandbox, never of the user's tree.
    [ "$TEST_MODE" -eq 1 ] && TEST_REPO="$_sbu"
  fi
  unset _errf _src _out _rc _e _cd _sbu
fi
if [ "$TEST_MODE" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  _errf="$(err_file)" || exit $QA_HARNESS
  TEST_WT="$(skill_py testrun.py --prepare "$(native_path "$TEST_REPO")" 2>"$_errf")" || {
    _e="$(cat "$_errf")"; rm -f "$_errf"
    die "--test: ${_e#qwen-test: }"; exit $QA_USAGE; }
  rm -f "$_errf"; unset _errf _e
  TEST_WT="$(printf '%s' "$TEST_WT" | tr -d '\r')"
  # Registered NOW, not at the runner: a failed preflight below must not leak the worktree.
  trap cleanup EXIT
fi
if [ "$TEST_MODE" -eq 1 ]; then
  if [ "$TOOLSET_EXPLICIT" -eq 0 ]; then
    case ",$TOOLSET," in *,Bash,*) ;; *) TOOLSET="$TOOLSET,Bash" ;; esac
  fi
  if [ "$TOOLS_EXPLICIT" -eq 0 ]; then
    TOOLS='Bash(qwen-test:*),Read,Glob,Grep'
    if [ "$WRITE_MODE" -eq 1 ]; then
      # A write-mode run (coder, --write) owns the repo it was given. Handing it
      # the worktree as an extra grant would blur which tree it is supposed to
      # edit, so the worktree stays the harness's scratch space: no //worktree
      # rules and no --add-dir for it below.
      TOOLS="$TOOLS,Edit,Write,MultiEdit"
    else
      _wt_rule="$(rule_path "${TEST_WT:-<worktree>}")"
      [ "$TOOLSET_EXPLICIT" -eq 0 ] && TOOLSET="$TOOLSET,Edit,Write"
      TOOLS="$TOOLS,Edit($_wt_rule/**),Write($_wt_rule/**)"
      unset _wt_rule
    fi
  fi
  # Read-only runs may write ONLY inside the worktree, so it must be readable. (A probe
  # run writes in its sandbox instead, and its own warning follows.)
  if [ "$WRITE_MODE" -eq 0 ] && [ -n "$TEST_WT" ] && [ "$PROBING" -eq 0 ]; then ADD_DIRS+=("$TEST_WT"); fi
  # die, not note: -q must not hide that a "read-only" run can now run code.
  [ "$WRITE_MODE" -eq 0 ] && [ "$PROBING" -eq 0 ] && die "WARNING: --test: this read-only run gains Bash (qwen-test only, which runs the repo's tests) and may write inside its throwaway worktree ${TEST_WT:-<worktree>}"
fi
if [ "$PROBING" -eq 1 ]; then
  # The probe fence: a whole shell and the edit tools, granted outright (dontAsk asks
  # nothing), inside the sandbox the session stands in.
  for _t in Edit Write Bash; do
    case ",$TOOLSET," in *,"$_t",*) ;; *) TOOLSET="$TOOLSET,$_t" ;; esac
  done
  unset _t
  TOOLS='Bash,Read,Edit,Write,MultiEdit,Glob,Grep'
  _probe_note="You are working in a throwaway copy of the project, not in the user's files. You have a full shell: run commands with the Bash tool. Use it to check your work: run the code, write small scripts or tests, and try the failure cases."
  if [ "$WRITE_MODE" -eq 1 ]; then
    _probe_note="$_probe_note Your edits are handed to the user as a patch; nothing is applied for you."
  else
    _probe_note="$_probe_note Files you create here are thrown away when the session ends."
  fi
  # shellcheck disable=SC2016  # the backticks are literal prompt text
  [ "$TEST_MODE" -eq 1 ] && _probe_note="$_probe_note"' The configured tests also run with `qwen-test [SELECTOR]`.'
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_probe_note"; else SYSTEM="$_probe_note"; fi
  unset _probe_note
  # die, not note: -q must not hide that this run has a shell.
  die "WARNING: $_pf: this run has a shell (Bash) and edits inside a throwaway sandbox${PROBE_CWD:+ ($PROBE_CWD)}; the sandbox isolates against accidents, not against a hostile model"
fi

# Web access is opt-in (--web / QWEN_WEB=1): nothing above ever names WebFetch,
# so by default no role, no --write run and no --test run reaches the web at all.
# --web adds ONLY WebFetch -- never WebSearch, which is a server-side tool that
# local servers (vLLM) reject with a 400 "body.tools.0.input_schema Field
# required" (search needs an MCP server). --all-tools leaves the toolset
# unrestricted and already has every built-in: it keeps that behaviour.
if [ "$WEB_MODE" -eq 1 ] && [ "$ALL_TOOLS" -eq 0 ]; then
  if [ -n "$TOOLSET" ] || [ "$TOOLSET_NONE" -eq 1 ]; then
    case ",$TOOLSET," in
      *,WebFetch,*) ;;
      *) TOOLSET="${TOOLSET:+$TOOLSET,}WebFetch" ;;
    esac
  fi
  case ",$TOOLS," in
    *,WebFetch,*) ;;
    *) TOOLS="${TOOLS:+$TOOLS,}WebFetch" ;;
  esac
  if [ "$TEST_MODE" -eq 1 ]; then
    # Raw printf, not die(): this line must START with "WARNING: --web with
    # --test", and die() would prefix the program name. Unconditional (not
    # note()) so -q cannot hide that the run can fetch the very answers its
    # tests and checks are supposed to derive.
    printf 'WARNING: --web with --test: tests and checks can be gamed by fetching upstream answers (WebFetch is enabled)\n' >&2
  fi
fi

# Task subagents (--subagents / QWEN_SUBAGENTS=1, also set by default depth): each one is another
# concurrent request against the same server, too much for a small GPU. A
# subagent inherits this run's tool restrictions and grants (and --restricted
# under --test), so Task adds no capability the run did not already have.
if [ "$SUBAGENTS" -eq 1 ] && [ "$ALL_TOOLS" -eq 0 ]; then
  if [ -n "$TOOLSET" ] || [ "$TOOLSET_NONE" -eq 1 ]; then
    case ",$TOOLSET," in *,Task,*) ;; *) TOOLSET="${TOOLSET:+$TOOLSET,}Task" ;; esac
  fi
  case ",$TOOLS," in *,Task,*) ;; *) TOOLS="${TOOLS:+$TOOLS,}Task" ;; esac
  _sub_note='You can delegate to a subagent with the Task tool; it runs on the same local model with the same tool limits as you. Your context window is limited: delegate broad reading and searching (for example "find every caller of X and report file:line") and keep your own context for edits and test runs. Do not delegate edits. Run one subagent at a time: each is another request the server must serve.'
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_sub_note"; else SYSTEM="$_sub_note"; fi
  unset _sub_note
fi
# The delegation section: PUSH REPLACES NUDGE when both are on (a caller who typed
# --subagents-push wants the mandate, not the suggestion; --deep and the implied
# default push, qwen-sweep batches still nudge).
if [ "$PUSH" -eq 1 ]; then
  _push='Delegation is part of this task, not optional. Before you go deep, split the work into independent areas (a module, a guarantee, a group of failure modes). Keep one area for yourself and hand EVERY other area to a subagent, one at a time: give it a self-contained brief with the exact paths and the questions to answer, and ask for path:line evidence and the commands it ran. Verify each subagent'"'"'s key claims yourself before relying on them (re-read the cited lines or re-run its check). Your answer has a DELEGATION section: each subagent'"'"'s area, what it found, and what you verified.'
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_push"; else SYSTEM="$_push"; fi
  unset _push
elif [ "$NUDGE" -eq 1 ]; then
  _nudge='Delegate more than feels necessary. Hand a subagent any piece of work that does not need what you are holding in mind: an independent probe or check, reading a large file or many files, or summarising a long log or test output. Give it a self-contained question and the exact paths it needs, and ask for path:line evidence. Verify what a subagent reports before you rely on it: re-read the lines it cites or re-run its check yourself.'
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_nudge"; else SYSTEM="$_nudge"; fi
  unset _nudge
fi

# --browser: the Playwright tools join the ONE --allowed-tools list the run already
# built (a second -t would replace it, not extend it), on top of whatever toolset the
# run has -- --tools governs built-ins only, MCP tools survive it, so the read-only
# default stays read-only. The server OFFERS more tools than are granted, and a tool
# that is merely not granted still shows up in the model's tool list: it gets tried and
# the run dies on the refusal. So the ones no run gets are passed to claude as
# --disallowedTools too, which keeps them out of the model's sight. browser_evaluate
# runs arbitrary JavaScript inside the page and browser_network_request answers one
# request WITH its response body -- the app's scripts, styles and server replies are
# the source a black-box tester must not read -- so both are granted, and left
# visible, only when --browser-eval asked for them; the LIST browser_network_requests
# (URLs, methods, statuses, no bodies) stays granted. browser_run_code_unsafe runs
# arbitrary code in the browser's own process and browser_install downloads a browser:
# never either.
if [ "$BROWSER" -eq 1 ]; then
  for _t in browser_navigate browser_navigate_back browser_snapshot browser_find \
            browser_click browser_type browser_fill_form browser_press_key \
            browser_select_option browser_hover browser_drag browser_drop \
            browser_file_upload browser_handle_dialog browser_tabs browser_resize \
            browser_emulate_media browser_wait_for browser_take_screenshot \
            browser_console_messages browser_network_requests \
            browser_close; do
    TOOLS="${TOOLS:+$TOOLS,}mcp__playwright__$_t"
  done
  unset _t
  DISALLOWED="mcp__playwright__browser_run_code_unsafe,mcp__playwright__browser_install"
  if [ "$BROWSER_EVAL" -eq 1 ]; then
    TOOLS="${TOOLS:+$TOOLS,}mcp__playwright__browser_evaluate"
    TOOLS="${TOOLS:+$TOOLS,}mcp__playwright__browser_network_request"
  else
    DISALLOWED="$DISALLOWED,mcp__playwright__browser_evaluate"
    DISALLOWED="$DISALLOWED,mcp__playwright__browser_network_request"
  fi
  # Playwright 0.0.83 answers a browser_take_screenshot GIVEN a filename with a LINK
  # only: the image itself comes back just when no filename is given. A saved
  # screenshot is worth nothing to a session that cannot open it, so Read joins the
  # grants even when the toolset is none -- Read ONLY, never Glob/Grep/Bash/Write.
  # With --toolset none Read joins the TOOLSET too (the way --web adds WebFetch), or
  # the empty --tools would take back the tool this whole grant exists to hand out.
  case ",$TOOLS," in *,Read,*) ;; *) TOOLS="${TOOLS:+$TOOLS,}Read" ;; esac
  if [ "$TOOLSET_NONE" -eq 1 ]; then
    case ",$TOOLSET," in *,Read,*) ;; *) TOOLSET="${TOOLSET:+$TOOLSET,}Read" ;; esac
  fi
  if [ "$TEST_MODE" -eq 1 ]; then
    # Raw printf, not die(): like the --web warning this line must START with
    # "WARNING: --browser with --test", and unconditional (not note()) so -q cannot
    # hide it: the browser opens the same upstream pages -- internet included,
    # independently of --web -- that a run's tests should derive their answers from.
    printf 'WARNING: --browser with --test: tests and checks can be gamed by browsing upstream answers\n' >&2
  fi
fi

# ------------------------------------------------------- validate -o target
# Do this BEFORE spending a model call: a completed run thrown away because of a
# typo'd path is the worst outcome, and with --timeout 1800 it can cost 30 min.
if [ -n "$OUT" ]; then
  outdir="$(dirname -- "$OUT")"
  [ -d "$outdir" ] || { die "--out: no such directory: $outdir"; exit $QA_USAGE; }
  [ -w "$outdir" ] || { die "--out: directory is not writable: $outdir"; exit $QA_USAGE; }
  if [ -e "$OUT" ] && [ ! -w "$OUT" ]; then
    die "--out: exists and is not writable: $OUT"; exit $QA_USAGE
  fi
fi

# --record DIR, checked before the same model call: the recording is the run's whole
# point, and finding at the end of it that the scripts had nowhere to go wastes the
# suite. The directory itself is made by scenarios.py when the first script is kept.
if [ -n "$RECORD" ]; then
  if [ -e "$RECORD" ]; then
    [ -d "$RECORD" ] || { die "--record: not a directory: $RECORD"; exit $QA_USAGE; }
    [ -w "$RECORD" ] || { die "--record: directory is not writable: $RECORD"; exit $QA_USAGE; }
  else
    _rec_parent="$(dirname -- "$RECORD")"
    { [ -d "$_rec_parent" ] && [ -w "$_rec_parent" ]; } || {
      die "--record: cannot create $RECORD (its parent is absent or not writable)"
      exit $QA_USAGE; }
    unset _rec_parent
  fi
fi

# ---------------------------------------------------------------- preflight
preflight() {
  local resp code body rows ids n chat len
  local hdr=()
  if [ -n "${QWEN_API_KEY:-}" ]; then
    hdr=(-H "Authorization: Bearer $QWEN_API_KEY" -H "x-api-key: $QWEN_API_KEY")
  fi
  # ${hdr[@]+...}: an empty array under set -u is an error before bash 4.4.
  resp="$(curl -s --max-time 8 ${hdr[@]+"${hdr[@]}"} -w '\n%{http_code}' "$BASE/v1/models" 2>/dev/null)"
  code="$(printf '%s\n' "$resp" | tail -n 1 | tr -d '\r')"
  body="$(printf '%s\n' "$resp" | sed '$d')"
  case "$code" in
    200) ;;
    000|'')
      die "cannot reach $BASE/v1/models — is the model server running? (set QWEN_BASE_URL or -b)"
      return $QA_PREFLIGHT ;;
    401|403)
      die "$BASE/v1/models answered $code: the server wants a key. Set QWEN_API_KEY."
      return $QA_PREFLIGHT ;;
    404)
      die "$BASE/v1/models answered 404. QWEN_BASE_URL takes no /v1 suffix; drop it if present."
      die "A server with no model listing needs QWEN_MODEL (and QWEN_CTX) plus QWEN_PREFLIGHT=0."
      return $QA_PREFLIGHT ;;
    *)
      die "$BASE/v1/models answered HTTP $code:"
      printf '%s' "$body" | head -c 200 >&2; echo >&2
      return $QA_PREFLIGHT ;;
  esac
  # One row per served model: "<id><TAB><context window, or empty>". Accepts the
  # OpenAI list shape, a bare list, and a "models" key; several window fields.
  rows="$(printf '%s' "$body" | "$QA_PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(9)
models = d if isinstance(d, list) else (d.get("data") or d.get("models") or [])
def window(m):
    for k in ("max_model_len", "context_length", "max_context_length",
              "loaded_context_length", "max_input_tokens"):
        v = m.get(k)
        if isinstance(v, str) and v.isdigit():
            v = int(v)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return str(v)
    return ""
for m in models:
    if isinstance(m, dict):
        mid = m.get("id") or m.get("name") or m.get("model") or ""
        sys.stdout.write("%s\t%s\n" % (mid, window(m)))
' 2>/dev/null)" || {
    die "could not parse $BASE/v1/models with '$QA_PY' -- either the endpoint"
    die "returned something that is not JSON, or the interpreter failed. Raw head:"
    printf '%s' "$body" | head -c 200 >&2; echo >&2
    return $QA_PREFLIGHT; }
  # A native Windows python writes CRLF; strip it before comparing ids.
  rows="$(printf '%s\n' "$rows" | tr -d '\r')"
  ids="$(printf '%s\n' "$rows" | cut -f1 | grep .)"
  n="$(printf '%s\n' "$ids" | grep -c .)"

  if [ "$n" -eq 0 ]; then
    die "$BASE/v1/models lists no models"
    return $QA_PREFLIGHT
  fi

  if [ -z "$MODEL" ]; then
    # Embedding and reranking models are listed by some servers but cannot chat.
    chat="$(printf '%s\n' "$ids" | grep -viE 'embed|rerank')"
    if [ "$n" -eq 1 ]; then
      MODEL="$ids"
      note "using '$MODEL' (the only model served at $BASE)"
    elif [ "$(printf '%s\n' "$chat" | grep -c .)" -eq 1 ]; then
      MODEL="$chat"
      note "using '$MODEL' (the only non-embedding model served at $BASE)"
    else
      die "several models are served at $BASE; choose one with -m or QWEN_MODEL:"
      printf '%s\n' "$ids" | sed 's/^/  /' >&2
      return $QA_PREFLIGHT
    fi
  elif ! printf '%s\n' "$ids" | grep -qxF -- "$MODEL"; then
    if [ "$AUTO_MODEL" -eq 1 ] && [ "$n" -eq 1 ]; then
      MODEL="$ids"
      note "--auto-model: using '$MODEL' (the only model served at $BASE)"
    else
      die "model '$MODEL' is not served at $BASE"
      die "served models are:"
      printf '%s\n' "$ids" | sed 's/^/  /' >&2
      [ "$n" -eq 1 ] && die "hint: pass --auto-model, or -m '$ids'"
      return $QA_PREFLIGHT
    fi
  fi

  if [ -z "$CTX" ]; then
    # ENVIRON, not awk -v: -v would interpret backslashes in the model id.
    len="$(printf '%s\n' "$rows" | m="$MODEL" awk -F '\t' '$1 == ENVIRON["m"] { print $2; exit }')"
    case "$len" in
      ''|*[!0-9]*) note "the server does not report a context window; set QWEN_CTX so long runs compact before its limit" ;;
      *) CTX="$len" ;;
    esac
  fi
  return 0
}

[ "$PREFLIGHT_ONLY" -eq 1 ] && NO_PREFLIGHT=0
if [ "$NO_PREFLIGHT" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  preflight; pf=$?
  [ "$pf" -eq 0 ] || exit "$pf"
fi

if [ -z "$MODEL" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    MODEL="<detected at run time>"
  elif [ "$PREFLIGHT_ONLY" -eq 0 ]; then
    die "no model: preflight was skipped, so it cannot be detected. Pass -m or set QWEN_MODEL."
    exit $QA_USAGE
  fi
fi

# 'default' autocompact = 3/4 of a known window, within claude's accepted range.
if [ "$AUTOCOMPACT" = "default" ]; then
  AUTOCOMPACT=""
  if [ -n "$CTX" ]; then
    _ac=$((CTX * 3 / 4))
    [ "$_ac" -gt 1000000 ] && _ac=1000000
    if [ "$_ac" -ge 100000 ]; then
      AUTOCOMPACT="$_ac"
    elif [ "$PREFLIGHT_ONLY" -eq 0 ]; then
      note "a $CTX-token window is below claude's autocompact floor; keep each task well inside it"
    fi
    unset _ac
  fi
fi
check_autocompact_vs_ctx || exit $QA_USAGE

# --preflight-only: installers and CI want the checks without a model round-trip.
if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  note "preflight ok: model '$MODEL' served at $BASE, context ${CTX:-unknown}, python '$QA_PY', claude '$CLAUDE_BIN'"
  exit 0
fi

# ------------------------------------------------------- the browser run folder
# Made HERE -- after every refusal, validation and preflight, and never for --dry-run
# (which exits below): a run that never gets to start claude leaves no browser folder
# behind. BROWSER_ROOT was resolved against the caller's directory up in the
# --browser setup; nothing since has needed the folder. The folder name is a fresh
# mktemp -d and is kept when the run ends: the screenshots and page snapshots inside
# are the evidence. The MCP config file lives in it.
if [ "$BROWSER" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p -- "$BROWSER_ROOT" \
    || { die "--browser: cannot create the run folder root: $BROWSER_ROOT"; exit $QA_HARNESS; }
  BROWSER_DIR="$(mktemp -d "$BROWSER_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")" \
    || { die "--browser: could not create a run folder under $BROWSER_ROOT"; exit $QA_HARNESS; }
  # The server command line. QWEN_PLAYWRIGHT_MCP replaces the whole
  # `npx -y --prefer-offline @playwright/mcp@0.0.83` part (and the Git Bash `cmd /c`
  # form of it): split on whitespace, the first word the command, the rest its
  # leading args (before the browser switches). Pathname expansion is OFF across the
  # split -- "node /x/*.js" names a literal file and must reach the config as one
  # word even when files match the pattern.
  _pw="${QWEN_PLAYWRIGHT_MCP:-}"
  case "$_pw" in
    *[![:space:]]*) ;;
    # Native Windows Claude Code cannot spawn `npx` directly as a stdio MCP server
    # (it is npx.cmd, and without the wrapper the connection just closes). cygpath
    # on PATH is what says Git Bash: there the server is started through cmd /c.
    *) if command -v cygpath >/dev/null 2>&1; then
         _pw="cmd /c npx -y --prefer-offline @playwright/mcp@0.0.83"
       else
         _pw="npx -y --prefer-offline @playwright/mcp@0.0.83"
       fi ;;
  esac
  PW_CMD=""
  PW_ARGS=()
  set -f
  # shellcheck disable=SC2086  # deliberate word split: the value is a whole command line
  for _w in $_pw; do
    if [ -z "$PW_CMD" ]; then PW_CMD="$_w"; else PW_ARGS+=("$_w"); fi
  done
  set +f
  unset _pw _w
  [ "$HEADED" -eq 1 ] || PW_ARGS+=("--headless")
  # --output-dir takes the NATIVE spelling: claude starts the server natively, and on
  # Git Bash a POSIX /c/... path would not mean anything to it.
  PW_ARGS+=("--isolated" "--output-dir" "$(native_path "$BROWSER_DIR")"
            "--image-responses" "allow" "--viewport-size" "1280,900")
  MCP_CONFIG="$BROWSER_DIR/mcp.json"
  # The argv is handed over verbatim (MSYS2_ARG_CONV_EXCL, the same guard the child
  # gets): an override like "node /x/cli.js" must reach the JSON unconverted.
  if ! MSYS2_ARG_CONV_EXCL='*' "$QA_PY" - "$(native_path "$MCP_CONFIG")" "$HEADED" \
       "$PW_CMD" ${PW_ARGS[@]+"${PW_ARGS[@]}"} <<'PY'
import json, os, sys
out, headed, cmd = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
server = {"command": cmd, "args": sys.argv[4:]}
if headed:
    # The browser needs the caller's session: copy what qwen-agent has -- the X
    # display when there is one, else the Wayland socket (which lives under
    # XDG_RUNTIME_DIR, so the server needs that too). No empty DISPLAY key on a
    # Wayland session: it would only send the server looking for an X display.
    env = {}
    if os.environ.get("DISPLAY"):
        env["DISPLAY"] = os.environ["DISPLAY"]
        if os.environ.get("XAUTHORITY"):
            env["XAUTHORITY"] = os.environ["XAUTHORITY"]
    elif os.environ.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = os.environ["WAYLAND_DISPLAY"]
        if os.environ.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    server["env"] = env
with open(out, "w", encoding="utf-8", newline="\n") as fh:
    json.dump({"mcpServers": {"playwright": server}}, fh, indent=2)
    fh.write("\n")
PY
  then
    die "--browser: could not write the MCP config: $MCP_CONFIG"
    exit $QA_HARNESS
  fi
  unset PW_CMD PW_ARGS
  # The session must be able to Read its own evidence (a screenshot saved by
  # filename, the snapshot files): the folder joins the extra readable dirs. It is
  # added HERE, not up in the --browser setup, precisely because the --probe
  # refusals ran long above: the browser folder is never the user's tree, so the
  # -D/--add-dir refusal must not (and here cannot) see it.
  ADD_DIRS+=("$BROWSER_DIR")
  # die, not note: -q must not hide where the evidence of a browser run lands.
  die "browser: screenshots and page snapshots in $(native_path "$BROWSER_DIR")"
fi

# --------------------------------------------------- build the claude argv
if [ "$INTERACTIVE" -eq 1 ]; then
  # No -p/--print, and none of the flags a headless run needs a fence for: no
  # --output-format (the session draws itself), no --tools/--allowed-tools, no
  # --permission-mode and no --restricted (the person at the keyboard answers
  # Claude Code's own prompts), no --append-system-prompt and no
  # --strict-mcp-config. What DOES point claude at this server is passed exactly
  # as a headless run passes it: --model, --effort, the context window through
  # the environment, --autocompact, and --setting-sources when configured.
  CLAUDE_ARGV=("$CLAUDE_BIN" --model "$MODEL")
  [ "$EFFORT" = default ] || CLAUDE_ARGV+=(--effort "$EFFORT")
  [ -n "$SETTING_SOURCES" ] && CLAUDE_ARGV+=(--setting-sources "$SETTING_SOURCES")
  [ -n "$AUTOCOMPACT" ]     && CLAUDE_ARGV+=(--autocompact "$AUTOCOMPACT")
  if [ "${#ADD_DIRS[@]}" -gt 0 ]; then
    for d in "${ADD_DIRS[@]}"; do CLAUDE_ARGV+=(--add-dir "$(native_path "$d")"); done
  fi
else
  CLAUDE_ARGV=("$CLAUDE_BIN" -p
    --model "$MODEL"
    --output-format json
    --allowed-tools "$TOOLS")
  # --disallowedTools HIDES a tool from the model. A tool that is merely not granted
  # still reaches the model's tool list, where it gets tried and the run dies on the
  # refusal; the browser tools are collected into ONE comma-joined value above, so
  # this is one flag, ever. (The review round reuses it: it is part of the base argv.)
  [ -n "$DISALLOWED" ] && CLAUDE_ARGV+=(--disallowedTools "$DISALLOWED")
  [ "$EFFORT" = default ] || CLAUDE_ARGV+=(--effort "$EFFORT")
  # --test must not depend on any settings file, and --restricted ignores them
  # anyway -- passing the flag there would only advertise a door the fence shuts.
  if [ "$TEST_MODE" -eq 0 ] && [ "$PROBING" -eq 0 ] && [ -n "$SETTING_SOURCES" ]; then
    CLAUDE_ARGV+=(--setting-sources "$SETTING_SOURCES")
  fi
  [ -n "$AUTOCOMPACT" ]  && CLAUDE_ARGV+=(--autocompact "$AUTOCOMPACT")
  if [ -n "$TOOLSET" ]; then CLAUDE_ARGV+=(--tools "$TOOLSET")
  elif [ "$TOOLSET_NONE" -eq 1 ]; then CLAUDE_ARGV+=(--tools "")
  fi
  if [ -n "$MCP_CONFIG" ]; then
    if [ "$BROWSER" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
      # --browser's dry-run value is the "<browser run folder>/mcp.json" placeholder,
      # not a path yet (only a run that starts makes the folder): print it verbatim,
      # never converted to a native spelling by cygpath.
      CLAUDE_ARGV+=(--mcp-config "$MCP_CONFIG")
    else
      CLAUDE_ARGV+=(--mcp-config "$(native_path "$MCP_CONFIG")")
    fi
  fi
  [ "$STRICT_MCP" -eq 1 ] && CLAUDE_ARGV+=(--strict-mcp-config)
  [ -n "$SYSTEM" ]    && CLAUDE_ARGV+=(--append-system-prompt "$SYSTEM")
  [ -n "$PERM_MODE" ] && CLAUDE_ARGV+=(--permission-mode "$PERM_MODE")
  if [ "$TEST_MODE" -eq 1 ] || [ "$PROBING" -eq 1 ]; then CLAUDE_ARGV+=(--restricted); fi
  if [ "${#ADD_DIRS[@]}" -gt 0 ]; then
    for d in "${ADD_DIRS[@]}"; do CLAUDE_ARGV+=(--add-dir "$(native_path "$d")"); done
  fi
  CLAUDE_BASE_N=${#CLAUDE_ARGV[@]}     # the review round reuses everything before this
  [ -n "$RESUME_ID" ] && CLAUDE_ARGV+=(--resume "$RESUME_ID")
  # '--' guards a prompt that begins with '-'.
  CLAUDE_ARGV+=(-- "$PROMPT")
fi

# Wrap in GNU timeout when one was found; otherwise run_claude's watchdog
# enforces --timeout. Never pass 0 (see above). An interactive session is never
# wrapped: the person at the keyboard ends it, and killing their session on a
# wall clock would throw away the work they are typing.
if [ "$INTERACTIVE" -eq 0 ] && [ -n "$TIMEOUT" ] && [ -n "$TIMEOUT_BIN" ]; then
  RUN_ARGV=("$TIMEOUT_BIN" -k 10 "$TIMEOUT" "${CLAUDE_ARGV[@]}")
else
  RUN_ARGV=("${CLAUDE_ARGV[@]}")
fi

# Environment for the child. CLAUDE_CODE_MAX_CONTEXT_TOKENS only when the window
# is actually known: a guessed value is worse than claude's own default.
CHILD_ENV=(
  "ANTHROPIC_BASE_URL=$BASE"
  "ANTHROPIC_AUTH_TOKEN=${QWEN_API_KEY:-dummy}"
  "ANTHROPIC_MODEL=$MODEL"
  "ANTHROPIC_SMALL_FAST_MODEL=$MODEL"
  "ANTHROPIC_DEFAULT_HAIKU_MODEL=$MODEL"
  "ANTHROPIC_DEFAULT_SONNET_MODEL=$MODEL"
  "ANTHROPIC_DEFAULT_OPUS_MODEL=$MODEL"
  "CLAUDE_CODE_SUBAGENT_MODEL=$MODEL")
[ -n "$CTX" ] && CHILD_ENV+=("CLAUDE_CODE_MAX_CONTEXT_TOKENS=$CTX")
# Claude Code's internal model calls (WebFetch summarises pages with its own
# request) ignore --effort and send "high", which Qwen chat templates reject
# (400). The env var is what reaches those calls; the parent's value is scrubbed,
# so with QWEN_EFFORT=default (no --effort) nothing is set for the child.
[ "$EFFORT" = default ] || CHILD_ENV+=("CLAUDE_CODE_EFFORT_LEVEL=$EFFORT")
[ -n "${QWEN_CUSTOM_HEADERS:-}" ] && CHILD_ENV+=("ANTHROPIC_CUSTOM_HEADERS=$QWEN_CUSTOM_HEADERS")
if [ "$TEST_MODE" -eq 1 ]; then
  # The Bash tool must never cut qwen-test off before the test timeout does:
  # qwen-test itself kills the test's process group at QWEN_TEST_TIMEOUT (600
  # default), and gets a 60s margin on top for the worktree sync and teardown.
  _qt="${QWEN_TEST_TIMEOUT:-600}"
  case "$_qt" in ''|*[!0-9]*) _qt=600 ;; esac
  _bash_ms=$(( _qt * 1000 + 60000 ))
  CHILD_ENV+=("QWEN_TEST_CMD=$QWEN_TEST_CMD" "QWEN_TEST_SOURCE=$TEST_REPO" "QWEN_TEST_WORKTREE=${TEST_WT:-<worktree>}"
              "BASH_DEFAULT_TIMEOUT_MS=$_bash_ms" "BASH_MAX_TIMEOUT_MS=$_bash_ms")
  unset _qt _bash_ms
fi
# Git Bash rewrites any argument that looks like a POSIX path when it starts a
# native program: a prompt or a model id beginning with "/" would reach claude as
# C:/Program Files/Git/... Pass arguments verbatim (--add-dir is converted above).
command -v cygpath >/dev/null 2>&1 && CHILD_ENV+=("MSYS2_ARG_CONV_EXCL=*")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "# env"
  # Never print the key itself: dry-run output ends up in logs and bug reports.
  printf '  %s\n' "${CHILD_ENV[@]}" \
    | sed -e 's/^\(  ANTHROPIC_AUTH_TOKEN=\).*/\1<redacted>/' -e 's/^\(  ANTHROPIC_CUSTOM_HEADERS=\).*/\1<redacted>/'
  [ -n "$CTX" ] || echo "  (no CLAUDE_CODE_MAX_CONTEXT_TOKENS: window detected at run time, else claude's default)"
  echo "# autocompact: ${AUTOCOMPACT:-<not passed>}"
  echo "# unset for the child: $SCRUB_LIST"
  echo "# argv (one per line, exactly as exec'd)"
  printf '  [%s]\n' "${RUN_ARGV[@]}"
  echo "# python: $QA_PY"
  echo "# config: $QA_CONFIG$([ -r "$QA_CONFIG" ] || echo '  (absent)')"
  echo "# cwd: $PWD"
  echo "# out: ${OUT:-<stdout>}"
  [ "$PROBE" -eq 1 ] && echo "# probe: the sandbox is made at run time (QWEN_PROBE_DIR: ${QWEN_PROBE_DIR:-<default>})"
  # The review round is the second call a depth run makes after the session ends;
  # a dry run prints the first call's argv (delegation text and Task included -- they are
  # part of it) and notes the round it would have made.
  [ "$REVIEW_ROUND" -eq 1 ] && echo "# review round: one --resume call after the first"
  # --record is a second resume call after the scoring, plus scenarios.py replay-check
  # over what it wrote: a dry run prints neither (both need the run's browser folder).
  [ -n "$RECORD" ] && echo "# record: one --resume call with the record prompt after the"
  [ -n "$RECORD" ] && echo "#   suite is scored, then scenarios.py replay-check into $RECORD"
  if [ "$INTERACTIVE" -eq 1 ]; then
    echo "# timeout: not applied (--interactive: the session ends when the person at the keyboard leaves)"
  else
    echo "# timeout: ${TIMEOUT:-<none (--no-timeout)>}${TIMEOUT:+ via ${TIMEOUT_BIN:-built-in watchdog}}"
  fi
  exit 0
fi

if [ "$INTERACTIVE" -eq 1 ]; then
  # Replace this process rather than spawn and supervise one: from here the
  # terminal, Ctrl-C and the exit code belong to the session, and there is no
  # JSON record to parse, classify or write to a file. `env` still gets the
  # scrubbed parent session's control channel and provider routing.
  note "interactive session on '$MODEL' at $BASE — Claude Code's own prompts ask you about every tool; leave with /exit"
  # shellcheck disable=SC2086  # SCRUB_ARGS is a deliberate word list
  exec env $SCRUB_ARGS "${CHILD_ENV[@]}" "${RUN_ARGV[@]}"
fi

# ------------------------------------------------------------------- runner
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/qwen-agent.XXXXXXXX")" || { die "mktemp failed"; exit $QA_HARNESS; }

# claude runs as a BACKGROUND job and is joined with `wait`, because a trap
# cannot interrupt a foreground command: with `timeout ... claude` in the
# foreground the script absorbed SIGTERM and left an orphan tree behind.
# `wait` IS interruptible, so the handler below actually runs and actually
# kills the child.
CPID=""
# shellcheck disable=SC2329  # invoked via trap
on_signal() {
  local name="$1" num="$2"
  if [ -n "${CPID:-}" ]; then
    kill -TERM "$CPID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$CPID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$CPID" 2>/dev/null
  fi
  cleanup
  die "interrupted by SIG$name"
  trap - EXIT
  exit $((128 + num))
}
trap cleanup EXIT
trap 'on_signal INT 2' INT
trap 'on_signal TERM 15' TERM

RAW="$TMPD/raw.json"
ERRF="$TMPD/stderr.txt"

# Known-benign stderr chatter from Claude Code when pointed at a non-Anthropic
# endpoint. Filtered so real errors stand out.
NOISE='claude\.ai connectors|unrecognized_model|no stdin data received'

run_claude() {
  local rc wpid=""
  # env is scoped to this subprocess only; the parent session keeps its own auth.
  # The `env -u` list drops the PARENT Claude Code session's control channel:
  # the messaging socket+token is a live channel back into that session, and a
  # parent CLAUDE_EFFORT could override --effort with a level the local chat
  # template rejects.
  # shellcheck disable=SC2086  # SCRUB_ARGS is a deliberate word list
  env $SCRUB_ARGS "${CHILD_ENV[@]}" "${RUN_ARGV[@]}" </dev/null >"$RAW" 2>"$ERRF" &
  CPID=$!
  if [ -n "$TIMEOUT" ] && [ -z "$TIMEOUT_BIN" ]; then
    # Built-in watchdog (no GNU timeout here). Polls once a second, so it exits
    # promptly when claude finishes instead of lingering for the full TIMEOUT.
    (
      i=0
      while [ "$i" -lt "$TIMEOUT" ]; do
        sleep 1
        kill -0 "$CPID" 2>/dev/null || exit 0
        i=$((i + 1))
      done
      : >"$TMPD/timed_out"
      kill -TERM "$CPID" 2>/dev/null
      i=0
      while [ "$i" -lt 10 ] && kill -0 "$CPID" 2>/dev/null; do sleep 1; i=$((i + 1)); done
      kill -KILL "$CPID" 2>/dev/null
    ) &
    wpid=$!
  fi
  wait "$CPID"; rc=$?
  CPID=""
  if [ -n "$wpid" ]; then
    kill "$wpid" 2>/dev/null
    wait "$wpid" 2>/dev/null
  fi
  [ -f "$TMPD/timed_out" ] && rc=124
  return $rc
}

# Parse the JSON result record. Emits shell-safe KEY=value lines.
parse_raw() {
  "$QA_PY" - "$RAW" <<'PY'
import json, sys, shlex
path = sys.argv[1]
try:
    with open(path, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
except OSError:
    print("qa_parse=readfail"); sys.exit(0)
if not txt.strip():
    print("qa_parse=empty"); sys.exit(0)
# Tolerate leading chatter: use the last JSON object on its own line.
obj = None
for line in reversed(txt.strip().splitlines()):
    line = line.strip()
    if line.startswith("{"):
        try:
            obj = json.loads(line); break
        except Exception:
            continue
if obj is None:
    try:
        obj = json.loads(txt)
    except Exception:
        print("qa_parse=badjson"); sys.exit(0)
def q(v): return shlex.quote("" if v is None else str(v))
print("qa_parse=ok")
print("qa_is_error=" + q(bool(obj.get("is_error"))))
print("qa_api_status=" + q(obj.get("api_error_status")))
print("qa_terminal=" + q(obj.get("terminal_reason")))
print("qa_subtype=" + q(obj.get("subtype")))
print("qa_turns=" + q(obj.get("num_turns")))
print("qa_dur_ms=" + q(obj.get("duration_ms")))
print("qa_denials=" + q(len(obj.get("permission_denials") or [])))
den = obj.get("permission_denials") or []
print("qa_denied_tools=" + q(",".join(sorted({str(d.get("tool_name", "?")) for d in den}))))
u = obj.get("usage") or {}
print("qa_in_tok=" + q(u.get("input_tokens")))
print("qa_out_tok=" + q(u.get("output_tokens")))
res = obj.get("result")
print("qa_result_len=" + q(len(res) if isinstance(res, str) else 0))
print("qa_session=" + q(obj.get("session_id")))
PY
}

extract() {  # $1 = "text" | "json"
  "$QA_PY" - "$RAW" "$1" <<'PY'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8", errors="replace") as fh:
    txt = fh.read()
obj = None
for line in reversed(txt.strip().splitlines()):
    line = line.strip()
    if line.startswith("{"):
        try:
            obj = json.loads(line); break
        except Exception:
            continue
if obj is None:
    sys.stdout.write(txt); sys.exit(0)
if mode == "json":
    import os
    if os.environ.get("QA_META") == "1":
        e = os.environ
        meta = {"depth": e.get("QA_META_DEPTH") or None,
                "switches": {"probe": e.get("QA_META_PROBE") == "1",
                             "role_variant": e.get("QA_META_VARIANT") or None,
                             "review_round": e.get("QA_META_REVIEW") == "1",
                             "subagents_nudge": e.get("QA_META_NUDGE") == "1"}}
        # The push key appears only on push runs, so records of nudge-only and
        # switchless runs keep the exact shape they had before push existed.
        if e.get("QA_META_PUSH") == "1":
            meta["switches"]["subagents_push"] = True
        if e.get("QA_META_REVIEW") == "1":
            meta["review_round"] = {"status": e.get("QA_META_REVIEW_STATUS") or None,
                                    "first_session": e.get("QA_META_REVIEW_FIRST") or None,
                                    "warning": e.get("QA_META_REVIEW_WARNING") or None}
        if e.get("QA_META_SHORT") == "1":
            meta["short_answer_warning"] = True
        if e.get("QA_META_PROBE") == "1":
            meta["patch"] = e.get("QA_META_PATCH") or None
            meta["sandbox"] = e.get("QA_META_SANDBOX") or None
        if e.get("QA_META_BROWSER") == "1":
            meta["browser"] = {"dir": e.get("QA_META_BROWSER_DIR") or None,
                               "headed": e.get("QA_META_BROWSER_HEADED") == "1",
                               "eval": e.get("QA_META_BROWSER_EVAL") == "1"}
        if e.get("QA_META_SCEN_FILE"):
            def _count(v):
                v = (v or "").strip()
                return int(v) if v.isdigit() else 0
            meta["scenarios"] = {"file": e.get("QA_META_SCEN_FILE"),
                                 "results": e.get("QA_META_SCEN_RESULTS") or None,
                                 "pass": _count(e.get("QA_META_SCEN_PASS")),
                                 "fail": _count(e.get("QA_META_SCEN_FAIL")),
                                 "blocked": _count(e.get("QA_META_SCEN_BLOCKED"))}
        obj["qwen_agent"] = meta
    json.dump(obj, sys.stdout, indent=2); sys.stdout.write("\n")
else:
    r = obj.get("result")
    sys.stdout.write((r if isinstance(r, str) else json.dumps(obj)).strip() + "\n")
PY
}

# Returns one of the QA_* codes; leaves the payload in $RAW.
classify() {
  local rc="$1"
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    die "TIMEOUT after ${TIMEOUT}s — no result. Raise --timeout or shrink the task."
    return $QA_TIMEOUT
  fi

  local ev; ev="$(parse_raw | tr -d '\r')"
  # shellcheck disable=SC2046
  eval "$ev"
  qa_parse="${qa_parse:-badjson}"

  if [ "$qa_parse" != "ok" ]; then
    die "HARNESS FAILURE: could not parse claude output ($qa_parse), exit was $rc"
    grep -vE "$NOISE" "$ERRF" | sed 's/^/  claude-stderr: /' >&2
    [ -s "$RAW" ] && { die "first 400 bytes of raw output:"; head -c 400 "$RAW" >&2; echo >&2; }
    return $QA_HARNESS
  fi

  if [ "${qa_api_status:-}" != "" ] || [ "${qa_terminal:-}" = "api_error" ]; then
    die "API ERROR (status ${qa_api_status:-?}) from $BASE"
    die "$(extract text | head -5)"
    return $QA_APIERR
  fi

  if [ "${qa_is_error:-False}" = "True" ]; then
    die "RUN FAILED (terminal_reason=${qa_terminal:-?}, subtype=${qa_subtype:-?})"
    die "$(extract text | head -5)"
    return $QA_HARNESS
  fi

  if [ "${qa_denials:-0}" -gt 0 ]; then
    die "PERMISSION DENIED: ${qa_denials} tool call(s) blocked [${qa_denied_tools:-?}]"
    die "the result below was produced WITHOUT those tools — treat it as suspect"
    die "allowed-tools was: $TOOLS"
    [ -n "$TOOLSET" ] && die "toolset was: $TOOLSET"
    if [ "$WARN_DENIALS" -eq 0 ]; then
      return $QA_DENIED
    fi
  fi

  if [ "${qa_result_len:-0}" -eq 0 ]; then
    die "EMPTY RESULT: the run completed but produced no text (turns=${qa_turns:-?})"
    return $QA_EMPTY
  fi

  note "ok — turns=${qa_turns:-?} in=${qa_in_tok:-?} out=${qa_out_tok:-?} tok, ${qa_dur_ms:-?}ms${qa_session:+ session=$qa_session}"
  return $QA_OK
}

repro_section() {  # the '## REPRO FILES' block for the newline-separated files in $1
  printf '\n## REPRO FILES\n'
  printf '%s\n' "$1" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '\n### %s\n\n```\n' "$f"; cat -- "$TEST_WT/$f"; printf '```\n'
  done
}

# The switches this run used (depth switches, --browser), for the qwen_agent key of
# --json's record. Exported only when one is set, so a run without any emits Claude
# Code's record unchanged.
export_meta() {
  # The key explains the switches that changed the argv, says WHICH depth ran (tools
  # that read the record total by mode), and carries a suspiciously short final
  # answer's mark, so it is findable in the record, not only in stderr a caller may
  # not have kept.
  [ -n "$ROLE_VARIANT" ] || [ "$NUDGE" -eq 1 ] || [ "$PUSH" -eq 1 ] \
    || [ "$REVIEW_ROUND" -eq 1 ] || [ "$PROBING" -eq 1 ] \
    || [ "$BROWSER" -eq 1 ] || [ "$DEPTH_MODE" != shallow ] || [ "$SHORT_ANSWER_WARNING" -eq 1 ] \
    || [ -n "$SCENARIOS" ] || return 0
  local kept="" browser_dir_meta=""
  [ "$KEEP_SANDBOX" -eq 1 ] && kept="$PROBE_CWD"
  browser_dir_meta="$(native_path "$BROWSER_DIR")"
  export QA_META=1 QA_META_VARIANT="$ROLE_VARIANT" QA_META_NUDGE="$NUDGE" QA_META_PUSH="$PUSH" \
         QA_META_REVIEW="$REVIEW_ROUND" QA_META_REVIEW_STATUS="$REVIEW_STATUS" \
         QA_META_REVIEW_FIRST="$REVIEW_FIRST" QA_META_REVIEW_WARNING="$REVIEW_WARNING" \
         QA_META_PROBE="$PROBING" QA_META_PATCH="$PROBE_PATCH" QA_META_SANDBOX="$kept" \
         QA_META_BROWSER="$BROWSER" QA_META_BROWSER_DIR="$browser_dir_meta" \
         QA_META_BROWSER_HEADED="$HEADED" QA_META_BROWSER_EVAL="$BROWSER_EVAL" \
         QA_META_DEPTH="$DEPTH_MODE" QA_META_SHORT="$SHORT_ANSWER_WARNING" \
         QA_META_SCEN_FILE="$SCENARIOS_GIVEN" QA_META_SCEN_RESULTS="$SCEN_RESULTS" \
         QA_META_SCEN_PASS="$SCEN_PASS" QA_META_SCEN_FAIL="$SCEN_FAIL" \
         QA_META_SCEN_BLOCKED="$SCEN_BLOCKED"
}

# --probe --write: the session's edits as a patch -- FILE.patch next to -o FILE (always
# written, empty when nothing changed), else a new file in QWEN_OUTDIR (the probe
# directory when QWEN_OUTDIR was not set: see the OUTDIR block above -- never the
# probed tree; only when there is a change). Never applied. On failure the sandbox is
# kept so the work is not lost.
write_probe_patch() {
  local target
  if [ -n "$OUT" ]; then
    target="$OUT.patch"
  elif mkdir -p -- "$QWEN_OUTDIR" 2>/dev/null \
       && target="$(mktemp "$QWEN_OUTDIR/qwen-agent-XXXXXXXX")" && mv -- "$target" "$target.patch"; then
    target="$target.patch"
  else
    KEEP_SANDBOX=1
    die "--probe: cannot create a patch file in $QWEN_OUTDIR; the sandbox is kept: $PROBE_CWD"
    return 1
  fi
  if ! skill_py probe.py diff "$(native_path "$PROBE_SB")" "$(native_path "$target")"; then
    KEEP_SANDBOX=1
    die "--probe: could not write the patch to $target; the sandbox is kept: $PROBE_CWD"
    return 1
  fi
  if [ -s "$target" ]; then
    PROBE_PATCH="$target"
    # One spelling for both paths: under Git Bash -o's is the shell's /c/..., the
    # source git's C:/..., and a pasted line should not mix them.
    die "patch: $(mixed_path "$target") (not applied; to apply: git -C $(sq "$(mixed_path "$PROBE_SRC")") apply $(sq "$(mixed_path "$target")"))"
  elif [ -n "$OUT" ]; then
    PROBE_PATCH="$target"
    note "--probe: the session changed nothing (empty $target)"
  else
    rm -f -- "$target"
    note "--probe: the session changed nothing; no patch written"
  fi
  return 0
}

# --review-round --json: the standing record is the review call's, but the run paid
# for both calls -- the record tools total from must carry their SUM
# (usage per key, num_turns, total_cost_usd when either call has it). Rewrites $RAW
# through a temp file, so a parse that fails halfway leaves the payload intact.
sum_first_usage() {
  "$QA_PY" - "$RAW" "$RAW.first" <<'PY' || return 1
import json, os, sys
def last_json(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in reversed(fh.read().strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
    return None
def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)
rec, first = last_json(sys.argv[1]), last_json(sys.argv[2])
if rec is None or first is None:
    sys.exit(0)
u, fu = rec.get("usage"), first.get("usage")
if isinstance(u, dict) and isinstance(fu, dict):
    for k, v in fu.items():
        if num(v):
            u[k] = (u.get(k) if num(u.get(k)) else 0) + v
for key in ("num_turns", "total_cost_usd"):
    if num(first.get(key)):
        rec[key] = (rec.get(key) if num(rec.get(key)) else 0) + first[key]
tmp = sys.argv[1] + ".qa-sum"
try:
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
        fh.write("\n")
    os.replace(tmp, sys.argv[1])
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(1)
PY
}

REVIEW_STATUS=""
REVIEW_FIRST=""
REVIEW_WARNING=""
# "<result_len> <num_turns>" of the last JSON result record in $1 -- the record
# parse_raw reads. The two numbers the short-answer checks judge; no output at all
# when the file holds no readable record.
result_stats() {
  "$QA_PY" - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
except OSError:
    sys.exit(0)
for line in reversed(txt.strip().splitlines()):
    line = line.strip()
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        r = obj.get("result")
        n = obj.get("num_turns")
        sys.stdout.write("%d %d" % (len(r) if isinstance(r, str) else 0,
                                    n if isinstance(n, int) and not isinstance(n, bool) else 0))
        break
PY
}

# --review-round: resume the session that just ended with REVIEW_PROMPT. $1 is the first
# call's exit code; returns the code that stands. The first answer stands (its RAW is put
# back) when the first call did not succeed, carried no session id, the review call
# itself fails, or the review comes back a stub while the first answer was a full one.
review_round() {
  local first="$1" rc code first_len review_len
  if [ "$first" -ne 0 ]; then REVIEW_STATUS="skipped"; return "$first"; fi
  if [ -z "${qa_session:-}" ]; then
    REVIEW_STATUS="skipped"
    REVIEW_WARNING="no session id to resume; the first answer stands"
    die "WARNING: --review-round: $REVIEW_WARNING"
    return "$first"
  fi
  REVIEW_FIRST="$qa_session"
  mv -f "$RAW" "$RAW.first"
  # With --subagents-push the review prompt also hands re-verification to a subagent.
  local _rp="$REVIEW_PROMPT"
  [ "$PUSH" -eq 1 ] && _rp="$REVIEW_PROMPT$REVIEW_PUSH_TEXT"
  CLAUDE_ARGV=("${CLAUDE_ARGV[@]:0:$CLAUDE_BASE_N}" --resume "$REVIEW_FIRST" -- "$_rp")
  unset _rp
  if [ -n "$TIMEOUT" ] && [ -n "$TIMEOUT_BIN" ]; then
    RUN_ARGV=("$TIMEOUT_BIN" -k 10 "$TIMEOUT" "${CLAUDE_ARGV[@]}")
  else
    RUN_ARGV=("${CLAUDE_ARGV[@]}")
  fi
  rm -f "$TMPD/timed_out"
  run_claude; rc=$?
  classify "$rc"; code=$?
  if [ "$code" -eq 0 ]; then
    # A review that comes back a stub while the first answer was a full one is the
    # auto-compact failure signature, not a review: treat it as a failed round so
    # the first answer stands, as when the review call itself fails.
    first_len="$(result_stats "$RAW.first")"; first_len="${first_len%% *}"
    review_len="$(result_stats "$RAW")";     review_len="${review_len%% *}"
    if [ -n "$first_len" ] && [ "$first_len" -ge 1000 ] \
       && [ -n "$review_len" ] && [ "$review_len" -lt 300 ]; then
      REVIEW_STATUS="failed"
      REVIEW_WARNING="the review answer is suspiciously short ($review_len characters against $first_len in the first); the first answer stands"
      die "WARNING: --review-round: $REVIEW_WARNING"
      mv -f "$RAW.first" "$RAW"
      return "$first"
    fi
    sum_first_usage || die "WARNING: --review-round: could not sum the first call's usage into the record"
    REVIEW_STATUS="ok"; return 0
  fi
  REVIEW_STATUS="failed"
  REVIEW_WARNING="the review round failed (exit $code); the first answer stands"
  die "WARNING: --review-round: $REVIEW_WARNING"
  mv -f "$RAW.first" "$RAW"
  return "$first"
}

# The auto-compact death signature: a long session (many turns) whose final answer
# is a stub. The answer still stands, but the person who asked must see why it may
# not be the whole answer -- this exact line on stderr -- and --json must carry the
# mark for tools that read the record, not the terminal.
short_answer_guard() {
  local stats len turns
  stats="$(result_stats "$RAW")"
  [ -n "$stats" ] || return 0
  len="${stats%% *}"; turns="${stats##* }"
  if [ "$len" -lt 300 ] && [ "$turns" -gt 20 ]; then
    SHORT_ANSWER_WARNING=1
    printf 'WARNING: the answer is suspiciously short for a long session (possible auto-compact failure); check it\n' >&2
  fi
}

# --scenarios: score the FINAL answer against the suite -- $RAW here, so when a
# review round ran it is the round's answer that gets scored. The result list
# goes to <browser run folder>/results.json and the table to summary.md beside
# the screenshots (they are the evidence, and belong together); the table and
# 'results: PATH' print on stderr -- stdout and -o keep belonging to the answer.
# The run's own code wins when the run itself failed; otherwise 0 when every
# scenario PASSED, $QA_SCENARIO_FAIL when any is FAIL or BLOCKED (a scenario the
# answer never mentioned is BLOCKED, so a silent run cannot pass a suite).
scenario_score() {
  local run_code="$1" ans sum line t p="" f="" b=""
  if [ -z "$BROWSER_DIR" ]; then    # cannot happen: --scenarios implies --browser
    die "--scenarios: no browser run folder to write the results into"
    return "$run_code"
  fi
  ans="$(extract text)"
  if ! printf '%s' "$ans" > "$TMPD/scenario-answer.txt"; then
    die "--scenarios: cannot write the answer for scoring"
    return "$run_code"
  fi
  if ! sum="$(skill_py scenarios.py results "$(native_path "$SCENARIOS")" \
               "$(native_path "$TMPD/scenario-answer.txt")" \
               "$(native_path "$BROWSER_DIR/results.json")")"; then
    die "--scenarios: could not score the answer against $SCENARIOS_GIVEN"
    return "$run_code"
  fi
  printf '%s\n' "$sum" > "$BROWSER_DIR/summary.md" \
    || die "--scenarios: could not write $BROWSER_DIR/summary.md"
  die "$sum"
  die "results: $BROWSER_DIR/results.json"
  line="$(printf '%s\n' "$sum" | tail -n 1 | tr -d '\r')"   # native Python may end lines CRLF
  case "$line" in
    "PASS "*" / FAIL "*" / BLOCKED "*) : ;;
    *) die "--scenarios: could not read the counts off the summary"
       return "$run_code" ;;
  esac
  t="${line#PASS }";  p="${t%% / FAIL *}"
  t="${t#* / FAIL }"; f="${t%% / BLOCKED *}"
  t="${t#* / BLOCKED }"; b="$t"
  SCEN_RESULTS="$(native_path "$BROWSER_DIR/results.json")"
  SCEN_PASS="$p" SCEN_FAIL="$f" SCEN_BLOCKED="$b"
  [ "$run_code" -ne 0 ] && return "$run_code"
  if [ "$f" -gt 0 ] || [ "$b" -gt 0 ]; then return "$QA_SCENARIO_FAIL"; fi
  return 0
}

# --record: resume the session that ran the suite ONCE with RECORD_PROMPT so it turns
# each scenario into a deterministic script, then let scenarios.py replay-check decide
# which of those scripts reproduce the verdicts the run recorded. Only a session that
# ended cleanly is resumed (a failed run has verdicts worth nothing, and resuming a
# session that never got its answer is not recording); the record round is NOT the run's
# answer -- its answer is the list of files it wrote -- so $RAW is put back afterwards
# whatever it did: stdout, -o and the scored report keep belonging to the tester's
# report, and --json keeps the usage of the call that produced it, not of the recording.
# The write fence of this one call: Write inside <browser folder>/replay/ and nowhere
# else (the rule names that path, and the Write tool joins the TOOLSET of this call
# alone -- the run's own toolset never had it, and nothing else gains it).
record_scripts() {
  local rc code _i _dir _prompt _rec_tools _rec_toolset _rec_argv _out _line _errf _msg
  if [ -z "$BROWSER_DIR" ]; then          # cannot happen: --scenarios implies --browser
    die "--record: no browser run folder to write the replay scripts into"
    return 0
  fi
  if [ -z "${qa_session:-}" ]; then
    die "WARNING: --record: no session id to resume; no replay scripts were written"
  else
    _dir="$(native_path "$BROWSER_DIR")"
    _prompt="${RECORD_PROMPT//@FOLDER@/$_dir}"
    # Claude Code matches file-write permissions through Edit(...) path rules (they cover
    # every file-editing tool); a Write(...) path rule alone was not honoured in use, so
    # both are given, exactly as --test grants its worktree.
    _rec_tools="$TOOLS,Edit($(rule_path "$BROWSER_DIR/replay")/**),Write($(rule_path "$BROWSER_DIR/replay")/**)"
    case ",$TOOLS," in *,Write,*) _rec_tools="$TOOLS" ;; esac     # already write-enabled
    _rec_toolset="$TOOLSET"
    case ",$TOOLSET," in
      *,Write,*) : ;;
      *) _rec_toolset="${TOOLSET:+$TOOLSET,}Write" ;;
    esac
    mv -f "$RAW" "$RAW.record"
    # Same base argv as the review round (every flag before CLAUDE_BASE_N), with this
    # call's two tool-policy values swapped into it.
    _rec_argv=("${CLAUDE_ARGV[@]:0:$CLAUDE_BASE_N}")
    for _i in "${!_rec_argv[@]}"; do
      case "${_rec_argv[$_i]}" in
        --allowed-tools) _rec_argv[_i + 1]="$_rec_tools" ;;
        --tools)         _rec_argv[_i + 1]="$_rec_toolset" ;;
      esac
    done
    CLAUDE_ARGV=("${_rec_argv[@]}" --resume "$qa_session" -- "$_prompt")
    if [ -n "$TIMEOUT" ] && [ -n "$TIMEOUT_BIN" ]; then
      RUN_ARGV=("$TIMEOUT_BIN" -k 10 "$TIMEOUT" "${CLAUDE_ARGV[@]}")
    else
      RUN_ARGV=("${CLAUDE_ARGV[@]}")
    fi
    rm -f "$TMPD/timed_out"
    run_claude; rc=$?
    classify "$rc"; code=$?
    [ "$code" -eq 0 ] || die "WARNING: --record: the record round failed (exit $code);"
    [ "$code" -eq 0 ] || die "the scripts it wrote are validated as they stand"
    mv -f "$RAW.record" "$RAW"
  fi
  # Whatever exists is validated -- nothing, when the round could not run at all: the
  # manifest then keeps no scripts and says why per scenario, which is the honest
  # record of a run that recorded nothing.
  _errf="$TMPD/record-err"
  # die, not note: these are model-written programs about to run as the user.
  die "--record: running the scripts the session wrote in $(native_path "$BROWSER_DIR/replay") with node, as you, unsandboxed"
  _out="$(skill_py scenarios.py replay-check "$(native_path "$SCENARIOS")" \
            "$(native_path "$BROWSER_DIR/results.json")" \
            "$(native_path "$BROWSER_DIR/replay")" \
            "$(native_path "$RECORD")" 2>"$_errf")"; rc=$?
  _msg="$(cat "$_errf" 2>/dev/null)"; rm -f "$_errf"
  if [ "$rc" -ne 0 ]; then
    [ -z "$_msg" ] || die "--record: ${_msg#scenarios.py: }"
    die "--record: could not validate the replay scripts; nothing was recorded"
    return 0
  fi
  while IFS= read -r _line; do
    [ -n "$_line" ] && die "$_line"
  done <<< "$_out"
  return 0
}

emit() {
  local rc code fmt _run_code
  run_claude; rc=$?
  classify "$rc"; code=$?
  if [ "$REVIEW_ROUND" -eq 1 ]; then review_round "$code"; code=$?; fi
  short_answer_guard
  if [ "$PROBE" -eq 1 ] && [ "$WRITE_MODE" -eq 1 ] && [ -n "$PROBE_SB" ]; then
    write_probe_patch || { [ "$code" -eq 0 ] && code=$QA_HARNESS; }
  fi
  if [ "$PROBE" -eq 1 ] && [ "$KEEP_SANDBOX" -eq 1 ] && [ -n "$PROBE_RUN" ]; then
    die "sandbox kept: $PROBE_CWD (remove it with: rm -rf $(sq "$PROBE_RUN"))"
  fi
  if [ -n "$SCENARIOS" ]; then
    _run_code="$code"                     # the run's own code, before the verdicts
    scenario_score "$code"; code=$?
    if [ -n "$RECORD" ]; then
      # A FAIL verdict does not stop the recording: a scenario that failed and replays
      # as FAILED is exactly the deterministic reproduction a later run needs, and a
      # BLOCKED scenario is rejected by name at validation. What stops it is the run
      # itself having failed -- no clean session to resume, no verdicts to reproduce.
      if [ "$_run_code" -eq 0 ]; then
        record_scripts
      else
        die "WARNING: --record: the run did not finish cleanly; nothing was recorded"
      fi
    fi
  fi
  export_meta

  fmt="text"; [ "$JSON_OUT" -eq 1 ] && fmt="json"

  # Always write whatever payload exists, even on failure — a partial or errored
  # result is still evidence, and silently losing it is the worst outcome.
  # Exception: in text mode on QA_EMPTY there is no text, and emitting it would
  # put a bare newline on stdout, so `r=$(qwen-agent ...)` would get whitespace
  # not "". With --json the record is always emitted: it carries the session id,
  # which a caller needs to resume a round that edited files but ended silent.
  if [ -s "$RAW" ] && { [ "$code" -ne "$QA_EMPTY" ] || [ "$fmt" = json ]; }; then
    if [ -n "$OUT" ]; then
      if extract "$fmt" >"$OUT"; then
        [ "$code" -eq 0 ] && note "wrote $OUT"
      else
        # The run is already paid for. Never discard the payload.
        die "cannot write $OUT — the result follows on stdout instead"
        extract "$fmt"
        [ "$code" -eq 0 ] && code=$QA_HARNESS
      fi
    else
      extract "$fmt"
    fi
  fi
  if [ "$TEST_MODE" -eq 1 ] && [ "$WRITE_MODE" -eq 0 ] && [ "$JSON_OUT" -eq 0 ] && [ -n "$TEST_WT" ] && [ "$PROBING" -eq 0 ]; then
    _rf="$(skill_py testrun.py --changed "$(native_path "$TEST_REPO")" "$(native_path "$TEST_WT")" | tr -d '\r')"
    if [ -n "$_rf" ]; then
      # Straight to stdout when there is no -o file: Git Bash on Windows has no
      # /dev/stdout to append to, and the whole section was silently lost there.
      if [ -n "$OUT" ]; then repro_section "$_rf" >>"$OUT"; else repro_section "$_rf"; fi
    fi
    unset _rf
  fi
  return $code
}

# ------------------------------------------------------------------- detach
if [ "$BG" -eq 1 ]; then
  if [ -z "$OUT" ]; then
    # BSD mktemp has no -p and wants the X's last, so create, then rename.
    if _tmpo="$(mktemp "${QWEN_OUTDIR:-$PWD}/qwen-agent-XXXXXXXX")" && mv -- "$_tmpo" "$_tmpo.out"; then
      OUT="$_tmpo.out"; unset _tmpo
    else
      die "cannot create an output file in ${QWEN_OUTDIR:-$PWD}"; exit $QA_HARNESS
    fi
  else
    # Two detached jobs sharing one -o silently clobber each other's sidecars.
    if [ "$FORCE" -eq 0 ]; then
      for f in "$OUT" "$OUT.status" "$OUT.err"; do
        [ -e "$f" ] && {
          die "$f already exists — another detached job may own it."
          die "pass --force to overwrite, or omit -o for a unique name"
          exit $QA_USAGE; }
      done
    fi
  fi
  : >"$OUT.err" || { die "cannot write $OUT.err"; exit $QA_HARNESS; }
  : >"$OUT.status"
  # The .out is 0600 from mktemp; .err echoes model output and error text, so
  # keep the sidecars just as private.
  chmod 600 "$OUT.err" "$OUT.status" 2>/dev/null

  # Detached child writes its own status sidecar, so a background failure is
  # never silent.
  (
    trap '' HUP
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    code=0
    emit >/dev/null 2>>"$OUT.err" || code=$?
    {
      echo "exit=$code"
      case "$code" in
        0) echo "reason=success" ;;
        "$QA_APIERR")    echo "reason=api_error" ;;
        "$QA_TIMEOUT")   echo "reason=timeout" ;;
        "$QA_EMPTY")     echo "reason=empty_result" ;;
        "$QA_DENIED")    echo "reason=permission_denied" ;;
        "$QA_HARNESS")   echo "reason=harness_failure" ;;
        "$QA_PREFLIGHT") echo "reason=preflight" ;;
        "$QA_SCENARIO_FAIL") echo "reason=scenario_fail" ;;
        *) echo "reason=unknown" ;;
      esac
      echo "started=$started"
      echo "finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "out=$OUT"
      echo "model=$MODEL"
    } >"$OUT.status"
    # The parent released TMPD to us; subshells do not inherit its EXIT trap.
    rm -rf "$TMPD"
  ) </dev/null >/dev/null 2>&1 &
  # ^ the job must not hold the caller's stdout/stderr open, or `r=$(qwen-agent -w ...)`
  #   and pipelines would block until the job finished, defeating -w.
  child=$!
  command -v disown >/dev/null 2>&1 && disown "$child" 2>/dev/null
  cat <<EOF
$QA_SELF: detached pid $child
  result : $OUT
  stderr : $OUT.err
  status : $OUT.status   (contains 'exit=' and 'reason=' when finished)
EOF
  trap - EXIT   # the child owns TMPD now
  exit 0
fi

emit
exit $?
