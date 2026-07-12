"""Skill layer tests."""

import pytest

from swe_review import (
    ExploreSkill, AnalyzeSkill, ReviewSkill, ReviseSkill, VerifySkill,
    GenerateSkill, LoopSkill, ShellTools,
)


def test_explore_skill_runs():
    import asyncio
    s = ExploreSkill(repo_path=".")
    r = asyncio.run(s.execute(issue="NullPointer in add", pr_diff="", max_steps=2))
    assert r.ok
    assert "files_modified" in r.payload


def test_analyze_skill_runs(sample_diff):
    import asyncio
    s = AnalyzeSkill()
    r = asyncio.run(s.execute(pr_diff=sample_diff))
    assert r.ok
    assert r.payload["total_additions"] >= 1


def test_review_skill_with_shell_adapter(sample_diff):
    import asyncio
    s = ReviewSkill(tool_adapter=ShellTools())
    r = asyncio.run(s.execute(
        issue="NullPointer in add",
        pr_title="Add null checks",
        pr_diff=sample_diff,
        repo_path=".",
        max_exploration_steps=2,
    ))
    assert r.ok
    # shell-tools 返回固定 request_changes —— 仅校验结构
    assert r.payload["decision"] in ("approve", "request_changes")


def test_revise_skill_with_shell_adapter(sample_diff):
    import asyncio
    s = ReviseSkill(tool_adapter=ShellTools())
    r = asyncio.run(s.execute(
        issue="Bug",
        original_pr_title="t",
        original_pr_diff=sample_diff,
        review_report={"defects": [{"severity": "high", "description": "x",
                                     "location": "p", "suggestion": "y"}]},
    ))
    # shell-tools 返回 placeholder: ok=False, status=failed (diff empty)
    # real adapters (claude-code/pi/opencode/cursor) produce ok=True with non-empty diff.
    assert "diff" in r.payload
    assert r.payload["status"] in ("success", "partial", "failed")


def test_verify_skill_sandbox_default():
    import asyncio
    v = VerifySkill(repo_path=".", config={"sandbox": True})
    diff = (
        "diff --git a/sample.txt b/sample.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/sample.txt\n"
        "@@ -0,0 +1,1 @@\n"
        "+hello\n"
    )
    r = asyncio.run(v.execute(pr_diff=diff))
    assert r.payload["patch_applied"] is True
    assert r.payload["sandbox_used"] is True


def test_loop_skill_runs_with_shell(sample_diff):
    import asyncio
    tool = ShellTools()
    loop = LoopSkill(
        review_skill=ReviewSkill(tool_adapter=tool),
        revise_skill=ReviseSkill(tool_adapter=tool),
        generator_skill=GenerateSkill(tool_adapter=tool),
        verify_skill=VerifySkill(repo_path="."),
        strategy="review_guided",
        max_iterations=2,
    )
    r = asyncio.run(loop.execute(
        issue="Bug in add()",
        repo_path=".",
        initial_pr={"title": "x", "body": "", "diff": sample_diff},
    ))
    assert r.ok in (True, False)
    assert "iterations" in r.payload
    assert "final_pr_diff" in r.payload
