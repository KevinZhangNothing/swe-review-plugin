"""
CursorAdapter - 严格按官方 SKILL §1 走 `agent --print --trust` + pty.spawn

为什么 pty：参考 cursor-cli-knowledge SKILL §1 "Agent run_terminal_cmd (常无 TTY)，
不能等同 `!` 终端，勿用 `script`，用 python3 + pty.spawn"。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, Any, Optional, Tuple

from ._pty_runner import run_in_pty, strip_ansi, strip_fences, extract_tokens_from_text


def _find_cli() -> str:
    return (
        os.environ.get("CURSOR_AGENT_BIN")
        or shutil.which("agent")
        or "agent"
    )


class CursorAdapter:
    name = "cursor"

    def __init__(
        self,
        cli_path: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 600,
    ):
        self.cli_path = cli_path or _find_cli()
        self.model = model or os.environ.get("CURSOR_MODEL")
        self.timeout = timeout

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Tuple[str, Dict[str, int]]:
        full_prompt = (
            f"{system}\n\n{user}\n\n"
            "IMPORTANT: Output ONLY valid JSON. No prose, no markdown fences."
        )

        # 顺序：agent --print --trust <prompt>
        # 不主动加 --model（按官方 SKILL §1 "不要默认加 --model"）。
        argv = [self.cli_path, "--print", "--trust", full_prompt]
        if self.model:
            argv += ["--model", self.model]

        out, err, rc = run_in_pty(argv, timeout=self.timeout)
        text_clean = strip_ansi(out)
        if rc != 0:
            err_tail = (err or text_clean)[-500:]
            raise RuntimeError(
                f"agent --print failed (rc={rc}). stderr_tail={err_tail!r}"
            )
        text = strip_fences(text_clean)
        tok = extract_tokens_from_text(text, full_prompt)
        return text, tok

    async def review(self, issue, pr_diff, repo_context=None):
        system = "You are an expert code reviewer. Output JSON only."
        user = (
            f"Issue:\n{issue}\n\nPR Diff:\n```diff\n{pr_diff}\n```\n\n"
            f"Context:\n{json.dumps(repo_context or {}, ensure_ascii=False)}\n\n"
            "Return JSON: {decision, confidence, summary, defects[]}."
        )
        return await self.chat(system=system, user=user)

    async def revise(self, issue, original_pr_diff, review_feedback):
        system = "You are a code revision expert. Output JSON only."
        user = (
            f"Issue:\n{issue}\n\nOriginal Diff:\n```diff\n{original_pr_diff}\n```\n\n"
            f"Feedback:\n{json.dumps(review_feedback, ensure_ascii=False, indent=2)}\n\n"
            "Return JSON: {title, body, diff, changes_summary, addressed_defect_indices[]}."
        )
        return await self.chat(system=system, user=user)

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cli": self.cli_path,
            "configured": bool(shutil.which(self.cli_path)),
            "model": self.model,
        }

    def diagnose(self) -> Dict[str, Any]:
        import shutil
        return {
            "tool": self.name,
            "cli_present": bool(shutil.which(self.cli_path)),
            "command_template": f"{self.cli_path} --print --trust <prompt>",
            "hint": (
                "Cursor auth is OAuth-based via the Cursor application. "
                "If `agent --print` fails with `usage limit` or "
                "`ActionRequiredError`, your Cursor Pro quota is exhausted; "
                "wait for reset or upgrade tier."
            ),
        }
