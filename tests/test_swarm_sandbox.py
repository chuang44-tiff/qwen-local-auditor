import os
import shutil
import subprocess
import time

import pytest

from lib.swarm_engine import sandbox
from swarm_fixtures import git_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(autouse=True)
def bash(monkeypatch):
    b = os.environ.get("TEST_BASH") or shutil.which("bash")
    if b:
        monkeypatch.setenv("QWEN_SWARM_BASH", b)


def worktrees(repo):
    out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(repo),
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.startswith("worktree ")]


def status(repo):
    return subprocess.run(["git", "status", "--porcelain"], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout


@pytest.mark.parametrize("kind", ["git", "copy"])
def test_create_diff_remove(tmp_path, kind):
    files = {"a.txt": "one\n", "node_modules/x.js": "junk\n", "sub/b.txt": "two\n"}
    if kind == "git":
        target = git_repo(tmp_path / "target", {"a.txt": "one\n", "sub/b.txt": "two\n"})
    else:
        target = tmp_path / "target"
        for rel, text in files.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(text, encoding="utf-8", newline="\n")
    sb = tmp_path / "run" / "sandboxes" / "probe-1"
    sandbox.create(target, sb)
    assert (sb / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert not (sb / "node_modules").exists()               # copy mode skips it; git never had it
    (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    (sb / "new.txt").write_text("fresh\n", encoding="utf-8", newline="\n")
    patch = sandbox.diff(sb)
    assert "+ONE" in patch and "new.txt" in patch and "+fresh" in patch
    assert (target / "a.txt").read_text(encoding="utf-8") == "one\n"   # the target is untouched
    sandbox.remove(target, sb)
    assert not sb.exists()
    if kind == "git":
        assert len(worktrees(target)) == 1 and status(target) == ""


def test_fingerprint_and_dirty(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    fp = sandbox.fingerprint(repo)
    assert fp.startswith("git:") and len(fp) == 44
    assert not sandbox.is_dirty(repo)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    assert sandbox.is_dirty(repo) and sandbox.fingerprint(repo) == fp   # HEAD did not move
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f").write_text("x", encoding="utf-8")
    one = sandbox.fingerprint(plain)
    (plain / "f").write_text("xy", encoding="utf-8")
    assert one.startswith("copy:") and sandbox.fingerprint(plain) != one


def test_run_cmd_applies_runs_and_cleans_up(tmp_path):
    repo = git_repo(tmp_path / "repo", {"v.txt": "bad\n"})
    sb = tmp_path / "run" / "sandboxes" / "cmd-1"
    res = sandbox.run_cmd(repo, sb, "grep -q good v.txt")
    assert res == {"applied": True, "rc": 1, "timed_out": False, "output_tail": ""}
    work = tmp_path / "run" / "sandboxes" / "w"
    sandbox.create(repo, work)
    (work / "v.txt").write_text("good\n", encoding="utf-8", newline="\n")
    patch = sandbox.diff(work)
    sandbox.remove(repo, work)
    assert sandbox.run_cmd(repo, sb, "grep -q good v.txt", patch=patch)["rc"] == 0
    bad = sandbox.run_cmd(repo, sb, "true", patch="this is not a patch\n")
    assert bad["applied"] is False and bad["rc"] is None
    slow = sandbox.run_cmd(repo, sb, "echo started; sleep 30", timeout=1)
    assert slow["timed_out"] is True and "started" in slow["output_tail"]
    assert not sb.exists() and len(worktrees(repo)) == 1
    assert (repo / "v.txt").read_text(encoding="utf-8") == "bad\n"


def test_cleanup_removes_leftover_sandboxes(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a": "1\n"})
    run = tmp_path / "run"
    for name in ("probe-1", "probe-2"):
        sandbox.create(repo, run / "sandboxes" / name)
    sandbox.cleanup(run, repo)
    assert list((run / "sandboxes").iterdir()) == [] and len(worktrees(repo)) == 1


def test_odd_targets_fall_back_to_a_copy(tmp_path):
    repo = git_repo(tmp_path / "repo", {"sub/a.txt": "1\n", "b.txt": "2\n"})
    assert sandbox.is_git_root(repo) and not sandbox.is_git_root(repo / "sub")
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)       # a repo with no commit
    (empty / "c.txt").write_text("3\n", encoding="utf-8")
    assert not sandbox.is_git_root(empty)
    for target, name in ((repo / "sub", "a.txt"), (empty, "c.txt")):
        sb = tmp_path / "run" / "sandboxes" / ("s-" + target.name)
        sandbox.create(target, sb)
        assert (sb / name).exists() and (sb / ".git").is_dir()          # a fresh repo, not a worktree
        (sb / name).unlink()
        (sb / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
        patch = sandbox.diff(sb)
        assert "deleted file" in patch and "new file" in patch
        sandbox.remove(target, sb)
        res = sandbox.run_cmd(target, sb, "test ! -e %s && test -f new.txt" % name, patch=patch)
        assert res["applied"] and res["rc"] == 0, res
    assert len(worktrees(repo)) == 1 and (repo / "sub" / "a.txt").exists()


# ---------------------------------------------------------------- the independent-clone design

def git_at(cwd, *args):
    """git in cwd; returns stdout as text, errors ignored (the point of the tests below is
    where commands land, not whether they succeed)."""
    return subprocess.run(["git"] + [str(a) for a in args], cwd=str(cwd), capture_output=True,
                          text=True, check=False).stdout


def target_state(repo):
    """Everything a sandbox could leak into the target: its refs and its stash list."""
    return git_at(repo, "for-each-ref"), git_at(repo, "stash", "list")


def loose_count(repo):
    for line in git_at(repo, "count-objects", "-v").splitlines():
        if line.startswith("count:"):
            return int(line.split()[1])
    raise AssertionError("no loose count in %r" % git_at(repo, "count-objects", "-v"))


def test_agent_git_commands_do_not_touch_the_target(tmp_path):
    target = git_repo(tmp_path / "target", {"a.txt": "one\n"})
    before = target_state(target)
    loose_before = loose_count(target)
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    git_at(sb, "add", "-A")
    git_at(sb, "stash")
    git_at(sb, "stash", "pop")
    git_at(sb, "branch", "agent-br")
    (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    git_at(sb, "-c", "user.name=a", "-c", "user.email=a@example.com", "commit", "-qam", "x")
    sandbox.diff(sb)
    sandbox.remove(target, sb)
    assert not sb.exists()
    assert target_state(target) == before
    assert loose_count(target) == loose_before


def test_agent_commit_is_still_in_the_patch(tmp_path):
    target = git_repo(tmp_path / "target", {"a.txt": "one\n"})
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.com",
                    "commit", "-qam", "agent edit"], cwd=str(sb), check=True)
    patch = sandbox.diff(sb)
    assert "+ONE" in patch and "-one" in patch, patch
    sandbox.remove(target, sb)


def test_target_hooks_do_not_run(tmp_path):
    if os.name != "posix":
        pytest.skip("needs POSIX to make the hook file executable")
    target = git_repo(tmp_path / "target", {"a.txt": "one\n"})
    hook = target / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch '%s'\n" % (target / "HOOKED"), encoding="utf-8")
    hook.chmod(0o755)
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    sandbox.diff(sb)
    sandbox.remove(target, sb)
    assert not (target / "HOOKED").exists()


def test_no_remote_points_at_the_target(tmp_path):
    target = git_repo(tmp_path / "target", {"a.txt": "1\n"})
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    assert git_at(sb, "remote").strip() == ""
    sandbox.remove(target, sb)


def test_crlf_and_latin1_edits_round_trip(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    (target / "b.txt").write_bytes(b"caf\xe9\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git"] + ident + ["add", "-A"], cwd=str(target), check=True)
    subprocess.run(["git"] + ident + ["commit", "-q", "-m", "init"], cwd=str(target), check=True)
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "a.txt").write_bytes(b"one\r\nTWO\r\n")
    (sb / "b.txt").write_bytes(b"CAF\xe9\n")
    patch = sandbox.diff(sb)
    res = sandbox.run_cmd(target, tmp_path / "run" / "sandboxes" / "cmd",
                          "cat a.txt b.txt > out.bin; exit 0", patch=patch)
    assert res["applied"] is True, res
    sb2 = tmp_path / "run" / "sandboxes" / "verify"
    sandbox.create(target, sb2)
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=str(sb2),
                   input=patch.encode("utf-8", "surrogateescape"), check=True)
    assert (sb2 / "a.txt").read_bytes() == b"one\r\nTWO\r\n"
    assert (sb2 / "b.txt").read_bytes() == b"CAF\xe9\n"
    sandbox.remove(target, sb)
    sandbox.remove(target, sb2)


def test_deleted_git_dir_still_cleans_up(tmp_path):
    target = git_repo(tmp_path / "target", {"a.txt": "1\n"})
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    shutil.rmtree(str(sb / ".git"))
    sandbox.remove(target, sb)
    assert not sb.exists() and not (sb.parent / (sb.name + ".base")).exists()
    sandbox.create(target, sb)                       # the path is free again
    assert (sb / "a.txt").read_text(encoding="utf-8") == "1\n"
    sandbox.remove(target, sb)


def test_cleanup_without_target(tmp_path):
    run = tmp_path / "run"
    leftover = run / "sandboxes" / "probe-1"
    leftover.mkdir(parents=True)
    (leftover / "f").write_text("x", encoding="utf-8")
    sandbox.cleanup(run, None)
    assert list((run / "sandboxes").iterdir()) == []


def test_copy_mode_ignored_file_edit_is_in_patch(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / ".gitignore").write_text("gen.txt\n", encoding="utf-8")
    (target / "gen.txt").write_text("1\n", encoding="utf-8", newline="\n")
    (target / "keep.txt").write_text("k\n", encoding="utf-8", newline="\n")
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "gen.txt").write_text("2\n", encoding="utf-8", newline="\n")
    patch = sandbox.diff(sb)
    assert "gen.txt" in patch and "+2" in patch, patch
    sandbox.remove(target, sb)


def test_copy_mode_skips_the_test_and_lint_caches(tmp_path):
    """A target left after a test run holds its caches; they are build artifacts, not the
    code under review -- and a .tox or .nox directory is a whole second copy of it."""
    target = tmp_path / "target"
    for rel in (".pytest_cache/v/cache/lastfailed", ".mypy_cache/x.data", ".ruff_cache/y",
                ".tox/py312/lib/site.py", ".nox/unit/a.py", "src/a.py", "keep.txt"):
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8", newline="\n")
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    assert (sb / "src" / "a.py").exists() and (sb / "keep.txt").exists()
    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"):
        assert not (sb / name).exists(), name
    sandbox.remove(target, sb)
    # the copy fingerprint walks the same list, so a cache written afterwards changes nothing
    fp = sandbox.fingerprint(target)
    (target / ".pytest_cache" / "z").write_text("changed\n", encoding="utf-8", newline="\n")
    assert sandbox.fingerprint(target) == fp


def test_timeout_with_a_background_grandchild_returns_quickly(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX only")
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    sb = tmp_path / "run" / "sandboxes" / "slow"
    cmd = ("setsid sleep 30 > /dev/null 2>&1 & sleep 30" if shutil.which("setsid")
           else "(sleep 30 &) ; sleep 30")
    start = time.time()
    res = sandbox.run_cmd(repo, sb, cmd, timeout=1)
    assert res["timed_out"] is True
    assert time.time() - start < 6


def test_build_artifacts_stay_out_of_the_patch(tmp_path):
    """A probe that runs Python leaves __pycache__/*.pyc in its sandbox; with no
    .gitignore on the target those used to land in the patch. create() excludes them."""
    for kind in ("git", "copy"):
        if kind == "git":                        # a git target with no .gitignore
            target = git_repo(tmp_path / ("t-" + kind), {"a.txt": "one\n", "sub/b.txt": "two\n"})
            assert not (target / ".gitignore").exists()
        else:                                    # the same files as a plain folder
            target = tmp_path / ("t-" + kind)
            (target / "sub").mkdir(parents=True)
            (target / "a.txt").write_text("one\n", encoding="utf-8", newline="\n")
            (target / "sub/b.txt").write_text("two\n", encoding="utf-8", newline="\n")
        sb = tmp_path / "run" / "sandboxes" / ("probe-" + kind)
        sandbox.create(target, sb)
        (sb / "__pycache__").mkdir()
        (sb / "__pycache__" / "test_calc.cpython-312.pyc").write_bytes(b"\xe3\r\r\nfake\n")
        (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
        patch = sandbox.diff(sb)
        assert "+ONE" in patch, kind
        assert "__pycache__" not in patch and ".pyc" not in patch, kind
        assert "sub/b.txt" not in patch, kind    # untouched tracked files stay out too
        sandbox.remove(target, sb)


# ---------------------------------------------------------------- relative paths and leftovers

def test_relative_paths_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = git_repo("tgt", {"a.txt": "one\n", "sub/b.txt": "two\n"})
    landing = tmp_path / "run" / "sandboxes" / "rel"
    sandbox.create(target, "run/sandboxes/rel")                # both paths relative
    assert landing.is_dir()
    assert not (tmp_path / "run" / "sandboxes" / "run").exists()   # not nested under itself
    assert (landing / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (landing / "sub" / "b.txt").read_text(encoding="utf-8") == "two\n"
    (landing / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    assert "+ONE" in sandbox.diff("run/sandboxes/rel")
    sandbox.remove(target, "run/sandboxes/rel")
    assert not landing.exists()
    assert not (tmp_path / "run" / "sandboxes" / "rel.base").exists()
    assert len(worktrees(target)) == 1 and status(target) == ""
    res = sandbox.run_cmd("tgt", "run/sandboxes/cmd", "exit 0")
    assert res["applied"] is True and res["rc"] == 0, res
    assert not (tmp_path / "run" / "sandboxes" / "cmd").exists()


def test_cleanup_removes_stray_files(tmp_path):
    run = tmp_path / "run"
    boxes = run / "sandboxes"
    boxes.mkdir(parents=True)
    (boxes / "x.base").write_text("deadbeef\n", encoding="utf-8")
    (boxes / "y.out").write_text("some output\n", encoding="utf-8")
    (boxes / "keep.txt").write_text("stays\n", encoding="utf-8")
    sandbox.cleanup(run, None)
    assert [p.name for p in boxes.iterdir()] == ["keep.txt"]


# ---------------------------------------------------------------- symlinked sandboxes


def precious_and_boxes(tmp_path):
    """<tmp>/precious holding keep.txt, plus an empty <tmp>/run/sandboxes ready to
    receive a symlink named sb. Returns (outside, the sb link path)."""
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete me\n", encoding="utf-8", newline="\n")
    boxes = tmp_path / "run" / "sandboxes"
    boxes.mkdir(parents=True)
    return outside, boxes / "sb"


def symlink_or_skip(target, link):
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):                # Windows without privileges, etc.
        pytest.skip("os.symlink(%r, %r) is not supported here" % (target, link))


def assert_link_gone_target_intact(outside, link):
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "do not delete me\n"
    assert not link.is_symlink() and not link.exists()


def test_remove_does_not_follow_a_symlinked_sandbox(tmp_path):
    outside, sb = precious_and_boxes(tmp_path)
    symlink_or_skip(outside, sb)                          # the whole sandbox is a link
    sandbox.remove(None, tmp_path / "run" / "sandboxes" / "sb")
    assert_link_gone_target_intact(outside, sb)
    symlink_or_skip(outside, sb)                          # put the link back for cleanup()
    sandbox.cleanup(tmp_path / "run", None)
    assert_link_gone_target_intact(outside, sb)


def test_relative_symlinked_sandbox_is_not_followed(tmp_path, monkeypatch):
    outside, sb = precious_and_boxes(tmp_path)
    symlink_or_skip("../../precious", sb)                 # a relative link, a relative name
    monkeypatch.chdir(tmp_path)
    sandbox.remove(None, "run/sandboxes/sb")
    assert_link_gone_target_intact(outside, sb)


# ----------------------------------------- links at the root, the .out file and the .base file

def test_symlinked_sandboxes_root_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    keep = outside / "sub" / "keep.txt"
    keep.write_text("do not delete me\n", encoding="utf-8", newline="\n")
    run = tmp_path / "run"
    run.mkdir()
    boxes = run / "sandboxes"
    symlink_or_skip(outside, boxes)                       # the whole root is the link
    sandbox.cleanup(run, None)
    assert keep.read_text(encoding="utf-8") == "do not delete me\n"
    assert not boxes.is_symlink() and not boxes.exists()  # cleanup() unlinked the link
    symlink_or_skip(outside, boxes)                       # put it back for create()
    target = git_repo(tmp_path / "target", {"a.txt": "1\n"})
    with pytest.raises(RuntimeError, match="is a symlink"):
        sandbox.create(target, boxes / "x")
    assert keep.read_text(encoding="utf-8") == "do not delete me\n"
    assert not (outside / "x").exists()                   # nothing was built through it


def test_run_cmd_does_not_write_through_a_planted_out_link(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.txt": "1\n"})
    boxes = tmp_path / "run" / "sandboxes"
    boxes.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep")
    symlink_or_skip(victim, boxes / "c.out")              # the fixed name run_cmd used to open
    res = sandbox.run_cmd(repo, boxes / "c", "echo hi")
    assert res["applied"] is True and res["rc"] == 0 and "hi" in res["output_tail"]
    assert victim.read_bytes() == b"keep"


def test_symlinked_base_file_falls_back_to_head(tmp_path):
    target = git_repo(tmp_path / "target", {"a.txt": "one\n"})
    sb = tmp_path / "run" / "sandboxes" / "probe"
    sandbox.create(target, sb)
    (sb / "a.txt").write_text("ONE\n", encoding="utf-8", newline="\n")
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("not a git sha\n", encoding="utf-8")
    base_file = sb.parent / (sb.name + ".base")
    base_file.unlink()
    symlink_or_skip(decoy, base_file)
    patch = sandbox.diff(sb)                              # must not raise: diff vs HEAD
    assert "+ONE" in patch
    sandbox.remove(target, sb)
    assert decoy.exists()                                 # remove() unlinked the link only


@pytest.mark.parametrize("kind,attrs", [("git", None), ("git", "* text=auto\n"), ("copy", None)])
def test_sandboxes_ignore_the_users_global_line_endings(tmp_path, monkeypatch, kind, attrs):
    # Windows Git Bash ships core.autocrlf=true globally: a sandbox must still check files
    # out byte-for-byte as committed, so a patch made in one sandbox applies in another.
    files = {"main.py": "print(1)\n"}
    if attrs:
        files[".gitattributes"] = attrs
    if kind == "git":
        target = git_repo(tmp_path / "target", files)
    else:
        target = tmp_path / "target"
        target.mkdir()
        for rel, text in files.items():
            (target / rel).write_text(text, encoding="utf-8", newline="\n")
    cfg = tmp_path / "windows-like.gitconfig"
    cfg.write_text("[core]\n\tautocrlf = true\n\teol = crlf\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    sb = sandbox.create(target, tmp_path / "run" / "sandboxes" / "probe")
    assert (sb / "main.py").read_bytes() == b"print(1)\n"
    assert status(sb) == ""
    (sb / "main.py").write_bytes(b"print(2)\n")
    patch = sandbox.diff(sb)
    assert "+print(2)" in patch and "\r" not in patch
    res = sandbox.run_cmd(target, tmp_path / "run" / "sandboxes" / "check", "cat main.py", patch=patch)
    assert res["applied"] and res["rc"] == 0 and "print(2)" in res["output_tail"]
    sandbox.remove(target, sb)
