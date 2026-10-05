"""The debug workflow on a fixture repo with a planted bug and fake agents."""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from lib.swarm_engine import runner
from swarm_fixtures import agent_args, git_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PY = pathlib.Path(sys.executable).as_posix()
REPRO = '"%s" -c "import calc, sys; sys.exit(0 if calc.add(2, 3) == 5 else 1)"' % PY
TESTS = '"%s" -c "import test_calc"' % PY
BUGGY = {"calc.py": "def add(a, b):\n    return a - b\n",
         "test_calc.py": "import calc\nassert calc.add(1, 1) == 2\n"}

TRIAGER = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"files": ["calc.py:2"], "notes": "add subtracts",
                                       "repro": ""}) + "\n```"
'''
HYPOTHESIZER = r'''
import json
def answer(p, r):
    hyps = [{"location": "calc.py:2", "mechanism": "add uses minus", "evidence_needed": "read it"},
            {"location": "test_calc.py:2", "mechanism": "the test is wrong", "evidence_needed": "x"},
            {"location": "calc.py:2", "mechanism": "fix plus a test edit", "evidence_needed": "y"}]
    return 0, "```json\n" + json.dumps(hyps) + "\n```"
'''
# H1 fixes the code; H2 only edits the test (the repro still fails); H3 fixes the code AND
# edits the test (passes, but touches_tests, so it can never win)
PROBER = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    hid = re.search(r"^- (H\d+):", p, re.M).group(1)
    d = pathlib.Path(cwd)
    if hid in ("H1", "H3"):
        (d / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8", newline="\n")
    if hid in ("H2", "H3"):
        (d / "test_calc.py").write_text("import calc\nassert calc.add(1, 1) == 2  # checked\n",
                                        encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": hid, "verdict": "confirmed", "evidence": "calc.py:2"}) + "\n```"
'''
REVIEWER = r'''
import json, re
def answer(p, r):
    ids = re.findall(r"^- (P\d+) ", p, re.M)
    return 0, "```json\n" + json.dumps([{"patch": i, "verdict": "root-cause", "reason": "r"} for i in ids]) + "\n```"
'''
PLANNER = "def answer(p, r):\n    return 0, '```json\\n[]\\n```'\n"
WRITER = "def answer(p, r):\n    return 0, '# Root cause\\n\\nadd subtracts instead of adding.\\n'\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    for name, src in (("triager", TRIAGER), ("hypothesizer", HYPOTHESIZER), ("prober", PROBER),
                      ("reviewer", REVIEWER), ("planner", PLANNER), ("writer", WRITER)):
        (d / ("%s.py" % name)).write_text(src, encoding="utf-8")
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    b = os.environ.get("TEST_BASH") or shutil.which("bash")
    if b:
        monkeypatch.setenv("QWEN_SWARM_BASH", b)
    return d


def debug(tmp_path, repo, *extra):
    run = tmp_path / "run"
    rc = runner.main(agent_args() + ["debug", "add(2, 3) is not 5", "--target", str(repo),
                                     "--out", str(run), "--depth", "quick", *extra])
    return rc, run


def git(repo, *args):
    return subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, text=True,
                          check=True).stdout


def test_a_planted_bug_gets_a_checked_patch(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", BUGGY)
    rc, run = debug(tmp_path, repo, "--set", "repro=" + REPRO, "--set", "tests=" + TESTS)
    assert rc == 0, capsys.readouterr().err
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["winner"] == "P1"
    by = {c["hypothesis"]: c for c in summary["candidates"]}
    assert by["H1"]["passes"] and by["H1"]["approved"] and not by["H1"]["touches_tests"]
    assert not by["H2"]["passes"] and by["H2"]["touches_tests"]      # a test-only edit fails
    assert by["H3"]["passes"] and by["H3"]["touches_tests"]          # passes, still no winner
    first = (run / "patches" / "1.diff").read_text(encoding="utf-8")
    assert "+    return a + b" in first and "test_calc.py" not in first
    report = (run / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Root cause") and "P1 (patches/1.diff)" in report
    # the writer is shown the winning patch, fenced like the review items
    prompt = (run / "agents" / "write-1.prompt.md").read_text(encoding="utf-8")
    assert "# Winning patch\n\n```diff\n" in prompt and "+    return a + b" in prompt
    # the user's tree is untouched: no edit, no leftover worktree, no sandbox left behind
    assert (repo / "calc.py").read_text(encoding="utf-8") == BUGGY["calc.py"]
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "worktree", "list").count("\n") == 1
    assert not (run / "sandboxes").exists() or not any((run / "sandboxes").iterdir())
    names = {q.name[:-len(".prompt.md")] for q in (run / "agents").glob("*.prompt.md")}
    assert {"triage-1", "hypothesize-1", "probe-1", "probe-2", "probe-3", "review-1", "write-1"} <= set(names)
    for a in json.loads("[%s]" % ",".join((env / "calls.jsonl").read_text(encoding="utf-8").splitlines())):
        if "--preflight-only" in a:
            continue
        cwd = pathlib.Path(a[a.index("-C") + 1])
        role = pathlib.Path(a[a.index("--role-file") + 1]).stem
        if role == "prober":
            assert cwd.parent == run / "sandboxes"                   # never the user's tree
        elif role in ("triager", "hypothesizer", "reviewer"):
            assert cwd == repo.resolve() and a[a.index("--toolset") + 1] == "Read,Glob,Grep"


def test_no_reproduction_exits_4_without_guessing(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    rc, run = debug(tmp_path, repo, "--set", "repro=true")
    assert rc == 4
    assert "did not reproduce" in (run / "report.md").read_text(encoding="utf-8")
    assert not list((run / "agents").glob("hypothesize-*")) and not list((run / "agents").glob("probe-*"))


def test_a_missing_repro_uses_the_triagers_proposal(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "triager.py").write_text(TRIAGER.replace('"repro": ""', '"repro": %r' % REPRO),
                                    encoding="utf-8")
    rc, run = debug(tmp_path, repo)
    assert rc == 0
    assert json.loads((run / "summary.json").read_text(encoding="utf-8"))["winner"] == "P1"


def test_no_winner_is_exit_4_with_the_report(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "reviewer.py").write_text(REVIEWER.replace('"root-cause"', '"symptom"'), encoding="utf-8")
    rc, run = debug(tmp_path, repo, "--set", "repro=" + REPRO)
    assert rc == 4
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "## Winner\n\nnone:" in report and (run / "patches" / "1.diff").exists()


def test_debug_needs_a_target(tmp_path, env, capsys):
    assert runner.main(agent_args() + ["debug", "x", "--out", str(tmp_path / "run")]) == 2
    assert "needs --target" in capsys.readouterr().err


def test_touches_tests_rule():
    wf = runner.load_module(runner.BUILTIN / "debug")
    def p(path):
        return "diff --git a/%s b/%s\n" % (path, path)
    for path in ("tests/x.py", "pkg/test/x.go", "spec/a.rb", "test_a.py", "a_test.go",
                 "src/a.spec.ts"):
        assert wf.touches_tests(p(path)), path
    for path in ("src/attest.py", "contest/x.py", "testing.py", "src/a.py"):
        assert not wf.touches_tests(p(path)), path


# A prober that leaves a real fix in its copy but answers "refuted": the diff must be
# ignored, not turned into a candidate patch.
REFUTER = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    hid = re.search(r"^- (H\d+):", p, re.M).group(1)
    (pathlib.Path(cwd) / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                               encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": hid, "verdict": "refuted",
                                        "evidence": "a print showed the plus already ran"}) + "\n```"
'''
HYPOTHESIZER_ONE = r'''
import json
def answer(p, r):
    hyps = [{"location": "calc.py:2", "mechanism": "add subtracts", "evidence_needed": "read it"}]
    return 0, "```json\n" + json.dumps(hyps) + "\n```"
'''
# H1 gets a symptom fix (it special-cases the repro input); anything later gets the real fix
PROBER_SYMPTOM_THEN_FIX = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    hid = re.search(r"^- (H\d+):", p, re.M).group(1)
    fix = ("def add(a, b):\n    return a + b\n" if hid != "H1" else
           "def add(a, b):\n    return 5 if (a, b) == (2, 3) else a - b\n")
    (pathlib.Path(cwd) / "calc.py").write_text(fix, encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": hid, "verdict": "confirmed", "evidence": "calc.py:2"}) + "\n```"
'''
REVIEWER_REAL_FIX_ONLY = r'''
import json, re
def answer(p, r):
    verdict = "root-cause" if "return a + b" in p else "symptom"
    ids = re.findall(r"^- (P\d+) ", p, re.M)
    return 0, "```json\n" + json.dumps([{"patch": i, "verdict": verdict, "reason": "r"} for i in ids]) + "\n```"
'''
PLANNER_NEW = r'''
import json
def answer(p, r):
    hyps = [{"location": "calc.py:2", "mechanism": "add computes a minus b for every input",
             "evidence_needed": "read add"}]
    return 0, "```json\n" + json.dumps(hyps) + "\n```"
'''


def test_pycache_does_not_mark_a_fix_as_touching_tests():
    wf = runner.load_module(runner.BUILTIN / "debug")
    patch = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
             " def add(a, b):\n-    return a - b\n+    return a + b\n"
             "diff --git a/__pycache__/test_calc.cpython-312.pyc b/__pycache__/test_calc.cpython-312.pyc\n"
             "--- /dev/null\n+++ b/__pycache__/test_calc.cpython-312.pyc\n@@ -0,0 +1 @@\n+binary\n")
    assert not wf.touches_tests(patch)          # the pyc is the probe's own bytecode cache
    assert wf.touches_tests("diff --git a/test_calc.py b/test_calc.py\n")   # real edits still flag


def test_planner_sees_voted_down_candidates():
    wf = runner.load_module(runner.BUILTIN / "debug")
    voted_down = {"id": "P1", "hypothesis": "H1", "patch": "diff --git a/calc.py b/calc.py\n",
                  "touches_tests": False, "lines": 2, "approved": False, "verdict": "refuted",
                  "votes": [{"claim": "P1", "verdict": "refuted", "reason": "special-cases"}],
                  "applied": True, "repro_rc": 0, "tests_rc": None, "passes": True}
    winner = dict(voted_down, id="P2", hypothesis="H2", approved=True, verdict="supported")
    failed = wf._failed({"candidates": [voted_down, winner], "hypotheses": [], "probes": []})
    assert "P1 (for H1)" in failed and "reviewers judged it a symptom fix" in failed
    assert "P2" not in failed                   # the winner is not a failure to explain
    prompt = wf.PLAN_P.format(symptom="s", tried="(none)", failed=failed, n=3)
    assert "P1 (for H1)" in prompt and "symptom" in prompt


def test_refuted_probe_contributes_no_candidate(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "prober.py").write_text(REFUTER, encoding="utf-8")
    rc, run = debug(tmp_path, repo, "--set", "repro=" + REPRO)
    assert rc == 4
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["winner"] is None and summary["candidates"] == []
    assert not (run / "patches").exists() or not list((run / "patches").iterdir())
    log = (run / "run.log").read_text(encoding="utf-8")
    for h in ("H1", "H2", "H3"):
        assert "%s: refuted" % h in log and "ignored" in log, h
    # the probe really did leave a fix in its sandbox -- and the workflow ignored it
    assert "return a + b" in (run / "agents" / "probe-1" / "patch.diff").read_text(encoding="utf-8")


def test_two_rounds_reach_a_fix(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "hypothesizer.py").write_text(HYPOTHESIZER_ONE, encoding="utf-8")
    (env / "prober.py").write_text(PROBER_SYMPTOM_THEN_FIX, encoding="utf-8")
    (env / "reviewer.py").write_text(REVIEWER_REAL_FIX_ONLY, encoding="utf-8")
    (env / "planner.py").write_text(PLANNER_NEW, encoding="utf-8")
    run = tmp_path / "run"
    rc = runner.main(agent_args() + ["debug", "add(2, 3) is not 5", "--target", str(repo),
                                     "--out", str(run), "--depth", "standard",
                                     "--set", "repro=" + REPRO])
    assert rc == 0, capsys.readouterr().err
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["winner"] == "P2"            # round 1's P1 was voted down; round 2 won
    assert {c["id"] for c in summary["candidates"]} == {"P1", "P2"}    # both are listed
    by = {c["id"]: c for c in summary["candidates"]}
    assert by["P1"]["hypothesis"] == "H1" and not by["P1"]["approved"]
    assert by["P2"]["hypothesis"] == "H2" and by["P2"]["passes"] and by["P2"]["approved"]
    first = (run / "patches" / "1.diff").read_text(encoding="utf-8")
    assert "+    return a + b" in first and "5 if" not in first
    assert "P2 (patches/1.diff)" in (run / "report.md").read_text(encoding="utf-8")
    # the planner saw the voted-down patch and continued the hypothesis numbering with H2
    plan = (run / "agents" / "r2-plan-1.prompt.md").read_text(encoding="utf-8")
    assert "P1 (for H1)" in plan and "symptom" in plan
    assert '"H2"' in (run / "round-2" / "hypotheses.json").read_text(encoding="utf-8")


def test_helper_rules():
    wf = runner.load_module(runner.BUILTIN / "debug")
    def p(path):
        return "diff --git a/%s b/%s\n" % (path, path)
    # quoted git paths, unquoted back to text, the a/ b/ side prefixes off
    assert wf.patch_paths('diff --git "a/x y" "b/x y"\n') == ["x y"]
    assert wf.patch_paths('diff --git "a/\\303\\274.py" "b/\\303\\274.py"\n') == ["ü.py"]
    assert wf.patch_paths('diff --git "a/\\303" "b/\\303"\n') == ["\udcc3"]
    assert wf.patch_paths("diff --git a/plain b/plain\n") == ["plain"]
    assert wf.touches_tests('diff --git "a/test_x y.py" "b/test_x y.py"\n')
    # jest / __tests__ / conftest.py / FooTest.ext, while artifacts are ignored
    for path in ("src/foo.test.js", "__tests__/foo.js", "pkg/__tests__/deep/foo.js",
                 "conftest.py", "app/FooTest.java", "app/FooTests.java"):
        assert wf.touches_tests(p(path)), path
    for path in ("Latest.java", "protest.py", "foo.testjs", "__tests__x/a.js", "src/a.py",
                 "__pycache__/test_calc.cpython-312.pyc", "node_modules/test_a.js",
                 "pkg.egg-info/tests/a.txt", "x/conftest.py.bak", "a/b.pyo"):
        assert not wf.touches_tests(p(path)), path
    # +/- lines inside hunks only: the "--- comment" of a removed "-- comment" counts,
    # the file headers never do
    assert wf._changed_lines("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,1 @@\n"
                             " keep\n--- comment\n") == 1
    assert wf._changed_lines("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
                             "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-c\n") == 3
    # a blank line inside a hunk is context, not a change: "" is a prefix of "+-", which
    # is why the check is on the line's own first character
    assert wf._changed_lines("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n"
                             "-a\n\n+b\n") == 2
    assert wf._changed_lines("@@ -1 +1 @@\n\n\n") == 0
    # ties break on the NUMBER in the id: P2 before P10
    def cand(pid):
        return {"id": pid, "passes": True, "approved": True, "touches_tests": False, "lines": 2}
    assert [c["id"] for c in sorted([cand("P10"), cand("P2")], key=wf._rank)] == ["P2", "P10"]
    # a multi-line python -c repro survives parse_triage verbatim (only stripped)
    cmd = 'python -c "\nimport calc\nassert calc.add(2, 3) == 5\n"'
    got = wf.parse_triage("```json\n%s\n```" % json.dumps({"files": [], "notes": "n", "repro": cmd}))
    assert got["repro"] == cmd
    # validate refuses degenerate knobs, naming each
    base = {"voters": 3, "hypotheses": 5, "max_agents": 8, "depth": "standard"}
    bad = wf.validate(dict(base, voters=0))
    assert bad and "voters" in bad
    bad = wf.validate(dict(base, hypotheses=0))
    assert bad and "hypotheses" in bad
    assert wf.validate(base) is None


def test_patch_paths_spaces_and_renames():
    wf = runner.load_module(runner.BUILTIN / "debug")
    # git does not quote a path merely for a space: the rename header is ambiguous,
    # the rename from/to lines name both paths exactly
    rename = ("diff --git a/src/a b.py b/tests/c.py\nsimilarity index 95%\n"
              "rename from src/a b.py\nrename to tests/c.py\n")
    paths = wf.patch_paths(rename)
    assert "src/a b.py" in paths and "tests/c.py" in paths
    assert wf.touches_tests(rename)
    edit = ("diff --git a/my dir/x.py b/my dir/x.py\nindex 1234567..89abcde 100644\n"
            "--- a/my dir/x.py\n+++ b/my dir/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert wf.patch_paths(edit) == ["my dir/x.py"]
    # a binary-only change has no ---/+++ lines: the unambiguous header answers it
    binary = ("diff --git a/img x.png b/img x.png\n"
              "Binary files a/img x.png b/img x.png differ\n")
    assert wf.patch_paths(binary) == ["img x.png"]
    # quoted paths still unquote, from the header and from ---/+++ lines alike
    assert wf.patch_paths('diff --git "a/\\303\\274 p.py" "b/\\303\\274 p.py"\n') == ["ü p.py"]
    assert wf.patch_paths('diff --git "a/\\303\\274 p.py" "b/\\303\\274 p.py"\n'
                          '--- "a/\\303\\274 p.py"\n+++ "b/\\303\\274 p.py"\n@@ -1 +1 @@\n-a\n+b\n') == ["ü p.py"]


def test_review_prompt_clips_and_fences():
    wf = runner.load_module(runner.BUILTIN / "debug")
    patch = "````\n" + "x" * (30000 - len("````\n"))        # 30000 chars, four backticks inside
    assert len(patch) == 30000
    item = wf._review_item({"id": "P1", "hypothesis": "H1", "patch": patch})
    prompt = wf.REVIEW_P.format(symptom="s", items=item)
    assert "[clipped]" in prompt                            # longer than PATCH_CLIP
    assert "`" * 5 in prompt                               # a fence longer than four backticks


def test_a_too_long_patch_is_no_winner_and_says_so():
    wf = runner.load_module(runner.BUILTIN / "debug")
    def cand(pid, patch):
        return {"id": pid, "hypothesis": "H" + pid[1:], "patch": patch, "applied": True,
                "repro_rc": 0, "tests_rc": None, "passes": True, "approved": True,
                "verdict": "supported", "votes": [], "touches_tests": False,
                "lines": wf._changed_lines(patch)}
    short = cand("P1", "diff --git a/calc.py b/calc.py\n@@ -1 +1 @@\n-a\n+b\n")
    long_ = cand("P2", "diff --git a/calc.py b/calc.py\n@@ -1 +1 @@\n-" + "+" * wf.PATCH_CLIP)
    assert wf._too_long(long_) and not wf._too_long(short)
    # the reviewers only ever judged the clipped text, so the whole patch cannot win
    assert wf._winner([long_, short]) is short and wf._winner([long_]) is None
    failed = wf._failed({"candidates": [long_, short], "hypotheses": [], "probes": []})
    assert "P2 (for H2): too long to review" in failed


# A prober that fixes the bug AND leaves a 50 KB file behind: the patch passes every check
# and the reviewers approve it, but it is longer than the clip they were shown.
PROBER_HUGE = r'''
import json, pathlib, re
def answer_cwd(p, r, cwd):
    hid = re.search(r"^- (H\d+):", p, re.M).group(1)
    d = pathlib.Path(cwd)
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8", newline="\n")
    (d / "big.txt").write_text("".join("padding line %04d\n" % i for i in range(2500)),
                               encoding="utf-8", newline="\n")
    return 0, "```json\n" + json.dumps({"id": hid, "verdict": "confirmed",
                                        "evidence": "calc.py:2"}) + "\n```"
'''


def test_a_patch_too_long_to_review_cannot_win_a_real_run(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "prober.py").write_text(PROBER_HUGE, encoding="utf-8")
    rc, run = debug(tmp_path, repo, "--set", "repro=" + REPRO)
    assert rc == 4
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["winner"] is None and len(summary["candidates"]) == 3
    assert all(c["passes"] and c["approved"] and not c["touches_tests"]
               for c in summary["candidates"])          # they lost on length, nothing else
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "too long to review" in report and "## Winner\n\nnone:" in report


# A hypothesizer whose unit fails (the fake exits 1): the run stops there.
HYPO_FAIL = "def answer(p, r):\n    return 1, ''\n"


def test_failed_hypothesizer_stops_with_reason(tmp_path, env):
    repo = git_repo(tmp_path / "repo", BUGGY)
    (env / "hypothesizer.py").write_text(HYPO_FAIL, encoding="utf-8")
    run = tmp_path / "run"
    rc = runner.main(agent_args() + ["debug", "add(2, 3) is not 5", "--target", str(repo),
                                     "--out", str(run), "--depth", "standard",
                                     "--set", "repro=" + REPRO])
    assert rc == 4
    assert not list((run / "agents").glob("probe-*"))       # the run stopped at the hypotheses
    totals = json.loads((run / "totals.json").read_text(encoding="utf-8"))
    assert "hypothesizer failed" in totals["stop_reason"]
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "hypothesizer failed" in report
