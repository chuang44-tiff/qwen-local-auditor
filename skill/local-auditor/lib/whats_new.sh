# shellcheck shell=bash
# Sourced by qwen-agent.sh and qwen-swarm.sh: qa_whats_new SKILL_DIR CONFIG_PATH prints the
# one-time "Claude settings changed" notice to STDERR (stdout carries results) and leaves a
# marker both tools share. QWEN_NO_NOTICE=1 suppresses it. Never fails the run: an
# unwritable state dir just means the notice may show again.
qa_whats_new() {
  [ "${QWEN_NO_NOTICE:-}" = 1 ] && return 0
  local _m="${XDG_STATE_HOME:-$HOME/.local/state}/qwen-agent/notice-claude-settings-1" _t _c="$2" _l
  [ -e "$_m" ] && return 0
  [ -r "$1/lib/whats_new.txt" ] || return 0
  while IFS= read -r _l || [ -n "$_l" ]; do
    printf '%s\n' "${_l//@CONFIG@/"$_c"}"
  done < "$1/lib/whats_new.txt" >&2
  { mkdir -p "$(dirname "$_m")" && : > "$_m"; } 2>/dev/null || true
  return 0
}
