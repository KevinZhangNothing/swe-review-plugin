"""Workspace preparation must fail closed without touching the source repo."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from swe_review.subagents import verifier_agent
from swe_review.subagents.verifier_agent import VerifierSubAgent


PATCH = """--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


@pytest.mark.parametrize("failure,sandbox", [
    ("create", True), ("copy", True), ("missing", True), ("file", True),
    ("missing", False), ("file", False),
])
def test_preparation_failure_stops_all_work(tmp_path, monkeypatch, failure, sandbox):
    repo = tmp_path / "source"
    repo.mkdir()
    source = repo / "hello.py"
    source.write_text("value = 1\n")
    work = tmp_path / "sandbox"

    def create(**kwargs):
        if failure == "create":
            raise OSError("temporary directory unavailable")
        work.mkdir()
        return str(work)

    def copy(*args, **kwargs):
        partial = work / "repo"
        partial.mkdir()
        (partial / "partial.txt").write_text("partial copy")
        raise OSError("copy interrupted")

    create_mock = Mock(side_effect=create)
    copy_mock = Mock(side_effect=copy)
    monkeypatch.setattr(verifier_agent.tempfile, "mkdtemp", create_mock)
    monkeypatch.setattr(verifier_agent.shutil, "copytree", copy_mock)
    target = repo
    if failure == "missing":
        target = tmp_path / "missing"
    elif failure == "file":
        target = source
    agent = VerifierSubAgent(str(target), config={"sandbox": sandbox})
    apply = Mock(return_value=True)
    build = AsyncMock(return_value=[])
    tests = AsyncMock(return_value=[])
    monkeypatch.setattr(agent, "_apply_patch", apply)
    monkeypatch.setattr(agent, "_run_build_checks", build)
    monkeypatch.setattr(agent, "_run_tests", tests)

    result = asyncio.run(agent.execute({"pr_diff": PATCH, "test_runner": ["pytest"]}))

    assert result.passed is False
    assert result.patch_applied is False
    assert result.sandbox_used is False
    assert result.resolution_status == "unknown"
    assert result.confidence == 0.0
    assert result.test_results == []
    assert result.build_results == []
    assert "Failed to prepare verification workspace:" in result.details
    apply.assert_not_called()
    build.assert_not_called()
    tests.assert_not_called()
    assert source.read_text() == "value = 1\n"
    assert not work.exists()
    if failure in ("missing", "file"):
        create_mock.assert_not_called()
    if failure != "copy":
        copy_mock.assert_not_called()


@pytest.mark.parametrize("sandbox", [True, False])
def test_real_patch_preserves_workspace_mode(tmp_path, monkeypatch, sandbox):
    repo = tmp_path / "source"
    repo.mkdir()
    source = repo / "hello.py"
    source.write_text("value = 1\n")
    work = tmp_path / "sandbox"

    def create(**kwargs):
        work.mkdir()
        return str(work)

    create_mock = Mock(side_effect=create)
    monkeypatch.setattr(verifier_agent.tempfile, "mkdtemp", create_mock)
    agent = VerifierSubAgent(str(repo), config={"sandbox": sandbox})
    result = asyncio.run(agent.execute({"pr_diff": PATCH}))

    assert result.patch_applied is True
    assert result.sandbox_used is sandbox
    assert result.build_results[0]["passed"] is True
    assert result.resolution_status == "unknown"
    assert result.passed is False  # No tests: preserve existing verifier semantics.
    assert source.read_text() == ("value = 1\n" if sandbox else "value = 2\n")
    assert not work.exists()
    if not sandbox:
        create_mock.assert_not_called()


def test_sandbox_is_cleaned_when_patch_raises(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    work = tmp_path / "sandbox"

    def create(**kwargs):
        work.mkdir()
        return str(work)

    monkeypatch.setattr(verifier_agent.tempfile, "mkdtemp", create)
    agent = VerifierSubAgent(str(repo))
    monkeypatch.setattr(agent, "_apply_patch", Mock(side_effect=RuntimeError("patch failed")))
    with pytest.raises(RuntimeError, match="patch failed"):
        asyncio.run(agent.execute({"pr_diff": PATCH}))
    assert not work.exists()


#: A hunk whose declared line count does not match its body — git calls this a
#: corrupt patch and names the line, which is exactly the detail that used to be
#: swallowed.
CORRUPT = ("diff --git a/hello.py b/hello.py\n"
           "--- a/hello.py\n"
           "+++ b/hello.py\n"
           "@@ -1,5 +1,5 @@\n"
           "-value = 1\n"
           "+value = 2\n")


def test_apply_failure_surfaces_gits_own_reason(tmp_path):
    """`Failed to apply patch (syntax or context mismatch)` hid the actionable
    part. A malformed candidate must report what git actually said."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "hello.py").write_text("value = 1\n")
    agent = VerifierSubAgent(str(repo), config={"sandbox": True})

    applied, reason = agent._apply_patch(CORRUPT, repo)
    assert applied is False
    assert reason and reason != "syntax or context mismatch"

    res = asyncio.run(agent.execute({"pr_diff": CORRUPT, "repo_path": str(repo)}))
    assert res.patch_applied is False
    assert res.details.startswith("Failed to apply patch: ")
    # the legacy generic wording must be gone
    assert "(syntax or context mismatch)" not in res.details
    assert res.details != "Failed to apply patch: "


def test_apply_failure_reason_is_empty_only_on_success(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "hello.py").write_text("value = 1\n")
    agent = VerifierSubAgent(str(repo), config={"sandbox": True})
    applied, reason = agent._apply_patch(PATCH, repo)
    assert (applied, reason) == (True, "")
    # empty diff is reported, not silently treated as an apply attempt
    assert agent._apply_patch("", repo) == (False, "empty diff")
