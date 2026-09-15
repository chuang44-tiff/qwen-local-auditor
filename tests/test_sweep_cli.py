"""qwen-sweep end to end, offline, with a fake dispatcher standing in for qwen-agent.

Exercises the parts that break across platforms or silently: symlink-free self
location, python discovery, CRLF-free batch files, brief rendering, the
content-based success check, the one-time preflight, budget scaling, the lock,
and every "nothing was audited" path.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SWEEP = ROOT / "skill" / "local-auditor" / "qwen-sweep.sh"
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")

FAKE_DISPATCH = r'''#!/usr/bin/env bash
# Stands in for qwen-agent. Called as `--preflight-only`, or `-r ROLE -C DIR -f BRIEF -o OUT`.
for a in "$@"; do
  if [ "$a" = "--preflight-only" ]; then
    if [ -n "${FAKE_PREFLIGHT_RC:-}" ]; then
      echo "qwen-agent: cannot reach the fake server" >&2
      exit "$FAKE_PREFLIGHT_RC"
    fi
    echo "qwen-agent: preflight ok: model 'fake' served at http://fake, context ${FAKE_CTX:-262144}, python 'python3', claude 'claude'" >&2
    exit 0
  fi
done
while [ $# -gt 0 ]; do
  case "$1" in
    -C) dir="$2"; shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *)  shift ;;
  esac
done
{
  last="$(grep . "$dir/expected.txt" | tail -n 1)"
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if [ -n "${FAKE_DROP_LAST:-}" ] && [ "$key" = "$last" ]; then continue; fi
    printf '## %s\nFINDING: fake finding for %s\nEVIDENCE: src/a.py:1\nWHY: the fake says so\n\n' "$key" "$key"
  done < "$dir/expected.txt"
} > "$out"
'''


def posix(p):
    return str(p).replace("\\", "/")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (r / "src" / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    return r


@pytest.fixture
def dispatch(tmp_path):
    p = tmp_path / "fake-dispatch"
    p.write_text(FAKE_DISPATCH, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def sweep(tmp_path, args, dispatch=None, extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("QWEN_") and k != "XDG_CACHE_HOME"}
    env["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")
    if dispatch is not None:
        env["QWEN_SWEEP_DISPATCH"] = posix(dispatch)
    env.update(extra or {})
    # cwd is deliberately NOT the repo: relative --glob/--docs must resolve under --repo.
    return subprocess.run([BASH, posix(SWEEP), *args], env=env, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          cwd=str(cwd or tmp_path))


def files_args(repo, out, glob="src/*.py"):
    return ["--builder", "files", "--repo", posix(repo), "--glob", glob, "--out", posix(out)]


def test_a_full_sweep_collates_every_block(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    r = sweep(tmp_path, files_args(repo, out), dispatch)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((out / "collated.json").read_text(encoding="utf-8"))
    assert sorted(row["key"] for row in doc["rows"]) == ["t1", "t2"]
    assert doc["problems"] == []
    brief = (out / "b01" / "brief.md").read_text(encoding="utf-8")
    assert "t1,t2" in brief and "{{" not in brief
    assert "results:" in r.stdout


def test_a_short_complete_answer_is_a_success(tmp_path, repo, dispatch):
    # The fake's answers are well under 1000 bytes; judged by content they are complete.
    out = tmp_path / "run"
    r = sweep(tmp_path, files_args(repo, out), dispatch)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.getsize(out / "b01" / "out.md") < 1000
    assert "retrying" not in r.stdout


def test_batch_files_have_no_carriage_returns(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    assert sweep(tmp_path, files_args(repo, out) + ["--dry-run"], dispatch).returncode == 0
    for name in ("expected.txt", "key.txt"):
        assert b"\r" not in (out / "b01" / name).read_bytes()


def test_dry_run_builds_but_dispatches_nothing(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    r = sweep(tmp_path, files_args(repo, out) + ["--dry-run"], dispatch)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "manifest.json").exists()
    assert not (out / "b01" / "out.md").exists()


def test_a_missing_block_fails_the_sweep(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    r = sweep(tmp_path, files_args(repo, out), dispatch, extra={"FAKE_DROP_LAST": "1"})
    assert r.returncode == 1
    assert "missing" in r.stdout


def test_a_held_lock_refuses_a_second_sweep(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    (out / ".runlock").mkdir(parents=True)
    assert sweep(tmp_path, files_args(repo, out), dispatch).returncode == 10


def test_an_unknown_builder_is_a_usage_error(tmp_path, repo, dispatch):
    r = sweep(tmp_path, ["--builder", "nosuchbuilder", "--repo", posix(repo),
                         "--out", posix(tmp_path / "run")], dispatch)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "known:" in r.stdout + r.stderr


def test_a_missing_builder_argument_is_a_one_line_usage_error(tmp_path, repo, dispatch):
    r = sweep(tmp_path, ["--builder", "files", "--repo", posix(repo),
                         "--out", posix(tmp_path / "run")], dispatch)
    assert r.returncode == 2
    assert "--glob" in r.stdout + r.stderr
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("glob", ["scr/*.py", "src/empty.py"], ids=["no-match", "all-withheld"])
def test_a_sweep_that_would_audit_nothing_fails(tmp_path, repo, dispatch, glob):
    (repo / "src" / "empty.py").write_text("", encoding="utf-8")
    r = sweep(tmp_path, files_args(repo, tmp_path / "run", glob=glob), dispatch)
    assert r.returncode == 9, r.stdout + r.stderr
    assert "nothing to audit" in r.stdout + r.stderr
    ok = sweep(tmp_path, files_args(repo, tmp_path / "run2", glob=glob) + ["--allow-empty"], dispatch)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_a_failed_preflight_stops_before_anything_is_dispatched(tmp_path, repo, dispatch):
    out = tmp_path / "run"
    r = sweep(tmp_path, files_args(repo, out), dispatch, extra={"FAKE_PREFLIGHT_RC": "3"})
    assert r.returncode == 3
    assert "cannot reach the fake server" in r.stderr
    assert not (out / "b01").exists()


def test_a_small_context_window_shrinks_the_budgets(tmp_path, repo, dispatch):
    r = sweep(tmp_path, files_args(repo, tmp_path / "run") + ["--dry-run"], dispatch,
              extra={"FAKE_CTX": "32768"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "batch=%d item=%d" % ((32768 - 20000) * 3, (32768 - 20000) * 3 // 2) in r.stdout


def test_the_default_run_dir_lives_under_the_home_cache(tmp_path, repo, dispatch):
    home = tmp_path / "home"
    home.mkdir()
    extra = {"HOME": posix(home), "USERPROFILE": str(home)}   # Windows python reads USERPROFILE
    r = sweep(tmp_path, ["--builder", "files", "--repo", posix(repo), "--glob", "src/*.py",
                         "--dry-run"], dispatch, extra=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    runs = list((home / ".cache" / "qwen-sweep").glob("*/run-*"))
    assert len(runs) == 1 and (runs[0] / "manifest.json").exists()


def test_a_lib_package_in_the_working_directory_is_not_imported(tmp_path, repo, dispatch):
    here = tmp_path / "elsewhere"
    (here / "lib").mkdir(parents=True)
    (here / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (here / "lib" / "engine.py").write_text("raise SystemExit('wrong lib imported')\n",
                                            encoding="utf-8")
    r = sweep(tmp_path, files_args(repo, tmp_path / "run"), dispatch, cwd=here)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrong lib" not in r.stdout + r.stderr


def test_version_flag(tmp_path):
    r = sweep(tmp_path, ["--version"])
    assert r.returncode == 0 and r.stdout.startswith("qwen-sweep ")
