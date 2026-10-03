#!/usr/bin/env bash
# Build the three live-acceptance repos under OUTDIR. Offline; no model calls.
set -eu
OUT="${1:?usage: make-fixtures.sh OUTDIR}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
mk() {
  rm -rf "${OUT:?}/$1"
  mkdir -p "$OUT/$1"
  cd "$OUT/$1"
  git init -q
  git config user.email t@example.com
  git config user.name t
  printf '__pycache__/\n.pytest_cache/\n' > .gitignore
}

mk coder
cat > calc.py <<'EOT'
def add(a, b):
    return a - b
EOT
cat > test_calc.py <<'EOT'
from calc import add
def test_add():
    assert add(2, 3) == 5
EOT
git add -A
git commit -qm init
cat > "$OUT/coder.task.md" <<'EOT'
# Goal
Fix add() so it adds.
# Checklist
- [ ] add works -- check: test test_calc.py::test_add
EOT

mk drift
cat > net.py <<'EOT'
ATTEMPTS = 1
def fetch(flaky):
    for _ in range(ATTEMPTS):
        if flaky.try_once():
            return True
    return False
EOT
cat > test_net.py <<'EOT'
from net import fetch
class Flaky:
    def __init__(self): self.n = 0
    def try_once(self):
        self.n += 1
        return self.n >= 5
def test_flaky_upstream():
    assert fetch(Flaky())
EOT
git add -A
git commit -qm init
cat > "$OUT/drift.task.md" <<'EOT'
# Goal
Add retries to fetch().
# Spec
fetch() makes at most 3 attempts (ATTEMPTS = 3).
# Checklist
- [ ] flaky upstream succeeds -- check: test test_net.py::test_flaky_upstream
EOT

mk repro
cat > parse.py <<'EOT'
def parse_port(s):
    return int(s.split(":")[1])
EOT
git add -A
git commit -qm init
echo "fixtures in $OUT"
