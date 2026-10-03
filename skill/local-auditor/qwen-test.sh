#!/usr/bin/env bash
# qwen-test -- run the configured test command (QWEN_TEST_CMD) in a throwaway git
# worktree. The only shell command a local agent is granted; see lib/testrun.py.
set -u
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SKILL_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
unset _src _dir

_cfg="${QWEN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/config}"
if [ -f "$_cfg" ] && [ -r "$_cfg" ]; then
  eval "$(tr -d '\r' < "$_cfg")"
fi
unset _cfg
# testrun.py reads these from the ENVIRONMENT; eval set shell variables only.
# Export whatever the config defined (guard unset ones for set -u).
[ -n "${QWEN_TEST_CMD:-}" ] && export QWEN_TEST_CMD
[ -n "${QWEN_TEST_TIMEOUT:-}" ] && export QWEN_TEST_TIMEOUT
[ -n "${QWEN_TEST_MAX_BYTES:-}" ] && export QWEN_TEST_MAX_BYTES
[ -n "${QWEN_TEST_WORKTREES:-}" ] && export QWEN_TEST_WORKTREES
[ -n "${QWEN_AGENT_STATE:-}" ] && export QWEN_AGENT_STATE
# The test command must not inherit the wrapper's Python settings.
export QWEN_TEST_ORIG_PYTHONPATH="${PYTHONPATH-__unset__}"
export QWEN_TEST_ORIG_PYTHONUTF8="${PYTHONUTF8-__unset__}"
export PYTHONUTF8=1
export QWEN_TEST_CMD="${QWEN_TEST_CMD:-}"

PY=""
for _c in "${QWEN_PYTHON:-}" python3 python; do
  [ -n "$_c" ] || continue
  command -v "$_c" >/dev/null 2>&1 || continue
  "$_c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || continue
  PY="$_c"
  break
done
unset _c
[ -n "$PY" ] || { echo "qwen-test: no working Python 3.8+ found (set QWEN_PYTHON)" >&2; exit 2; }

PY_NATIVE_WIN=0
if command -v cygpath >/dev/null 2>&1 \
   && [ "$("$PY" -c 'import os; print(os.sep)' | tr -d '\r')" = "\\" ]; then
  PY_NATIVE_WIN=1
fi
native_path() { if [ "$PY_NATIVE_WIN" -eq 1 ]; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

PYTHONPATH="$(native_path "$SKILL_DIR")" exec "$PY" "$(native_path "$SKILL_DIR/lib/testrun.py")" --selectors "$@"
