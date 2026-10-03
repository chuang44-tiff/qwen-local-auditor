#!/usr/bin/env bash
# qwen-cc — open an INTERACTIVE Claude Code session on the local model inside
# tmux, then read its screen, type into it and stop it, without the user running
# a single shell command.
#
# Why tmux: an interactive claude needs a terminal and a stdin. A detached tmux
# session is a pty with neither attached, which gives three things a script can
# use -- a screen to capture (--peek), keys to send (--say), and a process to end
# (--stop). The claude side is `qwen-agent --interactive`: this server, this
# model, the same scrubbed child environment, and no tool fence at all, because
# the person at the keyboard answers Claude Code's own permission prompts.
#
# Ownership rule: every session created here carries the tmux session option
# @qwen_cc=1, and --peek/--say/--stop refuse (exit 2) any session without it, or
# any session that does not exist. Nothing else in the user's tmux is touched.
#
# tmux matches a -t argument by PREFIX as well as by name, so a name that is also
# a prefix of another session ("qwen-x-121314" and "qwen-x-121314-2" is exactly
# how that happens here) could otherwise be resolved to the wrong session:
# existence and the tag are checked against the exact session list first.

set -uo pipefail

QC_VERSION="1.0"
QC_SELF="qwen-cc"
QC_TAG="@qwen_cc"            # the option that marks a session as ours
QC_PANE_TAG="@qwen_cc_pane"  # the agent's pane id, recorded at launch
NL='
'
CR=$(printf '\r')
PANE=""
QC_DEFAULT_LINES=60          # --peek's default history
QC_STOP_WAIT=10              # seconds --stop waits for the session to end

usage() {
  cat <<EOF
$QC_SELF v$QC_VERSION — open and steer an interactive Claude Code session on the
local model, inside tmux. A person (or you, on the user's behalf) types into it.

USAGE
  $QC_SELF [DIR] [window flags] [-- QWEN_AGENT_ARGS...]
      Start a DETACHED tmux session running:
          qwen-agent --interactive -C DIR [QWEN_AGENT_ARGS...]
      DIR defaults to the current directory and must exist. Everything after '--'
      goes to qwen-agent, e.g.  -- --model other-model --effort medium.
      The session is named  qwen-<dir>-<HHMMSS>  (non-alphanumerics in the
      directory name become '-', and -2, -3, ... is appended when that name is
      taken) and is tagged  $QC_TAG=1, which is what marks it as ours. Prints:
          session: NAME
          attach:  tmux attach -t NAME
  $QC_SELF --list
      Print the name of every $QC_SELF session, one per line.
  $QC_SELF --peek NAME [LINES]
      Print the session's pane -- its last LINES lines of scrollback plus what is on
      screen (default $QC_DEFAULT_LINES): what it is doing, and what it is asking.
  $QC_SELF --say NAME TEXT...
      Type TEXT literally into the session and press Enter -- to answer what it
      asks, or to give it the next instruction.
  $QC_SELF --stop [--force] NAME
      Ctrl-C, then /exit and Enter, and wait up to ${QC_STOP_WAIT}s. Prints
      'stopped NAME', or 'still running: NAME (use --stop --force)'.
      --force kills the session (tmux kill-session) instead of asking it.
  -h, --help      This text.
  -V, --version   Print the version.

WINDOW  (a launch only: also open a terminal ATTACHED to the new session)
  --window        Open one even with no display set.
  --no-window     Do not. This is what a headless caller wants: it reads the pane
                  with --peek instead of looking at a window.
  Default: open one when DISPLAY or WAYLAND_DISPLAY is set. The first opener
  found is used -- gnome-terminal ('gnome-terminal -- tmux attach -t NAME'),
  else x-terminal-emulator ('x-terminal-emulator -e tmux attach -t NAME'); on
  macOS, Terminal.app through osascript. With no opener at all:
      window: none (attach with the command above)
  --dry-run       Print the tmux and window commands instead of running them.

SAFETY
  --peek, --say and --stop refuse a session this script did not create -- one
  that does not exist, or whose $QC_TAG is not 1 -- and exit 2 naming it. Answering
  the session's own permission prompts stays the person's decision: use --say for
  what the USER asked you to hand over, and for nothing else.

  One mode's flags stay in their mode: --window, --no-window and --dry-run belong
  to a launch, --force to --stop, and any of them given to another mode is refused
  instead of quietly ignored.

  tmux is required. Windows has none: run 'qwen-agent --interactive' directly in
  a terminal there instead.
EOF
}

die() { printf '%s: %s\n' "$QC_SELF" "$*" >&2; }

need_tmux() {
  if command -v tmux >/dev/null 2>&1; then
    # 3.0 or later: a launch hands tmux an argv, which older versions run as a string.
    case "$(tmux -V 2>/dev/null)" in
      *" "[012].*) die "tmux 3.0 or later is required (found: $(tmux -V))"; exit 2 ;;
    esac
    return 0
  fi
  die "tmux is required and is not on PATH. Install it (apt/brew); on Windows run 'qwen-agent --interactive' directly in a terminal instead."
  exit 2
}

session_exists() {
  # Exact name only: tmux ALSO matches a plain -t argument by prefix, so identity is
  # settled against the real list. Every later target is exact too: "=NAME:" for the
  # session and its options, the recorded pane id for keys and captures.
  tmux list-sessions -F '#S' 2>/dev/null | grep -qxF -- "$1"
}

# A session this script made (or one the user tagged as such by hand).
is_ours() { [ "$(tmux show-options -qv -t "=$1:" "$QC_TAG" 2>/dev/null)" = 1 ]; }

# The agent's own pane, recorded at launch. Keys and captures go to it, never to
# whatever pane is active: a person who attached and opened a shell in another
# window must not have --say typed into that shell.
agent_pane() {
  local pane
  pane=$(tmux show-options -qv -t "=$1:" "$QC_PANE_TAG" 2>/dev/null)
  case "$pane" in %[0-9]*) ;; *) return 1 ;; esac
  # In this session, and in a window that belongs to no other session (a window linked
  # in from elsewhere would carry keys to that session too).
  [ "$(tmux display-message -p -t "$pane" '#{session_name} #{window_linked}' 2>/dev/null)" = "$1 0" ] || return 1
  printf '%s\n' "$pane"
}

require_ours() {   # refuse, exit 2, naming the session
  local name="$1"
  if ! session_exists "$name"; then
    die "$name: no such tmux session (see '$QC_SELF --list')"
    exit 2
  fi
  if ! is_ours "$name"; then
    die "$name: not a $QC_SELF session ($QC_TAG is not 1) -- refusing to peek at, type into or stop a session $QC_SELF did not create"
    exit 2
  fi
  if ! PANE=$(agent_pane "$name"); then
    die "$name: its agent pane is gone (or was never recorded) -- refusing to guess which pane to use; --stop --force ends the session"
    exit 2
  fi
}

remote_line() {   # over SSH there is no window to open: give the line to paste on the other end
  [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}" ] || return 0
  # tmux by absolute path: the non-login shell ssh starts may not have it on PATH
  # (Homebrew's prefix on macOS).
  printf 'remote: ssh -t %s@%s %s attach -t %s\n' "${USER:-$(id -un)}" \
    "$(hostname 2>/dev/null || uname -n)" "$(q_sq "$(command -v tmux)")" "$1"
}

q_sq() {  # single-quote one word for the command string tmux hands to a pane
  # The replacement comes from a variable: bash <= 4.2 does not remove the quotes of a
  # literal replacement inside "${x//...}", which turned ' into \'\'\' and let a
  # directory name such as x';touch PWNED;# run as a command.
  local r="'\\''"
  printf "'%s'" "${1//\'/$r}"
}

session_name() {  # an unused qwen-<dir slug>-<HHMMSS> for the directory in $1
  local base slug name i now
  base="${1%/}"
  base="${base##*/}"
  slug="${base//[^A-Za-z0-9]/-}"
  [ -n "$slug" ] || slug="dir"
  now="$(date +%H%M%S)"
  name="qwen-$slug-$now"
  i=1
  while session_exists "$name"; do
    i=$((i + 1))
    name="qwen-$slug-$now-$i"
  done
  printf '%s' "$name"
}

OPENER=()
pick_opener() {  # the terminal command that attaches to session $1; 1 if none
  OPENER=()
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      command -v osascript >/dev/null 2>&1 || return 1
      OPENER=(osascript -e "tell application \"Terminal\" to do script \"tmux attach -t $1\"")
      return 0 ;;
  esac
  if command -v gnome-terminal >/dev/null 2>&1; then
    OPENER=(gnome-terminal -- tmux attach -t "$1")
    return 0
  fi
  if command -v x-terminal-emulator >/dev/null 2>&1; then
    OPENER=(x-terminal-emulator -e tmux attach -t "$1")
    return 0
  fi
  return 1
}

run_opener() {  # start it without holding this script's stdout or the caller's open
  (
    trap '' HUP
    exec "${OPENER[@]}" >/dev/null 2>&1
  ) &
}

MODE=""          # '' (launch) | list | peek | say | stop
WINDOW=auto      # auto | yes | no
DRY=0
FORCE=0
LINES="$QC_DEFAULT_LINES"
SEEN_DD=0
POS=()
EXTRA=()

while [ $# -gt 0 ]; do
  a="$1"
  if [ "$SEEN_DD" -eq 1 ]; then
    EXTRA+=("$a"); shift; continue
  fi
  case "$a" in
    --)             SEEN_DD=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    -V|--version)   printf '%s %s\n' "$QC_SELF" "$QC_VERSION"; exit 0 ;;
    --list)         MODE=list; shift ;;
    --peek|--say|--stop)
                    [ -z "$MODE" ] || { die "one of --list/--peek/--say/--stop at a time"; exit 2; }
                    MODE="${a#--}"; shift ;;
    --force)        FORCE=1; shift ;;
    --window)       WINDOW=yes; shift ;;
    --no-window)    WINDOW=no; shift ;;
    --dry-run)      DRY=1; shift ;;
    -*)             die "unknown option: $a (see --help)"; exit 2 ;;
    *)              POS+=("$a"); shift ;;
  esac
done

# Launch flags and steering flags do not mix: silently ignoring one of them would
# advertise an effect the mode does not have.
if [ -z "$MODE" ] && [ "$FORCE" -eq 1 ]; then
  die "--force belongs to --stop, not to a launch"; exit 2
fi
if [ -n "$MODE" ]; then
  [ "$WINDOW" = auto ] || { die "--window/--no-window belong to a launch, not to --$MODE"; exit 2; }
  [ "$DRY" -eq 0 ] || { die "--dry-run belongs to a launch; --$MODE runs one tmux command"; exit 2; }
  [ "$MODE" = stop ] || [ "$FORCE" -eq 0 ] ||
    { die "--force belongs to --stop (mode given: --$MODE)"; exit 2; }
fi

case "$MODE" in
  list)
    need_tmux
    [ "${#POS[@]}" -eq 0 ] || { die "--list takes no arguments (got ${#POS[@]})"; exit 2; }
    # The names come from the session list itself, and each is asked for its tag: no
    # format string has to survive a name that contains a space (tmux forbids a
    # newline, nothing else). No server at all is an empty list, not an error.
    tmux list-sessions -F '#S' 2>/dev/null | while IFS= read -r s; do
      is_ours "$s" && printf '%s\n' "$s"
    done
    exit 0
    ;;

  peek)
    need_tmux
    [ "${#POS[@]}" -le 2 ] || { die "--peek takes a session name and an optional line count"; exit 2; }
    [ "${#POS[@]}" -ge 1 ] || { die "--peek needs a session name (see '$QC_SELF --list')"; exit 2; }
    NAME="${POS[0]}"
    if [ "${#POS[@]}" -eq 2 ]; then
      LINES="${POS[1]}"
      case "$LINES" in ''|*[!0-9]*) die "--peek: LINES must be a whole number above 0, got '${POS[1]}'"; exit 2 ;; esac
      [ "$LINES" -gt 0 ] || { die "--peek: LINES must be above 0, got '${POS[1]}'"; exit 2; }
    fi
    require_ours "$NAME"
    tmux capture-pane -p -J -t "$PANE" -S "-$LINES"
    exit 0
    ;;

  say)
    need_tmux
    [ "${#POS[@]}" -ge 2 ] || { die "--say needs a session name and the text to type into it"; exit 2; }
    NAME="${POS[0]}"
    require_ours "$NAME"
    # -l sends the text as text, never as key names: 'Enter' and '~/x' stay words.
    text="${POS[*]:1}"
    case "$text" in
      *"$NL"*|*"$CR"*) die "--say sends one line; the text contains a line break (send each line with its own --say)"; exit 2 ;;
    esac
    tmux send-keys -t "$PANE" -l -- "$text"
    # A moment before Enter, so the burst of text is not taken for a paste that the
    # Enter then lands inside of.
    sleep 0.3
    tmux send-keys -t "$PANE" Enter
    exit 0
    ;;

  stop)
    need_tmux
    [ "${#POS[@]}" -eq 1 ] || { die "--stop needs exactly one session name"; exit 2; }
    NAME="${POS[0]}"
    require_ours "$NAME"
    if [ "$FORCE" -eq 1 ]; then
      if tmux kill-session -t "=$NAME" 2>/dev/null; then
        printf 'stopped %s\n' "$NAME"
        exit 0
      fi
      die "$NAME: could not be killed"
      exit 1
    fi
    tmux send-keys -t "$PANE" C-c
    tmux send-keys -t "$PANE" -l -- '/exit'
    sleep 0.3
    tmux send-keys -t "$PANE" Enter
    i=0
    while [ "$i" -lt "$QC_STOP_WAIT" ]; do
      session_exists "$NAME" || { printf 'stopped %s\n' "$NAME"; exit 0; }
      # With remain-on-exit set (some tmux configs set it globally) the session
      # outlives its program: a session whose every pane is dead has stopped.
      if [ "$(tmux display-message -p -t "$PANE" '#{pane_dead}' 2>/dev/null)" = 1 ]; then
        tmux kill-session -t "=$NAME" 2>/dev/null
        printf 'stopped %s\n' "$NAME"
        exit 0
      fi
      sleep 1
      i=$((i + 1))
    done
    printf 'still running: %s (use --stop --force)\n' "$NAME"
    exit 1
    ;;

  "")
    need_tmux
    [ "${#POS[@]}" -le 1 ] || { die "one directory at a time (got ${#POS[@]} arguments)"; exit 2; }
    DIR="${POS[0]:-$PWD}"
    # Absolute: the pane starts wherever tmux was invoked, so a relative DIR would
    # point qwen-agent at some other directory entirely.
    case "$DIR" in /*|[A-Za-z]:/*) ;; *) DIR="$PWD/$DIR" ;; esac
    [ "$DIR" = "/" ] || DIR="${DIR%/}"
    [ -e "$DIR" ] || { die "$DIR: no such directory -- create it first, or pass one that exists"; exit 2; }
    [ -d "$DIR" ] || { die "$DIR: not a directory"; exit 2; }
    NAME="$(session_name "$DIR")"
    CMD="qwen-agent --interactive -C $(q_sq "$DIR")"
    for a in ${EXTRA[@]+"${EXTRA[@]}"}; do CMD="$CMD $(q_sq "$a")"; done
    want_window=0
    case "$WINDOW" in
      yes)  want_window=1 ;;
      no)   want_window=0 ;;
      auto) [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && want_window=1 ;;
    esac
    if [ "$DRY" -eq 1 ]; then
      printf 'session: %s\n' "$NAME"
      printf 'attach: tmux attach -t %s\n' "$NAME"
      remote_line "$NAME"
      printf 'command: tmux new-session -d -s %s %s\n' "$NAME" "$(q_sq "$CMD")"
      if [ "$want_window" -eq 1 ]; then
        if pick_opener "$NAME"; then
          printf 'window: %s\n' "${OPENER[*]}"
        else
          printf 'window: none (attach with the command above)\n'
        fi
      fi
      exit 0
    fi
    # The program and its arguments go to tmux as an argv, never as one string: tmux runs
    # a single string through the user's $SHELL, and no one quoting is safe in every
    # shell (fish reads \' inside single quotes differently from sh). CMD is for display.
    PANE=$(tmux new-session -d -P -F '#{pane_id}' -s "$NAME" \
             qwen-agent --interactive -C "$DIR" ${EXTRA[@]+"${EXTRA[@]}"}) ||
      { die "$NAME: tmux new-session failed"; exit 1; }
    # The pane dies at once when qwen-agent is not on PATH; tagging would then
    # fail with a tmux error that says nothing about the real cause.
    session_exists "$NAME" || { die "$NAME: the session ended as it started -- run it by hand to see why: $CMD"; exit 1; }
    # An untagged session is one no later --peek/--say/--stop will agree to touch, so
    # say so now, while the name and the attach line are still worth having.
    { tmux set-option -t "=$NAME:" "$QC_PANE_TAG" "$PANE" && tmux set-option -t "=$NAME:" "$QC_TAG" 1; } ||
      die "$NAME: it started, but tagging it failed -- $QC_SELF will not list, read, type into or stop it"
    printf 'session: %s\n' "$NAME"
    printf 'attach: tmux attach -t %s\n' "$NAME"
    remote_line "$NAME"
    if [ "$want_window" -eq 1 ]; then
      if pick_opener "$NAME"; then
        printf 'window: %s\n' "${OPENER[*]}"
        run_opener
      else
        printf 'window: none (attach with the command above)\n'
      fi
    fi
    exit 0
    ;;

  *)
    die "unknown mode: $MODE"
    exit 2
    ;;
esac
