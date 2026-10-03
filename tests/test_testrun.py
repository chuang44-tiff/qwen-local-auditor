"""qwen-test: only the configured command, only in a throwaway worktree."""
import os
import subprocess
import sys
import threading
import time

import pytest

from lib import testrun

PY = sys.executable.replace("\\", "/")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _worktrees_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(tmp_path / "wts"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "check.py").write_text(
        "import os, sys\n"
        "open('side-effect.txt', 'w').write('x')\n"
        "sel = sys.argv[1:]\n"
        "ok = open('value.txt').read().strip() == 'good'\n"
        "print('selected', sel)\n"
        "print('ok' if ok else 'FAILED value is not good')\n"
        "sys.exit(0 if ok else 1)\n")
    (r / "value.txt").write_text("bad\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def cmd():
    return "%s check.py" % PY


def test_selectors_accept_pytest_ids():
    assert testrun.validate_selectors(["tests/t.py::test_x[a-b]"]) == ["tests/t.py::test_x[a-b]"]
    assert testrun.validate_selectors(["-k", "retry and not slow"]) == ["-k", "retry and not slow"]


@pytest.mark.parametrize("bad", ["x; rm -rf /", "a|b", "$(id)", "`id`", "a && b", "x>y", "--co", "-x", "@args.txt",
                                "/etc/passwd", "../x", "a/../../x", "-"])
def test_selectors_refuse_shell_and_options(bad):
    with pytest.raises(ValueError):
        testrun.validate_selectors([bad])


def test_k_needs_an_expression():
    with pytest.raises(ValueError):
        testrun.validate_selectors(["-k"])


def test_build_argv_appends_selectors():
    assert testrun.build_argv("pytest -q", ["t.py"]) == ["pytest", "-q", "t.py"]
    with pytest.raises(ValueError):
        testrun.build_argv("   ", [])


def test_runs_against_the_working_tree_but_never_touches_it(repo):
    (repo / "value.txt").write_text("good\n")          # uncommitted edit: the coder's case
    code, out = testrun.run_tests([], cmd=cmd(), source=str(repo), worktree=None,
                                  timeout=60, max_bytes=20000)
    assert code == 0, out
    assert out.splitlines()[0] == "TEST ALL PASSED"
    assert not (repo / "side-effect.txt").exists()   # the test's write landed in the worktree
    assert subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                          capture_output=True, text=True).stdout.count("\n") == 1


def test_failure_line_is_citable(repo):
    code, out = testrun.run_tests(["t1"], cmd=cmd(), source=str(repo), worktree=None,
                                  timeout=60, max_bytes=20000)
    assert code == 1
    assert out.splitlines()[0] == "TEST t1 FAILED: FAILED value is not good"


def test_timeout(repo):
    slow = "%s -c \"import time; time.sleep(30)\"" % PY
    code, out = testrun.run_tests([], cmd=slow, source=str(repo), worktree=None,
                                  timeout=1, max_bytes=20000)
    assert code == 5
    assert out.startswith("TEST ALL TIMEOUT")


def test_output_is_trimmed(repo):
    noisy = "%s -c \"print('x' * 200000); import sys; sys.exit(1)\"" % PY
    code, out = testrun.run_tests([], cmd=noisy, source=str(repo), worktree=None,
                                  timeout=60, max_bytes=2000)
    assert code == 1
    assert len(out.encode()) < 4000
    assert "trimmed" in out


def test_not_a_repo_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="not inside a git repository"):
        testrun.toplevel(str(tmp_path))


def test_repo_without_commits_is_a_clear_error(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(RuntimeError, match="no commits"):
        testrun.toplevel(str(tmp_path))


def test_stale_worktree_is_pruned(repo, tmp_path):
    wt = tmp_path / "wts" / "stale"
    testrun.make_worktree(str(repo), str(wt))
    import shutil
    shutil.rmtree(wt)                                 # a crash: dir gone, git still lists it
    testrun.make_worktree(str(repo), str(wt))         # must not fail
    assert (wt / "check.py").exists()
    testrun.remove_worktree(str(repo), str(wt))


def test_persistent_worktree_keeps_extra_files_and_reports_them(repo, tmp_path):
    wt = testrun.prepare(str(repo))
    try:
        with open(os.path.join(wt, "test_repro.py"), "w") as f:
            f.write("assert False\n")
        code, _ = testrun.run_tests([], cmd=cmd(), source=str(repo), worktree=wt,
                                    timeout=60, max_bytes=20000)
        assert os.path.exists(os.path.join(wt, "test_repro.py"))   # untracked files survive sync
        assert "test_repro.py" in testrun.changed_files(str(repo), wt)
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_parallel_calls_are_serialized(repo):
    wt = testrun.prepare(str(repo))
    try:
        results = []

        def go():
            results.append(testrun.run_tests([], cmd=cmd(), source=str(repo), worktree=wt,
                                             timeout=60, max_bytes=20000)[0])
        ts = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results == [1, 1, 1]
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_cli_refuses_without_a_configured_command(repo):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    p = subprocess.run([sys.executable, testrun.__file__], cwd=str(repo), env=env,
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "QWEN_TEST_CMD" in p.stderr


def test_k_expression_allowlist():
    for bad in ("--co", "-x", "a; b", "$(id)", "a|b"):
        with pytest.raises(ValueError):
            testrun.validate_selectors(["-k", bad])


def test_trailing_newline_selector_refused():
    # A `$` anchor also matches before a trailing newline; only re.fullmatch
    # refuses "a.py\n", which would otherwise reach the test command's argv.
    for bad in ("a.py\n", "tests/t.py::test_x\n"):
        with pytest.raises(ValueError):
            testrun.validate_selectors([bad])
    with pytest.raises(ValueError):
        testrun.validate_selectors(["-k", "slow\n"])


def run_wrapper(repo, *args, **extra):
    wrapper = os.path.join(os.path.dirname(testrun.__file__), os.pardir, "qwen-test.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    env["QWEN_CONFIG"] = os.devnull
    env["QWEN_TEST_CMD"] = cmd()
    env["QWEN_TEST_WORKTREES"] = os.environ["QWEN_TEST_WORKTREES"]
    env.update(extra)
    return subprocess.run(["bash", wrapper, *args], cwd=str(repo), env=env,
                          capture_output=True, text=True)


@pytest.mark.skipif(os.name != "posix", reason="needs bash")
def test_wrapper_refuses_maintenance_modes_and_bad_selectors(repo, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    (repo / "args.txt").write_text("t.py\n")
    for args in (["--cleanup", str(repo), str(victim)], ["--prepare", str(repo)],
                 ["@args.txt"], ["-k", "--co"], ["/etc/passwd"], ["../x"]):
        p = run_wrapper(repo, *args)
        assert p.returncode == 2, (args, p.stdout, p.stderr)
    assert (victim / "keep.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="needs bash")
def test_wrapper_reads_timeout_from_config(repo, tmp_path):
    # The wrapper evals the config but testrun.py only reads the ENVIRONMENT:
    # an unexported QWEN_TEST_TIMEOUT silently falls back to the 600s default,
    # so an exported 1s timeout is what makes this 30s command stop after ~1s.
    cfg = tmp_path / "config"
    cfg.write_text("QWEN_TEST_CMD='sleep 30'\nQWEN_TEST_TIMEOUT=1\n", encoding="utf-8")
    t0 = time.time()
    p = run_wrapper(repo, QWEN_CONFIG=str(cfg))
    assert p.returncode == 5, (p.stdout, p.stderr)
    assert p.stdout.startswith("TEST ALL TIMEOUT")
    assert time.time() - t0 < 20, "the config timeout never reached the runner"


def test_remove_worktree_refuses_outside_root(repo, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    with pytest.raises(RuntimeError, match="refusing"):
        testrun.remove_worktree(str(repo), str(victim))
    assert (victim / "keep.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_timeout_kills_grandchildren(repo, tmp_path):
    pidfile = tmp_path / "pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(%r, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n" % str(pidfile))
    code, _ = testrun.run_tests([], cmd="%s %s" % (PY, script), source=str(repo), worktree=None,
                                timeout=2, max_bytes=20000)
    assert code == 5
    pid = int(pidfile.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_parallel_runs_do_not_overlap(repo, tmp_path):
    log = tmp_path / "times.log"
    script = tmp_path / "timed.py"
    script.write_text(
        "import time\n"
        "t0 = time.time(); time.sleep(0.4); t1 = time.time()\n"
        "open(%r, 'a').write('%%f %%f\\n' %% (t0, t1))\n" % str(log))
    wt = testrun.prepare(str(repo))
    try:
        ts = [threading.Thread(target=lambda: testrun.run_tests(
            [], cmd="%s %s" % (PY, script), source=str(repo), worktree=wt,
            timeout=60, max_bytes=20000)) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        iv = sorted(tuple(map(float, l.split())) for l in log.read_text().splitlines())
        assert len(iv) == 3
        for a, b in zip(iv, iv[1:]):
            assert a[1] <= b[0]
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_sync_deletes_tracked_but_keeps_untracked(repo):
    wt = testrun.prepare(str(repo))
    try:
        with open(os.path.join(wt, "mine.py"), "w") as f:
            f.write("x")
        (repo / "value.txt").unlink()
        testrun.sync_tree(str(repo), wt)
        assert not os.path.exists(os.path.join(wt, "value.txt"))
        assert os.path.exists(os.path.join(wt, "mine.py"))
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_killed_lock_holder_frees_the_lock_at_once(repo):
    # The lock is an OS file lock: the kernel drops it when the holder dies, even when it
    # is killed right after taking the lock (the old mkdir+pid lock was never stolen then).
    wt = testrun.prepare(str(repo))
    try:
        code = ("import sys, time; sys.path.insert(0, %r); from lib import testrun\n"
                "with testrun._lock(%r, 60):\n    print('held', flush=True); time.sleep(60)\n"
                % (os.path.dirname(os.path.dirname(testrun.__file__)), wt))
        holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(RuntimeError):
            with testrun._lock(wt, 1):          # held: a short wait times out
                pass
        holder.kill(); holder.wait()
        t0 = time.time()
        with testrun._lock(wt, 60):
            pass
        assert time.time() - t0 < 2
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_lock_waiters_never_overlap(repo):
    # Two concurrent takers never hold the lock at the same time.
    wt = testrun.prepare(str(repo))
    marks = os.path.join(str(repo), "..", "marks.txt")
    try:
        code = ("import sys, time; sys.path.insert(0, %r); from lib import testrun\n"
                "for _ in range(5):\n"
                "    with testrun._lock(%r, 60):\n"
                "        open(%r, 'a').write('in\\n'); time.sleep(0.05); open(%r, 'a').write('out\\n')\n"
                % (os.path.dirname(os.path.dirname(testrun.__file__)), wt, marks, marks))
        ps = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
        for p in ps:
            assert p.wait() == 0
        lines = open(marks).read().split()
        assert lines == ["in", "out"] * 15
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_legacy_lock_dir_does_not_block(repo):
    # A lock directory left by an older version (mkdir + pid) must not wedge new runs.
    wt = testrun.prepare(str(repo))
    try:
        os.mkdir(wt + ".lock")
        with open(os.path.join(wt + ".lock", "pid"), "w") as f:
            f.write(str(os.getpid()))
        t0 = time.time()
        with testrun._lock(wt, 60):
            pass
        assert time.time() - t0 < 2
    finally:
        testrun.remove_worktree(str(repo), wt)


def test_child_env_restores_python_vars(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "wrapper")
    monkeypatch.setenv("QWEN_TEST_ORIG_PYTHONPATH", "__unset__")
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("QWEN_TEST_ORIG_PYTHONUTF8", "0")
    env = testrun._child_env()
    assert "PYTHONPATH" not in env and env["PYTHONUTF8"] == "0"
    assert not any(k.startswith("QWEN_TEST_ORIG") for k in env)


def test_trim_is_strictly_bounded():
    text = "\n".join("FAILED line %d " % i + "y" * 200 for i in range(500))
    assert len(testrun._trim(text, 2000).encode()) <= 2000


def test_fail_lines_catch_compiler_and_jest():
    # A test command that dies in the build never prints FAILED: its reason is in
    # the compiler's or jest's own words, lowercase. Those lines must be put first
    # in an excerpt (and count as the failure line) just like a runner's.
    for line in [
        "src/main.c:12:5: error: expected ';' before '}' token",        # gcc / clang
        "cc1: error: unrecognized command-line option '-std=c23'",
        "src/main.c:1:10: fatal error: missing.h: No such file or directory",
        "error[E0425]: cannot find value `x` in this scope",             # rustc
        '    Expected: "two"',                                          # jest
        '    Received: "one"',
        "  - Expected  - 1",
        "  + Received  + 1",
        "--- FAIL: TestParse (0.00s)",                                   # go test
        "    --- FAIL: TestParse/empty (0.00s)",
    ]:
        assert testrun._FAIL_LINE.search(line), line
    # everything it matched before still matches
    for line in [
        "FAILED tests/test_x.py::test_y - assert 0 == 1",
        "ERROR tests/test_x.py",
        "Error: something went wrong",
        "AssertionError: nope",
        "thread 'main' panicked at 'index out of bounds'",
        "FAIL\texample.com/mypkg\t0.01s",
        "not ok 1 - the thing",
    ]:
        assert testrun._FAIL_LINE.search(line), line
    # a clean run is still not a failure line
    for line in ["test session starts", "collected 12 items", "ok 1 - the thing",
                 "PASS\texample.com/mypkg\t0.01s"]:
        assert not testrun._FAIL_LINE.search(line), line


def test_nonpositive_timeout_refused(repo):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
    env.update(QWEN_TEST_CMD=cmd(), QWEN_TEST_TIMEOUT="0")
    p = subprocess.run([sys.executable, testrun.__file__, "--selectors"], cwd=str(repo),
                       env=env, capture_output=True, text=True)
    assert p.returncode == 2


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_prepare_returns_the_resolved_path(repo, tmp_path, monkeypatch):
    # macOS: a temp dir under /var is really /private/var, and a permission rule
    # built from the unresolved path never matches what Claude Code compares.
    real = tmp_path / "real-wts"
    real.mkdir()
    link = tmp_path / "linked-wts"
    os.symlink(str(real), str(link))
    monkeypatch.setenv("QWEN_TEST_WORKTREES", str(link))
    wt = testrun.prepare(str(repo))
    try:
        assert wt == os.path.realpath(wt)
        assert wt.startswith(os.path.realpath(str(real)) + os.sep)
    finally:
        testrun.remove_worktree(str(repo), wt)
    assert not os.path.exists(wt)


def test_huge_test_output_is_read_back_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(testrun, "RUN_OUTPUT_CAP", 4096)
    rc, out = testrun._run([sys.executable, "-c",
                            "print('FIRST'); print('x' * 100000); print('FAILED: LAST')"],
                           str(tmp_path), 60)
    assert rc == 0 and len(out) < 6000
    assert out.startswith("FIRST") and "FAILED: LAST" in out and "not kept" in out
