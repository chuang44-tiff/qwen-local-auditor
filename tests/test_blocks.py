"""The generic block schema. VERDICT is optional; completeness is checked for
every builder, not just claims."""
from lib.blocks import parse, completeness, flag_prose_evidence, FAVOURABLE

TXT = """## t1
FINDING: the guard is absent
EVIDENCE: pkg/x.py:120
WHY: the named branch is unmoved at its named site

## t2.a
VERDICT: APPEARS_FIXED
FINDING: coercion is now handled
EVIDENCE: pkg/y.py:44
WHY: a module docstring describes the fix

## t2.b
VERDICT: STILL_PRESENT
FINDING: validation is still missing
EVIDENCE: pkg/y.py:99
WHY: no validation call on the path
"""


def test_parses_keys_clauses_and_optional_verdict():
    b = parse(TXT)
    assert [x["key"] for x in b] == ["t1", "t2.a", "t2.b"]
    assert b[0]["verdict"] is None and b[0]["clause"] is None
    assert b[1]["verdict"] == "APPEARS_FIXED" and b[1]["clause"] == "a"
    assert b[1]["item"] == 2


def test_parses_fields():
    b = parse(TXT)[0]
    assert b["finding"] == "the guard is absent"
    assert b["evidence"] == "pkg/x.py:120"
    assert "unmoved" in b["why"]


def test_parses_fenced_output():
    assert len(parse("```\n" + TXT + "```\n")) == 3


def test_completeness_reports_missing_keys():
    assert completeness(parse(TXT), ["t1", "t2.a", "t2.b", "t3"]) == ["t3"]


def test_completeness_clean_when_all_present():
    assert completeness(parse(TXT), ["t1", "t2.a", "t2.b"]) == []


def test_completeness_reports_unexpected_keys():
    missing, extra = completeness(parse(TXT), ["t1"], return_extra=True)
    assert extra == ["t2.a", "t2.b"]


def test_flags_favourable_verdict_citing_prose():
    # resolver says every cited line is prose
    flags = flag_prose_evidence(parse(TXT), lambda path, line: True)
    keys = " ".join(flags)
    assert "t2.a" in keys, "APPEARS_FIXED citing a prose line must be flagged"
    assert "t2.b" not in keys, "STILL_PRESENT is not favourable; do not flag it"
    assert "t1" not in keys, "no verdict at all means nothing to flag"


def test_flags_prose_cited_ALONGSIDE_code():
    """The recorded fail-open cited a docstring AND real code, stopping at the
    favourable half. An only-prose rule would have missed it."""
    txt = ("## t9\nVERDICT: APPEARS_FIXED\nFINDING: landed\n"
           "EVIDENCE: m.py:1, m.py:115\nWHY: both\n")
    flags = flag_prose_evidence(parse(txt), lambda p, n: n == 1)
    assert flags and "m.py:1" in flags[0]


def test_unresolvable_citation_fails_safe():
    flags = flag_prose_evidence(parse(TXT), lambda path, line: None)
    assert flags == [], "an unresolvable citation must not produce a false flag"


def test_favourable_set_is_explicit():
    assert "APPEARS_FIXED" in FAVOURABLE and "STILL_PRESENT" not in FAVOURABLE


def test_bare_filename_citation_is_not_resolvable():
    from lib.blocks import citations
    assert citations("ledger.py") == []          # no line number -> unusable
    assert citations("pkg/x.py:120, y.py:9") == [("pkg/x.py", 120), ("y.py", 9)]
