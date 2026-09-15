"""The engine owns writing, batching and withholding, so no builder can break them."""
import json
import pytest
from lib.builders.base import Built
from lib.engine import (plan_batches, write_batch, render_brief, collate, batch_root,
                        batch_status, main)


def mk(size, withheld=None, clauses=None, label="l"):
    return Built("body", "ctx", size, withheld, clauses, label)


def test_batches_respect_the_byte_budget():
    assert plan_batches([mk(60), mk(60), mk(10)], budget=100) == [[0], [1, 2]]


def test_an_oversized_item_still_gets_its_own_batch():
    assert plan_batches([mk(500), mk(10)], budget=100) == [[0], [1]]


def test_batching_is_not_a_fixed_count():
    got = plan_batches([mk(10)] * 20, budget=100)
    assert all(sum(1 for _ in b) <= 10 for b in got) and len(got) >= 2


def test_withheld_items_are_never_written_and_numbering_stays_contiguous(tmp_path):
    builts = [mk(10, label="a"), mk(10, "no evidence found", label="b"), mk(10, label="c")]
    keys = write_batch(tmp_path, builts)
    assert keys == ["t1", "t2"]
    assert (tmp_path / "t1.md").exists() and (tmp_path / "t2.md").exists()
    assert not (tmp_path / "t3.md").exists()
    assert "no evidence found" in (tmp_path / "skipped.txt").read_text()
    assert "b" in (tmp_path / "skipped.txt").read_text()


def test_clauses_expand_the_expected_key_set(tmp_path):
    keys = write_batch(tmp_path, [mk(10, clauses=["1", "2", "3"])])
    assert keys == ["t1.1", "t1.2", "t1.3"]


def test_render_brief_substitutes_declared_vars():
    assert render_brief("Emit {{N}} blocks.", {"N": 3}) == "Emit 3 blocks."


def test_render_brief_fails_loudly_on_an_unfilled_var():
    with pytest.raises(KeyError):
        render_brief("Emit {{N}} for {{ITEMS}}.", {"N": 3})


def test_batch_root_is_never_inside_the_target_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("QWEN_SWEEP_CACHE", raising=False)
    root = batch_root(str(tmp_path))
    assert str(tmp_path) not in root
    assert ".cache" in root


def test_collate_stamps_a_schema_version(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "key.txt").write_text("t1|alpha\n")
    (d / "out.md").write_text("## t1\nFINDING: f\nEVIDENCE: x.py:1\nWHY: w\n" + "x" * 1200)
    (d / "skipped.txt").write_text("")
    rows, problems, skipped = collate(str(tmp_path))
    assert problems == [] and len(rows) == 1
    doc = json.loads((tmp_path / "collated.json").read_text())
    assert doc["schema"] == 1 and len(doc["rows"]) == 1


def test_collate_reports_a_missing_block(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "key.txt").write_text("t1|alpha\nt2|beta\n")
    (d / "out.md").write_text("## t1\nFINDING: f\nEVIDENCE: x.py:1\nWHY: w\n" + "x" * 1200)
    rows, problems, skipped = collate(str(tmp_path))
    assert any("t2" in p for p in problems)


def test_collate_reports_the_autocompact_signature(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "key.txt").write_text("t1|alpha\n")
    (d / "out.md").write_text("I could not complete this.\n")
    rows, problems, skipped = collate(str(tmp_path))
    assert any("autocompact" in p or "too short" in p for p in problems)


def test_a_short_complete_answer_is_not_the_autocompact_signature(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "key.txt").write_text("t1|alpha\n")
    (d / "expected.txt").write_text("t1\n")
    (d / "out.md").write_text("## t1\nFINDING: nothing notable\nEVIDENCE: a.py:1\nWHY: w\n")
    assert batch_status(str(d))[0] == "ok"
    rows, problems, skipped = collate(str(tmp_path))
    assert problems == [] and len(rows) == 1


def test_batch_status_tells_incomplete_from_suspect(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "expected.txt").write_text("t1\nt2\n")
    (d / "out.md").write_text("## t1\nFINDING: f\nEVIDENCE: a.py:1\nWHY: w\n")
    assert batch_status(str(d))[0] == "incomplete"
    (d / "out.md").write_text("I ran out of room.\n")
    assert batch_status(str(d))[0] == "suspect"
    (d / "out.md").unlink()
    assert batch_status(str(d))[0] == "missing"


def test_withheld_items_reach_the_collated_result(tmp_path):
    d = tmp_path / "b01"; d.mkdir()
    (d / "key.txt").write_text("t1|alpha\n")
    (d / "out.md").write_text("## t1\nFINDING: f\nEVIDENCE: a.py:1\nWHY: w\n")
    (tmp_path / "needs-human.txt").write_text("empty.py|file is empty\n")
    rows, problems, skipped = collate(str(tmp_path))
    assert ["empty.py", "file is empty"] in skipped
    doc = json.loads((tmp_path / "collated.json").read_text())
    assert doc["withheld"] == [["empty.py", "file is empty"]]


def test_a_build_that_finds_nothing_fails(tmp_path):
    rc = main(["build", "--builder", "files", "--args",
               json.dumps({"repo": str(tmp_path), "glob": "nothing/*.py"}), "--out", str(tmp_path / "run")])
    assert rc == 9
    rc = main(["build", "--builder", "files", "--allow-empty", "--args",
               json.dumps({"repo": str(tmp_path), "glob": "nothing/*.py"}), "--out", str(tmp_path / "run2")])
    assert rc == 0


def test_a_build_where_everything_is_withheld_fails(tmp_path):
    (tmp_path / "empty.py").write_text("")
    rc = main(["build", "--builder", "files", "--args",
               json.dumps({"repo": str(tmp_path), "glob": "*.py"}), "--out", str(tmp_path / "run")])
    assert rc == 9
    assert "empty.py" in (tmp_path / "run" / "needs-human.txt").read_text()


def test_missing_builder_args_are_a_usage_error(tmp_path, capsys):
    rc = main(["build", "--builder", "claims", "--args", json.dumps({"repo": str(tmp_path)}),
               "--out", str(tmp_path / "run")])
    assert rc == 2
    assert "--docs" in capsys.readouterr().err


def test_an_unknown_builder_is_a_usage_error(tmp_path, capsys):
    rc = main(["build", "--builder", "nosuch", "--out", str(tmp_path / "run")])
    assert rc == 2
    assert "known:" in capsys.readouterr().err


def test_the_cache_root_honours_xdg_and_an_override(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_SWEEP_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert batch_root("r").startswith(str(tmp_path / "xdg"))
    monkeypatch.setenv("QWEN_SWEEP_CACHE", str(tmp_path / "mine"))
    assert batch_root("r").startswith(str(tmp_path / "mine"))
