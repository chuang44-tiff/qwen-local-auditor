from lib import decisions

REPLY = """Changed the retry loop.

## DEVIATION
SPEC: §3.2 retry 3 times
DID: retry 5 times
WHY: upstream needs 4 attempts
EVIDENCE: TEST tests/test_net.py::test_flaky FAILED: timeout at attempt 4

```
## DEVIATION
SPEC: names stay
DID: renamed fetch to fetch_once
WHY: clashes with the new helper
EVIDENCE: src/net.py:12
```

## DEVIATION
WHY: no spec or did -- dropped
"""


def test_parse_blocks_tolerates_fences_and_drops_incomplete():
    got = decisions.parse(REPLY)
    assert [e["did"] for e in got] == ["retry 5 times", "renamed fetch to fetch_once"]
    assert got[0]["evidence"].startswith("TEST tests/test_net.py::test_flaky FAILED")


def test_append_numbers_continue_across_calls(tmp_path):
    p = str(tmp_path / "d.jsonl")
    decisions.append(p, decisions.parse(REPLY), session="s1", commit="abc")
    decisions.append(p, decisions.parse(REPLY)[:1], session="s1", commit="def")
    got = decisions.load(p)
    assert [e["n"] for e in got] == [1, 2, 3]
    assert got[2]["commit"] == "def"


def test_render():
    assert decisions.render([]) == "(none recorded)"
    out = decisions.render([{"n": 1, "spec": "a", "did": "b", "why": "c", "evidence": "d"}])
    assert out.startswith("D1")
    assert "SPEC: a" in out and "EVIDENCE: d" in out


def test_load_missing_file(tmp_path):
    assert decisions.load(str(tmp_path / "none.jsonl")) == []


def test_parse_empty_why_value():
    text = "## DEVIATION\nSPEC: a\nWHY:\nDID: b\n"
    got = decisions.parse(text)
    assert len(got) == 1
    assert got[0]["did"] == "b"
    assert got[0]["why"] == ""


def test_parse_case_insensitive_and_markdown():
    text = "## Deviation\n**SPEC:** a\n**DID:** b\n"
    got = decisions.parse(text)
    assert len(got) == 1
    assert got[0]["spec"] == "a"
    assert got[0]["did"] == "b"


def test_load_skips_corrupt_json_lines(tmp_path):
    p = str(tmp_path / "d.jsonl")
    # Write a valid line and a truncated line
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"n": 1, "spec": "a", "did": "b", "why": "", "evidence": "", "session": "s1", "commit": "abc", "time": "2026-10-02T00:00:00Z"}\n')
        fh.write('{"n": 2, "sp')
    got = decisions.load(p)
    assert len(got) == 1
    assert got[0]["n"] == 1
    # Append should continue numbering from max valid n
    appended = decisions.append(p, [{"spec": "c", "did": "d", "why": "", "evidence": ""}], session="s1", commit="def")
    assert len(appended) == 1
    assert appended[0]["n"] == 2


def test_append_after_partial_line_keeps_the_record(tmp_path):
    p = str(tmp_path / "d.jsonl")
    # A run killed mid-write leaves a line that parses to nothing and has no newline.
    # The newline must still go in: without it the first record joins that line and
    # the file loses both entries.
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('{"n": 1, "spec": "a", "did": "b"')
    assert decisions.load(p) == []
    appended = decisions.append(p, [{"spec": "c", "did": "d", "why": "", "evidence": ""}],
                                session="s1", commit="def")
    assert appended[0]["n"] == 1
    assert [e["did"] for e in decisions.load(p)] == ["d"]
    assert len(open(p, encoding="utf-8").read().splitlines()) == 2


def test_append_adds_newline_if_missing(tmp_path):
    p = str(tmp_path / "d.jsonl")
    # Write a record without trailing newline
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('{"n": 1, "spec": "a", "did": "b", "why": "", "evidence": "", "session": "s1", "commit": "abc", "time": "2026-10-02T00:00:00Z"}')
    got = decisions.load(p)
    assert len(got) == 1
    # Append another record
    decisions.append(p, [{"spec": "c", "did": "d", "why": "", "evidence": ""}], session="s1", commit="def")
    got = decisions.load(p)
    assert len(got) == 2
    assert got[0]["n"] == 1
    assert got[1]["n"] == 2
