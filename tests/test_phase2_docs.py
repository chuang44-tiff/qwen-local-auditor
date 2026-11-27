"""The built-in opt-ins and the user-facing messaging docs: the workflows whose units
gain from shared findings pass share=True, reference/messaging.md carries every
channel, and the two SKILL.md pages grew only within their budgets."""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "skill" / "local-auditor" / "lib" / "workflows"
MESSAGING = ROOT / "skill" / "local-auditor" / "reference" / "messaging.md"
# line counts of the two SKILL.md pages at the last release
BASE_LINES = {
    "skill/local-auditor/SKILL.md": 195,
    "skill/local-swarm/SKILL.md": 199,
}
ALL_WFS = ("debug", "optimize", "research", "ui-test")
OPTED_PHASES = ("probe", "implement", "fetch")


def _messaging_calls(wf_name):
    """(kind, first-argument, share-True?) for every fan_out/vote call of a workflow."""
    path = WORKFLOWS / wf_name / "workflow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("fan_out", "vote"):
            continue
        first = node.args[0] if node.args and isinstance(node.args[0], ast.Constant) else None
        share = any(kw.arg == "share" and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True for kw in node.keywords)
        out.append((node.func.attr, first.value if first else None, share))
    return out


def test_builtin_opt_ins():
    # the four fan-outs that opt in: debug's probe, optimize's implement, research's
    # fetch (both its call sites); everything else -- ui-test's fan-outs, the search
    # fan-outs, every vote -- stays unopted, byte-identical prompts and keys
    seen = []
    for wf_name in ALL_WFS:
        for kind, phase, share in _messaging_calls(wf_name):
            if kind == "fan_out":
                if phase in OPTED_PHASES:
                    assert share, "%s: fan_out(%r) must pass share=True" % (wf_name, phase)
                    seen.append(phase)
                else:
                    assert not share, "%s: fan_out(%r) must not opt in" % (wf_name, phase)
            else:
                assert not share, "%s: vote(%r) must never carry share" % (wf_name, phase)
    assert sorted(seen) == sorted(["probe", "implement", "fetch", "fetch"])


def test_messaging_doc_mentions():
    assert MESSAGING.is_file(), "skill/local-auditor/reference/messaging.md is missing"
    text = MESSAGING.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 120, "messaging.md is over its line budget"
    for needle in ("--ask", "--answer", "share", "ask_main", "bash not confined",
                   "TMPDIR", "QWEN_SHARE_MAX", "QWEN_ASK_MAIN_MAX", "QWEN_STREAM=0"):
        assert needle in text, needle


def test_skill_docs_within_limits():
    for rel, limit, needles in (
            ("skill/local-auditor/SKILL.md", 4, ("--ask", "--answer")),
            ("skill/local-swarm/SKILL.md", 6, ("share", "messaging.md"))):
        cur = (ROOT / rel).read_text(encoding="utf-8")
        grew = len(cur.splitlines()) - BASE_LINES[rel]
        assert grew <= limit, "%s grew by %d lines (limit %d)" % (rel, grew, limit)
        for needle in needles:
            assert needle in cur, "%s never mentions %s" % (rel, needle)
