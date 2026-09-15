#!/usr/bin/env bash
# qwen-sweep -- run the local audit lane over N items.
#
# The engine (lib/engine.py) owns building, withholding, parsing and collation.
# This script owns the things that are bash's job and were measured the hard way:
#
#  * a single-instance mkdir LOCK. mkdir is atomic; a plain -e test is not, and
#    three concurrent runners once deleted each other's batch dirs, producing
#    "exit 8 with empty stdout AND stderr".
#  * -C rooted at the BATCH DIR, never the repo. Defense in depth, not a fence:
#    Read takes absolute paths. The real fence is the read-only toolset and
#    --strict-mcp, which qwen-agent applies by default.
#  * success judged by CONTENT, not exit code: a batch passes only when every
#    expected block is present. A tiny block-less body is the autocompact
#    self-destruct, and it has been observed at BOTH rc=0 and rc=8.
#  * retry an incomplete batch once, and --resume to skip batches that already
#    passed, instead of rm -rf from b01.
#
# Portable across Linux, macOS (bash 3.2, BSD userland) and Git Bash on Windows:
# no readlink -f, no stat -c, and no paths spliced into python source.
set -u
SWEEP_VERSION="3.0"

# ---- locate the skill directory, following symlinks without readlink -f ------
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
SKILL_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
unset _src _dir

# The same machine config qwen-agent reads (CRs stripped: Windows editors add them).
_cfg="${QWEN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/config}"
if [ -f "$_cfg" ] && [ -r "$_cfg" ]; then
  eval "$(tr -d '\r' < "$_cfg")"
fi
unset _cfg
# Native Windows Python defaults to cp1252 for files and pipes; everything here is UTF-8.
export PYTHONUTF8=1

# A python on PATH is not enough (on Windows `python3` is often a Store stub, and
# old systems still call Python 2 `python`), so each candidate is RUN first.
PY=""
for _c in "${QWEN_PYTHON:-}" python3 python; do
  [ -n "$_c" ] || continue
  command -v "$_c" >/dev/null 2>&1 || continue
  "$_c" -c 'import sys, json; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || continue
  PY="$_c"
  break
done
unset _c
[ -n "$PY" ] || { echo "qwen-sweep: no working Python 3.8+ found (set QWEN_PYTHON)" >&2; exit 8; }

DISPATCH="${QWEN_SWEEP_DISPATCH:-qwen-agent}"

# Only a NATIVE Windows python needs Windows-form paths (an MSYS2 or Cygwin python
# does not), so ask the interpreter rather than guessing from cygpath's presence.
PY_NATIVE_WIN=0
if command -v cygpath >/dev/null 2>&1 \
   && [ "$("$PY" -c 'import os; print(os.sep)' | tr -d '\r')" = "\\" ]; then
  PY_NATIVE_WIN=1
fi
native_path() { if [ "$PY_NATIVE_WIN" -eq 1 ]; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
posix_path()  { if [ "$PY_NATIVE_WIN" -eq 1 ]; then cygpath -u "$1"; else printf '%s' "$1"; fi; }
# engine.py runs as a SCRIPT, not `python -m lib.engine`: -m puts the cwd first on
# sys.path, so running a sweep inside a repo with its own lib/ package would
# import that package instead.
engine() { PYTHONPATH="$(native_path "$SKILL_DIR")" "$PY" "$(native_path "$SKILL_DIR/lib/engine.py")" "$@"; }

BUILDER=""; BRIEF=""; REPO="$PWD"; BASE=""; GLOB=""; INPUT=""; DOCS=""; ITEMS=""
DRY=0; RESUME=0; STRICT=0; ALLOW_EMPTY=0; BUDGET=""; ITEM_BUDGET=""; ROLE="auditor"; OUT=""
PREFLIGHT=1
case "${QWEN_PREFLIGHT:-1}" in 0|false|no) PREFLIGHT=0 ;; esac
EXTRA_ARGS=()

usage() {
  cat <<'USAGE'
qwen-sweep --builder {claims|diff|files|logs} [options]

Runs qwen-agent over many items: builds batches, dispatches each, collates the
answers. Always --dry-run a new sweep first.

BUILDERS
  diff    changed files      --repo DIR [--base REF]
                             no --base: uncommitted changes (staged + unstaged) vs HEAD
  files   files from a glob  --repo DIR --glob 'src/**/*.py'
  claims  claim documents    --repo DIR --docs DIR --items list.json
                             list.json is a JSON list of file names inside --docs,
                             e.g. ["ISSUE-12.md", "design/cache.md"]
  logs    chunks of a text   --input FILE

OPTIONS
  --repo DIR          repo under audit (default: cwd); --glob and --docs resolve under it
  --base REF          diff: compare against REF
  --glob PAT          files: glob relative to --repo
  --input FILE        logs: the text to chunk
  --docs DIR          claims: directory of claim documents
  --items FILE        claims: JSON list of document names inside --docs
  --arg KEY=VALUE     extra builder setting, repeatable. claims: test_dirs=test,spec
                      strip_fields=Status,Owner exts=.py,.go census_root=DIR
                      logs: chunk_bytes=N
  --brief NAME        use brief NAME ($QWEN_BRIEF_DIR/NAME.md, else the bundled one)
  --role NAME         qwen-agent role (default: auditor)
  -m, --model NAME    model for every batch (sets QWEN_MODEL)
  --effort LEVEL      effort for every batch (sets QWEN_EFFORT)
  --timeout SECS      wall clock per batch (sets QWEN_TIMEOUT)
  --budget BYTES      bytes per batch (default: scaled to the model's context window)
  --item-budget BYTES bytes per item (default: half the batch budget)
  --out DIR           run directory (default: a new run under the sweep cache)
  --dry-run           build and report, dispatch nothing
  --resume            continue the latest run for this repo, skipping complete batches
  --strict            a builder exception aborts the run instead of withholding the item
  --allow-empty       exit 0 even when there is nothing to audit
  --no-preflight      skip the one-time server check (also QWEN_PREFLIGHT=0)
  -V, --version       print the version

OUTPUT  (in the run directory, whose path is printed at the start and the end)
  collated.json       every answer: rows[] of {item, key, verdict, finding, evidence,
                      why}, plus problems[] and withheld[]
  needs-human.txt     items never dispatched, with the reason (no evidence, over
                      budget, empty): route these to a person
  progress.log        this run's console output
  bNN/                one batch: t*.md and context*.md (what the model saw),
                      brief.md, out.md (its answer), stderr.txt (qwen-agent's messages)
  Runs live under $QWEN_SWEEP_CACHE, else $XDG_CACHE_HOME/qwen-sweep, else
  ~/.cache/qwen-sweep, grouped per repo.

EXIT CODES
  0    every dispatched batch answered completely
  1    collation problems: missing blocks, or prose cited as evidence
  2    usage error (bad option, missing builder argument, unreadable --items)
  3    preflight failed: server, model or key (the message says which)
  8    no working Python 3.8+, or claude missing
  9    nothing was audited: build failed, no items, or every item withheld
  10   another sweep holds this run's lock
  130  interrupted
USAGE
}

need() { [ "$2" -ge 2 ] || { echo "qwen-sweep: $1 needs a value (see --help)" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --builder)     need "$1" $#; BUILDER="$2";     shift 2 ;;
    --brief)       need "$1" $#; BRIEF="$2";       shift 2 ;;
    --repo)        need "$1" $#; REPO="$2";        shift 2 ;;
    --base)        need "$1" $#; BASE="$2";        shift 2 ;;
    --glob)        need "$1" $#; GLOB="$2";        shift 2 ;;
    --input)       need "$1" $#; INPUT="$2";       shift 2 ;;
    --docs)        need "$1" $#; DOCS="$2";        shift 2 ;;
    --items)       need "$1" $#; ITEMS="$2";       shift 2 ;;
    --role)        need "$1" $#; ROLE="$2";        shift 2 ;;
    --out)         need "$1" $#; OUT="$2";         shift 2 ;;
    --budget)      need "$1" $#; BUDGET="$2";      shift 2 ;;
    --item-budget) need "$1" $#; ITEM_BUDGET="$2"; shift 2 ;;
    -m|--model)    need "$1" $#; export QWEN_MODEL="$2";   shift 2 ;;
    --effort)      need "$1" $#; export QWEN_EFFORT="$2";  shift 2 ;;
    --timeout)     need "$1" $#; export QWEN_TIMEOUT="$2"; shift 2 ;;
    --arg)         need "$1" $#
                   case "$2" in
                     [A-Za-z_]*=*) EXTRA_ARGS+=("$2") ;;
                     *) echo "qwen-sweep: --arg takes KEY=VALUE, got '$2'" >&2; exit 2 ;;
                   esac
                   shift 2 ;;
    --dry-run)      DRY=1;         shift ;;
    --resume)       RESUME=1;      shift ;;
    --strict)       STRICT=1;      shift ;;
    --allow-empty)  ALLOW_EMPTY=1; shift ;;
    --no-preflight) PREFLIGHT=0;   shift ;;
    -V|--version)   echo "qwen-sweep $SWEEP_VERSION"; exit 0 ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "qwen-sweep: unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[ -n "$BUILDER" ] || { echo "qwen-sweep: --builder is required (see --help)" >&2; exit 2; }
for _n in "$BUDGET" "$ITEM_BUDGET"; do
  case "$_n" in ''|[0-9]*[0-9]|[0-9]) ;; *) echo "qwen-sweep: budgets are whole numbers of bytes, got '$_n'" >&2; exit 2 ;; esac
  case "$_n" in *[!0-9]*) echo "qwen-sweep: budgets are whole numbers of bytes, got '$_n'" >&2; exit 2 ;; esac
done
unset _n
REPO="$(cd "$REPO" 2>/dev/null && pwd)" || { echo "qwen-sweep: --repo is not a directory" >&2; exit 2; }

# ---- one preflight for the whole sweep, instead of one failing retry per batch ----
CTX_DETECTED=""
if [ "$PREFLIGHT" -eq 1 ]; then
  pf_msg="$("$DISPATCH" --preflight-only 2>&1 >/dev/null)"
  pf_rc=$?
  if [ "$pf_rc" -eq 0 ]; then
    CTX_DETECTED="$(printf '%s\n' "$pf_msg" | tr -d '\r' | sed -n 's/.*context \([0-9][0-9]*\).*/\1/p' | head -n 1)"
  elif [ "$DRY" -eq 1 ]; then
    echo "sweep: WARNING: preflight failed (rc=$pf_rc); the dry run continues with default budgets:" >&2
    printf '%s\n' "$pf_msg" | sed 's/^/  /' >&2
  else
    echo "sweep: preflight failed (rc=$pf_rc) -- nothing dispatched:" >&2
    printf '%s\n' "$pf_msg" | sed 's/^/  /' >&2
    exit "$pf_rc"
  fi
fi

# ---- budgets: sized to the model's window unless given -----------------------
# ~3 bytes per token, with ~20k tokens reserved for claude's own prompt and tools.
if [ -z "$BUDGET" ]; then
  BUDGET=240000
  if [ -n "$CTX_DETECTED" ]; then
    BUDGET=$(( (CTX_DETECTED - 20000) * 3 ))
    [ "$BUDGET" -gt 240000 ] && BUDGET=240000
    [ "$BUDGET" -lt 8000 ] && BUDGET=8000
  fi
fi
[ -n "$ITEM_BUDGET" ] || ITEM_BUDGET=$(( BUDGET / 2 ))

# ---- assemble the builder's args as JSON (the builder defines its own keys) --
ARGS="$("$PY" - "$REPO" "$BASE" "$GLOB" "$INPUT" "$DOCS" "$ITEMS" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} <<'PYJSON'
import json, sys
repo, base, glob, inp, docs, items = sys.argv[1:7]
a = {"repo": repo}
if base:  a["base"] = base
if glob:  a["glob"] = glob
if inp:   a["input"] = inp
if docs:  a["docs"] = docs
if items:
    try:
        with open(items, encoding="utf-8") as fh:
            a["items"] = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("qwen-sweep: cannot read --items %s: %s\n" % (items, exc))
        sys.exit(2)
for kv in sys.argv[7:]:
    key, value = kv.split("=", 1)
    a[key] = value
print(json.dumps(a))
PYJSON
)" || exit 2

# ---- run directory, OUTSIDE the target repo --------------------------------
if [ -z "$OUT" ]; then
  ROOT="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from lib.engine import batch_root; print(batch_root(sys.argv[2]))' \
          "$(native_path "$SKILL_DIR")" "$REPO" | tr -d '\r')" || exit 2
  ROOT="$(posix_path "$ROOT")"
  if [ "$RESUME" -eq 1 ]; then
    # run-YYYYmmddTHHMMSS-PID names sort by time, so ls is safe here.
    # shellcheck disable=SC2012
    OUT="$(ls -d "$ROOT"/run-* 2>/dev/null | sort | tail -n 1)"
    [ -n "$OUT" ] || { echo "qwen-sweep: --resume but no previous run under $ROOT" >&2; exit 2; }
  else
    OUT="$ROOT/run-$(date -u +%Y%m%dT%H%M%S)-$$"
  fi
fi
mkdir -p "$OUT" || exit 2
OUT="$(cd "$OUT" && pwd)"

if [ "$RESUME" -eq 1 ] && [ -f "$OUT/manifest.json" ]; then
  prev="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("builder", ""))' \
          "$(native_path "$OUT/manifest.json")" 2>/dev/null | tr -d '\r')"
  if [ -n "$prev" ] && [ "$prev" != "$BUILDER" ]; then
    echo "qwen-sweep: --resume: $OUT was built with --builder $prev, not $BUILDER" >&2
    exit 2
  fi
fi

LOCK="$OUT/.runlock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "REFUSING: a sweep already owns $OUT (pid $(cat "$LOCK/pid" 2>/dev/null))." >&2
  exit 10
fi
echo "$$" > "$LOCK/pid"
# An INT/TERM trap must EXIT: otherwise the loop carries on with the lock released.
trap 'rm -rf "$LOCK"' EXIT
trap 'rm -rf "$LOCK"; echo "sweep: interrupted" >&2; exit 130' INT
trap 'rm -rf "$LOCK"; exit 143' TERM

PROG="$OUT/progress.log"
[ "$RESUME" -eq 1 ] || : > "$PROG"
echo "sweep: builder=$BUILDER repo=$REPO out=$OUT" | tee -a "$PROG"
echo "sweep: budgets batch=$BUDGET item=$ITEM_BUDGET bytes (context window: ${CTX_DETECTED:-unknown})" | tee -a "$PROG"

# ---- build (unless resuming an existing build) ------------------------------
if [ "$RESUME" -eq 0 ]; then
  BUILD_ARGS=(build --builder "$BUILDER" --args "$ARGS" --out "$(native_path "$OUT")"
              --budget "$BUDGET" --item-budget "$ITEM_BUDGET")
  [ -n "$BRIEF" ]          && BUILD_ARGS+=(--brief "$BRIEF")
  [ "$STRICT" -eq 1 ]      && BUILD_ARGS+=(--strict)
  [ "$ALLOW_EMPTY" -eq 1 ] && BUILD_ARGS+=(--allow-empty)
  # PIPESTATUS, not `if ! a | tee`: that would test tee, which always succeeds.
  engine "${BUILD_ARGS[@]}" 2>&1 | tr -d '\r' | tee -a "$PROG"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -eq 2 ]; then
    exit 2
  elif [ "$rc" -ne 0 ]; then
    echo "sweep: nothing dispatched (exit 9) -- the reason is above" | tee -a "$PROG"
    exit 9
  fi
fi

if [ -n "$BRIEF" ]; then
  BRIEF_NAME="$BRIEF"
else
  BRIEF_NAME="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["brief"])' \
                "$(native_path "$OUT/manifest.json")" | tr -d '\r')"
fi

if [ "$DRY" -eq 1 ]; then
  echo "sweep: --dry-run, nothing dispatched ($OUT)" | tee -a "$PROG"
  exit 0
fi

# ---- dispatch ---------------------------------------------------------------
for d in "$OUT"/b[0-9][0-9]*; do
  [ -d "$d" ] || continue
  tag="$(basename "$d")"
  n="$(grep -c . "$d/expected.txt" 2>/dev/null)"
  [ "${n:-0}" -gt 0 ] || { echo "$tag: no expected blocks, skipping" | tee -a "$PROG"; continue; }

  if [ "$RESUME" -eq 1 ] && engine check --dir "$(native_path "$d")" >/dev/null 2>&1; then
    echo "$tag: resume -- already complete" | tee -a "$PROG"; continue
  fi

  engine brief --name "$BRIEF_NAME" --expected "$(native_path "$d/expected.txt")" > "$d/brief.md" \
    || { echo "$tag: BRIEF RENDER FAILED" | tee -a "$PROG"; exit 9; }

  attempt=1
  while : ; do
    # -C is the batch dir: no repo file is reachable by a relative path at all.
    "$DISPATCH" -r "$ROLE" -C "$d" -f "$d/brief.md" -o "$d/out.md" \
        >"$d/stdout.txt" 2>"$d/stderr.txt"
    rc=$?
    status="$(engine check --dir "$(native_path "$d")" | tr -d '\r')"
    case "$status" in
      ok:*) echo "$tag: rc=$rc $status (attempt $attempt)" | tee -a "$PROG"; break ;;
    esac
    case "$rc" in
      2|3|8)   # usage, preflight or harness: retrying cannot fix these
        echo "$tag: rc=$rc $status -- not retrying; qwen-agent said:" | tee -a "$PROG"
        tail -n 5 "$d/stderr.txt" 2>/dev/null | sed 's/^/    /' | tee -a "$PROG"
        break ;;
    esac
    if [ "$attempt" -eq 1 ]; then
      echo "$tag: rc=$rc $status -- retrying once" | tee -a "$PROG"
      attempt=2; continue
    fi
    echo "$tag: rc=$rc $status after retry -- giving up on this batch; qwen-agent said:" | tee -a "$PROG"
    tail -n 5 "$d/stderr.txt" 2>/dev/null | sed 's/^/    /' | tee -a "$PROG"
    break
  done
done

echo "sweep: dispatch complete" | tee -a "$PROG"
engine collate --root "$(native_path "$OUT")" --repo "$(native_path "$REPO")" | tr -d '\r' | tee -a "$PROG"
exit "${PIPESTATUS[0]}"
