"""
Generator SubAgent - 生成候选 PR

Generate → Review → (Revise | Regenerate) → Verify 主流程里的"G"（Generate）。
此 SubAgent 接收 issue + (可选)repo_path，产出 unified diff。
用于 best-of-N 策略时一次性产生 K 个候选。

NOTE: 不注入任何 oracle/ground-truth 信息。
"""

import json
import re
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict


from .engineering_prompt import strip_fences, truncate_json_text
from .diff_validation import validate_unified_diff


# Candidate-generation perspectives for best_of_n diversity (swarm principle:
# never fan out the identical prompt; each explorer gets a different lens).
PERSPECTIVES: Dict[str, str] = {
    "minimal": (
        "Perspective: MINIMAL FIX. Prefer the smallest possible change that resolves the "
        "issue — fewest files, fewest lines, no speculative generalization. For every new "
        "helper/abstraction/dependency you consider adding, walk the ladder first: does it "
        "need to exist at all? does the repo already have an equivalent? does the standard "
        "library ship it? is it one line? Only add new code when all of those fail."
    ),
    "alternative": (
        "Perspective: ALTERNATIVE PATH. Deliberately look for a different implementation "
        "route than the most obvious one: a different abstraction level, a different call "
        "site, or fixing the root cause upstream instead of patching the symptom. Actively "
        "try counter-examples against the obvious fix before committing."
    ),
    "constraint_aware": (
        "Perspective: REVIEW-CONSTRAINT AWARE. Beyond functional correctness, actively "
        "satisfy the constraint categories real reviewers enforce most: error semantics "
        "(exception types/codes/messages preserved), scope generalization (fix ALL code "
        "paths with the same bug pattern, not just the reported one), lifecycle cleanup "
        "(resources released on every path including errors), and encoding/escaping/quoting "
        "correctness."
    ),
}
PERSPECTIVE_ROTATION: tuple = ("minimal", "alternative", "constraint_aware")


@dataclass
class GeneratedPR:
    title: str
    body: str
    diff: str
    rationale: str
    confidence: float
    # Generation is the most output-heavy call in the loop, yet its usage never
    # reached `LoopResult.token_usage_total` (that only accumulated review
    # usage), so best_of_n's N generations were invisible in the cost report.
    token_usage: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GeneratorSubAgent:
    """生成候选 PR 的 SubAgent"""

    def __init__(self, tool_adapter: Any = None, config: Optional[Dict[str, Any]] = None):
        self.tool_adapter = tool_adapter
        self.config = config or {}
        self.name = "generator"
        self.capabilities = [
            "generate_patch",
            "diff_formatting",
        ]

    async def execute(self, context: Dict[str, Any]) -> GeneratedPR:
        issue = context.get("issue", "")
        hint = context.get("hint", "")
        repo_path = context.get("repo_path")
        exploration: Dict[str, Any] = context.get("exploration") or {}
        perspective: Optional[str] = context.get("perspective")
        prior_failures: Optional[List[Dict[str, Any]]] = context.get("prior_failures")

        # 显式拒绝 oracle 字段
        for forbidden in ("golden_patch", "gold_patch", "oracle", "test_info"):
            if forbidden in context:
                raise ValueError(
                    f"[GeneratorSubAgent] '{forbidden}' is forbidden in generation context."
                )

        sys_p = self._system_prompt(perspective=perspective)
        usr_p = self._user_prompt(issue=issue, hint=hint, repo_path=repo_path,
                                  exploration=exploration, prior_failures=prior_failures)
        response, usage = await self._call(sys_p, usr_p)
        pr = self._parse(response)
        pr.token_usage = usage or None
        return pr

    def _system_prompt(self, perspective: Optional[str] = None) -> str:
        base = (
            "You are a software engineer fixing a real-world GitHub issue. Produce a "
            "REAL, MINIMAL unified diff that resolves the issue.\n\n"
            "## Hard Rules\n"
            "1. Output ONLY a JSON object (no markdown, no prose).\n"
            "2. `diff` MUST be `git apply`-compatible unified diff (start with `diff --git`, "
            "include `--- ` / `+++ ` headers and `@@ ... @@` hunks).\n"
            "3. Touch ONLY files needed to fix the issue. Do NOT refactor unrelated code.\n"
            "4. Keep public function signatures stable.\n"
            "5. Update or add tests inside the repo if available (`tests/`, `test/`).\n"
            "6. Explain in `rationale` what you changed and why.\n\n"
            "## Anti-Bloat Rules (mandatory, applies to every perspective)\n"
            "Solve the problem by ADJUSTING existing logic before ADDING new logic. The "
            "classic failure is a patch that adds a bool, then a flag for the edge case, "
            "then a compatibility branch — each step locally reasonable, the result "
            "unmaintainable.\n"
            "1. Check first whether an existing variable, state, method or data structure "
            "already expresses what you need — reuse it instead of adding a parallel one.\n"
            "2. Do not add a new boolean/state variable to control behaviour that can be "
            "derived from data you already have.\n"
            "3. Prefer rewriting the existing condition over adding another early-return "
            "guard in front of it.\n"
            "4. Do not keep the old implementation alive behind a compatibility flag "
            "unless something still calls it; otherwise delete it in this change.\n"
            "5. Do not introduce a class/manager/helper for a one-off need, and do not add "
            "a dependency for a few lines of code.\n"
            "6. Your diff MAY delete more lines than it adds. Removing the cause beats "
            "guarding the symptom.\n"
            "- Never trade correctness, readability or type safety for a smaller diff.\n\n"
            "## Output Schema\n"
            "{\n"
            '  "title": "short PR title",\n'
            '  "body": "1-3 sentence PR description",\n'
            '  "diff": "complete unified diff",\n'
            '  "rationale": "explanation",\n'
            '  "confidence": 0.0-1.0\n'
            "}"
        )
        directive = PERSPECTIVES.get(perspective or "")
        if directive:
            base += "\n\n## Generation Perspective\n" + directive
        return base

    def _user_prompt(
        self,
        issue: str,
        hint: str,
        repo_path: Optional[str],
        exploration: Dict[str, Any],
        prior_failures: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        # 截断 exploration 防止 prompt 溢出
        ex = json.dumps(exploration, indent=2, ensure_ascii=False)
        if len(ex) > 40_000:
            ex = ex[:40_000] + "\n... (exploration truncated)"
        failures_section = ""
        if prior_failures:
            # Swarm principle: failure memory is fed back as a compact summary
            # (no full diffs) so the next candidate avoids repeating dead ends.
            failures_section = (
                "## Previously Rejected Approaches — do NOT repeat these paths\n"
                + truncate_json_text(
                    json.dumps(prior_failures, indent=2, ensure_ascii=False), limit=4_000
                )
                + "\n\n"
            )
        return (
            f"## Issue\n{issue}\n\n"
            f"## Hint (optional, may be empty)\n{hint or 'N/A'}\n\n"
            f"## Repo Path\n{repo_path or 'unspecified (you decide files)'}\n\n"
            f"## Repository Context (from explorer)\n{ex}\n\n"
            f"{failures_section}"
            "Emit the JSON per schema. The `diff` MUST be git apply-compatible."
        )

    async def _call(self, system: str, user: str):
        if self.tool_adapter:
            return await self.tool_adapter.chat(
                system=system, user=user, max_tokens=4096, temperature=0.2,
            )
        return "{}", {}

    def _parse(self, response: str) -> GeneratedPR:
        cleaned = strip_fences(response)
        try:
            data = json.loads(cleaned)
            diff = data.get("diff", "") or ""
            ok = diff.startswith(("diff ", "diff --git")) and "@@" in diff
            # A generated diff with wrong hunk arithmetic gets APPROVED by review
            # (text only) and rejected by verify much later — the same failure the
            # reviser guards against. Drop the diff so it cannot propagate.
            #
            # SCOPE: this is a producer-side gate on hunk arithmetic only, NOT an
            # applicability check. Passing it says the counts add up; `git apply`
            # can still reject the diff on context mismatch, a missing target
            # file, or a path escape. VerifierSubAgent remains the authority — do
            # not treat "" here as "this patch will apply".
            shape_problem = validate_unified_diff(diff) if diff.strip() else ""
            if shape_problem:
                diff, ok = "", False
            return GeneratedPR(
                title=data.get("title") or "fix:",
                body=data.get("body") or "",
                diff=diff,
                rationale=((f"malformed diff rejected: {shape_problem}"
                            if shape_problem else data.get("rationale") or "")),
                confidence=float(data.get("confidence") or 0.5) if ok else 0.0,
            )
        except json.JSONDecodeError:
            return GeneratedPR(
                title="fix:", body="", diff="",
                rationale="generator parse failed", confidence=0.0,
            )

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "generator", "capabilities": self.capabilities}
