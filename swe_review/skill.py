"""
Skill 层 - 高级、能力化的封装（区别于 SubAgent 的执行单元）

设计原则：
- Skill = 给定上下文，调用一个或多个 SubAgent 完成"一段可被复用的能力"
- Skill 不持久化 LLM 客户端；只引用一个 tool adapter
- Skill 不向 SubAgent 注入 oracle / golden_patch / hidden test 字段
"""

import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict

from .subagents.reviewer_agent import (
    ReviewerSubAgent, ReviewReport, DECISION_CHOICES,
)
from .subagents.reviser_agent import ReviserSubAgent, RevisedPR
from .subagents.explorer_agent import ExplorerSubAgent, ExplorationResult
from .subagents.verifier_agent import VerifierSubAgent, VerificationResult
from .subagents.analyzer_agent import AnalyzerSubAgent, AnalyzerResult
from .subagents.generator_agent import GeneratorSubAgent, GeneratedPR
from .subagents.loop_agent import LoopSubAgent, LoopResult, has_code_evidence
# stdlib-only module, so a top-level import carries no cycle risk
from .subagents.location_grounding import ground_report_locations, unverified_high_severity
from .tools.base import BaseAdapter
from .subagents.diff_sharding import (
    DEFAULT_SHARD_BUDGET_CHARS,
    DEFAULT_SHARD_CONCURRENCY,
)


@dataclass
class SkillResult:
    ok: bool
    payload: Any
    raw: Any = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _tool_name(tool_adapter) -> str:
    """The adapter name used for degradation tagging — ONE lookup path.

    Review read it off the finished `report.tool_name` while revise read it off
    `self.tool.name` directly. Both currently resolve to the same string, but
    two sources for one fact is how a future rename silently stops tagging one
    command. Everything goes through here.
    """
    return str(getattr(tool_adapter, "name", "") or "")


def _degraded_reasons(tool_name: str, report=None) -> List[str]:
    """Every reason a result rests on degraded evidence.

    Applied consistently by every skill that calls an LLM adapter — review,
    revise AND generate — so the SAME offline backend cannot look healthy in one
    command and degraded in another. `shell` never calls an LLM, so any payload
    it produces is evidence-free regardless of which skill wrapped it.

    `verify` deliberately does NOT use this: it applies a patch and runs commands
    rather than calling a model, so `--tool shell` cannot degrade it and tagging
    it would be a false positive. Verify's evidence contract is
    `resolution_status` (weak-pass = `patch_applied_only`).
    """
    degraded: List[str] = []
    if tool_name == "shell":
        degraded.append(
            "shell_placeholder_backend: no LLM was called; this result contains "
            "no analysis of the diff")
    if report is not None:
        if getattr(report, "exploration_degraded", ""):
            degraded.append(f"exploration_degraded: {report.exploration_degraded}")
        if getattr(report, "truncated_repair", False):
            degraded.append("truncated_repair: JSON was only recoverable by dropping a "
                            "truncated tail — findings after the cut are unknown")
        if getattr(report, "parse_error", None):
            degraded.append(f"parse_error: {report.parse_error}")
    return degraded


def _with_degraded(payload: Dict[str, Any], tool_name: str, report=None) -> Dict[str, Any]:
    """Tag a result payload with its evidence-degradation reasons.

    Applied by every skill that calls an LLM adapter: review, revise, generate.
    Review and revise accept `--tool shell`, so tagging only one of them left the
    same zero-evidence failure mode unlabelled in the others and forced the
    downstream caller to reason per-command instead of reading one field.
    Generate needs it too: `shell` yields an empty diff there (`ok=False`), which
    says "no output" but not WHY.

    Verify is deliberately excluded — it applies a patch and runs commands
    rather than calling a model, so `--tool shell` cannot degrade it. Its
    evidence contract is `VerificationResult.evidence_level` instead.
    """
    degraded = _degraded_reasons(tool_name, report)
    if degraded:
        payload["degraded"] = degraded
    return payload


# ---------------------------------------------------------------------------
# Explore Skill
# ---------------------------------------------------------------------------
class ExploreSkill:
    name = "explore"

    def __init__(self, repo_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.subagent = ExplorerSubAgent(repo_path=repo_path, config=config)

    async def execute(
        self,
        issue: str,
        pr_diff: str = "",
        focus_files: Optional[List[str]] = None,
        max_steps: int = 8,
        repo_path: Optional[str] = None,
    ) -> SkillResult:
        ctx = {
            "issue": issue,
            "pr_diff": pr_diff,
            "focus_files": focus_files or [],
            "max_steps": max_steps,
            "repo_path": repo_path,
        }
        res: ExplorationResult = await self.subagent.execute(ctx)
        return SkillResult(ok=True, payload=res.to_dict(), raw=res)


# ---------------------------------------------------------------------------
# Analyze Skill
# ---------------------------------------------------------------------------
class AnalyzeSkill:
    name = "analyze"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.subagent = AnalyzerSubAgent(config=config)

    async def execute(
        self,
        pr_diff: str,
        old_content: Optional[Dict[str, str]] = None,
        new_content: Optional[Dict[str, str]] = None,
    ) -> SkillResult:
        res: AnalyzerResult = await self.subagent.execute({
            "pr_diff": pr_diff,
            "old_content": old_content or {},
            "new_content": new_content or {},
        })
        return SkillResult(ok=True, payload=res.to_dict(), raw=res)


# ---------------------------------------------------------------------------
# Review Skill
# ---------------------------------------------------------------------------
class ReviewSkill:
    """
    ReviewSkill 是面向 LLM 的"审查一段候选 PR"的能力。
    默认会先把任务交给 ExploreSkill 收集上下文，再喂给 ReviewerSubAgent。

    prompt_style:
      - "engineering" (default): senior code-quality review — 8 维度 100 分制评分、
                                 P0–P4 findings、4 档决策（APPROVE /
                                 APPROVE_WITH_SUGGESTIONS / REQUEST_CHANGES / BLOCK）
      - "concise"          : legacy bug-fix-centric 实用 prompt
      - "detailed"         : legacy Step 1→6 workflow + symptom-fix detection (root-cause-first)
    """

    name = "review"

    def __init__(self, tool_adapter=None, explore_skill: Optional[ExploreSkill] = None,
                 analyze_skill: Optional[AnalyzeSkill] = None,
                 prompt_style: str = "engineering",
                 shard_large_diffs: bool = True,
                 shard_budget_chars: int = DEFAULT_SHARD_BUDGET_CHARS,
                 shard_concurrency: int = DEFAULT_SHARD_CONCURRENCY):
        self.tool = tool_adapter or BaseAdapter()
        self.explore_skill = explore_skill or ExploreSkill()
        self.analyze_skill = analyze_skill or AnalyzeSkill()
        self.prompt_style = prompt_style
        self.subagent = ReviewerSubAgent(
            tool_adapter=self.tool,
            explorer=self.explore_skill.subagent,
            prompt_style=prompt_style,
            # Large changes are fanned out to concurrent file-aligned shards so a
            # single review request stays bounded (see subagents/diff_sharding.py).
            shard_large_diffs=shard_large_diffs,
            shard_budget_chars=shard_budget_chars,
            shard_concurrency=shard_concurrency,
        )

    async def execute(
        self,
        issue: str,
        pr_title: str,
        pr_diff: str,
        pr_body: str = "",
        repo_path: Optional[str] = None,
        max_exploration_steps: int = 8,
        focus_files: Optional[List[str]] = None,
        prompt_style: Optional[str] = None,
        deep: bool = False,
        repo_context: Optional[Dict[str, Any]] = None,
    ) -> SkillResult:
        """deep=True 时返回论文 nested schema（decision:{recommendation,confidence}）

        ``repo_context``: a caller that already ran the explorer for this exact
        repo + issue (e.g. best_of_n's shared exploration) can hand the result in
        so the reviewer does not re-explore once per candidate. Read-only reuse —
        the explorer output contains no candidate-specific evidence.
        """
        ctx = {
            "issue": issue,
            "pr_title": pr_title,
            "pr_body": pr_body,
            "pr_diff": pr_diff,
            "repo_path": repo_path,
            "max_exploration_steps": max_exploration_steps,
            "focus_files": focus_files or [],
            "prompt_style": prompt_style or self.prompt_style,
        }
        if repo_context is not None:
            ctx["repo_context"] = repo_context
        report: ReviewReport = await self.subagent.execute(ctx)
        # Deterministic grounding: verify/relocate finding locations against
        # the real workspace before serialization (no LLM cost).
        ground_report_locations(report, repo_path=repo_path)
        # Consume the grounding marks: unverifiable P0/P1 claims must be visible
        # to a human, not silently weighted the same as verified ones.
        unver = unverified_high_severity(report)
        if unver:
            report.summary["unverified_high_severity"] = (
                "以下高严重度 finding 的路径/行号未能在仓库中核实（可能已过期或"
                "不在本仓库），请人工确认: " + "; ".join(unver)
            )
        return SkillResult(
            ok=(report.decision in DECISION_CHOICES and not report.parse_error),
            payload=_with_degraded(report.to_dict(deep=deep), _tool_name(self.tool), report),
            raw=report,
            message=f"reviewed (prompt_style={report.prompt_style}, deep={deep})"
                    + (f"; parse_error={report.parse_error}" if report.parse_error else ""),
        )


# ---------------------------------------------------------------------------
# Revise Skill
# ---------------------------------------------------------------------------
class ReviseSkill:
    """修订 Skill

    feedback_level:
      - "with_feedback" (default): review has structured defects[]
      - "decision_only":           review said rejected but no defects
      - "no_review":               baseline — no review at all
    """

    name = "revise"

    def __init__(self, tool_adapter=None, prompt_style: str = "concise",
                 feedback_level: str = "full_feedback",
                 max_regen_attempts: int = 2):
        self.tool = tool_adapter or BaseAdapter()
        self.prompt_style = prompt_style
        self.feedback_level = feedback_level
        self.subagent = ReviserSubAgent(
            tool_adapter=self.tool,
            prompt_style=prompt_style,
            feedback_level=feedback_level,
            max_regen_attempts=max_regen_attempts,
        )

    async def execute(
        self,
        issue: str,
        original_pr_title: str,
        original_pr_diff: str,
        review_report: Dict[str, Any],
        original_pr_body: str = "",
        repo_path: Optional[str] = None,
        prompt_style: Optional[str] = None,
        feedback_level: Optional[str] = None,
    ) -> SkillResult:
        res: RevisedPR = await self.subagent.execute({
            "issue": issue,
            "original_pr_title": original_pr_title,
            "original_pr_body": original_pr_body,
            "original_pr_diff": original_pr_diff,
            "review_report": review_report,
            "repo_path": repo_path,
            "prompt_style": prompt_style or self.prompt_style,
            "feedback_level": feedback_level or self.feedback_level,
        })
        return SkillResult(
            ok=(res.status in ("success", "partial")),
            # Same degradation contract as review: a `--tool shell` revision is
            # a structurally complete object with no revision behind it.
            payload=_with_degraded(res.to_dict(), res.tool_name or _tool_name(self.tool)),
            raw=res,
            message=f"status={res.status}, feedback_level={res.feedback_level}"
        )


# ---------------------------------------------------------------------------
# Verify Skill
# ---------------------------------------------------------------------------
class VerifySkill:
    name = "verify"

    def __init__(self, repo_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.subagent = VerifierSubAgent(repo_path=repo_path, config=config)

    async def execute(
        self,
        pr_diff: str,
        repo_path: Optional[str] = None,
        test_info: Optional[Dict[str, Any]] = None,
        oracle: Optional[str] = None,  # 仅评测用，永远不会进入 review prompt
        test_runner: Optional[List[str]] = None,
    ) -> SkillResult:
        """注意 payload.ok 的语义：它表示**补丁是否成功应用**（`patch_applied`），
        不是"命令是否跑完"。工作区准备失败、补丁冲突都会给出 ok=False，而测试跑没跑、
        过没过要看 payload 里的 `resolution_status` / `test_results`。

        Deliberately NOT tagged with `_with_degraded`: verify never calls an LLM
        (it applies a patch and runs commands), so `--tool shell` has no effect
        here and tagging it would be a false positive. Verify's own evidence
        contract is `resolution_status`, whose weak-pass case
        (`patch_applied_only`) the loop already surfaces via
        `LoopResult.verification_status`.
        """
        res: VerificationResult = await self.subagent.execute({
            "pr_diff": pr_diff,
            "repo_path": repo_path,
            "test_info": test_info,
            "oracle": oracle,
            "test_runner": test_runner,
        })
        return SkillResult(ok=res.patch_applied, payload=res.to_dict(), raw=res)


# ---------------------------------------------------------------------------
# Generate Skill
# ---------------------------------------------------------------------------
class GenerateSkill:
    name = "generate"

    def __init__(self, tool_adapter=None, explore_skill: Optional[ExploreSkill] = None):
        self.tool = tool_adapter or BaseAdapter()
        self.subagent = GeneratorSubAgent(tool_adapter=self.tool)
        self.explore_skill = explore_skill or ExploreSkill()

    async def explore(self, issue: str, repo_path: Optional[str] = None) -> Dict[str, Any]:
        """Run the explorer once so a loop can share the result across N candidates.

        An empty result is tagged with `exploration_degraded` so a caller (and
        best_of_n's shared-exploration path) can tell "explored and found
        nothing" from "could not explore". `ExploreSkill.execute` reports `ok`
        for an empty payload, so without this tag the two were indistinguishable.
        """
        if not repo_path:
            return {"exploration_degraded":
                    "explore_skipped: no repo_path supplied, so no repository "
                    "evidence could be gathered"}
        ex = await self.explore_skill.execute(
            issue=issue, repo_path=repo_path, max_steps=4
        )
        payload = ex.payload or {}
        if payload.get("exploration_degraded"):
            return payload
        # Same predicate the loop uses for shared exploration — `keywords` is
        # NOT evidence (it comes from the issue text and is always populated),
        # only resolved files or a call chain are.
        if not has_code_evidence(payload):
            return {**payload, "exploration_degraded":
                    "explore_empty: the explorer ran but resolved no files in the "
                    "repository, so the candidate was written without code evidence"}
        return payload

    async def execute(
        self,
        issue: str,
        hint: str = "",
        repo_path: Optional[str] = None,
        perspective: Optional[str] = None,
        prior_failures: Optional[list] = None,
        exploration: Optional[Dict[str, Any]] = None,
    ) -> SkillResult:
        # exploration=None → run our own explore (backward compatible);
        # an explicit value (even {}) is used as-is so loops can share one.
        if exploration is None:
            exploration = await self.explore(issue=issue, repo_path=repo_path)

        res: GeneratedPR = await self.subagent.execute({
            "issue": issue,
            "hint": hint,
            "repo_path": repo_path,
            "exploration": exploration,
            "perspective": perspective,
            "prior_failures": prior_failures,
        })
        # Same degradation contract as review/revise. `--tool shell` produces no
        # usable diff here (its fixed payload has no `diff` key, so `_parse`
        # returns an empty diff), which means `ok=False` — but an empty diff
        # alone does not say WHY, and a caller comparing backends needs that.
        return SkillResult(
            ok=bool(res.diff),
            payload=_with_degraded(res.to_dict(), _tool_name(self.tool)),
            raw=res)


# ---------------------------------------------------------------------------
# Loop Skill (Generate-Review-Revise-Verify)
# ---------------------------------------------------------------------------
class LoopSkill:
    name = "loop"

    def __init__(
        self,
        review_skill: Optional[ReviewSkill] = None,
        revise_skill: Optional[ReviseSkill] = None,
        generator_skill: Optional[GenerateSkill] = None,
        verify_skill: Optional[VerifySkill] = None,
        tool_adapter=None,
        max_iterations: int = 5,
        early_stop: bool = True,
        strategy: str = "review_guided",
        n_best_of: int = 3,
        output_dir: Optional[str] = None,
        prompt_style: str = "engineering",
        revision_feedback_level: str = "full_feedback",
    ):
        self.prompt_style = prompt_style
        self.revision_feedback_level = revision_feedback_level
        self.review_skill = (
            review_skill
            or ReviewSkill(tool_adapter=tool_adapter, prompt_style=prompt_style)
        )
        self.revise_skill = (
            revise_skill
            or ReviseSkill(tool_adapter=tool_adapter, prompt_style=prompt_style,
                            feedback_level=revision_feedback_level)
        )
        self.generator_skill = generator_skill
        self.verify_skill = verify_skill or VerifySkill()
        self.subagent = LoopSubAgent(
            review_skill=self.review_skill,
            revise_skill=self.revise_skill,
            generator_skill=self.generator_skill,
            verifier_skill=self.verify_skill,
            max_iterations=max_iterations,
            early_stop=early_stop,
            strategy=strategy,
            n_best_of=n_best_of,
            output_dir=output_dir,
        )

    async def execute(
        self,
        issue: str,
        repo_path: Optional[str] = None,
        initial_pr: Optional[Dict[str, Any]] = None,
        max_iterations: Optional[int] = None,
        strategy: Optional[str] = None,
        n_best_of: Optional[int] = None,
        prompt_style: Optional[str] = None,
        revision_feedback_level: Optional[str] = None,
        test_info: Optional[Dict[str, Any]] = None,
        test_runner: Optional[List[str]] = None,
    ) -> SkillResult:
        ctx = {
            "issue": issue,
            "repo_path": repo_path,
            "initial_pr": initial_pr,
            "max_iterations": max_iterations or self.subagent.max_iterations,
            "strategy": strategy or self.subagent.strategy,
            "n_best_of": n_best_of or self.subagent.n_best_of,
            "prompt_style": prompt_style or self.prompt_style,
            "revision_feedback_level": revision_feedback_level or self.revision_feedback_level,
            # Verifier-only evaluation metadata; never reaches review/revise.
            "test_info": test_info,
            "test_runner": test_runner,
        }
        res: LoopResult = await self.subagent.execute(ctx)
        return SkillResult(ok=res.success, payload=res.to_dict(), raw=res)
