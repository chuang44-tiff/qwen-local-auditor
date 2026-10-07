"""Scripted UI suites for the browser tester: the scenario file, its prompt, its results.

A scenario file is Markdown (UTF-8):

    # Suite: <name>                      (required, first heading)
    base: <url>                          (optional; relative "open" targets resolve here)

    ## Scenario: <title>                 (one or more)
    id: <id>                             (optional; default s1, s2, ... in file order;
                                          [A-Za-z0-9_-]+, unique)
    steps:
    1. <step text>                       (numbered list, at least one)
    expect:
    - <expected result>                  (bullet list, at least one)

Anything else inside a scenario (blank lines, free text) is ignored. Every parse
error names the line it comes from ("line N: ...") and is a ScenarioError.

The module is the single contract between qwen-agent --scenarios and the tester:

  parse(text)                 -> {"suite", "base", "scenarios": [{"id","title",
                                          "steps","expect"}]}
  prompt(suite)               -> the task text handed to the tester (every scenario
                                          with its steps and expectations, then the
                                          reporting contract)
  results(answer, suite)      -> one result dict per scenario id, in file order
  summary(results)            -> the Markdown table plus the PASS/FAIL/BLOCKED line

CLI:

  scenarios.py check FILE                       exit 0 + "ok: N scenarios", or 2 + the error
  scenarios.py prompt FILE                      the task text (2 = the file does not parse)
  scenarios.py results FILE ANSWER_FILE OUT_JSON
                                                writes the result list to OUT_JSON, prints
                                                the summary, exit 0
  scenarios.py replay-check SUITE_FILE RESULTS_JSON REPLAY_DIR OUT_DIR
                                                runs <id>.mjs of every scenario, keeps the
                                                ones whose RESULT line reproduces the
                                                recorded verdict, copies them to OUT_DIR
                                                with manifest.json, prints 'recorded: ...'
                                                and the manifest path, exit 0
  scenarios.py replay DIR [--base URL] [--out RESULTS_JSON]
                                                runs every script OUT_DIR's manifest names,
                                                no model at all: prints the summary (the
                                                scenarios it kept no script for listed as
                                                NOT RECORDED), exit 0 all PASS, 9 any FAIL
                                                or ERROR, or no script kept at all

The reporting contract asks for the LAST fenced json block the answer carries;
qwen-agent.sh takes the per-status counts off the summary line the `results`
command prints, so this file is the only place the shape is decided.

A replay script is a Node ES module the recorded session wrote itself, and the
runner executes it with node, as the user, with no sandbox and the user's network:
replay only a folder you recorded or have read. Its whole
contract is one line on stdout -- 'RESULT <id> PASS' or 'RESULT <id> FAIL: <the
expectation that did not hold>' -- exit 0 after it, exit 1 on a script error, and
the base URL read from QWEN_REPLAY_BASE (its own default: the suite's base URL).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

STATUSES = ("PASS", "FAIL", "BLOCKED")
# What a REPLAYED scenario can be: BLOCKED needs a pair of eyes, so a script that
# crashed, printed nothing or ran out of its 120 s is an ERROR instead.
REPLAY_STATUSES = ("PASS", "FAIL", "ERROR")
REPLAY_TIMEOUT = 120                       # wall-clock seconds per script
# The one line a replay script must print, and nothing else that is read off it.
REPLAY_RESULT = re.compile(r"^RESULT\s+(\S+)\s+(PASS|FAIL)(?::\s*(.*))?$")
# A scenario the recording kept no script for: listed by a replay, never run.
NOT_RECORDED = "NOT RECORDED"
REPLAY_MANIFEST = "manifest.json"
REPLAY_INSTALL_HINT = "npm i -g playwright && npx playwright install chromium"
_ID_OK = re.compile(r"[A-Za-z0-9_-]+")
_SUITE_HEAD = re.compile(r"^#\s+Suite:\s*(.*\S)\s*$")
_SCENARIO_HEAD = re.compile(r"^##\s+Scenario:\s*(.*\S)\s*$")
_HEAD = re.compile(r"^#+\s")
_BASE = re.compile(r"^base:\s*(.*\S)\s*$")
_ID = re.compile(r"^id:\s*(.*?)\s*$")
_STEPS = re.compile(r"^steps:\s*$")
_EXPECT = re.compile(r"^expect:\s*$")
_STEP_ITEM = re.compile(r"^\s*\d+\.\s+(.*\S)\s*$")
_EXPECT_ITEM = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
# A fence opens with ``` plus an optional language tag and nothing else on the
# line; it closes at ```. DOTALL across blocks, MULTILINE so ^$ are line edges.
_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*$(.*?)^```[ \t]*$", re.S | re.M)


class ScenarioError(Exception):
    """The scenario file is refused, or the answer cannot be scored against it.

    The message always names its origin ("line N: ..." for a file); qwen-agent
    prints it verbatim, so it must read as the whole explanation."""


class ReplayError(Exception):
    """The replay cannot run at all: node or playwright is missing, or the files a
    replay needs are not there. The message is the whole explanation, and it is
    printed verbatim -- so it carries the fix, not just the complaint."""


def _lines(text):
    """(number, line) for every line, CRLF normalised, a UTF-8 BOM dropped.

    Lines keep their trailing newline (splitlines keeps it, and we re-add it
    otherwise), so a step that ran over one line continues on the next only if
    that next line is itself a list item: free text in between is ignored, not
    glued on."""
    bom = chr(0xFEFF)              # a UTF-8 BOM must not join the suite name; spelled as
    if text.startswith(bom):       # chr() so the source carries no invisible character
        text = text[len(bom):]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [(i, ln if ln.endswith("\n") else ln + "\n")
            for i, ln in enumerate(text.splitlines(), 1)]


CONTRACT = (
    "Run the scenarios in order, each from a fresh page load. For each scenario "
    "follow the steps exactly; do not skip or reorder them. Then end your answer "
    "with ONE fenced json block: {\"results\": [{\"id\": ..., \"status\": "
    "\"PASS\"|\"FAIL\"|\"BLOCKED\", \"failed_expectations\": [...], \"evidence\": "
    "[\"snapshot or screenshot file and what it shows\"], \"notes\": \"...\"}]} "
    "with one entry per scenario id. BLOCKED means a step could not be performed."
)


def parse(text):
    """The suite dict, or ScenarioError("line N: ...") naming the offending line.

    Refused: no suite heading as the first heading, no scenarios at all, a
    scenario without steps or without expectations, an id outside
    [A-Za-z0-9_-]+, a duplicate id (defaults included)."""
    lines = _lines(text)
    suite, suite_line = None, 0
    for num, line in lines:
        if not _HEAD.match(line):
            continue
        m = _SUITE_HEAD.match(line.rstrip("\n"))
        if not m:
            raise ScenarioError("line %d: the first heading must be "
                                "'# Suite: <name>', got %r" % (num, line.rstrip("\n")))
        suite, suite_line = m.group(1), num
        rest = lines[num:]                       # `num` is 1-based: the next line's number
        break
    if suite is None:
        raise ScenarioError("line 1: no '# Suite: <name>' heading")
    base = None
    scenarios = []                               # each: id/title/steps/expect + their lines
    cur = None

    def close():
        nonlocal cur
        if cur is not None:
            _check_scenario(cur)
            scenarios.append(cur)
        cur = None

    for num, line in rest:
        stripped = line.rstrip("\n")
        if _SCENARIO_HEAD.match(stripped):
            close()
            title = _SCENARIO_HEAD.match(stripped).group(1)
            cur = {"title": title, "title_line": num, "id": None, "id_line": 0,
                   "explicit": False, "steps": [], "expect": [], "section": ""}
            continue
        if stripped.startswith("#"):             # any other heading ends the scenario
            close()
            continue
        if cur is None:
            if base is None:
                m = _BASE.match(stripped)
                if m:
                    base = m.group(1)
            continue                             # suite-header free text: ignored
        m = _ID.match(stripped)
        if m:
            cur["id"], cur["id_line"], cur["explicit"] = m.group(1), num, True
            cur["section"] = ""
            continue
        if _STEPS.match(stripped):
            cur["section"] = "steps"
            continue
        if _EXPECT.match(stripped):
            cur["section"] = "expect"
            continue
        m = _STEP_ITEM.match(line)
        if m and cur["section"] == "steps":
            cur["steps"].append(m.group(1))
            continue
        m = _EXPECT_ITEM.match(line)
        if m and cur["section"] == "expect":
            cur["expect"].append(m.group(1))
            continue
        # anything else inside a scenario (blank lines, free text) is ignored
    close()
    if not scenarios:
        raise ScenarioError("line %d: suite '%s' has no scenarios "
                            "(add '## Scenario: <title>')" % (suite_line, suite))
    for i, sc in enumerate(scenarios, 1):
        if sc["explicit"] and not _ID_OK.fullmatch(sc["id"]):
            raise ScenarioError("line %d: invalid scenario id %r "
                                "(use [A-Za-z0-9_-]+)" % (sc["id_line"], sc["id"]))
        if sc["id"] is None:
            sc["id"] = "s%d" % i                 # the file-order default
    for i in range(len(scenarios)):
        for j in range(i + 1, len(scenarios)):
            if scenarios[i]["id"] == scenarios[j]["id"]:
                # blame the explicit id that took the name the other already holds
                blamed = scenarios[j] if scenarios[j]["explicit"] else scenarios[i]
                raise ScenarioError("line %d: duplicate scenario id %r"
                                    % (blamed["id_line"], scenarios[j]["id"]))
    return {"suite": suite, "base": base,
            "scenarios": [{"id": sc["id"], "title": sc["title"],
                           "steps": sc["steps"], "expect": sc["expect"]}
                          for sc in scenarios]}


def _check_scenario(sc):
    """The two completeness rules, each naming the scenario's own heading line."""
    if not sc["steps"]:
        raise ScenarioError("line %d: scenario '%s' has no steps "
                            "(add 'steps:' and a numbered list)"
                            % (sc["title_line"], sc["title"]))
    if not sc["expect"]:
        raise ScenarioError("line %d: scenario '%s' has no expectations "
                            "(add 'expect:' and a bullet list)"
                            % (sc["title_line"], sc["title"]))


def prompt(suite):
    """The task text handed to the tester: the suite, then every scenario with its
    id, steps and expectations, then the reporting contract verbatim."""
    lines = ["Suite: %s" % suite["suite"]]
    if suite.get("base"):
        lines.append("Base URL: %s" % suite["base"])
    for sc in suite["scenarios"]:
        lines += ["", "Scenario %s: %s" % (sc["id"], sc["title"]), "Steps:"]
        lines += ["%d. %s" % (n, step) for n, step in enumerate(sc["steps"], 1)]
        lines.append("Expectations:")
        lines += ["- %s" % exp for exp in sc["expect"]]
    return "\n".join(lines + ["", CONTRACT, ""])


def _results_list(block):
    """The entries of a parsed json block: {"results": [...]} or a bare [...]."""
    if isinstance(block, dict):
        block = block.get("results")
    return block if isinstance(block, list) else []


def results(answer, suite):
    """One result dict per scenario id, in file order.

    The LAST fenced json block of the answer is the report, parsed or empty -- a
    block that parses but carries no usable entry IS a report, and its scenarios
    simply went unreported ("no result reported"). Only no fenced json block at
    all means every scenario is BLOCKED with "no result block". Entries whose id
    is not in the suite are ignored; the LAST entry for an id wins. A status
    outside PASS/FAIL/BLOCKED becomes BLOCKED with a note saying what it was."""
    by_id = None                                 # None: no block has parsed yet
    if isinstance(answer, bytes):                # qwen-agent hands bytes; cp1252 decoding
        answer = answer.decode("utf-8", "replace")  # would turn valid UTF-8 into junk
    for block in _FENCE.findall(answer):
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        by_id = {}                               # the LAST block that parses is the report
        for entry in _results_list(parsed):      # ... whatever it carries, even nothing
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                by_id[entry["id"]] = entry
    if by_id is None:                            # nothing parsed: there was no report
        return [{"id": sc["id"], "status": "BLOCKED", "notes": "no result block"}
                for sc in suite["scenarios"]]
    out = []
    for sc in suite["scenarios"]:
        entry = by_id.get(sc["id"])
        if entry is None:
            out.append({"id": sc["id"], "status": "BLOCKED", "notes": "no result reported"})
            continue
        raw = entry.get("status")
        notes = entry.get("notes")
        notes = notes if isinstance(notes, str) else ("" if notes is None else str(notes))
        if raw not in STATUSES:
            invalid = ("no status" if raw is None else "invalid status: %s" % (raw,))
            notes = "; ".join(x for x in (notes, invalid) if x)
            raw = "BLOCKED"
        out.append({"id": sc["id"], "status": raw,
                    "failed_expectations": _str_list(entry.get("failed_expectations")),
                    "evidence": _str_list(entry.get("evidence")),
                    "notes": notes})
    return out


def _str_list(v):
    return v if isinstance(v, list) else ([] if v is None else [str(v)])


def summary(res, statuses=STATUSES):
    """The Markdown table one line per scenario, plus the PASS n / FAIL n / BLOCKED n
    counts (a replay passes its own three: PASS / FAIL / ERROR). A note with pipes or
    newlines cannot break the table."""
    rows = ["| id | status | notes |", "|---|---|---|"]
    for r in res:
        note = str(r.get("notes") or "").replace("|", "\\|").replace("\n", " ")
        rows.append("| %s | %s | %s |" % (r.get("id", ""), r.get("status", ""), note))
    n = {s: sum(1 for r in res if r.get("status") == s) for s in statuses}
    counts = ["%s %d" % (s, n[s]) for s in statuses]
    unrecorded = sum(1 for r in res if r.get("status") == NOT_RECORDED)
    if unrecorded:
        counts.append("%s %d" % (NOT_RECORDED, unrecorded))
    rows.append("")
    rows.append(" / ".join(counts))
    return "\n".join(rows) + "\n"


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


# ------------------------------------------------------------------ replay
#
# The replay runner: node plus the playwright package, and one script per scenario
# run in a fresh temporary directory. The scripts are model-written code run as the
# user, unsandboxed: whatever they do, including network access, is not confined.

def _npm_npx_node_modules():
    """The first node_modules holding playwright under "$(npm config get cache)"
    /_npx/*/node_modules -- where npx keeps the package the browser MCP server
    pulled -- or None. No npm, no answer: never an error of its own."""
    npm = shutil.which("npm")
    if npm is None:
        return None
    try:
        r = subprocess.run([npm, "config", "get", "cache"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, encoding="utf-8",
                           errors="replace", timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):     # no npm, no answer
        return None
    if r.returncode != 0:
        return None
    cache = (r.stdout or "").strip()
    if not cache:
        return None
    for d in sorted(glob.glob(os.path.join(cache, "_npx", "*", "node_modules"))):
        if os.path.isdir(os.path.join(d, "playwright")):
            return d
    return None


def _replay_toolchain():
    """(node, node_modules) -- the interpreter and the directory NODE_PATH needs.

    Resolved in the documented order: $QWEN_PLAYWRIGHT_NODE_PATH (a node_modules
    directory), then the npx cache. A missing node is the runner's own refusal: no
    script can be validated or replayed without it."""
    node = shutil.which("node")
    if node is None:
        raise ReplayError("replay needs node on PATH (the replay scripts are Node ES "
                          "modules): install Node.js, or put node on PATH")
    pinned = os.environ.get("QWEN_PLAYWRIGHT_NODE_PATH")
    if pinned and os.path.isdir(os.path.join(pinned, "playwright")):
        return node, pinned
    found = _npm_npx_node_modules()
    if found is None:
        raise ReplayError("the 'playwright' package was not found: set "
                          "QWEN_PLAYWRIGHT_NODE_PATH to a node_modules directory that "
                          "holds it, or install it once: " + REPLAY_INSTALL_HINT)
    return node, found


def _kill_script(proc):
    """Kill the script AND what it started: a browser it left behind would hold the
    output pipes open and keep a timed-out script's directory (and memory) alive. On
    Windows the tree goes through taskkill /T, under a timeout of its own."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
            proc.kill()
    except OSError:                        # already gone: exactly what was wanted
        pass


def _link_node_modules(target, link):
    """`link` -> `target`: a symlink, else (Windows without the symlink privilege) a
    directory junction. Neither possible is a ReplayError: without the link every
    script's `import 'playwright'` fails, and that is the environment, not a verdict."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as e:
        first = e
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(target, link)
            return
        except (ImportError, OSError, AttributeError):
            pass
    raise ReplayError("cannot link node_modules beside the replay script (%s); on "
                      "Windows, enable Developer Mode or run from a shell that may "
                      "create symbolic links" % first)


def _run_script(node, node_modules, script, base=None, timeout=REPLAY_TIMEOUT, sid=None):
    """(status, note) for one replay script: PASS, FAIL with the expectation that
    did not hold, or ERROR -- no RESULT line (the script crashed) or its wall clock
    ran out. With `sid`, only a RESULT line naming that scenario counts. stderr is not
    echoed: its first error line goes into the ERROR note.

    The script runs as a COPY inside a fresh temp dir beside a node_modules link to the
    resolved Playwright: replay scripts are ES modules, and an ES module resolves a bare
    import ('playwright') from its own folder upward -- NODE_PATH is ignored for import.
    NODE_PATH stays set for CommonJS helpers."""
    env = dict(os.environ)
    env["NODE_PATH"] = node_modules
    if base:
        env["QWEN_REPLAY_BASE"] = base
    cwd = tempfile.mkdtemp(prefix="qwen-replay-")
    try:
        local = os.path.join(cwd, os.path.basename(script))
        shutil.copyfile(script, local)
        _link_node_modules(node_modules, os.path.join(cwd, "node_modules"))
        proc = subprocess.Popen([node, local], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_script(proc)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:  # a grandchild still holds the pipes
                out, err = b"", b""
            timed_out = True
        code, out = proc.returncode, out.decode("utf-8", "replace")
        err = err.decode("utf-8", "replace")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if timed_out:                          # whatever it printed, it never finished
        return "ERROR", "timed out after %gs" % timeout
    hit = None
    for line in out.splitlines():          # the LAST RESULT line is its verdict
        m = REPLAY_RESULT.match(line.strip())
        if m and (sid is None or m.group(1) == sid):
            hit = m
    if hit is None:
        why = next((ln.strip() for ln in err.splitlines()
                    if "rror" in ln or "xecutable" in ln), "")
        return "ERROR", "no RESULT line (exit %s)%s" % (code, ": " + why[:200] if why else "")
    if hit.group(2) == "FAIL":
        return "FAIL", (hit.group(3) or "").strip()
    return "PASS", ""


def _read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        raise ReplayError("%s is not valid json: %s" % (what, path))
    except OSError as e:
        raise ReplayError("cannot read %s: %s" % (what, e))


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def replay_check(suite_file, results_file, src_dir, out_dir, timeout=REPLAY_TIMEOUT):
    """The manifest of what survived validation, as written into OUT_DIR.

    A script is kept only when replaying it now answers what the run answered: a
    PASS that replays FAIL is a script that does not test what was tested (or an app
    that moved under it), and a kept script is trusted without a model from here on,
    so a mismatch is rejected with its reason rather than kept with a warning.
    BLOCKED and unreported scenarios get no script at all: nothing was established
    about them for a script to reproduce."""
    suite = parse(_read(suite_file))
    if os.path.realpath(src_dir) == os.path.realpath(out_dir):
        raise ReplayError("the recording folder %s is the folder the scripts were written "
                          "to; record into another directory" % out_dir)
    recorded = _read_json(results_file, "the results file of the recorded run")
    if not isinstance(recorded, list):
        raise ReplayError("the results file of the recorded run is not a list of "
                          "results: %s" % results_file)
    by_id = {r.get("id"): r for r in recorded
             if isinstance(r, dict) and isinstance(r.get("id"), str)}
    node, node_modules = _replay_toolchain()
    scripts, rejected = [], []
    for sc in suite["scenarios"]:
        sid = sc["id"]
        want = (by_id.get(sid) or {}).get("status")
        if want is None:
            rejected.append({"id": sid, "reason": "not run (no result recorded)"})
            continue
        if want not in ("PASS", "FAIL"):
            rejected.append({"id": sid, "reason": "recorded %s" % want})
            continue
        src = os.path.join(src_dir, sid + ".mjs")
        if not os.path.isfile(src):
            rejected.append({"id": sid, "reason": "no script was written"})
            continue
        # No QWEN_REPLAY_BASE here: the script's own default IS the suite's base URL,
        # and validation has to run the same command a later replay runs.
        got, note = _run_script(node, node_modules, src, None, timeout, sid)
        if got != want:
            reason = "replay said %s, the run recorded %s" % (got, want)
            rejected.append({"id": sid, "reason": "%s: %s" % (reason, note)
                             if note else reason})
            continue
        name = sid + ".mjs"
        try:
            os.makedirs(out_dir, exist_ok=True)
            shutil.copyfile(src, os.path.join(out_dir, name))
            digest = _sha256(os.path.join(out_dir, name))
        except OSError as e:
            raise ReplayError("cannot write the kept script into %s: %s" % (out_dir, e))
        scripts.append({"id": sid, "verdict": want, "sha256": digest, "file": name})
    manifest = {"suite": suite["suite"], "file": os.path.abspath(suite_file),
                "base": suite.get("base"),
                "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scripts": scripts, "rejected": rejected}
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, REPLAY_MANIFEST), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as e:
        raise ReplayError("cannot write the manifest into %s: %s" % (out_dir, e))
    return manifest


def replay(rec_dir, base=None, timeout=REPLAY_TIMEOUT):
    """One result dict per manifest script, in manifest order -- with NO model in the
    loop: the verdicts were recorded once, and the scripts carry them from there. The
    scenarios the recording kept no script for follow as NOT RECORDED rows.

    A script whose bytes are not the ones that were validated is never run: the
    manifest is what says a PASS was ever observed for this file, and a file that
    changed since says nothing about the run that recorded it."""
    path = os.path.join(rec_dir, REPLAY_MANIFEST)
    manifest = _read_json(path, "the replay manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("scripts"), list):
        raise ReplayError("the replay manifest has no 'scripts' list: %s" % path)
    node, node_modules = _replay_toolchain()
    out = []
    for entry in manifest["scripts"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ReplayError("the replay manifest has an entry with no scenario id: %s"
                              % path)
        sid = entry["id"]
        name = entry.get("file") or (sid + ".mjs")
        if not isinstance(name, str) or os.path.basename(name) != name \
                or name in (os.curdir, os.pardir):
            out.append({"id": sid, "status": "ERROR",
                        "notes": "script is not a file of the recording: %s" % name})
            continue
        script = os.path.join(rec_dir, name)
        try:
            with open(script, "rb") as fh:
                data = fh.read()
        except OSError:
            out.append({"id": sid, "status": "ERROR",
                        "notes": "script is missing: %s" % name})
            continue
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            out.append({"id": sid, "status": "ERROR",
                        "notes": "script changed since recording"})
            continue
        status, note = _run_script(node, node_modules, script, base, timeout, sid)
        out.append({"id": sid, "status": status, "notes": note})
    for entry in manifest.get("rejected") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.append({"id": entry["id"], "status": NOT_RECORDED,
                        "notes": str(entry.get("reason") or "")})
    return out


def _replay_main(cmd, args, prog, usage):
    """`replay-check SUITE_FILE RESULTS_JSON REPLAY_DIR OUT_DIR` and
    `replay DIR [--base URL] [--out RESULTS_JSON]`, the two commands that run scripts
    instead of scoring an answer. Their table and verdict lines go to stdout like the
    `results` command's; a refusal to run at all is 2 on stderr."""
    if cmd == "replay-check":
        if len(args) != 4:
            print(usage, file=sys.stderr)
            return 2
        suite_file, results_file, src_dir, out_dir = args
        try:
            manifest = replay_check(suite_file, results_file, src_dir, out_dir)
        except (ScenarioError, ReplayError) as e:
            print("%s: %s" % (prog, e), file=sys.stderr)
            return 2
        kept, rejected = manifest["scripts"], manifest["rejected"]
        print("recorded: %d of %d scenarios (rejected: %s)"
              % (len(kept), len(kept) + len(rejected),
                 ", ".join(r["id"] for r in rejected) or "none"))
        print("manifest: %s" % os.path.join(out_dir, REPLAY_MANIFEST))
        return 0
    rec_dir, base, out_json = None, None, None
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a == "--base":
            if not rest or base is not None:
                print(usage, file=sys.stderr)
                return 2
            base = rest.pop(0)
        elif a.startswith("--base="):
            if base is not None:
                print(usage, file=sys.stderr)
                return 2
            base = a.split("=", 1)[1]
        elif a == "--out":
            if not rest or out_json is not None:
                print(usage, file=sys.stderr)
                return 2
            out_json = rest.pop(0)
        elif a.startswith("--out="):
            if out_json is not None:
                print(usage, file=sys.stderr)
                return 2
            out_json = a.split("=", 1)[1]
        elif rec_dir is None and not a.startswith("-"):
            rec_dir = a
        else:
            print(usage, file=sys.stderr)
            return 2
    if rec_dir is None or base == "" or out_json == "":
        print(usage, file=sys.stderr)
        return 2
    try:
        res = replay(rec_dir, base)
    except ReplayError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    if out_json is not None:
        try:
            with open(out_json, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except OSError as e:
            print("%s: %s" % (prog, e), file=sys.stderr)
            return 2
    sys.stdout.write(summary(res, REPLAY_STATUSES))
    ran = [r for r in res if r["status"] != NOT_RECORDED]
    if not ran:
        print("%s: the recording kept no scripts: nothing was replayed" % prog,
              file=sys.stderr)
        return 9
    return 0 if all(r["status"] == "PASS" for r in ran) else 9


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    prog = "scenarios.py"
    usage = ("usage: %s check FILE | prompt FILE | results FILE ANSWER_FILE OUT_JSON"
             " | replay-check SUITE_FILE RESULTS_JSON REPLAY_DIR OUT_DIR"
             " | replay DIR [--base URL] [--out RESULTS_JSON]") % prog
    if len(argv) < 2 or argv[0] not in ("check", "prompt", "results",
                                        "replay", "replay-check"):
        print(usage, file=sys.stderr)
        return 2
    if argv[0] in ("replay", "replay-check"):
        return _replay_main(argv[0], argv[1:], prog, usage)
    want = {"check": 2, "prompt": 2, "results": 4}[argv[0]]
    if len(argv) != want:
        print(usage, file=sys.stderr)
        return 2
    try:
        suite = parse(_read(argv[1]))
    except ScenarioError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    except OSError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    if argv[0] == "check":
        print("ok: %d scenarios" % len(suite["scenarios"]))
        return 0
    if argv[0] == "prompt":
        sys.stdout.write(prompt(suite))
        return 0
    try:
        answer = _read(argv[2])
    except OSError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    res = results(answer, suite)
    try:
        with open(argv[3], "w", encoding="utf-8", newline="\n") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    print(summary(res), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
