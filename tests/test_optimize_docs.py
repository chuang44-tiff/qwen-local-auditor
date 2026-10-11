"""The optimize docs name what the workflow actually reads and writes."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_swarm_reference_documents_optimize():
    s = read("skill/local-auditor/reference/swarm.md")
    for frag in ["## The optimize workflow", "--set bench=", '"measure"', '"guard"', '"focus"', '"target"',
                 '"keep"', "bench-review.md", "bench-suggested.json", "patches/stacked.diff",
                 "bench: problem (see bench-review.md)", "exit 4"]:
        assert frag in s, frag


def test_skill_has_the_pre_handover_checklist():
    s = read("skill/local-swarm/SKILL.md")
    for frag in ["optimize", "bench", "run measure twice", "guard", "bench-review.md", "noise"]:
        assert frag in s, frag


def test_readme_lists_optimize():
    assert "`optimize`" in read("README.md")


def test_docs_name_the_sandbox_the_bench_commands_run_in():
    # measure and guard run on a clone of HEAD: a bench whose dependencies live only in
    # the working tree (uncommitted files, node_modules, .venv) fails at round 1's first run
    for doc in ("skill/local-auditor/reference/swarm.md", "skill/local-swarm/SKILL.md"):
        t = read(doc).lower()
        for frag in ("clone of", "head", "uncommitted", "git-ignored", "node_modules", ".venv"):
            assert frag in t, "%s: %s" % (doc, frag)
        assert "HEAD" in read(doc), doc          # the exact ref the sandbox clones


def test_swarm_reference_documents_the_counting_and_the_checks():
    s = read("skill/local-auditor/reference/swarm.md")
    assert "every test summary" in s and "pytest tests/a && pytest tests/b" in s
    assert "advisory" in s and "never stops the run" in s
    assert "fnmatch" in s and "also crosses" in s
    assert "one voter" in s
    assert "no new idea that passes its checks" in s


def test_skill_step_three_runs_the_bench_the_way_the_run_will():
    t = read("skill/local-swarm/SKILL.md")
    step3 = [ln for ln in t.splitlines() if "run measure twice and guard once" in ln][0]
    assert "the way the run will" in step3 and "clean clone of HEAD" in step3
