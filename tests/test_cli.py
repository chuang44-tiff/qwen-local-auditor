"""qwen-agent end to end, offline: a fake /v1/models server and a fake `claude`.

These run the real shell script under the bash named by $TEST_BASH (default: the
first `bash` on PATH), which is how CI exercises macOS's stock bash 3.2 and Git
Bash on Windows as well as Linux.
"""
import http.server
import json
import re
import os
import pathlib
import shutil
import subprocess
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = ROOT / "skill" / "local-auditor" / "qwen-agent.sh"
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")

FAKE_CLAUDE = r'''#!/usr/bin/env bash
# Records how it was invoked, then behaves as $FAKE_MODE says.
# `--help` is the capability probe: answer it without recording, like a real
# claude that has --restricted (FAKE_NO_RESTRICTED=1: an older one that does not).
if [ "${1:-}" = --help ]; then
  echo "  --permission-mode <mode>  Permission mode to use for the session"
  [ -n "${FAKE_NO_RESTRICTED:-}" ] || echo "  --restricted              Restricted mode"
  exit 0
fi
{
  for a in "$@"; do printf 'ARG:%s\n' "$a"; done
  env | grep -E '^(ANTHROPIC_|CLAUDE_|AWS_BEARER|QWEN_TEST_)' | sort
} > "$FAKE_RECORD"
ok='{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","session_id":"fake-session-1","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
case "${FAKE_MODE:-ok}" in
  ok)      printf '%s\n' "$ok" ;;
  unicode) printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"“quoted” → ❌ 完了"}' ;;
  error)   printf '%s\n' '{"type":"result","subtype":"error_max_turns","is_error":true,"terminal_reason":"max_turns","result":"gave up"}' ;;
  apierr)  printf '%s\n' '{"type":"result","is_error":true,"api_error_status":400,"result":"API Error: 400"}' ;;
  empty)   printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"","session_id":"fake-session-1"}' ;;
  denied)  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":2,"result":"partial","permission_denials":[{"tool_name":"Bash"}]}' ;;
  garbage) printf 'this is not json\n' ;;
  repro)   wt="$QWEN_TEST_WORKTREE"; command -v cygpath >/dev/null 2>&1 && wt="$(cygpath -u "$wt")"
           printf 'def test_repro():\n    assert False\n' > "$wt/test_repro.py"; printf '%s\n' "$ok" ;;
  sleep)   sleep 20; printf '%s\n' "$ok" ;;
esac
'''


class _Models(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server naming)
        srv = self.server
        if srv.key and self.headers.get("Authorization") != "Bearer " + srv.key:
            return self._send(401, b'{"error": "unauthorized"}')
        if self.path.rstrip("/") != "/v1/models":
            return self._send(404, b'{"error": "not found"}')
        payload = srv.payload if srv.payload is not None else {"object": "list", "data": srv.models}
        return self._send(200, json.dumps(payload).encode())

    def log_message(self, *args):
        pass


def posix(p):
    """Forward slashes work for Git Bash on Windows and change nothing elsewhere."""
    return str(p).replace("\\", "/")


def same_path(p):
    """One spelling for a path whatever produced it: Python on Windows says C:\\x\\y,
    Git Bash says /c/x/y, cygpath -m says C:/x/y. Compare these, not the raw strings."""
    s = str(p).replace("\\", "/")
    m = re.match(r"^/([A-Za-z])/(.*)$", s)
    if m and os.name == "nt":
        s = "%s:/%s" % (m.group(1), m.group(2))
    if re.match(r"^[A-Za-z]:/", s):
        s = s[0].upper() + s[1:]
    return s


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    httpd.models = [{"id": "local-model", "object": "model", "max_model_len": 262144}]
    httpd.payload = None
    httpd.key = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def base_url(server):
    return "http://127.0.0.1:%d" % server.server_address[1]


@pytest.fixture
def fake(tmp_path):
    p = tmp_path / "fake-claude"
    p.write_text(FAKE_CLAUDE, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def run(tmp_path, args, server=None, fake=None, extra=None, timeout=90, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QWEN_", "CLAUDE_", "ANTHROPIC_"))}
    env["QWEN_CONFIG"] = posix(tmp_path / "no-such-config")   # never read the dev's own
    env["FAKE_RECORD"] = posix(tmp_path / "record.txt")
    if server is not None:
        env["QWEN_BASE_URL"] = base_url(server)
    if fake is not None:
        env["QWEN_CLAUDE_BIN"] = posix(fake)
    env.update(extra or {})
    return subprocess.run([BASH, posix(AGENT), *args], env=env, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=str(cwd or tmp_path))


def record(tmp_path):
    lines = (tmp_path / "record.txt").read_text(encoding="utf-8").splitlines()
    argv = [ln[4:] for ln in lines if ln.startswith("ARG:")]
    env = dict(ln.split("=", 1) for ln in lines if not ln.startswith("ARG:") and "=" in ln)
    return argv, env


def rule_path(p):
    """What a permission rule for absolute path p must look like: //POSIX-form.

    Claude Code matches rules against POSIX-form paths and turns a Windows drive
    path C:\\x\\y into /c/x/y, so on Windows (Git Bash) the native worktree path
    the harness reports is expected in that form.
    """
    p = posix(p)
    if len(p) > 2 and p[1] == ":" and p[2] == "/":
        p = "/" + p[0].lower() + p[2:]
    return "//" + p.lstrip("/")


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def wait_for_status(path, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists() and "exit=" in path.read_text(encoding="utf-8"):
            return path.read_text(encoding="utf-8")
        time.sleep(0.2)
    raise AssertionError("no finished status in %s" % path)


# ------------------------------------------------------------------ preflight

def test_preflight_uses_the_only_served_model(tmp_path, server, fake):
    r = run(tmp_path, ["--preflight-only"], server, fake)
    assert r.returncode == 0, r.stderr
    assert "local-model" in r.stderr and "context 262144" in r.stderr


def test_several_served_models_require_a_choice(tmp_path, server, fake):
    server.models = [{"id": "alpha"}, {"id": "beta"}]
    r = run(tmp_path, ["--preflight-only"], server, fake)
    assert r.returncode == 3
    assert "several models" in r.stderr and "alpha" in r.stderr and "beta" in r.stderr
    assert run(tmp_path, ["-m", "beta", "--preflight-only"], server, fake).returncode == 0


def test_embedding_models_do_not_count_as_a_choice(tmp_path, server, fake):
    server.models = [{"id": "text-embedding-small"}, {"id": "chat-model"}]
    r = run(tmp_path, ["--preflight-only"], server, fake)
    assert r.returncode == 0, r.stderr
    assert "chat-model" in r.stderr


def test_a_named_model_must_be_served_unless_auto_model(tmp_path, server, fake):
    assert run(tmp_path, ["-m", "other", "--preflight-only"], server, fake).returncode == 3
    r = run(tmp_path, ["-m", "other", "--auto-model", "--preflight-only"], server, fake)
    assert r.returncode == 0, r.stderr
    r = run(tmp_path, ["-m", "other", "--preflight-only"], server, fake,
            extra={"QWEN_AUTO_MODEL": "1"})
    assert r.returncode == 0, r.stderr


def test_an_unreachable_server_is_a_preflight_failure(tmp_path, fake):
    r = run(tmp_path, ["--preflight-only"], fake=fake,
            extra={"QWEN_BASE_URL": "http://127.0.0.1:9"})
    assert r.returncode == 3
    assert "cannot reach" in r.stderr


def test_a_server_that_wants_a_key_gets_one_and_says_so(tmp_path, server, fake):
    server.key = "sekrit"
    r = run(tmp_path, ["--preflight-only"], server, fake)
    assert r.returncode == 3 and "QWEN_API_KEY" in r.stderr
    r = run(tmp_path, ["--preflight-only"], server, fake, extra={"QWEN_API_KEY": "sekrit"})
    assert r.returncode == 0, r.stderr


def test_a_v1_suffix_on_the_base_url_is_diagnosed(tmp_path, server, fake):
    r = run(tmp_path, ["--preflight-only"], fake=fake,
            extra={"QWEN_BASE_URL": base_url(server) + "/v1"})
    assert r.returncode == 3
    assert "404" in r.stderr and "/v1" in r.stderr


def test_preflight_can_be_skipped_from_the_environment(tmp_path, fake):
    extra = {"QWEN_BASE_URL": "http://127.0.0.1:9", "QWEN_PREFLIGHT": "0"}
    r = run(tmp_path, ["hi"], fake=fake, extra=extra)
    assert r.returncode == 2 and "no model" in r.stderr
    r = run(tmp_path, ["hi"], fake=fake, extra=dict(extra, QWEN_MODEL="m"))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("payload", [
    [{"id": "local-model", "context_length": "32768"}],
    {"models": [{"name": "local-model", "max_context_length": 32768}]},
], ids=["bare-list", "models-key"])
def test_other_model_listing_shapes_are_understood(tmp_path, server, fake, payload):
    server.payload = payload
    r = run(tmp_path, ["--preflight-only"], server, fake)
    assert r.returncode == 0, r.stderr
    assert "local-model" in r.stderr and "context 32768" in r.stderr


def test_a_config_saved_with_crlf_still_works(tmp_path, server, fake):
    cfg = tmp_path / "config"
    cfg.write_bytes(('QWEN_BASE_URL="%s"\r\nQWEN_MODEL="local-model"\r\n' % base_url(server)).encode())
    r = run(tmp_path, ["--preflight-only"], fake=fake, extra={"QWEN_CONFIG": posix(cfg)})
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------ the run

def test_a_run_returns_the_text_and_is_read_only_by_default(tmp_path, server, fake):
    r = run(tmp_path, ["say", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "fake answer"
    argv, env = record(tmp_path)
    assert flag(argv, "--tools") == "Read,Glob,Grep"
    assert "--strict-mcp-config" in argv
    assert flag(argv, "--model") == "local-model"
    assert flag(argv, "--effort") == "medium"
    assert argv[-2:] == ["--", "say hi"]
    assert env["ANTHROPIC_BASE_URL"] == base_url(server)


def test_the_parent_sessions_provider_and_credentials_do_not_leak(tmp_path, server, fake):
    parent = {"CLAUDE_EFFORT": "high", "CLAUDE_CODE_USE_BEDROCK": "1",
              "ANTHROPIC_API_KEY": "sk-real-key", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "cloud-haiku",
              "AWS_BEARER_TOKEN_BEDROCK": "aws-token"}
    assert run(tmp_path, ["hi"], server, fake, extra=parent).returncode == 0
    _, env = record(tmp_path)
    for k in ("CLAUDE_EFFORT", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY",
              "AWS_BEARER_TOKEN_BEDROCK"):
        assert k not in env, k
    for k in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert env[k] == "local-model", k


def test_the_context_window_is_read_from_the_server(tmp_path, server, fake):
    assert run(tmp_path, ["hi"], server, fake).returncode == 0
    argv, env = record(tmp_path)
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert flag(argv, "--autocompact") == str(262144 * 3 // 4)


def test_an_unknown_window_leaves_claude_defaults_alone_and_says_so(tmp_path, server, fake):
    server.models = [{"id": "local-model"}]
    r = run(tmp_path, ["hi"], server, fake)
    assert r.returncode == 0
    assert "QWEN_CTX" in r.stderr
    argv, env = record(tmp_path)
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env
    assert "--autocompact" not in argv


def test_an_explicit_ctx_wins(tmp_path, server, fake):
    assert run(tmp_path, ["--ctx", "150000", "hi"], server, fake).returncode == 0
    argv, env = record(tmp_path)
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "150000"
    assert flag(argv, "--autocompact") == "112500"


def test_autocompact_must_stay_below_the_window(tmp_path, server, fake):
    r = run(tmp_path, ["--ctx", "150000", "--autocompact", "200000", "hi"], server, fake)
    assert r.returncode == 2


def test_effort_passes_through_unless_an_allow_list_refuses_it(tmp_path, server, fake):
    assert run(tmp_path, ["-e", "high", "hi"], server, fake).returncode == 0
    assert flag(record(tmp_path)[0], "--effort") == "high"
    r = run(tmp_path, ["-e", "high", "hi"], server, fake,
            extra={"QWEN_EFFORT_ALLOWED": "low medium xhigh"})
    assert r.returncode == 2
    assert "not accepted" in r.stderr


def test_effort_default_omits_the_flag(tmp_path, server, fake):
    assert run(tmp_path, ["hi"], server, fake, extra={"QWEN_EFFORT": "default"}).returncode == 0
    assert "--effort" not in record(tmp_path)[0]


def test_effort_reaches_child_env(tmp_path, server, fake):
    # Claude Code's internal model calls (WebFetch summarises pages with its own
    # request) ignore --effort and send "high", which Qwen chat templates reject
    # with a 400. The level must therefore also reach the child environment.
    assert run(tmp_path, ["-e", "xhigh", "hi"], server, fake).returncode == 0
    _, env = record(tmp_path)
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "xhigh"


def test_default_effort_sets_no_env(tmp_path, server, fake):
    # QWEN_EFFORT=default omits --effort; qwen-agent must not set the env var
    # either, or the omitted flag would be contradicted by the child's value.
    assert run(tmp_path, ["hi"], server, fake, extra={"QWEN_EFFORT": "default"}).returncode == 0
    _, env = record(tmp_path)
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env


def test_setting_sources_are_passed_through(tmp_path, server, fake):
    r = run(tmp_path, ["hi"], server, fake, extra={"QWEN_SETTING_SOURCES": "project,local"})
    assert r.returncode == 0
    assert flag(record(tmp_path)[0], "--setting-sources") == "project,local"


def test_a_prompt_starting_with_a_slash_arrives_verbatim(tmp_path, server, fake):
    assert run(tmp_path, ["/review this"], server, fake).returncode == 0
    assert record(tmp_path)[0][-1] == "/review this"


@pytest.mark.parametrize("mode,code", [
    ("error", 8), ("apierr", 4), ("empty", 6), ("denied", 7), ("garbage", 8)])
def test_failure_modes_have_distinct_exit_codes(tmp_path, server, fake, mode, code):
    r = run(tmp_path, ["hi"], server, fake, extra={"FAKE_MODE": mode})
    assert r.returncode == code, r.stderr


def test_empty_result_with_json_still_emits_the_record(tmp_path, server, fake):
    r = run(tmp_path, ["--json", "hi"], server, fake, extra={"FAKE_MODE": "empty"})
    assert r.returncode == 6, r.stderr
    assert json.loads(r.stdout)["session_id"] == "fake-session-1"
    r = run(tmp_path, ["hi"], server, fake, extra={"FAKE_MODE": "empty"})
    assert r.returncode == 6 and r.stdout == ""


def test_non_ascii_output_survives_text_and_json(tmp_path, server, fake):
    r = run(tmp_path, ["hi"], server, fake, extra={"FAKE_MODE": "unicode"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "“quoted” → ❌ 完了"
    r = run(tmp_path, ["--json", "hi"], server, fake, extra={"FAKE_MODE": "unicode"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["result"] == "“quoted” → ❌ 完了"


@pytest.mark.parametrize("timeout_bin", ["", "none"], ids=["gnu-timeout-if-present", "watchdog"])
def test_the_wall_clock_timeout_fires(tmp_path, server, fake, timeout_bin):
    extra = {"FAKE_MODE": "sleep"}
    if timeout_bin:
        extra["QWEN_TIMEOUT_BIN"] = timeout_bin
    t0 = time.time()
    r = run(tmp_path, ["--timeout", "2", "hi"], server, fake, extra=extra)
    assert r.returncode == 5, r.stderr
    assert time.time() - t0 < 18, "the timeout did not cut the run short"


# ------------------------------------------------------------------ output

def test_out_writes_the_result_to_a_file(tmp_path, server, fake):
    out = tmp_path / "result.md"
    assert run(tmp_path, ["-o", posix(out), "hi"], server, fake).returncode == 0
    assert out.read_text(encoding="utf-8").strip() == "fake answer"


def test_a_relative_out_is_relative_to_the_caller_not_to_cd(tmp_path, server, fake):
    audited = tmp_path / "audited"
    audited.mkdir()
    r = run(tmp_path, ["-C", posix(audited), "-o", "findings.md", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "findings.md").exists()
    assert not (audited / "findings.md").exists()


def test_detach_writes_a_status_sidecar(tmp_path, server, fake):
    out = tmp_path / "job.md"
    r = run(tmp_path, ["-w", "-o", posix(out), "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    assert "exit=0" in wait_for_status(pathlib.Path(str(out) + ".status"))
    assert out.read_text(encoding="utf-8").strip() == "fake answer"


def test_detach_returns_before_the_job_finishes(tmp_path, server, fake):
    out = tmp_path / "slow.md"
    t0 = time.time()
    r = run(tmp_path, ["-w", "--timeout", "8", "-o", posix(out), "hi"], server, fake,
            extra={"FAKE_MODE": "sleep"})
    assert r.returncode == 0, r.stderr
    assert time.time() - t0 < 6, "-w must not hold the caller's output open until the job ends"
    assert "exit=5" in wait_for_status(pathlib.Path(str(out) + ".status"))


def test_detach_without_out_creates_a_unique_file(tmp_path, server, fake):
    r = run(tmp_path, ["-w", "hi"], server, fake, extra={"QWEN_OUTDIR": posix(tmp_path)})
    assert r.returncode == 0, r.stderr
    outs = list(tmp_path.glob("qwen-agent-*.out"))
    assert len(outs) == 1
    assert "exit=0" in wait_for_status(pathlib.Path(str(outs[0]) + ".status"))


# ------------------------------------------------------------------ resume

def test_resume_is_passed_through(tmp_path, server, fake):
    r = run(tmp_path, ["--resume", "abc-123", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = record(tmp_path)
    assert flag(argv, "--resume") == "abc-123"
    # the prompt still comes last, after the '--' guard
    assert argv[-2:] == ["--", "hi"]


def test_resume_needs_a_value(tmp_path, server, fake):
    r = run(tmp_path, ["--resume"], server, fake)
    assert r.returncode == 2


@pytest.mark.parametrize("args", [
    ["--resume", "-x"],
    ["--resume=-x"],
    ["--resume", "--model"],
    ["--resume", ""],
    ["--resume="],
], ids=["dash", "dash-eq", "double-dash", "empty", "empty-eq"])
def test_resume_refuses_option_like_values(tmp_path, server, fake, args):
    # A value that reads like an option makes claude treat the argument that
    # follows as the session id and the real one as spare; an empty id resumes
    # nothing while looking like a resume. Neither is a session id, so refuse
    # before anything runs -- on every run, not just --test.
    r = run(tmp_path, [*args, "hi"], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--resume" in r.stderr
    assert not (tmp_path / "record.txt").exists()       # claude never ran
    # a real id still passes straight through
    assert run(tmp_path, ["--resume", "abc-123", "hi"], server, fake).returncode == 0


def test_status_line_reports_the_session(tmp_path, server, fake):
    r = run(tmp_path, ["hi"], server, fake)
    assert r.returncode == 0
    assert "session=fake-session-1" in r.stderr


def test_prompt_with_dash_words_works(tmp_path, server, fake):
    r = run(tmp_path, ["hi", "-x", "--", "y"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = record(tmp_path)
    assert argv[-1] == "hi -x -- y"


# ------------------------------------------------------------------ misc

def test_dry_run_never_prints_secrets(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "-m", "x", "hi"], fake=fake,
            extra={"QWEN_API_KEY": "sekrit-123", "QWEN_CUSTOM_HEADERS": "X-Token: hdr-456"})
    assert r.returncode == 0, r.stderr
    assert "sekrit-123" not in r.stdout and "hdr-456" not in r.stdout
    assert "<redacted>" in r.stdout


def test_list_roles_reads_the_role_dir(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "reviewer.md").write_text("x", encoding="utf-8")
    (roles / "notes.txt").write_text("y", encoding="utf-8")
    r = run(tmp_path, ["--list-roles"], extra={"QWEN_ROLE_DIR": posix(roles)})
    assert r.returncode == 0, r.stderr
    assert "reviewer" in r.stdout and "notes" in r.stdout
    assert ".md" not in r.stdout.replace(posix(roles), "")


def test_help_names_the_command_and_its_environment(tmp_path):
    r = run(tmp_path, ["--help"])
    assert r.returncode == 0
    assert r.stdout.startswith("qwen-agent v")
    for var in ("QWEN_API_KEY", "QWEN_PREFLIGHT", "QWEN_CLAUDE_BIN", "QWEN_OUTDIR"):
        assert var in r.stdout, var


# ------------------------------------------------------------------ --test

def _git_repo(path):
    path.mkdir(exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, capture_output=True)
    return path


def test_test_flag_grants_only_qwen_test(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    assert r.returncode == 0, r.stderr
    argv, env = record(tmp_path)
    tools = flag(argv, "--tools").split(",")
    assert "Bash" in tools
    grants = flag(argv, "--allowed-tools").split(",")
    bash_grants = [g for g in grants if g.startswith("Bash")]
    assert bash_grants == ["Bash(qwen-test:*)"]
    assert "Edit" not in grants and "Write" not in grants
    assert "Edit" in tools and "Write" in tools
    wt = env["QWEN_TEST_WORKTREE"]
    assert "Write(%s/**)" % rule_path(wt) in grants
    assert "Edit(%s/**)" % rule_path(wt) in grants
    assert flag(argv, "--add-dir") is not None
    assert "--restricted" in argv and flag(argv, "--permission-mode") == "dontAsk"
    assert env["QWEN_TEST_CMD"] == "true"
    # the worktree is removed when the run ends
    assert not os.path.exists(wt)


def test_test_flag_with_write_grants_edits_everywhere(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "-r", "coder", "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    assert r.returncode == 0, r.stderr
    argv, _ = record(tmp_path)
    grants = flag(argv, "--allowed-tools").split(",")
    assert "Edit" in grants and "Write" in grants
    assert flag(argv, "--permission-mode") == "dontAsk"


def test_coder_test_run_has_no_worktree_grants(tmp_path, server, fake):
    # A coder in write mode owns the repo; the worktree is the harness's scratch
    # space. Granting it as well blurs which tree the coder is supposed to edit.
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "-r", "coder", "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    assert r.returncode == 0, r.stderr
    argv, env = record(tmp_path)
    grants = flag(argv, "--allowed-tools").split(",")
    assert "Edit" in grants and "Write" in grants
    assert not [g for g in grants if g.startswith(("Edit(//", "Write(//"))]
    assert "--add-dir" not in argv                     # the worktree is not even shared
    assert same_path(env["QWEN_TEST_WORKTREE"]).startswith(same_path(tmp_path / "wts"))  # still named in the env


def test_test_flag_sets_bash_timeouts(tmp_path, fake):
    # The Bash tool must not cut qwen-test off before the test timeout does.
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake,
            extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_TIMEOUT": "120"})
    assert r.returncode == 0, r.stderr
    assert "BASH_DEFAULT_TIMEOUT_MS=180000" in r.stdout      # (120 + 60) * 1000
    assert "BASH_MAX_TIMEOUT_MS=180000" in r.stdout
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 0, r.stderr
    assert "BASH_DEFAULT_TIMEOUT_MS=660000" in r.stdout      # the 600s default + 60
    assert "BASH_MAX_TIMEOUT_MS=660000" in r.stdout


def _dry_argv(r):
    """The argv lines of a --dry-run, without the brackets."""
    return [ln.strip()[1:-1] for ln in r.stdout.splitlines() if ln.startswith("  [")]


def test_test_flag_read_only_run_is_restricted_and_dont_ask(tmp_path, fake):
    # The fence must not depend on the user's or the repo's Claude settings.
    r = run(tmp_path, ["--dry-run", "-q", "--test", "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    assert "--restricted" in argv
    assert flag(argv, "--permission-mode") == "dontAsk"
    # -q cannot hide that a read-only run now gains Bash and worktree writes
    assert "WARNING" in r.stderr and "gains Bash" in r.stderr


def test_test_flag_write_run_is_restricted_and_dont_ask(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "--test", "-r", "coder", "hi"], fake=fake,
            extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 0, r.stderr
    argv = _dry_argv(r)
    assert "--restricted" in argv
    # dontAsk, not acceptEdits: edits happen because --allowed-tools grants them,
    # and acceptEdits would auto-accept edits the fence never granted.
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert "gains Bash" not in r.stderr


def test_restricted_only_with_test(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "hi"], fake=fake)
    assert r.returncode == 0, r.stderr
    assert "--restricted" not in _dry_argv(r)


@pytest.mark.parametrize("args,needle", [
    (["--permission-mode", "bypassPermissions"], "bypassPermissions"),
    (["--permission-mode=bypassPermissions"], "bypassPermissions"),
    (["--toolset", "Read,Bash"], "--toolset"),
    (["--toolset=Read"], "--toolset"),
    (["--read-only"], "--read-only"),
], ids=["bypass", "bypass-eq", "toolset", "toolset-eq", "read-only"])
def test_test_flag_refuses_fence_overrides(tmp_path, fake, args, needle):
    r = run(tmp_path, ["--dry-run", "--test", *args, "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "record.txt").exists()       # claude never ran


def test_test_runs_always_use_dont_ask(tmp_path, server, fake):
    # dontAsk asks nothing: whatever was not granted is refused, on write runs
    # too -- their edits arrive already granted through --allowed-tools.
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")}
    r = run(tmp_path, ["--test", "-C", posix(repo), "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    assert flag(record(tmp_path)[0], "--permission-mode") == "dontAsk"
    r = run(tmp_path, ["--test", "-r", "coder", "-C", posix(repo), "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    argv, _ = record(tmp_path)
    assert flag(argv, "--permission-mode") == "dontAsk"
    # and the coder still edits, through the grants -- not through acceptEdits
    grants = flag(argv, "--allowed-tools").split(",")
    assert "Edit" in grants and "Write" in grants and "MultiEdit" in grants


@pytest.mark.parametrize("args", [
    ["--permission-mode", "acceptEdits"],
    ["--permission-mode=dontAsk"],          # even the value --test itself uses
    ["--permission-mode", "bypassPermissions"],
], ids=["accept-edits", "own-value", "bypass"])
def test_test_refuses_explicit_permission_mode(tmp_path, fake, args):
    # Any hand-picked mode could skip the grants that fence qwen-test.
    r = run(tmp_path, ["--dry-run", "--test", *args, "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--permission-mode" in r.stderr
    assert not (tmp_path / "record.txt").exists()       # claude never ran
    # without --test the mode stays the caller's to choose
    r = run(tmp_path, ["--dry-run", "--permission-mode", "plan", "hi"], fake=fake)
    assert r.returncode == 0, r.stderr
    assert flag(_dry_argv(r), "--permission-mode") == "plan"


def test_test_drops_setting_sources(tmp_path, fake):
    # --restricted already ignores settings files; --setting-sources under
    # --test would only advertise the door the fence exists to shut.
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake,
            extra={"QWEN_TEST_CMD": "true", "QWEN_SETTING_SOURCES": "project,local"})
    assert r.returncode == 0, r.stderr
    assert "--setting-sources" not in _dry_argv(r)
    # a run without --test still gets it
    r = run(tmp_path, ["--dry-run", "hi"], fake=fake, extra={"QWEN_SETTING_SOURCES": "project,local"})
    assert r.returncode == 0, r.stderr
    assert flag(_dry_argv(r), "--setting-sources") == "project,local"


FAKE_ARGV_NUL = r'''#!/usr/bin/env bash
# Records argv NUL-separated -- a multi-line --append-system-prompt does not
# survive the line-per-arg record.txt format -- and answers with a plain result.
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
printf '%s\0' "$@" > "$FAKE_ARGV_FILE"
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"ok","session_id":"fake-s"}'
'''


def _sys_prompt(tmp_path, server, args):
    """The --append-system-prompt value the run passes to claude (None if absent)."""
    f = tmp_path / "fake-argv"
    f.write_text(FAKE_ARGV_NUL, encoding="utf-8", newline="\n")
    f.chmod(0o755)
    r = run(tmp_path, args, server, f,
            extra={"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
                   "FAKE_ARGV_FILE": posix(tmp_path / "argv.bin")})
    assert r.returncode == 0, r.stderr
    argv = [p.decode("utf-8", "replace") for p in (tmp_path / "argv.bin").read_bytes().split(b"\0") if p]
    return flag(argv, "--append-system-prompt")


def test_test_appends_fence_note(tmp_path, server):
    # The first sentence names the TOOL the model must use: in a benchmark run it
    # called a tool literally named `qwen-test` three times, because "your only
    # shell command" reads like a tool name.
    fence = ("Your only shell command is `qwen-test [SELECTOR]`, run with the Bash tool "
             "(it is a command, not a tool).")
    repo = _git_repo(tmp_path / "repo")
    p = _sys_prompt(tmp_path, server,
                    ["--test", "-r", "coder", "-s", "EXTRA-SYS-TEXT", "-C", posix(repo), "hi"])
    assert fence in p
    assert "Every other Bash command is denied and wastes a turn." in p
    assert "Instead of cat/head/tail use Read; instead of grep/rg use Grep; " \
           "instead of find/ls use Glob." in p
    assert "You cannot run git, python, pip, env or which. " \
           "To check a change, run its test with qwen-test." in p
    # after the role text and the -s text, not before them
    assert 0 <= p.index("CODER") < p.index("EXTRA-SYS-TEXT") < p.index(fence)
    # every role gets it -- a read-only auditor run too
    p = _sys_prompt(tmp_path, server, ["--test", "-r", "auditor", "-C", posix(repo), "hi"])
    assert fence in p and "CODE AUDITOR" in p
    # without --test the system prompt is untouched
    p = _sys_prompt(tmp_path, server, ["-r", "coder", "hi"])
    assert "Your only shell command" not in (p or "")


def test_coder_role_has_spec_and_leniency_rules(tmp_path, server):
    # Benchmark finding: the coder stopped at "it works" instead of "it matches
    # the spec". The role carries the two rules verbatim.
    p = _sys_prompt(tmp_path, server, ["-r", "coder", "hi"])
    flat = " ".join(p.split())
    assert ("- Before you stop, compare your actual output with every explicit requirement in the spec "
            "(names, headers, exact messages, output shape). Read the files you wrote.") in flat
    assert ("- Do not add leniency the spec did not ask for (trimming, normalising, accepting malformed "
            "input), and do not special-case the examples.") in flat


def test_test_flag_needs_a_claude_with_restricted(tmp_path, fake):
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake,
            extra={"QWEN_TEST_CMD": "true", "FAKE_NO_RESTRICTED": "1"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--restricted" in r.stderr and "upgrade claude" in r.stderr
    # without --test an older claude is still fine
    assert run(tmp_path, ["--dry-run", "hi"], fake=fake,
               extra={"FAKE_NO_RESTRICTED": "1"}).returncode == 0


def test_test_flag_outside_a_repo_is_a_usage_error(tmp_path, server, fake):
    r = run(tmp_path, ["--test", "-C", posix(tmp_path), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2
    assert "git repository" in r.stderr


def test_test_flag_refuses_detach(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "-w", "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2


def test_test_flag_needs_a_test_command(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "-C", posix(repo), "hi"], server, fake)
    assert r.returncode == 2
    assert "QWEN_TEST_CMD" in r.stderr


@pytest.mark.parametrize("cmd", ["", " ", "\t ", " \n "], ids=["empty", "space", "tab", "newline"])
def test_test_refuses_whitespace_test_cmd(tmp_path, fake, cmd):
    # Whitespace-only survives a bare -n test but splits into nothing, so every
    # qwen-test in the run would die; qwen-agent must read it as unset, the way
    # qwen-sweep's guard already does.
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake, extra={"QWEN_TEST_CMD": cmd})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "QWEN_TEST_CMD" in r.stderr
    # a real command is still accepted
    r = run(tmp_path, ["--dry-run", "--test", "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 0, r.stderr


def test_coder_role_implies_write(tmp_path, server, fake):
    r = run(tmp_path, ["-r", "coder", "hi"], server, fake)
    assert r.returncode == 0, r.stderr
    argv, _ = record(tmp_path)
    assert "Edit" in flag(argv, "--tools")


def test_test_flag_refuses_all_tools(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", "--all-tools", "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2
    assert "--all-tools" in r.stderr


@pytest.mark.parametrize("args", [
    ["-t", "Bash(qwen-test:*)"],
    ["--tools", "Bash(ls:*)"],
    ["--tools=Bash(ls:*)"],
], ids=["short", "long", "equals"])
def test_test_refuses_explicit_tool_grants(tmp_path, fake, args):
    # An explicit --allowed-tools list REPLACES the qwen-test-only grants, so
    # `--test -t 'Bash(*)'` would hand an unrestricted shell to a dontAsk run.
    # The fence cannot be widened from the command line.
    r = run(tmp_path, ["--dry-run", "--test", *args, "hi"], fake=fake, extra={"QWEN_TEST_CMD": "true"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--tools" in r.stderr                      # the message names the flag
    assert not (tmp_path / "record.txt").exists()     # claude never ran
    # without --test, the grant list is still the caller's to choose
    assert run(tmp_path, ["--dry-run", *args, "hi"], fake=fake).returncode == 0


# ------------------------------------------------------------------ --web

def test_no_web_tools_by_default(tmp_path, server, fake):
    # Benchmark finding turned into policy: no run gets the web unless --web
    # asks for it. Every role, plain and --write runs, with and without --test.
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")}
    for role in ([], ["-r", "auditor"], ["-r", "coder"], ["-r", "mechanic"],
                 ["-r", "plain"], ["--write"]):
        for args in (role, ["--test", *role]):
            r = run(tmp_path, [*args, "-C", posix(repo), "hi"], server, fake, extra=extra)
            assert r.returncode == 0, "%s: %s" % (args, r.stderr)
            argv, _ = record(tmp_path)
            assert not [a for a in argv if "WebFetch" in a or "WebSearch" in a], args


def test_web_adds_only_webfetch(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")}
    for args in (["--web"], ["--web", "--write"], ["--web", "-r", "coder"],
                 ["--web", "--test"], ["--test", "-r", "coder", "--web"]):
        r = run(tmp_path, [*args, "-C", posix(repo), "hi"], server, fake, extra=extra)
        assert r.returncode == 0, "%s: %s" % (args, r.stderr)
        argv, _ = record(tmp_path)
        assert "WebFetch" in flag(argv, "--tools").split(","), args
        assert "WebFetch" in flag(argv, "--allowed-tools").split(","), args
        # never WebSearch: Claude Code sends it as a server-side tool and vLLM
        # rejects the request outright (body.tools.0.input_schema Field required)
        assert not [a for a in argv if "WebSearch" in a], args
    # the env form does the same
    assert run(tmp_path, ["-C", posix(repo), "hi"], server, fake,
               extra=dict(extra, QWEN_WEB="1")).returncode == 0
    argv, _ = record(tmp_path)
    assert "WebFetch" in flag(argv, "--tools").split(",")
    # --web does not widen the fixed --test grants beyond WebFetch
    assert run(tmp_path, ["--test", "--web", "-C", posix(repo), "hi"], server, fake,
               extra=extra).returncode == 0
    grants = flag(record(tmp_path)[0], "--allowed-tools").split(",")
    assert "Bash(qwen-test:*)" in grants and "WebFetch" in grants
    # --all-tools keeps today's behaviour: already unrestricted, no --tools at all
    assert run(tmp_path, ["--web", "--all-tools", "-C", posix(repo), "hi"], server, fake,
               extra=extra).returncode == 0
    assert flag(record(tmp_path)[0], "--tools") is None


def test_web_with_test_warns(tmp_path, server, fake):
    # Tests and checks can be gamed by fetching upstream answers -- the run
    # proceeds, but it says so, and -q must not hide it.
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
             "QWEN_WEB": "1"}
    r = run(tmp_path, ["--test", "-C", posix(repo), "hi"], server, fake, extra=extra)
    assert r.returncode == 0, r.stderr
    # the spec fixes the LINE to start with the flag pair, not "qwen-agent: WARNING: ..."
    assert any(ln.startswith("WARNING: --web with --test") for ln in r.stderr.splitlines())
    assert "gamed" in r.stderr and "upstream" in r.stderr
    # without --test there is no warning (nothing to game), web still on
    r = run(tmp_path, ["-C", posix(repo), "hi"], server, fake, extra=extra)
    assert r.returncode == 0 and "--web with --test" not in r.stderr
    assert "WebFetch" in flag(record(tmp_path)[0], "--tools").split(",")


def _repro_run(tmp_path, server, fake, args):
    repo = _git_repo(tmp_path / "repo")
    r = run(tmp_path, ["--test", *args, "-C", posix(repo), "hi"], server, fake,
            extra={"QWEN_TEST_CMD": "true", "FAKE_MODE": "repro",
                   "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    return repo, r


def test_repro_files_listed_for_read_only_run(tmp_path, server, fake):
    repo, r = _repro_run(tmp_path, server, fake, [])
    assert r.returncode == 0, r.stderr
    assert "## REPRO FILES" in r.stdout, r.stdout + r.stderr
    assert "### test_repro.py" in r.stdout
    assert "assert False" in r.stdout
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        check=True, capture_output=True, text=True)
    assert st.stdout.strip() == ""


def test_repro_files_absent_with_json(tmp_path, server, fake):
    _, r = _repro_run(tmp_path, server, fake, ["--json"])
    assert r.returncode == 0, r.stderr
    assert "REPRO FILES" not in r.stdout
    json.loads(r.stdout)


def test_repro_files_absent_in_write_mode(tmp_path, server, fake):
    _, r = _repro_run(tmp_path, server, fake, ["-r", "coder"])
    assert r.returncode == 0, r.stderr
    assert "REPRO FILES" not in r.stdout


FAKE_SCRIPTED = r'''#!/usr/bin/env bash
# Each call: record argv, then print the next scripted JSON result.
if [ "${1:-}" = --help ]; then echo "  --restricted  Restricted mode"; exit 0; fi
for a in "$@"; do printf 'ARG:%s\n' "$a"; done >> "$FAKE_RECORD"
printf -- '---\n' >> "$FAKE_RECORD"
n=$(cat "$FAKE_RECORD.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_RECORD.n"
if [ "$n" -ge 1 ]; then printf good > "$PWD/value.txt"; fi
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"worked","session_id":"sess-9","usage":{"input_tokens":5,"output_tokens":1},"permission_denials":[]}'
'''


def test_until_done_without_audit_is_done(tmp_path, server):
    fake2 = tmp_path / "fake2"
    fake2.write_text(FAKE_SCRIPTED, encoding="utf-8", newline="\n"); fake2.chmod(0o755)
    repo = _git_repo(tmp_path / "repo")
    (repo / "value.txt").write_text("bad")
    (repo / "check.py").write_text("import sys\nsys.exit(0 if open('value.txt').read()=='good' else 1)\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "v"], check=True, capture_output=True)
    task = tmp_path / "task.md"
    task.write_text("- [ ] value good -- check: test ALL\n")
    py = shutil.which("python3") or shutil.which("python")
    r = run(tmp_path, ["--until-done", posix(task), "-C", posix(repo), "--no-deviation-audit"], server, fake2,
            extra={"QWEN_TEST_CMD": "%s check.py" % posix(py),
                   "QWEN_AGENT_STATE": posix(tmp_path / "state"),
                   "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
                   "QWEN_SUPERVISOR_BACKOFF": "0"}, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = (tmp_path / "record.txt").read_text()
    assert "ARG:--resume\nARG:sess-9" in rec        # round 2 resumed round 1's session


def test_until_done_reads_test_cmd_from_config(tmp_path, server, fake):
    # The supervisor is exec'd, so it only sees what qwen-agent.sh EXPORTED.
    # With QWEN_TEST_CMD set only in the config file and absent from the
    # environment, an un-exported value makes the run refuse ("not set").
    py = posix(shutil.which("python3") or shutil.which("python"))
    cfg = tmp_path / "config"
    cfg.write_text('QWEN_TEST_CMD=\'%s -c "pass"\'\n' % py, encoding="utf-8", newline="\n")
    repo = _git_repo(tmp_path / "repo")
    task = tmp_path / "task.md"
    task.write_text("- [ ] value good -- check: test ALL\n")
    r = run(tmp_path, ["--until-done", posix(task), "-C", posix(repo), "--no-deviation-audit"],
            server, fake,
            extra={"QWEN_CONFIG": posix(cfg), "QWEN_AGENT_STATE": posix(tmp_path / "state"),
                   "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")})
    assert r.returncode == 0, r.stdout + r.stderr


def _ud(tmp_path, args, extra=None):
    return run(tmp_path, ["--until-done", "t.md", *args], None, None,
               extra=dict({"QWEN_PREFLIGHT": "0"}, **(extra or {})))


@pytest.mark.parametrize("args,needle", [
    (["fix it"], "prompt"),
    (["--", "fix it"], "prompt"),
    (["-f", "x.md"], "-f"),
    (["--prompt-file=x.md"], "--prompt-file"),
    (["--json"], "--json"),
    (["--resume", "abc"], "--resume"),
    (["-o", "out.md"], "-o"),
    (["-r", "auditor"], "coder"),
    (["--role=auditor"], "coder"),
])
def test_until_done_refuses_round_owned_options(tmp_path, args, needle):
    r = _ud(tmp_path, args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr


@pytest.mark.parametrize("args,needle", [
    (["--toolset", "Read,Bash"], "--toolset"),
    (["--toolset=Read,Bash"], "--toolset"),
    (["--read-only"], "--read-only"),
    (["--all-tools"], "--all-tools"),
    (["--permission-mode", "acceptEdits"], "--permission-mode"),
    (["--permission-mode=acceptEdits"], "--permission-mode"),
])
def test_until_done_refuses_fence_flags(tmp_path, args, needle):
    # Every round runs --test, and --test owns the tool policy; these collide
    # with the fence, so they must die here, not at each round's qwen-agent.
    r = _ud(tmp_path, args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "record.txt").exists()       # nothing ran


@pytest.mark.parametrize("args,needle", [
    (["-t", "Bash(*)"], "-t"),
    (["--tools", "Bash(*)"], "--tools"),
    (["--tools=Bash(*)"], "--tools"),
    (["--unrestricted"], "--unrestricted"),
    (["-D", "/"], "-D"),
    (["--add-dir", "/"], "--add-dir"),
    (["--add-dir=/"], "--add-dir"),
], ids=["t", "tools", "tools-eq", "unrestricted", "D", "add-dir", "add-dir-eq"])
def test_until_done_refuses_grant_and_dir_flags(tmp_path, args, needle):
    # -t/--tools and --unrestricted collide with every round's --test fence, and
    # -D/--add-dir would widen the coder's file access past its own tree --
    # -D / reaches the whole disk. Refuse them all before the first round.
    r = _ud(tmp_path, args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "record.txt").exists()       # nothing ran


@pytest.mark.parametrize("args", [
    ["--role-file", "role.md"],
    ["--role-file=role.md"],
])
def test_until_done_refuses_role_file(tmp_path, args):
    # --role-file replaces the coder role every round runs with; a custom file
    # carries no coder instructions, so the rounds silently turn read-only and
    # the loop "codes" without ever writing. Refuse it up front.
    r = _ud(tmp_path, args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--role-file" in r.stderr
    assert not (tmp_path / "record.txt").exists()       # nothing ran


def test_until_done_forwards_filtered_options(tmp_path):
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\n[ "$1" = -c ] && exit 0\n'
                      'for a in "$@"; do printf "ARG:%s\\n" "$a"; done > "$FAKE_RECORD"\n',
                      encoding="utf-8", newline="\n")
    fakepy.chmod(0o755)
    (tmp_path / "sub").mkdir()
    r = run(tmp_path, ["--until-done", "t.md", "-C", "sub", "--max-rounds=3", "--model", "a b",
                       "--test", "-r", "coder"], None, None,
            extra={"QWEN_PYTHON": posix(fakepy)})
    assert r.returncode == 0, r.stdout + r.stderr
    argv, _ = record(tmp_path)
    assert same_path(flag(argv, "--repo")) == same_path(tmp_path / "sub")
    assert argv.count("--max-rounds") + sum(a.startswith("--max-rounds=") for a in argv) == 1
    assert argv[argv.index("--") + 1:] == ["--model", "a b"]


def test_no_subagents_by_default(tmp_path, server, fake):
    # Subagents are opt-in: on a small GPU every subagent is one more concurrent request.
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")}
    for args in ([], ["--write"], ["-r", "coder"], ["-r", "auditor"], ["--test"], ["--test", "-r", "coder"]):
        r = run(tmp_path, [*args, "-C", posix(repo), "hi"], server, fake, extra=extra)
        assert r.returncode == 0, "%s: %s" % (args, r.stderr)
        argv, _ = record(tmp_path)
        assert "Task" not in (flag(argv, "--tools") or "").split(","), args
        assert "Task" not in (flag(argv, "--allowed-tools") or "").split(","), args


def test_subagents_flag_adds_task_and_keeps_the_fence(tmp_path, server, fake):
    repo = _git_repo(tmp_path / "repo")
    extra = {"QWEN_TEST_CMD": "true", "QWEN_TEST_WORKTREES": posix(tmp_path / "wts")}
    for args in (["--subagents"], ["--subagents", "-r", "coder"], ["--test", "-r", "coder", "--subagents"]):
        r = run(tmp_path, [*args, "-C", posix(repo), "hi"], server, fake, extra=extra)
        assert r.returncode == 0, "%s: %s" % (args, r.stderr)
        argv, _ = record(tmp_path)
        assert "Task" in flag(argv, "--tools").split(","), args
        assert "Task" in flag(argv, "--allowed-tools").split(","), args
    # under --test the shell grant is still qwen-test only, and no web tool rides along
    grants = flag(argv, "--allowed-tools").split(",")
    assert [g for g in grants if g.startswith("Bash")] == ["Bash(qwen-test:*)"]
    assert "WebFetch" not in grants
    # the env form does the same
    assert run(tmp_path, ["-C", posix(repo), "hi"], server, fake,
               extra=dict(extra, QWEN_SUBAGENTS="1")).returncode == 0
    assert "Task" in flag(record(tmp_path)[0], "--tools").split(",")


def test_subagents_note_only_when_enabled(tmp_path, server):
    note = "delegate broad reading and searching"
    assert note in (_sys_prompt(tmp_path, server, ["--subagents", "-r", "coder", "hi"]) or "")
    assert note not in (_sys_prompt(tmp_path, server, ["-r", "coder", "hi"]) or "")


# ------------------------------------------------------------- --interactive

def test_interactive_runs_claude_without_print(tmp_path, server, fake):
    parent = {"CLAUDE_EFFORT": "high", "CLAUDE_CODE_USE_BEDROCK": "1",
              "ANTHROPIC_API_KEY": "sk-real-key", "AWS_BEARER_TOKEN_BEDROCK": "aws-token"}
    r = run(tmp_path, ["--interactive"], server, fake, extra=parent)
    assert r.returncode == 0, r.stdout + r.stderr
    argv, env = record(tmp_path)
    assert "-p" not in argv and "--print" not in argv
    assert flag(argv, "--model") == "local-model"
    assert flag(argv, "--effort") == "medium"
    # the same child environment a headless run gets: this server, every tier
    assert env["ANTHROPIC_BASE_URL"] == base_url(server)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    for k in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert env[k] == "local-model", k
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
    # ...and still without the parent session's credentials or provider routing
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_EFFORT", "CLAUDE_CODE_USE_BEDROCK",
              "AWS_BEARER_TOKEN_BEDROCK"):
        assert k not in env, k
    # the child's own output is the session: nothing parsed or re-emitted it
    assert "fake answer" in r.stdout
    # --effort is passed the same way a headless run passes it
    assert run(tmp_path, ["-e", "xhigh", "--interactive"], server, fake).returncode == 0
    argv, env = record(tmp_path)
    assert flag(argv, "--effort") == "xhigh" and env["CLAUDE_CODE_EFFORT_LEVEL"] == "xhigh"


def test_interactive_passes_no_fence_flags(tmp_path, server, fake):
    # The person at the keyboard answers Claude Code's normal permission prompts,
    # so an interactive claude must be a plain one: no tool list, no permission
    # mode, no JSON output format, no appended system prompt, no --restricted.
    fence = ("--tools", "--allowed-tools", "--restricted", "--permission-mode",
             "--output-format", "--append-system-prompt", "--strict-mcp-config")
    for extra in ({}, {"QWEN_SETTING_SOURCES": "project,local"}):
        r = run(tmp_path, ["--interactive"], server, fake, extra=extra)
        assert r.returncode == 0, r.stdout + r.stderr
        argv, _ = record(tmp_path)
        for f in fence:
            assert f not in argv, f
    # a headless run keeps the whole fence; the modes must not drift together
    assert run(tmp_path, ["-r", "auditor", "hi"], server, fake).returncode == 0
    argv, _ = record(tmp_path)
    for f in ("--tools", "--allowed-tools", "--output-format", "--strict-mcp-config",
              "--append-system-prompt"):
        assert f in argv, f


@pytest.mark.parametrize("args,needle", [
    (["hi"], "prompt"),
    (["--", "hi"], "prompt"),
    (["-f", "task.md"], "-f"),
    (["--prompt-file=task.md"], "--prompt-file"),
    (["--stdin"], "--stdin"),
    (["--until-done", "task.md"], "--until-done"),
    (["--test"], "--test"),
    (["--write"], "--write"),
    (["--all-tools"], "--all-tools"),
    (["--unrestricted"], "--unrestricted"),
    (["--toolset", "Read,Bash"], "--toolset"),
    (["--toolset=Read"], "--toolset"),
    (["-t", "Bash(ls:*)"], "-t"),
    (["--tools", "Bash(ls:*)"], "--tools"),
    (["--tools=Bash(ls:*)"], "--tools"),
    (["--web"], "--web"),
    (["--subagents"], "--subagents"),
    (["--json"], "--json"),
    (["-o", "out.md"], "-o"),
    (["--out=out.md"], "--out"),
    (["-w"], "-w"),
    (["--detach"], "--detach"),
    (["--bg"], "--bg"),
    (["--resume", "abc"], "--resume"),
    (["-r", "coder"], "-r"),
    (["--role=coder"], "--role"),
    (["--role-file", "role.md"], "--role-file"),
    (["-s", "extra text"], "-s"),
    (["--read-only"], "--read-only"),
    (["--strict-mcp"], "--strict-mcp"),
    (["--permission-mode", "plan"], "--permission-mode"),
    (["--permission-mode=plan"], "--permission-mode"),
    (["--test-repo", "."], "--test-repo"),
    (["--max-rounds", "3"], "--max-rounds"),
    (["--budget-seconds=60"], "--budget-seconds"),
    (["--allow-dirty"], "--allow-dirty"),
    (["--warn-denials"], "--warn-denials"),
], ids=["prompt", "prompt-after-dash", "f", "prompt-file-eq", "stdin", "until-done", "test",
        "write", "all-tools", "unrestricted", "toolset", "toolset-eq", "t", "tools", "tools-eq",
        "web", "subagents", "json", "o", "out-eq", "w", "detach", "bg", "resume", "r", "role-eq",
        "role-file", "s", "read-only", "strict-mcp", "permission-mode", "permission-mode-eq",
        "test-repo", "max-rounds", "budget-seconds-eq", "allow-dirty", "warn-denials"])
def test_interactive_refuses_incompatible_flags(tmp_path, server, fake, args, needle):
    # Every one of these shapes a HEADLESS run: a tool fence, a fixed permission
    # mode, a role prompt, one result file, a background job. In an interactive
    # session there is nothing to pass them to, so say so instead of ignoring.
    r = run(tmp_path, ["--interactive", *args], server, fake)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--interactive" in r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "record.txt").exists()       # claude never ran


def test_interactive_dry_run(tmp_path, fake):
    work = tmp_path / "work"
    work.mkdir()
    r = run(tmp_path, ["--interactive", "--dry-run", "-m", "svc-model", "-e", "xhigh",
                       "-C", posix(work)], fake=fake,
            extra={"QWEN_API_KEY": "sekrit-123", "QWEN_CUSTOM_HEADERS": "X-Token: hdr-456"})
    assert r.returncode == 0, r.stdout + r.stderr
    argv = _dry_argv(r)
    assert argv[1:] == ["--model", "svc-model", "--effort", "xhigh"]      # no -p, nothing else
    assert "-p" not in argv and "--print" not in argv
    for f in ("--tools", "--allowed-tools", "--restricted", "--permission-mode",
              "--output-format", "--append-system-prompt", "--strict-mcp-config"):
        assert f not in argv, f
    # the child environment is shown, with the secret redacted
    assert "# env" in r.stdout
    assert "  ANTHROPIC_BASE_URL=" in r.stdout
    assert "  ANTHROPIC_MODEL=svc-model" in r.stdout
    assert "  CLAUDE_CODE_EFFORT_LEVEL=xhigh" in r.stdout
    assert "  ANTHROPIC_AUTH_TOKEN=<redacted>" in r.stdout
    assert "sekrit-123" not in r.stdout and "hdr-456" not in r.stdout
    assert "# unset for the child:" in r.stdout
    # -C is the directory the session runs in
    printed = [ln for ln in r.stdout.splitlines() if ln.startswith("# cwd: ")][0][len("# cwd: "):]
    assert printed.rstrip("/").endswith("work"), printed
    assert posix(printed).rstrip("/") != posix(tmp_path).rstrip("/")
    assert "# timeout: not applied" in r.stdout
    assert not (tmp_path / "record.txt").exists()       # nothing was run
