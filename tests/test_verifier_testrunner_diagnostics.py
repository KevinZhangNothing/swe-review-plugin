"""Test-runner failures must be distinguishable from real test failures."""
import asyncio

import pytest

from swe_review import VerifySkill


def _payload(result):
    # SkillResult.ok tracks patch application, not test verdicts.
    assert result.ok is True
    return result.payload


def test_missing_explicit_runner_reports_startup_failure(repo_with_hello, minimal_diff):
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello)).execute(
        pr_diff=minimal_diff,
        test_info={"fail_to_pass": ["tests/test_x.py::test_one"]},
        test_runner=["definitely-not-a-runner", "{test}"],
    ))
    p = _payload(res)
    entry = p["test_results"][0]
    assert entry["passed"] is False
    assert entry["runner_ok"] is False
    assert "definitely-not-a-runner" in entry["stderr_tail"]
    assert "No test runner could be started" in p["details"]
    assert p["resolution_status"] == "not_resolved"


def test_all_default_runners_fail_reports_each_attempt(repo_with_hello, minimal_diff,
                                                       monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout):
        calls.append(cmd[0])
        raise FileNotFoundError(f"{cmd[0]} missing")

    import swe_review.subagents.verifier_agent as mod
    monkeypatch.setattr(mod, "_run", fake_run)
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello)).execute(
        pr_diff=minimal_diff, test_info={"fail_to_pass": ["t::a"]}))
    p = _payload(res)
    entry = p["test_results"][0]
    # calls[0] is the build check (sys.executable); the rest are runner attempts.
    assert calls[-4:] == ["python", "python", "python", "npm"]
    assert entry["runner_ok"] is False
    assert entry["error"] == "runner_startup_failed"
    assert "python" in entry["stderr_tail"]
    assert "npm" in entry["stderr_tail"]
    assert "No test runner could be started" in p["details"]


def test_timeout_marks_error_not_plain_failure(repo_with_hello, minimal_diff, monkeypatch):
    import swe_review.subagents.verifier_agent as mod

    async def fake_run(cmd, cwd, timeout):
        if cmd[:2] == ["python", "-m"]:
            return {"returncode": -1, "stdout": "", "stderr": "timeout"}
        raise FileNotFoundError("npm missing")

    monkeypatch.setattr(mod, "_run", fake_run)
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello)).execute(
        pr_diff=minimal_diff, test_info={"fail_to_pass": ["t::one"]}))
    entry = _payload(res)["test_results"][0]
    assert entry["passed"] is False
    assert entry["error"] == "timeout"
    assert entry["runner_ok"] is True
    assert "timed out" in entry["stderr_tail"]


def test_working_runner_stays_clean(repo_with_hello, minimal_diff):
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello)).execute(
        pr_diff=minimal_diff,
        test_info={"pass_to_pass": ["hello.py"]},
        test_runner=["python", "-c", "import sys; sys.exit(0)"],
    ))
    p = res.payload
    entry = p["test_results"][0]
    assert p["passed"] is True
    assert p["resolution_status"] == "resolved"
    assert entry["runner_ok"] is True
    assert entry["error"] is None
    assert "No test runner" not in p["details"]


# ---------------------------------------------------------------------------
# harness ERROR (collection/fixture) vs real test FAILURE
#
# pytest exits non-zero for both. Conflating them made a live loop run report
# `verification_failed` for a candidate whose tests were actually fine (the
# fixture ERROR came from an unwritable pytest tmp dir).
# ---------------------------------------------------------------------------

from swe_review.subagents.verifier_agent import _looks_like_harness_error  # noqa: E402


@pytest.mark.parametrize("stdout,stderr,expected", [
    ("", "", False),
    ("1 error in 1.19s", "", True),
    ("7 passed, 1 error in 1.19s", "", True),
    ("2 failed, 1 passed", "", False),          # a real failure
    ("1 failed, 1 error", "", False),           # both -> real failure wins
    ("no summary line at all", "", False),
    ("", "2 errors in 0.51s", True),
    ("0 errors", "", False),                    # no duration marker -> not a summary
    # A stray count in traceback prose is NOT a pytest summary: the old
    # keyword-counting implementation called this a harness error.
    ("", "ValueError: 3 errors occurred while reading config", False),
    ("3 errors in configuration\npassed", "", False),
])
def test_harness_error_detection(stdout, stderr, expected):
    assert _looks_like_harness_error(stdout, stderr) is expected


def test_fixture_error_marked_as_harness_error_but_still_fails_closed(
        repo_with_hello, minimal_diff, monkeypatch):
    import swe_review.subagents.verifier_agent as mod

    async def fake_run(cmd, cwd, timeout):
        return {"returncode": 1, "stdout": "7 passed, 1 error in 1.19s", "stderr": ""}

    monkeypatch.setattr(mod, "_run", fake_run)
    # build_check off so the stubbed _run only serves the test runner
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello),
                                  config={"build_check": False}).execute(
        pr_diff=minimal_diff, test_info={"fail_to_pass": ["t::a"]}))
    p = res.payload
    entry = p["test_results"][0]
    assert entry["error"] == "harness_error"
    assert entry["runner_ok"] is True
    # semantics unchanged: still fail-closed
    assert entry["passed"] is False
    assert p["passed"] is False
    assert p["resolution_status"] == "not_resolved"
    assert "ERROR (collection/fixture) rather than FAILED" in p["details"]


def test_real_test_failure_is_not_labelled_a_harness_error(
        repo_with_hello, minimal_diff, monkeypatch):
    import swe_review.subagents.verifier_agent as mod

    async def fake_run(cmd, cwd, timeout):
        return {"returncode": 1, "stdout": "1 failed, 6 passed in 0.4s", "stderr": ""}

    monkeypatch.setattr(mod, "_run", fake_run)
    # build_check off so the stubbed _run only serves the test runner
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello),
                                  config={"build_check": False}).execute(
        pr_diff=minimal_diff, test_info={"fail_to_pass": ["t::a"]}))
    p = res.payload
    assert p["test_results"][0]["error"] is None
    assert "harness" not in p["details"].lower() or "ERROR (collection" not in p["details"]
