import json
import threading

import pytest

from lib.swarm_engine import events


def lines(run):
    return (run / "events.jsonl").read_bytes().decode("utf-8").splitlines(keepends=True)


def test_emit_writes_kind_t_and_fields_as_one_line(tmp_path):
    events.emit(tmp_path, "unit_done", unit="a-1", role="worker", ok=True)
    (line,) = lines(tmp_path)
    assert line.endswith("\n")
    rec = json.loads(line)
    assert rec["kind"] == "unit_done" and isinstance(rec["t"], float)
    assert {k: rec[k] for k in ("unit", "role", "ok")} == {"unit": "a-1", "role": "worker", "ok": True}
    with pytest.raises(ValueError):
        events.emit(tmp_path, "x", t=1)


def test_concurrent_emits_from_8_threads_stay_whole_lines(tmp_path):
    def burst(n):
        for i in range(200):
            events.emit(tmp_path, "tick", thread=n, i=i, pad="x" * (50 * (i % 40)))

    threads = [threading.Thread(target=burst, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = lines(tmp_path)
    assert len(got) == 1600
    recs = [json.loads(x) for x in got]                       # every line parses
    assert sorted((r["thread"], r["i"]) for r in recs) == [(n, i) for n in range(8) for i in range(200)]


def test_a_long_field_is_cut_so_the_line_stays_under_4_kb(tmp_path):
    events.emit(tmp_path, "attention", item="S1", detail="é" * 10000,
                nested={"why": ["y" * 9000]})
    (line,) = lines(tmp_path)
    assert len(line.encode("utf-8")) < events.MAX_LINE
    rec = json.loads(line)
    assert rec["item"] == "S1" and rec["detail"].startswith("éééé") and rec["detail"].endswith("[cut]")
    assert rec["nested"]["why"][0].endswith("[cut]")
    events.emit(tmp_path, "attention", **{"f%d" % i: i for i in range(2000)})
    rec = json.loads(lines(tmp_path)[-1])
    assert rec == {"kind": "attention", "t": rec["t"], "truncated": True}


def test_a_surrogate_is_replaced_not_raised(tmp_path):
    events.emit(tmp_path, "run_start", goal="caf\udce9")
    assert json.loads(lines(tmp_path)[0])["goal"] == "caf?"


def test_an_unwritable_run_folder_never_fails_the_caller(tmp_path):
    events.emit(tmp_path / "missing" / "deeper", "unit_done", unit="a")   # no folder: no raise
