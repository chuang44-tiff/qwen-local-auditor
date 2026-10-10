"""bench.py: the optimize bench file and the evidence checks."""
import importlib.util
import json
import os
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "skill" / "local-auditor" / "lib" / "workflows" / "optimize" / "bench.py"


def _load():
    spec = importlib.util.spec_from_file_location("optimize_bench_t", str(SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load()


def o(ok=True, wall=10.0, passed=5, rc=0):
    return {"ok": ok, "wall": wall, "passed": passed, "rc": rc, "timed_out": False, "applied": True}


def test_from_data_defaults_and_errors():
    b, why = B.from_data({"measure": "pytest -q tests/a.py"})
    assert why is None
    assert b == {"measure": "pytest -q tests/a.py", "guard": "pytest -q tests/a.py", "focus": [],
                 "target": None, "keep": B.DEFAULT_KEEP}
    for bad, frag in [([], "JSON object"), ({}, "measure"), ({"measure": " "}, "measure"),
                      ({"measure": "x", "speed": 1}, "unknown bench field"),
                      ({"measure": "x", "focus": "a.py"}, "list"),
                      ({"measure": "x", "focus": ["../a"]}, "relative"),
                      ({"measure": "x", "focus": ["/abs"]}, "relative"),
                      ({"measure": "x", "target": "fast"}, "-30%"),
                      ({"measure": "x", "target": "-0%"}, "between"),
                      ({"measure": "x", "target": "-100%"}, "between"),
                      ({"measure": "x", "target": "0s"}, "above 0"),
                      ({"measure": "x", "guard": ""}, "guard"),
                      ({"measure": "x", "keep": ""}, "keep")]:
        b, why = B.from_data(bad)
        assert b is None and frag in why, (bad, why)


def test_load_reads_a_file(tmp_path):
    p = tmp_path / "bench.json"
    p.write_text(json.dumps({"measure": "x", "focus": ["./tests\\a.py"], "target": "-30%"}), encoding="utf-8")
    b, why = B.load(str(p))
    assert why is None and b["focus"] == ["tests/a.py"] and b["target"] == "-30%"
    p.write_text("{", encoding="utf-8")
    assert "not valid JSON" in B.load(str(p))[1]
    assert "cannot read" in B.load(str(tmp_path / "missing.json"))[1]


def test_targets_and_noise():
    assert B.parse_target("-30%") == ("pct", 0.3)
    assert B.parse_target(" 60s ") == ("sec", 60.0)
    assert B.target_seconds({"target": "-30%"}, 100.0) == pytest.approx(70.0)
    assert B.target_seconds({"target": "60s"}, 100.0) == 60.0
    assert B.target_seconds({"target": None}, 100.0) is None
    assert B.noise(10.0, 10.5, 10.0) == 0.5
    assert B.noise(10.0, 10.0, 10.0) == pytest.approx(0.2)    # 2% floor
    assert B.noise(1.0, 1.0, 1.0) == 0.05                     # absolute floor


def test_focus_matching():
    assert B.in_focus("anything.py", [])
    focus = ["tests/test_cli.py", "lib/", "src/*.py"]
    assert B.in_focus("./tests/test_cli.py", focus)
    assert B.in_focus("lib/a/b.py", focus)
    assert B.in_focus("src/x.py", focus)
    assert not B.in_focus("tests/test_other.py", focus)
    assert not B.in_focus(".github/x.yml", ["github"])
    assert B.outside_focus(["lib/a.py", "README.md"], focus) == ["README.md"]


def test_quote_found():
    texts = ["TIME PER FILE:\n    12.50  tests/test_cli.py\n"]
    assert B.quote_found("12.50 tests/test_cli.py", texts)          # whitespace squashed
    assert not B.quote_found("13.50 tests/test_cli.py", texts)
    assert not B.quote_found("12.50", texts)                         # too short to be evidence
    assert not B.quote_found(None, texts)


def test_location_ok(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("1\n2\n3\n", encoding="utf-8")
    assert B.location_ok(tmp_path, "pkg/a.py:2") == ("pkg/a.py", None)
    assert B.location_ok(tmp_path, "pkg/a.py:1-3") == ("pkg/a.py", None)
    assert B.location_ok(tmp_path, "pkg/a.py") == ("pkg/a.py", None)
    for bad in ["pkg/a.py:9", "pkg/a.py:0", "pkg/b.py:1", "../x.py:1", "/etc/passwd:1", ""]:
        path, why = B.location_ok(tmp_path, bad)
        assert path is None and why, bad


def test_location_ok_rejects_hostile_input(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("1\n2\n3\n", encoding="utf-8")
    cases = ["pkg/a\x00.py:1",                                # a NUL in the path
             "pkg/" + "x" * 5000 + ".py:1",                   # a segment longer than the os allows
             "pkg/a.py:" + "9" * 5000,                        # more digits than int() parses
             "pkg/a.py:" + "9" * 4000,                        # parses, but no file has that many lines
             "pkg/a\u2028b.py:1", "pkg/a\rb.py:1", "pkg/a\x85b.py:1"]  # line separators in the path
    try:
        os.symlink("loop", tmp_path / "loop")                 # a symlink that resolves to itself
    except OSError:                                           # Windows without the privilege
        pass
    else:
        cases.append("loop:1")
    for loc in cases:
        path, why = B.location_ok(tmp_path, loc)
        assert path is None and why, (loc[:30], path, why)
        assert len(why) <= 200 and len(why.splitlines()) == 1, (loc[:30], why)


def test_cited_lines():
    patch = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-DELAY = 1.0\n+DELAY = 0.1\n"
    assert B.cited_lines(["+DELAY = 0.1", "DELAY = 1.0", "+made up", "+", 7], patch) == \
        ["+DELAY = 0.1", "DELAY = 1.0"]


def test_checks_all_pass():
    b = dict(B.default("m"), guard="g", focus=["tests/a.py"], target="-30%")
    res = B.checks(b, o(), o(wall=10.4), o(passed=9), ["tests/a.py", "tests/b.py"])
    assert [r["ok"] for r in res] == [True, True, True, True]


def test_checks_each_failure():
    b = dict(B.default("m"), focus=["src/"], target="-1%")
    res = {r["id"]: r for r in B.checks(b, o(ok=False, rc=1), o(), o(passed=3), ["tests/a.py"])}
    assert res["B1"]["ok"] is False and "rc 1" in res["B1"]["detail"]
    assert res["B2"]["ok"] is False and "fewer tests" in res["B2"]["detail"]
    assert res["B3"]["ok"] is None and "tests/a.py" in res["B3"]["detail"]     # advisory
    assert res["B4"]["ok"] is None                                    # measure failed: skipped
    res = {r["id"]: r for r in B.checks(b, o(), o(wall=11.0), o(ok=False, rc=127), None)}
    assert res["B2"]["ok"] is False and "rc 127" in res["B2"]["detail"]
    assert res["B3"]["ok"] is None                                    # no per-file timing
    assert res["B4"]["ok"] is False and "noise" in res["B4"]["detail"]


def test_why():
    assert B.why(o(ok=False, rc=2)) == "rc 2"
    assert B.why(dict(o(ok=False), timed_out=True)) == "timed out"
    assert B.why(dict(o(ok=False), applied=False)) == "the patch did not apply"
