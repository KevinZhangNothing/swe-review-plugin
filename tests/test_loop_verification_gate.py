"""An approving review must not override a failed verifier."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from swe_review import LoopSkill, VerifySkill
from swe_review.subagents.loop_agent import LoopResult, LoopSubAgent


def _skill(payload):
    class FakeSkill:
        execute = AsyncMock(return_value={"ok": True, "payload": payload})
    return FakeSkill()


def _review(decision="approve"):
    return _skill({"decision": decision, "confidence": 0.9, "defects": [],
                   "token_usage": {"prompt_tokens": 2, "completion_tokens": 3,
                                   "total_tokens": 5}})


def _verifier(status, applied=True):
    return _skill({"passed": status == "resolved", "patch_applied": applied,
                   "resolution_status": status, "confidence": 0.7,
                   "details": "verification evidence"})


@pytest.mark.parametrize("decision", ["approve", "approve_with_suggestions"])
@pytest.mark.parametrize("status,applied", [
    ("not_resolved", True), ("partially_resolved", True), ("unknown", False),
])
def test_approval_cannot_override_verification_failure(decision, status, applied):
    review = _review(decision)
    verify = _verifier(status, applied)
    revise = _skill({"diff": "unexpected revision"})
    loop = LoopSubAgent(review_skill=review, verifier_skill=verify, revise_skill=revise)
    result = asyncio.run(loop.execute({"issue": "fix", "initial_pr": {"diff": "candidate"}}))

    assert result.success is False
    assert result.final_decision == "verification_failed"
    assert result.final_pr_diff == "candidate"
    assert result.total_iterations == 2
    assert result.iterations[0].review_payload["decision"] == decision
    assert result.iterations[-1].decision == "verification_failed"
    assert result.iterations[-1].notes == "verification evidence"
    assert "verification failed" in result.message
    assert "verification evidence" in result.message
    assert result.token_usage_total["total_tokens"] == 5
    assert result.resolve_rate == (0.5 if status == "partially_resolved" else 0.0)
    review.execute.assert_awaited_once()
    verify.execute.assert_awaited_once()
    revise.execute.assert_not_awaited()


@pytest.mark.parametrize("mode", ["resolved", "unknown", "no_verifier"])
@pytest.mark.parametrize("decision", ["approve", "approve_with_suggestions"])
def test_existing_success_modes_are_preserved(mode, decision):
    verify = None if mode == "no_verifier" else _verifier(mode)
    loop = LoopSubAgent(review_skill=_review(decision), verifier_skill=verify)
    result = asyncio.run(loop.execute({"issue": "fix", "initial_pr": {"diff": "candidate"}}))
    assert result.success is True
    assert result.final_decision == decision
    assert result.final_pr_diff == "candidate"
    assert result.total_iterations == (1 if verify is None else 2)


def test_hybrid_preserves_fallback_failure_and_details(monkeypatch):
    loop = LoopSubAgent(review_skill=_review(), verifier_skill=_verifier("not_resolved"),
                        strategy="hybrid")
    # Isolate the handoff; execute the real review-guided fallback and verifier wrapper.
    monkeypatch.setattr(loop, "_run_best_of_n", AsyncMock(return_value=LoopResult(
        success=False, final_decision="reject", final_pr_diff="seed", total_iterations=0)))
    result = asyncio.run(loop.execute({"issue": "fix"}))
    assert result.strategy == "hybrid"
    assert result.success is False
    assert result.final_decision == "verification_failed"
    assert result.final_pr_diff == "seed"
    assert result.total_iterations == len(result.iterations) == 2
    assert "hybrid" in result.message
    assert "verification failed" in result.message
    assert "verification evidence" in result.message


def test_best_of_n_verify_fail_uses_verification_failed_label():
    """best_of_n verify-phase records must use verification_failed, matching
    the review_guided gate; 'review_failed' conflates review with verification."""
    class FakeVerify:
        async def execute(self, pr_diff="", **kw):
            return {"ok": True,
                    "payload": {"passed": False, "confidence": 0.1,
                                "details": "fake verify", "patch_applied": True,
                                "resolution_status": "failed"},
                    "raw": None, "message": ""}

    class FakeReview:
        async def execute(self, **kw):
            return {"ok": True,
                    "payload": {"decision": "approve", "confidence": 0.9,
                                "defects": [], "findings": [],
                                "token_usage": None},
                    "raw": None, "message": ""}

    class FakeGen:
        async def explore(self, issue, repo_path=None):
            return {}

        async def execute(self, **kw):
            return {"ok": True,
                    "payload": {"title": "t", "body": "b", "diff": "d1",
                                "rationale": "r", "confidence": 0.9},
                    "raw": None, "message": ""}

    loop = LoopSubAgent(review_skill=FakeReview(), generator_skill=FakeGen(),
                        verifier_skill=FakeVerify())
    r = asyncio.run(loop.execute({"issue": "x", "strategy": "best_of_n",
                                  "n_best_of": 2}))
    assert r.success is False
    verify_iters = [it for it in r.iterations if it.phase == "verify"]
    assert verify_iters, "expected at least one verify iteration"
    assert all(it.decision == "verification_failed" for it in verify_iters)
    assert not any(it.decision == "review_failed" for it in r.iterations)


def test_loop_skill_reports_real_workspace_failure(tmp_path):
    missing = tmp_path / "missing"
    loop = LoopSkill(review_skill=_review(), verify_skill=VerifySkill(repo_path=str(missing)))
    result = asyncio.run(loop.execute(issue="fix", repo_path=str(missing),
                                      initial_pr={"diff": "candidate"}))
    assert result.ok is False
    assert result.payload["success"] is False
    assert result.payload["final_decision"] == "verification_failed"
    assert "Failed to prepare verification workspace" in result.payload["message"]
    assert result.payload["final_pr_diff"] == "candidate"
    assert not missing.exists()
