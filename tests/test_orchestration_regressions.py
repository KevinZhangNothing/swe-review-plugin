"""Regressions for orchestration defects found in the v1.0.0 architecture review.

Every test here pins a *silent* failure mode — one that exited 0 and produced a
plausible-looking result while doing the wrong thing. That shape is why they
survived 260 green unit tests: those tests are cut by module, and each defect
lived in the seam between an adapter/CLI and a loop strategy.
"""

import asyncio
import tempfile

import pytest

from swe_review import GenerateSkill, ReviewSkill
from swe_review.subagents.loop_agent import LoopSubAgent
from swe_review.tools.host_adapter import HostAdapter, HostTurnRequired


from conftest import (
    SEED_DIFF, USAGE_GEN, USAGE_REVIEW, USAGE_REVISE,
    StubAdapter, StubGenerator, StubReview, StubRevise, StubVerify,
)

# Local aliases keep the test bodies readable; the shapes live in conftest.
_Gen, _Review, _Revise, _Verify = (
    StubGenerator, StubReview, StubRevise, StubVerify)


# ---------------------------------------------------------------------------
# P0-1: HostTurnRequired must never be swallowed by error isolation
# ---------------------------------------------------------------------------

def test_host_turn_required_propagates_through_best_of_n():
    """`best_of_n` used to convert this control signal into an empty candidate.

    Result: a run under `--tool host` printed "no candidate approved+verified",
    exited 0, and left the prompts unanswered on disk forever.
    """
    host = HostAdapter(work_dir=tempfile.mkdtemp())
    loop = LoopSubAgent(
        review_skill=ReviewSkill(tool_adapter=host),
        generator_skill=GenerateSkill(tool_adapter=host),
        strategy="best_of_n",
        n_best_of=2,
    )
    with pytest.raises(HostTurnRequired):
        asyncio.run(loop.execute(
            {"issue": "x", "repo_path": None, "n_best_of": 2}))


def test_host_turn_required_propagates_through_review_wave():
    """The review gather had the same bug as the generate gather."""
    from pathlib import Path

    class _HostReview:
        async def execute(self, **kwargs):
            # Any path works; HostTurnRequired only needs one for its message.
            raise HostTurnRequired("0123456789abcdef",
                                   Path(tempfile.gettempdir()) / "req.json")

    loop = LoopSubAgent(review_skill=_HostReview(), generator_skill=_Gen(),
                        strategy="best_of_n", n_best_of=2)
    with pytest.raises(HostTurnRequired):
        asyncio.run(loop.execute({"issue": "x", "repo_path": None, "n_best_of": 2}))


# ---------------------------------------------------------------------------
# P0-2: an explicit initial_pr must be respected by every strategy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strategy", ["review_guided", "best_of_n", "hybrid"])
def test_initial_pr_is_never_discarded(strategy):
    """`best_of_n` used to ignore --initial-pr-diff and generate instead.

    A user asking "review/revise THIS patch" silently got a different patch.
    """
    gen = _Gen()
    loop = LoopSubAgent(review_skill=_Review(), revise_skill=_Revise(),
                        generator_skill=gen, strategy=strategy, n_best_of=3)
    result = asyncio.run(loop.execute({
        "issue": "x", "strategy": strategy,
        "initial_pr": {"diff": SEED_DIFF}, "n_best_of": 3,
        "max_iterations": 1,
    }))
    if strategy == "review_guided":
        # review_guided never generates when a diff is supplied
        assert gen.calls == 0
    else:
        assert gen.calls == 0, f"{strategy} regenerated despite initial_pr"
        assert result.iterations[0].phase == "generate"
        assert result.iterations[0].decision == "skipped"
    assert result.final_pr_diff == SEED_DIFF


# ---------------------------------------------------------------------------
# P1-1: shared exploration must not be re-run per candidate
# ---------------------------------------------------------------------------

def test_best_of_n_reuses_one_exploration():
    """N candidates each ran a full explorer pass over the same repo+issue."""
    explore_calls = {"n": 0}
    review = ReviewSkill(tool_adapter=None)
    original = review.subagent.explorer.execute

    async def counted(ctx):
        explore_calls["n"] += 1
        return await original(ctx)

    review.subagent.explorer.execute = counted

    repo = tempfile.mkdtemp()
    with open(f"{repo}/x", "w") as fh:
        fh.write("a\n")

    loop = LoopSubAgent(review_skill=review, generator_skill=_Gen(),
                        strategy="best_of_n", n_best_of=3)
    asyncio.run(loop.execute(
        {"issue": "fix x", "repo_path": repo, "n_best_of": 3}))
    assert explore_calls["n"] == 0, (
        "the shared exploration should make per-candidate explorer passes "
        "unnecessary")


def test_review_skill_forwards_supplied_repo_context():
    """The reuse path must actually reach the reviewer, not just skip its own explore."""
    review = _Review()
    loop = LoopSubAgent(review_skill=review, generator_skill=_Gen(),
                        strategy="best_of_n", n_best_of=2)
    asyncio.run(loop.execute({"issue": "x", "repo_path": None, "n_best_of": 2}))
    assert review.kwargs_seen, "review was never called"
    assert all("repo_context" in kw for kw in review.kwargs_seen)


def test_real_review_skill_forwards_repo_context_to_reviewer():
    """End-to-end through the REAL ReviewSkill → ReviewerSubAgent boundary.

    The stub above proves the loop passes the kwarg; it does not prove
    `ReviewSkill.execute` forwards it into the reviewer's context. That second
    hop is what actually suppresses the per-candidate explorer pass, so pin it
    with the real classes and only the LLM call stubbed out.
    """
    import json

    from swe_review.subagents.reviewer_agent import ReviewerSubAgent

    seen: dict = {}

    class _CapturingReviewer:
        """Stands in for ReviewerSubAgent.execute, capturing the context."""

        async def execute(self, context):
            seen.update(context)
            return _empty_report()

    def _empty_report():
        from swe_review.subagents.reviewer_agent import ReviewReport
        return ReviewReport(decision="approve", confidence=0.9,
                            summary={"overall_assessment": "stub"})

    skill = ReviewSkill(tool_adapter=None)
    skill.subagent = _CapturingReviewer()

    sentinel = {"files_modified": ["sentinel.py"], "steps": 7, "truncated": False}
    result = asyncio.run(skill.execute(
        issue="i", pr_title="t", pr_diff=SEED_DIFF,
        repo_path="/nonexistent", repo_context=sentinel))

    assert result.ok
    assert seen["repo_context"] == sentinel, (
        "ReviewSkill dropped repo_context before the reviewer saw it")


def test_real_reviewer_skips_explore_when_context_supplied():
    """With repo_context supplied the reviewer must not run its own explorer."""
    from swe_review.subagents.reviewer_agent import ReviewerSubAgent
    from swe_review.subagents.explorer_agent import ExplorerSubAgent

    calls = {"n": 0}

    class _CountingExplorer(ExplorerSubAgent):
        async def execute(self, context):
            calls["n"] += 1
            return await super().execute(context)

    reviewer = ReviewerSubAgent(tool_adapter=StubAdapter("stub"),
                                explorer=_CountingExplorer(),
                                prompt_style="concise")
    asyncio.run(reviewer.execute({
        "issue": "i", "pr_title": "t", "pr_diff": SEED_DIFF,
        "repo_path": ".", "prompt_style": "concise",
        "repo_context": {"files_modified": ["x"], "steps": 3},
    }))
    assert calls["n"] == 0, "reviewer re-explored despite a supplied repo_context"


# ---------------------------------------------------------------------------
# P1-2: exploration degradation must be distinguishable from success
# ---------------------------------------------------------------------------

def test_explore_timeout_is_tagged_not_silent():
    """A collapsed `except (TimeoutError, Exception)` hid WHY evidence was missing."""
    from swe_review.subagents.explorer_agent import ExplorerSubAgent
    from swe_review.subagents.reviewer_agent import ReviewerSubAgent

    class _Slow(ExplorerSubAgent):
        async def execute(self, context):
            await asyncio.sleep(5)

    reviewer = ReviewerSubAgent(tool_adapter=None, explorer=_Slow(),
                                explore_timeout=0.01)
    ctx, steps = asyncio.run(reviewer._explore(
        repo_path=".", issue="i", pr_diff="", focus_files=[], max_steps=1))
    assert steps == 0
    assert ctx["exploration_degraded"] == "explore_timeout"


def test_explore_error_is_tagged_with_cause():
    from swe_review.subagents.explorer_agent import ExplorerSubAgent
    from swe_review.subagents.reviewer_agent import ReviewerSubAgent

    class _Boom(ExplorerSubAgent):
        async def execute(self, context):
            raise RuntimeError("grep exploded")

    reviewer = ReviewerSubAgent(tool_adapter=None, explorer=_Boom())
    ctx, _ = asyncio.run(reviewer._explore(
        repo_path=".", issue="i", pr_diff="", focus_files=[], max_steps=1))
    assert "explore_error" in ctx["exploration_degraded"]
    assert "grep exploded" in ctx["exploration_degraded"]


# ---------------------------------------------------------------------------
# P1-3: token accounting must include generation and revision
# ---------------------------------------------------------------------------

def test_token_usage_total_includes_generate_and_revise():
    """Only review usage was accumulated — the two most expensive call types
    (N generations in best_of_n, one revision per iteration) were invisible."""
    loop = LoopSubAgent(review_skill=_Review(decision="request_changes"),
                        revise_skill=_Revise(), generator_skill=_Gen())
    result = asyncio.run(loop.execute({
        "issue": "x", "strategy": "review_guided", "max_iterations": 2}))
    total = result.token_usage_total["total_tokens"]
    # 1 generate (300) + 1 revise (15) + 2 reviews (30 each)
    assert total == 300 + 15 + 60, f"got {total}"


def test_best_of_n_token_total_includes_every_candidate():
    loop = LoopSubAgent(review_skill=_Review(decision="request_changes"),
                        generator_skill=_Gen(), strategy="best_of_n", n_best_of=2)
    result = asyncio.run(loop.execute({
        "issue": "x", "strategy": "best_of_n", "n_best_of": 2}))
    total = result.token_usage_total["total_tokens"]
    assert total >= 2 * USAGE_GEN["total_tokens"], (
        f"all generated candidates must be counted; got {total}")

def test_sharded_path_carries_exploration_degradation():
    """The degradation tag must survive the sharding path too.

    `execute` fills `exploration_degraded` AFTER the shard/single-call branch,
    so both paths are covered — but that is invisible from either branch's code.
    Pin it, or a future refactor that moves the assignment inside one branch
    would leave large-diff reviews silently unattributed.
    """
    import json

    from swe_review.subagents.diff_sharding import plan_shards, should_shard
    from swe_review.subagents.explorer_agent import ExplorerSubAgent
    from swe_review.subagents.reviewer_agent import ReviewerSubAgent

    class _Boom(ExplorerSubAgent):
        async def execute(self, context):
            raise RuntimeError("explorer died")

    # A diff that definitely exceeds the budget and splits into many shards.
    blocks = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n"
        f"@@ -1 +1 @@\n-a\n+b{'x' * 400}\n"
        for i in range(80))
    assert should_shard(blocks, 5_000)
    assert len(plan_shards(blocks, 5_000)) > 1

    reviewer = ReviewerSubAgent(
        tool_adapter=StubAdapter("stub", {
            "decision": "request_changes", "confidence": 0.5,
            "summary": {"overall_assessment": "x"},
            "scores": {}, "findings": [], "defects": [],
        }),
        explorer=_Boom(), shard_budget_chars=5_000,
        prompt_style="engineering")
    report = asyncio.run(reviewer.execute({
        "issue": "i", "pr_title": "t", "pr_diff": blocks,
        "repo_path": ".", "prompt_style": "engineering",
    }))

    assert report.exploration_degraded, (
        "sharded review lost the exploration degradation tag")
    assert "explore_error" in report.exploration_degraded


def test_explore_error_detail_is_bounded():
    """The degraded reason is serialized into CLI JSON that lands in CI logs.

    An unbounded `str(exc)` can carry an absolute path or config fragment. The
    type name is what triage needs; the body is bounded.
    """
    from swe_review.subagents.reviewer_agent import (
        _EXC_DETAIL_CHARS,
        _safe_exc_summary,
    )

    long_message = "/Users/someone/private/project/" + ("x" * 5_000)
    summary = _safe_exc_summary(RuntimeError(long_message))

    assert summary.startswith("RuntimeError: ")
    assert len(summary) < _EXC_DETAIL_CHARS + 64
    assert "truncated" in summary
    assert "x" * 300 not in summary


def test_explore_error_summary_keeps_type_when_message_empty():
    assert _safe_exc_summary_public(ValueError()) == "ValueError"


def _safe_exc_summary_public(exc):
    from swe_review.subagents.reviewer_agent import _safe_exc_summary
    return _safe_exc_summary(exc)


def test_shared_exploration_empty_is_marked_degraded():
    """best_of_n's shared-exploration shortcut must state its own health.

    Per-candidate exploration tags its failures, but the shared pass bypassed
    that path: a `{}` result reached every review as a bare empty dict, so the
    report looked repository-grounded while using no evidence at all.
    """
    repo = tempfile.mkdtemp()
    with open(f"{repo}/x", "w") as fh:
        fh.write("a\n")

    for empty in ({}, None):
        review = _Review(decision="approve")
        loop = LoopSubAgent(review_skill=review, generator_skill=_Gen(exploration=empty),
                            strategy="best_of_n", n_best_of=1)
        asyncio.run(loop.execute({"issue": "i", "repo_path": repo, "n_best_of": 1}))
        ctx = review.kwargs_seen[0]["repo_context"]
        assert "shared_exploration_empty" in ctx["exploration_degraded"], (
            f"empty shared exploration ({empty!r}) was not marked degraded")


def test_shared_exploration_with_evidence_is_not_marked():
    """Negative control: real context must not be tagged as degraded."""
    repo = tempfile.mkdtemp()
    with open(f"{repo}/x", "w") as fh:
        fh.write("a\n")

    review = _Review(decision="approve")
    loop = LoopSubAgent(
        review_skill=review,
        generator_skill=_Gen(exploration={"files_modified": ["x"], "steps": 1}),
        strategy="best_of_n", n_best_of=1)
    asyncio.run(loop.execute({"issue": "i", "repo_path": repo, "n_best_of": 1}))
    assert "exploration_degraded" not in review.kwargs_seen[0]["repo_context"]


def test_shared_exploration_without_repo_says_so():
    """No repo_path has a different cause than "explorer came back empty"."""
    review = _Review(decision="approve")
    loop = LoopSubAgent(review_skill=review, generator_skill=_Gen(exploration=None),
                        strategy="best_of_n", n_best_of=1)
    asyncio.run(loop.execute({"issue": "i", "repo_path": None, "n_best_of": 1}))
    reason = review.kwargs_seen[0]["repo_context"]["exploration_degraded"]
    assert "shared_exploration_skipped" in reason
