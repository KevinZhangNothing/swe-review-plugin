"""
shell_tools - 兜底的 shell 工具集

当所有 LLM CLI 都不可用时，用本地规则做最朴素的 review/revise。
主要意义是跑通 pipeline + 离线测试。
"""

from .base import BaseAdapter
import json
import re
from typing import Dict, Any, Optional, Tuple


class ShellTools(BaseAdapter):
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
        # `configured` answers "is this backend wired to a real LLM?" — for the
        # stub that is False, because it calls nothing. `is_placeholder` names
        # the actual nature of the adapter, so a consumer that wants to *list*
        # shell (or render it differently) does not have to overload one boolean
        # with two meanings. Both fields are additive; nothing is removed.
        return {
            "name": self.name,
            "configured": False,
            "is_placeholder": True,
            "backend": "shell-placeholder",
            "hint": "Offline stub: returns a fixed request_changes payload without "
                    "calling any LLM. Use it for pipeline smoke tests only.",
        }

    def diagnose(self) -> Dict[str, Any]:
        """Present for contract uniformity; the stub has nothing to diagnose.

        The other adapters probe a CLI binary here. `ShellTools` shells out to
        nothing, so it reports that fact instead of pretending to check.
        """
        return {
            "name": self.name,
            "is_placeholder": True,
            "checks": [],
            "note": "shell adapter calls no external process — nothing to diagnose",
        }
