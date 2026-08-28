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


def test_adapters_never_pin_a_model(monkeypatch):
    """Regression for swe-review-loop hard constraint #4: the loop must never
    specify a concrete model — no model= ctor param, no *_MODEL env reads,
    and no --model flag may ever reach the spawned CLI argv."""
    import asyncio
    import inspect

    for cls in (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter):
        sig = inspect.signature(cls.__init__)
        assert "model" not in sig.parameters, f"{cls.__name__} re-added a model= parameter"

    # Even if *_MODEL vars are exported, they must stay inert.
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "evil-model")
    monkeypatch.setenv("CURSOR_MODEL", "evil-model")
    monkeypatch.setenv("OPENCODE_MODEL", "evil-model")
    monkeypatch.setenv("PI_MODEL", "evil-model")

    captured = {}

    def fake_run(argv, *a, **kw):
        captured["argv"] = list(argv)
        return "{}", "", 0

    from swe_review.tools import (
        claude_code_adapter as cc, cursor_adapter as cu,
        opencode_adapter as oc, pi_adapter as pi,
    )
    monkeypatch.setattr(cc, "run_in_pty", fake_run)
    monkeypatch.setattr(cu, "run_in_pty", fake_run)
    monkeypatch.setattr(oc, "run_subprocess", fake_run)
    monkeypatch.setattr(pi, "run_subprocess", fake_run)

    for cls in (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter):
        captured.clear()
        adapter = cls()
        asyncio.run(adapter.chat(system="s", user="u"))
        joined = " ".join(captured["argv"])
        assert "--model" not in captured["argv"], f"{cls.__name__} passed --model"
        assert "evil-model" not in joined, f"{cls.__name__} leaked a model name"

    captured.clear()
    adapter = PiAdapter(skills_dir="/tmp/pi-test-skills", auto_install_skills=False)
    asyncio.run(adapter.chat(system="s", user="u"))
    joined = " ".join(captured["argv"])
    assert "--model" not in captured["argv"], "PiAdapter passed --model"
    assert "evil-model" not in joined, "PiAdapter leaked a model name"
    # Hard constraint #5: subagent calls are pure text generation.
    assert "--no-tools" in captured["argv"], "PiAdapter must run with --no-tools"
