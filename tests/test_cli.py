"""qwen-agent end to end, offline: a fake /v1/models server and a fake `claude`.

These run the real shell script under the bash named by $TEST_BASH (default: the
first `bash` on PATH), which is how CI exercises macOS's stock bash 3.2 and Git
Bash on Windows as well as Linux.
"""
import http.server
import json
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
{
  for a in "$@"; do printf 'ARG:%s\n' "$a"; done
  env | grep -E '^(ANTHROPIC_|CLAUDE_|AWS_BEARER)' | sort
} > "$FAKE_RECORD"
ok='{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":5,"result":"fake answer","usage":{"input_tokens":10,"output_tokens":2},"permission_denials":[]}'
case "${FAKE_MODE:-ok}" in
  ok)      printf '%s\n' "$ok" ;;
  unicode) printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"“quoted” → ❌ 完了"}' ;;
  error)   printf '%s\n' '{"type":"result","subtype":"error_max_turns","is_error":true,"terminal_reason":"max_turns","result":"gave up"}' ;;
  apierr)  printf '%s\n' '{"type":"result","is_error":true,"api_error_status":400,"result":"API Error: 400"}' ;;
  empty)   printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":""}' ;;
  denied)  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":2,"result":"partial","permission_denials":[{"tool_name":"Bash"}]}' ;;
  garbage) printf 'this is not json\n' ;;
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
