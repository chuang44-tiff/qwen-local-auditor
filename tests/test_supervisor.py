import json
import os
import subprocess
import sys

import pytest

from lib import supervisor
from lib.builders import history

PY = sys.executable
FAKE = os.path.join(os.path.dirname(__file__), "fake_agent.py")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q"); git(repo, "config", "user.email", "t@example.com"); git(repo, "config", "user.name", "t")
    (repo / "value.txt").write_text("bad\n")
    (repo / "check.py").write_text("import sys\nsys.exit(0 if open('value.txt').read().strip()=='good' else 1)\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "init")
    (tmp_path / ".gitignore-none").write_text("")
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.setenv("QWEN_TEST_CMD", "%s check.py" % PY.replace("\\", "/"))
    monkeypatch.setenv("QWEN_SUPERVISOR_BACKOFF", "0,0,0")
    monkeypatch.setenv("FAKE_AGENT_RECORD", str(tmp_path / "record.jsonl"))
    task = tmp_path / "task.md"
    task.write_text("# Goal\nMake the value good.\n\n- [ ] value is good -- check: test ALL\n")
    return repo, task, tmp_path


def script(tmp_path, steps):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(steps))
    os.environ["FAKE_AGENT_SCRIPT"] = str(p)


def calls(tmp_path):
    return [json.loads(l) for l in (tmp_path / "record.jsonl").read_text().splitlines()]


def sup(repo, task, *extra):
    return supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY, "--agent", FAKE,
                            "--no-deviation-audit", *extra])


def test_fail_fail_pass_is_done_after_three_rounds(env):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "tried again", "write": {"value.txt": "meh\n"}},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    assert len(c) == 3
    assert "--resume" not in c[0]
    assert c[1][c[1].index("--resume") + 1] == "s1" and c[2][c[2].index("--resume") + 1] == "s1"


def test_not_done_prompt_names_the_failing_item(env):
    repo, task, tmp = env
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    second = calls(tmp)[1]
    prompt = open(second[second.index("-f") + 1], encoding="utf-8").read()
    assert "Not done" in prompt and "value is good" in prompt and "TEST ALL FAILED" in prompt


def test_same_failures_twice_is_no_progress(env):
    repo, task, tmp = env
    script(tmp, [{"result": "nothing changes"}])
    assert sup(repo, task) == 12
    assert len(calls(tmp)) == 2


def test_writing_check_does_not_fake_progress(env):
    # A `cmd` check runs in the live repo, so one that writes a file changes the tree
    # every round. The round's tree signature is the tree the AGENT left, taken before
    # any check runs, so a round that changed nothing is still a stuck round: exit 12
    # after two rounds, not the round limit.
    repo, task, tmp = env
    (repo / "wcheck.py").write_text(
        "import sys\n"
        "with open('side.txt', 'a', encoding='utf-8') as fh:\n    fh.write('ran\\n')\n"
        "sys.exit(1)\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "a check that writes")
    task.write_text("# Goal\nNothing here passes.\n\n"
                    "- [ ] the writing check -- check: cmd %s wcheck.py\n" % PY.replace("\\", "/"))
    script(tmp, [{"result": "nothing changes"}])
    assert sup(repo, task) == 12
    assert len(calls(tmp)) == 2
    report = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "no progress" in report
    assert (repo / "side.txt").read_text() == "ran\nran\n"      # the check wrote, twice


def test_round_limit_is_partial_with_a_report(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "x", "write": {"value.txt": "v%d\n" % i}} for i in range(10)])
    assert sup(repo, task, "--max-rounds", "3") == 11
    out = capsys.readouterr().out
    report = [l for l in out.splitlines() if l.startswith("report: ")][0][8:]
    text = open(report, encoding="utf-8").read()
    assert "round limit" in text and "value is good" in text


def test_token_budget_is_partial(env):
    repo, task, tmp = env
    script(tmp, [{"result": "x", "tokens": 1000, "write": {"value.txt": "v%d\n" % i}} for i in range(10)])
    assert sup(repo, task, "--budget-tokens", "1500") == 11


def test_budget_seconds_is_partial(env):
    repo, task, tmp = env
    # Each round burns ~0.6s of wall clock: a 1s budget stops the run on the
    # second round, long before the 8-round limit, so the 11 is the budget's.
    script(tmp, [{"result": "x", "sleep": 0.6, "write": {"value.txt": "v%d\n" % i}} for i in range(10)])
    assert sup(repo, task, "--budget-seconds", "1") == 11
    # If the budget were ignored the run would spend all 8 rounds before the
    # round limit gave the same 11; the report names which one stopped it.
    assert len(calls(tmp)) < 8
    report = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "budget exhausted" in report


def test_unusable_reply_resumes_once_then_stops(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 6}, {"rc": 6}])
    assert sup(repo, task) == 8
    assert len(calls(tmp)) == 2


def test_empty_reply_that_fixed_the_tree_is_done(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 6, "result": "", "session_id": "s-empty", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert len(calls(tmp)) == 1


def test_empty_reply_with_changes_keeps_the_session_and_continues(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 6, "result": "", "session_id": "s-empty", "write": {"value.txt": "meh\n"}},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    assert len(c) == 2
    assert c[1][c[1].index("--resume") + 1] == "s-empty"


def test_empty_replies_with_no_changes_still_stop_with_8(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 6, "session_id": "s-empty"}, {"rc": 6, "session_id": "s-empty"}])
    assert sup(repo, task) == 8
    assert len(calls(tmp)) == 2


def test_agent_usage_error_stops_with_2(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"rc": 2, "stderr": "qwen-agent: unknown option: --frobnicate\n"}])
    assert sup(repo, task) == 2
    assert len(calls(tmp)) == 1                    # a refused call is never retried
    assert "unknown option: --frobnicate" in open(_report_path(capsys), encoding="utf-8").read()


def test_round_timeout_resumes(env):
    repo, task, tmp = env
    # A timed-out round that changed the tree is a normal round whose session is
    # resumed; only timeouts that change nothing, twice in a row, are unusable.
    script(tmp, [{"rc": 5, "result": "", "write": {"value.txt": "a\n"}},
                 {"rc": 5, "result": "", "write": {"value.txt": "b\n"}},
                 {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    assert len(c) == 3
    assert c[1][c[1].index("--resume") + 1] == "s1"
    assert c[2][c[2].index("--resume") + 1] == "s1"


def test_round_timeout_that_fixed_the_tree_is_done(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 5, "result": "", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert len(calls(tmp)) == 1


def test_two_timeouts_with_no_changes_stop_with_8(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 5, "result": ""}])
    assert sup(repo, task) == 8
    assert len(calls(tmp)) == 2


def test_two_timeouts_reason_says_timed_out(env):
    repo, task, tmp = env
    # Two timeouts in a row say "timed out", not the generic "unusable": the
    # fix (raise the round timeout) differs, and the reason names it.
    script(tmp, [{"rc": 5, "result": ""}])
    assert sup(repo, task) == 8
    report = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "two rounds timed out with no change" in report


def test_server_error_after_backoff(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 4}])
    assert sup(repo, task) == 4
    assert len(calls(tmp)) == 4          # first try + three backoff retries


def test_preflight_failure_backs_off_then_exits_4(env):
    repo, task, tmp = env
    script(tmp, [{"rc": 3}])             # the server went away mid-run
    assert sup(repo, task) == 4
    assert len(calls(tmp)) == 4          # same backoff as rc 4; persisting stops with 4


def test_dirty_tree_is_refused_unless_allowed(env):
    repo, task, tmp = env
    (repo / "value.txt").write_text("dirty\n")
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 13
    assert sup(repo, task, "--allow-dirty") == 0


def test_lock_held(env):
    repo, task, tmp = env
    os.makedirs(os.path.join(supervisor.repo_state_dir(str(repo)), "lock"))
    script(tmp, [{"result": "x"}])
    assert sup(repo, task) == 14


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlinked_repo_uses_same_state_dir(env):
    repo, task, tmp = env
    link = tmp / "link-to-repo"
    os.symlink(str(repo), str(link))
    assert supervisor.repo_state_dir(str(link)) == supervisor.repo_state_dir(str(repo))
    assert history.transcript_dir(str(link)) == history.transcript_dir(str(repo))
    # one lock guards the checkout, whichever path it was reached through
    os.makedirs(os.path.join(supervisor.repo_state_dir(str(repo)), "lock"))
    script(tmp, [{"result": "x"}])
    assert supervisor.main(["--task", str(task), "--repo", str(link), "--agent", PY, "--agent", FAKE,
                            "--no-deviation-audit"]) == 14


def test_deviation_blocks_reach_the_log(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "## DEVIATION\nSPEC: s\nDID: d\nWHY: w\nEVIDENCE: e\n",
                  "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    report = [l for l in capsys.readouterr().out.splitlines() if l.startswith("report: ")][0][8:]
    log = os.path.join(os.path.dirname(report), "decisions.jsonl")
    assert json.loads(open(log).read().splitlines()[0])["did"] == "d"


def test_not_a_repo_is_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    t = tmp_path / "task.md"; t.write_text("- [ ] x\n")
    assert supervisor.main(["--task", str(t), "--repo", str(tmp_path), "--agent", PY, "--agent", FAKE]) == 2


def test_bad_task_file_is_usage(env):
    repo, _, tmp = env
    t = tmp / "bad.md"; t.write_text("prose only\n")
    assert supervisor.main(["--task", str(t), "--repo", str(repo), "--agent", PY, "--agent", FAKE]) == 2


def test_check_runner_failure_stops_with_a_report(env, monkeypatch, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "x"}])

    def boom(*a, **k):
        raise RuntimeError("lock held")
    monkeypatch.setattr(supervisor.checks, "run_checks", boom)
    assert sup(repo, task) == 8
    out = capsys.readouterr().out
    report = [l for l in out.splitlines() if l.startswith("report: ")][0][8:]
    assert "check runner failed: lock held" in open(report, encoding="utf-8").read()


def _report_path(capsys):
    return [l for l in capsys.readouterr().out.splitlines() if l.startswith("report: ")][0][8:]


def test_new_untracked_files_appear_in_diff_and_report(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "x", "write": {"value.txt": "good\n", "new_module.py": "print('hello new')\n"}}])
    assert sup(repo, task) == 0
    report = _report_path(capsys)
    patch = open(os.path.join(os.path.dirname(report), "diff.patch"), encoding="utf-8").read()
    assert "new_module.py" in patch and "hello new" in patch
    text = open(report, encoding="utf-8").read()
    assert "new files:" in text and "new_module.py" in text
    assert git(repo, "diff", "--cached").strip() == ""


def test_denied_tool_calls_are_recorded(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "a", "denied": ["Bash", "Write"]},
                 {"result": "b", "denied": ["Bash"], "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert "denied tool calls: 3 (Bash, Write)" in open(_report_path(capsys), encoding="utf-8").read()


def test_no_denials_says_zero(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "a", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert "denied tool calls: 0" in open(_report_path(capsys), encoding="utf-8").read()


def test_denials_add_fence_line_to_feedback(env):
    repo, task, tmp = env
    script(tmp, [{"result": "a", "denied": ["Bash", "Bash"]},
                 {"result": "b", "denied": ["Bash"], "write": {"value.txt": "meh\n"}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    line = ("%s Bash calls were denied last round. Only qwen-test runs; "
            "use Read, Grep and Glob for files.")
    p2 = open(c[1][c[1].index("-f") + 1], encoding="utf-8").read()
    assert "Not done" in p2 and p2.rstrip().endswith(line % 2)
    # The count is that round's, not the running total: round 3's prompt says 1,
    # not 2+1.
    p3 = open(c[2][c[2].index("-f") + 1], encoding="utf-8").read()
    assert p3.rstrip().endswith(line % 1) and (line % 3) not in p3
    # And no line at all when nothing was denied.
    git(repo, "checkout", "-q", "value.txt")
    (tmp / "record.jsonl.n").unlink()
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    second_run = calls(tmp)[4]                  # 3 calls from the first run
    text = open(second_run[second_run.index("-f") + 1], encoding="utf-8").read()
    assert "Not done" in text and "Bash calls were denied" not in text


def test_state_dir_inside_repo_is_usage(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_AGENT_STATE", str(repo / "state"))
    script(tmp, [{"result": "x"}])
    assert sup(repo, task) == 2


def test_non_integer_timeout_is_usage(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TEST_TIMEOUT", "soon")
    script(tmp, [{"result": "x"}])
    assert sup(repo, task) == 2


def test_run_dir_collision_gets_a_suffix(env, monkeypatch, capsys):
    repo, task, tmp = env
    monkeypatch.setattr(supervisor.time, "strftime", lambda *a: "20260101T000000Z")
    script(tmp, [{"result": "a", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    first = _report_path(capsys)
    git(repo, "checkout", "-q", "value.txt")
    assert sup(repo, task) == 0
    second = _report_path(capsys)
    assert first != second and second.split(os.sep)[-2].endswith("-2")


def test_non_ascii_untracked_name_is_in_diff_and_report(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "x", "write": {"value.txt": "good\n", "café x.py": "print('unicode name')\n"}}])
    assert sup(repo, task) == 0
    report = _report_path(capsys)
    patch = open(os.path.join(os.path.dirname(report), "diff.patch"), encoding="utf-8").read()
    assert "unicode name" in patch
    assert "café x.py" in open(report, encoding="utf-8").read()


def test_negative_backoff_is_usage(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_SUPERVISOR_BACKOFF", "5,-1")
    script(tmp, [{"result": "x"}])
    assert sup(repo, task) == 2


def test_parse_audit():
    assert supervisor.parse_audit("NO CONTRADICTIONS", 0) == []
    txt = ("## CONTRADICTION\nSPEC: retry 3\nCODE: src/n.py:4\nLOGGED: D1\n\n"
           "## CONTRADICTION\nSPEC: keep name\nCODE: src/n.py:9\nLOGGED: NONE\n\n"
           "## CONTRADICTION\nSPEC: x\nCODE: src/n.py:20\nLOGGED: D7\n")
    assert supervisor.parse_audit(txt, 1) == ["src/n.py:9", "src/n.py:20"]   # D7 does not exist
    assert supervisor.parse_audit("I looked and it seems fine", 0) is None


@pytest.mark.parametrize("value,logged", [
    ("D2", True), ("D2 (also D4, D6, D8, D9)", True), ("**D3**", True), ("`D3`", True),
    ("d5.", True), ("D10", False), ("D0", False), ("NONE", False), ("see D2", False),
    ("", False),
])
def test_parse_audit_accepts_a_leading_d_number(value, logged):
    txt = "## CONTRADICTION\nSPEC: s\nCODE: a.py:1\nLOGGED: %s\n" % value
    assert supervisor.parse_audit(txt, 9) == ([] if logged else ["a.py:1"])


def test_parse_audit_empty_value_does_not_swallow_next_line():
    txt = "## contradiction:\nSPEC:\nCODE: src/n.py:3\nLOGGED: none\n"
    assert supervisor.parse_audit(txt, 2) == ["src/n.py:3"]
    assert supervisor.parse_audit("## CONTRADICTION\nSPEC:\nCODE:\nLOGGED: D1\n", 1) == []


def _audited(env, steps):
    repo, task, tmp = env
    script(tmp, steps)
    return supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY, "--agent", FAKE])


def test_unlogged_deviation_keeps_the_task_open(env):
    rc = _audited(env, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},          # coder round 1
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: NONE\n"},  # audit 1
        {"result": "## DEVIATION\nSPEC: s\nDID: d\nWHY: w\nEVIDENCE: value.txt:1\n"},  # coder 2
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: D1\n"},     # audit 2
    ])
    assert rc == 0
    c = calls(env[2])
    assert c[1][c[1].index("-r") + 1] == "auditor" and "--test" not in c[1] and "--resume" not in c[1]
    p = open(c[2][c[2].index("-f") + 1], encoding="utf-8").read()
    assert "no DEVIATION entry" in p and "value.txt:1" in p


def test_parse_audit_tolerates_emphasis_but_only_whole_lines():
    assert supervisor.parse_audit("**No contradictions.**", 0) == []
    assert supervisor.parse_audit("NO CONTRADICTIONS.", 0) == []
    assert supervisor.parse_audit("ok\n`_No Contradictions!_`\n", 0) == []
    assert supervisor.parse_audit("I found no contradictions in the diff", 0) is None
    assert supervisor.parse_audit("No contradictions found.", 0) is None


def _audit_replies(tmp):
    return sorted((tmp / "state").rglob("audit-*.reply.md"))


def test_unusable_audit_then_parseable_retry_is_done(env):
    rc = _audited(env, [{"result": "fixed", "write": {"value.txt": "good\n"}},
                        {"result": "looks fine"}, {"result": "**No contradictions.**"}])
    assert rc == 0
    c = calls(env[2])
    assert len(c) == 3 and all(x[x.index("-r") + 1] == "auditor" for x in c[1:])
    assert "--resume" not in c[2]
    got = [p.read_text() for p in _audit_replies(env[2])]
    assert len(got) == 2 and set(got) == {"looks fine", "**No contradictions.**"}
    prompts = sorted((env[2] / "state").rglob("audit-*.prompt.md"))
    assert [p.name.split(".")[0] for p in prompts] == [p.name.split(".")[0] for p in _audit_replies(env[2])]


def test_unusable_audit_twice_stops_partial_without_reprompting_coder(env):
    rc = _audited(env, [{"result": "fixed", "write": {"value.txt": "good\n"}}, {"result": "looks fine"}])
    assert rc == 11
    c = calls(env[2])
    assert len(c) == 3 and c[0][c[0].index("-r") + 1] != "auditor"
    assert all(x[x.index("-r") + 1] == "auditor" for x in c[1:])
    reps = _audit_replies(env[2])
    assert len(reps) == 2
    report = next((env[2] / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "checks pass; deviation audit unusable" in report and "review the diff manually" in report
    assert all(str(p) in report for p in reps)


def test_audit_api_error_exits_4(env):
    rc = _audited(env, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},   # coder round 1 passes
        {"rc": 4},
    ])
    assert rc == 4
    c = calls(env[2])
    # call_agent's own backoff retried the API error; the audit loop added none of
    # its own (a second loop pass would mean 8 auditor calls, not 4).
    assert len(c) == 5 and all(x[x.index("-r") + 1] == "auditor" for x in c[1:])
    report = next((env[2] / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "server error after retries (deviation audit)" in report


def test_audit_usage_error_exits_2_with_stderr(env):
    rc = _audited(env, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},
        {"rc": 2, "stderr": "qwen-agent: unknown option: --frobnicate\n"},
    ])
    assert rc == 2
    c = calls(env[2])
    assert len(c) == 2 and c[1][c[1].index("-r") + 1] == "auditor"   # never retried
    report = next((env[2] / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "agent usage error (deviation audit)" in report
    # The refusal reason reaches the report exactly as a coder-round refusal does.
    assert "## Agent stderr" in report and "unknown option: --frobnicate" in report


def test_audit_timeout_reason_says_timed_out(env):
    rc = _audited(env, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},
        {"rc": 5, "result": ""},          # a timed-out audit gets its one retry...
    ])
    assert rc == 11
    assert len(calls(env[2])) == 3        # coder + audit + the retry, which also times out
    report = next((env[2] / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "deviation audit timed out" in report


def test_audit_drops_mutation_flags_from_passthrough(env):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}, {"result": "NO CONTRADICTIONS"}])
    rc = supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY, "--agent", FAKE, "--",
                          "--write", "-t", "Bash", "--tools=Edit", "--toolset", "Read,Bash",
                          "--toolset=Bash", "--all-tools", "--unrestricted", "--permission-mode",
                          "bypassPermissions", "--permission-mode=acceptEdits", "-m", "mod"])
    assert rc == 0
    coder, audit = calls(tmp)
    assert "--toolset" in coder and "--all-tools" in coder       # the coder gets them as given
    tail = audit[audit.index("-f") + 2:]
    assert tail == ["-m", "mod"]                                  # the audit keeps only the rest


def test_read_only_passthrough_keeps_other_options():
    assert supervisor._read_only_passthrough(
        ["--model", "a b", "--write", "-e", "low", "--permission-mode", "plan", "--ctx=9"]) == \
        ["--model", "a b", "-e", "low", "--ctx=9"]


def test_interrupt_message_names_the_real_resume_command(env, monkeypatch, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}])
    real = supervisor.call_agent
    n = {"calls": 0}

    def flaky(*a, **k):
        n["calls"] += 1
        if n["calls"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(supervisor, "call_agent", flaky)
    assert sup(repo, task) == 130
    err = capsys.readouterr().err
    assert 'qwen-agent --resume s1 "<prompt>"' in err
    assert "--until-done" not in err


FAKE_SLOW_AGENT = r'''#!/usr/bin/env bash
# Stands in for qwen-agent --test: holds a "worktree" that only its signal
# handler removes, and takes a moment to do it (as testrun --cleanup does).
wt="$FAKE_WT"
mkdir -p "$wt"
cleanup() { sleep 1; rm -rf "$wt"; exit 143; }
trap cleanup TERM INT
: > "$wt.ready"
while :; do sleep 0.1; done
'''


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_interrupt_lets_the_agent_remove_its_worktree(env):
    import signal
    import time
    repo, task, tmp = env
    agent = tmp / "slow-agent.sh"
    agent.write_text(FAKE_SLOW_AGENT)
    agent.chmod(0o755)
    wt = tmp / "wts" / "wt-fake"
    envv = dict(os.environ, FAKE_WT=str(wt), PYTHONPATH=os.path.dirname(os.path.dirname(supervisor.__file__)))
    p = subprocess.Popen([PY, supervisor.__file__, "--task", str(task), "--repo", str(repo),
                          "--agent", "bash", "--agent", str(agent), "--no-deviation-audit"],
                         env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while not os.path.exists(str(wt) + ".ready"):
        assert time.time() < deadline and p.poll() is None, p.communicate()
        time.sleep(0.05)
    assert wt.is_dir()
    p.send_signal(signal.SIGINT)                 # Ctrl-C reaches the supervisor
    out, err = p.communicate(timeout=60)
    assert p.returncode == 130, out + err
    assert not wt.exists(), "the agent was killed before it could clean up"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_to_supervisor_stops_agent_and_frees_lock(env):
    # Closing a terminal or killing the supervisor must not orphan the agent:
    # the agent runs in its own session, so the supervisor is the only thing
    # that can tell it to clean up -- and the repo lock must not outlive it.
    import signal
    import time
    repo, task, tmp = env
    agent = tmp / "slow-agent.sh"
    agent.write_text(FAKE_SLOW_AGENT)
    agent.chmod(0o755)
    wt = tmp / "wts" / "wt-fake"
    envv = dict(os.environ, FAKE_WT=str(wt), PYTHONPATH=os.path.dirname(os.path.dirname(supervisor.__file__)))
    p = subprocess.Popen([PY, supervisor.__file__, "--task", str(task), "--repo", str(repo),
                          "--agent", "bash", "--agent", str(agent), "--no-deviation-audit"],
                         env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while not os.path.exists(str(wt) + ".ready"):
        assert time.time() < deadline and p.poll() is None, p.communicate()
        time.sleep(0.05)
    assert wt.is_dir()
    lock = os.path.join(supervisor.repo_state_dir(str(repo)), "lock")
    assert os.path.isdir(lock)
    p.send_signal(signal.SIGTERM)                # no terminal, no Ctrl-C
    out, err = p.communicate(timeout=60)
    assert p.returncode == 130, out + err
    assert not wt.exists(), "the agent outlived the supervisor: it was orphaned"
    assert not os.path.exists(lock), "the next until-done run would be locked out"
    report = [l for l in out.splitlines() if l.startswith("report: ")][0][8:]
    assert "stop: interrupted (exit 130)" in open(report, encoding="utf-8").read()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_second_signal_during_cleanup_still_exits_130(env, monkeypatch, request):
    # The first signal starts the interrupt path; a second one landing mid-report
    # must not escape it -- the handler ignores everything once cleanup has
    # begun, so the report is finished, the lock released, and 130 returned.
    # (In-process here, and _run_agent's agent stop is covered by the SIGINT/
    # SIGTERM subprocess tests above; this pins the handler's second-signal arm.)
    import signal
    import time
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}])
    # These signals land on THIS process, so what the handler installs must not
    # outlive the test.
    saved = {getattr(signal, n): signal.getsignal(getattr(signal, n))
             for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)}

    def restore():
        for s, h in saved.items():
            signal.signal(s, h)
    request.addfinalizer(restore)

    def interrupt_during_call(*a, **k):
        os.kill(os.getpid(), signal.SIGINT)              # the first Ctrl-C
        time.sleep(0.3)                                 # let the handler raise
        raise KeyboardInterrupt                         # (a real one did)

    real_report = supervisor._report

    def second_signal_during_cleanup(*a, **k):
        path = real_report(*a, **k)
        os.kill(os.getpid(), signal.SIGTERM)             # and a second, mid-cleanup
        time.sleep(0.3)                                 # must be ignored, not raised
        return path

    monkeypatch.setattr(supervisor, "call_agent", interrupt_during_call)
    monkeypatch.setattr(supervisor, "_report", second_signal_during_cleanup)
    assert sup(repo, task) == 130
    assert not os.path.exists(os.path.join(supervisor.repo_state_dir(str(repo)), "lock"))


def test_interrupt_with_broken_stderr_exits_130(env, monkeypatch, capsys):
    # Closing the terminal under an interrupted run must not turn the interrupt
    # into exit 1: every print on the cleanup path swallows OSError, so run()
    # still returns 130 -- and the report on stdout is unaffected.
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "session_id": "s1"}])
    real = supervisor.call_agent
    n = {"calls": 0}

    def flaky(*a, **k):
        n["calls"] += 1
        if n["calls"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    class _DeadPipe:
        def write(self, *a):
            raise OSError(32, "Broken pipe")

        def flush(self, *a):
            raise OSError(32, "Broken pipe")

    monkeypatch.setattr(supervisor, "call_agent", flaky)
    monkeypatch.setattr(sys, "stderr", _DeadPipe())
    assert sup(repo, task) == 130
    assert "report:" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix" or not os.path.exists("/dev/full"),
                    reason="POSIX signals and /dev/full")
def test_real_process_dead_stderr_exits_130(env):
    # The in-process test above only covers the prints swallowing OSError. A
    # real interpreter gets one more chance at shutdown: flushing the dead
    # stderr there fails again and CPython leaves 120 where the report says
    # 130. The interrupt path must hard-exit without any shutdown -- and
    # with the lock released, which only a real subprocess can pin.
    import signal
    import time
    repo, task, tmp = env
    script(tmp, [{"result": "tried", "sleep": 60}])       # round 1 stays open
    envv = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(supervisor.__file__)))
    rec = tmp / "record.jsonl"
    with open("/dev/full", "w") as dead_stderr:
        p = subprocess.Popen([PY, supervisor.__file__, "--task", str(task), "--repo", str(repo),
                              "--agent", PY, "--agent", FAKE, "--no-deviation-audit"],
                             env=envv, stdout=subprocess.PIPE, stderr=dead_stderr, text=True)
        deadline = time.time() + 30
        while not rec.exists():                           # the fake agent has started its round
            assert time.time() < deadline and p.poll() is None, p.communicate()
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)                     # the terminal closes under it
        out, _ = p.communicate(timeout=90)
    assert p.returncode == 130, out                       # 120: CPython flushed at shutdown
    assert "report: " in out                              # the flush before os._exit reached us
    assert not os.path.exists(os.path.join(supervisor.repo_state_dir(str(repo)), "lock"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal masks")
def test_lock_creation_blocks_signals(env, monkeypatch, request):
    # A signal landing between os.mkdir(lock) and the `mine = True` below it
    # starts the cleanup path with mine still False: the lock outlives the
    # run and every later until-done exits 14. Handlers cannot guard a window
    # of two statements; only the signal mask can. So the signals must wait
    # in the kernel across it, land after the assignment, and the run must
    # still exit 130 with its own lock removed.
    import signal
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}])
    stop = {getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP")}
    start_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    saved = {s: signal.getsignal(s) for s in stop}

    def restore():
        for s in saved:                                   # a stray pending stop signal must
            signal.signal(s, signal.SIG_IGN)              # not kill the test session when
        signal.pthread_sigmask(signal.SIG_SETMASK, start_mask)   # the mask goes back...
        for s, h in saved.items():
            signal.signal(s, h)                           # ...and neither may its old handler
    request.addfinalizer(restore)

    real_mkdir = os.mkdir
    blocked_at_mkdir = []

    def watch_mkdir(path, *a, **k):
        if os.path.basename(str(path)) == "lock":
            blocked_at_mkdir.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            real_mkdir(path, *a, **k)
            os.kill(os.getpid(), signal.SIGTERM)          # land it inside the window
        else:
            real_mkdir(path, *a, **k)                     # os.makedirs reaches mkdir too

    monkeypatch.setattr(os, "mkdir", watch_mkdir)
    assert sup(repo, task) == 130                         # the signal still interrupts the run
    assert len(blocked_at_mkdir) == 1 and stop <= blocked_at_mkdir[0]
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == start_mask   # unblocked after
    assert not os.path.exists(os.path.join(supervisor.repo_state_dir(str(repo)), "lock")), \
        "the signal outran the `mine` assignment and the lock leaked"


def test_failure_output_reaches_the_coder(env):
    # The coder must see WHY a check failed, not only that it did: the next prompt carries
    # an excerpt of the failing check's output (test and cmd checks alike).
    repo, task, tmp = env
    (repo / "check.py").write_text(
        "import sys\nv = open('value.txt').read().strip()\n"
        "print('noise line')\nprint('AssertionError: expected good, got %s' % v)\nprint('tail line')\n"
        "sys.exit(0 if v == 'good' else 1)\n")
    (repo / "cmdcheck.py").write_text(
        "import sys\nv = open('value.txt').read().strip()\n"
        "print('cmd says: value is %s' % v)\nprint('last line')\nsys.exit(0 if v == 'good' else 1)\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "checks")
    task.write_text("# Goal\nMake the value good.\n\n"
                    "- [ ] value is good -- check: test ALL\n"
                    "- [ ] cmd agrees -- check: cmd %s cmdcheck.py\n" % PY.replace("\\", "/"))
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    p2 = open(c[1][c[1].index("-f") + 1], encoding="utf-8").read()
    assert "AssertionError: expected good, got bad" in p2      # test check: failure line
    assert "cmd says: value is bad" in p2                       # cmd check: not just its last line
    # The report keeps one short evidence line per item.
    report = next((tmp / "state").rglob("report.md")).read_text()
    assert "cmd says: value is bad" not in report


def test_until_done_passes_web_to_rounds(env):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY,
                            "--agent", FAKE, "--no-deviation-audit", "--", "--web"]) == 0
    c = calls(tmp)
    assert len(c) == 2 and all("--web" in argv for argv in c)


def test_tool_counts_per_round_and_report(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp / "transcripts"))
    script(tmp, [{"result": "tried", "tools": ["Read", "Read", "Task"]},
                 {"result": "fixed", "write": {"value.txt": "good\n"}, "tools": ["Read", "Bash"]}])
    assert sup(repo, task) == 0
    assert (tmp / "transcripts" / "s1.jsonl").is_file()
    r1 = json.loads(next((tmp / "state").rglob("round-1.json")).read_text(encoding="utf-8"))
    r2 = json.loads(next((tmp / "state").rglob("round-2.json")).read_text(encoding="utf-8"))
    assert r1["tools"] == {"Read": 2, "Task": 1}      # this round's calls, not the file's totals
    assert r2["tools"] == {"Read": 1, "Bash": 1}      # the transcript accumulates; the diff counts
    report = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "## Tool use" in report and "| tool | calls |" in report
    assert "| Read | 3 |" in report and "| Bash | 1 |" in report and "| Task | 1 |" in report
    # sorted by count, descending (names break ties)
    assert report.index("| Read | 3 |") < report.index("| Bash | 1 |") < report.index("| Task | 1 |")


def test_tool_counts_without_transcript(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp / "no-such-transcripts"))
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0                       # a missing transcript never fails the run
    r1 = json.loads(next((tmp / "state").rglob("round-1.json")).read_text(encoding="utf-8"))
    assert r1["tools"] == {}
    report = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "## Tool use" in report and "(no transcript found)" in report


def test_failure_output_in_feedback_is_bounded(env):
    repo, task, tmp = env
    (repo / "check.py").write_text(
        "import sys\nv = open('value.txt').read().strip()\nprint('x' * 200000)\n"
        "sys.exit(0 if v == 'good' else 1)\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "loud")
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    c = calls(tmp)
    p2 = open(c[1][c[1].index("-f") + 1], encoding="utf-8").read()
    assert len(p2.encode()) < 20000


def test_until_done_passes_subagents_to_rounds(env):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY,
                            "--agent", FAKE, "--no-deviation-audit", "--", "--subagents"]) == 0
    c = calls(tmp)
    assert len(c) == 2 and all("--subagents" in argv for argv in c)


def test_locked_message_says_how_to_clear_a_stale_lock(env, capsys):
    # A run killed with SIGKILL cannot remove its lock; exit 14 must say what to do.
    repo, task, tmp = env
    script(tmp, [{"result": "a"}])
    sup(repo, task, "--max-rounds", "1")                       # creates this repo's state dir
    run_dir = os.path.dirname(next((tmp / "state").rglob("task.md")))
    os.mkdir(os.path.join(os.path.dirname(run_dir), "lock"))   # what a killed run leaves
    capsys.readouterr()
    assert sup(repo, task) == 14
    err = capsys.readouterr().err
    assert "another run holds" in err and "remove that directory" in err


def test_tool_counts_restart_when_the_session_changes(env, monkeypatch):
    # A resume that comes back with a new session id has its own transcript; its calls
    # are counted from zero, not subtracted from the old session's totals.
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp / "transcripts"))
    script(tmp, [{"result": "a", "session_id": "s1", "tools": ["Read", "Read", "Read"]},
                 {"result": "b", "session_id": "s2", "tools": ["Read"],
                  "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run_dir = next((tmp / "state").rglob("round-2.json")).parent
    r2 = json.loads((run_dir / "round-2.json").read_text())
    assert r2["tools"] == {"Read": 1}
    assert "| Read | 4 |" in (run_dir / "report.md").read_text()


def test_web_warning_reaches_the_user_once(env, capsys):
    repo, task, tmp = env
    script(tmp, [{"result": "a", "write": {"value.txt": "good\n"}}])
    assert supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY,
                            "--agent", FAKE, "--no-deviation-audit", "--", "--web"]) == 0
    assert capsys.readouterr().err.count("WARNING: --web with --until-done") == 1


def test_timed_out_round_does_not_double_count_tools(env, monkeypatch):
    # A timed-out round returns no session id; the next round's counts must still be
    # diffed against the same session's earlier totals.
    repo, task, tmp = env
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp / "transcripts"))
    script(tmp, [{"result": "a", "session_id": "s1", "tools": ["Read", "Read"]},
                 {"rc": 5, "result": "", "session_id": "", "tools": []},
                 {"result": "c", "session_id": "s1", "tools": ["Read"], "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    run_dir = next((tmp / "state").rglob("round-3.json")).parent
    assert "| Read | 3 |" in (run_dir / "report.md").read_text()
