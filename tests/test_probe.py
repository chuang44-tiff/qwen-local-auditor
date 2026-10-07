"""lib/probe.py: the sandbox a qwen-agent --probe session runs in."""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from lib import probe
from swarm_fixtures import git_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
PROBE = pathlib.Path(__file__).resolve().parents[1] / "skill" / "local-auditor" / "lib" / "probe.py"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "probes"
    monkeypatch.setenv("QWEN_PROBE_DIR", str(r))
    return r


def status(repo):
    return subprocess.run(["git", "status", "--porcelain"], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout


def cli(*args, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(PROBE), *args], capture_output=True,
                          encoding="utf-8", errors="replace", env=e)


def cli_bytes(*args, env=None):
    """Like cli() but stdout/stderr stay bytes: a name whose bytes are not UTF-8 must
    survive the round trip instead of being decoded (and silently replaced) here."""
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(PROBE), *args], capture_output=True, env=e)


def test_create_sandboxes_the_whole_work_tree_and_enters_the_cwd(tmp_path, root):
    repo = git_repo(tmp_path / "my repo", {"src/a.py": "x = 1\n", "README": "r\n"})
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")           # dirty
    (repo / "src" / "new.py").write_text("y = 1\n", encoding="utf-8")         # untracked
    before = status(repo)
    run, sb, here, src = probe.create(repo / "src")
    assert probe._inside(run, root) and sb == run / "sandboxes" / "tree"
    assert here == sb / "src" and src.samefile(repo)
    assert (sb / "README").exists()                                   # the whole work tree
    assert (here / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (here / "new.py").exists()
    assert status(repo) == before
    probe.remove(run)
    assert not run.exists()


def test_source_names_what_is_sandboxed(tmp_path, root):
    outer = git_repo(tmp_path / "outer", {"inner/t.txt": "t\n", "o.txt": "o\n"})
    run, sb, here, _ = probe.create(outer / "inner", source=outer / "inner")
    assert (sb / "t.txt").exists() and not (sb / "o.txt").exists() and here == sb
    probe.remove(run)
    with pytest.raises(probe.Refused):
        probe.create(outer, source=outer / "inner")                  # -C outside the source


def test_a_plain_folder_is_copied(tmp_path, root):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("f\n", encoding="utf-8")
    run, sb, here, src = probe.create(plain)
    assert here == sb and src.samefile(plain) and (sb / "f.txt").read_text(encoding="utf-8") == "f\n"
    probe.remove(run)


def test_a_probe_dir_inside_the_source_is_refused(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {"a": "a\n"})
    monkeypatch.setenv("QWEN_PROBE_DIR", str(repo / ".cache"))
    with pytest.raises(probe.Refused, match="QWEN_PROBE_DIR"):
        probe.create(repo)
    assert not (repo / ".cache").exists()


def test_patch_is_exact_bytes_and_empty_when_nothing_changed(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    run, sb, here, _ = probe.create(repo)
    out = tmp_path / "p.patch"
    assert probe.write_patch(sb, out) == 0 and out.read_bytes() == b""
    (here / "a.txt").write_bytes(b"A\r\n\xe9\n")
    assert probe.write_patch(sb, out) > 0
    chk = subprocess.run(["git", "apply", "--check", str(out)], cwd=str(repo), capture_output=True)
    assert chk.returncode == 0, chk.stderr
    probe.remove(run)


def test_remove_leaves_a_foreign_folder_alone(tmp_path, root):
    run = root / "not-ours"
    run.mkdir(parents=True)
    (run / "keep.txt").write_text("k", encoding="utf-8")
    probe.remove(run)
    assert (run / "keep.txt").exists()


def test_cli_create_diff_remove(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    r = cli("create", "--cwd", str(repo), env={"QWEN_PROBE_DIR": str(root)})
    assert r.returncode == 0, r.stderr
    run, sb, here, src = r.stdout.splitlines()
    assert pathlib.Path(src).samefile(repo)
    pathlib.Path(here, "b.txt").write_text("b\n", encoding="utf-8")
    out = tmp_path / "x.patch"
    assert cli("diff", sb, str(out)).returncode == 0
    assert b"+b" in out.read_bytes()
    assert cli("remove", run).returncode == 0 and not pathlib.Path(run).exists()


def test_cli_exit_codes(tmp_path, root):
    assert cli().returncode == 2
    r = cli("create", "--cwd", str(tmp_path / "missing"), env={"QWEN_PROBE_DIR": str(root)})
    assert r.returncode == 2 and "not a directory" in r.stderr
    r = cli("diff", str(tmp_path / "no-sandbox"), str(tmp_path / "o.patch"))
    assert r.returncode == 2 and "not a qwen-probe sandbox" in r.stderr      # refused, not run


def test_check_accepts_only_real_sandboxes(tmp_path, root):
    """--probe-here asks check before it grants a full shell: the marker files alone are
    text anyone can copy, so only a layout create() actually made passes."""
    e = {"QWEN_PROBE_DIR": str(root)}
    repo = git_repo(tmp_path / "repo", {"src/a.py": "a\n"})

    run, sb, here, _ = probe.create(repo / "src")           # a real sandbox, from any dir in it
    for d in (here, sb):
        r = cli("check", str(d), env=e)
        assert r.returncode == 0, r.stderr
        assert pathlib.Path(r.stdout.strip()).samefile(sb)  # it prints the sandbox it checked
    probe.remove(run)

    plain = git_repo(tmp_path / "plain", {"a.txt": "a\n"})  # a plain repo with a `.base`
    head = subprocess.run(["git", "-C", str(plain), "rev-parse", "HEAD"], capture_output=True,
                          encoding="utf-8", check=True).stdout.strip()          # a REAL sha
    (tmp_path / "plain.base").write_text(head + "\n", encoding="utf-8")          # beside it
    r = cli("check", str(plain), env=e)                     # is still not a sandbox
    assert r.returncode == 2 and r.stderr.strip()

    run2, sb2, here2, _ = probe.create(repo)                # a real sandbox whose .base
    (sb2.parent / (sb2.name + ".base")).write_text("0" * 40 + "\n", encoding="utf-8")
    r = cli("check", str(here2), env=e)                     # names no commit in it
    assert r.returncode == 2 and r.stderr.strip()
    probe.remove(run2)


def test_check_refuses_dir_outside_the_sandbox(tmp_path, root, monkeypatch):
    """An inherited GIT_WORK_TREE makes git answer a different work tree for any -C:
    pointed at a create() sandbox, `check(<plain repo>)` reads that sandbox as the plain
    repo's top level -- markers, sandboxes/ parent and .base commit and all -- and would
    hand a full shell to the plain repo. check strips the GIT_* steering from every git
    it runs, and demands that DIR itself be the sandbox top or inside it, compared
    component-wise on realpaths: a sibling sb2, whose name merely starts with the
    sandbox's (sb), is not inside sb however well the raw strings line up."""
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    run, sb, _, _ = probe.create(repo)
    sibling = sb.parent / (sb.name + "2")
    git_repo(sibling, {"a.txt": "a\n"})                   # a plain repo, the sibling sb2
    try:
        monkeypatch.setenv("GIT_WORK_TREE", str(sb))      # git answers sb for any -C
        for outside in (repo, sibling):
            with pytest.raises(probe.Refused):
                probe.check(outside)
        monkeypatch.delenv("GIT_WORK_TREE")
    finally:
        shutil.rmtree(str(sibling), ignore_errors=True)
        probe.remove(run)


def test_check_refuses_bogus_base_under_a_sandboxes_parent(tmp_path, root):
    """Every marker create() leaves, faithfully faked -- the run marker, a `sandboxes/`
    parent, a `.base` of 40 hex chars -- and the one fact markers cannot fake is still
    checked: that sha names a commit IN the repository. A plain git repo dressed up as a
    sandbox is refused, not fenced in."""
    x = tmp_path / "X"
    name = git_repo(x / "sandboxes" / "name", {"a.txt": "a\n"})
    (x / ".qwen-probe-run").write_text("qwen-probe\n", encoding="utf-8")
    (x / "sandboxes" / "name.base").write_text("0" * 40 + "\n", encoding="utf-8")
    r = cli("check", str(name), env={"QWEN_PROBE_DIR": str(root)})
    assert r.returncode == 2 and "not a commit in it" in r.stderr


def test_remove_refuses_foreign_dirs(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {"sandboxes/x.txt": "x\n", "keep.txt": "k\n"})
    stranger = tmp_path / "random"
    stranger.mkdir()
    for run in (repo / "sandboxes", "", stranger):        # a tracked sandboxes/, an empty
        r = cli("remove", str(run), env={"QWEN_PROBE_DIR": str(root)})     # argument (a
        assert r.returncode == 2                            # usage error), a random dir
        # The refusal names what it refused to do, not just the argument: an empty
        # argument makes `str(run) in r.stderr` true for any message at all.
        assert "is not a probe run folder made by create" in r.stderr
    assert (repo / "sandboxes" / "x.txt").exists() and (repo / "keep.txt").exists()
    assert stranger.is_dir() and status(repo) == ""        # nothing was removed


def test_diff_refuses_non_sandbox(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    index = repo / ".git" / "index"
    before = index.read_bytes()
    out = tmp_path / "o.patch"
    r = cli("diff", str(repo), str(out), env={"QWEN_PROBE_DIR": str(root)})
    assert r.returncode == 2 and "not a qwen-probe sandbox" in r.stderr
    assert index.read_bytes() == before and not out.exists()   # no git command ran in the repo
    (tmp_path / "repo.base").write_text("0" * 40 + "\n", encoding="utf-8")   # the .base alone
    r = cli("diff", str(repo), str(out), env={"QWEN_PROBE_DIR": str(root)})  # is not enough:
    assert r.returncode == 2 and index.read_bytes() == before               # no run marker
    assert not out.exists()


def test_create_and_diff_never_write_the_target_index(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    (repo / "a.txt").write_text("b\n", encoding="utf-8")          # dirty: the index is stale
    index = repo / ".git" / "index"                              # by mtime, so a status
    before = index.read_bytes()                                   # refresh would rewrite it
    run, sb, here, _ = probe.create(repo)
    (here / "a.txt").write_text("c\n", encoding="utf-8")
    (here / "new.txt").write_text("n\n", encoding="utf-8")
    out = tmp_path / "p.patch"
    assert probe.write_patch(sb, out) > 0 and b"+c" in out.read_bytes()
    probe.remove(run)
    assert not run.exists()
    assert index.read_bytes() == before


NAMES = ["café-ü"]
if os.name == "posix":
    NAMES.append(os.fsdecode(b"caf\xe9") + "-raw")     # byte 0xE9 alone: not UTF-8 decodable


@pytest.mark.parametrize("name", NAMES)
def test_non_ascii_paths(tmp_path, root, name):
    raw = os.fsencode(name)
    repo = git_repo(tmp_path / name, {name + "/a.txt": "a\n"})
    e = {"QWEN_PROBE_DIR": str(root)}
    r = cli_bytes("create", "--cwd", str(repo / name), env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr, r.stderr
    run, sb, here, src = r.stdout.splitlines()
    if b"\xe9" in raw:                       # the raw-bytes name must come back byte-exact,
        assert raw in r.stdout               # not replaced by "?"s
    assert pathlib.Path(os.fsdecode(src)).samefile(repo)
    here_p = pathlib.Path(os.fsdecode(here))
    assert here_p.is_dir() and (here_p / "a.txt").read_text(encoding="utf-8") == "a\n"
    (here_p / "b.txt").write_text("b\n", encoding="utf-8")
    out = tmp_path / "p.patch"
    r = cli_bytes("diff", os.fsdecode(sb), str(out), env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr and b"+b" in out.read_bytes()
    r = cli_bytes("remove", os.fsdecode(run), env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr
    assert not pathlib.Path(os.fsdecode(run)).exists()


def test_session_dir_outside_the_copy_is_refused(tmp_path, root):
    repo = git_repo(tmp_path / "repo", {".gitignore": "build/\n", "a.txt": "a\n"})
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_bytes(b"o")
    for outside in (repo / "build", repo / ".git"):      # ignored, or never copied at all
        r = cli("create", "--cwd", str(outside), env={"QWEN_PROBE_DIR": str(root)})
        assert r.returncode == 2 and "not part of the copy" in r.stderr
        assert not root.exists() or not list(root.iterdir())   # no half-made run folder


def test_help_exits_0():
    r = cli("--help")
    assert r.returncode == 0 and "usage" in r.stdout.lower()


def test_create_prints_utf8_whatever_the_console_codec(tmp_path, root):
    """The console codec is not the contract: qwen-agent.sh reads stdout in bash, so
    every line leaves as UTF-8 bytes. A cp1252 text layer (a piped Windows stdout)
    would raise UnicodeEncodeError on the final print of a CJK run -- exit 1 with the
    run folder leaked -- and surrogateescape there only covers non-UTF-8 bytes."""
    repo = git_repo(tmp_path / "仓库", {"src/a.py": "a\n"})
    e = {"QWEN_PROBE_DIR": str(root), "PYTHONIOENCODING": "cp1252"}
    r = cli_bytes("create", "--cwd", str(repo / "src"), env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr, r.stderr
    run, sb, here, src = r.stdout.decode("utf-8").splitlines()   # exact UTF-8: no "?"s
    assert "仓库" in src and pathlib.Path(src).samefile(repo) and pathlib.Path(here).is_dir()
    here_p = pathlib.Path(here)
    (here_p / "b.txt").write_text("b\n", encoding="utf-8")
    out = tmp_path / "p.patch"
    r = cli_bytes("diff", sb, str(out), env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr and b"+b" in out.read_bytes()
    r = cli_bytes("remove", run, env=e)
    assert r.returncode == 0 and b"Traceback" not in r.stderr
    assert not pathlib.Path(run).exists()


def snap(d):
    """Everything under d, names only -- what a link target must keep untouched."""
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


def test_symlinked_sandbox_is_refused(tmp_path, root):
    link = tmp_path / "link-probe"
    try:
        os.symlink(str(tmp_path), str(link))
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("os.symlink is unavailable here")
    link.unlink()
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    e = {"QWEN_PROBE_DIR": str(root)}

    # the sandbox itself replaced by a link to elsewhere: diff must not run git through
    run, sb, _, _ = probe.create(repo)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("k\n", encoding="utf-8")
    before = snap(outside)
    shutil.rmtree(sb)
    os.symlink(str(outside), str(sb))                            # .base stays beside it
    out = tmp_path / "p.patch"
    r = cli("diff", str(sb), str(out), env=e)
    assert r.returncode == 2 and "not a qwen-probe sandbox" in r.stderr
    assert not out.exists() and snap(outside) == before and not (outside / ".git").exists()
    r = cli("remove", str(run), env=e)     # refused outright, or the link alone goes --
    assert r.returncode == 2 or not sb.is_symlink()              # never the target
    assert snap(outside) == before and (outside / "keep.txt").is_file()

    # ... and the same with the sandboxes/ parent being the link: every other fact
    # (.base, the run marker, a tree two levels down) reads true through the link
    run2, sb2, _, _ = probe.create(repo)
    outside2 = tmp_path / "elsewhere2"
    (outside2 / "tree").mkdir(parents=True)
    (outside2 / "tree" / "keep.txt").write_text("k\n", encoding="utf-8")
    (outside2 / "tree.base").write_text("0" * 40 + "\n", encoding="utf-8")
    shutil.rmtree(run2 / "sandboxes")
    os.symlink(str(outside2), str(run2 / "sandboxes"))
    before2 = snap(outside2)
    out2 = tmp_path / "q.patch"
    r = cli("diff", str(sb2), str(out2), env=e)
    assert r.returncode == 2 and "not a qwen-probe sandbox" in r.stderr and not out2.exists()
    r = cli("remove", str(run2), env=e)
    assert r.returncode == 2 or not run2.exists()                # link only, or refused
    assert snap(outside2) == before2 and not (outside2 / "tree" / ".git").exists()


def test_remove_reports_a_sandbox_it_could_not_delete(tmp_path, root, monkeypatch):
    from lib.swarm_engine import sandbox
    repo = git_repo(tmp_path / "repo", {"a.txt": "a\n"})
    run, sb, _, _ = probe.create(repo)
    monkeypatch.setattr(sandbox, "_rmtree", lambda path: None)     # deletion fails silently
    assert probe.remove(run) is False
    assert sb.exists()
    monkeypatch.undo()                                  # the marker is still there:
    assert probe.remove(run) is True and not sb.exists()
