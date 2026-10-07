"""qwen-agent's depth switches, offline: --role-variant, --subagents-nudge, --review-round,
--probe (and --keep-sandbox, --probe-here), --deep, and the qwen_agent key of --json.

The fake claude here records EVERY call (argv NUL-separated, plus the directory it ran
in) and answers per call from $FAKE_MODES, a comma list (call 1 uses the first entry;
calls past the list answer "ok"). The model server and the runner are test_cli's.
"""
import hashlib
import http.server
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import threading
import time

import pytest

import test_cli
from test_cli import _dry_argv, _git_repo, flag, posix, run, same_path

FAKE_DEEP = r'''#!/usr/bin/env bash
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
d="$FAKE_DIR"
n=$(cat "$d/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/n"
printf '%s\0' "$@" > "$d/argv.$n"
pwd -P > "$d/pwd.$n"
env | grep -E '^QWEN_TEST_' | sort > "$d/env.$n"
mode="$(printf '%s' "${FAKE_MODES:-ok}" | cut -d, -f"$n")"
[ -n "$mode" ] || mode=ok
answer() {
  printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}\n' "$n" "$n"
}
case "$mode" in
  ok)        answer ;;
  edit)      cat a.txt u.txt > "$d/seen.$n" 2>/dev/null
             printf 'probe edit\n' > a.txt; printf 'new\n' > made-by-session.txt; answer ;;
  apierr)    printf '%s\n' '{"type":"result","is_error":true,"api_error_status":400,"result":"API Error: 400"}' ;;
  nosession) printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"no id"}' ;;
  sleep)     sleep 30; answer ;;
  # usage:OUT_TOKENS:TURNS -- a success answer with a chosen usage/turn count, so a
  # two-call run can pin that its record totals the two calls.
  usage:*)   o="${mode#usage:}"; out="${o%%:*}"; turns="${o##*:}"
             printf '{"type":"result","subtype":"success","is_error":false,"num_turns":%s,"result":"answer %s","session_id":"sess-%s","usage":{"input_tokens":10,"output_tokens":%s},"permission_denials":[]}\n' "$turns" "$n" "$n" "$out" ;;
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
    p = tmp_path / "fake-deep"
    p.write_text(FAKE_DEEP, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    (tmp_path / "calls").mkdir()
    return p


def go(tmp_path, args, server, fake, modes="ok", extra=None, timeout=90):
    env = {"FAKE_DIR": posix(tmp_path / "calls"), "FAKE_MODES": modes,
           "QWEN_PROBE_DIR": posix(tmp_path / "probes"),
           "QWEN_OUTDIR": posix(tmp_path / "outdir")}
    (tmp_path / "outdir").mkdir(exist_ok=True)
    env.update(extra or {})
    return run(tmp_path, args, server, fake, extra=env, timeout=timeout)


def calls(tmp_path):
    """[(argv, cwd)] for every claude call, in order."""
    d = tmp_path / "calls"
    n = int((d / "n").read_text()) if (d / "n").exists() else 0
    out = []
    for i in range(1, n + 1):
        argv = [p.decode("utf-8", "replace") for p in (d / ("argv.%d" % i)).read_bytes().split(b"\0") if p]
        out.append((argv, (d / ("pwd.%d" % i)).read_text(encoding="utf-8").strip()))
    return out


def sys_prompt(argv):
    return flag(argv, "--append-system-prompt") or ""


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ role variants

def test_existing_role_texts_are_unchanged(tmp_path, server, fake):
    # sha256 of the built-in role texts as released; the deep variants live beside them
    pinned = {"auditor": "dbcc0b8e488b3f4fc5ab2afc65903656dcb84b2cb55d7e467e7ba55c25d47e81",
              "coder": "ef4b57eae82a310a5b8cb9595d53e1be6719bb21e46d5d27252409b1d56ce652",
              "mechanic": "1648791f6b4b6e46f0c62696bb763bae30b5bb9aa3d8ac8a8d924ab27f34238d"}
    for role, digest in pinned.items():
        shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
        assert go(tmp_path, ["-r", role, "hi"], server, fake).returncode == 0
        assert sha(sys_prompt(calls(tmp_path)[0][0])) == digest, role


def test_auditor_deep_is_the_method_not_the_short_audit(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--role-variant", "deep", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = sys_prompt(argv)
    assert "DEEP audit" in p and "UNVERIFIED\n  SUSPICIONS" in p and "Default to FAIL" in p
    assert "A short honest audit" not in p
    assert flag(argv, "--tools") == "Read,Glob,Grep"          # still the auditor's fence


def test_coder_deep_keeps_the_coder_text_and_adds_the_edge_case_pass(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "coder", "--role-variant=deep", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = sys_prompt(argv)
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    go(tmp_path, ["-r", "coder", "hi"], server, fake)
    plain = sys_prompt(calls(tmp_path)[0][0])
    assert p.startswith(plain + "\n\nWhen the checks pass, do an edge-case pass")
    assert "EDGE CASES" in p
    assert "Edit" in flag(argv, "--tools").split(",")          # coder still implies --write


@pytest.mark.parametrize("args,needle", [
    (["-r", "mechanic", "--role-variant", "deep"], "has no 'deep' variant"),
    (["-r", "auditor", "--role-variant", "shallow"], "has no 'shallow' variant"),
    (["--role-variant", "deep"], "needs a built-in role"),
    (["--role-file", "r.md", "--role-variant", "deep"], "--role-file"),
], ids=["mechanic", "unknown-variant", "no-role", "role-file"])
def test_role_variant_usage_errors(tmp_path, server, fake, args, needle):
    (tmp_path / "r.md").write_text("custom role\n", encoding="utf-8")
    r = go(tmp_path, [*args, "hi"], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert calls(tmp_path) == []                               # claude never ran


def test_list_roles_is_unchanged(tmp_path):
    r = run(tmp_path, ["--list-roles"])
    assert r.stdout.splitlines()[0] == "built-in: auditor, coder, mechanic, plain, tester"


# ------------------------------------------------------------------ --subagents-nudge

def test_subagents_nudge_adds_task_the_note_and_the_nudge(tmp_path, server, fake):
    r = go(tmp_path, ["--subagents-nudge", "-r", "auditor", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    assert "Task" in flag(argv, "--tools").split(",")
    assert "Task" in flag(argv, "--allowed-tools").split(",")
    p = sys_prompt(argv)
    note, nudge = "delegate broad reading and searching", "Delegate more than feels necessary."
    assert note in p and nudge in p and p.index(note) < p.index(nudge)
    assert "Verify what a subagent reports before you rely on it" in p


def test_plain_subagents_has_no_nudge(tmp_path, server, fake):
    assert go(tmp_path, ["--subagents", "-r", "auditor", "hi"], server, fake).returncode == 0
    assert "Delegate more than feels necessary." not in sys_prompt(calls(tmp_path)[0][0])


# ------------------------------------------------------------------ the JSON record

def test_json_record_lists_the_switches(tmp_path, server, fake):
    r = go(tmp_path, ["--json", "-r", "auditor", "--role-variant", "deep", "--subagents-nudge", "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["qwen_agent"]["switches"] == {"probe": False, "role_variant": "deep",
                                             "review_round": False, "subagents_nudge": True}
    assert rec["result"] == "answer 1"


def test_json_record_is_claudes_own_without_switches(tmp_path, server, fake):
    r = go(tmp_path, ["--json", "-r", "auditor", "--subagents", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert "qwen_agent" not in json.loads(r.stdout)


# ------------------------------------------------------------------ combinations

@pytest.mark.parametrize("flag_", ["--role-variant", "--subagents-nudge"])
def test_interactive_refuses_the_depth_switches(tmp_path, fake, flag_):
    args = [flag_, "deep"] if flag_ == "--role-variant" else [flag_]
    r = run(tmp_path, ["--interactive", "--dry-run", *args], fake=fake)
    assert r.returncode == 2 and flag_ in r.stderr


def test_until_done_forwards_variant_and_nudge_to_each_round(tmp_path):
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\n[ "$1" = -c ] && exit 0\n'
                      'for a in "$@"; do printf "ARG:%s\\n" "$a"; done > "$FAKE_RECORD"\n',
                      encoding="utf-8", newline="\n")
    fakepy.chmod(0o755)
    r = run(tmp_path, ["--until-done", "t.md", "--role-variant", "deep", "--subagents-nudge"],
            extra={"QWEN_PYTHON": posix(fakepy)})
    assert r.returncode == 0, r.stdout + r.stderr
    argv = [ln[4:] for ln in (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
            if ln.startswith("ARG:")]
    assert argv[argv.index("--") + 1:] == ["--role-variant", "deep", "--subagents-nudge"]


def test_the_deviation_audit_drops_the_depth_switches():
    from lib import supervisor
    got = supervisor._read_only_passthrough(["--role-variant", "deep", "--subagents-nudge",
                                             "--subagents-push", "--model", "m",
                                             "--role-variant=deep"])
    assert got == ["--model", "m"]


# ------------------------------------------------------------------ --review-round

REVIEW_START = "Review round: before your answer is final, try to break it."


def test_review_round_resumes_the_session_and_returns_the_revised_answer(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--review-round", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "answer 2"
    (a1, _), (a2, _) = calls(tmp_path)
    assert "--resume" not in a1 and a1[-2:] == ["--", "hi"]
    assert a2[:-4] == a1[:-2]                          # the same call, fence and role included
    assert a2[-4:-1] == ["--resume", "sess-1", "--"]
    assert a2[-1].startswith(REVIEW_START)
    assert "in exactly the format your previous answer had to follow" in a2[-1]


def test_review_round_replaces_a_callers_resume_id(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "--resume", "old-id", "go on"], server, fake)
    assert r.returncode == 0, r.stderr
    (a1, _), (a2, _) = calls(tmp_path)
    assert flag(a1, "--resume") == "old-id"
    assert a2.count("--resume") == 1 and flag(a2, "--resume") == "sess-1"


def test_a_failed_review_round_keeps_the_first_answer(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "--json", "hi"], server, fake, modes="ok,apierr")
    assert r.returncode == 0, r.stderr
    assert "WARNING: --review-round: the review round failed (exit 4)" in r.stderr
    rec = json.loads(r.stdout)
    assert rec["result"] == "answer 1" and rec["session_id"] == "sess-1"
    assert rec["qwen_agent"]["review_round"] == {
        "status": "failed", "first_session": "sess-1",
        "warning": "the review round failed (exit 4); the first answer stands"}


def test_no_review_round_after_a_failed_first_call(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "hi"], server, fake, modes="apierr")
    assert r.returncode == 4
    assert len(calls(tmp_path)) == 1


def test_no_session_id_means_no_review_round(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "hi"], server, fake, modes="nosession")
    assert r.returncode == 0 and r.stdout.strip() == "no id"
    assert "no session id to resume" in r.stderr
    assert len(calls(tmp_path)) == 1


def test_review_round_json_record(tmp_path, server, fake):
    r = go(tmp_path, ["--review-round", "--json", "-o", "out.json", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    rec = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rec["result"] == "answer 2" and rec["session_id"] == "sess-2"
    assert rec["qwen_agent"]["switches"]["review_round"] is True
    assert rec["qwen_agent"]["review_round"] == {"status": "ok", "first_session": "sess-1",
                                                 "warning": None}


def test_review_round_json_sums_usage(tmp_path, server, fake):
    # The run paid for TWO calls; the record must carry their sum, not the review
    # call's alone: 100+50 output tokens, 10+10 input, and 2+1 turns.
    r = go(tmp_path, ["--review-round", "--json", "hi"], server, fake,
           modes="usage:100:2,usage:50:1")
    assert r.returncode == 0, r.stderr
    assert "could not sum" not in r.stderr
    rec = json.loads(r.stdout)
    assert rec["result"] == "answer 2"                       # still the review call's answer
    assert rec["usage"]["output_tokens"] == 150 and rec["usage"]["input_tokens"] == 20
    assert rec["num_turns"] == 3
    assert rec["qwen_agent"]["review_round"]["status"] == "ok"


def test_interactive_refuses_review_round(tmp_path, fake):
    r = run(tmp_path, ["--interactive", "--dry-run", "--review-round"], fake=fake)
    assert r.returncode == 2 and "--review-round" in r.stderr


def test_review_round_in_a_detached_run(tmp_path, server, fake):
    # -w runs emit() in the background; the review round must still happen.
    r = go(tmp_path, ["-w", "-o", "bg.md", "--review-round", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    status = test_cli.wait_for_status(tmp_path / "bg.md.status")
    assert "exit=0" in status
    assert (tmp_path / "bg.md").read_text(encoding="utf-8").strip() == "answer 2"


# ------------------------------------------------------------------ --probe

def dirty_repo(tmp_path, name="repo"):
    repo = _git_repo(tmp_path / name)                     # a.txt = "a\n", committed
    (repo / "sub").mkdir()
    (repo / "sub" / "s.txt").write_text("s\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "sub"], check=True, capture_output=True)
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")        # uncommitted
    (repo / "u.txt").write_text("untracked\n", encoding="utf-8")    # untracked
    return repo


def tree_state(repo):
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                        encoding="utf-8").stdout
    return st, sorted(p.name for p in repo.iterdir()), (repo / "a.txt").read_text(encoding="utf-8")


def probe_runs(tmp_path):
    root = tmp_path / "probes"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def test_probe_runs_in_a_sandbox_of_the_dirty_tree_and_never_writes_it(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    before = tree_state(repo)
    r = go(tmp_path, ["--probe", "-r", "auditor", "-C", posix(repo), "hi"], server, fake, modes="edit")
    assert r.returncode == 0, r.stderr
    (argv, cwd), = calls(tmp_path)
    assert not same_path(cwd).startswith(same_path(repo))
    assert same_path(cwd).startswith(same_path(os.path.realpath(str(tmp_path / "probes"))))
    seen = (tmp_path / "calls" / "seen.1").read_text(encoding="utf-8")
    assert seen == "dirty\nuntracked\n"                     # the session saw the user's state
    assert set(flag(argv, "--tools").split(",")) == {"Read", "Glob", "Grep", "Edit", "Write", "Bash"}
    assert flag(argv, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep"
    assert flag(argv, "--permission-mode") == "dontAsk" and "--restricted" in argv
    p = sys_prompt(argv)
    assert "throwaway copy of the project" in p and "thrown away when the session ends" in p
    assert "WARNING: --probe: this run has a shell" in r.stderr
    assert tree_state(repo) == before                       # the user's tree was only read
    assert probe_runs(tmp_path) == []                       # and the sandbox is gone


def test_probe_enters_the_sandboxs_copy_of_a_subdirectory(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-C", posix(repo / "sub"), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    (_, cwd), = calls(tmp_path)
    assert pathlib.Path(cwd).name == "sub" and pathlib.Path(cwd).parent.name == "tree"


def test_probe_write_run_reports_a_patch_next_to_out(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    before = tree_state(repo)
    r = go(tmp_path, ["--probe", "-r", "coder", "-C", posix(repo), "-o", "out.md", "hi"],
           server, fake, modes="edit")
    assert r.returncode == 0, r.stderr
    patch = tmp_path / "out.md.patch"
    text = patch.read_text(encoding="utf-8")
    assert "-dirty" in text and "+probe edit" in text and "made-by-session.txt" in text
    assert "u.txt" not in text                                  # the base already had it
    assert "patch: %s" % posix(patch) in posix(r.stderr)
    assert "Your edits are handed to the user as a patch" in sys_prompt(calls(tmp_path)[0][0])
    assert tree_state(repo) == before                           # nothing applied
    chk = subprocess.run(["git", "-C", str(repo), "apply", "--check", str(patch)], capture_output=True)
    assert chk.returncode == 0, chk.stderr                      # it applies to the dirty tree


def test_probe_write_patch_without_out_goes_to_the_outdir(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--write", "-C", posix(repo), "hi"], server, fake, modes="edit")
    assert r.returncode == 0, r.stderr
    made = list((tmp_path / "outdir").glob("qwen-agent-*.patch"))
    assert len(made) == 1 and "+probe edit" in made[0].read_text(encoding="utf-8")


def test_probe_write_without_o_leaves_caller_dir_clean(tmp_path, server, fake):
    # The caller stands INSIDE the tree being probed and sets no QWEN_OUTDIR: the
    # default patch must not land in the cwd -- that is the tree the run "only
    # reads", and the next --probe would copy the leftover patch back as dirt.
    # With no QWEN_OUTDIR (nor -o) the patch goes to the probe directory itself.
    repo = dirty_repo(tmp_path)
    before = tree_state(repo)
    r = run(tmp_path, ["--probe", "--write", "-r", "coder", "-C", posix(repo), "hi"], server, fake,
            cwd=str(repo),
            extra={"FAKE_DIR": posix(tmp_path / "calls"), "FAKE_MODES": "edit",
                   "QWEN_PROBE_DIR": posix(tmp_path / "probes")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert tree_state(repo) == before                       # not one file added to the tree
    assert not list(repo.glob("qwen-agent-*.patch"))        # ... and the run names no patch there
    line = next(ln for ln in r.stderr.splitlines() if "patch: " in ln)
    patch = line.split("patch: ", 1)[1].split(" (not applied", 1)[0]
    assert same_path(patch).startswith(same_path(tmp_path / "probes"))
    assert len(list((tmp_path / "probes").glob("qwen-agent-*.patch"))) == 1
    assert "+probe edit" in pathlib.Path(patch).read_text(encoding="utf-8")


def test_probe_write_with_no_change(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--write", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert list((tmp_path / "outdir").glob("*.patch")) == []
    assert "changed nothing" in r.stderr
    r = go(tmp_path, ["--probe", "--write", "-C", posix(repo), "-o", "o.md", "hi"], server, fake)
    assert r.returncode == 0 and (tmp_path / "o.md.patch").read_bytes() == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting and sh -c")
def test_probe_apply_command_is_quoted(tmp_path, server, fake):
    # The printed `git -C DIR apply PATCH` must paste into a POSIX shell as it stands:
    # DIR and PATCH single-quoted, each embedded ' written '\''. A --test-repo path with
    # a quote and a space is exactly what a bare '$DIR' spelling breaks on.
    repo = dirty_repo(tmp_path, "my' repo")
    r = go(tmp_path, ["--probe", "--write", "--test-repo", posix(repo), "-C", posix(repo),
                      "hi"], server, fake, modes="edit")
    assert r.returncode == 0, r.stderr
    patch = next(iter((tmp_path / "outdir").glob("qwen-agent-*.patch")))
    line = next(ln for ln in r.stderr.splitlines() if "to apply: " in ln)
    cmd = line.rsplit("to apply: ", 1)[1]
    assert cmd.endswith(")") and "''" in cmd                  # the quoting is in the line
    cmd = cmd[:-1]                                            # drop the message's ")"
    argv = shlex.split(cmd)
    assert argv == ["git", "-C", posix(repo), "apply", posix(patch)]   # exact, absolute
    applied = subprocess.run(["sh", "-c", cmd], capture_output=True, encoding="utf-8")
    assert applied.returncode == 0, applied.stdout + applied.stderr    # pastes and applies
    assert "probe edit" in (repo / "a.txt").read_text(encoding="utf-8")


def test_probe_with_test_runs_the_sandboxs_tests(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--test", "-r", "auditor", "-C", posix(repo), "hi"], server, fake,
           extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    assert r.returncode == 0, r.stderr
    (argv, cwd), = calls(tmp_path)
    env = dict(ln.split("=", 1) for ln in
               (tmp_path / "calls" / "env.1").read_text(encoding="utf-8").splitlines() if "=" in ln)
    # same_path first: the fake is a Git Bash script, so on Windows it sees the shell's
    # /c/... spelling, which realpath alone would read as a folder on the current drive.
    assert same_path(os.path.realpath(same_path(env["QWEN_TEST_SOURCE"]))) == same_path(cwd)
    assert "--add-dir" not in argv
    p = sys_prompt(argv)
    assert "Your only shell command" not in p and "also run with `qwen-test [SELECTOR]`" in p
    assert flag(argv, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep"
    assert probe_runs(tmp_path) == [] and not list((tmp_path / "wts").glob("*"))


def test_keep_sandbox_then_resume_there_with_probe_here(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--keep-sandbox", "--json", "-C", posix(repo), "hi"], server, fake,
           modes="edit")
    assert r.returncode == 0, r.stderr
    kept = json.loads(r.stdout)["qwen_agent"]["sandbox"]
    assert "sandbox kept: %s" % kept in r.stderr
    assert (pathlib.Path(kept) / "made-by-session.txt").exists()
    assert len(probe_runs(tmp_path)) == 1
    r = go(tmp_path, ["--probe-here", "-C", posix(kept), "--resume", "sess-1", "go on"], server, fake)
    assert r.returncode == 0, r.stderr
    (argv, cwd) = calls(tmp_path)[1]
    assert same_path(cwd) == same_path(os.path.realpath(kept)) and flag(argv, "--resume") == "sess-1"
    assert flag(argv, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep"
    assert len(probe_runs(tmp_path)) == 1                   # --probe-here made and removed nothing


def test_probe_here_refuses_a_tree_that_is_not_a_sandbox(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe-here", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 2 and "not inside a probe sandbox" in r.stderr
    assert calls(tmp_path) == []


def head_sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          encoding="utf-8", check=True).stdout.strip()


def test_probe_here_refuses_a_stray_base_marker(tmp_path, server, fake):
    # The .base and run-marker files are only text: next to a real repo they must not
    # be enough for --probe-here, or a coder with full Bash would run in the user's
    # tree. A genuine-looking marker (the repo's own HEAD sha) and a copied run marker
    # must not change that.
    repo = dirty_repo(tmp_path)
    (tmp_path / "repo.base").write_text(head_sha(repo) + "\n", encoding="utf-8")
    r = go(tmp_path, ["--probe-here", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 2 and "not inside a probe sandbox" in r.stderr
    assert calls(tmp_path) == []

    deep = tmp_path / "a" / "b"                             # deep enough that a copied run
    deep.mkdir(parents=True)                                # marker sits two levels above
    nested = _git_repo(deep / "repo")
    (tmp_path / "a" / ".qwen-probe-run").write_text("qwen-probe\n", encoding="utf-8")
    (deep / "repo.base").write_text(head_sha(nested) + "\n", encoding="utf-8")
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--probe-here", "-C", posix(nested), "hi"], server, fake)
    assert r.returncode == 2 and "not inside a probe sandbox" in r.stderr
    assert calls(tmp_path) == []


def git_test_state(repo):
    """What a refused --test-repo must leave untouched: the repo's .git/HEAD and index
    bytes, and its worktree list (--test-repo would have added one to it)."""
    wt = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                        encoding="utf-8", check=True).stdout
    return ((repo / ".git" / "HEAD").read_bytes(), (repo / ".git" / "index").read_bytes(), wt)


def test_probe_here_refuses_test_repo(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--keep-sandbox", "--json", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    kept = json.loads(r.stdout)["qwen_agent"]["sandbox"]
    before = git_test_state(repo)
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--probe-here", "--test", "--test-repo", posix(repo), "-C", posix(kept),
                      "hi"], server, fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--probe-here runs the sandbox's own tests; --test-repo is not allowed" in r.stderr
    assert git_test_state(repo) == before                   # the user's repo .git untouched
    assert calls(tmp_path) == []                            # and nothing ran


def repo_bytes(repo):
    """Every work-tree file of a repo plus the .git/index bytes -- what a refused
    --probe-here must not have touched, gate and session alike."""
    state = {str(p.relative_to(repo)): p.read_bytes() for p in sorted(repo.rglob("*"))
             if p.is_file() and ".git" not in p.parts}
    state[".git/index"] = (repo / ".git" / "index").read_bytes()
    return state


@pytest.mark.parametrize("steer", ["work_tree", "git_dir"])
def test_probe_here_ignores_git_work_tree_env(tmp_path, server, fake, steer):
    # An inherited GIT_* aims every git call -- the gate's rev-parse and the session's
    # Bash alike -- at some other repository: GIT_WORK_TREE=<kept sandbox> answers that
    # sandbox as the top level of the user's plain repo, and --probe-here would read the
    # plain repo as fenced and grant it a full shell. qwen-agent unsets the six GIT_*
    # steering variables before probe.py and before the session; probe.py strips them
    # from every git it runs itself. Either half alone refuses; the byte snapshot catches
    # any git command reaching the user's repo either way.
    src = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "--keep-sandbox", "--json", "-C", posix(src), "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    kept = pathlib.Path(json.loads(r.stdout)["qwen_agent"]["sandbox"])
    assert (kept / ".git").is_dir()                       # clone mode: a real .git to aim
    user = _git_repo(tmp_path / "user repo")
    before = repo_bytes(user)
    env = ({"GIT_WORK_TREE": posix(kept)} if steer == "work_tree"
           else {"GIT_DIR": posix(kept / ".git")})
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--probe-here", "-r", "coder", "-C", posix(user), "hi"], server, fake,
           extra=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not inside a probe sandbox" in r.stderr
    assert calls(tmp_path) == []                          # fake claude never started
    assert repo_bytes(user) == before                     # not one byte of the repo moved


@pytest.mark.skipif(os.name == "nt", reason="chmod does not keep writes out on Windows")
def test_probe_patch_write_failure_keeps_the_sandbox_and_exits_8(tmp_path, server, fake):
    # The patch destination (QWEN_OUTDIR here) refuses the write: the session's work is
    # in the sandbox, so the sandbox is kept and its path printed rather than removed.
    repo = dirty_repo(tmp_path)
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    sealed.chmod(0o555)
    if os.access(str(sealed), os.W_OK):                     # e.g. running as root
        pytest.skip("a read-only directory is still writable here")
    r = go(tmp_path, ["--probe", "-r", "coder", "--json", "-C", posix(repo), "hi"],
           server, fake, modes="edit", extra={"QWEN_OUTDIR": posix(sealed)})
    assert r.returncode == 8, r.stdout + r.stderr
    assert "cannot create a patch file" in r.stderr
    rec = json.loads(r.stdout)
    kept = rec["qwen_agent"]["sandbox"]
    assert kept and rec["qwen_agent"]["patch"] is None
    assert pathlib.Path(kept).is_dir() and len(probe_runs(tmp_path)) == 1   # sandbox kept
    assert kept in r.stderr and "sandbox kept" in r.stderr                  # and printed
    sealed.chmod(0o755)                                                     # let tmp clean up


@pytest.mark.parametrize("args,needle", [
    (["-w"], "-w"),
    (["--all-tools"], "--all-tools"),
    (["--toolset", "Read"], "--toolset"),
    (["--read-only"], "--read-only"),
    (["-t", "Bash"], "-t/--tools"),
    (["--permission-mode", "plan"], "--permission-mode"),
    (["-D", "."], "-D/--add-dir"),
    (["--resume", "abc"], "--resume"),
    (["--probe-here"], "exclusive"),
], ids=["detach", "all-tools", "toolset", "read-only", "tools", "perm", "add-dir", "resume", "here"])
def test_probe_refuses_flags_that_would_widen_or_break_the_fence(tmp_path, server, fake, args, needle):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", *args, "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert calls(tmp_path) == [] and probe_runs(tmp_path) == []


def test_keep_sandbox_needs_probe(tmp_path, server, fake):
    r = go(tmp_path, ["--keep-sandbox", "hi"], server, fake)
    assert r.returncode == 2 and "--keep-sandbox needs --probe" in r.stderr


def test_a_probe_dir_inside_the_tree_is_refused(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-C", posix(repo), "hi"], server, fake,
           extra={"QWEN_PROBE_DIR": posix(repo / ".probes")})
    assert r.returncode == 2 and "QWEN_PROBE_DIR" in r.stderr
    assert calls(tmp_path) == [] and not (repo / ".probes").exists()


def test_probe_dry_run_makes_nothing(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "--probe", "hi"], fake=fake,
            extra={"QWEN_PROBE_DIR": posix(tmp_path / "probes")})
    assert r.returncode == 0, r.stderr
    assert "# probe: the sandbox is made at run time" in r.stdout
    assert "--restricted" in _dry_argv(r) and probe_runs(tmp_path) == []


def test_probe_json_record(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--probe", "-r", "coder", "--json", "-o", "o.json", "-C", posix(repo), "hi"],
           server, fake, modes="edit")
    assert r.returncode == 0, r.stderr
    meta = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["qwen_agent"]
    assert meta["switches"]["probe"] is True
    assert same_path(meta["patch"]) == same_path(tmp_path / "o.json.patch")
    assert meta["sandbox"] is None


@pytest.mark.skipif(os.name != "posix", reason="sends SIGTERM")
def test_sigterm_mid_session_still_removes_the_sandbox(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env.update({"QWEN_CONFIG": posix(tmp_path / "none"), "QWEN_BASE_URL": test_cli.base_url(server),
                "QWEN_CLAUDE_BIN": posix(fake), "FAKE_DIR": posix(tmp_path / "calls"),
                "FAKE_MODES": "sleep", "QWEN_PROBE_DIR": posix(tmp_path / "probes")})
    p = subprocess.Popen([test_cli.BASH, posix(test_cli.AGENT), "--probe", "-C", posix(repo), "hi"],
                         env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while not (tmp_path / "calls" / "n").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert len(probe_runs(tmp_path)) == 1
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=30)
    assert p.returncode == 143
    assert probe_runs(tmp_path) == []


def test_probe_with_spaces_in_every_path_and_web(tmp_path, server, fake):
    # A project and a probe directory whose paths hold spaces, plus --web.
    repo = dirty_repo(tmp_path, "my project")
    r = go(tmp_path, ["--probe", "--web", "-r", "coder", "-C", posix(repo), "-o", "out file.md", "hi"],
           server, fake, modes="edit", extra={"QWEN_PROBE_DIR": posix(tmp_path / "probe dir")})
    assert r.returncode == 0, r.stderr
    (argv, cwd), = calls(tmp_path)
    assert "probe dir" in cwd
    assert flag(argv, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,WebFetch"
    assert "+probe edit" in (tmp_path / "out file.md.patch").read_text(encoding="utf-8")
    assert not list((tmp_path / "probe dir").iterdir())


# ------------------------------------------------------------ caller-environment hygiene

def test_an_ambient_QA_META_adds_no_qwen_agent_key(tmp_path, server, fake):
    # A caller's QA_META/QA_META_* are unset at startup: only export_meta sets them,
    # so an inherited QA_META=1 cannot bolt the qwen_agent key onto a plain run.
    r = go(tmp_path, ["--json", "hi"], server, fake, extra={"QA_META": "1", "QA_META_PROBE": "1"})
    assert r.returncode == 0, r.stderr
    assert "qwen_agent" not in json.loads(r.stdout)


@pytest.mark.parametrize("args,needle", [
    (["--interactive", "--dry-run", "--probe=1"], "--probe"),
    (["--interactive", "--dry-run", "--probe-here=1"], "--probe-here"),
    (["--interactive", "--dry-run", "--keep-sandbox=1"], "--keep-sandbox"),
    (["--until-done", "t.md", "--review-round=x"], "--review-round"),
    (["--until-done", "t.md", "--probe=1"], "--probe"),
    (["--probe=1", "hi"], "--probe"),
], ids=["probe", "probe-here", "keep-sandbox", "review-round-until-done",
        "probe-until-done", "probe-plain"])
def test_the_equals_form_of_a_valueless_switch_is_refused(tmp_path, fake, args, needle):
    # --flag=value splits into --flag plus an orphan value that reads like a prompt;
    # every refusal of these switches must catch the =-form and name the flag.
    r = run(tmp_path, args, None, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "calls" / "n").exists()       # claude never ran


def test_until_done_gives_the_review_round_to_the_supervisor(tmp_path):
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\n[ "$1" = -c ] && exit 0\n'
                      'for a in "$@"; do printf "ARG:%s\\n" "$a"; done > "$FAKE_RECORD"\n',
                      encoding="utf-8", newline="\n")
    fakepy.chmod(0o755)
    r = run(tmp_path, ["--until-done", "t.md", "--review-round", "--max-rounds", "4"],
            extra={"QWEN_PYTHON": posix(fakepy)})
    assert r.returncode == 0, r.stdout + r.stderr
    argv = [ln[4:] for ln in (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
            if ln.startswith("ARG:")]
    sep = argv.index("--")
    assert "--review-round" in argv[:sep] and "--review-round" not in argv[sep + 1:]


def _until_done_argv(tmp_path, args):
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\n[ "$1" = -c ] && exit 0\n'
                      'for a in "$@"; do printf "ARG:%s\\n" "$a"; done > "$FAKE_RECORD"\n',
                      encoding="utf-8", newline="\n")
    fakepy.chmod(0o755)
    r = run(tmp_path, ["--until-done", "t.md", *args], extra={"QWEN_PYTHON": posix(fakepy)})
    if r.returncode != 0:
        return r, None
    return r, [ln[4:] for ln in (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
               if ln.startswith("ARG:")]


def test_until_done_gives_probe_and_keep_sandbox_to_the_supervisor(tmp_path):
    r, argv = _until_done_argv(tmp_path, ["--probe", "--keep-sandbox"])
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert "--probe" in argv[:sep] and "--keep-sandbox" in argv[:sep]
    assert argv[sep + 1:] == []


def test_until_done_deep_splits_into_supervisor_and_round_switches(tmp_path):
    r, argv = _until_done_argv(tmp_path, ["--deep"])
    assert r.returncode == 0, r.stdout + r.stderr
    sep = argv.index("--")
    assert "--probe" in argv[:sep] and "--review-round" in argv[:sep]
    # the rounds get the delegation PUSH (--deep and the default depth push; only
    # qwen-sweep batches keep nudging)
    assert argv[sep + 1:] == ["--role-variant", "deep", "--subagents-push"]


@pytest.mark.parametrize("args,needle", [
    (["--probe-here"], "--probe-here belongs to the supervisor"),
    (["--keep-sandbox"], "--keep-sandbox needs --probe"),
])
def test_until_done_refuses(tmp_path, args, needle):
    r, _ = _until_done_argv(tmp_path, args)
    assert r.returncode == 2 and needle in r.stderr


@pytest.mark.parametrize("sw", ["--probe", "--deep"])
def test_until_done_probe_refuses_test_repo(tmp_path, sw):
    # One sandbox holds the whole loop and every round tests IT; a --test-repo
    # would send each round's --probe-here elsewhere and die at the agent after a
    # round. Refuse the combination up front, in the caller's own words (--deep
    # includes --probe, so name what the caller typed).
    r, _ = _until_done_argv(tmp_path, [sw, "--test-repo", posix(tmp_path / "tests-here")])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--test-repo is not supported with --until-done %s" % sw in r.stderr
    assert not (tmp_path / "record.txt").exists()            # the supervisor was never exec'd


# ------------------------------------------------------------------ --deep

def test_deep_is_all_four_switches(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "--json", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["qwen_agent"]["switches"] == {"probe": True, "role_variant": "deep",
                                             "review_round": True, "subagents_nudge": False,
                                             "subagents_push": True}
    assert rec["result"] == "answer 2" and rec["qwen_agent"]["review_round"]["status"] == "ok"
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert c1 == c2                                           # the review round ran in the sandbox
    p = sys_prompt(a1)
    # --deep pushes delegation now: the mandate text stands, the nudge text does not
    assert "DEEP audit" in p and "Delegation is part of this task" in p
    assert "Delegate more than feels necessary." not in p
    assert "throwaway copy of the project" in p and "Task" in flag(a1, "--tools").split(",")
    assert probe_runs(tmp_path) == []


@pytest.mark.parametrize("args", [[], ["-r", "mechanic"], ["--role-file", "r.md"]])
def test_deep_needs_the_auditor_or_the_coder(tmp_path, server, fake, args):
    (tmp_path / "r.md").write_text("x\n", encoding="utf-8")
    r = go(tmp_path, ["--deep", *args, "hi"], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert calls(tmp_path) == [] and probe_runs(tmp_path) == []


def test_interactive_refuses_probe_and_deep(tmp_path, fake):
    for f in ("--probe", "--deep", "--keep-sandbox", "--probe-here"):
        r = run(tmp_path, ["--interactive", "--dry-run", f], fake=fake)
        assert r.returncode == 2 and f in r.stderr, f


def test_deep_refuses_a_value(tmp_path, fake):
    # --deep=1 split into --deep plus an orphan "1" that reads like the prompt: the
    # audit call got "drop the prompt", the session's prompt became "1". Like the
    # other valueless switches it is refused where the message can name the flag.
    r = run(tmp_path, ["--deep=1", "hi"], fake=fake)
    assert r.returncode == 2
    assert "option --deep takes no value (got '--deep=1')" in r.stderr
    assert calls(tmp_path) == []                             # claude never started


def test_subagents_nudge_refuses_a_value(tmp_path, fake):
    # --subagents-nudge=1 is the same split as --deep=1: the orphan "1" reads like
    # the prompt. The switch that was missing from the =-form refusals.
    r = run(tmp_path, ["--subagents-nudge=1", "hi"], None, fake)
    assert r.returncode == 2
    assert "option --subagents-nudge takes no value (got '--subagents-nudge=1')" in r.stderr
    assert calls(tmp_path) == []                             # claude never started


def test_deep_rejects_another_role_variant(tmp_path, server, fake):
    # --deep sets --role-variant deep. An explicit different variant must not win or
    # lose by flag ORDER alone -- under --until-done the forwarded --role-variant
    # deep comes last, so an explicit shallow would silently become deep. Refuse it.
    r = go(tmp_path, ["--deep", "-r", "auditor", "--role-variant", "shallow", "hi"], server, fake)
    assert r.returncode == 2 and "--deep sets --role-variant deep" in r.stderr
    assert calls(tmp_path) == [] and probe_runs(tmp_path) == []
    r = go(tmp_path, ["--role-variant", "shallow", "--deep", "-r", "auditor", "hi"], server, fake)
    assert r.returncode == 2 and "--deep sets --role-variant deep (got --role-variant shallow)" in r.stderr
    r = go(tmp_path, ["--deep", "-r", "auditor", "--role-variant=shallow", "hi"], server, fake)
    assert r.returncode == 2 and "--deep sets --role-variant deep (got --role-variant=shallow)" in r.stderr
    # The same collision under --until-done is refused too.
    r, _ = _until_done_argv(tmp_path, ["--deep", "--role-variant", "shallow"])
    assert r.returncode == 2 and "--deep sets --role-variant deep" in r.stderr
    # Naming the variant --deep names is fine.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "--role-variant", "deep", "-C", posix(repo), "hi"],
           server, fake)
    assert r.returncode == 0, r.stdout + r.stderr


def test_deep_with_probe_here_uses_probe_here(tmp_path, server, fake):
    # --deep --probe-here resumes a deep session IN the sandbox --deep --keep-sandbox
    # left: --probe-here wins over --deep's implied --probe instead of dying with
    # "--probe and --probe-here are exclusive" -- which made the user spell out
    # --review-round --role-variant deep --subagents-nudge by hand.
    repo = dirty_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "--keep-sandbox", "--json", "-C", posix(repo), "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    kept = json.loads(r.stdout)["qwen_agent"]["sandbox"]
    assert len(probe_runs(tmp_path)) == 1
    shutil.rmtree(str(tmp_path / "calls")); (tmp_path / "calls").mkdir()
    r = go(tmp_path, ["--deep", "--probe-here", "-r", "auditor", "--json", "-C", posix(kept),
                      "--resume", "sess-1", "go on"], server, fake)
    assert r.returncode == 0, r.stdout + r.stderr
    (argv, cwd) = calls(tmp_path)[0]
    assert same_path(cwd) == same_path(os.path.realpath(kept))     # in the sandbox that exists
    assert flag(argv, "--resume") == "sess-1"
    # The probe fence, plus Task: --deep implies --subagents.
    assert flag(argv, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,Task"
    assert "--restricted" in argv
    assert "DEEP audit" in sys_prompt(argv)                        # the deep fence, kept sandbox
    assert len(probe_runs(tmp_path)) == 1                          # nothing made, nothing removed


def test_until_done_probe_end_to_end(tmp_path, server, fake):
    repo = dirty_repo(tmp_path)
    py = posix(shutil.which("python3") or shutil.which("python"))
    (repo / "check.py").write_text(
        "import sys\nsys.exit(0 if open('a.txt').read() == 'probe edit\\n' else 1)\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text("* [ ] a.txt edited -- check: cmd %s check.py\n" % py, encoding="utf-8")
    before = tree_state(repo)
    r = go(tmp_path, ["--until-done", posix(task), "-C", posix(repo), "--probe", "--no-deviation-audit"],
           server, fake, modes="ok,edit",
           extra={"QWEN_TEST_CMD": "%s -c pass" % py, "QWEN_AGENT_STATE": posix(tmp_path / "state"),
                  "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"), "QWEN_SUPERVISOR_BACKOFF": "0"},
           timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    (a1, c1), (a2, c2) = calls(tmp_path)
    assert c1 == c2 and "--restricted" in a1
    assert flag(a2, "--allowed-tools") == "Bash,Read,Edit,Write,MultiEdit,Glob,Grep"
    assert tree_state(repo) == before
    patch = [ln for ln in r.stdout.splitlines() if ln.startswith("patch: ")][0][len("patch: "):]
    assert "+probe edit" in open(patch, encoding="utf-8").read()


def test_until_done_probe_clears_git_env(tmp_path, server, fake):
    # The until-done path execs the supervisor, so qwen-agent's unset of the six
    # steering GIT_* must happen BEFORE that exec: with GIT_INDEX_FILE=<the user's
    # index> -- as a pre-commit hook leaves it -- probe.make's dirty-state copy would
    # run against the user's index, leaving it listing the session's files and
    # unreadable to `git status`.
    repo = dirty_repo(tmp_path)
    py = posix(shutil.which("python3") or shutil.which("python"))
    (repo / "check.py").write_text(
        "import sys\nsys.exit(0 if open('a.txt').read() == 'probe edit\\n' else 1)\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text("* [ ] a.txt edited -- check: cmd %s check.py\n" % py, encoding="utf-8")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    r = go(tmp_path, ["--until-done", posix(task), "-C", posix(repo), "--probe", "--no-deviation-audit"],
           server, fake, modes="ok,edit",
           extra={"QWEN_TEST_CMD": "%s -c pass" % py, "QWEN_AGENT_STATE": posix(tmp_path / "state"),
                  "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"), "QWEN_SUPERVISOR_BACKOFF": "0",
                  "GIT_INDEX_FILE": posix(index)},
           timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert index.read_bytes() == before                   # not one byte moved
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                        encoding="utf-8")
    assert st.returncode == 0, st.stderr                  # git in the repo still works
