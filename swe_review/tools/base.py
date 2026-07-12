"""
BaseAdapter - 所有 tool adapter 的抽象基类
"""

from typing import Dict, Any, Tuple, Optional


class BaseAdapter:
    name: str = "base"

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Tuple[str, Dict[str, int]]:
        raise NotImplementedError

    async def review(self, issue: str, pr_diff: str,
                     repo_context: Optional[Dict[str, Any]] = None
                     ) -> Tuple[str, Dict[str, int]]:
        return await self.chat(system="You are a code reviewer.",
                               user=f"Issue:{issue}\nDiff:\n{pr_diff}")

    async def revise(self, issue: str, original_pr_diff: str,
                     review_feedback: Dict[str, Any]
                     ) -> Tuple[str, Dict[str, int]]:
        import json
        return await self.chat(
            system="You are a code reviser.",
            user=f"Issue:{issue}\nOriginal:\n{original_pr_diff}\n"
                 f"Feedback:{json.dumps(review_feedback)}",
        )

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "configured": False}
