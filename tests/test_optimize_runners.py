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


def test_two_pytest_summaries_sum():
    text = "============================= 2 passed in 0.10s ==============================\n" \
           "======================== 4 passed, 1 skipped in 0.20s =========================\n"
    assert R.counts(text) == ("pytest", {"passed": 6, "failed": 0, "skipped": 1})


def test_unittest_then_pytest_sum():
    text = "Ran 2 tests in 0.02s\n\nOK\n============================= 3 passed in 0.30s =====\n"
    assert R.counts(text) == ("unittest", {"passed": 5, "failed": 0, "skipped": 0})


def test_two_unittest_runs_sum():
    text = ("Ran 4 tests in 0.10s\n\nFAILED (failures=1)\n"
            "----------------------------------------------------------------------\nRan 2 tests in 0.05s\n\nOK\n")
    assert R.counts(text) == ("unittest", {"passed": 5, "failed": 1, "skipped": 0})


def test_two_jest_runs_sum():
    text = ("Tests:       1 failed, 2 skipped, 10 passed, 13 total\nTime:        6.2 s\n"
            "Tests:       3 passed, 3 total\nTime:        1.0 s\n")
    assert R.counts(text) == ("jest", {"passed": 13, "failed": 1, "skipped": 2})


def test_two_vitest_runs_sum():
    text = " Test Files  2 passed (2)\n      Tests  1 failed | 9 passed (10)\n" \
           " Test Files  1 passed (1)\n      Tests  2 passed (2)\n"
    assert R.counts(text) == ("vitest", {"passed": 11, "failed": 1, "skipped": 0})


def test_each_unittest_run_pairs_only_with_its_own_result():
    # the OK belongs to the first run and cannot leak to the second: pairing is by order
    text = "Ran 3 tests in 0.10s\n\nOK\nRan 2 tests in 0.10s\n\nFAILED (failures=1)\n"
    assert R.counts(text) == ("unittest", {"passed": 4, "failed": 1, "skipped": 0})


def test_unittest_pair_detail_words_keep_the_old_formula():
    text = ("Ran 6 tests in 0.10s\n\nFAILED (failures=1, errors=1, skipped=1,"
            " expected failures=1, unexpected successes=1)\n")
    assert R.counts(text) == ("unittest", {"passed": 1, "failed": 3, "skipped": 2})


def test_a_thousand_chained_pytest_summaries_still_sum():
    text = "no tests ran in 0.01s\n" + "".join("2 passed in 0.01s\n" for _ in range(1000))
    assert R.counts(text) == ("pytest", {"passed": 2000, "failed": 0, "skipped": 0})


def test_a_cargo_summary_is_counted_once_even_with_finished_in():
    # cargo's real line ends "finished in 0.02s" and holds "5 passed": the loose pytest
    # line test must not claim it, or one cargo suite counts twice
    line = ("test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out;"
            " finished in 0.02s\n")
    assert R.counts(line) == ("cargo", {"passed": 5, "failed": 0, "skipped": 1})
    assert R.counts(line + "5 passed in 0.30s\n") == ("cargo", {"passed": 10, "failed": 0,
                                                                "skipped": 1})


def test_a_chained_no_tests_ran_line_adds_zero_like_before():
    text = "no tests ran in 0.01s\n4 passed in 0.20s\n"
    assert R.counts(text) == ("pytest", {"passed": 4, "failed": 0, "skipped": 0})
    assert R.counts("no tests ran in 0.01s\n") == ("pytest", {"passed": 0, "failed": 0, "skipped": 0})


def test_a_runner_without_counts_adds_nothing_to_a_sum():
    # go test without -v is found but counted None: it joins the run as the timing parser
    # (it is found first) while the unittest pair carries the whole count
    text = "Ran 2 tests in 0.01s\n\nOK\nok  \texample.com/p\t0.1s\n"
    assert R.counts(text) == ("go", {"passed": 2, "failed": 0, "skipped": 0})


def test_hiding_tests_in_the_first_of_two_chained_suites_changes_the_count():
    # the B2 trap the old last-summary-only parse set: one suite alone vs two chained
    assert R.counts("1 passed in 0.10s\n")[1] == {"passed": 1, "failed": 0, "skipped": 0}
    assert R.counts("1 passed in 0.10s\n4 passed in 0.20s\n")[1] == {
        "passed": 5, "failed": 0, "skipped": 0}


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
    # the wrapped command embeds runners.py, so it is too long for `bash -c` on Windows:
    # run it from a script file, the way sandbox.run_cmd does
    script = tmp_path / "wrap.sh"
    script.write_bytes(R.wrap(cmd, "label one", bash=bash).encode("utf-8"))
    p = subprocess.run([bash, script.as_posix()], cwd=str(tmp_path),
                       capture_output=True, text=True)
    assert p.returncode == 0
    res = R.parse_result(p.stdout)
    assert res["passed"] == 3 and res["wall"] >= 0.2
    script.write_bytes(R.wrap("exit 3", "label two", bash=bash).encode("utf-8"))
    p = subprocess.run([bash, script.as_posix()], cwd=str(tmp_path),
                       capture_output=True, text=True)
    assert p.returncode == 3 and R.parse_result(p.stdout)["rc"] == 3
    script.unlink()
    assert list(tmp_path.iterdir()) == []          # the output file lives outside the tree


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_wrap_survives_a_non_utf8_locale(tmp_path):
    bash = shutil.which("bash")
    py = pathlib.Path(sys.executable).as_posix()
    cmd = 'PYTHONIOENCODING=utf-8 "%s" -c "print(\'\\u6d4b\\u8bd5 1 passed in 0.1s\')"' % py
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    script = tmp_path / "wrap.sh"                  # a script file, as in run_cmd (long -c)
    script.write_bytes(R.wrap(cmd, "enc", bash=bash).encode("utf-8"))
    p = subprocess.run([bash, script.as_posix()], cwd=str(tmp_path),
                       capture_output=True, env=env)
    assert p.returncode == 0
    assert R.parse_result(p.stdout.decode("utf-8"))["passed"] == 1


def test_wrap_differs_by_label():
    assert R.wrap("pytest", "a") != R.wrap("pytest", "b")
