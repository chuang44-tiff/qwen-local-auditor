#!/usr/bin/env bash
# qwen-swarm -- run a workflow (workflow.json + workflow.py + role files) on a swarm of
# local-model Claude Code sessions. lib/swarm_engine/runner.py owns the run; every unit
# of work is a qwen-agent session. This wrapper owns the bash-side setup: the skill
# directory behind the install.sh symlink, the machine config, the interpreter, the env
# export. qwen-deep-research is this script with --as-deep-research first.
#
# Run `qwen-swarm --help` for usage. Exit codes are documented there.
set -u

_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SKILL_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
unset _src _dir
# Versioned with qwen-agent: every worker is a qwen-agent session. Plain BRE for BSD sed.
SW_VERSION="$(sed -n 's/^QA_VERSION="\(.*\)".*/\1/p' "$SKILL_DIR/qwen-agent.sh")"

usage() {
  cat <<'QSW_HELP'
qwen-swarm — run a workflow on a swarm of local-model Claude Code sessions. A workflow
is a folder: workflow.json (the manifest: roles and their fences, knobs, depth presets),
workflow.py (run(wf): the calls to make) and the role files. The report is written into
a run folder: on exit 0 and 4 the last line of stdout is the report's path, on exit 5
the run folder's path.

USAGE
  qwen-swarm WORKFLOW "goal" [options]     WORKFLOW = a built-in name or a folder path
  qwen-swarm WORKFLOW --stdin [options]    the goal from stdin
  qwen-swarm --resume RUN_DIR [options]    continue an interrupted run
  qwen-swarm --check WORKFLOW              validate the manifest, dry-run run(wf) twice
                                           against fake agents (no agent starts)
  qwen-swarm --record-verdict RUN --id ID --verdict CONFIRMED|FALSE_ALARM|NEEDS_HUMAN --evidence TEXT [--evidence TEXT ...]
             the main session's verdict on one row of a run (--evidence is repeatable)
  qwen-swarm --preflight [WORKFLOW]        model (and search, if the workflow uses it)
  qwen-swarm --list                        built-in workflows with descriptions

BUILT-IN WORKFLOWS
  research   a cited report on a question (qwen-deep-research is this workflow)
  debug      root cause and a checked patch for a bug: --target REPO, --set repro=CMD
  ui-test    a scripted UI suite run as a swarm: one browser agent per scenario, the
             scenario file is --set scenarios=PATH (the one qwen-agent --scenarios takes)

FLAGS
  --depth NAME         a preset of the workflow (qwen-swarm --list; default: its own)
  --set KNOB=VALUE     override one knob (repeatable); also budget, retries, rounds, hours
  --target DIR         the codebase, for workflows that need one (debug). Agents that can
                       edit or run commands work only in throwaway copies of it
  --max-agents N       most agents one phase starts (default 8; env QWEN_SWARM_MAX_AGENTS)
  --max-items N        most items one agent holds (default 10; env QWEN_SWARM_MAX_ITEMS)
  --seats N            agents running at once (default 4; env QWEN_SWARM_SEATS)
  --web-seats N        web agents running at once (default --seats; env QWEN_SWARM_WEB_SEATS)
  --timeout SECONDS    per-item budget (default: the preset's budget; env QWEN_SWARM_TIMEOUT)
  --retries N          re-runs of a unit that timed out or answered unusably (default:
                       the preset's; env QWEN_SWARM_RETRIES)
  --rounds N|until     rounds of a multi-round workflow (default: the preset's); until
                       needs --hours
  --hours H            hard deadline for the whole run (env QWEN_SWARM_HOURS)
  --effort LEVEL       reasoning effort for every role
  --role-effort ROLE=LEVEL[,ROLE=LEVEL...]   effort for single roles
  --deep ROLE[,ROLE...]|all   force deeper agents on these roles: each gets a review
                       round and a delegation nudge (qwen-agent --review-round
                       --subagents-nudge); cached apart from the plain answers
  --shallow ROLE[,ROLE...]|all   opt these roles out of depth, which is the default —
                       they answer once, with no review round and no nudge. The opt-out
                       for a role with high fan-out; --shallow all makes the whole run
                       shallow. A role listed in both is shallow
  --out DIR            the run folder (default swarm/<workflow>/<UTC stamp>-<slug>)
  --keep-sandboxes     leave the agents' sandbox copies in RUN/sandboxes for inspection
  -h, --help           this text
  --version            print the version

  On --resume only --seats, --web-seats, --timeout, --retries, --rounds, --hours,
  --effort, --role-effort, --deep, --shallow and --keep-sandboxes may be given. The
  research workflow also reads the QWEN_DR_* names of these variables (QWEN_SWARM_* wins).

EXIT CODES
  0    report written; the last line of stdout is the report's path
  2    usage or manifest error (the message names the field), or a failed --check
  3    preflight failed — the message says model or search
  4    report written, but agents were dropped, the deadline left items unrun, or the
       workflow finished without its goal (debug: no winning patch)
  5    nothing usable (no report); the last line of stdout is the run folder's path
  8    internal error (also an exception in workflow.py): RUN/error.log has the traceback
  130  interrupted (Ctrl-C); continue with --resume RUN_DIR

Writing a workflow: skill/local-auditor/reference/swarm.md.
QSW_HELP
}

PROG="qwen-swarm"
_compat=()
if [ "${1:-}" = "--as-deep-research" ]; then
  # qwen-deep-research execs this script; it has already answered --help and --version
  PROG="qwen-deep-research"
  _compat=(--compat deep-research)
  shift
else
  # Help and version answer only as the FIRST argument; anywhere else they reach the
  # runner, whose parser reports a usage error.
  case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --version) echo "qwen-swarm $SW_VERSION"; exit 0 ;;
  esac
fi

_cfg="${QWEN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/config}"
if [ -f "$_cfg" ] && [ -r "$_cfg" ]; then
  eval "$(tr -d '\r' < "$_cfg")"
fi
unset _cfg
# The runner reads these from the ENVIRONMENT; eval set shell variables only.
for _p in QWEN_SWARM_ QWEN_DR_; do
  for _n in MAX_AGENTS MAX_ITEMS SEATS WEB_SEATS TIMEOUT RETRIES HOURS MAX_UNIT_SECONDS \
            BACKOFF SKIP_SEARCH_CHECK; do
    _v="$_p$_n"
    eval "[ -n \"\${$_v:-}\" ] && export $_v"
  done
done
# QWEN_PLAYWRIGHT_MCP, QWEN_CLAUDE_BIN and QWEN_EXEC_RETRY_BACKOFF: wf.claude_check runs
# claude (and builds its browser's MCP config) inside the runner itself, not in a
# qwen-agent child that would read this file on its own. The backoff goes out whenever it
# is SET, even when blank: a blank value means "no retry" (as qwen-agent.sh reads it), and
# dropping it here would hand the runner the default of "10 30" instead.
for _v in QWEN_SEARCH_BACKEND QWEN_SEARCH_URL QWEN_SEARCH_KEY QWEN_SEARCH_BRAVE_URL \
          QWEN_PLAYWRIGHT_MCP QWEN_CLAUDE_BIN; do
  eval "[ -n \"\${$_v:-}\" ] && export $_v"
done
[ -n "${QWEN_EXEC_RETRY_BACKOFF+x}" ] && export QWEN_EXEC_RETRY_BACKOFF
unset _p _n _v
export PYTHONUTF8=1

PY=""
for _c in "${QWEN_PYTHON:-}" python3 python; do
  [ -n "$_c" ] || continue
  command -v "$_c" >/dev/null 2>&1 || continue
  "$_c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || continue
  PY="$_c"
  break
done
unset _c
[ -n "$PY" ] || { echo "$PROG: no working Python 3.8+ found (set QWEN_PYTHON)" >&2; exit 2; }

PY_NATIVE_WIN=0
if command -v cygpath >/dev/null 2>&1 \
   && [ "$("$PY" -c 'import os; print(os.sep)' | tr -d '\r')" = "\\" ]; then
  PY_NATIVE_WIN=1
fi
native_path() { if [ "$PY_NATIVE_WIN" -eq 1 ]; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# run_cmd (the debug workflow's checks) runs `bash -c CMD` in a sandbox: this bash, not
# whatever a native Windows python would find first (WSL's bash.exe in System32).
QWEN_SWARM_BASH="$(native_path "${BASH:-bash}")"
export QWEN_SWARM_BASH

_override="${QWEN_SWARM_AGENT_OVERRIDE:-${QWEN_DR_AGENT_OVERRIDE:-}}"
if [ -n "$_override" ]; then
  # test hook: a stand-in agent command (split on spaces)
  # shellcheck disable=SC2206
  _agent=($_override)
  _args=()
  for _a in "${_agent[@]}"; do _args+=(--agent "$_a"); done
else
  _args=(--agent "$(native_path "${BASH:-bash}")" --agent "$(native_path "$SKILL_DIR/qwen-agent.sh")")
fi
unset _override
PYTHONPATH="$(native_path "$SKILL_DIR")" exec "$PY" "$(native_path "$SKILL_DIR/lib/swarm_engine/runner.py")" \
  ${_compat[@]+"${_compat[@]}"} "${_args[@]}" "$@"
