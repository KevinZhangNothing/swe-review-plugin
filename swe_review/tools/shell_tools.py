"""
shell_tools - 兜底的 shell 工具集

当所有 LLM CLI 都不可用时，用本地规则做最朴素的 review/revise。
主要意义是跑通 pipeline + 离线测试。
"""

import json
import re
from typing import Dict, Any, Optional, Tuple


class ShellTools:
    name = "shell"

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 0,
        temperature: float = 0.0,
    ) -> Tuple[str, Dict[str, int]]:
        # Fallback when no real CLI is configured/working: emit a deterministic
        # JSON shell so the pipeline can still exercise the data flow.
        fallback_response = {
            "decision": "request_changes",
            "confidence": 0.0,
            "summary": {"overall_assessment": "No LLM backend available; using shell fallback."},
            "defects": [],
        }
        text = json.dumps(fallback_response, ensure_ascii=False)
        tok = {
            "prompt_tokens": (len(system) + len(user)) // 4,
            "completion_tokens": 0,
            "total_tokens": (len(system) + len(user)) // 4,
        }
        return text, tok

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "configured": True, "backend": "shell-placeholder"}
