#!/usr/bin/env bash
# Live acceptance for local-agent. Needs a QUIET server (nothing running/waiting).
# Run by hand; CI never runs this.
set -u
OUT="${1:?usage: run-acceptance.sh OUTDIR (after make-fixtures.sh OUTDIR)}"
OUT="$(cd "$OUT" && pwd)" || exit 1

# The same machine config qwen-agent reads (CRs stripped), so we probe the right server.
_cfg="${QWEN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/qwen-agent/config}"
if [ -f "$_cfg" ] && [ -r "$_cfg" ]; then
  eval "$(tr -d '\r' < "$_cfg")"
fi
unset _cfg
BASE="${QWEN_BASE_URL:-http://127.0.0.1:8000}"

m="$(curl -s -m5 "$BASE/metrics")" || { echo "metrics unreachable"; exit 1; }
for _k in running waiting; do
  printf '%s\n' "$m" | grep -q "^vllm:num_requests_${_k}[{ ]" \
    || { echo "REFUSING: vllm:num_requests_$_k not found in $BASE/metrics"; exit 1; }
done
busy="$(printf '%s\n' "$m" | awk '/^vllm:num_requests_(running|waiting)[{ ]/ {s+=$NF} END {print s+0}')"
[ "$busy" = 0 ] || { echo "REFUSING: server not quiet ($busy in flight)"; exit 1; }
export QWEN_TEST_CMD="python3 -m pytest -q"
res=0

( cd "$OUT/coder" && qwen-agent --until-done "$OUT/coder.task.md" --no-deviation-audit ) > "$OUT/coder.log" 2>&1
c=$?
if [ "$c" -eq 0 ] && grep -q "^report: " "$OUT/coder.log"; then
  echo "PASS coder  ($(sed -n 's/^report: //p' "$OUT/coder.log" | tail -n 1))"
else
  echo "FAIL coder (exit $c); log: $OUT/coder.log"; res=1
fi

( cd "$OUT/drift" && qwen-agent --until-done "$OUT/drift.task.md" ) > "$OUT/drift.log" 2>&1
c=$?
rep="$(sed -n 's/^report: //p' "$OUT/drift.log" | tail -n 1)"
# decisions.jsonl only ever holds parsed DEVIATION blocks, so any "did" entry is a recorded deviation.
if [ "$c" -eq 0 ] && [ -n "$rep" ] && grep -q '"did"' "$(dirname "$rep")/decisions.jsonl" 2>/dev/null; then
  ( cd "$OUT/drift" && git add -A && git commit -qm coder && \
    qwen-sweep --builder deviations --repo . --base HEAD~1 --arg spec="$OUT/drift.task.md" \
      --out "$OUT/drift-sweep" ) > "$OUT/drift-audit.log" 2>&1
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if any((r.get("verdict") or "").upper()=="DEVIATION_EXPLAINED" for r in d["rows"]) else 1)' "$OUT/drift-sweep/collated.json" 2>/dev/null; then
    echo "PASS drift  ($rep)"
  else
    echo "FAIL drift (no DEVIATION_EXPLAINED); log: $OUT/drift-audit.log"; res=1
  fi
else
  echo "FAIL drift (agent exit $c or no decision recorded); log: $OUT/drift.log"; res=1
fi

before="$(cd "$OUT/repro" && git status --porcelain)"
( cd "$OUT/repro" && qwen-agent -r auditor --test \
    "parse_port('localhost') crashes. Prove it with a failing pytest test; do not fix it." ) > "$OUT/repro.log" 2>&1
after="$(cd "$OUT/repro" && git status --porcelain)"
if grep -q "## REPRO FILES" "$OUT/repro.log" && [ "$before" = "$after" ]; then
  echo "PASS repro  (log: $OUT/repro.log)"
else
  echo "FAIL repro; log: $OUT/repro.log"; res=1
fi
exit "$res"
