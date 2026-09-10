"""
BaseAdapter - 所有 tool adapter 的抽象基类
"""

from typing import Dict, Any, Tuple, Optional

#: get_status() model placeholder — the swe loop never pins a model; model
#: choice is inherited from the host CLI/environment (swe-review-loop hard
#: constraint #4).
MODEL_INHERITED_NOTE = "(inherited from CLI — never pinned by swe-review)"


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

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "configured": False}
