"""
Skill 层 - 高级、能力化的封装（区别于 SubAgent 的执行单元）

设计原则：
- Skill = 给定上下文，调用一个或多个 SubAgent 完成"一段可被复用的能力"
- Skill 不持久化 LLM 客户端；只引用一个 tool adapter
- Skill 不向 SubAgent 注入 oracle / golden_patch / hidden test 字段
"""

import asyncio
from typing import Dict, Any, Optional, List, Callable, Awaitable
from dataclasses import dataclass, asdict

from .subagents.reviewer_agent import (
    ReviewerSubAgent, ReviewReport, DECISION_CHOICES,
)
from .subagents.reviser_agent import ReviserSubAgent, RevisedPR
from .subagents.explorer_agent import ExplorerSubAgent, ExplorationResult
from .subagents.verifier_agent import VerifierSubAgent, VerificationResult
from .subagents.analyzer_agent import AnalyzerSubAgent, AnalyzerResult
from .subagents.generator_agent import GeneratorSubAgent, GeneratedPR
from .subagents.loop_agent import LoopSubAgent, LoopResult


@dataclass
class SkillResult:
    ok: bool
    payload: Any
    raw: Any = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
                 prompt_style: str = "engineering"):
        from .tools.base import BaseAdapter
        self.tool = tool_adapter or BaseAdapter()
        self.explore_skill = explore_skill or ExploreSkill()
        self.analyze_skill = analyze_skill or AnalyzeSkill()
        self.prompt_style = prompt_style
        self.subagent = ReviewerSubAgent(
            tool_adapter=self.tool,
            explorer=self.explore_skill.subagent,
            prompt_style=prompt_style,
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
    ) -> SkillResult:
        """deep=True 时返回论文 nested schema（decision:{recommendation,confidence}）"""
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
        report: ReviewReport = await self.subagent.execute(ctx)
        return SkillResult(
            ok=(report.decision in DECISION_CHOICES),
            payload=report.to_dict(deep=deep),
            raw=report,
            message=f"reviewed (prompt_style={report.prompt_style}, deep={deep})",
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
                 feedback_level: str = "full_feedback"):
        from .tools.base import BaseAdapter
        self.tool = tool_adapter or BaseAdapter()
        self.prompt_style = prompt_style
        self.feedback_level = feedback_level
        self.subagent = ReviserSubAgent(
            tool_adapter=self.tool,
            prompt_style=prompt_style,
            feedback_level=feedback_level,
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
            payload=res.to_dict(),
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
    ) -> SkillResult:
        res: VerificationResult = await self.subagent.execute({
            "pr_diff": pr_diff,
            "repo_path": repo_path,
            "test_info": test_info,
            "oracle": oracle,
        })
        return SkillResult(ok=res.patch_applied, payload=res.to_dict(), raw=res)


# ---------------------------------------------------------------------------
# Generate Skill
# ---------------------------------------------------------------------------
class GenerateSkill:
    name = "generate"

    def __init__(self, tool_adapter=None, explore_skill: Optional[ExploreSkill] = None):
        from .tools.base import BaseAdapter
        self.tool = tool_adapter or BaseAdapter()
        self.subagent = GeneratorSubAgent(tool_adapter=self.tool)
        self.explore_skill = explore_skill or ExploreSkill()

    async def execute(
        self,
        issue: str,
        hint: str = "",
        repo_path: Optional[str] = None,
    ) -> SkillResult:
        exploration: Dict[str, Any] = {}
        if repo_path:
            ex = await self.explore_skill.execute(
                issue=issue, repo_path=repo_path, max_steps=4
            )
            exploration = ex.payload or {}

        res: GeneratedPR = await self.subagent.execute({
            "issue": issue,
            "hint": hint,
            "repo_path": repo_path,
            "exploration": exploration,
        })
        return SkillResult(ok=bool(res.diff), payload=res.to_dict(), raw=res)


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
        deep: bool = False,
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
        }
        res: LoopResult = await self.subagent.execute(ctx)
        # If deep=True and the final result includes review payloads, callers want nested schema;
        # we re-emit review payloads if present in iterations[].
        payload = res.to_dict()
        if deep:
            for it in payload.get("iterations", []):
                # iteration notes may include review JSON in raw form; skip deep here.
                pass
        return SkillResult(ok=res.success, payload=payload, raw=res)
