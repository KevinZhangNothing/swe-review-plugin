"""Loop robustness: verifier metadata plumbing, honest stop reasons, reviser regen.

Three defects found by actually running the closed loop against this repository:

1. `test_info` / `test_runner` placed in the loop context were **silently
   dropped** — `_verify` forwarded only `pr_diff`/`repo_path` to the verifier.
   Every loop verify therefore degraded to a patch-application check, so
   `resolution_status` was always "unknown" and `resolve_rate` (the RRR metric)
   was structurally stuck at 0.0. The CLI had no `--test-info` either.
2. A failed revision made the loop `break` on iteration 1, yet `LoopResult.
   message` still reported "max iterations reached (N)" — an actively misleading
   explanation for why the run stopped.
3. `ReviserSubAgent` had no recovery path for an unparseable/empty answer
   (unlike `ReviewerSubAgent`, which has a bounded regen budget plus a JSON
   repair round). One flaky response aborted the entire loop.
"""
import asyncio
import json

from swe_review.subagents.loop_agent import LoopSubAgent
from swe_review.subagents.reviser_agent import ReviserSubAgent

CANDIDATE = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
TEST_INFO = {"fail_to_pass": ["tests/test_x.py::test_a"], "pass_to_pass": []}
TEST_RUNNER = ["python", "-m", "pytest", "{test}", "-q"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _ApprovingReview:
    async def execute(self, **kw):
        return {"decision": "approve", "confidence": 0.9, "defects": [],
                "findings": [], "token_usage": None}


class _RequestChangesReview:
    async def execute(self, **kw):
        return {"decision": "request_changes", "confidence": 0.8,
                "defects": [{"severity": "high", "description": "d",
                             "location": "x.py:1", "suggestion": "s"}],
                "findings": [], "token_usage": None}


class _RecordingVerifier:
    """Records the kwargs the loop actually hands the verifier."""

    def __init__(self, status="resolved"):
        self.seen = {}
        self._status = status

    async def execute(self, **kw):
        self.seen.update(kw)
        return {"passed": True, "patch_applied": True, "confidence": 0.9,
                "details": "fake", "resolution_status": self._status,
                "test_results": []}


class _FailingRevise:
    async def execute(self, **kw):
        return {"status": "failed", "diff": "", "title": "", "body": "",
                "changes_summary": "revise parse failed"}


class _OkRevise:
    async def execute(self, **kw):
        return {"status": "success", "diff": CANDIDATE, "title": "t", "body": "",
                "changes_summary": "ok"}


def _run(loop, **ctx):
    ctx.setdefault("issue", "i")
    return asyncio.run(loop.execute(ctx))


# ---------------------------------------------------------------------------
# 1. verifier metadata plumbing
# ---------------------------------------------------------------------------

def test_test_info_and_runner_reach_the_verifier():
    """Regression: they used to be dropped on the floor, leaving the verifier
    with only pr_diff/repo_path."""
    verifier = _RecordingVerifier(status="resolved")
    loop = LoopSubAgent(review_skill=_ApprovingReview(), verifier_skill=verifier,
                        max_iterations=1)
    _run(loop, repo_path=".", initial_pr={"diff": CANDIDATE},
         test_info=TEST_INFO, test_runner=TEST_RUNNER)

    assert verifier.seen["test_info"] == TEST_INFO
    assert verifier.seen["test_runner"] == TEST_RUNNER


def test_resolve_rate_reflects_a_measured_resolution():
    """Before the fix resolve_rate was structurally 0.0: status stayed 'unknown'."""
    verifier = _RecordingVerifier(status="resolved")
    loop = LoopSubAgent(review_skill=_ApprovingReview(), verifier_skill=verifier,
                        max_iterations=1)
    result = _run(loop, repo_path=".", initial_pr={"diff": CANDIDATE},
                  test_info=TEST_INFO, test_runner=TEST_RUNNER)
    assert result.resolve_rate == 1.0
    assert result.success is True


def test_metadata_reaches_verifier_in_best_of_n_too():
    verifier = _RecordingVerifier(status="partially_resolved")
    loop = LoopSubAgent(review_skill=_ApprovingReview(), verifier_skill=verifier,
                        generator_skill=None, strategy="best_of_n", n_best_of=1)

    class _Gen:
        async def explore(self, issue, repo_path=None):
            return {}

        async def execute(self, **kw):
            return {"title": "t", "body": "", "diff": CANDIDATE,
                    "rationale": "r", "confidence": 0.9}

    loop.generator_skill = _Gen()
    result = _run(loop, repo_path=".", test_info=TEST_INFO,
                  test_runner=TEST_RUNNER)
    assert verifier.seen.get("test_info") == TEST_INFO
    assert result.resolve_rate == 0.5


# ---------------------------------------------------------------------------
# 2. honest stop reason
# ---------------------------------------------------------------------------

def test_message_reports_revise_failure_not_max_iterations():
    """The run stopped at iteration 1 — 'max iterations reached (5)' was wrong."""
    loop = LoopSubAgent(review_skill=_RequestChangesReview(),
                        revise_skill=_FailingRevise(), verifier_skill=None,
                        max_iterations=5)
    result = _run(loop, initial_pr={"diff": CANDIDATE})

    assert "revise produced no usable diff" in result.message
    assert "max iterations" not in result.message
    assert result.total_iterations == 2  # review + the failed revise
    assert result.success is False


def test_message_still_reports_max_iterations_when_actually_exhausted():
    loop = LoopSubAgent(review_skill=_RequestChangesReview(),
                        revise_skill=_OkRevise(), verifier_skill=None,
                        max_iterations=2)
    result = _run(loop, initial_pr={"diff": CANDIDATE})
    assert result.message == "max iterations reached (2)"


def test_message_reports_missing_reviser():
    loop = LoopSubAgent(review_skill=_RequestChangesReview(), revise_skill=None,
                        verifier_skill=None, max_iterations=5)
    result = _run(loop, initial_pr={"diff": CANDIDATE})
    assert "no reviser configured" in result.message


# ---------------------------------------------------------------------------
# 3. reviser regen budget
# ---------------------------------------------------------------------------

VALID = json.dumps({
    "title": "t", "body": "b", "diff": CANDIDATE,
    "changes_summary": "s", "addressed_defect_indices": [0],
})


class _ScriptedAdapter:
    """Returns canned responses in order; counts calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, system, user, max_tokens=4096, temperature=0.1):
        self.calls += 1
        return self.responses.pop(0), {"total_tokens": 1}


def _revise(adapter, **kw):
    rev = ReviserSubAgent(tool_adapter=adapter, prompt_style="concise",
                          regen_backoff_seconds=0, **kw)
    return asyncio.run(rev.execute({
        "issue": "i", "original_pr_title": "t", "original_pr_body": "",
        "original_pr_diff": CANDIDATE,
        "review_report": {"decision": "request_changes",
                          "defects": [{"severity": "high", "description": "d"}]},
    }))


def test_reviser_retries_after_unparseable_response():
    adapter = _ScriptedAdapter(["this is not JSON at all", VALID])
    res = _revise(adapter)
    assert adapter.calls == 2
    assert res.status == "success"
    assert res.diff.startswith("--- ")


def test_reviser_does_not_retry_when_first_attempt_succeeds():
    adapter = _ScriptedAdapter([VALID, VALID])
    res = _revise(adapter)
    assert adapter.calls == 1
    assert res.status == "success"


def test_reviser_gives_up_after_budget():
    adapter = _ScriptedAdapter(["bad", "still bad", "never used"])
    res = _revise(adapter, max_regen_attempts=2)
    assert adapter.calls == 2
    assert res.status == "failed"
    assert res.diff == ""


def test_reviser_retries_on_valid_json_with_empty_diff():
    """A degenerate-but-parseable answer deserves the same second chance."""
    empty = json.dumps({"title": "t", "body": "b", "diff": "",
                        "changes_summary": "nothing"})
    adapter = _ScriptedAdapter([empty, VALID])
    res = _revise(adapter)
    assert adapter.calls == 2
    assert res.status == "success"
