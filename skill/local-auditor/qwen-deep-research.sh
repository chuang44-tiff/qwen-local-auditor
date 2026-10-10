#!/usr/bin/env bash
# qwen-deep-research -- research one question on the web with a swarm of local-model
# Claude Code sessions: scope -> search -> fetch -> verify -> synthesize.
# It is qwen-swarm's built-in "research" workflow under its released name: this script
# answers --help and --version itself, then execs qwen-swarm.sh, which owns the config,
# the interpreter and the env export (QWEN_DR_* still work).
#
# Run `qwen-deep-research --help` for usage. Exit codes are documented there and
# are deliberately distinct so a caller can tell failure modes apart.
set -u

_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SKILL_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
unset _src _dir
# The same version source qwen-agent prints with --version: every research worker
# is a qwen-agent session, so this command is versioned with it. Read from the file
# rather than a hand copy, which drifts. Plain BRE: bash 3.2 / BSD sed compatible.
DR_VERSION="$(sed -n 's/^QA_VERSION="\(.*\)".*/\1/p' "$SKILL_DIR/qwen-agent.sh")"

usage() {
  # Deliberately not <<'USAGE': the text itself has a USAGE heading, and a heredoc
  # ends at the first line that is just the delimiter.
  cat <<'QDR_HELP'
qwen-deep-research — research a question with a swarm of local-model Claude Code
sessions: scope -> search -> fetch -> verify -> synthesize. The report is written
into a run folder: on exit 0 and 4 the last line of stdout is the report's path,
on exit 5 the run folder's path.

USAGE
  qwen-deep-research "the question" [options]
  cat question.md | qwen-deep-research --stdin [options]
  qwen-deep-research --resume RUN_DIR [options]   continue an interrupted run
  qwen-deep-research --check                      preflight only, research nothing

FLAGS
  QUESTION             The question, positional. Quote it. Or use --stdin.
  --stdin              Read the question from stdin.
  --depth NAME         quick | standard | deep | overnight (default standard), the
                       preset below. Deeper presets are meant for long unattended
                       runs: locally time is cheap, so they trade wall time for
                       completeness.
                        preset     angles  sources  claims  voters  budget  retries  rounds
                        quick          3        6     10       1     240s       1       1
                        standard       5       15     25       3     240s       1       1
                        deep           8       30     50       3     600s       2       2
                        overnight     10       40     80       5     900s       3   until
                       From round 2 a planner turns the last report's gaps into
                       new angles; the run stops early when nothing new is found.
  --max-agents N       Most agents one phase starts; work is dealt among them,
                       never cut (default 8; env QWEN_DR_MAX_AGENTS). Must be at
                       least the voters per claim (3 for standard and deep, 5
                       for overnight).
  --max-items N        Most items one agent holds (angles, sources or claim
                       votes; default 10; env QWEN_DR_MAX_ITEMS; N >= 1). A phase
                       with more items than --max-agents x --max-items splits
                       them in order into waves of that many items, each wave
                       dealt over --max-agents agents and run before the next
                       wave starts; wave 1 keeps the unit names ("verify-3"),
                       later waves carry the wave number ("verify-w2-3").
  --seats N            Agents running at once (default 4; env QWEN_DR_SEATS).
  --web-seats N        Search/fetch/verify agents running at once: default
                       --seats, never above it (env QWEN_DR_WEB_SEATS). Web
                       agents are told to call one tool at a time; lower
                       --web-seats if the server shows requests waiting during
                       the search, fetch and verify phases.
  --timeout SECONDS    budget in seconds (per-item, default: the depth preset's,
                       see the table; env QWEN_DR_TIMEOUT): an agent holding k
                       items gets max(300, k x timeout); readers get twice the
                       per-item budget, since each source is a whole page: a
                       reader holding k sources gets max(300, 2 x k x timeout);
                       scope and synthesis get 2 items. No unit's timeout, retry
                       doublings included, ever exceeds 4 h — 14400 seconds
                       (env QWEN_DR_MAX_UNIT_SECONDS).
  --retries N          A unit is retried only when its last failure was
                       qwen-agent exit 5 (a timeout), exit 3 or 4 after the
                       backoff wait, an empty result (exit 6, including on the
                       repair call), or an unusable answer (after its repair
                       round, or with no session to repair); any other exit
                       code (1, 2, 7, 8, a negative/signal exit) drops the unit
                       at once. Each retry gets double the previous timeout, up
                       to the 4 h cap; the repair call keeps its attempt's
                       timeout, and only the last failure counts as dropped
                       (default: the depth preset, see the table; N >= 0;
                       env QWEN_DR_RETRIES).
  --hours H            Hard stop for the whole run (H > 0; default: 8 for
                       --depth overnight, none otherwise; env QWEN_DR_HOURS):
                       a deadline H hours from the first start, stored in
                       config.json as an absolute UTC time. Once it passes,
                       no new wave and no new unit starts (running units
                       finish, queued units of the current wave are not
                       started), every item not run is logged as deadline in
                       run.log (not a drop), the phase continues with what it
                       has without writing its phase file, and synthesis
                       always runs; the report's Run table gains a "stopped at
                       deadline" row. Each agent is capped at 4 h; --hours
                       bounds the whole run. On --resume the stored deadline
                       stands unless --hours is given again, which sets a new
                       deadline from now.
  --effort LEVEL       Reasoning effort for every role, passed to qwen-agent
                       as -e LEVEL (default: qwen-agent's own configured
                       level). qwen-agent validates the level.
  --role-effort ROLE=LEVEL[,ROLE=LEVEL...]
                       Effort for single roles, roles scoper, searcher,
                       reader, verifier, planner, synthesizer; beats --effort. An
                       unknown role, a pair without = or an empty level is a
                       usage error. --effort and --role-effort
                       are stored in config.json (reused on resume unless given
                       again; on resume --effort replaces the stored level and
                       --role-effort merges into the stored dict, later wins)
                       and are part of each agent's cache key.
  --out DIR            The run folder (default: deep-research/<UTC stamp>-<slug>).
                       An existing run folder needs --resume, not --out again.
  --resume RUN_DIR     Continue a run: finished phases and agents are reused;
                       only --seats, --web-seats, --timeout, --retries, --hours,
                       --effort and --role-effort may change. The resume rewrites
                       config.json with the merged settings it ran with, so a
                       later resume reuses them; --hours there starts a new
                       deadline from now.
  --check              Preflight only: the model server is reachable and serves
                       the model, and one real search succeeds (no model answer
                       is asked for). Prints which one failed.
  -h, --help           This text.
  --version            Print the version.

ENVIRONMENT  (settable in $QWEN_CONFIG, the config qwen-agent uses; flags win)
  QWEN_SEARCH_BACKEND  searxng | brave. Inferred from the vars below when unset;
                       with both configured, SearXNG wins.
  QWEN_SEARCH_URL      SearXNG base URL, e.g. http://127.0.0.1:8888. One-liner:
                       docker run -d --name searxng -p 127.0.0.1:8888:8080
                       -v "$HOME/searxng:/etc/searxng" searxng/searxng — then add
                       json to search.formats in the settings.yml it generates,
                       or every search comes back 403.
  QWEN_SEARCH_KEY      Brave Search API key. Read from the environment only; it
                       appears in no prompt, log or report.
  QWEN_SEARCH_BRAVE_URL  Brave endpoint override.
  QWEN_DR_MAX_AGENTS / QWEN_DR_MAX_ITEMS / QWEN_DR_SEATS / QWEN_DR_WEB_SEATS /
                       QWEN_DR_TIMEOUT / QWEN_DR_RETRIES / QWEN_DR_HOURS /
                       QWEN_DR_MAX_UNIT_SECONDS  Defaults for the flags above.
                       A QWEN_DR_MAX_UNIT_SECONDS below 1 or non-numeric is
                       a usage error (exit 2).
  QWEN_DR_BACKOFF      Seconds before re-spawning an agent after a server error
                       (exit 3 or 4) (30).
  QWEN_PYTHON          The interpreter, probed by EXECUTION (Python 3.8+): a
                       PATH entry that cannot run is not trusted.
  QWEN_CONFIG          The config file, shared with qwen-agent.
  QWEN_DR_SKIP_SEARCH_CHECK=1   Skip the search preflight (test hook).
  QWEN_DR_AGENT_OVERRIDE        Stands in for `bash qwen-agent.sh` as the agent
                       command, split on spaces (test hook).

EXIT CODES
  0    report written; the last line of stdout is the report's path
  2    usage error (empty question, bad flag, a non-positive --hours, reusing
       a run folder); also
       "no working Python 3.8+ found (set QWEN_PYTHON)"
  3    preflight failed — the message says model or search
  4    report written, but agents were dropped or the --hours deadline left
       items not run (see RUN_DIR/run.log; --resume RUN --hours H continues);
       the last line of stdout is still the report's path
  5    a phase produced nothing usable (no report); the last line of stdout is
       the run folder's path
  8    internal error: the message is on stderr, <run>/error.log has the details
  130  interrupted (Ctrl-C); continue with --resume RUN_DIR

Setup detail, the phase table and the fence:
skill/local-auditor/reference/deep-research.md.
QDR_HELP
}

# Help and version answer before anything is probed: neither needs the config, a
# Python or a server. They mean "print this" only as the FIRST argument; anywhere
# else they are the caller's mistake and reach the runner, whose parser reports a
# usage error (exit 2) rather than printing a help screen nobody asked for.
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --version) echo "qwen-deep-research $DR_VERSION"; exit 0 ;;
esac

# Everything else is qwen-swarm's research workflow; --as-deep-research keeps this
# command's flags, messages, exit codes and run folder exactly as they were.
exec "${BASH:-bash}" "$SKILL_DIR/qwen-swarm.sh" --as-deep-research "$@"
