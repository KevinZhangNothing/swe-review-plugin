"""
ClaudeCodeAdapter - 严格按 Claude Code 官方 SKILL §1 走 `claude -p` + pty.spawn

为什么 pty：
  Claude Code 的 headless 模式在无 TTY 环境（agent run_terminal_cmd）会 hang
  或丢认证。官方 SKILL 强制 `python3 + pty.spawn`。

为什么 load_claude_env：
  Claude Code 把 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 等认证配置放在
  ~/.claude/settings.json 的 env 段。PTY 起的 bash 是干净的 shell，必须把
  这个 env merge 进去，否则 401。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from typing import Dict, Any, Optional, Tuple

from ._pty_runner import (
    load_claude_env, run_in_pty, strip_ansi, strip_fences, extract_tokens_from_text,
)


def _find_cli() -> str:
    # CLI_BIN_* is the legacy name install.sh used to write into .env.local;
    # accepted so existing files keep working (they were silently ignored before).
    return (
        os.environ.get("CLAUDE_CODE_BIN")
        or os.environ.get("CLI_BIN_CLAUDE_CODE")
        or shutil.which("claude")
        or "claude"
    )


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(
        self,
        cli_path: Optional[str] = None,
        timeout: int = 1800,
    ):
        self.cli_path = cli_path or _find_cli()
        # 设计原则：swe 循环不指定具体模型 —— 永远不传 --model，也不读 *_MODEL 环境变量。
        self.timeout = timeout
        self._claude_env = load_claude_env()

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        output_format: str = "text",
    ) -> Tuple[str, Dict[str, int]]:
        full_prompt = (
            f"{system}\n\n{user}\n\n"
            "IMPORTANT: Output ONLY valid JSON. No prose, no markdown fences."
        )

        # claude CLI: positional prompt + flags. Order matters.
        argv = [self.cli_path, "-p", "--bare"]
        if output_format and output_format != "text":
            argv += ["--output-format", output_format]
        # 不加 --model（遵守官方 SKILL §1："don't default to --model"；模型由宿主环境决定）
        argv.append(full_prompt)

        # to_thread, NOT a direct call: `run_in_pty` is synchronous, so calling it
        # straight from this coroutine blocks the event loop for the child's whole
        # lifetime, silently serialising every `asyncio.gather` in the project.
        # (`run_in_pty` installs no signal handlers and waits on its own pid, so it
        # is safe to run from a worker thread.)
        out, err, rc = await asyncio.to_thread(
            run_in_pty, argv, timeout=self.timeout, extra_env=self._claude_env,
        )
        text_clean = strip_ansi(out)
        if rc != 0:
            # 把 rc -1 也判作失败但保留 stdout（CLI 在 pipefail 下可能诡异）
            err_tail = (err or text_clean)[-400:]
            raise RuntimeError(
                f"claude -p failed (rc={rc}). stderr_tail={err_tail!r}"
            )
        text = strip_fences(text_clean)
        tok = extract_tokens_from_text(text, full_prompt)

        # --output-format json 时 claude 给的是 {"type":"result","result":"...", "usage":...}
        if output_format == "json" and not text_clean.startswith("{"):
            # 解析失败，text 已尽力剥 fence
            pass
        elif output_format == "json":
            try:
                import json as _json
                obj = _json.loads(text_clean.strip())
                t = obj.get("result") or obj.get("text") or text
                text = strip_fences(t)
                # usage 可选
            except Exception:
                pass

        return text, tok

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cli": self.cli_path,
            "configured": bool(shutil.which(self.cli_path)),
            "auth_env_keys": sorted(self._claude_env.keys())[:3] if self._claude_env else [],
        }

    def diagnose(self) -> Dict[str, Any]:
        """Returns a diagnostic record useful for `swe-review health`-style output."""
        import socket
        env = self._claude_env
        base = env.get("ANTHROPIC_BASE_URL", "").replace("http://", "").split(":")[0]
        port = int(env.get("ANTHROPIC_BASE_URL", "").rsplit(":", 1)[1].split("/")[0]) if ":" in env.get("ANTHROPIC_BASE_URL", "") else 0
        # Probe TCP connectivity
        tcp_ok = False
        if base and port:
            try:
                with socket.create_connection((base, port), timeout=2):
                    tcp_ok = True
            except Exception:
                tcp_ok = False
        return {
            "tool": self.name,
            "cli_present": bool(shutil.which(self.cli_path)),
            "env_has_auth_token": "ANTHROPIC_AUTH_TOKEN" in env,
            "env_token_is_proxy_managed": env.get("ANTHROPIC_AUTH_TOKEN") == "PROXY_MANAGED",
            "configured_base_url": env.get("ANTHROPIC_BASE_URL"),
            "tcp_to_base_url_ok": tcp_ok,
            "hint": (
                "PROXY_MANAGED means Claude Code is configured to use a local proxy"
                " (Headroom) that must be running. Auth token is injected only via"
                " the proxy. Either start the proxy or set ANTHROPIC_API_KEY."
            ) if env.get("ANTHROPIC_AUTH_TOKEN") == "PROXY_MANAGED" else None,
        }
