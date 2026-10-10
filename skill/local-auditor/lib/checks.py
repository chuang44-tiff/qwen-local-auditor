"""Run a task's checks. The HARNESS runs these, never the model."""
import shlex
from dataclasses import dataclass

from lib import testrun


@dataclass(frozen=True)
class CheckResult:
    index: int
    text: str
    status: str      # PASS | FAIL | UNVERIFIED
    evidence: str
    # An excerpt of a failing check's output, for the coder's next prompt (the report
    # keeps the one-line evidence). Empty unless the check failed.
    detail: str = ""


DETAIL_BYTES = 3000


def _cmd(item, repo, timeout):
    rc, out = testrun._run(testrun.split_command(item.arg), repo, timeout, "check")
    last = next((l.strip() for l in reversed(out.splitlines()) if l.strip()), "")
    if rc is None:
        return CheckResult(item.index, item.text, "FAIL", "CMD %s TIMEOUT after %ds" % (item.arg, timeout),
                           testrun._trim(out, DETAIL_BYTES))
    status = "PASS" if rc == 0 else "FAIL"
    return CheckResult(item.index, item.text, status,
                       "CMD %s exit %d%s" % (item.arg, rc, (": " + last[:200]) if last else ""),
                       testrun._trim(out, DETAIL_BYTES) if status == "FAIL" else "")


def run_checks(items, repo, *, test_cmd, timeout):
    results, wt = [], None
    try:
        for it in items:
            if it.kind == "none":
                results.append(CheckResult(it.index, it.text, "UNVERIFIED", "no check given"))
            elif it.kind == "cmd":
                results.append(_cmd(it, repo, timeout))
            else:
                if wt is None:
                    wt = testrun.prepare(testrun.toplevel(repo))
                try:
                    code, text = testrun.run_tests(shlex.split(it.arg), cmd=test_cmd, source=repo,
                                                   worktree=wt, timeout=timeout, max_bytes=DETAIL_BYTES)
                except ValueError as exc:
                    code, text = 1, "TEST %s ERROR: %s" % (it.arg, exc)
                head, _, rest = text.partition("\n")
                results.append(CheckResult(it.index, it.text, "PASS" if code == 0 else "FAIL",
                                           head, rest if code != 0 else ""))
    finally:
        if wt is not None:
            testrun.remove_worktree(testrun.toplevel(repo), wt)
    return results
