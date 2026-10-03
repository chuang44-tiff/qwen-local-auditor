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
READ_ONLY_FLAG=0    # --read-only given (refused with --test)
WRITE_MODE=0       # --write     : allow Edit/Write
ALL_TOOLS=0         # --all-tools : no restriction at all (dangerous)
WEB_MODE=0          # --web       : web access is opt-in (adds WebFetch only, never WebSearch)
case "${QWEN_WEB:-}" in 1) WEB_MODE=1 ;; esac
SUBAGENTS=0         # --subagents : opt-in Task tool (each subagent is one more concurrent request)
case "${QWEN_SUBAGENTS:-}" in 1) SUBAGENTS=1 ;; esac
STRICT_MCP=0
STRICT_MCP_EXPLICIT=0
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
  -r, --role NAME      Prepend a role. Built-ins: auditor, coder, mechanic, plain.
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

EXECUTION  (the DEFAULT is read-only — mutation must be asked for)
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
                       (the ONLY shell command granted). The test command is
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
      --subagents      Subagents are opt-in: adds the Task tool, so the model can
                       hand broad reading and searching to a subagent and keep
                       its own context small. A subagent runs on the same model
                       with the same tool limits, and is one more concurrent
                       request: leave it off on a small GPU. Off by default.
                       (env QWEN_SUBAGENTS=1)
      --toolset LIST   Passed to claude as --tools — the REAL restriction: it
                       removes every built-in tool you do not name. Overrides
                       --write/--all-tools. Default: '$TOOLSET_READONLY'.
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
      --permission-mode M   Passed through to claude (e.g. acceptEdits, plan).
  -C, --cd DIR         chdir here before running (tool access is rooted at cwd).
  -D, --add-dir DIR    Extra readable directory. Repeatable.
      --timeout SECS   Wall clock limit, default $TIMEOUT.  (env QWEN_TIMEOUT)
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

ENVIRONMENT  (also settable in the config file; flags win)
  QWEN_BASE_URL, QWEN_MODEL, QWEN_CTX, QWEN_AUTOCOMPACT, QWEN_EFFORT, QWEN_TIMEOUT
                       Defaults for the flags above.
  QWEN_API_KEY         Sent as the auth token, and on the preflight request.
  QWEN_CUSTOM_HEADERS  Passed to claude as ANTHROPIC_CUSTOM_HEADERS (gateways).
  QWEN_EFFORT_ALLOWED  Effort levels the server accepts; others are refused up
                       front. QWEN_EFFORT=default omits --effort entirely.
                       With --effort (or QWEN_EFFORT) the level is also set in
                       the child environment as CLAUDE_CODE_EFFORT_LEVEL, so
                       Claude Code's own internal model calls use it too.
  QWEN_WEB=1           Same as --web: adds WebFetch. Off by default.
  QWEN_SUBAGENTS=1     Same as --subagents: adds Task. Off by default.
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
  QWEN_OUTDIR          Where -w puts generated output files. Default: cwd.
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

SAFETY
  A bare run is read-only: --tools 'Read,Glob,Grep' --strict-mcp-config, which
  is a schema-level restriction (the model has no Bash and no Write tool at
  all). File mutation requires --write, --all-tools, or an explicit --toolset
  naming Edit/Write/Bash. --allowed-tools alone never restricts anything.
  --test adds Bash for qwen-test only; the tests it runs are arbitrary code.
  No run gets web tools unless you pass --web: web access is opt-in, and --web
  adds only WebFetch (never WebSearch, which local servers reject anyway).
EOF
}

die()  { printf '%s: %s\n' "$QA_SELF" "$*" >&2; }
note() { [ "$QUIET" -eq 1 ] || printf '%s: %s\n' "$QA_SELF" "$*" >&2; }
# Git Bash: argument conversion is switched off for the child (see CHILD_ENV), so
# a path that must reach a native program is converted explicitly. No-op elsewhere.
native_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
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
    *) return 1 ;;
  esac
}

list_roles() {
  echo "built-in: auditor, coder, mechanic, plain"
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
    --toolset)            need_arg "$1" $(($#-1)); TOOLSET="$2"; TOOLSET_EXPLICIT=1; shift 2 ;;
    --read-only)          TOOLSET="$TOOLSET_READONLY"; TOOLSET_EXPLICIT=1; READ_ONLY_FLAG=1
                          STRICT_MCP=1; STRICT_MCP_EXPLICIT=1; shift ;;
    --write)              WRITE_MODE=1; shift ;;
    --web)                WEB_MODE=1; shift ;;
    --subagents)          SUBAGENTS=1; shift ;;
    --interactive)        INTERACTIVE=1; shift ;;
    --test)               TEST_MODE=1; shift ;;
    --test-repo)          need_arg "$1" $(($#-1)); TEST_REPO="$2"; shift 2 ;;
    --all-tools|--unrestricted) ALL_TOOLS=1; shift ;;
    --strict-mcp)         STRICT_MCP=1; STRICT_MCP_EXPLICIT=1; shift ;;
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
    --timeout)            need_arg "$1" $(($#-1)); TIMEOUT="$2"; shift 2 ;;
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
    esac
  done
  unset _a
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
  if [ "$WRITE_MODE" -eq 1 ]; then TOOLS="$GRANTS_WRITE"; else TOOLS="$GRANTS_DEFAULT"; fi
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
elif [ "$WRITE_MODE" -eq 1 ] && [ -z "$PERM_MODE" ]; then
  PERM_MODE="acceptEdits"
fi
# Warn on stderr whenever this run can mutate anything. Deliberately uses die()
# (not note()) so -q cannot hide it.
if [ "$ALL_TOOLS" -eq 1 ]; then
  die "WARNING: --all-tools — every built-in tool is available, including Bash and Write"
elif [ "$WRITE_MODE" -eq 1 ]; then
  die "WARNING: write-enabled run (toolset '$TOOLSET', permission-mode '$PERM_MODE')"
elif [ "$TOOLSET_EXPLICIT" -eq 1 ]; then
  case ",$TOOLSET," in
    *,Bash,*|*,Write,*|*,Edit,*|*,MultiEdit,*|*,NotebookEdit,*)
      die "WARNING: --toolset '$TOOLSET' can modify files" ;;
  esac
fi

# -o and QWEN_OUTDIR are relative to where the CALLER is (like -f), never to --cd:
# output must not land inside the directory under audit by accident.
case "$OUT" in ''|/*|[A-Za-z]:*) ;; *) OUT="$PWD/$OUT" ;; esac
QWEN_OUTDIR="${QWEN_OUTDIR:-$PWD}"
case "$QWEN_OUTDIR" in /*|[A-Za-z]:*) ;; *) QWEN_OUTDIR="$PWD/$QWEN_OUTDIR" ;; esac

# ------------------------------------------------------------- working dir
if [ -n "$WORKDIR" ]; then
  [ -d "$WORKDIR" ] || { die "--cd: not a directory: $WORKDIR"; exit $QA_USAGE; }
  cd -- "$WORKDIR" || { die "--cd failed: $WORKDIR"; exit $QA_USAGE; }
fi

# shellcheck disable=SC2329  # invoked via trap
cleanup() {
  [ -n "${TMPD:-}" ] && rm -rf "$TMPD"
  [ -n "${TEST_WT:-}" ] && skill_py testrun.py --cleanup "$(native_path "$TEST_REPO")" "$(native_path "$TEST_WT")" >/dev/null 2>&1
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
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_fence_note"; else SYSTEM="$_fence_note"; fi
  unset _fence_note
fi
if [ "$TEST_MODE" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  _errf="${TMPDIR:-/tmp}/qwen-test-err.$$"
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
  # Read-only runs may write ONLY inside the worktree, so it must be readable.
  if [ "$WRITE_MODE" -eq 0 ] && [ -n "$TEST_WT" ]; then ADD_DIRS+=("$TEST_WT"); fi
  # die, not note: -q must not hide that a "read-only" run can now run code.
  [ "$WRITE_MODE" -eq 0 ] && die "WARNING: --test: this read-only run gains Bash (qwen-test only, which runs the repo's tests) and may write inside its throwaway worktree ${TEST_WT:-<worktree>}"
fi

# Web access is opt-in (--web / QWEN_WEB=1): nothing above ever names WebFetch,
# so by default no role, no --write run and no --test run reaches the web at all.
# --web adds ONLY WebFetch -- never WebSearch, which is a server-side tool that
# local servers (vLLM) reject with a 400 "body.tools.0.input_schema Field
# required" (search needs an MCP server). --all-tools leaves the toolset
# unrestricted and already has every built-in: it keeps that behaviour.
if [ "$WEB_MODE" -eq 1 ] && [ "$ALL_TOOLS" -eq 0 ]; then
  if [ -n "$TOOLSET" ]; then
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
    # Raw printf, not die(): the spec fixes this line to START with
    # "WARNING: --web with --test", and die() would prefix the program name.
    # Unconditional (not note()) so -q cannot hide that the run can fetch the
    # very answers its tests and checks are supposed to derive (seen in the
    # benchmark).
    printf 'WARNING: --web with --test: tests and checks can be gamed by fetching upstream answers (WebFetch is enabled)\n' >&2
  fi
fi

# Subagents are opt-in (--subagents / QWEN_SUBAGENTS=1): each one is another
# concurrent request against the same server, too much for a small GPU. A
# subagent inherits this run's tool restrictions and grants (and --restricted
# under --test), so Task adds no capability the run did not already have.
if [ "$SUBAGENTS" -eq 1 ] && [ "$ALL_TOOLS" -eq 0 ]; then
  if [ -n "$TOOLSET" ]; then
    case ",$TOOLSET," in *,Task,*) ;; *) TOOLSET="${TOOLSET:+$TOOLSET,}Task" ;; esac
  fi
  case ",$TOOLS," in *,Task,*) ;; *) TOOLS="${TOOLS:+$TOOLS,}Task" ;; esac
  _sub_note='You can delegate to a subagent with the Task tool; it runs on the same local model with the same tool limits as you. Your context window is limited: delegate broad reading and searching (for example "find every caller of X and report file:line") and keep your own context for edits and test runs. Do not delegate edits. Run one subagent at a time: each is another request the server must serve.'
  if [ -n "$SYSTEM" ]; then SYSTEM="$SYSTEM

$_sub_note"; else SYSTEM="$_sub_note"; fi
  unset _sub_note
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
  [ "$EFFORT" = default ] || CLAUDE_ARGV+=(--effort "$EFFORT")
  # --test must not depend on any settings file, and --restricted ignores them
  # anyway -- passing the flag there would only advertise a door the fence shuts.
  if [ "$TEST_MODE" -eq 0 ] && [ -n "$SETTING_SOURCES" ]; then
    CLAUDE_ARGV+=(--setting-sources "$SETTING_SOURCES")
  fi
  [ -n "$AUTOCOMPACT" ]  && CLAUDE_ARGV+=(--autocompact "$AUTOCOMPACT")
  [ -n "$TOOLSET" ]      && CLAUDE_ARGV+=(--tools "$TOOLSET")
  [ "$STRICT_MCP" -eq 1 ] && CLAUDE_ARGV+=(--strict-mcp-config)
  [ -n "$SYSTEM" ]    && CLAUDE_ARGV+=(--append-system-prompt "$SYSTEM")
  [ -n "$PERM_MODE" ] && CLAUDE_ARGV+=(--permission-mode "$PERM_MODE")
  [ "$TEST_MODE" -eq 1 ] && CLAUDE_ARGV+=(--restricted)
  if [ "${#ADD_DIRS[@]}" -gt 0 ]; then
    for d in "${ADD_DIRS[@]}"; do CLAUDE_ARGV+=(--add-dir "$(native_path "$d")"); done
  fi
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

emit() {
  local rc code fmt
  run_claude; rc=$?
  classify "$rc"; code=$?

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
  if [ "$TEST_MODE" -eq 1 ] && [ "$WRITE_MODE" -eq 0 ] && [ "$JSON_OUT" -eq 0 ] && [ -n "$TEST_WT" ]; then
    _rf="$(skill_py testrun.py --changed "$(native_path "$TEST_REPO")" "$(native_path "$TEST_WT")" | tr -d '\r')"
    if [ -n "$_rf" ]; then
      {
        printf '\n## REPRO FILES\n'
        printf '%s\n' "$_rf" | while IFS= read -r f; do
          [ -n "$f" ] || continue
          printf '\n### %s\n\n```\n' "$f"; cat -- "$TEST_WT/$f"; printf '```\n'
        done
      } >>"${OUT:-/dev/stdout}"
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
  # never silent (the previous version discarded background errors entirely).
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
