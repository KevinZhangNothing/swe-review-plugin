"""Tests for subagents — focus on forbidden-context guards & basic dataclass flow."""

import pytest

from swe_review.subagents.reviewer_agent import (
    ReviewerSubAgent, ReviewReport, Defect, Finding,
    _parse_engineering_payload, _normalize_decision,
)
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


# ---------------------------------------------------------------------------
# Engineering prompt style (senior code-quality review)
# ---------------------------------------------------------------------------

ENGINEERING_PAYLOAD = {
    "decision": "REQUEST_CHANGES",
    "confidence": 0.9,
    "summary": {
        "problem": "重构设备状态计算逻辑",
        "solution": "拆分 calculateState 并引入缓存写入",
        "overall_assessment": "设计方向正确，但职责耦合明显",
    },
    "scores": {
        "design_quality": {"score": 12, "max": 20, "reason": "职责耦合"},
        "maintainability": {"score": 9, "max": 15, "reason": "高耦合"},
        "consistency": {"score": 13, "max": 15, "reason": "符合项目分层"},
        "simplicity": 7,
        "readability": {"score": 8, "max": 10, "reason": "命名清晰"},
        "testability": {"score": 5, "max": 10, "reason": "副作用难以 mock"},
        "risk": {"score": 6, "max": 10, "reason": "状态来源分散"},
        "change_scope": {"score": 9, "max": 10, "reason": "范围克制"},
    },
    "total_score": 69,
    "hard_gate": {"triggered": False, "reason": ""},
    "findings": [
        {
            "severity": "P1", "title": "状态计算与副作用耦合",
            "location": "lib/device_manager.dart:120-145",
            "observation": "同一方法负责状态计算、缓存写入与事件通知",
            "why_it_matters": "未来修改状态规则会同时影响缓存与事件分发",
            "evidence": "calculateState() / saveCache() / notifyListeners()",
            "recommendation": "拆分纯计算与副作用",
            "confidence": "High",
        },
        {
            "severity": "P3", "title": "命名缩写",
            "location": "lib/device_manager.dart:88",
            "observation": "dm 变量名", "why_it_matters": "轻微",
            "evidence": "var dm = ...", "recommendation": "可改为 deviceManager",
            "confidence": "low",
        },
    ],
}


def test_engineering_payload_parse_and_defect_mapping():
    r = _parse_engineering_payload(ENGINEERING_PAYLOAD, {"prompt_tokens": 10, "completion_tokens": 20})
    d = r.to_dict()
    assert d["decision"] == "request_changes"
    assert d["prompt_style"] == "engineering"
    assert d["total_score"] == 69.0
    assert len(d["findings"]) == 2
    # scores normalized: bare number tolerated, max re-asserted from schema
    assert d["scores"]["simplicity"] == {"score": 7, "max": 10, "reason": ""}
    assert d["scores"]["design_quality"]["max"] == 20
    # findings mapped onto legacy defects for downstream revise compatibility
    assert [x["severity"] for x in d["defects"]] == ["high", "low"]
    assert d["defects"][0]["location"] == "lib/device_manager.dart:120-145"
    # finding confidence normalized to lowercase enum
    assert r.findings[0].confidence == "high"
    # deep schema: location parsed into {path, start_line, end_line}
    deep = r.to_dict(deep=True)
    assert deep["findings"][0]["location"] == {
        "path": "lib/device_manager.dart", "start_line": 120, "end_line": 145,
        "function": None,
    }


def test_engineering_hard_gate_forces_block():
    payload = dict(ENGINEERING_PAYLOAD)
    payload["decision"] = "APPROVE"  # model says approve…
    payload["hard_gate"] = {"triggered": True, "reason": "Critical Security Risk"}
    r = _parse_engineering_payload(payload, None)
    assert r.decision == "block"  # …but hard gate wins


def test_engineering_p0_finding_forces_block():
    """Prompt rule: P0 must block merge — enforced even if the model says APPROVE."""
    import copy
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    payload["decision"] = "APPROVE"
    payload["hard_gate"] = {"triggered": False, "reason": ""}
    payload["findings"][0]["severity"] = "P0"
    r = _parse_engineering_payload(payload, None)
    assert r.decision == "block"


def test_engineering_p1_downgrades_approving_decision():
    """Prompt rule: one or more P1 ⇒ REQUEST_CHANGES, for any approving decision."""
    import copy
    for approving in ("APPROVE", "APPROVE_WITH_SUGGESTIONS"):
        payload = copy.deepcopy(ENGINEERING_PAYLOAD)
        payload["decision"] = approving
        r = _parse_engineering_payload(payload, None)
        assert r.decision == "request_changes", approving
    # non-approving decisions are left alone (block stays block)
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    payload["decision"] = "BLOCK"
    assert _parse_engineering_payload(payload, None).decision == "block"


def test_finding_to_defect_carries_impact_and_evidence():
    """The revise chain must receive evidence-rich feedback, not a weakened copy."""
    f = Finding(
        severity="P1", title="职责耦合",
        location="a/b.py:10",
        observation="一个方法做三件事",
        why_it_matters="修改状态规则会连带影响缓存与事件分发",
        evidence="calculateState() / saveCache() / notifyListeners()",
        recommendation="拆分纯计算与副作用",
    )
    d = f.to_defect()
    assert d.severity == "high"
    assert "一个方法做三件事" in d.description
    assert "[impact: 修改状态规则会连带影响缓存与事件分发]" in d.description
    assert "[evidence: calculateState() / saveCache() / notifyListeners()]" in d.description
    assert d.suggestion == "拆分纯计算与副作用"


def test_engineering_decision_normalization():
    assert _normalize_decision("APPROVE") == "approve"
    assert _normalize_decision("approve_with_suggestions") == "approve_with_suggestions"
    assert _normalize_decision("Approve With Suggestions") == "approve_with_suggestions"
    assert _normalize_decision("BLOCK") == "block"
    assert _normalize_decision("request-changes") == "request_changes"
    assert _normalize_decision("lol") == "request_changes"
    assert _normalize_decision(None) == "request_changes"


def test_engineering_system_prompt_contract():
    from swe_review.subagents import engineering_prompt
    sp = engineering_prompt.system_prompt()
    # framework pillars present
    for marker in (
        "Design Quality", "Maintainability", "Consistency", "Simplicity",
        "Readability", "Testability", "Risk", "Change Scope",
        "P0", "P4", "Hard Gate", "Project Convention",
        "Over-engineering", "Under-engineering",
        "APPROVE_WITH_SUGGESTIONS", "BLOCK",
    ):
        assert marker in sp, f"missing: {marker}"
    # STRICT JSON output contract
    assert '"total_score"' in sp and '"findings"' in sp and '"hard_gate"' in sp


def test_reviewer_execute_engineering_end_to_end():
    """ReviewerSubAgent(prompt_style='engineering') routes prompts and parses the
    engineering JSON contract end-to-end via a stub tool adapter."""
    import asyncio, json as _json

    class StubAdapter:
        def __init__(self):
            self.calls = []

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
            return _json.dumps(ENGINEERING_PAYLOAD), {"prompt_tokens": 5, "completion_tokens": 7}

    adapter = StubAdapter()
    sub = ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering")
    report = asyncio.run(sub.execute({
        "issue": "重构设备状态模块",
        "pr_title": "refactor: split device state",
        "pr_diff": "diff --git a/lib/device_manager.dart b/lib/device_manager.dart\n"
                   "@@ -1 +1,2 @@\n+x\n",
    }))
    assert report.prompt_style == "engineering"
    assert report.decision == "request_changes"
    assert report.total_score == 69.0
    assert len(report.findings) == 2
    # engineering reports get the larger completion budget
    assert adapter.calls[0]["max_tokens"] == 8192
    assert "Code Review Agent System Prompt" in adapter.calls[0]["system"]


def test_reviser_maps_engineering_style_to_detailed():
    sub = ReviserSubAgent(tool_adapter=None, prompt_style="engineering")
    assert sub.prompt_style == "detailed"


def test_loop_approves_with_suggestions(sample_diff):
    """approve_with_suggestions must terminate the loop as a success."""
    import asyncio

    class FakeReviewSkill:
        async def execute(self, **kwargs):
            class Out:
                def to_dict(self_inner, deep=False):
                    return {
                        "decision": "approve_with_suggestions",
                        "confidence": 0.88,
                        "defects": [],
                        "findings": [],
                        "token_usage": None,
                    }
            return Out()

    loop = LoopSubAgent(review_skill=FakeReviewSkill(), max_iterations=3)
    result = asyncio.run(loop.execute({"issue": "x", "initial_pr": {"diff": sample_diff}}))
    assert result.success is True
    assert result.final_decision == "approve_with_suggestions"


def test_loop_passes_real_decision_to_reviser(sample_diff):
    """_revise must forward the actual review decision (e.g. block), not a
    hardcoded request_changes."""
    import asyncio

    class FakeReviewSkill:
        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            decision = "block" if self.calls == 1 else "approve"

            class Out:
                def to_dict(self_inner, deep=False):
                    return {
                        "decision": decision,
                        "confidence": 0.8,
                        "defects": [{"severity": "high", "description": "d",
                                     "location": "a.py:1", "suggestion": "s"}],
                        "findings": [],
                        "token_usage": None,
                    }
            return Out()

    class FakeReviseSkill:
        def __init__(self):
            self.captured = None

        async def execute(self, **kwargs):
            self.captured = kwargs
            return {"title": "t", "body": "b", "diff": sample_diff,
                    "changes_summary": "fixed"}

    revise = FakeReviseSkill()
    loop = LoopSubAgent(review_skill=FakeReviewSkill(), revise_skill=revise,
                        max_iterations=3)
    result = asyncio.run(loop.execute({"issue": "x", "initial_pr": {"diff": sample_diff}}))
    assert result.success is True
    assert revise.captured is not None
    assert revise.captured["review_report"]["decision"] == "block"


# ---------------------------------------------------------------------------
# Lenient JSON parsing + one-shot LLM repair round
# ---------------------------------------------------------------------------

def test_best_effort_json_direct_brace_span_and_garbage():
    from swe_review.subagents.reviewer_agent import _best_effort_json
    assert _best_effort_json('{"a": 1}') == {"a": 1}
    assert _best_effort_json('noise before {"a": 1} noise after') == {"a": 1}
    assert _best_effort_json("") is None
    assert _best_effort_json("not json at all") is None


def test_best_effort_json_truncation_repair():
    import json as _json
    from swe_review.subagents.reviewer_agent import _best_effort_json
    full = _json.dumps(ENGINEERING_PAYLOAD, ensure_ascii=False)
    truncated = full[: len(full) - 200]  # cut into the tail
    obj = _best_effort_json(truncated)
    assert obj is not None
    assert obj.get("decision") == "REQUEST_CHANGES"
    # the leading findings survive the truncation repair
    assert len(obj.get("findings", [])) >= 1


def test_execute_repairs_malformed_json_with_one_retry():
    """Broken JSON on first call ⇒ exactly one LLM repair round; merged tokens."""
    import asyncio, json as _json

    class StubAdapter:
        def __init__(self):
            self.calls = []

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls.append({"system": system, "user": user})
            if len(self.calls) == 1:
                # missing comma — deterministic heuristics cannot fix this
                return '{"decision": "APPROVE" "confidence": 0.9}', \
                    {"prompt_tokens": 3, "completion_tokens": 2}
            return _json.dumps({"decision": "APPROVE", "confidence": 0.9,
                                "summary": {}, "findings": []}), \
                {"prompt_tokens": 4, "completion_tokens": 5}

    adapter = StubAdapter()
    sub = ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering")
    report = asyncio.run(sub.execute({
        "issue": "x", "pr_title": "t", "pr_diff": "diff --git a/a b/a\n",
    }))
    assert report.parse_error is None
    assert report.decision == "approve"
    assert len(adapter.calls) == 2  # exactly one repair round
    assert "malformed JSON" in adapter.calls[1]["system"]
    assert report.token_usage == {"prompt_tokens": 7, "completion_tokens": 7}


def test_parse_error_surfaced_when_unrepairable():
    import asyncio

    class GarbageAdapter:
        def __init__(self):
            self.calls = 0

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls += 1
            return "definitely not json {{{", {"prompt_tokens": 1, "completion_tokens": 1}

    adapter = GarbageAdapter()
    sub = ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering")
    report = asyncio.run(sub.execute({
        "issue": "x", "pr_title": "t", "pr_diff": "diff --git a/a b/a\n",
    }))
    assert report.parse_error is not None
    assert report.decision == "request_changes"
    assert adapter.calls == 2  # one repair attempt, then give up
    assert report.to_dict()["parse_error"] is not None
