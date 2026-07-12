"""Adapter tests — only test construction & get_status (no LLM calls)."""

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
