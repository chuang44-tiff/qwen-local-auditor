#!/usr/bin/env bash
# Install contract, in a throwaway HOME: the skill is linked (or copied with a
# marker where symlinks are unavailable), two forwarders are written, the config
# is seeded once and never overwritten, and --uninstall removes what was added.
#
# Runs install.sh and the forwarders under $TEST_BASH when set, so a CI leg that
# claims to test a particular bash really does.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
B="${TEST_BASH:-$BASH}"
FAKE="$(mktemp -d)"
export HOME="$FAKE" USERPROFILE="$FAKE"
unset XDG_CONFIG_HOME CLAUDE_CONFIG_DIR QWEN_INSTALL_FORCE_COPY
fail() { echo "FAIL: $1"; [ -f "$FAKE/install.log" ] && sed 's/^/  | /' "$FAKE/install.log"; rm -rf "$FAKE"; exit 1; }
run_install() { "$B" "$REPO/install.sh" "$@" >"$FAKE/install.log" 2>&1 || fail "install.sh $* returned $?"; }

SKILL="$FAKE/.claude/skills/local-auditor"
CFG="$FAKE/.config/qwen-agent/config"

run_install --no-preflight
if [ -L "$SKILL" ]; then
  LINKED=1
  [ "$(cd -P "$SKILL" && pwd)" = "$(cd -P "$REPO/skill/local-auditor" && pwd)" ] \
    || fail "skill link points at the wrong place"
elif [ -f "$SKILL/.installed-copy" ]; then
  LINKED=0
  echo "note: symlinks unavailable here; the copy fallback was used"
else
  fail "skill was neither linked nor copied"
fi
[ -f "$SKILL/qwen-agent.sh" ] || fail "skill contents missing"
[ -f "$CFG" ] || fail "config was not seeded from config.example"
[ -x "$FAKE/.local/bin/qwen-agent" ] || fail "qwen-agent forwarder missing"
[ -x "$FAKE/.local/bin/qwen-sweep" ] || fail "qwen-sweep forwarder missing"
"$B" "$FAKE/.local/bin/qwen-agent" --version >/dev/null 2>&1 || fail "the qwen-agent forwarder does not run"
"$B" "$FAKE/.local/bin/qwen-sweep" --version >/dev/null 2>&1 || fail "the qwen-sweep forwarder does not run"

# An untouched config means there is no server to check yet: say what to do next.
run_install
grep -q "Next:" "$FAKE/install.log" || fail "an untouched config should end with the next step"

# Re-running is idempotent and never clobbers the user's config. The single quotes
# are deliberate: the literal ${...} text is what gets checked.
# shellcheck disable=SC2016
echo 'QWEN_MODEL="${QWEN_MODEL:-mine}"' >> "$CFG"
run_install --no-preflight
# shellcheck disable=SC2016
grep -q 'QWEN_MODEL="${QWEN_MODEL:-mine}"' "$CFG" || fail "re-install overwrote the user's config"

# The copy fallback, as used where symlinks cannot be created.
QWEN_INSTALL_FORCE_COPY=1 run_install --no-preflight
[ ! -L "$SKILL" ] && [ -f "$SKILL/.installed-copy" ] || fail "the forced copy fallback did not copy"
"$B" "$FAKE/.local/bin/qwen-agent" --version >/dev/null 2>&1 || fail "the forwarder does not run a copied skill"
run_install --no-preflight
if [ "$LINKED" -eq 1 ]; then
  [ -L "$SKILL" ] || fail "re-install did not replace its own copy with a link"
fi

run_install --uninstall
[ ! -e "$SKILL" ] && [ ! -L "$SKILL" ] || fail "--uninstall left the skill behind"
[ ! -e "$FAKE/.local/bin/qwen-agent" ] || fail "--uninstall left qwen-agent behind"
[ ! -e "$FAKE/.local/bin/qwen-sweep" ] || fail "--uninstall left qwen-sweep behind"
[ -f "$CFG" ] || fail "--uninstall must keep the user's config"

rm -rf "$FAKE"
echo "PASS"
