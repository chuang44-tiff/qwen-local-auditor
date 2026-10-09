"""Depth is the DEFAULT: every direct qwen-agent run gets the depth switches that
fit it (--probe P when read-only, --role-variant deep V when the role has a deep
variant, --review-round R and --subagents-push N on every session -- the push
replaces the softer --subagents-nudge text), silently
dropping the ones that do not. --shallow (or QWEN_DEPTH=shallow) opts out.

Implied switches never cause a refusal; typed switches keep theirs. --json records
the mode as "depth" and the report too. The timeout default rises to 3600 whenever
depth is on. The fakes, the model server and the run helpers are test_cli's and
test_cli_deep's; every call here states its QWEN_DEPTH explicitly ("" is unset-deep,
so the default depth is what runs).
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

import test_cli
from test_cli import _dry_argv, _git_repo, flag, posix, run, same_path
from test_cli_deep import calls, dirty_repo, go, probe_runs, sys_prompt, tree_state

# QWEN_DEPTH="" behaves as unset (`${QWEN_DEPTH:-deep}`), i.e. the new default.
DEEP_ENV = {"QWEN_DEPTH": ""}

FAKE_LONG = r'''#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
d="$FAKE_DIR"
n=$(cat "$d/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/n"
printf '%s\0' "$@" > "$d/argv.$n"
for _a in "$@"; do case "$_a" in *.json) [ -f "$_a" ] && cp "$_a" "$d/cfg.$n.$(basename "$_a")" ;; esac; done
# advcall:K -- act like a session that made K advisor calls (writes the server's records)
case "$(printf '%s' "${FAKE_MODES:-}" | cut -d, -f"$n")" in advcall)
  for _a in "$@"; do case "$_a" in *advisor.json)
    st="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["mcpServers"]["qla_advisor"]["env"]["QA_ADVISOR_STATE"])' "$_a")"
    printf '%s\n' '{"n": 1, "model": "claude-opus-test", "seconds": 4.5, "cost_usd": 0.1, "unavailable": null}' \
                  '{"n": 2, "model": "opus", "seconds": 1.0, "cost_usd": null, "unavailable": "no answer within 600s"}' >> "$st/calls.jsonl" ;;
  esac; done ;;
esac
pwd -P > "$d/pwd.$n"
env | grep -E '^(QWEN_TEST_|CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=)' | sort > "$d/env.$n"
mode="$(printf '%s' "${FAKE_MODES:-ok}" | cut -d, -f"$n")"
[ -n "$mode" ] || mode=ok
answer() {
  printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}\n' "$n" "$n"
}
case "$mode" in
  ok)        answer ;;
  edit)      cat a.txt u.txt > "$d/seen.$n" 2>/dev/null
             printf 'probe edit\n' > a.txt; printf 'new\n' > made-by-session.txt; answer ;;
  lock)      mkdir -p locked && : > locked/f && chmod 500 locked; answer ;;
  apierr)    printf '%s\n' '{"type":"result","is_error":true,"api_error_status":400,"result":"API Error: 400"}' ;;
  repro)     wt="$QWEN_TEST_WORKTREE"; command -v cygpath >/dev/null 2>&1 && wt="$(cygpath -u "$wt")"
             printf 'def test_repro():\n    assert False\n' > "$wt/test_repro.py"; answer ;;
  sleep)     sleep 30; answer ;;
  advcall)   answer ;;
  # deny:TOOL+TOOL -- a finished run whose record lists those tools as denied.
  deny:*)    den=""; for x in $(printf '%s' "${mode#deny:}" | tr '+' ' '); do
               den="$den${den:+,}{\"tool_name\":\"$x\",\"tool_input\":{}}"; done
             printf '{"type":"result","subtype":"success","is_error":false,"num_turns":3,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[%s]}\n' "$n" "$n" "$den" ;;
  # usage:OUT_TOKENS:TURNS -- a short answer after many turns, the auto-compact
  # death signature the short-answer guard watches for.
  usage:*)   o="${mode#usage:}"; out="${o%%:*}"; turns="${o##*:}"
             printf '{"type":"result","subtype":"success","is_error":false,"num_turns":%s,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":%s},"permission_denials":[]}\n' "$turns" "$n" "$n" "$out" ;;
  # long:CHARS -- a result of exactly CHARS characters, so a review round can come
  # back far shorter than the answer it was sent to check.
  long:*)    r="$(printf 'r%.0s' $(seq 1 "${mode#long:}"))"
             printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"%s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}\n' "$r" "$n" ;;
esac
'''


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), test_cli._Models)
    httpd.models = [{"id": "local-model", "object": "model", "max_model_len": 262144}]
    httpd.payload = None
    httpd.key = None
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fake(tmp_path):
    p = tmp_path / "fake-long"
    p.write_text(FAKE_LONG, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "calls").mkdir()
    return p


# ------------------------------------------------------------------ the implied set

def test_auditor_default_is_full_deep(tmp_path, server, fake):
    # The auditor gets exactly what --deep means, by default.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--json", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    # depth implies PUSH now, not NUDGE (the push replaces the nudge text)
    assert rec["qwen_agent"]["switches"] == {"probe": True, "role_variant": "deep",
                                             "review_round": True, "subagents_nudge": False,
                                             "subagents_push": True}
    assert rec["qwen_agent"]["depth"] == "default"
    assert rec["result"] == "answer 2"
    assert rec["qwen_agent"]["review_round"]["status"] == "ok"
    assert "suspiciously short" not in r.stderr        # one turn is not the guard
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert same_path(c1).startswith(same_path(os.path.realpath(str(tmp_path / "probes"))))
    assert c1 == c2                                      # the review round ran in the sandbox
    p = sys_prompt(a1)
    assert "DEEP audit" in p and "throwaway copy of the project" in p
    assert "Delegation is part of this task" in p
    assert "Delegate more than feels necessary." not in p
    assert "Task" in flag(a1, "--tools").split(",")
    assert a2[-4:-1] == ["--resume", "sess-1", "--"]
    assert probe_runs(tmp_path) == []                    # implied depth cleans up too


def test_coder_write_default_has_no_probe(tmp_path, server, fake):
    # A writing run is not read-only: P steps aside, V, R and N still come.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "coder", "-C", posix(repo), "hi"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert same_path(c1) == same_path(repo) and probe_runs(tmp_path) == []
    assert "--restricted" not in a1
    p = sys_prompt(a1)
    assert "EDGE CASES" in p and "Delegation is part of this task" in p
    assert "Delegate more than feels necessary." not in p
    assert "throwaway copy" not in p
    assert a2[-4:-1] == ["--resume", "sess-1", "--"]      # the review round still ran


def test_plain_read_only_default_gets_probe_review_nudge(tmp_path, server, fake):
    # No role at all: no deep variant exists, so V is silently dropped; P, R, N fit.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-C", posix(repo), "hi"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert same_path(c1).startswith(same_path(os.path.realpath(str(tmp_path / "probes"))))
    assert same_path(c2) == same_path(c1)
    p = sys_prompt(a1)
    assert "throwaway copy of the project" in p and "Delegation is part of this task" in p
    assert "Delegate more than feels necessary." not in p
    assert probe_runs(tmp_path) == []


def test_implied_probe_steps_aside_for_explicit_toolset(tmp_path, server, fake):
    # An explicit toolset is the caller taking over the tool policy: implied P must
    # step aside, not refuse (implied parts never refuse). The rest still comes.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-t", "Read", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "cannot be combined" not in r.stderr
    assert probe_runs(tmp_path) == []
    (a1, c1), (a2, _) = calls(tmp_path)
    assert same_path(c1) == same_path(repo)
    assert "throwaway copy" not in sys_prompt(a1)
    assert a2[-4:-1] == ["--resume", "sess-1", "--"]      # V, R and N were still implied


# ------------------------------------------------- where the implied probe steps aside

def test_default_auditor_test_keeps_repro_files(tmp_path, server, fake):
    # A --test run gets no implied sandbox AT ALL (not even a stepped-aside one):
    # the reproduction test lives in the test worktree and a sandbox deletes
    # itself at exit -- it would take the test with it. ## REPRO FILES still
    # returns the file for you or local-coder to adopt, and qwen-test is still
    # the only shell command the run is granted.
    repo = _git_repo(tmp_path / "repo")
    r = go(tmp_path, ["-r", "auditor", "--test", "-C", posix(repo), "hi"], server, fake,
           modes="repro",
           extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
                  **DEEP_ENV})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "## REPRO FILES" in r.stdout and "### test_repro.py" in r.stdout
    assert "no sandbox for this run" not in r.stderr        # P never applied, nothing to undo
    assert probe_runs(tmp_path) == []
    a1 = calls(tmp_path)[0][0]
    bash_grants = [g for g in flag(a1, "--allowed-tools").split(",") if g.startswith("Bash")]
    assert bash_grants == ["Bash(qwen-test:*)"]             # the fence is intact, no full shell
    for _, cwd in calls(tmp_path):
        assert same_path(cwd) == same_path(repo)            # the worktree, not a sandbox, is the shell


def _ignored_build_repo(tmp_path):
    """A committed repo with a gitignored build/ holding an untracked file: build/
    is never copied into a sandbox, so -C build is not part of the copy."""
    repo = _git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore"],
                   check=True, capture_output=True)
    (repo / "build").mkdir()
    (repo / "build" / "b.txt").write_text("b\n", encoding="utf-8")
    return repo


def test_implied_probe_steps_aside_on_ignored_dir(tmp_path, server, fake):
    # The shell's cheap pre-check cannot see every refusal probe.py makes. When an
    # IMPLIED --probe meets one at create, the run does not die: one note on stderr
    # and the session runs unsandboxed, exactly the --shallow run it would have been.
    repo = _ignored_build_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo / "build"), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "depth: no sandbox for this run (" in r.stderr
    assert "an ignored directory is not copied into the sandbox" in r.stderr
    assert "); running without --probe" in r.stderr
    assert probe_runs(tmp_path) == []                       # the half-made run folder is gone
    for _, cwd in calls(tmp_path):
        assert same_path(cwd) == same_path(repo / "build")  # every call, review round included
    assert "--restricted" not in calls(tmp_path)[0][0]      # no probe fence left standing


def test_implied_probe_steps_aside_on_test_repo_mismatch(tmp_path, server, fake):
    # The other shape: --test-repo names the tree to copy, and -C sits in a
    # different repo. probe.py refuses the combination; an implied P turns the
    # refusal into "no sandbox". (--test itself never reaches this -- a --test run
    # gets no implied sandbox -- so --test-repo is passed without --test here.)
    r1 = _git_repo(tmp_path / "r1")
    r2 = _git_repo(tmp_path / "r2")
    r = go(tmp_path, ["-r", "auditor", "-C", posix(r1), "--test-repo", posix(r2), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "depth: no sandbox for this run (" in r.stderr
    assert "is not inside" in r.stderr and "); running without --probe" in r.stderr
    assert probe_runs(tmp_path) == []
    for _, cwd in calls(tmp_path):
        assert same_path(cwd) == same_path(r1)


def test_typed_probe_still_refuses_ignored_dir(tmp_path, server, fake):
    # Typed switches keep every refusal: the very tree that only costed the implied
    # probe its sandbox ends a typed --probe with exit 2 and no session at all.
    repo = _ignored_build_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo / "build"), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--probe:" in r.stderr
    assert "no sandbox for this run" not in r.stderr        # a refusal, not a step aside
    assert calls(tmp_path) == []
    assert probe_runs(tmp_path) == []


def test_deep_probe_steps_aside_on_ignored_dir(tmp_path, server, fake):
    # --deep's --probe is the IMPLIED one (only a typed --probe is typed -- the
    # refusal-naming block says so), so create's refusal steps it aside the same
    # way: one note, unsandboxed, and deep's other three switches stand.
    repo = _ignored_build_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "-C", posix(repo / "build"), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "depth: no sandbox for this run (" in r.stderr
    assert "an ignored directory is not copied into the sandbox" in r.stderr
    assert "); running without --probe" in r.stderr
    assert probe_runs(tmp_path) == []
    (a1, c1), _ = calls(tmp_path)                           # round + review, no sandbox
    assert same_path(c1) == same_path(repo / "build")
    assert "DEEP audit" in sys_prompt(a1)                   # deep's other switches stand
    assert "Delegation is part of this task" in sys_prompt(a1)


def test_deep_test_step_aside_keeps_the_test_fence(tmp_path, server, fake):
    # --deep brings its --probe to a --test run too (only an IMPLIED P excludes
    # --test), so this is the shape where a step aside could erode the --test
    # fence: dontAsk there is fixed by --test itself, not pinned by the probe, and
    # undoing the probe's default must leave it standing. The stepped-aside run is
    # exactly a --shallow --test run: --restricted, dontAsk, qwen-test as its only
    # shell command, and the repro file comes back.
    repo = _ignored_build_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "--test", "-C", posix(repo / "build"), "hi"],
           server, fake, modes="repro",
           extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
                  **DEEP_ENV})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "depth: no sandbox for this run (" in r.stderr
    assert "); running without --probe" in r.stderr
    assert probe_runs(tmp_path) == []
    assert "## REPRO FILES" in r.stdout and "### test_repro.py" in r.stdout
    a1 = calls(tmp_path)[0][0]
    assert "--restricted" in a1
    assert flag(a1, "--permission-mode") == "dontAsk"             # the fence, not emptied
    bash_grants = [g for g in flag(a1, "--allowed-tools").split(",") if g.startswith("Bash")]
    assert bash_grants == ["Bash(qwen-test:*)"]                    # not the probe's full shell


def test_deep_write_run_still_refuses_an_unbuildable_sandbox(tmp_path, server, fake):
    # The one shape that keeps the exit 2: --deep on a WRITING run promises the
    # sandbox -- the edits come back as a patch, the tree stays untouched -- so a
    # sandbox that cannot be built ends the run instead of sending the writes to
    # the user's tree.
    repo = _ignored_build_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "coder", "-C", posix(repo / "build"), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--probe:" in r.stderr
    assert "no sandbox for this run" not in r.stderr
    assert calls(tmp_path) == []
    assert probe_runs(tmp_path) == []


@pytest.fixture
def fake_unrestricted(tmp_path):
    """FAKE_LONG for a Claude Code whose --help has no --restricted."""
    p = tmp_path / "fake-unrestricted"
    p.write_text(FAKE_LONG.replace('echo "  --restricted  Restricted mode"', 'echo "  --print"'),
                 encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "calls").mkdir(exist_ok=True)
    return p


def test_implied_probe_steps_aside_without_restricted(tmp_path, server, fake_unrestricted):
    # A Claude Code too old for the probe fence costs a bare run its sandbox, not
    # the run: one note, then the read-only run it would have been under --shallow.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake_unrestricted,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.count("depth: no sandbox for this run (") == 1
    assert "--restricted" in r.stderr and "upgrade claude" not in r.stderr
    assert probe_runs(tmp_path) == []
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert flag(a1, "--tools").split(",")[:3] == ["Read", "Glob", "Grep"]
    assert "Bash" not in flag(a1, "--tools").split(",")
    assert "--restricted" not in a1 and flag(a1, "--permission-mode") is None
    assert same_path(c1) == same_path(repo) and same_path(c2) == same_path(repo)


def test_typed_probe_still_refuses_without_restricted(tmp_path, server, fake_unrestricted):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server,
           fake_unrestricted, extra=dict(DEEP_ENV))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--probe needs a Claude Code with --restricted" in r.stderr
    assert calls(tmp_path) == []


def _unusable_probe_dir(tmp_path):
    """A QWEN_PROBE_DIR under a regular file: creating it fails with an OSError,
    which probe.py create reports as a broken environment (rc 8), not a refusal."""
    f = tmp_path / "not-a-dir"
    f.write_text("x\n", encoding="utf-8")
    return posix(f / "probes")


def test_implied_probe_steps_aside_when_probe_dir_unusable(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_PROBE_DIR=_unusable_probe_dir(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.count("depth: no sandbox for this run (") == 1
    assert "); running without --probe" in r.stderr
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert "Bash" not in flag(a1, "--tools").split(",")
    assert "--restricted" not in a1
    assert same_path(c1) == same_path(repo) and same_path(c2) == same_path(repo)


def test_typed_probe_still_fails_when_probe_dir_unusable(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_PROBE_DIR=_unusable_probe_dir(tmp_path)))
    assert r.returncode == 8, r.stdout + r.stderr
    assert "--probe:" in r.stderr and "no sandbox for this run" not in r.stderr
    assert calls(tmp_path) == []


def test_deep_write_run_still_fails_when_probe_dir_unusable(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "coder", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_PROBE_DIR=_unusable_probe_dir(tmp_path)))
    assert r.returncode == 8, r.stdout + r.stderr
    assert "no sandbox for this run" not in r.stderr
    assert calls(tmp_path) == []


# cygpath as Git Bash's behaves where it matters here: it refuses an empty path and a
# path that runs through a regular file (printing nothing), else hands the path back.
STRICT_CYGPATH = r'''#!/usr/bin/env bash
p="${@: -1}"
[ -n "$p" ] || { echo "cygpath: can't convert empty path" >&2; exit 1; }
q="$p"
while [ "${q%/*}" != "$q" ] && [ -n "${q%/*}" ]; do
  q="${q%/*}"
  [ -f "$q" ] && { echo "cygpath: error converting \"$p\" - Not a directory" >&2; exit 1; }
done
printf '%s\n' "$p"
'''


@pytest.mark.skipif(os.name == "nt", reason="simulates Git Bash's cygpath; Windows has the real one")
def test_unusable_probe_dir_is_not_swapped_for_the_default_on_git_bash(tmp_path, server, fake):
    # cygpath printing nothing for the unusable directory once left QWEN_PROBE_DIR
    # empty, and probe.py then built the sandbox in the DEFAULT probe directory: the
    # typed --probe ran instead of failing, somewhere the caller never named.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cyg = bindir / "cygpath"
    cyg.write_text(STRICT_CYGPATH, encoding="utf-8", newline="\n")
    cyg.chmod(0o755)
    cache = tmp_path / "cache"
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_PROBE_DIR=_unusable_probe_dir(tmp_path),
                      XDG_CACHE_HOME=posix(cache),
                      PATH="%s%s%s" % (posix(bindir), os.pathsep, os.environ["PATH"])))
    assert r.returncode == 8, r.stdout + r.stderr
    assert calls(tmp_path) == []
    assert not cache.exists()                           # nothing built in the default
    assert "can't convert empty path" not in r.stderr   # no empty path reaches cygpath


def test_implied_probe_through_a_symlinked_cd(tmp_path, server, fake):
    # -C spelled through a symlink names the same work tree: the implied probe
    # applies, it does not quietly step aside over a spelling.
    repo = dirty_repo(tmp_path)
    link = tmp_path / "link"
    try:
        os.symlink(str(repo), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symlink here")
    r = go(tmp_path, ["-r", "auditor", "-C", posix(link), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no sandbox for this run" not in r.stderr
    (a1, c1), _ = calls(tmp_path)
    assert same_path(c1).startswith(same_path(os.path.realpath(str(tmp_path / "probes"))))
    assert probe_runs(tmp_path) == []


FAKE_DRIVE_GIT = r'''#!/usr/bin/env bash
# Git for Windows prints the top level as C:/...; this one answers the same way.
case " $* " in
  *" rev-parse --show-toplevel "*) out="$("$REAL_GIT" "$@")" || exit $?
                                   printf 'C:%s\n' "$out"; exit 0 ;;
esac
exec "$REAL_GIT" "$@"
'''

FAKE_CYGPATH = r'''#!/usr/bin/env bash
# -u turns C:/x into /x (this test's drive is the root); -w and -m keep the path.
p="$2"
[ "$1" = -u ] && case "$p" in [A-Za-z]:/*) p="${p#?:}" ;; esac
printf '%s\n' "$p"
'''


@pytest.mark.skipif(os.name != "posix", reason="the fake git and cygpath are bash scripts")
def test_implied_probe_with_drive_letter_toplevel(tmp_path, fake):
    # Under Git Bash the -C directory is /c/... while git names its top level C:/...;
    # the two spellings of one tree must not cost the run its implied probe.
    real_git = shutil.which("git")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("git", FAKE_DRIVE_GIT), ("cygpath", FAKE_CYGPATH)):
        (bindir / name).write_text(text, encoding="utf-8", newline="\n")
        (bindir / name).chmod(0o755)
    repo = dirty_repo(tmp_path)
    r = run(tmp_path, ["--dry-run", "-r", "auditor", "-C", posix(repo), "hi"], None, fake,
            extra=dict(DEEP_ENV, REAL_GIT=real_git, QWEN_PROBE_DIR=posix(tmp_path / "probes"),
                       PATH=str(bindir) + os.pathsep + os.environ["PATH"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# probe: the sandbox is made at run time" in r.stdout


def test_relative_probe_dir_is_the_callers(tmp_path, server, fake):
    # A relative QWEN_PROBE_DIR resolves against the caller's directory, as
    # QWEN_OUTDIR does -- never against -C, which would put it inside the tree.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "coder", "--write", "-C", posix(repo), "hi"], server,
           fake, modes="edit", extra=dict(DEEP_ENV, QWEN_PROBE_DIR="rel-probes",
                                          QWEN_OUTDIR=""))
    assert r.returncode == 0, r.stdout + r.stderr
    (a1, c1), = calls(tmp_path)[:1]
    assert same_path(c1).startswith(same_path(os.path.realpath(str(tmp_path / "rel-probes"))))
    patches = list((tmp_path / "rel-probes").glob("qwen-agent-*.patch"))
    assert len(patches) == 1 and "probe edit" in patches[0].read_text(encoding="utf-8")
    assert not (repo / "rel-probes").exists()


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions that bind the user")
def test_a_sandbox_that_cannot_be_removed_is_reported(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           modes="lock", extra={"QWEN_DEPTH": "shallow"})
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "sandbox not removed: " in r.stderr
        assert probe_runs(tmp_path) != []
    finally:
        # the failed removal also stripped permissions on the way up: restore them all
        subprocess.run(["chmod", "-R", "u+rwx", str(tmp_path / "probes")], check=False)


def test_typed_nudge_is_kept_under_default_depth(tmp_path, server, fake):
    # A typed --subagents-nudge is the caller's choice of delegation text: depth
    # does not turn it into the push, as --until-done does not either.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--subagents-nudge", "--json", "-C", posix(repo), "hi"],
           server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    sw = json.loads(r.stdout)["qwen_agent"]["switches"]
    assert sw["subagents_nudge"] is True and sw.get("subagents_push", False) is False
    p = sys_prompt(calls(tmp_path)[0][0])
    assert "Delegate more than feels necessary." in p
    assert "Delegation is part of this task" not in p


def test_mechanic_default_gets_review_and_push_but_no_probe(tmp_path, server, fake):
    # -r mechanic writes, so the probe never applies; the review round and the push do.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "mechanic", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert same_path(c1) == same_path(repo) and probe_runs(tmp_path) == []
    assert "--restricted" not in a1 and "throwaway copy" not in sys_prompt(a1)
    assert "Delegation is part of this task" in sys_prompt(a1)
    assert "Task" in flag(a1, "--tools").split(",")
    assert a2[-4:-1] == ["--resume", "sess-1", "--"]


def test_toolset_none_default_is_one_plain_call(tmp_path, server, fake):
    # No tools: nothing to probe, nothing to delegate with, nothing to review with.
    r = go(tmp_path, ["--toolset", "none", "hi"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stdout + r.stderr
    (argv, cwd), = calls(tmp_path)
    assert "Task" not in (flag(argv, "--tools") or "").split(",")
    assert "Delegation is part of this task" not in sys_prompt(argv)
    assert "--resume" not in argv and probe_runs(tmp_path) == []


def test_config_file_depth_shallow_is_one_plain_call(tmp_path, server, fake):
    cfg = tmp_path / "config"
    cfg.write_text('QWEN_DEPTH="shallow"\n', encoding="utf-8")
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, QWEN_CONFIG=posix(cfg)))
    assert r.returncode == 0, r.stdout + r.stderr
    (argv, cwd), = calls(tmp_path)
    assert flag(argv, "--tools") == "Read,Glob,Grep"
    assert same_path(cwd) == same_path(repo) and probe_runs(tmp_path) == []
    assert "DEEP audit" not in sys_prompt(argv)


# ------------------------------------------------------------------ opting out

def test_shallow_restores_old_argv(tmp_path, server, fake):
    # --shallow beats an environment that asks for depth: exactly the pre-depth run.
    r = go(tmp_path, ["--shallow", "--json", "hi"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "qwen_agent" not in json.loads(r.stdout)        # no switches, no record key
    (argv, cwd), = calls(tmp_path)
    assert flag(argv, "--tools") == "Read,Glob,Grep"
    assert "Task" not in flag(argv, "--allowed-tools").split(",")
    assert "--restricted" not in argv
    assert "Delegate more than feels necessary." not in sys_prompt(argv)
    assert argv[-2:] == ["--", "hi"]
    assert same_path(cwd) == same_path(tmp_path)


def test_qwen_depth_shallow_env(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--json", "hi"], server, fake,
           extra={"QWEN_DEPTH": "shallow"})
    assert r.returncode == 0, r.stderr
    assert "qwen_agent" not in json.loads(r.stdout)
    (argv, cwd), = calls(tmp_path)
    assert "--restricted" not in argv and "DEEP audit" not in sys_prompt(argv)
    assert same_path(cwd) == same_path(tmp_path)
    assert len(calls(tmp_path)) == 1                       # no implied review round either


def test_qwen_depth_bad_value(tmp_path, server, fake):
    r = go(tmp_path, ["hi"], server, fake, extra={"QWEN_DEPTH": "wide"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "QWEN_DEPTH" in r.stderr and "wide" in r.stderr
    assert calls(tmp_path) == []                           # claude never ran


def test_shallow_and_deep_conflict(tmp_path, server, fake):
    r = go(tmp_path, ["--shallow", "--deep", "-r", "auditor", "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--shallow" in r.stderr and "--deep" in r.stderr
    assert calls(tmp_path) == []


def test_interactive_and_resume_get_no_implied_parts(tmp_path, server, fake):
    r = run(tmp_path, ["--interactive", "--dry-run"], None, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "# probe:" not in r.stdout and "--restricted" not in r.stdout
    assert "Delegate more than feels necessary." not in r.stdout
    r = go(tmp_path, ["--json", "--resume", "sess-old", "go on"], server, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, _), = calls(tmp_path)                             # no review round onto a resume
    assert flag(a1, "--resume") == "sess-old"
    assert "throwaway copy" not in sys_prompt(a1)
    assert probe_runs(tmp_path) == []
    sw = json.loads(r.stdout)["qwen_agent"]
    assert sw["depth"] == "default"
    assert sw["switches"]["probe"] is False and sw["switches"]["review_round"] is False
    assert sw["switches"]["subagents_nudge"] is False and not sw["switches"]["role_variant"]


def test_auditor_resume_has_no_deep_text(tmp_path, server, fake):
    # --resume continues the session that ran; swapping the deep role text in
    # mid-session is wrong, so a resumed auditor gets no implied part -- the role
    # that HAS a deep variant included.
    r = go(tmp_path, ["-r", "auditor", "--json", "--resume", "sess-old", "go on"], server,
           fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, _), = calls(tmp_path)                              # one call: no review round on a resume
    assert flag(a1, "--resume") == "sess-old"
    p = sys_prompt(a1)
    assert "DEEP audit" not in p and "throwaway copy" not in p
    assert "Delegate more than feels necessary." not in p
    assert "Task" not in flag(a1, "--allowed-tools").split(",")
    assert probe_runs(tmp_path) == []
    sw = json.loads(r.stdout)["qwen_agent"]
    assert sw["depth"] == "default" and not sw["switches"]["role_variant"]


def test_dry_run_shows_review_and_nudge(tmp_path, fake):
    # --dry-run prints "the argv, exactly as exec'd": the delegation push and Task ARE
    # part of that argv, so a depth dry run must show them; the review round is the
    # second call a real run would make, so it is noted rather than printed as a flag.
    r = run(tmp_path, ["--dry-run", "-r", "auditor", "hi"], None, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "# review round: one --resume call after the first" in r.stdout
    argv = _dry_argv(r)
    assert "Task" in flag(argv, "--tools").split(",")
    assert "Task" in flag(argv, "--allowed-tools").split(",")
    assert "Delegation is part of this task" in r.stdout    # in the printed prompt (multiline)
    assert "Delegate more than feels necessary." not in r.stdout
    assert "--review-round" not in argv                        # noted, not passed to claude
    r = run(tmp_path, ["--shallow", "--dry-run", "-r", "auditor", "hi"], None, fake,
            extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "# review round" not in r.stdout
    argv = _dry_argv(r)
    assert "Task" not in flag(argv, "--tools").split(",")
    assert "Delegate more than feels necessary." not in r.stdout
    assert "Delegation is part of this task" not in r.stdout


def test_default_timeout_3600_when_deep(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "hi"], None, fake, extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "# timeout: 3600" in r.stdout
    r = run(tmp_path, ["--shallow", "--dry-run", "hi"], None, fake, extra=dict(DEEP_ENV))
    assert "# timeout: 1800" in r.stdout
    r = run(tmp_path, ["--dry-run", "--timeout", "120", "hi"], None, fake, extra=dict(DEEP_ENV))
    assert "# timeout: 120" in r.stdout                    # a typed --timeout still wins
    r = run(tmp_path, ["--dry-run", "hi"], None, fake,
            extra={"QWEN_DEPTH": "", "QWEN_TIMEOUT": "45"})
    assert "# timeout: 45" in r.stdout                     # and QWEN_TIMEOUT still wins


# ------------------------------------------------------------------ sandbox grants

def test_auditor_probe_grants_write_in_sandbox(tmp_path, server, fake):
    # The implied sandbox is throwaway: the auditor keeps Write and Edit there, so
    # probing with a scratch file is a tool call, not a denial (exit 7).
    repo = dirty_repo(tmp_path)
    before = tree_state(repo)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           modes="edit", extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr                     # the scratch write was allowed
    (a1, c1), (_, c2) = calls(tmp_path)
    assert same_path(c1).startswith(same_path(os.path.realpath(str(tmp_path / "probes"))))
    assert same_path(c1) == same_path(c2)
    assert {"Edit", "Write", "Bash"} <= set(flag(a1, "--tools").split(","))
    assert "Write" in flag(a1, "--allowed-tools").split(",")
    assert "Edit" in flag(a1, "--allowed-tools").split(",")
    assert (tmp_path / "calls" / "seen.1").read_text(encoding="utf-8") == "dirty\nuntracked\n"
    assert tree_state(repo) == before                      # only the copy was written


# ------------------------------------------------------------------ short answers

SHORT_MSG = ("WARNING: the answer is suspiciously short for a long session "
             "(possible auto-compact failure); check it")


def test_short_answer_warning(tmp_path, server, fake):
    r = go(tmp_path, "--shallow --json hi".split(), server, fake,
           modes="usage:5:25", extra={"QWEN_DEPTH": "shallow"})
    assert r.returncode == 0, r.stderr
    assert SHORT_MSG in r.stderr.splitlines()              # printed exactly
    rec = json.loads(r.stdout)
    assert rec["num_turns"] == 25
    assert rec["qwen_agent"]["short_answer_warning"] is True
    assert rec["qwen_agent"]["depth"] == "shallow"
    # A short answer in a short session stays quiet.
    r = go(tmp_path, "--shallow hi".split(), server, fake, modes="ok",
           extra={"QWEN_DEPTH": "shallow"})
    assert r.returncode == 0 and SHORT_MSG not in r.stderr


def test_short_review_answer_keeps_first(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "--json", "hi"], server, fake,
           modes="long:1200,ok", extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    assert "WARNING: --review-round" in r.stderr and "suspiciously short" in r.stderr
    rec = json.loads(r.stdout)
    assert rec["result"] == "r" * 1200 and rec["session_id"] == "sess-1"   # the first answer
    rr = rec["qwen_agent"]["review_round"]
    assert rr["status"] == "failed" and "suspiciously short" in rr["warning"]
    assert SHORT_MSG not in r.stderr                       # one warning, not two
    assert len(calls(tmp_path)) == 2


# ------------------------------------------------------------------ git steering env

FAKE_GIT = r'''#!/usr/bin/env bash
# Records the GIT_DIR each call was made with; --help is the capability probe,
# answered without recording.
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
printf 'GIT_DIR=%s\n' "${GIT_DIR-<unset>}" >> "$FAKE_RECORD"
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"ok","session_id":"s1","usage":{"input_tokens":1,"output_tokens":1},"permission_denials":[]}\n'
'''


def test_git_env_kept_for_write_run(tmp_path, server):
    # Clearing GIT_* is PROBE hygiene, not depth hygiene: it fires where a sandbox
    # of this run is used and nowhere else. A --write run has no sandbox and no
    # reason to lose the caller's git steering (bare-repo dotfiles, a hook's GIT_*).
    g = tmp_path / "fake-git"
    g.write_text(FAKE_GIT, encoding="utf-8", newline="\n")
    g.chmod(0o755)
    repo = _git_repo(tmp_path / "repo")
    wd = posix(tmp_path / "nowhere.git")

    def recorded():
        return (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()

    r = run(tmp_path, ["-r", "coder", "--write", "-C", posix(repo), "hi"], server, g,
            extra=dict(DEEP_ENV, GIT_DIR=wd))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = recorded()
    assert lines and all(ln == "GIT_DIR=%s" % wd for ln in lines)   # every call, kept
    (tmp_path / "record.txt").unlink()
    r = run(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server, g,
            extra=dict(DEEP_ENV, GIT_DIR=wd, QWEN_PROBE_DIR=posix(tmp_path / "probes")))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = recorded()
    assert lines and all(ln == "GIT_DIR=<unset>" for ln in lines)   # sandboxed: cleared
    assert probe_runs(tmp_path) == []


# ------------------------------------------------------------------ --until-done

def _until_done_argv(tmp_path, args, depth_env="", extra=None):
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\n[ "$1" = -c ] && exit 0\n'
                      'printf "ENV:QWEN_TIMEOUT=%s\\n" "${QWEN_TIMEOUT-<unset>}" > "$FAKE_RECORD"\n'
                      'for a in "$@"; do printf "ARG:%s\\n" "$a"; done >> "$FAKE_RECORD"\n',
                      encoding="utf-8", newline="\n")
    fakepy.chmod(0o755)
    r = run(tmp_path, ["--until-done", "t.md", *args], None, None,
            extra={"QWEN_PYTHON": posix(fakepy), "QWEN_DEPTH": depth_env, **(extra or {})})
    if r.returncode != 0:
        return r, None
    lines = (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
    return r, [ln[4:] for ln in lines if ln.startswith("ARG:")]


def _round_timeout_env(tmp_path):
    """The QWEN_TIMEOUT the exec'd supervisor (and with it every round and audit
    call) was handed -- recorded by _until_done_argv's fake python."""
    lines = [ln for ln in (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
             if ln.startswith("ENV:")]
    assert len(lines) == 1
    return lines[0][len("ENV:QWEN_TIMEOUT="):]


def test_until_done_default_deep_rounds(tmp_path):
    # The coder rounds get V + R + N -- --deep without P -- and P only when typed.
    r, argv = _until_done_argv(tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert "--review-round" in argv[:sep]
    assert "--probe" not in argv[:sep] and "--keep-sandbox" not in argv[:sep]
    assert argv[sep + 1:] == ["--role-variant", "deep", "--subagents-push"]
    assert argv[:sep][argv[:sep].index("--depth") + 1] == "default"
    # --shallow keeps the plain loop.
    r, argv = _until_done_argv(tmp_path, [], depth_env="shallow")
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert "--review-round" not in argv[:sep] and argv[sep + 1:] == []


def test_until_done_round_timeout_3600(tmp_path):
    # The rounds ARE the deep work of a depth loop, so the shell hands them the 3600 s
    # per-round default through QWEN_TIMEOUT in the environment the supervisor and
    # every round inherits (each runs shallow and would otherwise cut back to 1800).
    # A typed --timeout or QWEN_TIMEOUT still wins; a shallow loop hands nothing.
    r, argv = _until_done_argv(tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _round_timeout_env(tmp_path) == "3600"
    r, argv = _until_done_argv(tmp_path, ["--timeout", "120"])
    assert r.returncode == 0, r.stdout + r.stderr
    tail = argv[argv.index("--") + 1:]
    assert tail.count("--timeout") == 1 and "120" in tail          # the typed flag, once
    assert _round_timeout_env(tmp_path) == "<unset>"               # the flag does the work
    r, argv = _until_done_argv(tmp_path, [], extra={"QWEN_TIMEOUT": "45"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _round_timeout_env(tmp_path) == "45"                    # QWEN_TIMEOUT wins
    assert "--timeout" not in argv[argv.index("--") + 1:]          # not typed into its teeth
    r, argv = _until_done_argv(tmp_path, [], depth_env="shallow")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _round_timeout_env(tmp_path) == "<unset>"               # a shallow loop keeps 1800
    assert "--timeout" not in argv[argv.index("--") + 1:]


# ------------------------------------------------------- the supervisor side

SHIM = r"""
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["SHIM_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "depth": os.environ.get("QWEN_DEPTH")}) + "\n")
with open(os.path.join(argv[argv.index("-C") + 1], "value.txt"), "w", encoding="utf-8") as fh:
    fh.write("good\n")
print(json.dumps({"type": "result", "is_error": False,
                  "result": os.environ.get("SHIM_RESULT", "fixed"),
                  "session_id": "s1", "usage": {"input_tokens": 5, "output_tokens": 5},
                  "permission_denials": []}))
"""


def test_supervisor_runs_rounds_shallow_and_records_depth(tmp_path, monkeypatch):
    # --until-done decomposes depth once, in the shell: the supervisor hands the
    # rounds their switches as typed tokens and runs them with QWEN_DEPTH=shallow,
    # so no round implies a depth switch of its own; report.md carries the mode.
    from lib import supervisor
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "value.txt").write_text("bad\n")
    (repo / "check.py").write_text("import sys\nsys.exit(0 if open('value.txt').read().strip()=='good' else 1)\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.setenv("QWEN_TEST_CMD", "%s check.py" % sys.executable.replace("\\", "/"))
    monkeypatch.setenv("QWEN_SUPERVISOR_BACKOFF", "0,0,0")
    shim = tmp_path / "shim.py"
    shim.write_text(SHIM, encoding="utf-8")
    monkeypatch.setenv("SHIM_RECORD", str(tmp_path / "shim.jsonl"))
    task = tmp_path / "task.md"
    task.write_text("# Goal\nMake the value good.\n\n- [ ] value is good -- check: test ALL\n",
                    encoding="utf-8")
    rc = supervisor.main(["--task", str(task), "--repo", str(repo),
                          "--agent", sys.executable, "--agent", str(shim),
                          "--no-deviation-audit", "--depth", "default",
                          "--", "--role-variant", "deep", "--subagents-nudge"])
    assert rc == 0
    lines = [json.loads(ln) for ln in (tmp_path / "shim.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["depth"] == "shallow"                  # rounds never re-imply depth
    assert "--role-variant" in lines[0]["argv"] and "--subagents-nudge" in lines[0]["argv"]
    text = next((tmp_path / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "depth: default" in text
    assert "switches: role_variant=deep, subagents_nudge" in text


def test_until_done_rounds_are_shallow_even_with_config_deep(tmp_path, monkeypatch):
    # The rounds' QWEN_DEPTH=shallow is not enough on its own: qwen-agent reads its
    # config file AFTER the environment, so a QWEN_DEPTH=deep sitting there would
    # re-imply depth into every round and into the audit. The flag -- shallow on the
    # COMMAND LINE, as call_agent passes it -- beats env and config alike, and the
    # audit is an agent call like a round, so it must carry it too.
    from lib import supervisor
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "value.txt").write_text("bad\n")
    (repo / "check.py").write_text("import sys\nsys.exit(0 if open('value.txt').read().strip()=='good' else 1)\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.setenv("QWEN_TEST_CMD", "%s check.py" % sys.executable.replace("\\", "/"))
    monkeypatch.setenv("QWEN_SUPERVISOR_BACKOFF", "0,0,0")
    config = tmp_path / "config"
    config.write_text("QWEN_DEPTH=deep\n", encoding="utf-8")   # the setting a flag must beat
    monkeypatch.setenv("QWEN_CONFIG", str(config))
    shim = tmp_path / "shim.py"
    shim.write_text(SHIM, encoding="utf-8")
    monkeypatch.setenv("SHIM_RECORD", str(tmp_path / "shim.jsonl"))
    monkeypatch.setenv("SHIM_RESULT", "NO CONTRADICTIONS")      # the audit parses clean
    task = tmp_path / "task.md"
    task.write_text("# Goal\nMake the value good.\n\n- [ ] value is good -- check: test ALL\n",
                    encoding="utf-8")
    rc = supervisor.main(["--task", str(task), "--repo", str(repo),
                          "--agent", sys.executable, "--agent", str(shim),
                          "--depth", "default",
                          "--", "--role-variant", "deep", "--subagents-nudge"])
    assert rc == 0
    lines = [json.loads(ln) for ln in (tmp_path / "shim.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2                                       # one round + the deviation audit
    for rec in lines:
        assert "--shallow" in rec["argv"]                        # typed, so the config cannot win
        assert rec["depth"] == "shallow"                         # the env is set as well
        assert "--review-round" not in rec["argv"]               # R belongs to the supervisor
    assert "--role-variant" in lines[0]["argv"] and "--subagents-nudge" in lines[0]["argv"]
    assert "--role-variant" not in lines[1]["argv"]              # the audit stays the plain auditor


# ------------------------------------------------------------- fence denials, background

def test_probe_fence_denials_are_a_note_not_a_failure(tmp_path, server, fake):
    # On a probe run Read/Write/... are granted outright: a denial of one is the
    # sandbox confinement (a scratch file under /tmp), and the run still succeeds.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, FAKE_MODES="deny:Write+Read,deny:Write"))
    assert r.returncode == 0, r.stderr
    assert "PERMISSION DENIED" not in r.stderr
    assert "outside the sandbox were blocked [Read,Write]" in r.stderr
    assert "treat it as suspect" not in r.stderr


def test_probe_other_denials_still_fail(tmp_path, server, fake):
    # A denial of anything but a file tool keeps its exit code.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, FAKE_MODES="deny:Write+WebFetch"))
    assert r.returncode == 7, r.stderr
    assert "PERMISSION DENIED: 2 tool call(s) blocked [WebFetch,Write]" in r.stderr


def test_shallow_file_denials_still_fail(tmp_path, server, fake):
    # Without a probe there is no sandbox fence: a file-tool denial means something else.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--shallow", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV, FAKE_MODES="deny:Write"))
    assert r.returncode == 7, r.stderr


def test_background_tasks_off_and_foreground_asked(tmp_path, server, fake):
    # A background subagent reporting after the answer makes the model write a
    # postscript that becomes the result: every session runs with them off.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    d = tmp_path / "calls"
    for n in (1, 2):
        assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" in (d / ("env.%d" % n)).read_text()
    (a1, _), (a2, _) = calls(tmp_path)
    assert "Run each subagent in the foreground" in sys_prompt(a1)
    assert "Wait for its result before you answer." in a2[-1]
    assert "only inside this copy" in sys_prompt(a1)
