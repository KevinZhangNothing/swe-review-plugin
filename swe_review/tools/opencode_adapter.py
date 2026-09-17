"""
OpenCodeAdapter - 按 opencode-cli-knowledge SKILL §1 走 `opencode run` 子进程

OpenCode 在 stdout 干净时不需要 pty（避免 ANSI）；subprocess.run 更稳。

配置注意：
  - 工程根目录存在 `.opencode/opencode.json` 时会被 opencode 加载；含未识别字段
    会让整个配置 invalid。本工程提供最小化版本以避免冲突。
  - `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` 禁用加载 `.claude/skills`（默认开启）
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Dict, Any, Optional, Tuple

from .base import MODEL_INHERITED_NOTE

from ._pty_runner import (
    run_subprocess, strip_ansi, strip_fences, extract_tokens_from_text,
)


def _find_cli() -> str:
    # CLI_BIN_* is the legacy name install.sh used to write; kept for existing env files.
    return (
        os.environ.get("OPENCODE_BIN")
        or os.environ.get("CLI_BIN_OPENCODE")
        or os.path.expanduser("~/.opencode/bin/opencode")
        or shutil.which("opencode")
        or "opencode"
    )


class OpenCodeAdapter:
    name = "opencode"

    def __init__(
        self,
        cli_path: Optional[str] = None,
        timeout: int = 1800,
    ):
        self.cli_path = cli_path or _find_cli()
        # 设计原则：swe 循环不指定具体模型 —— 永远不传 --model，也不读 *_MODEL 环境变量。
        # 模型由 opencode 工程配置/默认 provider 决定（也避免 unknown-server 错）。
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

        env = {
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "0",
        }
        # 不要把 cwd 强制设到工程根（外部调用方可能用别的 repo）

        # 不加 --model：让 opencode 用工程配置/默认 provider（设计原则，
        # 同时避免 unknown-server 错）。
        argv = [self.cli_path, "run", full_prompt]

        # to_thread, NOT a direct call: `run_subprocess` is synchronous, so calling
        # it straight from this coroutine blocks the event loop for the subprocess's
        # whole lifetime, silently serialising every `asyncio.gather` in the project
        # (best_of_n's candidate wave, the shard fan-out, health probes).
        out, err, rc = await asyncio.to_thread(
            run_subprocess, argv, timeout=self.timeout, extra_env=env,
        )
        text_clean = strip_ansi(out)
        if rc != 0:
            err_tail = (err or text_clean)[-500:]
            raise RuntimeError(
                f"opencode run failed (rc={rc}). stderr_tail={err_tail!r}"
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
            "command_template": f"{self.cli_path} run <prompt>",
            "hint": (
                "OpenCode reads `~/.config/opencode/opencode.json` for providers. "
                "If `opencode run` fails with `Unexpected server error`, the "
                "configured upstream provider is unavailable; check the model "
                "used or switch providers."
            ),
        }
