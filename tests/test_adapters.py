"""Adapter tests — only test construction & get_status (no LLM calls)."""

import os
from pathlib import Path

from swe_review import (
    ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter, ShellTools,
)


def test_claude_code_status():
    s = ClaudeCodeAdapter().get_status()
    assert s["name"] == "claude_code"
    assert "cli" in s


def test_cursor_status():
    s = CursorAdapter().get_status()
    assert s["name"] == "cursor"
    assert "cli" in s


def test_opencode_status():
    s = OpenCodeAdapter().get_status()
    assert s["name"] == "opencode"
    assert "cli" in s


def test_pi_status():
    s = PiAdapter(skills_dir="/tmp/pi-test-skills").get_status()
    assert s["name"] == "pi"
    assert "skills_dir" in s


def test_shell_status():
    s = ShellTools().get_status()
    assert s["name"] == "shell"
    assert s["configured"] is True


def test_adapters_share_chat_signature():
    """All concrete adapters must implement `chat(system, user)` returning (str, dict)."""
    for cls in (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter, ShellTools):
        assert hasattr(cls, "chat")
        assert hasattr(cls, "review")
        assert hasattr(cls, "revise")


def test_pi_install_skills_is_symlink_safe(tmp_path):
    """Regression: with the symlinked skill layout (repo = source of truth),
    install_skills must NOT copy through the live link into the repo, must not
    crash on rmtree-of-symlink, and must repoint stale links instead."""
    import asyncio

    src = tmp_path / "src"
    (src / "swe-review-x").mkdir(parents=True)
    (src / "swe-review-x" / "SKILL.md").write_text("x")
    (src / "swe-review-y").mkdir()
    (src / "swe-review-y" / "SKILL.md").write_text("y")

    tgt = tmp_path / "home" / ".pi" / "agent" / "skills" / "swe-review"
    tgt.mkdir(parents=True)
    live = tgt / "swe-review-x"
    live.symlink_to(src / "swe-review-x")          # correct, live link
    stale = tgt / "swe-review-y"
    stale.symlink_to(tmp_path / "elsewhere")       # stale link

    a = PiAdapter(skills_dir=str(tmp_path / "home" / ".pi" / "agent" / "skills"),
                  skills_source_dir=src)
    installed = asyncio.run(a.install_skills())
    assert set(installed) == {"swe-review-x", "swe-review-y"}

    # live link preserved as a link; source dir NOT polluted by copy-through
    assert live.is_symlink()
    assert sorted(p.name for p in (src / "swe-review-x").iterdir()) == ["SKILL.md"]
    # stale link repointed at the source
    assert stale.is_symlink()
    assert Path(os.readlink(str(stale))) == src / "swe-review-y"
