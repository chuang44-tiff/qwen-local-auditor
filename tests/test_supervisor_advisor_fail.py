# ruff: noqa: F811
# The loop tests import the `env` fixture from test_supervisor (below) for pytest to
# find BY NAME, so every test's `env` parameter re-binds a name ruff reads as an unused
# import -- the fixture use is pytest's, invisible to a linter. F811 is off for this file.
"""The --until-done failure trigger: the supervisor asks the advisor after a failing round."""
import json
import subprocess
import sys

import pytest

from lib import supervisor

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake claude is a POSIX script")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_ADV_DIR"]
n = len([f for f in os.listdir(d) if f.startswith("stdin.")]) + 1
open(os.path.join(d, "stdin.%d" % n), "w").write(sys.stdin.read())
mode = os.environ.get("FAKE_ADV_MODE", "ok")
if mode == "err":
    print(json.dumps({"type": "result", "is_error": True, "result": "usage limit"})); sys.exit(1)
print(json.dumps({"type": "result", "is_error": False,
                  "result": "The value must be the word good, not meh.",
                  "total_cost_usd": 0.2, "modelUsage": {"claude-opus-test": {}}}))
'''


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    p = tmp_path / "claude"
    p.write_text(FAKE_CLAUDE, encoding="utf-8")
    p.chmod(0o755)
    d = tmp_path / "advcalls"
    d.mkdir()
    monkeypatch.setenv("QWEN_ADVISOR_CLAUDE", str(p))
    monkeypatch.setenv("FAKE_ADV_DIR", str(d))
    return d


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q"); git(r, "config", "user.email", "t@example.com"); git(r, "config", "user.name", "t")
    (r / "value.txt").write_text("bad\n")
    git(r, "add", "-A"); git(r, "commit", "-qm", "init")
    return r


def test_context_puts_failures_first_and_fits():
    ctx = supervisor.harness_context("F" * 20000, "D" * 20000, "T" * 20000)
    assert len(ctx) <= 12000
    assert ctx.startswith("FAILING CHECKS:\n")
    assert ctx.index("FAILING CHECKS") < ctx.index("DIFF SINCE") < ctx.index("THE TASK")
    assert "characters cut]" in ctx
    assert ctx.count("F") >= 5000


def test_context_short_inputs_are_not_cut():
    ctx = supervisor.harness_context("item 1 failed", "", "make it good")
    assert "item 1 failed" in ctx and "make it good" in ctx and "(no changes)" in ctx
    assert "characters cut]" not in ctx


def test_changed_files_skips_binary_and_big_untracked(repo):
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "value.txt").write_text("meh\n")
    (repo / "new.py").write_text("x = 1\n")
    (repo / "blob.bin").write_bytes(b"\0\1\2" * 10)
    (repo / "big.txt").write_text("a" * 2_000_000)
    got = supervisor.changed_files(str(repo), start)
    assert "value.txt" in got and "new.py" in got
    assert "blob.bin" not in got and "big.txt" not in got
    assert len(got) <= 5


@posix_only
def test_harness_advice_ok(repo, tmp_path, fake_claude):
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "value.txt").write_bytes(b"meh \xff\n")          # non-UTF-8 in the diff
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, str(tmp_path / "h.md"),
                                           str(repo), start, "TEST ALL FAILED", "Make it good")
    assert "the word good" in text
    assert info == {"called": True, "skipped_reason": None, "cost_usd": 0.2}
    sent = (fake_claude / "stdin.1").read_text()
    assert supervisor.HARNESS_Q in sent and "TEST ALL FAILED" in sent and "value.txt" in sent


@posix_only
def test_harness_advice_failure_is_a_skip(repo, tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_ADV_MODE", "err")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo),
                                           git(repo, "rev-parse", "HEAD").strip(), "F", "T")
    assert text is None and info["called"] is False and "usage limit" in info["skipped_reason"]


def test_interrupt_is_not_swallowed(repo, tmp_path, monkeypatch):
    # Review focus 5: Ctrl-C during the call stops the run the normal way.
    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(supervisor.advisor_mcp, "ask", boom)
    state = tmp_path / "hstate"; state.mkdir()
    with pytest.raises(KeyboardInterrupt):
        supervisor.harness_advice("opus", str(state), 1, "", str(repo),
                                  git(repo, "rev-parse", "HEAD").strip(), "F", "T")


def test_harness_advice_budget_spent_is_a_skip(repo, tmp_path):
    state = tmp_path / "hstate"; state.mkdir()
    (state / "count").write_text("1")
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo),
                                           git(repo, "rev-parse", "HEAD").strip(), "F", "T")
    assert text is None and "budget" in info["skipped_reason"]


@posix_only
def test_harness_call_leaves_the_local_budget_untouched(repo, tmp_path, fake_claude):
    # The run's advisor directory (the coder rounds' one) and the harness's subdirectory
    # are separate state dirs: the failure call spends ONLY the harness's own count.
    (repo / "value.txt").write_text("meh\n")
    run_dir = tmp_path / "run"; run_dir.mkdir()
    (run_dir / "count").write_text("0")
    harness = run_dir / "harness"; harness.mkdir()
    text, info = supervisor.harness_advice("opus", str(harness), 4, "", str(repo),
                                           git(repo, "rev-parse", "HEAD").strip(), "F", "T")
    assert text and info["called"]
    assert (run_dir / "count").read_text() == "0"       # the local model's budget stands
    assert (harness / "count").read_text() == "1"


@posix_only
def test_secret_files_stay_out_of_the_diff(repo, tmp_path, fake_claude):
    (repo / ".env").write_text("TOP_SECRET=abc123\n", encoding="utf-8")
    git(repo, "add", ".env"); git(repo, "commit", "-qm", "track a secret")
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / ".env").write_text("TOP_SECRET=abc123\nTOP_SECRET_ROTATED=leak999\n", encoding="utf-8")
    (repo / "value.txt").write_text("meh\n", encoding="utf-8")
    (repo / "prod.secret").write_text("untracked-nope\n", encoding="utf-8")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "F", "Make it good")
    assert text and info["called"]
    sent = (fake_claude / "stdin.1").read_text()
    assert "diff --git a/value.txt" in sent and "meh" in sent   # the ordinary change rides, diffed
    assert "TOP_SECRET" not in sent and "leak999" not in sent   # no line of the .env diff
    assert ".env" not in sent                           # not even its name, nor an attachment
    assert "prod.secret" not in sent and "untracked-nope" not in sent   # new secrets not named


@posix_only
def test_secret_with_odd_name_stays_out_of_the_diff(repo, tmp_path, fake_claude):
    # git porcelain octal-quotes the non-ASCII name (core.quotePath) and keeps the
    # space one unquoted: the exclude pathspecs are built from `-z` output, so both
    # real paths reach git and both files really leave the diff.
    (repo / "clé.pem").write_text("PRIV_KEY_MIRROR=1\n", encoding="utf-8")
    (repo / "signing key.pem").write_text("KEYBODY=2\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "track odd-named keys")
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "clé.pem").write_text("PRIV_KEY_MIRROR=rotated\n", encoding="utf-8")
    (repo / "signing key.pem").write_text("KEYBODY=rotated\n", encoding="utf-8")
    (repo / "value.txt").write_text("meh\n", encoding="utf-8")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "F", "Make it good")
    assert text and info["called"]
    sent = (fake_claude / "stdin.1").read_text()
    assert "diff --git a/value.txt" in sent and "meh" in sent   # real diff, not the fallback
    assert "PRIV_KEY_MIRROR" not in sent and "KEYBODY" not in sent
    assert "clé.pem" not in sent and "signing key.pem" not in sent


@posix_only
def test_secret_with_globbing_name_is_excluded_exact(repo, tmp_path, fake_claude):
    # A magic pathspec matches its body as a glob: [1] is a one-character class, so
    # without the literal magic the exclusion silently misses and the content rides.
    (repo / "key[1].pem").write_text("GLOBLEAK=1\n", encoding="utf-8")
    (repo / "value.txt").write_text("bad\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "glob-named key")
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "key[1].pem").write_text("GLOBLEAK=rotated\n", encoding="utf-8")
    (repo / "value.txt").write_text("meh\n", encoding="utf-8")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "F", "Make it good")
    assert text and info["called"]
    sent = (fake_claude / "stdin.1").read_text()
    assert "GLOBLEAK" not in sent
    # exact exclusion, not the whole-diff fallback: the real diff (not "(diff omitted")
    # still rides -- with a glob body the literal magic is what makes the match.
    assert "diff --git a/value.txt" in sent and "meh" in sent


@posix_only
def test_secret_with_undecodable_name_drops_the_diff(repo, tmp_path, fake_claude):
    # A raw 0xFF byte in the name: the text channel hands it back replaced, so no
    # textual pathspec can ever name it. The verification step notices and the WHOLE
    # diff is held back rather than that file's content -- the advisor still gets the
    # failing output and the task.
    name = "bad\udcffname.pem"
    try:
        (repo / name).write_text("BYTELEAK=1\n", encoding="utf-8")
    except OSError:
        # macOS (APFS) refuses names that are not valid UTF-8 (EILSEQ): such a file
        # cannot exist there, so neither can the leak this test guards against.
        pytest.skip("this filesystem refuses non-UTF-8 file names")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "raw-byte name")
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / name).write_text("BYTELEAK=rotated\n", encoding="utf-8")
    (repo / "value.txt").write_text("meh\n", encoding="utf-8")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "CHECK FAILED", "Make it good")
    assert text and info["called"]                      # the call itself still happens
    sent = (fake_claude / "stdin.1").read_text()
    assert "BYTELEAK" not in sent
    assert "(diff omitted" in sent                      # the fallback really engaged
    assert "CHECK FAILED" in sent and "Make it good" in sent


@posix_only
def test_secret_deletion_and_untouched_secret_do_not_ride(repo, tmp_path, fake_claude):
    # A secret only REMOVED this round (its old content is in the diff) and a secret
    # unchanged since the start: neither's content may reach the advisor.
    (repo / "old.env").write_text("GONE=1\n", encoding="utf-8")
    (repo / "quiet.env").write_text("QUIET=shh\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "secrets")
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "old.env").unlink()
    (repo / "value.txt").write_text("meh\n", encoding="utf-8")
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "F", "Make it good")
    assert text and info["called"]
    sent = (fake_claude / "stdin.1").read_text()
    assert "GONE=1" not in sent and "QUIET=shh" not in sent
    assert "diff --git a/value.txt" in sent and "meh" in sent   # the ordinary diff still rides


def test_changed_files_skips_secret_paths(repo):
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "value.txt").write_text("meh\n")
    (repo / "untracked.env").write_text("X=1\n")
    (repo / "server.pem").write_text("-----BEGIN\n")
    got = supervisor.changed_files(str(repo), start)
    assert "value.txt" in got and "untracked.env" not in got and "server.pem" not in got


@posix_only
def test_harness_advice_attaches_check_files(repo, tmp_path, fake_claude):
    # check.py sits in HEAD untouched: nothing but check_files can bring it to the call.
    (repo / "check.py").write_text("raise SystemExit('CHECK_MARKER_42')\n", encoding="utf-8")
    git(repo, "add", "check.py"); git(repo, "commit", "-qm", "a check script")
    start = git(repo, "rev-parse", "HEAD").strip()
    state = tmp_path / "hstate"; state.mkdir()
    text, info = supervisor.harness_advice("opus", str(state), 1, "", str(repo), start,
                                           "CHECK FAILED", "Make it good", check_files=["check.py"])
    assert text and info["called"]
    sent = (fake_claude / "stdin.1").read_text()
    assert "FILE check.py" in sent and "CHECK_MARKER_42" in sent


def test_attachments_are_check_files_first_deduped_and_capped(repo, tmp_path, monkeypatch):
    start = git(repo, "rev-parse", "HEAD").strip()
    (repo / "value.txt").write_text("meh\n")            # the changed file a real list has
    seen = {}

    def fake_ask(question, context, paths, env, root):
        seen["paths"] = list(paths)
        return "ADVICE ok"
    monkeypatch.setattr(supervisor.advisor_mcp, "ask", fake_ask)
    text, info = supervisor.harness_advice(
        "opus", str(tmp_path / "hstate"), 1, "", str(repo), start, "F", "T",
        check_files=["check.py", "check.py", "value.txt", "x1", "x2", "x3", "x4"])
    assert text == "ADVICE ok" and info["called"]
    # deduplicated, check_files first (value.txt keeps its check_files place, not the
    # changed_files one), at most the 5 advisor_mcp accepts.
    assert seen["paths"] == ["check.py", "value.txt", "x1", "x2", "x3"]


from test_supervisor import FAKE, PY, calls, env, script, sup  # noqa: E402,F401


def _prompt(call):
    return open(call[call.index("-f") + 1], encoding="utf-8").read()


def _harness_log(tmp):
    import glob
    paths = glob.glob(str(tmp / "state" / "*" / "*" / "advisor-harness.jsonl"))
    return [json.loads(l) for p in paths for l in open(p, encoding="utf-8") if l.strip()]


@pytest.fixture
def stub_advice(monkeypatch):
    seen = []

    def fake(model, state_dir, budget, log_path, repo, start, fails, task, check_files=()):
        seen.append({"budget": budget, "fails": fails, "state_dir": state_dir,
                     "check_files": list(check_files)})
        return "Use the word good.", {"called": True, "skipped_reason": None, "cost_usd": 0.2}
    monkeypatch.setattr(supervisor, "harness_advice", fake)
    return seen


def test_failing_round_gets_advice_in_next_prompt(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus", "--max-rounds", "2") == 0
    c = calls(tmp)
    p2 = _prompt(c[1])
    assert "Not done" in p2 and "# Advice from a stronger model on these failures" in p2
    assert "Use the word good." in p2
    assert len(stub_advice) == 1 and stub_advice[0]["budget"] == 1
    assert "value is good" in stub_advice[0]["fails"]
    assert stub_advice[0]["state_dir"].endswith("harness")
    log = _harness_log(tmp)
    assert log[0]["round"] == 1 and log[0]["called"] and log[0]["reply"] == "fixed"


def test_round_one_passes_no_call(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus") == 0
    assert stub_advice == []


def test_failing_last_round_no_call(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}])
    assert sup(repo, task, "--advisor", "opus", "--max-rounds", "1") == 11
    assert stub_advice == []


def test_triggers_none_is_the_old_behaviour(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus", "--advisor-triggers", "none") == 0
    assert stub_advice == [] and "Advice from a stronger model" not in _prompt(calls(tmp)[1])


def test_no_advisor_no_call(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task) == 0
    assert stub_advice == []


def test_skipped_call_leaves_plain_not_done(env, monkeypatch):
    repo, task, tmp = env
    monkeypatch.setattr(supervisor, "harness_advice", lambda *a, **k: (
        None, {"called": False, "skipped_reason": "Unavailable: no answer within 600s",
               "cost_usd": None}))
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus") == 0
    p2 = _prompt(calls(tmp)[1])
    assert "Not done" in p2 and "Advice from a stronger model" not in p2
    assert _harness_log(tmp)[0]["skipped_reason"].startswith("Unavailable")


def test_timed_out_round_gets_no_call(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"rc": 5, "result": ""}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    sup(repo, task, "--advisor", "opus")
    assert stub_advice == []
    # An unusable round skips the whole failing-round branch: the next prompt is the
    # plain timeout continue prompt, with no advice block riding along.
    p2 = _prompt(calls(tmp)[1])
    assert "Your last round ran out of time" in p2
    assert "Advice from a stronger model" not in p2


def test_harness_budget_is_rounds_minus_one(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "meh\n"}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus", "--max-rounds", "3") == 0
    assert [s["budget"] for s in stub_advice] == [2, 2]


@pytest.mark.parametrize("val", ["plan", "bogus", "fail,bogus", ""])
def test_bad_trigger_values_are_usage_errors(env, val):
    repo, task, tmp = env
    assert sup(repo, task, "--advisor", "opus", "--advisor-triggers", val) == 2


def test_triggers_without_advisor_is_usage_error(env):
    repo, task, tmp = env
    assert sup(repo, task, "--advisor-triggers", "fail") == 2


def _sup_audited(repo, task, *extra):
    """sup() without its --no-deviation-audit: the audit runs, and a checks-pass round
    with an unlogged contradiction is a failing round whose `failing` list is empty."""
    return supervisor.main(["--task", str(task), "--repo", str(repo), "--agent", PY,
                            "--agent", FAKE, *extra])


def test_unlogged_deviation_only_gets_no_call(env, stub_advice):
    repo, task, tmp = env
    # The steps follow test_supervisor.test_unlogged_deviation_keeps_the_task_open:
    # round 1 passes the checks, the audit finds an unlogged contradiction, round 2
    # logs it as a DEVIATION and the run is done.
    script(tmp, [
        {"result": "fixed", "write": {"value.txt": "good\n"}},
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: NONE\n"},
        {"result": "## DEVIATION\nSPEC: s\nDID: d\nWHY: w\nEVIDENCE: value.txt:1\n"},
        {"result": "## CONTRADICTION\nSPEC: s\nCODE: value.txt:1\nLOGGED: D1\n"},
    ])
    assert _sup_audited(repo, task, "--advisor", "opus") == 0
    assert stub_advice == []                            # no failing check to advise on
    log = _harness_log(tmp)
    assert len(log) == 1 and log[0]["called"] is False and log[0]["round"] == 1
    assert log[0]["skipped_reason"] == "no failing checks (unlogged deviations only)"
    assert log[0]["reply"] == ("## DEVIATION\nSPEC: s\nDID: d\nWHY: w\nEVIDENCE: value.txt:1\n")
    import glob
    text = open(glob.glob(str(tmp / "state" / "*" / "*" / "report.md"))[0], encoding="utf-8").read()
    assert "## Notes" in text
    assert "advisor after round 1 skipped: no failing checks (unlogged deviations only)" in text
    assert "harness calls: 0" in text and "harness skipped: 1" in text
    # The next prompt still names the deviations -- and carries no advice block.
    p2 = _prompt(calls(tmp)[2])
    assert "no DEVIATION entry" in p2 and "Advice from a stronger model" not in p2


def test_failing_cmd_check_names_its_script(env, stub_advice):
    repo, task, tmp = env
    task.write_text("# Goal\nMake the value good.\n\n"
                    "- [ ] value is good -- check: cmd %s check.py\n" % PY.replace("\\", "/"))
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus") == 0
    # The failing check's own script travels to the call; the interpreter (an absolute
    # path) does not.
    assert len(stub_advice) == 1 and stub_advice[0]["check_files"] == ["check.py"]


def test_report_counts_calls_and_skips(env, monkeypatch):
    repo, task, tmp = env
    answers = iter([
        ("Use the word good.", {"called": True, "skipped_reason": None, "cost_usd": 0.2}),
        (None, {"called": False, "skipped_reason": "Unavailable: no answer within 600s",
                "cost_usd": None}),
    ])
    monkeypatch.setattr(supervisor, "harness_advice", lambda *a, **k: next(answers))
    script(tmp, [{"result": "a"}, {"result": "b", "write": {"value.txt": "meh\n"}},
                 {"result": "c", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus", "--max-rounds", "3") == 0
    log = _harness_log(tmp)
    assert len(log) == 2 and log[0]["called"] and not log[1]["called"]
    import glob
    text = open(glob.glob(str(tmp / "state" / "*" / "*" / "report.md"))[0], encoding="utf-8").read()
    assert "harness calls: 1" in text and "harness skipped: 1" in text


def test_failed_flush_does_not_mask_the_round_error(env, monkeypatch):
    # Crash path with a pending harness record: the record is written inside
    # contextlib.suppress(OSError), so a write that fails neither replaces the round's
    # own error in the report nor takes the report (and the exit 8) down with it.
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "never"}])
    monkeypatch.setattr(supervisor, "harness_advice", lambda *a, **k: (
        "ask anyway", {"called": True, "skipped_reason": None, "cost_usd": 0.1}))
    real_sig, real_open = supervisor._tree_sig, open
    state = {"n": 0, "armed": False}

    def sig_third_time_fails(r):
        # Round 1 asks for the before and after signatures; round 2's before-signature
        # is the third call -- the loop dies there, with round 1's record still pending.
        state["n"] += 1
        if state["n"] == 3:
            state["armed"] = True
            raise RuntimeError("the round died")
        return real_sig(r)

    def guarded_open(file, *a, **k):
        mode = a[0] if a else k.get("mode", "r")
        if state["armed"] and str(file).endswith("advisor-harness.jsonl") and "a" in mode:
            raise OSError(28, "No space left on device")
        return real_open(file, *a, **k)

    monkeypatch.setattr(supervisor, "_tree_sig", sig_third_time_fails)
    # raising=False: supervisor has no module-global `open` to replace -- adding one
    # is what routes its bare open() lookups through the guard (builtins stay clean).
    monkeypatch.setattr(supervisor, "open", guarded_open, raising=False)
    assert sup(repo, task, "--advisor", "opus", "--probe") == 8
    text = next((tmp / "state").rglob("report.md")).read_text(encoding="utf-8")
    assert "error: RuntimeError: the round died" in text
    # The attempted write never landed -- proof the suppression guarded a real flush:
    # without the crash this exact run would have logged its record there.
    assert list((tmp / "state").rglob("advisor-harness.jsonl")) == []


def test_report_lists_harness_calls(env, stub_advice):
    repo, task, tmp = env
    script(tmp, [{"result": "tried"}, {"result": "fixed", "write": {"value.txt": "good\n"}}])
    assert sup(repo, task, "--advisor", "opus") == 0
    import glob
    text = open(glob.glob(str(tmp / "state" / "*" / "*" / "report.md"))[0], encoding="utf-8").read()
    assert "harness calls: 1" in text and "### Advice after failing rounds" in text
    assert "Use the word good." in text
    # The agent's own advisor lines are separate lines of their own (the rounds asked
    # nobody, so their count is 0) -- the harness lines neither replace nor inflate them.
    assert "\ncalls: 0\n" in text and "budget: 4" in text
