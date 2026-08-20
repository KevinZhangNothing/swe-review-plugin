"""
Reviser SubAgent — code revision guided by structured review feedback.

Revision feedback level (three modes selectable per call):
  - "full_feedback"   (default): review provided structured defects[]
  - "minimal_feedback":         review said rejected but no defect details
  - "baseline":                  no review at all — previous attempt known not to resolve

Hard constraint: oracle / golden_patch / hidden tests are rejected at the
context level to keep the reviser self-sufficient.
"""

import json
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, asdict


REVISION_FEEDBACK_LEVELS = ("full_feedback", "minimal_feedback", "baseline")


@dataclass
class RevisedPR:
    title: str
    body: str
    diff: str  # git-apply compatible unified diff
    changes_summary: str
    addressed_defect_indices: List[int]
    status: str  # "success" | "partial" | "failed"
    feedback_level: str = "full_feedback"  # which mode was used

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# Prompts
# ============================================================================

def _system_prompt_concise() -> str:
    return (
        "You are a code revision expert. You receive a candidate PR and a structured "
        "list of defects (severity / description / location / suggestion). Your job: "
        "produce a REVISED unified diff that addresses the defects with the MINIMUM "
        "necessary changes.\n\n"
        "## Hard Rules\n"
        "1. Output ONLY a JSON object — no markdown fences, no prose.\n"
        "2. `diff` MUST be a complete unified diff (with `diff --git` / `@@` hunks) that "
        "`git apply` accepts. Re-emit the WHOLE file hunks you touch; do NOT emit only "
        "fragments.\n"
        "3. Address ALL `high` severity defects; try to address `medium`; ignore `low` "
        "unless trivial.\n"
        "4. Do NOT introduce new files unless the suggestion explicitly requires it.\n"
        "5. Do NOT change function signatures unless the defect demands it.\n\n"
        "## Output Schema\n"
        "{\n"
        '  "title": "revised PR title",\n'
        '  "body": "what changed and why (1-3 sentences)",\n'
        '  "diff": "complete unified diff",\n'
        '  "changes_summary": "one-line summary",\n'
        '  "addressed_defect_indices": [0, 2]\n'
        "}"
    )


def _system_prompt_detailed() -> str:
    """Detailed revision prompt with explicit feedback-level framing."""
    return (
        "# Revision Task\n\n"
        "You are a code revision agent. You will be given a bug report and (optionally) "
        "a previous patch attempt and a code review. Use the review feedback to guide "
        "your revision. Address each defect identified in the review, focusing on HIGH "
        "severity issues first.\n\n"
        "## Output\n"
        "Output ONLY a JSON object (no markdown fences, no prose):\n"
        "{\n"
        '  "title": "short PR title",\n'
        '  "body": "what changed and why (1-3 sentences)",\n'
        '  "diff": "complete unified diff (git apply compatible)",\n'
        '  "changes_summary": "one-line summary",\n'
        '  "addressed_defect_indices": [list of 0-based defect indices you addressed]\n'
        "}\n\n"
        "## Constraints\n"
        "- `diff` MUST start with `diff --git` and contain `@@` hunks for every file touched.\n"
        "- Address every `high` severity defect; attempt every `medium`; ignore `low` "
        "unless trivial.\n"
        "- Do NOT change function signatures unless the defect requires it.\n"
        "- Keep the change MINIMAL — do not refactor unrelated code."
    )


# Mode-specific user-message templates
def _user_prompt_baseline(issue, original_diff):
    return (
        f"## Bug Report\n{issue}\n\n"
        "## Previous Attempt (did NOT fully resolve the issue)\n"
        f"```diff\n{original_diff}\n```\n\n"
        "You may treat this as a starting point (apply it first in your head, then revise), "
        "or you may rewrite from scratch if the approach is wrong."
    )


def _user_prompt_minimal_feedback(issue, original_diff):
    return (
        f"## Bug Report\n{issue}\n\n"
        "## Previous Attempt (REJECTED, no specific feedback provided)\n"
        f"```diff\n{original_diff}\n```\n\n"
        "You may apply the previous attempt as a starting point and revise, or rewrite "
        "from scratch. A code reviewer rejected the attempt but did not provide specific "
        "feedback — you must identify and address the issues yourself."
    )


def _user_prompt_full_feedback(issue, original_diff, review_report):
    defects_block = json.dumps(review_report.get("defects", []), indent=2, ensure_ascii=False)
    decision_summary = review_report.get("decision", "request_changes")
    return (
        f"## Bug Report\n{issue}\n\n"
        "## Previous Attempt\n"
        f"```diff\n{original_diff}\n```\n\n"
        "## Code Review Feedback\n"
        f"Reviewer decision: {decision_summary}\n"
        f"Defects (self-contained; address HIGH first):\n"
        f"```json\n{defects_block}\n```\n\n"
        "Apply the previous attempt as a starting point. Then address each defect "
        "in the review, focusing on HIGH-severity issues."
    )


def _build_user_prompt_detailed(issue, original_title, original_body,
                                original_diff, review_report, feedback_level):
    if feedback_level == "full_feedback":
        return _user_prompt_full_feedback(issue=issue, original_diff=original_diff,
                                          review_report=review_report)
    if feedback_level == "minimal_feedback":
        return _user_prompt_minimal_feedback(issue=issue, original_diff=original_diff)
    if feedback_level == "baseline":
        return _user_prompt_baseline(issue=issue, original_diff=original_diff)
    raise ValueError(f"unknown feedback_level {feedback_level!r}")


# ============================================================================
# SubAgent
# ============================================================================

class ReviserSubAgent:
    """Revise a candidate PR given varying feedback levels."""

    def __init__(
        self,
        tool_adapter: Any = None,
        config: Optional[Dict[str, Any]] = None,
        prompt_style: str = "concise",
        feedback_level: str = "full_feedback",
    ):
        # The reviser has no engineering-specific prompt; engineering-style
        # review feedback (P0-P4 findings mapped to defects) is evidence-rich,
        # so route it to the detailed revision prompt.
        if prompt_style == "engineering":
            prompt_style = "detailed"
        if prompt_style not in ("concise", "detailed"):
            raise ValueError(
                f"prompt_style must be concise or detailed, got {prompt_style!r}"
            )
        if feedback_level not in REVISION_FEEDBACK_LEVELS:
            raise ValueError(
                f"feedback_level must be one of {REVISION_FEEDBACK_LEVELS!r}, "
                f"got {feedback_level!r}"
            )
        self.tool_adapter = tool_adapter
        self.config = config or {}
        self.prompt_style = prompt_style
        self.feedback_level = feedback_level
        self.name = "reviser"
        self.capabilities = [
            "parse_feedback",
            "generate_fix",
            "validate_diff",
            "variable_feedback_levels",
        ]

    async def execute(self, context: Dict[str, Any]) -> RevisedPR:
        # Hard guards: oracle / golden_patch must never reach the reviser
        for forbidden in ("golden_patch", "gold_patch", "oracle", "test_info"):
            if forbidden in context:
                raise ValueError(
                    f"'{forbidden}' is not allowed in revision context."
                )

        issue = context.get("issue", "")
        original_title = context.get("original_pr_title", "")
        original_body = context.get("original_pr_body", "")
        original_diff = context.get("original_pr_diff", "")
        review_report = context.get("review_report") or {}

        # Allow per-call overrides
        prompt_style = context.get("prompt_style") or self.prompt_style
        if prompt_style == "engineering":
            prompt_style = "detailed"
        feedback_level = context.get("feedback_level") or self.feedback_level

        if prompt_style == "detailed":
            system_prompt = _system_prompt_detailed()
            user_prompt = _build_user_prompt_detailed(
                issue=issue,
                original_title=original_title,
                original_body=original_body,
                original_diff=original_diff,
                review_report=review_report,
                feedback_level=feedback_level,
            )
        else:
            system_prompt = _system_prompt_concise()
            user_prompt = self._build_user_prompt_concise(
                issue=issue,
                original_title=original_title,
                original_body=original_body,
                original_diff=original_diff,
                review_report=review_report,
            )

        response, _ = await self._call_ai(system_prompt, user_prompt)
        return self._parse_response(response, original_title, original_body, feedback_level)

    def _build_user_prompt_concise(self, issue, original_title, original_body,
                                    original_diff, review_report):
        diff = original_diff
        if len(diff) > 50_000:
            diff = diff[:50_000] + "\n... (diff truncated)"
        defects_block = json.dumps(review_report.get("defects", []),
                                   indent=2, ensure_ascii=False)
        return (
            f"## Issue\n{issue}\n\n"
            f"## Original PR\n- Title: {original_title}\n- Description: {original_body or 'N/A'}\n\n"
            f"## Original Diff\n```diff\n{diff}\n```\n\n"
            f"## Review Defects to Address\n```json\n{defects_block}\n```\n\n"
            "## Your Task\n"
            "1. Solve every high severity defect.\n"
            "2. Solve as many medium severity defects as possible.\n"
            "3. Keep changes minimal.\n"
            "4. Do not introduce new bugs.\n\n"
            "Output ONLY the JSON."
        )

    async def _call_ai(self, system: str, user: str) -> Tuple[str, Dict[str, int]]:
        if self.tool_adapter:
            return await self.tool_adapter.chat(
                system=system, user=user, max_tokens=4096, temperature=0.1,
            )
        return "{}", {}

    def _parse_response(self, response, original_title, original_body, feedback_level):
        cleaned = _strip_fences(response)
        try:
            data = json.loads(cleaned)
            diff = data.get("diff", "") or ""
            addressed = data.get("addressed_defect_indices") or []
            if not isinstance(addressed, list):
                addressed = []
            ok = diff.startswith(("diff ", "diff --git", "--- ")) and "@@" in diff
            return RevisedPR(
                title=data.get("title") or f"Revised: {original_title or 'patch'}",
                body=data.get("body") or original_body or "",
                diff=diff,
                changes_summary=data.get("changes_summary") or "revised per review",
                addressed_defect_indices=[i for i in addressed if isinstance(i, int)],
                status=("success" if ok and addressed else ("partial" if diff else "failed")),
                feedback_level=feedback_level,
            )
        except json.JSONDecodeError:
            return RevisedPR(
                title=f"Revised: {original_title or 'patch'}",
                body=original_body or "",
                diff="",
                changes_summary="revise parse failed",
                addressed_defect_indices=[],
                status="failed",
                feedback_level=feedback_level,
            )

    def get_capabilities(self):
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": "reviser",
            "prompt_style": self.prompt_style,
            "feedback_level": self.feedback_level,
            "capabilities": self.capabilities,
        }


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()
