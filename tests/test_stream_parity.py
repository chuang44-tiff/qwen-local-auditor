"""Launch parity: streamed or not, `claude` must be asked the SAME thing.

Streaming is going to become the only launch path, so this file pins what the old
argv launch handed the model against what the driver hands it, on the three things
qwen-agent.sh actually controls (measured against a real Claude Code through a
logging proxy on 2026-11-20: the server received an identical system prompt, tools,
task message and round-2 prompt in both modes):

  (1) the `claude` argv, (2) the prompt text, (3) the environment `claude` runs in.

Both modes run the REAL qwen-agent.sh over tests/fake_claude_record.py, which
witnesses argv / stdin_user_texts / env per launch the way tests/fake_claude_stream.py
witnesses the driver's lines -- and the way tests/test_messaging_wiring.py witnesses
launches through its WRAP fake.

The normalisation is deliberately exact, not a loose subset check: the streamed
launch's argv has to be the argv-mode argv with the trailing `-- <prompt>` pair gone,
the `--output-format json` pair gone from its slot (the driver re-adds
`--output-format stream-json` inside the tail it appends), and the driver's four
flags appended last in their one order. Equality of two lists is what says "no other
flag added, removed or reordered".

Phase 2 added one KNOWN mode difference (spec 12.7): the streamed launch also carries
the qla_msg MCP config pair, the reply grant and the two env keys the launcher
exports for the server. shared_core strips them for the equality (strip_qla_msg),
golden_payload leaves the keys out of the pinned env list -- and their presence on
the streamed side, absence on the unstreamed one, is asserted plainly by
test_parity_single_shot_argv, so stripping cannot hide them.

The golden (tests/data/stream_launch_golden.json) is that same normalised stream
argv plus the sorted list of the env keys the launcher itself contributes, with
placeholders for the values that name a run rather than a launch. It keeps its
meaning after the argv path is deleted: it is what the launch looked like, checked in.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from test_cli import base_url, posix, run, server            # noqa: F401 (server: the fixture)
from test_messaging_wiring import inbox_dir_for, pointer_dir, wait_for, watch_cache

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_RECORD = ROOT / "tests" / "fake_claude_record.py"
GOLDEN = ROOT / "tests" / "data" / "stream_launch_golden.json"

FAKE_SESSION = "fake-record-session-1"
RECORD_ANSWER = "record answer"
PROMPT = "audit the login flow for missing checks"
REGENERATE = "QLA_UPDATE_GOLDEN=1 pytest -k golden"

# The driver's own four flags, in the one order lib/stream_driver.py appends them
# (its STREAM_FLAGS). Nothing else may be in the tail, and none of it may show up in
# an unstreamed launch.
DRIVER_TAIL = ["--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--replay-user-messages"]

# Phase 2 (spec 12.7): the streamed launch legitimately carries what the argv launch
# must never see -- the qla_msg MCP config (as one more --mcp-config pair) and the
# reply grant, plus the two env keys the launcher exports for the server. These are
# THE mode difference: shared_core strips them from both sides (a config file under
# TMPD names the run, so it could never be a verbatim pin), the comparison test
# asserts presence on the streamed side and absence on the unstreamed one, and
# golden_payload's env-key list leaves them out for the same presence-assert to
# guard. QWEN_MSG_* are not in SCRUB_LIST (spec 4.1).
QLA_MSG_CONFIG_FILE = "qla_msg.json"
QLA_MSG_GRANT = ",mcp__qla_msg__reply"
QLA_MSG_ENV_KEYS = ("QWEN_MSG_DIR", "QWEN_MSG_TOOLS")

# argv flags whose VALUE names this run (or this machine's served model) rather than
# this kind of launch, and what the golden writes in their place. The order matters:
# --append-system-prompt goes first, so a token that appears inside the role's own
# text is never mistaken for a real flag.
VALUE_PLACEHOLDERS = (
    ("--append-system-prompt", "<APPENDED-SYSTEM-PROMPT>"),   # the role's whole text
    ("--model", "<MODEL>"),                                   # the server's one model
    ("--settings", "<PER-RUN-SETTINGS-FILE>"),                # mktemp'd hook settings
)
RUN_ID_PLACEHOLDER = "<RUN-ID>"
# Of those three, the one whose value CANNOT be equal between the two launches: it
# names the mktemp folder this run made for its hook settings. Both launches must
# still pass the flag, and name the same FILE -- only the folder may differ. Every
# other placeholdered value is compared verbatim across the modes by
# assert_same_run_values, before normalisation replaces it.
SHAPE_ONLY_VALUES = {"--settings": "settings.json"}
# What may never survive normalisation: the launcher's own mktemp prefixes (a path
# from mktemp names one run) and anything the test handed the launcher. A token that
# still holds one of these has no placeholder rule, and failing here beats pinning
# one machine's temporary directory into a checked-in golden.
LEAK_NEEDLES = ("/qwen-watch.", "/qwen-agent.", "/qwen-plan.", "/qwen-advisor.",
                "/qwen-agent/watch/")     # the registry folder (lib/watch.py:167) that
                                          # holds a run's pointer, its activity file and
                                          # its INBOX (qwen-agent.sh:3333-3334). The last
                                          # is the driver's own argument and must never
                                          # reach claude's argv; the hook settings live
                                          # under /qwen-watch. instead (3411-3415)

# The environment keys whose VALUE is this run's path or id: identical key sets, and
# identical values for every other key, is what parity means here. Each one names the
# watch registration the launcher made, which no two runs share.
PER_RUN_ENV_KEYS = {
    "QWEN_WATCH_ID": "the 12-hex id this run's registration was handed",
    "QWEN_WATCH_ACTIVITY": "that run's activity file -- its name carries the id",
    "QWEN_WATCH_PARENT": "the pid of the shell that registered the run",
}
# Two keys that are not per-run paths but cannot be equal across the two launches by
# construction: the first is the switch the TEST sets to choose the launch, the
# second bash's own marker for the last word of the exec line -- and the two exec
# lines end on different words, since the driver's argv stops where the prompt began.
MODE_ENV_KEYS = {
    "QWEN_STREAM": "the mode switch itself: its value IS the thing under test",
    "_": "bash's last-word marker: the last token of the exec line differs by mode",
}
# The env keys a checked-in golden can pin: the ones the launcher (or this suite's
# harness) puts there. The rest -- PATH, HOME, LANG, TERM, ... -- belong to whoever
# ran pytest, and pinning them would make the golden fail for a reason that has
# nothing to do with what claude is asked.
OWN_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "QWEN_", "QA_", "FAKE_")
OWN_ENV_KEYS_EXTRA = ("PYTHONUTF8",)     # qwen-agent.sh exports it for the child


# ---------------------------------------------------------------- the doubles

def record_fake(tmp_path):
    """QWEN_CLAUDE_BIN: a two-line launcher for tests/fake_claude_record.py, run with
    THIS interpreter the way test_messaging_wiring's WRAP runs its stream fake."""
    wrap = tmp_path / "fake-claude-record"
    wrap.write_text('#!/usr/bin/env bash\nexec "%s" "%s" "$@"\n'
                    % (posix(sys.executable), posix(FAKE_RECORD)),
                    encoding="utf-8", newline="\n")
    wrap.chmod(0o755)
    return wrap


def records_dir(tmp_path):
    d = tmp_path / "records"
    d.mkdir(exist_ok=True)
    return d


def read_record(records, n):
    """The nth launch's witness. The double renames it into place, so a file that
    exists is a file that is whole."""
    path = records / ("%d.json" % n)
    wait_for(lambda: path.exists(), timeout=120,
             message="the fake never recorded launch %d" % n)
    return json.loads(path.read_text(encoding="utf-8"))


def launched(records, names):
    """Every launch the double witnessed, and nothing else: a stray claude call (an
    advisor, a retry, a second driver) between two rounds would shift every number
    this file compares, so the count is pinned, not assumed."""
    wait_for(lambda: sorted(p.name for p in records.glob("*.json")) == list(names),
             timeout=30, message="the launches witnessed are %r, not %r"
             % (sorted(p.name for p in records.glob("*.json")), list(names)))


def record_env(tmp_path, stream, flag_file=None):
    """The knobs the double reads -- identical for both modes of one test, because
    they are part of the environment parity compares. One records folder per TEST:
    the launches of the two modes number from 1 in sequence across it.

    The flag file is the until-done check's pass word: a path INSIDE the repo (the
    check runs with cwd=the repo), and never written by a single-shot -- no launch
    there resumes a session, which is what makes the double write it."""
    return {"QWEN_CLAUDE_BIN": posix(record_fake(tmp_path)),
            "QWEN_STREAM": "1" if stream else "0",
            "QWEN_NOTE_POLL": "0.2",
            "FAKE_RECORD_DIR": posix(records_dir(tmp_path)),
            "FAKE_RECORD_SESSION": FAKE_SESSION,
            "FAKE_RECORD_RESULT": RECORD_ANSWER,
            "FAKE_RECORD_FLAG_FILE": posix(flag_file or tmp_path / "no-launch-asks-for-this")}


def _pointers(tmp_path):
    d = pointer_dir(tmp_path)
    return {p.stem for p in d.glob("*.json")} if d.is_dir() else set()


def new_pointer(tmp_path, before):
    """The watch id of the run that has just registered (a parity test runs both
    modes, so the registry already holds the other run's pointer)."""
    box = {}

    def pred():
        fresh = _pointers(tmp_path) - before
        if not fresh:
            return False
        # one launch, one registration. Picking one of two silently would aim every
        # later assert at whichever id sorted first, not at the run that launched.
        assert len(fresh) == 1, "two runs registered from one launch: %r" % sorted(fresh)
        box["v"] = sorted(fresh)[0]
        return True
    wait_for(pred, timeout=90, message="the run never registered a watch pointer")
    return box["v"]


# ------------------------------------------------------------- normalisation

def drop_output_format(tokens, value, side):
    """Remove the one `--output-format <value>` pair: `json` is what the argv launch
    passes and the driver strips out of its slot, `stream-json` is the one the driver
    appends in its tail. Exactly one, always followed by that value."""
    at = [i for i, a in enumerate(tokens) if a == "--output-format"]
    assert len(at) == 1, "%s: expected exactly one --output-format in %r" % (side, tokens)
    i = at[0]
    assert i + 1 < len(tokens) and tokens[i + 1] == value, \
        "%s: --output-format is not followed by %r: %r" % (side, value, tokens)
    return tokens[:i] + tokens[i + 2:]


def last_segment(path):
    """The file a path names, whichever separator wrote it."""
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def run_values(tokens, side):
    """The RAW values of the flags the golden replaces, read off a launch before
    normalisation hides them.

    Placeholdering is what lets a golden survive two runs of the same command; it is
    also what could let a difference slip past an equality that only sees
    placeholders. So the values behind the placeholders are compared between the
    launches first, where they can be.
    """
    out = {}
    for flag, _placeholder in VALUE_PLACEHOLDERS:
        at = [i for i, a in enumerate(tokens) if a == flag]
        assert len(at) <= 1, "%s: %s appears %d times in one launch: %r" % (side, flag, len(at), tokens)
        if at:
            i = at[0]
            assert i + 1 < len(tokens), "%s: %s was passed with no value: %r" % (side, flag, tokens)
            out[flag] = tokens[i + 1]
    return out


def value_mismatch(flag, a, s):
    """Where the two launches stop agreeing, said in 120 characters rather than in the
    several kilobytes of role text that usually differ. Both values exist: the caller
    has just required the two launches to pass the same value flags."""
    at = next((n for n, (x, y) in enumerate(zip(a, s)) if x != y), min(len(a), len(s)))
    return ("%s: the two launches pass different values (%d vs %d characters, the first"
            " difference at character %d)\n  argv-mode: %r\n  streamed:  %r"
            % (flag, len(a), len(s), at, a[max(0, at - 60):at + 60], s[max(0, at - 60):at + 60]))


def assert_same_run_values(argv, streamed):
    """Compare what normalisation replaces -- so that no placeholdered value, above
    all the system prompt the model is sent, can differ between the two launches and
    still read as parity. `--settings` is the single exception, and only in its
    folder: both sides must name the same file (SHAPE_ONLY_VALUES)."""
    va, vs = run_values(argv, "argv-mode"), run_values(streamed, "streamed")
    assert set(va) == set(vs), \
        "the launches pass different value flags: %r vs %r" % (sorted(va), sorted(vs))
    for flag in sorted(va):
        a, s = va[flag], vs[flag]
        if flag in SHAPE_ONLY_VALUES:
            want = SHAPE_ONLY_VALUES[flag]
            assert last_segment(a) == want and last_segment(s) == want, \
                "%s: one launch names another file (argv-mode %r, streamed %r); both " \
                "must be the run's own %s" % (flag, a, s, want)
        else:
            assert a == s, value_mismatch(flag, a, s)


AUDITOR_ROLE_LINE = "You are operating as a CODE AUDITOR"   # qwen-agent.sh:879


def assert_asks_the_run_asked(tokens, model="local-model", role_line=AUDITOR_ROLE_LINE):
    """The two placeholders that name nothing on their own, pinned on the launch
    itself: which model the server is asked for, and the role text the run says it is
    -- both on the streamed path as much as on the old one, and so still pinned after
    the argv launch is deleted and only the golden is left."""
    values = run_values(tokens, "the launch under test")
    assert values.get("--model") == model, \
        "the launch asks model %r, not %r" % (values.get("--model"), model)
    prompt = values.get("--append-system-prompt") or ""
    assert role_line in prompt, \
        "the launch sends no auditor role text: its --append-system-prompt is %d " \
        "characters and does not contain %r" % (len(prompt), role_line)


def placeholderise(tokens, run_id, per_run, side):
    """Reduce an argv to what a checked-in golden can hold. Whatever this replaces
    has already been compared between the launches (assert_same_run_values); a value
    compared nowhere is a value pinned by nobody."""
    out = list(tokens)
    for flag, placeholder in VALUE_PLACEHOLDERS:
        if flag in out:
            i = out.index(flag)
            assert i + 1 < len(out), "%s: %s was passed with no value: %r" % (side, flag, out)
            out[i + 1] = placeholder
    if run_id:
        out = [RUN_ID_PLACEHOLDER if tok == run_id else tok for tok in out]
    for needle in [str(p) for p in per_run if str(p)] + list(LEAK_NEEDLES) \
            + ([run_id] if run_id else []):
        for tok in out:
            assert needle not in tok, \
                "%s launch: %r names this run and placeholderise has no rule for it" \
                % (side, needle)
    return out


def strip_qla_msg(tokens, side):
    """Remove the qla_msg additions from a launch's tokens BEFORE placeholdering
    and the leak check: the --mcp-config pair naming the run's TMPD file, and the
    reply-grant suffix on --allowed-tools (phase 2, spec 12.7 -- the streamed
    launch's legitimate extras, shared_core's one known difference). Stripping
    happens on both modes: an unstreamed launch has nothing to strip, and its
    carrying the pair would be the real bug -- caught by the presence/absence
    asserts in test_parity_single_shot_argv, not by silence here. A second pair
    or a second --allowed-tools is not the known difference and says so."""
    out = list(tokens)
    at = [i for i, a in enumerate(out)
          if a == "--mcp-config" and i + 1 < len(out)
          and last_segment(out[i + 1]) == QLA_MSG_CONFIG_FILE]
    assert len(at) <= 1, "%s: %d qla_msg --mcp-config pairs in one launch: %r" \
        % (side, len(at), tokens)
    for i in reversed(at):
        del out[i:i + 2]
    g = [i for i, a in enumerate(out) if a == "--allowed-tools"]
    assert len(g) <= 1, "%s: %d --allowed-tools in one launch: %r" % (side, len(g), tokens)
    if g and out[g[0] + 1].endswith(QLA_MSG_GRANT):
        out[g[0] + 1] = out[g[0] + 1][:-len(QLA_MSG_GRANT)]
    return out


def mcp_values(tokens):
    """Every --mcp-config VALUE in a raw launch argv."""
    return [tokens[i + 1] for i, a in enumerate(tokens)
            if a == "--mcp-config" and i + 1 < len(tokens)]


def grants_of(tokens):
    """The raw --allowed-tools value of a launch argv ("" when it passes none)."""
    return tokens[tokens.index("--allowed-tools") + 1] if "--allowed-tools" in tokens else ""


def shared_core(tokens, *, mode, prompt=None, run_id=None, per_run=()):
    """The one shape both launches must reduce to, in order.

    argv mode:   drop the trailing `-- <prompt>` pair, drop the `--output-format
                 json` pair the driver replaces, placeholder what names the run.
    stream mode: drop the driver's tail -- asserted to be EXACTLY its four flags in
                 its one order, which is the "plus exactly the set" half; the
                 `--output-format stream-json` pair lives in that tail, so nothing of
                 the sort may be left behind in the core -- then placeholder.

    What is left is the same list, in the same order, for both modes -- or parity is
    broken. The `--` guard and the driver's flags must not appear on the other side
    either, so nothing can be added, removed or slid past another.
    """
    tokens = list(tokens)
    if mode == "stream":
        n = len(DRIVER_TAIL)
        assert tokens[-n:] == DRIVER_TAIL, \
            "the streamed launch does not end on the driver's own flags %r: %r" % (DRIVER_TAIL, tokens)
        tokens = tokens[:-n]
        assert "--output-format" not in tokens, \
            "the streamed launch left an --output-format outside the driver's tail: %r" % (tokens,)
        assert "--" not in tokens, "the streamed launch still carries the prompt guard: %r" % (tokens,)
    else:
        assert len(tokens) >= 2 and tokens[-2] == "--", \
            "the argv launch does not end on `-- <prompt>`: %r" % (tokens,)
        if prompt is not None:
            assert tokens[-1] == prompt, "the argv prompt is not what the run was asked"
        tokens = drop_output_format(tokens[:-2], "json", "argv-mode")
        for flag in ("--input-format", "--replay-user-messages", "--verbose"):
            assert flag not in tokens, \
                "the unstreamed launch gained the driver's flag %s: %r" % (flag, tokens)
    tokens = strip_qla_msg(tokens, mode)
    return placeholderise(tokens, run_id, per_run, mode)


def normalise_stream_argv(tokens, *, run_id=None, per_run=()):
    """The whole streamed launch, normalised: the shared core with the driver's four
    flags back on the end, exactly as claude is started with it. This is what the
    golden holds."""
    return shared_core(tokens, mode="stream", run_id=run_id, per_run=per_run) + list(DRIVER_TAIL)


def comparable_env(env):
    """An environment with only what the two launches must share: everything but the
    keys whose value names a run (PER_RUN_ENV_KEYS), the two that cannot be equal
    across the modes by construction (MODE_ENV_KEYS), and the qla_msg trio the
    launcher exports for the streamed launch only (QLA_MSG_ENV_KEYS -- their
    one-sided presence is asserted by the test that compares the modes)."""
    out = dict(env)
    for key in list(PER_RUN_ENV_KEYS) + list(MODE_ENV_KEYS) + list(QLA_MSG_ENV_KEYS):
        out.pop(key, None)
    return out


def env_diff(a, s):
    only_a, only_s = sorted(set(a) - set(s)), sorted(set(s) - set(a))
    changed = sorted(k for k in set(a) & set(s) if a[k] != s[k])
    lines = ["keys only the argv-mode launch saw: %s" % (only_a or "none"),
             "keys only the streamed launch saw: %s" % (only_s or "none"),
             "keys whose value differs: %s" % (changed or "none")]
    for key in changed:
        lines.append("  %s\n    argv-mode: %r\n    streamed:  %r" % (key, a[key], s[key]))
    for key in only_a + only_s:
        lines.append("  %s -> %r" % (key, a.get(key, s.get(key))))
    return "\n".join(lines)


def assert_parity(argv_rec, stream_rec, prompt, *, run_id=None, per_run=()):
    """The three pinned things of one launch: its argv, its prompt, its environment.

    The values the golden replaces are compared FIRST, whole, from the raw argv lists
    (assert_same_run_values); the list equality that follows then compares everything
    else, including the position each of those flags sits at.
    """
    argv, streamed = argv_rec["argv"], stream_rec["argv"]
    assert_same_run_values(argv, streamed)
    # shared_core strips the qla_msg additions (phase 2's one known difference);
    # what is left must be the same list, in the same order, for both modes.
    assert shared_core(streamed, mode="stream", run_id=run_id, per_run=per_run) \
        == shared_core(argv, mode="argv", prompt=prompt, run_id=run_id, per_run=per_run), \
        "the two launches do not reduce to the same claude argv\n" \
        "argv-mode: %r\nstreamed:  %r" % (argv, streamed)
    assert stream_rec["stdin_user_texts"], "the streamed launch never wrote a prompt on stdin"
    assert stream_rec["stdin_user_texts"][0] == prompt, \
        "the streamed prompt is not the argv prompt byte for byte:\n%r\n%r" \
        % (stream_rec["stdin_user_texts"][0], prompt)
    assert argv_rec["stdin_user_texts"] == [], \
        "the unstreamed launch read something off stdin: %r" % (argv_rec["stdin_user_texts"],)
    for side, rec in (("argv-mode", argv_rec), ("streamed", stream_rec)):
        for key in ("QWEN_INBOX", "QWEN_INBOX_DEFER"):
            assert key not in rec["env"], "claude saw the driver's %s (%s launch): 4.2" % (key, side)
    ea, es = comparable_env(argv_rec["env"]), comparable_env(stream_rec["env"])
    assert ea == es, "the two environments differ:\n%s" % env_diff(ea, es)
    # ...and dropping a key may never hide the fact that only one launch was given it
    dropped = set(PER_RUN_ENV_KEYS) | set(MODE_ENV_KEYS)
    seen_a = sorted(k for k in argv_rec["env"] if k in dropped)
    seen_s = sorted(k for k in stream_rec["env"] if k in dropped)
    assert seen_a == seen_s, \
        "a dropped env key is present on one launch only: %r vs %r" % (seen_a, seen_s)


# ---------------------------------------------------------------- the launches

def single_shot(tmp_path, models, args, *, stream, call, flag_file=None, expect=0):
    """One qwen-agent single-shot of the recording double, in one mode: (completed,
    its record -- None when `call` is None -- the watch id it registered).

    --shallow is typed on purpose: at the default depth an `-r auditor` run would
    also get the implied review round, i.e. a SECOND launch of the same session, and
    this file compares one launch at a time.

    Watch ON, with a cache folder of the test's own: that is how the streamed run
    gets its inbox (the registration makes <watch_dir>/<id>.inbox, K2), and why the
    QWEN_STREAM=0 run next to it must make no inbox at all (P5).

    `expect` other than 0 is for a launch the caller wants to see die: the exit code
    it must report, with the exec-retry backoff emptied so a claude that cannot be
    started says so at once instead of after the 10 s + 30 s retries.
    """
    extra = record_env(tmp_path, stream, flag_file)
    extra.update({"QWEN_WATCH": "1", "QWEN_WATCH_NOSERVE": "1",
                  "XDG_CACHE_HOME": posix(watch_cache(tmp_path)),
                  "QWEN_BASE_URL": base_url(models)})
    if expect:
        extra["QWEN_EXEC_RETRY_BACKOFF"] = ""
    before = _pointers(tmp_path)
    r = run(tmp_path, ["--shallow", *args], None, None, extra=extra, timeout=120)
    assert r.returncode == expect, \
        "exit %d, expected %d\n%s\n%s" % (r.returncode, expect, r.stdout, r.stderr)
    rid = new_pointer(tmp_path, before)
    rec = read_record(records_dir(tmp_path), call) if call is not None else None
    return r, rec, rid


# ------------------------------------------------------------------ the tests

def test_parity_single_shot_argv(tmp_path, server):                       # noqa: F811
    _, argv_rec, argv_id = single_shot(tmp_path, server,
                                       ["-r", "auditor", "--json", PROMPT],
                                       stream=False, call=1)
    stream_out, stream_rec, stream_id = single_shot(tmp_path, server,
                                                    ["-r", "auditor", "--json", PROMPT],
                                                    stream=True, call=2)
    records = records_dir(tmp_path)
    launched(records, ["1.json", "2.json"])

    assert_parity(argv_rec, stream_rec, PROMPT, run_id=stream_id,
                  per_run=[tmp_path, watch_cache(tmp_path)])

    # the flags that make it a streamed launch, and only those: the driver's four, in
    # its order, at the end -- and none of them in the unstreamed argv
    argv, streamed = argv_rec["argv"], stream_rec["argv"]
    assert streamed[-len(DRIVER_TAIL):] == DRIVER_TAIL
    assert streamed.count("--verbose") == 1
    assert argv.count("--verbose") == 0 and "--input-format" not in argv
    assert argv.count("--output-format") == 1 and argv[argv.index("--output-format") + 1] == "json"
    assert streamed.count("--output-format") == 1 \
        and streamed[streamed.index("--output-format") + 1] == "stream-json"
    assert "--" not in streamed and argv[-2:] == ["--", PROMPT]

    # phase 2 (spec 12.7): the qla_msg server, its reply grant and the env the
    # launcher exports for them ARE the streamed-mode addition -- and the whole
    # point is that the unstreamed launch carries none of it (byte-identical).
    assert any(last_segment(v) == QLA_MSG_CONFIG_FILE for v in mcp_values(streamed)), \
        "the streamed launch carries no qla_msg --mcp-config: %r" % (streamed,)
    assert not any(last_segment(v) == QLA_MSG_CONFIG_FILE for v in mcp_values(argv)), \
        "the unstreamed launch carries a qla_msg --mcp-config: %r" % (argv,)
    assert grants_of(streamed).endswith(QLA_MSG_GRANT), \
        "the streamed launch's grants do not end on the reply grant: %r" % (streamed,)
    assert not grants_of(argv).endswith(QLA_MSG_GRANT), \
        "the unstreamed launch was granted mcp__qla_msg__reply: %r" % (argv,)
    for key in QLA_MSG_ENV_KEYS:
        assert key in stream_rec["env"], "%s is not in the streamed launch env" % key
        assert key not in argv_rec["env"], "%s is in the unstreamed launch env" % key

    # the prompt went to stdin exactly once there, and to the argv exactly once here
    assert stream_rec["stdin_user_texts"] == [PROMPT]
    assert argv[-1] == PROMPT

    # the placeholdered values said plainly, on each path: the server's one model and
    # the auditor role text the -r run is supposed to send
    assert_asks_the_run_asked(argv_rec["argv"])
    assert_asks_the_run_asked(stream_rec["argv"])

    # what each mode handed back: the stream's record is the old one plus the trio
    rec = json.loads(stream_out.stdout)
    assert rec["stream"] is True and rec["notes_delivered"] == 0 and rec["note_ids"] == []
    assert rec["result"] == RECORD_ANSWER and rec["session_id"] == FAKE_SESSION

    # K1/P5: an inbox for the run that streamed, none for the one that did not -- and
    # both live under this test's cache folder, never in the real watch dir
    assert inbox_dir_for(tmp_path, stream_id).is_dir()
    assert (inbox_dir_for(tmp_path, stream_id) / "new").is_dir()
    assert not inbox_dir_for(tmp_path, argv_id).exists()
    for rid in (argv_id, stream_id):
        assert str(pointer_dir(tmp_path) / ("%s.json" % rid)).startswith(str(watch_cache(tmp_path)))


def test_parity_single_shot_prompt_file(tmp_path, server):                # noqa: F811
    """A -f prompt that exercises every way a prompt can be mangled on its way to the
    model: non-ASCII, a line that starts with `--` (the argv guard's own token), a
    trailing newline (`$(cat -- FILE)` eats it, so neither mode may invent one), and
    a 200 KiB body a printf, a pipe and a JSON line must all carry whole.

    The 200 KiB body also draws the one difference between the two launches that is
    not qwen-agent's doing, and the test pins it rather than hiding it: the kernel
    caps a SINGLE argument at MAX_ARG_STRLEN (128 KiB on Linux), so a prompt that big
    cannot be put into `-- <prompt>` at all -- the argv launch dies in execve with
    E2BIG ("Argument list too long") before claude is started -- while the driver's
    launch writes the same text to a prompt file and streams it, and it arrives whole.
    The byte-for-byte comparison of the two launches therefore runs on the same-shaped
    prompt at a size both paths can carry -- 96 KiB, but 24 KiB on Windows, where the
    whole command line is capped at 32,767 characters (CreateProcess) and the argv-mode
    launch could not carry 96 KiB plus the run's other flags; the 200 KiB one runs on
    both paths and is asserted to reach only one of them -- and that last, exit-8 half
    is a Linux-only premise (only Linux caps a single argument at MAX_ARG_STRLEN), so
    it is gated on sys.platform while the streamed 200 KiB half runs everywhere."""
    head = ("Review the login flow, then answer.\n"
            "--this-line-starts-with-the-argv-guard\n"
            "“ünïcødé” ⚙ 漢字 🎯 – ño ã accéntos\n")
    # Windows caps a WHOLE command line at 32,767 characters (CreateProcess), so the
    # argv-mode launch there could not carry the 96 KiB prompt: the exec dies with
    # E2BIG ("Argument list too long", exit 8) before the fake witnesses a launch.
    # 24 KiB keeps the same shape -- non-ASCII, a leading `--` line, a trailing
    # newline -- with room for the rest of the line (flags, role text, model name).
    fits_size = 24 * 1024 if os.name == "nt" else 96 * 1024
    fits = head + ("x" * 63 + "\n") * (fits_size // 64)        # one argv slot's worth
    oversized = head + ("x" * 63 + "\n") * (200 * 1024 // 64)  # past MAX_ARG_STRLEN
    fits_file = tmp_path / "prompt.md"
    fits_file.write_bytes(fits.encode("utf-8"))         # ends on a trailing \n
    big_file = tmp_path / "prompt-200k.md"
    big_file.write_bytes(oversized.encode("utf-8"))
    assert len(fits_file.read_bytes()) == len(head.encode("utf-8")) + fits_size
    assert len(big_file.read_bytes()) == len(head.encode("utf-8")) + 200 * 1024

    args = ["-r", "auditor", "--json", "-f", posix(fits_file)]
    _, argv_rec, argv_id = single_shot(tmp_path, server, args, stream=False, call=1)
    _out, stream_rec, stream_id = single_shot(tmp_path, server, args, stream=True, call=2)

    argv_prompt = argv_rec["argv"][-1]
    assert argv_rec["argv"][-2] == "--"
    assert argv_prompt == fits.rstrip("\n")                   # the file, minus the newline $(cat) strips
    assert argv_prompt.endswith("x")
    assert "--this-line-starts-with-the-argv-guard" in argv_prompt
    assert "“ünïcødé” ⚙ 漢字 🎯" in argv_prompt
    assert len(argv_prompt.encode("utf-8")) >= fits_size

    assert_parity(argv_rec, stream_rec, argv_prompt, run_id=stream_id,
                  per_run=[tmp_path, watch_cache(tmp_path)])
    assert_asks_the_run_asked(argv_rec["argv"])
    assert_asks_the_run_asked(stream_rec["argv"])
    assert stream_rec["stdin_user_texts"] == [argv_prompt]
    assert stream_rec["stdin_user_texts"][0].encode("utf-8") == argv_prompt.encode("utf-8")
    # the whole prompt went on stdin and none of it stayed in the argv: the streamed
    # launch has no `--` guard to carry it, and no inbox for a run that did not stream
    assert "--" not in stream_rec["argv"]
    assert inbox_dir_for(tmp_path, stream_id).is_dir()
    assert not inbox_dir_for(tmp_path, argv_id).exists()

    # ...and the same prompt at 200 KiB: the streamed launch hands it over whole.
    big_args = ["-r", "auditor", "--json", "-f", posix(big_file)]
    big_out, big_rec, _bid = single_shot(tmp_path, server, big_args, stream=True, call=3)
    assert big_out.returncode == 0, big_out.stdout + big_out.stderr
    assert big_rec["stdin_user_texts"] == [oversized.rstrip("\n")]
    assert big_rec["stdin_user_texts"][0].encode("utf-8") == oversized.rstrip("\n").encode("utf-8")
    assert len(big_rec["stdin_user_texts"][0].encode("utf-8")) >= 200 * 1024
    assert "--this-line-starts-with-the-argv-guard" in big_rec["stdin_user_texts"][0]
    assert "“ünïcødé” ⚙ 漢字 🎯" in big_rec["stdin_user_texts"][0]
    launched(records_dir(tmp_path), ["1.json", "2.json", "3.json"])

    # The argv launch cannot start claude with that prompt at all -- but that rests
    # on Linux's MAX_ARG_STRLEN: the kernel caps ONE argument at 128 KiB, so execve
    # fails with E2BIG ("Argument list too long") before claude runs. macOS and
    # Windows have no such per-argument cap, so this half runs on Linux only; the
    # streamed 200 KiB half above (and everything before it) runs on every platform.
    if sys.platform.startswith("linux"):
        big_argv, _none, _sid = single_shot(tmp_path, server, big_args,
                                            stream=False, call=None, expect=8)
        assert "Argument list too long" in big_argv.stderr, \
            "the argv launch failed for some reason other than the kernel's per-argument " \
            "cap, and this test's premise is wrong:\n%s" % big_argv.stderr
        assert not (records_dir(tmp_path) / "4.json").exists(), \
            "claude was started after all: the failure was not the exec"


def reset_repo(repo):
    """Back to the committed state: until-done refuses a dirty tree at the start, and
    the flag file the double writes in round 2 must not survive into the other mode."""
    subprocess.run(["git", "-C", str(repo), "reset", "--hard", "HEAD", "-q"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-fdq"], check=True, capture_output=True)
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], check=True,
                          capture_output=True, text=True).stdout.strip() == ""


def ud_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.com"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo)] + a, check=True, capture_output=True)
    (repo / "check.py").write_text(
        "import os, sys\nsys.exit(0 if os.path.exists('good') else 1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True,
                   capture_output=True)
    task = tmp_path / "task.md"
    task.write_text("- [ ] the flag file exists -- check: cmd %s check.py\n"
                    % posix(sys.executable), encoding="utf-8")
    return repo, task


def run_dirs(state):
    return {p.parent for p in state.rglob("report.md")}


def new_run_dir(state, before):
    wait_for(lambda: bool(run_dirs(state) - before), timeout=180,
             message="the until-done loop left no run directory")
    fresh = sorted(run_dirs(state) - before)
    assert len(fresh) == 1, "both runs reported into one folder: %r" % (fresh,)
    return fresh[0]


def until_done(tmp_path, models, repo, task, state, *, stream, first_call):
    """One 2-round --until-done run of the recording double, in one mode: (completed,
    [round-1 record, round-2 record], the loop's run directory).

    The check is a flag file the double writes the moment a round is RESUMED, so
    round 1 fails and round 2 passes in both modes. The round prompts are
    supervisor.FIRST and NOT_DONE, and no note is ever queued, so the between-rounds
    drain adds nothing to either side of the comparison. The page stays off (the
    suite's QWEN_WATCH=0): the loop still opens RUN/inbox for its rounds to stream
    into, and neither run touches the real watch dir.
    """
    extra = record_env(tmp_path, stream, flag_file=repo / "good")
    extra.update({"QWEN_BASE_URL": base_url(models),
                  "QWEN_AGENT_STATE": posix(state),
                  "QWEN_TEST_WORKTREES": posix(tmp_path / "wts"),
                  "QWEN_SUPERVISOR_BACKOFF": "0"})
    reset_repo(repo)
    before = run_dirs(state)
    r = run(tmp_path, ["--shallow", "--until-done", posix(task), "-C", posix(repo),
                       "--no-deviation-audit", "--max-rounds", "2"],
            None, None, extra=extra, timeout=300)
    assert r.returncode == 0, "exit %d\n%s\n%s" % (r.returncode, r.stdout, r.stderr)
    this_run = new_run_dir(state, before)
    records = records_dir(tmp_path)
    return r, [read_record(records, first_call), read_record(records, first_call + 1)], this_run


def test_parity_until_done_rounds(tmp_path, server):                      # noqa: F811
    repo, task = ud_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()

    _a, argv_rounds, argv_dir = until_done(tmp_path, server, repo, task, state,
                                           stream=False, first_call=1)
    _s, stream_rounds, stream_dir = until_done(tmp_path, server, repo, task, state,
                                               stream=True, first_call=3)
    launched(records_dir(tmp_path), ["1.json", "2.json", "3.json", "4.json"])

    # the second round is the one that fixed it: the check failed once, then passed
    assert (repo / "good").exists()
    # every round of both modes got an inbox to stream into, and the unstreamed loop
    # made none at all (P5) -- both under the test's own state folder
    assert (stream_dir / "inbox" / "new").is_dir()
    assert str(stream_dir).startswith(str(state))
    assert not (argv_dir / "inbox").exists()

    for k, (argv_rec, stream_rec) in enumerate(zip(argv_rounds, stream_rounds), start=1):
        prompt_file = argv_dir / ("round-%d.prompt.md" % k)
        argv_prompt = argv_rec["argv"][-1]
        assert argv_rec["argv"][-2] == "--"
        assert argv_prompt == prompt_file.read_text(encoding="utf-8").rstrip("\n")
        assert_parity(argv_rec, stream_rec, argv_prompt, per_run=[tmp_path, state])
        assert stream_rec["stdin_user_texts"] == [argv_prompt], \
            "round %d: the driver wrote the prompt %d time(s)" % (
                k, len(stream_rec["stdin_user_texts"]))
        if k == 1:
            assert "--resume" not in argv_rec["argv"], "round 1 resumed something"
            assert "--resume" not in stream_rec["argv"], "round 1 resumed something"
        else:
            # round 2 resumes round 1's session in BOTH modes, with the id the double
            # reported: the same session, so the server's prefix cache stays warm
            for rec, side in ((argv_rec, "argv-mode"), (stream_rec, "streamed")):
                assert "--resume" in rec["argv"], "round 2 of the %s run did not resume" % side
                assert rec["argv"][rec["argv"].index("--resume") + 1] == FAKE_SESSION, side


def golden_payload(argv, env):
    # QLA_MSG_ENV_KEYS leave the golden's list out: they are THE streamed-side
    # addition (spec 12.7), and the comparison test asserts their presence where
    # that is what is under test -- a golden that grows a key every time a feature
    # exports one stops being a pinned picture of the launch.
    return {"argv": normalise_stream_argv(argv),
            "env_keys": sorted(k for k in env
                               if (k.startswith(OWN_ENV_PREFIXES) or k in OWN_ENV_KEYS_EXTRA)
                               and k not in QLA_MSG_ENV_KEYS)}


def golden_message(payload, golden):
    return ("the streamed launch is no longer what the golden says it is.\n"
            "%s\n\n"
            "If this change is intended, regenerate the golden with\n"
            "    %s\n"
            "and have a reviewer LOOK AT the regenerated file before it is committed: "
            "a golden that has only been regenerated has stopped being a check.\n"
            "golden argv:  %r\ncurrent argv: %r\ngolden env_keys: %r\ncurrent env_keys: %r"
            % (GOLDEN, REGENERATE, golden.get("argv"), payload["argv"],
               golden.get("env_keys"), payload["env_keys"]))


def test_stream_launch_matches_golden(tmp_path, server):                  # noqa: F811
    """The streamed launch pinned on its own, so it keeps being pinned after the
    argv path is deleted and there is no second launch left to compare it against."""
    _out, rec, _rid = single_shot(tmp_path, server, ["-r", "auditor", "--json", PROMPT],
                                  stream=True, call=1)
    # the two placeholders that name nothing on their own are pinned on the launch,
    # not only in the golden's angle brackets
    assert_asks_the_run_asked(rec["argv"])
    payload = golden_payload(rec["argv"], rec["env"])
    if os.environ.get("QLA_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        pytest.skip("the golden was rewritten from this machine's launch -- a regenerated "
                    "golden needs a reviewer's look before it is committed")
    assert GOLDEN.exists(), "%s is missing. Create it with %s." % (GOLDEN, REGENERATE)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert payload["argv"] == golden["argv"], golden_message(payload, golden)
    assert payload["env_keys"] == golden["env_keys"], golden_message(payload, golden)
