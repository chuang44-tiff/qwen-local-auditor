"""runners.py: the optimize workflow's timing wrapper and output parsers."""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "skill" / "local-auditor" / "lib" / "workflows" / "optimize" / "runners.py"


def _load():
    spec = importlib.util.spec_from_file_location("optimize_runners_t", str(SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load()

PYTEST = """\
============================= slowest durations ==============================
2.01s call     tests/test_a.py::test_slow
0.50s setup    tests/test_a.py::test_fast
0.30s call     tests/test_b.py::test_x
1 failed, 3 passed, 1 skipped, 2 warnings in 3.12s
"""
UNITTEST = "...s.\n----------------------------------------------------------------------\nRan 6 tests in 0.601s\n\nFAILED (failures=1, errors=1, skipped=1)\n"
JEST = "PASS src/a.test.js (5.123 s)\nFAIL src/b.test.js\nTests:       1 failed, 2 skipped, 10 passed, 13 total\nTime:        6.2 s\n"
VITEST = " Test Files  2 passed (2)\n      Tests  1 failed | 9 passed (10)\n"
GO = "=== RUN   TestA\n--- PASS: TestA (0.12s)\n--- FAIL: TestB (1.50s)\n--- SKIP: TestC (0.00s)\nFAIL\nFAIL\texample.com/p\t1.700s\n"
GO_QUIET = "ok  \texample.com/p\t0.123s\n"
CARGO = "test result: ok. 4 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\ntest result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"


@pytest.mark.parametrize("text,runner,c", [
    (PYTEST, "pytest", {"passed": 3, "failed": 1, "skipped": 1}),
    ("3 passed in 0.12s\n", "pytest", {"passed": 3, "failed": 0, "skipped": 0}),
    ("==== 2 passed, 1 error in 0.5s ====\n", "pytest", {"passed": 2, "failed": 1, "skipped": 0}),
    (UNITTEST, "unittest", {"passed": 3, "failed": 2, "skipped": 1}),
    ("Ran 2 tests in 0.001s\n\nOK\n", "unittest", {"passed": 2, "failed": 0, "skipped": 0}),
    (JEST, "jest", {"passed": 10, "failed": 1, "skipped": 2}),
    (VITEST, "vitest", {"passed": 9, "failed": 1, "skipped": 0}),
    (GO, "go", {"passed": 1, "failed": 1, "skipped": 1}),
    (CARGO, "cargo", {"passed": 6, "failed": 1, "skipped": 1}),
])
def test_counts(text, runner, c):
    assert R.counts(text) == (runner, c)


def test_go_without_v_is_known_but_uncounted():
    assert R.counts(GO_QUIET) == ("go", None)


def test_unknown_runner_gives_rc_only():
    assert R.counts("all good\n") == (None, None)
    out = R.summary("all good\n", 0, 1.5)
    res = R.parse_result(out)
    assert res["runner"] is None and res["passed"] is None and res["files"] is None
    assert res["rc"] == 0 and res["wall"] == 1.5


def test_pytest_timings_and_files():
    rows = R.timings("pytest", PYTEST)
    assert (2.01, "tests/test_a.py", "tests/test_a.py::test_slow") in rows
    res = R.parse_result(R.summary(PYTEST, 1, 3.2))
    assert res["files"] == ["tests/test_a.py", "tests/test_b.py"]
    assert res["failed"] == 1


def test_jest_file_timings():
    assert R.timings("jest", JEST) == [(5.123, "src/a.test.js", "src/a.test.js")]


def test_summary_fits_run_cmd_tail():
    rows = "".join("%.2fs call     tests/test_%03d_%s.py::test_x\n" % (1.0 + i / 100, i, "y" * 60)
                   for i in range(300))
    out = R.summary(rows + "300 passed in 9.0s\n" + "z" * 5000, 0, 9.0)
    assert len(out) <= 4000
    assert R.parse_result(out[-4000:])["passed"] == 300


def test_clip_terminates_and_bounds_escaped_length():
    for name in ("测" * 80, "测" * 20):
        result = R._clip(name)
        assert len(json.dumps(result)) <= R.NAME_CHARS + 2
    assert R._clip("a" * 80) == "a" * 80
    long_ascii = R._clip("a" * 200)
    assert len(long_ascii) == 80 and long_ascii.startswith("...")


def test_summary_fits_with_non_ascii_names():
    rows = "".join("%.2fs call     %s::test_x\n" % (1.0 + i / 10, "%s_%d.py" % ("测" * 70, i))
                   for i in range(10))
    out = R.summary(rows + "10 passed in 1.0s\n", 0, 1.0)
    assert len(out) <= 4000
    assert R.parse_result(out[-4000:])["passed"] == 10


def test_parse_result_takes_the_last_mark_and_rejects_garbage():
    assert R.parse_result("QLA_RESULT {\"rc\": 1}\nQLA_RESULT {\"rc\": 0}\n") == {"rc": 0}
    assert R.parse_result("QLA_RESULT not json\n") is None
    assert R.parse_result("nothing\n") is None


def test_outcome_rules():
    ok = {"applied": True, "rc": 0, "timed_out": False,
          "output_tail": "x\nQLA_RESULT " + json.dumps({"rc": 0, "wall": 1.0, "runner": "pytest",
                                                        "files": None, "passed": 2, "failed": 0,
                                                        "skipped": 0}) + "\n"}
    o = R.outcome(ok)
    assert o["ok"] and o["wall"] == 1.0 and o["passed"] == 2 and o["profile"] == "x"
    assert not R.outcome(dict(ok, applied=False))["ok"]
    assert not R.outcome(dict(ok, timed_out=True))["ok"]
    assert not R.outcome(dict(ok, output_tail="no mark"))["ok"]
    failed = dict(ok, output_tail=ok["output_tail"].replace('"failed": 0', '"failed": 1'))
    assert not R.outcome(failed)["ok"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_wrap_runs_times_and_keeps_the_exit_code(tmp_path):
    bash = shutil.which("bash")
    py = pathlib.Path(sys.executable).as_posix()
    cmd = '"%s" -c "import time; time.sleep(0.2); print(\'3 passed in 0.2s\')"' % py
    p = subprocess.run([bash, "-c", R.wrap(cmd, "label one", bash=bash)], cwd=str(tmp_path),
                       capture_output=True, text=True)
    assert p.returncode == 0
    res = R.parse_result(p.stdout)
    assert res["passed"] == 3 and res["wall"] >= 0.2
    p = subprocess.run([bash, "-c", R.wrap("exit 3", "label two", bash=bash)], cwd=str(tmp_path),
                       capture_output=True, text=True)
    assert p.returncode == 3 and R.parse_result(p.stdout)["rc"] == 3
    assert list(tmp_path.iterdir()) == []          # the output file lives outside the tree


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_wrap_survives_a_non_utf8_locale(tmp_path):
    bash = shutil.which("bash")
    py = pathlib.Path(sys.executable).as_posix()
    cmd = 'PYTHONIOENCODING=utf-8 "%s" -c "print(\'\\u6d4b\\u8bd5 1 passed in 0.1s\')"' % py
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    p = subprocess.run([bash, "-c", R.wrap(cmd, "enc", bash=bash)], cwd=str(tmp_path),
                       capture_output=True, env=env)
    assert p.returncode == 0
    assert R.parse_result(p.stdout.decode("utf-8"))["passed"] == 1


def test_wrap_differs_by_label():
    assert R.wrap("pytest", "a") != R.wrap("pytest", "b")
