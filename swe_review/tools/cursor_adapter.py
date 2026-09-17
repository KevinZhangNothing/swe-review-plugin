"""
CursorAdapter - 严格按官方 SKILL §1 走 `agent --print --trust` + pty.spawn

为什么 pty：参考 cursor-cli-knowledge SKILL §1 "Agent run_terminal_cmd (常无 TTY)，
不能等同 `!` 终端，勿用 `script`，用 python3 + pty.spawn"。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Dict, Any, Optional, Tuple

from ._pty_runner import run_in_pty, strip_ansi, strip_fences, extract_tokens_from_text
from .base import MODEL_INHERITED_NOTE


def _find_cli() -> str:
    # CLI_BIN_* is the legacy name install.sh used to write; kept for existing env files.
    return (
        os.environ.get("CURSOR_AGENT_BIN")
        or os.environ.get("CLI_BIN_CURSOR")
        or shutil.which("agent")
        or "agent"
    )


class CursorAdapter:
    name = "cursor"

    def __init__(
        self,
        cli_path: Optional[str] = None,
        timeout: int = 600,
    ):
        self.cli_path = cli_path or _find_cli()
        # 设计原则：swe 循环不指定具体模型 —— 永远不传 --model，也不读 *_MODEL 环境变量。
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
        # 不加 --model（按官方 SKILL §1 "不要默认加 --model"；模型由宿主环境决定）。
        argv = [self.cli_path, "--print", "--trust", full_prompt]

        # to_thread, NOT a direct call: `run_in_pty` is synchronous, so calling it
        # straight from this coroutine blocks the event loop for the child's whole
        # lifetime, silently serialising every `asyncio.gather` in the project.
        # (`run_in_pty` installs no signal handlers and waits on its own pid, so it
        # is safe to run from a worker thread.)
        out, err, rc = await asyncio.to_thread(
            run_in_pty, argv, timeout=self.timeout,
        )
        text_clean = strip_ansi(out)
        if rc != 0:
            err_tail = (err or text_clean)[-500:]
            raise RuntimeError(
                f"agent --print failed (rc={rc}). stderr_tail={err_tail!r}"
            )
        text = strip_fences(text_clean)
        tok = extract_tokens_from_text(text, full_prompt)
        return text, tok

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cli": self.cli_path,
            "configured": bool(shutil.which(self.cli_path)),
            "model": MODEL_INHERITED_NOTE,
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
