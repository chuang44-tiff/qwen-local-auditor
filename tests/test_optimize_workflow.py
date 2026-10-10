"""The optimize workflow on a fixture repo with a slow test and fake agents."""
import json
import os
import pathlib
import shutil
import sys

import pytest

from lib.swarm_engine import runner
from swarm_fixtures import agent_args, git_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PY = pathlib.Path(sys.executable).as_posix()
SLOW = {"slow.py": "import time\nDELAY = 0.6\n\n\ndef work():\n    time.sleep(DELAY)\n    return 42\n",
        "test_slow.py": "import slow\n\n\ndef test_work():\n    assert slow.work() == 42\n",
        "test_other.py": "def test_other():\n    assert 1 + 1 == 2\n"}
PYTEST = '"%s" -m pytest -q -p no:cacheprovider' % PY
MEASURE = PYTEST + " test_slow.py"
GUARD = PYTEST

CHECKER_OK = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"verdict": "ok", "findings": [], "suggested_bench": None}) + "\n```"
'''
CHECKER_PROBLEM = r'''
import json, re
def answer(p, r):
    line = re.search(r"^\s*[\d.]+\s+test_slow\.py\S*$", p, re.M).group(0).strip()
    return 0, "```json\n" + json.dumps({"verdict": "problem",
        "findings": [{"claim": "time is in test_slow", "evidence": line},
                     {"claim": "made up", "evidence": "this line is nowhere at all"}],
        "suggested_bench": {"measure": "x", "focus": ["slow.py"]}}) + "\n```"
'''
CHECKER_INVENTED = r'''
import json
def answer(p, r):
    return 0, "```json\n" + json.dumps({"verdict": "problem",
        "findings": [{"claim": "made up", "evidence": "this line is nowhere at all"}],
        "suggested_bench": None}) + "\n```"
'''
WRITER = "def answer(p, r):\n    return 0, '# Optimize\\n\\nslow.py sleeps less.\\n'\n"


def write_fakes(d, **roles):
    for name, src in roles.items():
        (d / ("%s.py" % name)).write_text(src, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    write_fakes(d, checker=CHECKER_OK, writer=WRITER)
    monkeypatch.setenv("FAKE_SWARM_DIR", str(d))
    monkeypatch.setenv("FAKE_SWARM_SLEEP", "0")
    b = os.environ.get("TEST_BASH") or shutil.which("bash")
    if b:
        monkeypatch.setenv("QWEN_SWARM_BASH", b)
    return d


def bench_file(tmp_path, **fields):
    data = {"measure": MEASURE, "guard": GUARD, "focus": ["slow.py", "test_slow.py"]}
    data.update(fields)
    p = tmp_path / "bench.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def optimize(tmp_path, repo, *extra, depth="quick"):
    run = tmp_path / "run"
    rc = runner.main(agent_args() + ["optimize", "make test_slow faster", "--target", str(repo),
                                     "--out", str(run), "--depth", depth, *extra])
    return rc, run


def report(run):
    return (run / "report.md").read_text(encoding="utf-8")


def test_no_bench_and_no_tests_is_exit_2(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo)
    assert rc == 2 and "--set bench=" in capsys.readouterr().err
    assert not run.exists()


def test_a_bad_bench_file_is_exit_2(tmp_path, env, capsys):
    repo = git_repo(tmp_path / "repo", SLOW)
    p = bench_file(tmp_path, speed="fast")
    rc, _ = optimize(tmp_path, repo, "--set", "bench=%s" % p)
    assert rc == 2 and "unknown bench field" in capsys.readouterr().err


def test_failing_measure_stops_before_round_1(tmp_path, env):
    repo = git_repo(tmp_path / "repo", dict(SLOW, **{"test_slow.py": "def test_x():\n    assert False\n"}))
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path))
    assert rc == 4
    first = report(run).splitlines()[0]
    assert first.startswith("bench: problem (see bench-review.md): B1")
    review = (run / "bench-review.md").read_text(encoding="utf-8")
    assert "B1 FAIL" in review and "rc 1" in review
    events = [json.loads(x) for x in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"verdict": "problem", "phase": "start"}.items() <= [e for e in events if e["kind"] == "bench"][0].items()
    assert not (run / "ideas.json").exists()


def test_focus_outside_the_profile_is_a_note_not_a_stop(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, focus=["slow.py"]))
    assert report(run).splitlines()[0] == "bench: ok"
    assert "B3 skipped: note: no profiled file is inside focus" in (run / "bench-review.md").read_text(encoding="utf-8")


def test_target_inside_the_noise_stops(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path, target="-0.1%"))
    assert rc == 4 and "B4" in report(run).splitlines()[0]


def test_checker_problem_with_real_evidence_stops_and_suggests(tmp_path, env):
    write_fakes(env, checker=CHECKER_PROBLEM)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path))
    assert rc == 4 and "checker: time is in test_slow" in report(run).splitlines()[0]
    review = (run / "bench-review.md").read_text(encoding="utf-8")
    assert "time is in test_slow" in review
    assert "Dropped (evidence not found" in review and "made up" in review
    assert json.loads((run / "bench-suggested.json").read_text(encoding="utf-8"))["focus"] == ["slow.py"]


def test_checker_finding_without_evidence_does_not_stop(tmp_path, env):
    write_fakes(env, checker=CHECKER_INVENTED)
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(tmp_path))
    assert report(run).splitlines()[0] == "bench: ok"
    assert "made up" in (run / "bench-review.md").read_text(encoding="utf-8")


def test_tests_knob_alone_times_the_whole_suite(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "tests=%s" % GUARD)
    saved = json.loads((run / "bench.json").read_text(encoding="utf-8"))
    assert saved["measure"] == GUARD and saved["focus"] == [] and saved["target"] is None
    assert report(run).splitlines()[0] == "bench: ok"


def test_profile_falls_back_when_flags_break_the_command(tmp_path, env):
    # appending the flags to `... && exit 0` gives `exit 0 --durations=0 ...`, which bash
    # refuses (too many arguments, rc 1): the plain baseline output is then the profile
    repo = git_repo(tmp_path / "repo", SLOW)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % bench_file(
        tmp_path, measure=MEASURE + " && exit 0", focus=[]))
    assert report(run).splitlines()[0] == "bench: ok"


def test_resume_uses_the_saved_bench(tmp_path, env):
    repo = git_repo(tmp_path / "repo", SLOW)
    p = bench_file(tmp_path)
    rc, run = optimize(tmp_path, repo, "--set", "bench=%s" % p)
    p.write_text("{", encoding="utf-8")                     # now unreadable
    rc2 = runner.main(agent_args() + ["optimize", "--resume", str(run)])
    assert rc2 == rc
