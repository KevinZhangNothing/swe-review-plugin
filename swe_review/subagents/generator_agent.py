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


@dataclass
class GeneratedPR:
    title: str
    body: str
    diff: str
    rationale: str
    confidence: float

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

        # 显式拒绝 oracle 字段
        for forbidden in ("golden_patch", "gold_patch", "oracle", "test_info"):
            if forbidden in context:
                raise ValueError(
                    f"[GeneratorSubAgent] '{forbidden}' is forbidden in generation context."
                )

        sys_p = self._system_prompt()
        usr_p = self._user_prompt(issue=issue, hint=hint, repo_path=repo_path, exploration=exploration)
        response, _ = await self._call(sys_p, usr_p)
        return self._parse(response)

    def _system_prompt(self) -> str:
        return (
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
            "## Output Schema\n"
            "{\n"
            '  "title": "short PR title",\n'
            '  "body": "1-3 sentence PR description",\n'
            '  "diff": "complete unified diff",\n'
            '  "rationale": "explanation",\n'
            '  "confidence": 0.0-1.0\n'
            "}"
        )

    def _user_prompt(
        self,
        issue: str,
        hint: str,
        repo_path: Optional[str],
        exploration: Dict[str, Any],
    ) -> str:
        # 截断 exploration 防止 prompt 溢出
        ex = json.dumps(exploration, indent=2, ensure_ascii=False)
        if len(ex) > 40_000:
            ex = ex[:40_000] + "\n... (exploration truncated)"
        return (
            f"## Issue\n{issue}\n\n"
            f"## Hint (optional, may be empty)\n{hint or 'N/A'}\n\n"
            f"## Repo Path\n{repo_path or 'unspecified (you decide files)'}\n\n"
            f"## Repository Context (from explorer)\n{ex}\n\n"
            "Emit the JSON per schema. The `diff` MUST be git apply-compatible."
        )

    async def _call(self, system: str, user: str):
        if self.tool_adapter:
            return await self.tool_adapter.chat(
                system=system, user=user, max_tokens=4096, temperature=0.2,
            )
        return "{}", {}

    def _parse(self, response: str) -> GeneratedPR:
        cleaned = _strip_fences(response)
        try:
            data = json.loads(cleaned)
            diff = data.get("diff", "") or ""
            ok = diff.startswith(("diff ", "diff --git")) and "@@" in diff
            return GeneratedPR(
                title=data.get("title") or "fix:",
                body=data.get("body") or "",
                diff=diff,
                rationale=data.get("rationale") or "",
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


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()
