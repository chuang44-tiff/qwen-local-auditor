"""The builder contract. The `withheld` reason is what carries the hardest-won
lesson -- empty context is refused at the tool boundary -- into every future builder."""
import pytest
from lib.builders.base import Built, load_builder

BUILDERS = ["claims", "diff", "files", "logs"]


@pytest.mark.parametrize("name", BUILDERS)
def test_builder_exposes_the_contract(name):
    b = load_builder(name)
    assert isinstance(b.DEFAULT_BRIEF, str) and b.DEFAULT_BRIEF
    assert callable(b.build)


def test_built_rejects_a_bare_boolean_withheld():
    with pytest.raises(TypeError):
        Built(body="b", context="c", size=1, withheld=True, clauses=None, label="l")


def test_built_accepts_none_or_a_reason():
    assert Built("b", "c", 1, None, None, "l").withheld is None
    assert Built("b", "c", 1, "no evidence found", None, "l").withheld


def test_load_builder_rejects_traversal():
    with pytest.raises(ValueError):
        load_builder("../../etc/passwd")


# ---------------------------------------------------------------- claims

def _doc(tmp_path, text, name="claim-x.md"):
    d = tmp_path / "docs"; d.mkdir(exist_ok=True)
    p = d / name; p.write_text(text)
    return {"doc": str(p), "repo": str(tmp_path), "census_root": str(tmp_path),
            "test_pat": "tests"}


def test_claims_withholds_when_no_file_yields_windows(tmp_path):
    item = _doc(tmp_path, "Some claim about `NoSuchSymbol` in `nowhere.py`.\n")
    out = load_builder("claims").build(item)
    assert out.withheld, "an item with no source windows MUST be withheld"
    assert "window" in out.withheld.lower()


def test_claims_dispatches_when_a_window_exists(tmp_path):
    (tmp_path / "real.py").write_text("def WidgetMaker():\n    return 1\n")
    item = _doc(tmp_path, "The `WidgetMaker` in `real.py` is broken.\n")
    out = load_builder("claims").build(item)
    assert out.withheld is None
    assert "real.py" in out.context and "def WidgetMaker" in out.context
    assert out.size > 0


def test_claims_enumerates_numbered_clauses(tmp_path):
    (tmp_path / "real.py").write_text("def WidgetMaker():\n    return 1\n")
    item = _doc(tmp_path, "Problems with `WidgetMaker` in `real.py`:\n\n"
                          "1. it returns the wrong type\n"
                          "2. it never validates input\n"
                          "3. it leaks a handle\n")
    out = load_builder("claims").build(item)
    assert out.clauses == ["1", "2", "3"]


def test_claims_single_claim_has_no_clauses(tmp_path):
    (tmp_path / "real.py").write_text("def WidgetMaker():\n    return 1\n")
    item = _doc(tmp_path, "The `WidgetMaker` in `real.py` is broken.\n")
    assert load_builder("claims").build(item).clauses is None


def test_claims_strips_status_lines(tmp_path):
    (tmp_path / "real.py").write_text("def WidgetMaker():\n    return 1\n")
    item = _doc(tmp_path, "- **Status:** OPEN\nThe `WidgetMaker` in `real.py` is broken.\n")
    out = load_builder("claims").build(item)
    assert "Status:" not in out.body, "the item's own disposition must not leak in"


# ---------------------------------------------------------------- diff

def test_diff_withholds_when_there_are_no_hunks(tmp_path):
    out = load_builder("diff").build(
        {"path": "x.py", "abspath": str(tmp_path / "x.py"), "hunks": [], "repo": str(tmp_path)})
    assert out.withheld and "hunk" in out.withheld.lower()


def test_diff_windows_the_changed_lines(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("\n".join("line%d" % i for i in range(1, 60)) + "\n")
    out = load_builder("diff").build(
        {"path": "x.py", "abspath": str(f), "hunks": [(30, 2)], "repo": str(tmp_path)})
    assert out.withheld is None
    assert "line30" in out.context and "x.py" in out.context
    assert "line1\n" not in out.context, "must be a window, not the whole file"


def test_diff_declares_no_clauses(tmp_path):
    f = tmp_path / "x.py"; f.write_text("a = 1\n")
    out = load_builder("diff").build(
        {"path": "x.py", "abspath": str(f), "hunks": [(1, 1)], "repo": str(tmp_path)})
    assert out.clauses is None, "a diff review has no claim to adjudicate"


# ---------------------------------------------------------------- files

def test_files_withholds_over_budget(tmp_path):
    big = tmp_path / "big.py"; big.write_text("x = 1\n" * 40000)
    out = load_builder("files").build({"path": str(big), "repo": str(tmp_path)})
    assert out.withheld and "budget" in out.withheld.lower()


def test_files_withholds_unreadable(tmp_path):
    out = load_builder("files").build({"path": str(tmp_path / "nope.py"), "repo": str(tmp_path)})
    assert out.withheld


def test_files_returns_whole_file_under_budget(tmp_path):
    f = tmp_path / "s.py"; f.write_text("def hello():\n    return 1\n")
    out = load_builder("files").build({"path": str(f), "repo": str(tmp_path)})
    assert out.withheld is None and "def hello" in out.context


# ---------------------------------------------------------------- logs

def test_logs_withholds_empty_chunk():
    out = load_builder("logs").build({"text": "   \n", "label": "chunk1"})
    assert out.withheld


def test_logs_passes_a_chunk_through():
    out = load_builder("logs").build({"text": "ERROR boom\nWARN x\n", "label": "chunk1"})
    assert out.withheld is None and "ERROR boom" in out.context


def test_claims_strip_fields_are_configurable(tmp_path):
    (tmp_path / "real.py").write_text("def WidgetMaker():\n    return 1\n")
    item = _doc(tmp_path, "- **Owner:** someone\nThe `WidgetMaker` in `real.py` is broken.\n")
    item["strip_fields"] = ["Owner"]
    assert "Owner:" not in load_builder("claims").build(item).body


def test_files_glob_is_relative_to_the_repo_not_the_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    items = load_builder("files").enumerate_items({"glob": "src/*.py", "repo": str(repo)})
    assert len(items) == 1
    assert load_builder("files").build(items[0]).label == "src/a.py"



# ---------------------------------------------------------------- generality

def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    *args], check=True, capture_output=True)


@pytest.fixture
def gitrepo(tmp_path):
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not available")
    r = tmp_path / "mono"
    (r / "svc" / "api").mkdir(parents=True)
    (r / "web").mkdir()
    (r / "svc" / "api" / "h.py").write_text("def handler():\n    return 1\n")
    (r / "web" / "app.ts").write_text("export const x = 1;\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "initial")
    (r / "svc" / "api" / "h.py").write_text("def handler():\n    return 2\n")
    (r / "web" / "app.ts").write_text("export const x = 2;\n")
    return r


def test_diff_scopes_to_a_subdirectory_repo(gitrepo):
    items = load_builder("diff").enumerate_items({"repo": str(gitrepo / "svc" / "api")})
    assert [i["path"] for i in items] == ["h.py"]
    out = load_builder("diff").build(items[0])
    assert out.withheld is None and "return 2" in out.context


def test_diff_ignores_user_colour_config(gitrepo):
    _git(gitrepo, "config", "color.ui", "always")
    _git(gitrepo, "config", "color.diff", "always")
    items = load_builder("diff").enumerate_items({"repo": str(gitrepo)})
    assert sorted(i["path"] for i in items) == ["svc/api/h.py", "web/app.ts"]
    assert all(load_builder("diff").build(i).withheld is None for i in items)


def test_diff_shows_removed_lines_and_commit_intent(gitrepo):
    _git(gitrepo, "commit", "-q", "-am", "Return two instead of one")
    items = load_builder("diff").enumerate_items({"repo": str(gitrepo), "base": "HEAD~1"})
    out = load_builder("diff").build([i for i in items if i["path"].endswith("h.py")][0])
    assert "-    return 1" in out.context and "+    return 2" in out.context
    assert "Return two instead of one" in out.body


def test_diff_a_bad_base_is_an_error_not_an_empty_diff(gitrepo):
    with pytest.raises(Exception, match="git"):
        load_builder("diff").enumerate_items({"repo": str(gitrepo), "base": "no-such-ref"})


def test_claims_finds_non_python_files_and_counts_their_symbols(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.ts").write_text(
        "/** Fixed: validateToken now rejects expired tokens. */\n"
        "export function validateToken(t) {\n  return check(t);\n}\n")
    (tmp_path / "src" / "use.ts").write_text("validateToken(a); validateToken(b);\n")
    item = _doc(tmp_path, "`validateToken` in `src/server.ts` still accepts expired tokens.\n")
    out = load_builder("claims").build(item)
    assert out.withheld is None
    assert "src/server.ts" in out.context
    assert "ABSENT" not in out.context, "a symbol used in .ts must not be reported absent"


def test_claims_qualified_symbols_and_duplicate_clause_numbers(tmp_path):
    (tmp_path / "real.py").write_text("class Cache:\n    def evict(self):\n        pass\n")
    item = _doc(tmp_path, "`Cache.evict()` in `real.py`:\n\nSteps:\n1. a\n2. b\n\n"
                          "Claims:\n1. leaks\n2. races\n")
    out = load_builder("claims").build(item)
    assert out.withheld is None and "def evict" in out.context
    assert out.clauses == ["1", "2"]


def test_logs_long_lines_are_chunked_not_dropped(tmp_path):
    from lib.builders import base
    base.set_item_budget(5000)
    try:
        log = tmp_path / "app.log"
        log.write_text("".join('{"n": %d, "msg": "%s"}\n' % (i, "x" * 400) for i in range(100)))
        items = load_builder("logs").enumerate_items({"input": str(log)})
        assert len(items) > 1
        text = "\n".join(load_builder("logs").build(i).context for i in items)
        assert all('"n": %d,' % i in text for i in range(100)), "every line must survive"
    finally:
        base.set_item_budget(base.MAX_ITEM_BYTES)
