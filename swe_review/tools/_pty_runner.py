"""
子进程运行器 — 严格按 4 款 CLI 的官方 SKILL 规范

参考:
- /Users/kevin/.agents/skills/claudecode-cli-knowledge/SKILL.md
- /Users/kevin/.agents/skills/cursor-cli-knowledge/SKILL.md
- /Users/kevin/.agents/skills/opencode-cli-knowledge/SKILL.md
- /opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md

WHY PTY:
  Claude Code 与 Cursor 的 headless 模式 (claude -p / agent --print) 在无 TTY
  环境会因 spawn 检测 / stdin 处理 / pty sizing 而 hang 或缺认证。所以官方
  SKILL 强制走 `python3 + pty.spawn`。

AUTH:
  Claude Code 把认证信息放在 ~/.claude/settings.json 的 env 段（ANTHROPIC_AUTH_TOKEN，
  ANTHROPIC_BASE_URL 等）。PTY 子进程是干净的 shell，必须把这些 env 字段一并
  merge，否则 401。
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Settings files scanned for ANTHROPIC_* env vars (auth proxy + base URL).
_CLAUDE_SETTINGS_PATHS = [
    Path("~/.claude/settings.local.json").expanduser(),
    Path("~/.claude/settings.json").expanduser(),
    Path("~/.claude.json").expanduser(),
]

# Only keys relevant to the network call need to be forwarded into the PTY child.
# ANTHROPIC_DEFAULT_*_MODEL are dashboard-only and don't affect `claude -p`.
_CLAUDE_ENV_PASS_THROUGH_KEYS = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_USE_GATEWAY",
}


def load_claude_env() -> Dict[str, str]:
    """Load ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL from Claude Code settings files.

    Claude Code stores auth credentials in `~/.claude/settings*.json`. A PTY child
    doesn't inherit those automatically, so we extract and forward them.
    """
    merged: Dict[str, str] = {}
    for p in _CLAUDE_SETTINGS_PATHS:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        env_section = data.get("env") or {}
        if not isinstance(env_section, dict):
            continue
        for k, v in env_section.items():
            if k in _CLAUDE_ENV_PASS_THROUGH_KEYS and isinstance(v, str):
                merged[k] = v
    return merged


def run_in_pty(
    argv: List[str],
    timeout: int = 600,
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, int]:
    """走 `python3 + pty.spawn` 模式跑 headless CLI（参考官方 SKILL §1）。

    关键修复：必须用 SIGCHLD + waitpid 正确拿退出码；同时清空 OPOST 防止 PTY 把 `\n`
    翻译成 `\r\n`（Windows-style）。这里我们 strip ANSI 后由调用方处理。
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    # bash -lc 不需要 —— 直接 exec argv 即可（避免 shell 引号转义陷阱）
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            if cwd:
                os.chdir(cwd)
            # Merge extra_env 到 child (子进程继承父 env，但为防御性显式合并)
            for k, v in (extra_env or {}).items():
                os.environ[k] = v
            os.execvpe(argv[0], argv, env)
        except OSError:
            os._exit(127)

    chunks: List[bytes] = []
    deadline = time.monotonic() + timeout
    status: Optional[int] = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                status = -1
                break

            try:
                rlist, _, _ = select.select([master_fd], [], [], min(1.0, remaining))
            except (OSError, ValueError):
                break
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    # PTY closed (child exited)
                    break
                chunks.append(data)
            # check child status
            try:
                pid_done, st = os.waitpid(pid, os.WNOHANG)
                if pid_done != 0:
                    status = os.waitstatus_to_exitcode(st)
                    # drain remaining
                    try:
                        while True:
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            chunks.append(data)
                    except OSError:
                        pass
                    break
            except ChildProcessError:
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if status is None:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            try:
                _, st = os.waitpid(pid, 0)
                status = os.waitstatus_to_exitcode(st)
            except OSError:
                status = -1

    return (
        b"".join(chunks).decode(errors="replace"),
        "",
        status if status is not None else -1,
    )


def run_subprocess(
    argv: List[str],
    timeout: int = 600,
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, int]:
    """OpenCode / Pi 这种不要 pty 的 CLI 直接 subprocess.run。"""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=env,
        )
        return p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        return (e.stdout or ""), (e.stderr or ""), -1


def strip_ansi(s: str) -> str:
    # CSI: \x1b[ ... letter  (covers all private modes including >, =, ?, <)
    s = re.sub(r"\x1b\[[\?>=<][0-9;]*[a-zA-Z@`]?", "", s)
    # Plain CSI with no intermediate
    s = re.sub(r"\x1b\[[0-9;]*[a-zA-Z@`]", "", s)
    # OSC: \x1b] ... \x07
    s = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", s)
    # Single-char ESC + symbol
    s = re.sub(r"\x1b[()][AB012]", "", s)
    # Cursor save/restore  \x1b7 / \x1b8 (and \x1b9)
    s = re.sub(r"\x1b[789]", "", s)
    # Misc controls: SI/SO
    s = re.sub(r"\x0f|\x0e", "", s)
    return s


def strip_fences(s: str) -> str:
    s = s.strip()
    # 1) 去掉围栏 ```json ... ```
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 2) 找首段 { ... } / [ ... ]
    m2 = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return s


def extract_tokens_from_text(text: str, prompt: str) -> Dict[str, int]:
    return {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(text) // 4),
        "total_tokens": max(1, (len(prompt) + len(text)) // 4),
    }
