"""Tests for subagents — focus on forbidden-context guards & basic dataclass flow."""

import pytest

from swe_review.subagents.reviewer_agent import ReviewerSubAgent, ReviewReport, Defect
from swe_review.subagents.reviser_agent import ReviserSubAgent
from swe_review.subagents.explorer_agent import ExplorerSubAgent
from swe_review.subagents.verifier_agent import VerifierSubAgent
from swe_review.subagents.analyzer_agent import AnalyzerSubAgent
from swe_review.subagents.generator_agent import GeneratorSubAgent
from swe_review.subagents.loop_agent import LoopSubAgent


def test_reviewer_rejects_golden_patch(sample_diff):
    sub = ReviewerSubAgent(tool_adapter=None)
    with pytest.raises(ValueError, match="golden_patch"):
        import asyncio
        asyncio.run(sub.execute({
            "issue": "Bug",
            "pr_title": "Fix",
            "pr_diff": sample_diff,
            "golden_patch": "anything",  # MUST be rejected
        }))


def test_reviewer_rejects_test_info(sample_diff):
    sub = ReviewerSubAgent(tool_adapter=None)
    with pytest.raises(ValueError, match="test_info"):
        import asyncio
        asyncio.run(sub.execute({
            "issue": "Bug",
            "pr_title": "Fix",
            "pr_diff": sample_diff,
            "test_info": {"fail_to_pass": [], "pass_to_pass": []},
        }))


def test_reviser_rejects_oracle(sample_diff):
    sub = ReviserSubAgent(tool_adapter=None)
    with pytest.raises(ValueError, match="oracle|golden_patch"):
        import asyncio
        asyncio.run(sub.execute({
            "issue": "Bug",
            "original_pr_title": "Fix",
            "original_pr_diff": sample_diff,
            "review_report": {"defects": []},
            "oracle": "anything",
        }))


def test_generator_rejects_oracle():
    sub = GeneratorSubAgent(tool_adapter=None)
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(sub.execute({
            "issue": "Bug",
            "golden_patch": "anything",
        }))


def test_explorer_static_diff(sample_diff):
    sub = ExplorerSubAgent(repo_path=".")
    import asyncio
    result = asyncio.run(sub.execute({
        "issue": "NullPointerException in foo add function",
        "pr_diff": sample_diff,
        "max_steps": 4,
    }))
    assert "foo.py" in result.files_modified
    assert len(result.keywords) > 0  # at least some keyword extracted
    assert len(result.root_hint) > 0  # at least function:foo or keyword:*


def test_analyzer_counts_add_del(sample_diff):
    sub = AnalyzerSubAgent()
    import asyncio
    result = asyncio.run(sub.execute({"pr_diff": sample_diff}))
    assert result.total_additions >= 1 and result.total_deletions >= 1
    assert 0.0 <= result.complexity_score <= 1.0


def test_reviewer_review_report_dataclass():
    r = ReviewReport(
        decision="approve", confidence=0.9,
        summary={"problem": "x", "solution": "y"},
        defects=[Defect("low", "n", "p.py", "s")],
    )
    d = r.to_dict()
    assert d["decision"] == "approve"
    assert d["defects"][0]["severity"] == "low"


def test_loop_rejects_golden_patch():
    sub = LoopSubAgent()
    import asyncio
    with pytest.raises(ValueError, match="golden_patch"):
        asyncio.run(sub.execute({"issue": "x", "golden_patch": "y"}))


# ---------------------------------------------------------------------------
# Schema / prompt-style / feedback-level additions
# ---------------------------------------------------------------------------

def test_reviewer_unknown_prompt_style_raises():
    with pytest.raises(ValueError, match="prompt_style"):
        ReviewerSubAgent(tool_adapter=None, prompt_style="lol")


def test_reviser_unknown_feedback_level_raises():
    with pytest.raises(ValueError, match="feedback_level"):
        ReviserSubAgent(tool_adapter=None, feedback_level="lol")


def test_review_report_flat_and_deep_schemas():
    """to_dict() (default) vs to_dict(deep=True) should both serialize, with
    different shapes — flat for daily use, deep for downstream SFT / eval."""
    r = ReviewReport(
        decision="request_changes",
        confidence=0.92,
        summary={"problem": "x", "solution": "y", "overall_assessment": "z"},
        defects=[Defect(
            severity="high", category="correctness",
            description="bug",
            location={"path": "src/foo.py", "start_line": 42, "end_line": 42},
            suggestion="fix it",
        )],
    )
    flat = r.to_dict()
    deep = r.to_dict(deep=True)
    # Flat: decision is a string, location is "path:line" string
    assert flat["decision"] == "request_changes"
    assert flat["defects"][0]["location"] == "src/foo.py:42"
    # Deep: decision is {recommendation, confidence}; location is dict
    assert deep["decision"]["recommendation"] == "request_changes"
    assert deep["decision"]["confidence"] == 0.92
    assert deep["defects"][0]["location"]["path"] == "src/foo.py"
    assert deep["defects"][0]["location"]["start_line"] == 42
    assert deep["defects"][0]["category"] == "correctness"


def test_review_report_to_dict_handles_string_location():
    """Calling to_dict(deep=True) on a string location must still parse it."""
    r = ReviewReport(
        decision="request_changes", confidence=0.5,
        defects=[Defect("medium", "x", "src/bar.py:10-15", "y")],
    )
    deep = r.to_dict(deep=True)
    assert deep["defects"][0]["location"]["path"] == "src/bar.py"
    assert deep["defects"][0]["location"]["start_line"] == 10
    assert deep["defects"][0]["location"]["end_line"] == 15


def test_reviser_accepts_all_feedback_levels():
    """ReviserSubAgent constructor + each user-prompt branch must work."""
    for level in ("full_feedback", "minimal_feedback", "baseline"):
        ReviserSubAgent(tool_adapter=None, feedback_level=level)
    # the user-prompt builder itself
    from swe_review.subagents.reviser_agent import _build_user_prompt_detailed
    for level, has_feedback in (("full_feedback", True), ("minimal_feedback", False), ("baseline", False)):
        p = _build_user_prompt_detailed(
            issue="Bug", original_title="t", original_body="b",
            original_diff="diff --git a/x b/x",
            review_report={"defects": []} if has_feedback else {},
            feedback_level=level,
        )
        assert isinstance(p, str) and len(p) > 0


def test_reviewer_prompt_style_routes_to_correct_builder():
    """Sanity check: each prompt_style produces a different system prompt length
    (detailed is longer because of Step 1-6 workflow + symptom-fix rules)."""
    from swe_review.subagents.reviewer_agent import (
        _system_prompt_concise, _system_prompt_detailed,
    )
    c = _system_prompt_concise()
    d = _system_prompt_detailed()
    assert len(d) > len(c) * 1.5
    # Both name their distinct fields
    assert "decision" in c and "summary" in c
    assert "Step 1" in d and "symptom" in d.lower()


def test_defect_new_fields_serialize():
    """Defect.category must round-trip through to_dict(deep=True)."""
    d = Defect("high", "d", "p.py:1", "s", category="security")
    r = ReviewReport(decision="approve", confidence=0.8,
                     defects=[d])
    flat = r.to_dict()["defects"][0]
    # flat schema intentionally does NOT include category (daily-use minimal)
    assert "category" not in flat
    # deep schema DOES include category
    assert r.to_dict(deep=True)["defects"][0]["category"] == "security"
