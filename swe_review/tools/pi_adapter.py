"""
PiAdapter - 通过 pi CLI 走 `pi --mode print -p` headless（subprocess 即可）

参考:
- /opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md
- ~/.pi/agent/skills/<name>/SKILL.md 是 Pi 标准发现路径
- 我们的 swe-review-* SKILL.md 通过 install.sh 已复制到该路径下

Pi 比 Claude Code 友好：subprocess.run 直接可调，不需要 pty。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from .base import MODEL_INHERITED_NOTE

from ._pty_runner import (
    run_subprocess, strip_ansi, strip_fences, extract_tokens_from_text,
)


PI_DEFAULT_SKILLS_ROOT = Path("~/.pi/agent/skills").expanduser()


def _find_cli() -> str:
    return (
        os.environ.get("PI_BIN")
        or shutil.which("pi")
        or "pi"
    )


class PiAdapter:
    name = "pi"

    def __init__(
        self,
        cli_path: Optional[str] = None,
        skills_dir: Optional[str] = None,
        timeout: int = 600,
        auto_install_skills: bool = True,
        skills_source_dir: Optional[Path] = None,
    ):
        self.cli_path = cli_path or _find_cli()
        # 设计原则：swe 循环不指定具体模型 —— 模型由宿主 CLI/环境决定，
        # adapter 永远不传 --model，也不读 *_MODEL 环境变量。
        self.skills_dir = Path(skills_dir).expanduser() if skills_dir else PI_DEFAULT_SKILLS_ROOT
        self.timeout = timeout
        self.auto_install_skills = auto_install_skills
        # 默认 skills source: 工程内 .claude/skills
        if skills_source_dir:
            self.skills_source_dir = Path(skills_source_dir)
        else:
            # from package resource: swe_review/.claude_skills/
            pkg_root = Path(__file__).resolve().parent.parent
            candidate = pkg_root / ".claude_skills"
            self.skills_source_dir = candidate if candidate.exists() else (pkg_root.parent / ".claude" / "skills")

    async def install_skills(self) -> Dict[str, str]:
        target = self.skills_dir / "swe-review"
        target.mkdir(parents=True, exist_ok=True)
        installed: Dict[str, str] = {}

        if not self.skills_source_dir.exists():
            return installed

        for skill_path in self.skills_source_dir.iterdir():
            if not skill_path.is_dir():
                continue
            if not (skill_path / "SKILL.md").exists():
                continue
            dest = target / skill_path.name
            if dest.is_symlink():
                # Symlink layout (repo as source of truth): NEVER copy into a
                # live link — that writes through it into the repo and nests.
                # Only repoint stale links.
                try:
                    stale = Path(os.readlink(str(dest))).resolve() != skill_path.resolve()
                except OSError:
                    stale = True
                if stale:
                    dest.unlink()
                    dest.symlink_to(skill_path)
                installed[skill_path.name] = str(dest)
                continue
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_path, dest)
            installed[skill_path.name] = str(dest)
        return installed

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Tuple[str, Dict[str, int]]:
        if self.auto_install_skills:
            await self.install_skills()

        # --system-prompt 替换 pi 默认的 coding-assistant 系统提示（那个提示会介绍
        # read/bash/edit/write 工具——即使 --no-tools 禁用了工具，模型看到工具说明
        # 仍会输出 <tool_call> 文本块；实测 deepseek 后端反复如此）。
        # 不加 --model：模型选择交给 pi 自身配置（设计原则）。
        # --no-tools：subagent 是纯文本生成（探索由本地 ExplorerSubAgent 完成）。
        # 若放开 write/edit/bash，review 子进程会真的改动目标仓库 —— 实测曾把
        # baseline worktree 改成 PR 应用后的状态，污染后续 verify 的 patch apply。
        argv = [
            self.cli_path, "--mode", "print", "--no-tools",
            "--system-prompt", system,
            "-p",
            user + "\n\nIMPORTANT: Output ONLY valid JSON. No prose, no markdown "
                   "fences. Tools are DISABLED in this session: do NOT emit "
                   "<tool_call> blocks, todo lists, or any function calls — "
                   "answer directly with the JSON object as your entire response.",
        ]

        env = {"PI_NO_TUI": "1"}
        out, err, rc = run_subprocess(argv, timeout=self.timeout, extra_env=env)
        text_clean = strip_ansi(out)
        if rc != 0:
            err_tail = (err or text_clean)[-500:]
            raise RuntimeError(
                f"pi --mode print failed (rc={rc}). stderr_tail={err_tail!r}"
            )
        text = strip_fences(text_clean)
        tok = extract_tokens_from_text(text, f"{system}\n{user}")
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
            "skills_dir": str(self.skills_dir),
            "model": MODEL_INHERITED_NOTE,
        }

    def diagnose(self) -> Dict[str, Any]:
        import shutil
        return {
            "tool": self.name,
            "cli_present": bool(shutil.which(self.cli_path)),
            "skills_dir": str(self.skills_dir),
            "command_template": f"{self.cli_path} --mode print -p <prompt>",
            "hint": (
                "Pi reads API keys from `~/.pi/agent/auth.json`. The installed "
                "swe-review skills are auto-copied to "
                "`~/.pi/agent/skills/swe-review/` and discovered on `pi` startup."
            ),
        }
