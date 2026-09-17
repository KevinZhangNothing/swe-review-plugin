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
from swe_review.subagents import engineering_prompt
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


def test_engineering_optional_feedback_fields_roundtrip():
    """whats_good / recommended_actions are optional; tolerate string or list,
    default to empty, and serialize in both flat and deep to_dict()."""
    import copy
    # absent → empty defaults
    r = _parse_engineering_payload(ENGINEERING_PAYLOAD, None)
    assert r.whats_good == [] and r.recommended_actions == []
    assert r.to_dict()["whats_good"] == [] and r.to_dict(deep=True)["recommended_actions"] == []
    # list payload round-trips
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    payload["whats_good"] = ["清晰的职责拆分", "测试覆盖了异常路径"]
    payload["recommended_actions"] = ["先拆分 calculateState", "补充缓存写入的单测"]
    r = _parse_engineering_payload(payload, None)
    assert r.to_dict()["whats_good"] == ["清晰的职责拆分", "测试覆盖了异常路径"]
    assert r.to_dict(deep=True)["recommended_actions"][0] == "先拆分 calculateState"
    # bare string tolerated
    payload["whats_good"] = "结构清晰"
    assert _parse_engineering_payload(payload, None).whats_good == ["结构清晰"]


def test_engineering_language_checklist_trimming():
    """Section 十六 is trimmed to the languages present in the diff (P3 self-review
    finding: fixed prompt token cost). Default keeps full coverage."""
    full = engineering_prompt.system_prompt()
    assert "**Go**" in full and "**Rust**" in full and "**SQL**" in full
    trimmed = engineering_prompt.system_prompt(languages=["Python"])
    assert "**Python**" in trimmed
    assert "**Go**" not in trimmed and "**Rust**" not in trimmed
    assert "**JavaScript/TypeScript**" not in trimmed
    assert "其他/未识别语言" in trimmed  # universal fallback always kept
    assert len(trimmed) < len(full)
    # explicit empty → fallback only
    assert "**Python**" not in engineering_prompt.system_prompt(languages=[])


def test_engineering_detect_languages_from_diff():
    diff = (
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "--- a/web/index.tsx\n+++ b/web/index.tsx\n"
        "--- a/README.md\n+++ b/README.md\n"
    )
    langs = engineering_prompt.detect_languages(diff)
    assert langs == ["Python", "JavaScript/TypeScript"]
    assert engineering_prompt.detect_languages("") == []
    # non-standard diff shapes (P3 self-review finding): quoted paths,
    # --no-prefix diffs, rename-only entries, and /dev/null must not silently
    # drop language coverage
    assert engineering_prompt.detect_languages('+++ "b/dir/my file.py"\n') == ["Python"]
    assert engineering_prompt.detect_languages("--- main.rs\n+++ main.rs\n") == ["Rust"]
    assert engineering_prompt.detect_languages(
        "diff --git a/old.go b/new.go\nrename from old.go\nrename to new.go\n"
    ) == ["Go"]
    assert engineering_prompt.detect_languages("--- /dev/null\n+++ b/new.sql\n") == ["SQL"]
    # diff --git header: quoted paths and paths containing " b/" (loop P3 finding)
    assert engineering_prompt.detect_languages(
        'diff --git "a/old name.go" "b/new name.go"\n'
    ) == ["Go"]
    assert engineering_prompt.detect_languages(
        "diff --git a/x b/y b/z.go\n"
    ) == ["Go"]  # last token wins; the " b/" inside the path is not a delimiter


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


def test_engineering_total_score_recomputed_from_dimensions():
    """total_score must equal the sum of the 8 dimensions — per-dimension scores
    are the evidence-based source of truth."""
    import copy
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    payload["total_score"] = 95  # model claims more than its own dimensions add up to
    r = _parse_engineering_payload(payload, None)
    assert r.total_score == 69.0  # 12+9+13+7+8+5+6+9


def test_engineering_incomplete_dimensions_invalidate_total():
    """With dimensions missing, the model's self-reported total is not trusted."""
    import copy
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    del payload["scores"]["risk"]
    payload["total_score"] = 95
    r = _parse_engineering_payload(payload, None)
    assert r.total_score is None


def test_truncate_json_text_cuts_at_newline_boundary():
    from swe_review.subagents.engineering_prompt import truncate_json_text
    short = '{"a": 1}'
    assert truncate_json_text(short) == short
    long_text = '{\n' + ',\n'.join(f'"k{i}": "v{i}"' for i in range(5000)) + '\n}'
    cut = truncate_json_text(long_text, limit=1000)
    assert cut.endswith("... (truncated)")
    body = cut[: -len("... (truncated)")]
    assert body.endswith("\n")  # cut at a line boundary, not mid-token
    assert len(body) <= 1000 + 1


def test_engineering_unknown_severity_is_conservative():
    """Unrecognized severity must not be silently demoted below the block gate."""
    import copy
    payload = copy.deepcopy(ENGINEERING_PAYLOAD)
    payload["findings"][0]["severity"] = "critical"  # not in P0..P4
    r = _parse_engineering_payload(payload, None)
    assert r.findings[0].severity == "P1"
    assert r.defects[0].severity == "high"


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
    assert _best_effort_json('{"a": 1}') == ({"a": 1}, False)
    assert _best_effort_json('noise before {"a": 1} noise after') == ({"a": 1}, False)
    assert _best_effort_json("") == (None, False)
    assert _best_effort_json("not json at all") == (None, False)


def test_best_effort_json_truncation_repair():
    import json as _json
    from swe_review.subagents.reviewer_agent import _best_effort_json
    full = _json.dumps(ENGINEERING_PAYLOAD, ensure_ascii=False)
    truncated = full[: len(full) - 200]  # cut into the tail
    obj, repaired = _best_effort_json(truncated)
    assert obj is not None and repaired is True
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


def test_execute_truncation_triggers_repair_round():
    """A repaired (truncated) parse must NOT be accepted silently — the tail may
    hold findings (possibly a P0), so one LLM repair round still runs."""
    import asyncio, json as _json

    full = _json.dumps(ENGINEERING_PAYLOAD, ensure_ascii=False)
    truncated = full[: len(full) - 200]

    class StubAdapter:
        def __init__(self):
            self.calls = []

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls.append(system)
            if len(self.calls) == 1:
                return truncated, {"prompt_tokens": 3, "completion_tokens": 2}
            return full, {"prompt_tokens": 4, "completion_tokens": 5}

    adapter = StubAdapter()
    sub = ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering")
    report = asyncio.run(sub.execute({
        "issue": "x", "pr_title": "t", "pr_diff": "diff --git a/a b/a\n",
    }))
    assert len(adapter.calls) == 2                       # repair round fired
    # Trust follows content provenance: original output was truncated, so the
    # final (repaired) report stays untrusted even though it parsed cleanly.
    assert report.truncated_repair is True
    assert len(report.findings) == len(ENGINEERING_PAYLOAD["findings"])


def test_parse_error_surfaced_when_unrepairable():
    import asyncio

    class GarbageAdapter:
        def __init__(self):
            self.calls = 0

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls += 1
            return "definitely not json {{{", {"prompt_tokens": 1, "completion_tokens": 1}

    adapter = GarbageAdapter()
    sub = ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering",
                           regen_backoff_seconds=0.0)
    report = asyncio.run(sub.execute({
        "issue": "x", "pr_title": "t", "pr_diff": "diff --git a/a b/a\n",
    }))
    assert report.parse_error is not None
    assert report.decision == "request_changes"
    # bounded regen retries: max_regen_attempts x (original + repair round)
    assert adapter.calls == 6
    assert report.to_dict()["parse_error"] is not None
    # no attempt's token cost may be lost (6 calls x {1,1})
    assert report.token_usage == {"prompt_tokens": 6, "completion_tokens": 6}


def test_revise_status_survives_loop_unwrap():
    """Round-5 finding: LoopSubAgent._revise used to drop the revise result's
    status, so failure notes always said status=no_output."""
    import asyncio

    class FakeReviseSkill:
        async def execute(self, **kw):
            return {"ok": False,
                    "payload": {"title": "t", "body": "b", "diff": "",
                                "changes_summary": "", "status": "failed",
                                "addressed_defect_indices": []},
                    "raw": None, "message": "status=failed"}

    loop = LoopSubAgent(revise_skill=FakeReviseSkill())
    out = asyncio.run(loop._revise(
        "issue", {"title": "", "body": "", "diff": "d"}, [], None))
    assert out is not None
    assert out.get("status") == "failed"


def test_verify_without_tests_is_weak_pass():
    """Round-5 finding: with no test_info the verifier only applies the patch;
    that must read as a weak pass (patch applied), not 'review_failed'."""
    import asyncio

    class FakeVerifierSkill:
        async def execute(self, **kw):
            return {"ok": True,
                    "payload": {"passed": False, "confidence": 0.0,
                                "details": "No tests executed",
                                "patch_applied": True,
                                "resolution_status": "unknown"},
                    "raw": None, "message": ""}

    loop = LoopSubAgent(verifier_skill=FakeVerifierSkill())
    rr = asyncio.run(loop._verify({"diff": "x"}, None))
    assert rr is not None
    assert rr["passed"] is True
    assert rr["confidence"] == 0.5


def test_hybrid_forwards_runtime_max_iterations():
    """Round-5 self-review: hybrid phase must honor the runtime max_iterations
    override instead of hardcoding self.max_iterations."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopResult

    loop = LoopSubAgent()
    captured = {}

    async def fake_bon(issue, repo_path, n, t0, prompt_style=None,
                       test_info=None, test_runner=None):
        return LoopResult(success=False, final_decision="reject",
                          final_pr_diff="d", total_iterations=0,
                          strategy="best_of_n")

    async def fake_rg(issue, repo_path, initial_pr, max_iter, t0,
                      prompt_style=None, feedback_level=None,
                      test_info=None, test_runner=None):
        captured["max_iter"] = max_iter
        return LoopResult(success=True, final_decision="approve",
                          final_pr_diff="d", total_iterations=1,
                          strategy="review_guided")

    loop._run_best_of_n = fake_bon
    loop._run_review_guided = fake_rg
    asyncio.run(loop.execute({"issue": "x", "strategy": "hybrid",
                              "max_iterations": 7}))
    assert captured["max_iter"] == 7


def test_engineering_legacy_shape_fallback():
    """Round-8 P3: if the model answers in the legacy defects[] shape, the
    feedback must not be silently dropped."""
    payload = {
        "decision": "REQUEST_CHANGES",
        "confidence": 0.8,
        "summary": {},
        "defects": [
            {"severity": "high", "description": "legacy bug",
             "location": "a.py:3", "suggestion": "fix it"},
        ],
        "findings": [],
    }
    r = _parse_engineering_payload(payload, None)
    assert r.decision == "request_changes"
    assert len(r.defects) == 1 and r.defects[0].severity == "high"


def test_loop_stops_on_unparseable_review(sample_diff):
    """Round-6: a persistently unparseable review carries no actionable feedback —
    stop early instead of blind-revising until max_iterations."""
    import asyncio

    class UnparseableReviewSkill:
        async def execute(self, **kwargs):
            class Out:
                def to_dict(self_inner, deep=False):
                    return {"decision": "request_changes", "confidence": 0.5,
                            "defects": [], "findings": [], "token_usage": None,
                            "parse_error": "not valid JSON"}
            return Out()

    class FakeReviseSkill:
        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return {"title": "t", "body": "b", "diff": sample_diff,
                    "changes_summary": "fixed"}

    revise = FakeReviseSkill()
    loop = LoopSubAgent(review_skill=UnparseableReviewSkill(),
                        revise_skill=revise, max_iterations=5)
    result = asyncio.run(loop.execute({"issue": "x",
                                       "initial_pr": {"diff": sample_diff}}))
    assert result.success is False
    assert result.final_decision == "review_unparseable"
    assert "unparseable" in result.message
    assert revise.calls == 0  # no blind revision


def test_review_skill_ok_false_on_parse_error():
    """Round-6: ReviewSkill.execute must not report ok=True for a fallback
    report produced by unparseable model output."""
    import asyncio
    from swe_review import ReviewSkill

    class GarbageAdapter:
        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            return "not json {{{", {"prompt_tokens": 1, "completion_tokens": 1}

    skill = ReviewSkill(tool_adapter=GarbageAdapter())
    res = asyncio.run(skill.execute(issue="x", pr_title="t",
                                    pr_diff="diff --git a/a b/a\n"))
    assert res.ok is False
    assert res.payload["parse_error"] is not None
    assert "parse_error" in res.message


def test_loop_withholds_approve_on_untrusted_review(sample_diff):
    """Round-5 self-review: an approving review whose output was truncated
    (tail findings possibly lost, possibly a P0) must NOT terminate the loop
    as success — force another revision round instead."""
    import asyncio

    class UntrustedApproveSkill:
        async def execute(self, **kwargs):
            class Out:
                def to_dict(self_inner, deep=False):
                    return {"decision": "approve", "confidence": 0.9,
                            "defects": [], "findings": [], "token_usage": None,
                            "truncated_repair": True, "parse_error": None}
            return Out()

    class FakeReviseSkill:
        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return {"title": "t", "body": "b", "diff": sample_diff,
                    "changes_summary": "fixed"}

    revise = FakeReviseSkill()
    loop = LoopSubAgent(review_skill=UntrustedApproveSkill(),
                        revise_skill=revise, max_iterations=2)
    result = asyncio.run(loop.execute({"issue": "x",
                                       "initial_pr": {"diff": sample_diff}}))
    assert result.success is False       # approve withheld
    assert revise.calls >= 1             # forced revision instead
    review_iters = [it for it in result.iterations if it.phase == "review"]
    assert all("untrusted" in (it.notes or "") for it in review_iters)


def test_loop_runtime_style_overrides_reach_skills(sample_diff):
    """context prompt_style / revision_feedback_level must actually reach the
    review and revise calls (previously a dead path)."""
    import asyncio

    class FakeReviewSkill:
        def __init__(self):
            self.calls = 0
            self.kwargs_seen = []

        async def execute(self, **kwargs):
            self.calls += 1
            self.kwargs_seen.append(kwargs)
            decision = "request_changes" if self.calls == 1 else "approve"

            class Out:
                def to_dict(self_inner, deep=False):
                    return {"decision": decision, "confidence": 0.8,
                            "defects": [{"severity": "high", "description": "d",
                                         "location": "a.py:1", "suggestion": "s"}],
                            "findings": [], "token_usage": None}
            return Out()

    class FakeReviseSkill:
        def __init__(self):
            self.kwargs_seen = []

        async def execute(self, **kwargs):
            self.kwargs_seen.append(kwargs)
            return {"title": "t", "body": "b", "diff": sample_diff,
                    "changes_summary": "fixed"}

    review, revise = FakeReviewSkill(), FakeReviseSkill()
    loop = LoopSubAgent(review_skill=review, revise_skill=revise, max_iterations=3)
    asyncio.run(loop.execute({
        "issue": "x", "initial_pr": {"diff": sample_diff},
        "prompt_style": "detailed",
        "revision_feedback_level": "minimal_feedback",
    }))
    assert review.kwargs_seen[0]["prompt_style"] == "detailed"
    assert revise.kwargs_seen[0]["prompt_style"] == "detailed"
    assert revise.kwargs_seen[0]["feedback_level"] == "minimal_feedback"



# ---------------------------------------------------------------------------
# Redundancy / reuse checks — repeated_added_blocks signal + prompt clauses
# ---------------------------------------------------------------------------

REDUNDANT_DIFF = (
    "diff --git a/service_a.py b/service_a.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def a():\n"
    "+    total = price * quantity * (1 - discount)\n"
    "+    logger.info('computed total for order %s', order_id)\n"
    "+    return round(total, 2)\n"
    " pass\n"
    "diff --git a/service_b.py b/service_b.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def b():\n"
    "+    total = price * quantity * (1 - discount)\n"
    "+    logger.info('computed total for order %s', order_id)\n"
    "+    return round(total, 2)\n"
    " pass\n"
)


def test_detect_repeated_added_blocks_flags_cross_file_copy_paste():
    from swe_review.subagents.analyzer_agent import detect_repeated_added_blocks
    blocks = detect_repeated_added_blocks(REDUNDANT_DIFF)
    assert len(blocks) >= 2
    lines = {b["line"] for b in blocks}
    assert "total = price * quantity * (1 - discount)" in lines
    top = blocks[0]
    assert top["count"] >= 2 and top["hunks"] >= 2
    assert "service_a.py" in top["files"] and "service_b.py" in top["files"]


def test_detect_repeated_added_blocks_empty_when_unique():
    from swe_review.subagents.analyzer_agent import detect_repeated_added_blocks
    unique = (
        "diff --git a/x.py b/x.py\n@@ -1,1 +1,3 @@\n def x():\n"
        "+    alpha = compute_alpha(value)\n"
        "+    beta = compute_beta(value)\n"
    )
    assert detect_repeated_added_blocks(unique) == []


def test_detect_repeated_added_blocks_filters_short_and_imports():
    from swe_review.subagents.analyzer_agent import detect_repeated_added_blocks
    diff = (
        "diff --git a/m.py b/m.py\n@@ -1,1 +1,2 @@\n pass\n"
        "+import os\n"
        "+    x = 1\n"
        "diff --git a/n.py b/n.py\n@@ -1,1 +1,2 @@\n pass\n"
        "+import os\n"
        "+    x = 1\n"
    )
    assert detect_repeated_added_blocks(diff) == []


def test_analyzer_result_exposes_repeated_added_blocks():
    sub = AnalyzerSubAgent()
    import asyncio
    result = asyncio.run(sub.execute({"pr_diff": REDUNDANT_DIFF}))
    assert len(result.repeated_added_blocks) >= 2
    assert "repeated_added_blocks" in result.to_dict()


def test_reviewer_patch_analysis_includes_redundancy_signal():
    sub = ReviewerSubAgent(tool_adapter=None)
    analysis = sub._analyze_patch(
        REDUNDANT_DIFF, {"files_modified": ["service_a.py", "service_b.py"]}
    )
    assert analysis["repeated_added_blocks"]
    assert analysis["files_changed"] == 2


def test_engineering_prompt_covers_reuse_and_redundancy():
    from swe_review.subagents import engineering_prompt
    sp = engineering_prompt.system_prompt()
    assert "Reuse Impact" in sp
    assert "repeated_added_blocks" in sp
    assert "提取公共函数" in sp


def test_legacy_review_prompts_cover_redundancy():
    from swe_review.subagents.reviewer_agent import (
        _system_prompt_concise, _system_prompt_detailed,
    )
    assert "Redundancy check" in _system_prompt_concise()
    assert "copy-paste" in _system_prompt_detailed()


def test_empty_review_payload_is_flagged():
    """Regression (self-loop run): a content-free model answer like `{}` used
    to degrade into a silent request_changes@0.5 with zero findings and burn
    a loop iteration. It must now surface as a parse_error so the repair
    path retries."""
    from swe_review.subagents.reviewer_agent import _is_empty_review_payload

    assert _is_empty_review_payload({})
    assert _is_empty_review_payload({"noise": "x"})
    assert not _is_empty_review_payload({"decision": "APPROVE"})
    assert not _is_empty_review_payload({"findings": [{"severity": "P2"}]})
    assert not _is_empty_review_payload({"defects": [{"severity": "high"}]})
    assert not _is_empty_review_payload({"summary": {"problem": "p"}})
    assert not _is_empty_review_payload({"scores": {"risk": {"score": 5}}})
    assert _is_empty_review_payload({"scores": {"risk": {"score": None}}})

    agent = ReviewerSubAgent()
    report = agent._parse_response("{}", {}, "engineering")
    assert report.parse_error and "empty" in report.parse_error
    # repair path must pick this up
    assert report.parse_error or report.truncated_repair


def test_empty_review_payload_triggers_internal_repair():
    """An empty first answer must trigger ReviewerSubAgent's one-shot JSON
    repair retry; a good repaired answer becomes the final report."""
    import asyncio
    import json

    valid_report = {
        "decision": "APPROVE",
        "confidence": 0.9,
        "summary": {"problem": "p", "solution": "s", "overall_assessment": "ok"},
        "findings": [],
        "scores": {
            "design_quality": {"score": 1, "reason": ""},
            "maintainability": {"score": 1, "reason": ""},
            "consistency": {"score": 1, "reason": ""},
            "simplicity": {"score": 1, "reason": ""},
            "readability": {"score": 1, "reason": ""},
            "testability": {"score": 1, "reason": ""},
            "risk": {"score": 1, "reason": ""},
            "change_scope": {"score": 1, "reason": ""},
        },
        "total_score": 8,
        "hard_gate": {"triggered": False, "reason": ""},
    }

    class StubAdapter:
        def __init__(self):
            self.calls = []
            self._responses = ["{}", json.dumps(valid_report)]

        async def chat(self, system, user, **kw):
            self.calls.append(user)
            idx = min(len(self.calls) - 1, len(self._responses) - 1)
            return self._responses[idx], {"prompt_tokens": 1, "completion_tokens": 1}

    stub = StubAdapter()
    agent = ReviewerSubAgent(tool_adapter=stub, prompt_style="engineering",
                             regen_backoff_seconds=0.0)
    report = asyncio.run(agent.execute({
        "issue": "issue text",
        "pr_title": "pr title",
        "pr_diff": "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
    }))
    assert len(stub.calls) == 2, "empty payload must trigger exactly one repair retry"
    assert "failed to parse" in stub.calls[1].lower() or "json" in stub.calls[1].lower()
    assert report.parse_error is None
    assert report.decision == "approve"
    assert report.total_score == 8.0


def test_unparseable_review_retries_fresh_calls():
    """When both the answer and its repair round come back empty, the
    reviewer must regenerate with a fresh call (bounded by
    max_regen_attempts) instead of handing the loop a parse_error."""
    import asyncio
    import json

    valid_report = {
        "decision": "APPROVE",
        "confidence": 0.9,
        "summary": {"problem": "p", "solution": "s", "overall_assessment": "ok"},
        "findings": [],
        "scores": {
            "design_quality": {"score": 1, "reason": ""},
            "maintainability": {"score": 1, "reason": ""},
            "consistency": {"score": 1, "reason": ""},
            "simplicity": {"score": 1, "reason": ""},
            "readability": {"score": 1, "reason": ""},
            "testability": {"score": 1, "reason": ""},
            "risk": {"score": 1, "reason": ""},
            "change_scope": {"score": 1, "reason": ""},
        },
        "total_score": 8,
        "hard_gate": {"triggered": False, "reason": ""},
    }

    class StubAdapter:
        def __init__(self):
            self.calls = []

        async def chat(self, system, user, **kw):
            self.calls.append(user)
            tok = {"prompt_tokens": 1, "completion_tokens": 1}
            # attempt 1 (original + repair) and attempt 2's original all
            # come back empty; attempt 2's repair round finally succeeds.
            if len(self.calls) >= 4:
                return json.dumps(valid_report), tok
            return "{}", tok

    stub = StubAdapter()
    agent = ReviewerSubAgent(tool_adapter=stub, prompt_style="engineering",
                             regen_backoff_seconds=0.0)
    report = asyncio.run(agent.execute({
        "issue": "issue text",
        "pr_title": "pr title",
        "pr_diff": "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
    }))
    assert len(stub.calls) == 4, f"expected 4 calls (2 attempts x original+repair), got {len(stub.calls)}"
    assert report.parse_error is None
    assert report.decision == "approve"


# ======================================================================
# best_of_n swarm rework: parallel waves + perspectives + failure memory
# + evidence-first adjudication
# ======================================================================

def _fake_gen_skill(gens):
    """gens: list of payload dicts returned in call order."""
    class FakeGenSkill:
        def __init__(self):
            self.calls = []
            self.explore_calls = 0

        async def explore(self, issue, repo_path=None):
            self.explore_calls += 1
            return {"shared": True}

        async def execute(self, **kw):
            self.calls.append(kw)
            payload = gens[min(len(self.calls), len(gens)) - 1]
            return {"ok": True, "payload": payload, "raw": None, "message": ""}

    return FakeGenSkill()


def _fake_review_skill(decisions):
    """decisions: list of (decision, confidence) returned in call order."""
    class FakeReviewSkill:
        def __init__(self):
            self.calls = 0

        async def execute(self, **kw):
            d, c = decisions[min(self.calls, len(decisions) - 1)]
            self.calls += 1
            return {"ok": True,
                    "payload": {"decision": d, "confidence": c,
                                "defects": [], "findings": [],
                                "token_usage": None},
                    "raw": None, "message": ""}

    return FakeReviewSkill()


def _fake_verify_skill(pass_diffs):
    """pass_diffs: set of diffs that verify as passed."""
    class FakeVerifySkill:
        def __init__(self):
            self.verified = []

        async def execute(self, pr_diff="", **kw):
            self.verified.append(pr_diff)
            ok = pr_diff in pass_diffs
            return {"ok": True,
                    "payload": {"passed": ok, "confidence": 0.9 if ok else 0.1,
                                "details": "fake verify",
                                "patch_applied": True,
                                "resolution_status": "resolved" if ok else "failed"},
                    "raw": None, "message": ""}

    return FakeVerifySkill()


def _gen_payload(diff, rationale="fix", confidence=0.5):
    return {"title": "t", "body": "b", "diff": diff,
            "rationale": rationale, "confidence": confidence}


def test_best_of_n_verify_fail_falls_through_to_next_approved_candidate():
    """Bug fix: previously an approved candidate returned success=True even
    when verify FAILED. Now a failed verify falls through to the next
    approved candidate."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopSubAgent

    gen = _fake_gen_skill([_gen_payload("d1"), _gen_payload("d2")])
    rev = _fake_review_skill([("approve", 0.9), ("approve", 0.8)])
    ver = _fake_verify_skill({"d2"})  # d1 (higher confidence) fails verify

    loop = LoopSubAgent(review_skill=rev, generator_skill=gen,
                        verifier_skill=ver, strategy="best_of_n")
    r = asyncio.run(loop.execute(
        {"issue": "x", "strategy": "best_of_n", "n_best_of": 2}))
    assert r.success is True
    assert r.final_pr_diff == "d2"
    assert ver.verified == ["d1", "d2"]  # evidence-first ordering by confidence
    assert "approved+verified" in r.message


def test_best_of_n_rejected_candidates_verify_tiebreak():
    """No approvals: verify top-2 by confidence as evidence tie-break; a
    verify-pass does NOT flip success (review gate stands) but becomes the
    returned diff."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopSubAgent

    gen = _fake_gen_skill([_gen_payload("d1"), _gen_payload("d2"),
                           _gen_payload("d3")])
    rev = _fake_review_skill([("request_changes", 0.9),
                              ("request_changes", 0.6),
                              ("request_changes", 0.7)])
    ver = _fake_verify_skill({"d3"})

    loop = LoopSubAgent(review_skill=rev, generator_skill=gen,
                        verifier_skill=ver, strategy="best_of_n")
    r = asyncio.run(loop.execute(
        {"issue": "x", "strategy": "best_of_n", "n_best_of": 3}))
    assert r.success is False
    assert r.final_decision == "reject"
    # top-2 by confidence are d1 (0.9) then d3 (0.7); d3 verify-passes
    assert r.final_pr_diff == "d3"
    assert "verify-passed but review-rejected" in r.message


def test_best_of_n_wave1_parallel_perspectives_and_shared_exploration():
    """Wave 1 must fan out with rotating perspectives over ONE shared
    exploration (not one explorer run per candidate)."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopSubAgent

    gen = _fake_gen_skill([_gen_payload("d1"), _gen_payload("d2"),
                           _gen_payload("d3")])
    rev = _fake_review_skill([("approve", 0.9)] * 3)
    ver = _fake_verify_skill({"d1"})

    loop = LoopSubAgent(review_skill=rev, generator_skill=gen,
                        verifier_skill=ver, strategy="best_of_n")
    r = asyncio.run(loop.execute(
        {"issue": "x", "strategy": "best_of_n", "n_best_of": 3}))
    assert r.success is True
    assert gen.explore_calls == 1
    perspectives = [c.get("perspective") for c in gen.calls]
    assert perspectives == ["minimal", "alternative", "constraint_aware"]
    # shared exploration object reached every candidate
    assert all(c.get("exploration") == {"shared": True} for c in gen.calls)
    # wave 1 has no failure memory
    assert all(c.get("prior_failures") is None for c in gen.calls)


def test_best_of_n_second_wave_carries_failure_memory():
    """Wave 2 (serial expansion) must receive a compact summary of every
    candidate rejected in wave 1."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopSubAgent

    gen = _fake_gen_skill([_gen_payload(f"d{i}", rationale=f"approach {i}")
                           for i in range(1, 5)])
    rev = _fake_review_skill([("request_changes", 0.5)] * 4)
    ver = _fake_verify_skill(set())

    loop = LoopSubAgent(review_skill=rev, generator_skill=gen,
                        verifier_skill=ver, strategy="best_of_n")
    r = asyncio.run(loop.execute(
        {"issue": "x", "strategy": "best_of_n", "n_best_of": 4}))
    assert r.success is False
    assert len(gen.calls) == 4  # 3 in wave 1 + 1 serial expansion
    pf = gen.calls[3].get("prior_failures")
    assert pf is not None and len(pf) == 3
    assert all("approach" in f and "review_decision" in f for f in pf)
    # expansion uses the rotated perspective too
    assert gen.calls[3].get("perspective") == "minimal"


def test_generator_perspective_and_failure_memory_prompts():
    """Perspective directive lands in the system prompt; failure memory lands
    in the user prompt; defaults keep prompts unchanged."""
    from swe_review.subagents.generator_agent import (
        GeneratorSubAgent, PERSPECTIVES)

    g = GeneratorSubAgent()
    base_sys = g._system_prompt()
    assert base_sys == g._system_prompt(perspective=None)
    for key, directive in PERSPECTIVES.items():
        assert directive in g._system_prompt(perspective=key)
    assert "unknown" not in g._system_prompt(perspective="unknown") or \
        g._system_prompt(perspective="unknown") == base_sys

    base_usr = g._user_prompt("i", "", None, {})
    assert "Previously Rejected Approaches" not in base_usr
    usr = g._user_prompt("i", "", None, {},
                         prior_failures=[{"approach": "patched parser",
                                          "review_decision": "request_changes",
                                          "top_findings": ["P1:too broad"]}])
    assert "Previously Rejected Approaches" in usr
    assert "patched parser" in usr


def test_engineering_hollow_payload_flagged_as_parse_error():
    """Self-review finding: a structurally valid but hollow payload (scores
    attempted but mostly null, out-of-range score, findings with empty
    evidence) must be treated as a parse failure so regen/repair retries and
    the loop's trust gate engage."""
    from swe_review.subagents.reviewer_agent import _parse_engineering_payload

    hollow = {
        "decision": "REQUEST_CHANGES", "confidence": 0.5,
        "summary": {"problem": "x"},
        "findings": [{"severity": "P1", "title": "hallucinated",
                      "location": "a.py:1", "observation": "",
                      "evidence": ""}],
        "scores": {"readability": {"score": 80, "max": 10},
                   "risk": {"score": None}},
    }
    rep = _parse_engineering_payload(hollow, None)
    assert rep.parse_error and "incomplete_content" in rep.parse_error
    assert rep.scores["readability"]["score"] is None  # out-of-range nulled
    assert rep.total_score is None

    # minimal approve WITHOUT scores key stays valid (legacy tolerance)
    minimal = {"decision": "APPROVE", "confidence": 0.9,
               "summary": {}, "findings": []}
    rep2 = _parse_engineering_payload(minimal, None)
    assert rep2.parse_error is None

    # full healthy payload passes
    healthy = {"decision": "APPROVE", "confidence": 0.9, "summary": {},
               "findings": [],
               "scores": {d: {"score": 1, "max": m, "reason": "r"}
                          for d, m in
                          [("design_quality", 20), ("maintainability", 15),
                           ("consistency", 15), ("simplicity", 10),
                           ("readability", 10), ("testability", 10),
                           ("risk", 10), ("change_scope", 10)]},
               "total_score": 8}
    rep3 = _parse_engineering_payload(healthy, None)
    assert rep3.parse_error is None
    assert rep3.total_score == 8.0


def test_best_of_n_wave_survives_candidate_exceptions():
    """Self-review P1: a raising generate/review must not sink the wave —
    the surviving approved candidate still wins."""
    import asyncio
    from swe_review.subagents.loop_agent import LoopSubAgent

    class FlakyGenSkill:
        async def explore(self, issue, repo_path=None):
            return {}

        async def execute(self, **kw):
            if kw.get("perspective") == "alternative":
                raise RuntimeError("boom")
            return {"ok": True, "payload": _gen_payload("good-diff"),
                    "raw": None, "message": ""}

    class FlakyReviewSkill:
        async def execute(self, **kw):
            return {"ok": True,
                    "payload": {"decision": "approve", "confidence": 0.9,
                                "defects": [], "findings": [],
                                "token_usage": None},
                    "raw": None, "message": ""}

    ver = _fake_verify_skill({"good-diff"})
    loop = LoopSubAgent(review_skill=FlakyReviewSkill(),
                        generator_skill=FlakyGenSkill(),
                        verifier_skill=ver, strategy="best_of_n")
    r = asyncio.run(loop.execute(
        {"issue": "x", "strategy": "best_of_n", "n_best_of": 3}))
    assert r.success is True
    assert r.final_pr_diff == "good-diff"
    # the failed candidate is recorded, not fatal
    gen_iters = [i for i in r.iterations if i.phase == "generate"]
    assert any("generate error" in (i.notes or "") for i in gen_iters)


def test_constraint_category_normalized_against_canonical_list():
    """Self-review P2: parser normalizes unknown constraint_category to ''
    using the same constant the prompt advertises."""
    from swe_review.subagents.reviewer_agent import _parse_engineering_payload
    from swe_review.subagents.engineering_prompt import (
        CONSTRAINT_CATEGORIES, system_prompt)

    assert "scope_generalization" in CONSTRAINT_CATEGORIES
    for cat in CONSTRAINT_CATEGORIES:
        assert cat in system_prompt(["Python"])

    def parse(cat):
        return _parse_engineering_payload(
            {"decision": "REQUEST_CHANGES", "confidence": 0.5,
             "findings": [{"severity": "P1", "title": "t", "location": "a:1",
                           "observation": "o", "evidence": "e",
                           "constraint_category": cat}]},
            None).findings[0].constraint_category

    assert parse("scope_generalization") == "scope_generalization"
    assert parse("made_up_category") == ""
    assert parse("") == ""
