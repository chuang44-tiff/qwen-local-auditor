"""The deeper prompts and the delegation PUSH (--subagents-push), offline.

auditor-deep gained the 1b coverage list, the finish check and the COVERAGE section;
coder-deep gained its finish check with NOT CHECKED; the review prompt gained the
REVIEW ledger and, under push, hands re-verification to a subagent. The push makes
delegation mandatory and REPLACES the nudge text when both apply; --deep and the
implied default now push, while qwen-sweep batches and the swarm role default keep
nudging (a swarm role opts into push through its manifest "deep" list).

The fakes, the server and the run helpers are test_cli's and test_cli_deep's; the
sweep side reads the recorded batch commands of test_sweep_cli's fake dispatcher.
"""
import shutil

import test_cli_deep
import test_sweep_cli
from test_cli import flag, posix, run
from test_cli_deep import calls, dirty_repo, go, sha, sys_prompt
from test_cli_default_depth import DEEP_ENV
from test_sweep_cli import files_args, sweep
from test_sweep_swarm_depth import batches, role_workflow, unit_from

# the fake claude + model server of test_cli_deep, and the recording dispatcher
# fixtures of test_sweep_cli (assignment so pytest sees them as this module's)
server = test_cli_deep.server
fake = test_cli_deep.fake
repo = test_sweep_cli.repo
dispatch = test_sweep_cli.dispatch

PUSH = "Delegation is part of this task"
NUDGE = "Delegate more than feels necessary."

# sha256 of the deep texts: auditor-deep with the "1b. Coverage" entry-point list and
# the step-6 finish check (COVERAGE section), coder-deep with its finish check (NOT
# CHECKED), and REVIEW_PROMPT with the REVIEW ledger. A change to any of them is a
# deliberate change of what the model is told, and must re-pin here.
CHANGED_TEXTS = {
    "auditor-deep": "ce9eca260b0eaf65b4ac2a997ad6721788c443ed43ed63c025c2e14a00fe3d5a",
    "coder-deep": "a8f7959a293ae82b767a71f2f458ae57fd73ec52e7c92231ba48b1ab4808e888",
    "review-prompt": "c2b5781be228b67eec746341255f7cf0d604b8a4c398734e86947a436440194f",
}

# the other built-in role texts, pinned
UNCHANGED_TEXTS = {
    "plain": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "auditor": "dbcc0b8e488b3f4fc5ab2afc65903656dcb84b2cb55d7e467e7ba55c25d47e81",
    "coder": "ef4b57eae82a310a5b8cb9595d53e1be6719bb21e46d5d27252409b1d56ce652",
    "mechanic": "1648791f6b4b6e46f0c62696bb763bae30b5bb9aa3d8ac8a8d924ab27f34238d",
    # the tester text carries the black-box sentence (the source-reaching browser
    # tools are hidden by default)
    "tester": "640d1ebeec3e33228cb89c10d60e3dc4fca3998fabdac7e9418de6178df3bc1e",
}


def reset(tmp_path):
    shutil.rmtree(str(tmp_path / "calls"))
    (tmp_path / "calls").mkdir()


# ------------------------------------------------------------------ the deeper texts

def test_auditor_deep_text_has_coverage_and_finish_check(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--role-variant", "deep", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    p = sys_prompt(calls(tmp_path)[0][0])
    assert "1b. Coverage" in p and "Finish check" in p and "COVERAGE section" in p
    assert "Default to FAIL when evidence is missing for a guarantee that matters." in p
    assert sha(p) == CHANGED_TEXTS["auditor-deep"]     # the text changed on purpose


def test_coder_deep_text_has_finish_check(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "coder", "--role-variant", "deep", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    p = sys_prompt(calls(tmp_path)[0][0])
    assert "Finish check" in p and "NOT CHECKED" in p and "EDGE CASES" in p
    assert sha(p) == CHANGED_TEXTS["coder-deep"]       # the text changed on purpose


def test_review_prompt_asks_for_review_section(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--review-round", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    (_, _), (a2, _) = calls(tmp_path)
    assert "under REVIEW" in a2[-1]
    assert sha(a2[-1]) == CHANGED_TEXTS["review-prompt"]   # changed on purpose


def test_other_role_texts_unchanged(tmp_path, server, fake):
    # the push and the deeper prompts must not breathe on any other role text
    for role, digest in UNCHANGED_TEXTS.items():
        reset(tmp_path)
        extra = {"QWEN_BROWSER_DIR": posix(tmp_path / "browser")} if role == "tester" else None
        assert go(tmp_path, ["-r", role, "hi"], server, fake, extra=extra).returncode == 0
        assert sha(sys_prompt(calls(tmp_path)[0][0])) == digest, role


# ------------------------------------------------------------------ --subagents-push

def test_push_replaces_nudge(tmp_path, server, fake):
    r = go(tmp_path, ["--subagents-push", "--subagents-nudge", "-r", "auditor", "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = calls(tmp_path)[0]
    p = sys_prompt(argv)
    assert PUSH in p                                   # the mandate stands...
    assert NUDGE not in p                              # ...and the suggestion is gone
    assert "DELEGATION section" in p
    assert "delegate broad reading and searching" in p         # the Task note is kept
    assert p.index("delegate broad reading and searching") < p.index(PUSH)
    assert "Task" in flag(argv, "--tools").split(",")          # push implies --subagents
    assert "Task" in flag(argv, "--allowed-tools").split(",")


def test_push_refuses_value_and_interactive(tmp_path, fake):
    r = run(tmp_path, ["--subagents-push=1", "hi"], None, fake)
    assert r.returncode == 2
    assert "option --subagents-push takes no value (got '--subagents-push=1')" in r.stderr
    assert calls(tmp_path) == []                             # claude never started
    r = run(tmp_path, ["--interactive", "--dry-run", "--subagents-push"], fake=fake)
    assert r.returncode == 2 and "--subagents-push" in r.stderr


def test_deep_uses_push(tmp_path, server, fake):
    repo_ = dirty_repo(tmp_path)
    r = go(tmp_path, ["--deep", "-r", "auditor", "-C", posix(repo_), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    (a1, _), _ = calls(tmp_path)
    p = sys_prompt(a1)
    assert PUSH in p and NUDGE not in p


def test_default_depth_uses_push(tmp_path, server, fake):
    repo_ = dirty_repo(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "-C", posix(repo_), "hi"], server, fake,
           extra=dict(DEEP_ENV))
    assert r.returncode == 0, r.stderr
    (a1, _), _ = calls(tmp_path)
    p = sys_prompt(a1)
    assert PUSH in p and NUDGE not in p


def test_review_with_push_asks_for_subagent_reverification(tmp_path, server, fake):
    r = go(tmp_path, ["-r", "auditor", "--review-round", "--subagents-push", "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    _, (a2, _) = calls(tmp_path)
    assert ("Hand the re-verification of your three most important claims to a subagent, "
            "and compare its result with yours.") in a2[-1]
    assert "under REVIEW" in a2[-1]                          # the base review prompt stands
    # a nudge-only review round keeps REVIEW_PROMPT verbatim: no subagent sentence
    reset(tmp_path)
    r = go(tmp_path, ["-r", "auditor", "--review-round", "--subagents-nudge", "hi"],
           server, fake)
    assert r.returncode == 0, r.stderr
    _, (b2, _) = calls(tmp_path)
    assert "Hand the re-verification" not in b2[-1]


# ------------------------------------------------------------------ sweep/swarm keep nudge

def test_sweep_and_swarm_keep_nudge(tmp_path, repo, dispatch):
    # the batches a sweep dispatches and the units a swarm runs (role default depth)
    # still get the nudge -- only --deep and the implied default depth push
    out, rec = tmp_path / "run", tmp_path / "rec.txt"
    r = sweep(tmp_path, files_args(repo, out), dispatch, extra={"FAKE_RECORD": posix(rec)})
    assert r.returncode == 0, r.stdout + r.stderr
    got = batches(rec)
    assert got, "nothing was dispatched"
    for argv in got:
        assert "--subagents-nudge" in argv, argv
        assert "--subagents-push" not in argv, argv
    m = role_workflow(tmp_path)
    u, sw = unit_from(tmp_path, m.roles["worker"])
    argv = sw._argv(u, tmp_path / "p.md", 600)
    assert "--subagents-nudge" in argv and "--subagents-push" not in argv


def test_swarm_role_can_opt_into_push(tmp_path):
    # "deep": ["subagents_push"] is the opt-in; "subagents" stays the nudge. The
    # cache key carries the switch list, so the push unit never reuses a nudge's row.
    m_push = role_workflow(tmp_path / "push", deep=["subagents_push"])
    assert m_push.roles["worker"].deep == ("subagents_push",)
    u, sw = unit_from(tmp_path, m_push.roles["worker"])
    argv = sw._argv(u, tmp_path / "p.md", 600)
    assert "--subagents-push" in argv and "--subagents-nudge" not in argv
    assert "--review-round" not in argv                      # only what the list names
    m_nudge = role_workflow(tmp_path / "nudge", deep=["subagents"])
    u_nudge, _ = unit_from(tmp_path, m_nudge.roles["worker"])
    assert sw._key(u) != sw._key(u_nudge)
    # a list naming both still means push only (qwen-agent's own rule)
    m_both = role_workflow(tmp_path / "both", deep=["review_round", "subagents", "subagents_push"])
    assert m_both.roles["worker"].deep == ("review_round", "subagents", "subagents_push")
    u_both, _ = unit_from(tmp_path, m_both.roles["worker"])
    argv = sw._argv(u_both, tmp_path / "p.md", 600)
    assert "--subagents-push" in argv and "--subagents-nudge" not in argv
    assert "--review-round" in argv
