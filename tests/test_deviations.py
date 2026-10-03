import subprocess

from lib import blocks
from lib.builders import deviations


def test_flag_unverified_deviation():
    parsed = blocks.parse(
        "## t1\nVERDICT: DEVIATION_EXPLAINED\nFINDING: f\nEVIDENCE: D1, src/n.py:4\nWHY: w\n"
        "## t2\nVERDICT: DEVIATION_EXPLAINED\nFINDING: f\nEVIDENCE: TEST tests/t.py::x PASSED\nWHY: w\n"
        "## t3\nVERDICT: DRIFT_UNEXPLAINED\nFINDING: f\nEVIDENCE: src/n.py:9\nWHY: w\n")
    flags = blocks.flag_unverified_deviation(parsed)
    assert len(flags) == 1 and flags[0].startswith("t1:")


def test_builder_context_carries_spec_log_and_diff(tmp_path, monkeypatch):
    repo = tmp_path / "r"; repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "n.py").write_text("tries = 3\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True, capture_output=True)
    (repo / "n.py").write_text("tries = 5\n")
    spec = tmp_path / "spec.md"; spec.write_text("Retry 3 times.\n")
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp_path / "none"))
    from lib import decisions, supervisor
    import pathlib
    rd = pathlib.Path(supervisor.repo_state_dir(str(repo))) / "run1"
    rd.mkdir(parents=True)
    decisions.append(str(rd / "decisions.jsonl"),
                     [{"spec": "retry 3", "did": "retry 5", "why": "w", "evidence": "TEST x FAILED"}],
                     session="s", commit="c")
    items = deviations.enumerate_items({"repo": str(repo), "spec": str(spec)})
    b = deviations.build(items[0])
    assert b.withheld is None
    assert "Retry 3 times." in b.context and "DID: retry 5" in b.context and "tries = 5" in b.context


def test_missing_transcript_folder_is_noted(tmp_path, monkeypatch):
    repo = tmp_path / "r"; repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 2\n")
    spec = tmp_path / "spec.md"; spec.write_text("s\n")
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp_path / "gone"))

    def built():
        return deviations.build(deviations.enumerate_items({"repo": str(repo), "spec": str(spec)})[0])

    # An absent folder must not read as an empty one: the auditor needs to know
    # the transcript evidence was unavailable, not that none existed.
    b = built()
    assert "(no Claude transcript folder for this repo)" in b.context
    # The folder exists but holds nothing about the file: "(none)" is accurate.
    (tmp_path / "gone").mkdir()
    b = built()
    assert "(none)" in b.context and "(no Claude transcript folder" not in b.context


def test_context_names_the_run_and_renumbers(tmp_path, monkeypatch):
    import pathlib
    from lib import decisions, supervisor
    repo = tmp_path / "r"; repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 2\n")
    spec = tmp_path / "spec.md"; spec.write_text("s\n")
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp_path / "none"))
    for run in ("runA", "runB"):
        rd = pathlib.Path(supervisor.repo_state_dir(str(repo))) / run
        rd.mkdir(parents=True)
        decisions.append(str(rd / "decisions.jsonl"),
                         [{"spec": "s", "did": "d-" + run, "why": "w", "evidence": "e"}],
                         session="s", commit="c")
    b = deviations.build(deviations.enumerate_items({"repo": str(repo), "spec": str(spec)})[0])
    assert "D1\nRUN: runA\nSPEC: s\nDID: d-runA" in b.context
    assert "D2\nRUN: runB\nSPEC: s\nDID: d-runB" in b.context


def test_clipped_spec_is_marked(tmp_path, monkeypatch):
    repo = tmp_path / "r"; repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True, capture_output=True)
    (repo / "n.py").write_text("a = 2\n")
    monkeypatch.setenv("QWEN_AGENT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QWEN_TRANSCRIPT_DIR", str(tmp_path / "none"))
    # A spec past MAX_SPEC is cut, and the cut must be visible: a clipped spec that
    # reads as the whole one has the auditor rule on clauses it was never shown.
    spec = tmp_path / "spec.md"
    spec.write_text("Retry 3 times.\n" + "x" * deviations.MAX_SPEC + "\nPAST THE CUT: keep the name\n")
    item = deviations.enumerate_items({"repo": str(repo), "spec": str(spec)})[0]
    mark = "[spec truncated at %d characters]" % deviations.MAX_SPEC
    assert item["spec"].endswith("\n" + mark)
    assert "PAST THE CUT: keep the name" not in item["spec"]
    assert mark in deviations.build(item).context
    # A spec that fits carries no marker.
    fits = tmp_path / "fits.md"
    fits.write_text("y" * deviations.MAX_SPEC)
    whole = deviations.enumerate_items({"repo": str(repo), "spec": str(fits)})[0]
    assert "[spec truncated" not in whole["spec"]
