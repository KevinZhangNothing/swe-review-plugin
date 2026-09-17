"""Large-diff sharding: deterministic split + concurrent map-reduce review.

Context: `pr_diff` was the single prompt input with NO budget (repo_context 60k,
exploration 40k, reviser diff 50k), so a large change went out as one unbounded
request — measured at a 45.6k-token prompt for a 180 KB diff, 98% of it raw diff —
and the whole review serialised into that one call.

The invariants these tests protect:
  - the split is deterministic and LOSSLESS (every file, every hunk, exactly once);
  - a file is never split across shards (half a file invents "missing" findings);
  - small diffs do not shard at all (zero regression);
  - findings survive by DETERMINISTIC merge — a model never decides whether a
    shard's evidence is kept;
  - a broken shard degrades, it does not sink the review; an all-broken shard set
    never fabricates an approve.
"""
import asyncio
import json

import pytest

from swe_review.subagents import diff_sharding as ds
from swe_review.subagents.diff_sharding import (
    DiffShard,
    plan_shards,
    should_shard,
    split_file_blocks,
)
from swe_review.subagents.reviewer_agent import ReviewerSubAgent, SCORE_DIMENSIONS


def _diff(*files):
    """`_diff(("a.py", 2), ("b.py", 1))` -> a diff whose files have N hunks each."""
    out = []
    for path, hunks in files:
        out.append(f"diff --git a/{path} b/{path}")
        out.append(f"--- a/{path}")
        out.append(f"+++ b/{path}")
        for h in range(hunks):
            out.append(f"@@ -{h + 1},1 +{h + 1},1 @@")
            out.append(f"-old_{path}_{h}")
            out.append(f"+new_{path}_{h}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# deterministic split
# ---------------------------------------------------------------------------

def test_split_file_blocks_preserves_everything():
    text = _diff(("a.py", 2), ("b.py", 1))
    preamble, blocks = split_file_blocks(text)
    assert preamble == ""
    assert [p for p, _ in blocks] == ["a.py", "b.py"]
    # lossless: every non-empty line of the input survives in the blocks
    from collections import Counter
    joined = Counter(l for l in "\n".join(b for _, b in blocks).split("\n") if l)
    assert joined == Counter(l for l in text.split("\n") if l)


def test_split_keeps_a_format_patch_preamble():
    text = "From abc123 Mon Sep 17 00:00:00 2026\nSubject: x\n\n" + _diff(("a.py", 1))
    preamble, blocks = split_file_blocks(text)
    assert "Subject: x" in preamble
    assert len(blocks) == 1


def test_split_handles_empty_and_bare_hunk():
    assert split_file_blocks("") == ("", [])
    bare = "@@ -1 +1 @@\n-a\n+b\n"
    preamble, blocks = split_file_blocks(bare)
    # no `diff --git` header at all: kept as preamble so it is never dropped
    assert preamble.rstrip("\n") == bare.rstrip("\n")
    assert blocks == []


def test_plan_shards_is_lossless_and_never_splits_a_file_or_hunk():
    text = _diff(("a.py", 3), ("b.py", 2), ("c.py", 4), ("d.py", 1))
    shards = plan_shards(text, budget_chars=120)
    assert len(shards) > 1

    # every file appears exactly once
    seen = [f for s in shards for f in s.files]
    assert sorted(seen) == ["a.py", "b.py", "c.py", "d.py"]
    assert len(seen) == len(set(seen))

    # hunk headers and +/- lines are conserved across the split
    assert sum(s.diff.count("\n@@ ") for s in shards) == text.count("\n@@ ")
    assert (sum(s.diff.count("\n+") + s.diff.count("\n-") for s in shards)
            == text.count("\n+") + text.count("\n-"))

    # shard 1 starts with its diff header, not mid-file
    assert shards[0].diff.startswith("diff --git a/a.py")
    assert all(s.diff.lstrip().startswith("diff --git") for s in shards)


def test_plan_shards_is_deterministic():
    text = _diff(*[(f"f{i}.py", i % 3 + 1) for i in range(12)])
    a = [(s.index, s.files, s.chars) for s in plan_shards(text, 200)]
    b = [(s.index, s.files, s.chars) for s in plan_shards(text, 200)]
    assert a == b


def test_single_oversized_file_becomes_its_own_flagged_shard():
    big = _diff(("big.py", 400), ("small.py", 1))
    shards = plan_shards(big, budget_chars=500)
    flagged = [s for s in shards if s.oversized]
    assert len(flagged) == 1
    assert flagged[0].files == ["big.py"]
    # not split further, and the small file still lands somewhere
    assert any("small.py" in s.files for s in shards)


def test_should_shard_is_a_single_budget_comparison():
    text = _diff(("a.py", 1))
    assert should_shard(text, budget_chars=len(text) - 1) is True
    assert should_shard(text, budget_chars=len(text)) is False
    assert should_shard(text, budget_chars=0) is True


def test_shard_scope_header_states_the_boundary():
    """The scope header is what stops a shard inventing cross-file findings."""
    from swe_review.subagents.engineering_prompt import _shard_scope_section
    text = _shard_scope_section(DiffShard(index=2, total=5, files=["x.py", "y.py"]).to_dict())
    assert "SHARD 2 of 5" in text
    assert "`x.py`" in text and "`y.py`" in text
    assert "Do NOT raise findings about files you cannot see" in text


# ---------------------------------------------------------------------------
# integration: map-reduce review through ReviewerSubAgent
# ---------------------------------------------------------------------------

GOOD_SCORES = {k: {"score": max(1, m // 2), "max": m, "reason": "r"}
               for k, m in SCORE_DIMENSIONS.items()}


def _dim_sum(scores) -> float:
    return float(sum(e["score"] for e in scores.values()))


def _engineering_payload(findings, decision="request_changes", total=60, scores=None):
    """NOTE: the parser recomputes `total_score` as the SUM of the 8 dimension
    scores (the dimensions are treated as the evidence-based source of truth), so
    `total` here is only read when the dimensions are incomplete. Assert against
    `_dim_sum(...)`, not against `total`."""
    return json.dumps({
        "decision": {"recommendation": decision, "confidence": 0.8},
        "summary": {"overall_assessment": "assessment"},
        "scores": scores if scores is not None else GOOD_SCORES,
        "total_score": total,
        "findings": findings,
        "hard_gate": {"triggered": False, "reason": ""},
    })


def _finding(title, path):
    return {"severity": "P2", "title": title,
            "location": {"path": path, "start_line": 1, "end_line": 2},
            "observation": "obs", "why_it_matters": "why", "evidence": "ev",
            "recommendation": "rec", "confidence": 0.8,
            "constraint_category": "compatibility"}


class _Adapter:
    """Answers shard prompts from a per-shard script; synthesis and the
    single-call path are answered from their own payloads (keyed by prompt
    shape, so a test can tell exactly which path produced which call)."""

    def __init__(self, shard_findings=None, synthesis=None, bad_shards=(),
                 default=None, shard_payload=None):
        self.prompts = []
        self.calls = 0
        self.shard_findings = shard_findings or {}
        self.synthesis = synthesis
        self.bad_shards = set(bad_shards)
        self.default = default if default is not None else _engineering_payload([])
        # builder: findings -> raw response text for a shard call
        self.shard_payload = shard_payload or (
            lambda fs: _engineering_payload(fs))

    async def chat(self, system, user, max_tokens=4096, temperature=0.1):
        self.calls += 1
        self.prompts.append(user)
        if "## Shape of the whole change" in user:
            if self.synthesis is None:
                return "GARBAGE, not json", {"total_tokens": 5}
            return self.synthesis, {"total_tokens": 7}
        if "## SHARD " not in user:
            return self.default, {"total_tokens": 9}   # the un-sharded review path
        idx = len([p for p in self.prompts if "## SHARD " in p])
        if idx in self.bad_shards:
            return "GARBAGE, not json", {"total_tokens": 3}
        return self.shard_payload(self.shard_findings.get(idx, [])), {"total_tokens": 11}


#: `ReviewerSubAgent` floors the budget at 1000, so tests must stay above it or
#: the reviewer and `plan_shards` would disagree about the split.
BUDGET = 1_000


def _reviewer(adapter, **kw):
    kw.setdefault("max_regen_attempts", 1)
    kw.setdefault("shard_budget_chars", BUDGET)
    return ReviewerSubAgent(tool_adapter=adapter, prompt_style="engineering", **kw)


BIG = _diff(*[(f"{c}.py", 10) for c in "abcdefgh"])
SMALL = _diff(("a.py", 1))


def test_small_diff_does_not_shard():
    """Zero regression: under budget means the single-call path, untouched."""
    approve = _engineering_payload([], decision="approve", total=90)
    adapter = _Adapter(default=approve, synthesis=approve)
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": SMALL}))
    assert adapter.calls == 1
    assert "## SHARD " not in adapter.prompts[0]
    assert rep.decision == "approve"


def test_large_diff_shards_and_each_shard_gets_a_scope_header():
    approve = _engineering_payload([], decision="approve", total=90)
    adapter = _Adapter(default=approve, synthesis=approve)
    shards = plan_shards(BIG, BUDGET)
    assert len(shards) > 2                       # the test is meaningless otherwise
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    shard_prompts = [p for p in adapter.prompts if "## SHARD " in p]
    assert len(shard_prompts) == len(shards) == adapter.calls - 1  # +1 synthesis
    for i, p in enumerate(shard_prompts, start=1):
        assert f"SHARD {i} of {len(shards)}" in p
    assert f"{len(shards)} shards" in rep.raw_response


def test_shard_provenance_is_serialized_where_callers_can_see_it():
    """`raw_response` is NOT part of `to_dict()`, so provenance written only there
    was invisible in every serialized report — it must live in `summary`."""
    approve = _engineering_payload([], decision="approve", total=90)
    adapter = _Adapter(default=approve, synthesis=approve)
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    payload = rep.to_dict()
    assert "sharded_review" in payload["summary"]
    assert "shards" in payload["summary"]["sharded_review"]


def test_findings_are_the_deterministic_union_of_all_shards():
    """A finding only one shard could see must survive the merge."""
    adapter = _Adapter(
        shard_findings={1: [_finding("only in shard 1", "a.py")],
                        2: [_finding("only in shard 2", "b.py")]},
        synthesis=_engineering_payload([], decision="request_changes", total=55),
    )
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    titles = {f.title for f in rep.findings}
    assert "only in shard 1" in titles and "only in shard 2" in titles
    # findings come from the merge, not from the synthesis call
    assert rep.decision == "request_changes"


def test_duplicate_findings_across_shards_are_deduplicated():
    same = [_finding("same issue", "a.py")]
    adapter = _Adapter(shard_findings={1: same, 2: list(same)},
                       synthesis=_engineering_payload([]))
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert sum(1 for f in rep.findings if f.title == "same issue") == 1


def test_findings_are_severity_ordered():
    adapter = _Adapter(
        shard_findings={1: [_finding("low", "a.py")], 2: [_finding("high", "b.py")]},
        synthesis=_engineering_payload([]))
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    for f in rep.findings:
        f.severity = {"low": "P4", "high": "P1"}[f.title]  # noqa: B909 (test-only normalise)
    # re-run the merge directly for a deterministic ordering assertion
    from swe_review.subagents.reviewer_agent import merge_findings
    ordered = merge_findings([[f for f in rep.findings if f.severity == "P4"],
                              [f for f in rep.findings if f.severity == "P1"]])
    assert [f.severity for f in ordered] == ["P1", "P4"]


def test_broken_shard_degrades_but_does_not_sink_the_review():
    # `default` is garbage too: shard 1's bad answer must survive the one-shot
    # JSON repair round as still-bad, otherwise the repair rescues it and there is
    # nothing to degrade (which is the repair round working as designed).
    adapter = _Adapter(shard_findings={2: [_finding("survivor", "b.py")]},
                       synthesis=_engineering_payload([], decision="request_changes"),
                       bad_shards={1}, default="STILL GARBAGE")
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert [f.title for f in rep.findings] == ["survivor"]
    assert "degraded_shards" in rep.summary
    assert "shard 1" in rep.summary["degraded_shards"]


def test_all_shards_broken_never_fabricates_an_approve():
    n = len(plan_shards(BIG, BUDGET))
    adapter = _Adapter(bad_shards=set(range(1, n + 1)), default="STILL GARBAGE")
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert rep.decision == "request_changes"
    assert rep.confidence == 0.0
    assert rep.parse_error and rep.parse_error.startswith("sharded_review_all_shards_failed")


def test_synthesis_failure_falls_back_to_deterministic_worst_case():
    """A synthesis outage must not discard real shard evidence."""
    adapter = _Adapter(
        shard_findings={1: [_finding("kept", "a.py")]},
        synthesis=None,  # every synthesis attempt returns garbage
    )
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert [f.title for f in rep.findings] == ["kept"]
    assert rep.decision == "request_changes"
    assert rep.total_score is not None
    assert "deterministic aggregation" in rep.raw_response


def test_shard_raising_is_isolated(monkeypatch):
    """One shard exploding must not sink the review (same rule as best_of_n)."""
    adapter = _Adapter(synthesis=_engineering_payload([], decision="approve"))
    reviewer = _reviewer(adapter)
    real = reviewer._review_once
    calls = {"n": 0}

    async def flaky(system_prompt, user_prompt, max_tokens, prompt_style):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("shard exploded")
        return await real(system_prompt, user_prompt, max_tokens, prompt_style)

    monkeypatch.setattr(reviewer, "_review_once", flaky)
    rep = asyncio.run(reviewer.execute({"issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert "exploded" in rep.summary.get("degraded_shards", "")


def test_sharding_disabled_restores_single_call():
    adapter = _Adapter(synthesis=_engineering_payload([], decision="approve"))
    rep = asyncio.run(_reviewer(adapter, shard_large_diffs=False).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert adapter.calls == 1
    assert "## SHARD " not in adapter.prompts[0]


def test_legacy_prompt_styles_are_never_sharded():
    """The shard prompt is the engineering template; imposing it silently would
    change what a concise/detailed review means."""
    adapter = _Adapter(synthesis=_engineering_payload([], decision="approve"))
    asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG, "prompt_style": "concise"}))
    assert "## SHARD " not in "".join(adapter.prompts)


def test_concurrency_cap_is_respected():
    """Bound the fan-out: each shard spawns a CLI process."""
    active = {"now": 0, "peak": 0}

    class _ConcurrencyAdapter(_Adapter):
        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            try:
                await asyncio.sleep(0.01)
                return await super().chat(system, user, max_tokens, temperature)
            finally:
                active["now"] -= 1

    adapter = _ConcurrencyAdapter(synthesis=_engineering_payload([], decision="approve"))
    reviewer = _reviewer(adapter, shard_concurrency=2, shard_budget_chars=BUDGET)
    asyncio.run(reviewer.execute({"issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert len(plan_shards(BIG, BUDGET)) > 2   # enough shards to exceed the cap
    assert active["peak"] <= 2


def test_merge_findings_handles_flat_and_dict_locations():
    from swe_review.subagents.reviewer_agent import Finding, merge_findings
    a = Finding(severity="P2", title="x", location="pkg/a.py:10")
    b = Finding(severity="P2", title="x", location={"path": "pkg/a.py",
                                                    "start_line": 10, "end_line": 10})
    c = Finding(severity="P1", title="y", location="pkg/b.py:1")
    merged = merge_findings([[a], [b, c]])
    assert len(merged) == 2
    assert merged[0].severity == "P1"          # severity-ordered


def test_hollow_synthesis_is_rejected_in_favour_of_the_fallback():
    """A parseable-but-scoreless synthesis must NOT become the report.

    Found by running the feature against a real large diff: the synthesis step was
    given a custom system prompt with no JSON schema, so it returned a minimal
    payload. The sanity gate only enforces score completeness when the `scores`
    key is present, so the hollow answer was accepted and the shipped report had
    every dimension null and no summary at all — while still looking successful.
    """
    hollow = json.dumps({"decision": "request_changes", "confidence": 0.5})
    adapter = _Adapter(shard_findings={1: [_finding("kept", "a.py")]},
                       synthesis=hollow)
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert [f.title for f in rep.findings] == ["kept"]
    numeric = [e.get("score") for e in rep.scores.values()
               if isinstance(e.get("score"), (int, float))]
    assert numeric, "deterministic fallback must supply the scores"
    assert rep.summary.get("overall_assessment")
    assert "deterministic aggregation" in rep.raw_response


def test_synthesis_prompt_carries_the_schema():
    """Regression for the same bug: the synthesis call must be sent the engineering
    system prompt (which documents the JSON schema), not just the task framing."""
    adapter = _Adapter(synthesis=_engineering_payload([], decision="approve"))
    seen = {}

    class _SysCapture(_Adapter):
        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            if "## Shape of the whole change" in user:
                seen["system"] = system
            return await super().chat(system, user, max_tokens, temperature)

    a = _SysCapture(synthesis=_engineering_payload([], decision="approve"))
    asyncio.run(_reviewer(a).execute({"issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert "total_score" in seen["system"]          # schema section present
    assert "SYNTHESIS step" in seen["system"]       # task framing present


def test_shard_prompts_narrow_the_repo_context_to_their_own_files():
    """The context is re-sent per shard, so shipping the whole file_contents map in
    every one multiplies the prompt overhead by the shard count (~2x total tokens
    versus a single call, measured)."""
    from swe_review.subagents.reviewer_agent import _context_for_shard
    shards = plan_shards(BIG, BUDGET)
    all_files = [f for s in shards for f in s.files]
    repo_context = {"file_contents": {f: f"content of {f}" for f in all_files},
                    "keywords": ["k"], "files_modified": all_files}

    narrowed = _context_for_shard(repo_context, shards[0])
    assert set(narrowed["file_contents"]) == set(shards[0].files)
    assert narrowed["keywords"] == ["k"]           # global signals preserved
    assert len(repo_context["file_contents"]) == len(all_files)   # input untouched
    # nothing to narrow -> the context is passed through unchanged
    assert _context_for_shard({"keywords": []}, shards[0]) == {"keywords": []}


# ---------------------------------------------------------------------------
# per-shard OUTPUT budget — the actual bottleneck
#
# Measured on a real 251 KB diff: one call emitted 2.3k completion tokens;
# sharded emitted 13.6k because every shard produced a full 8-dimension report
# whose scores and summary are then discarded. That ate the entire parallelism
# win (193 s vs 53 s). Shards get a narrower contract now.
# ---------------------------------------------------------------------------

def _findings_only_payload(findings, decision="request_changes", confidence=0.7):
    return json.dumps({
        "decision": {"recommendation": decision, "confidence": confidence},
        "findings": findings,
        "hard_gate": {"triggered": False, "reason": ""},
    })


def test_shard_prompt_asks_for_findings_only():
    approve = _engineering_payload([], decision="approve", total=90)
    adapter = _Adapter(default=approve, synthesis=approve)
    asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    shard_prompt = next(p for p in adapter.prompts if "## SHARD " in p)

    assert "Output contract — this is a SHARD" in shard_prompt
    assert "Do NOT emit `scores`" in shard_prompt
    # the contract must come LAST: trailing instructions are the ones followed
    assert shard_prompt.rstrip().endswith("Output ONLY that JSON object.")
    # and a shard is not asked for the full 8-dimension review
    assert "按 8 个维度评估" not in shard_prompt


def test_findings_only_shard_payload_still_parses_and_merges():
    """Dropping scores/summary from the shard contract must not break parsing or
    the merge — findings and the shard decision are all that is used."""
    adapter = _Adapter(
        shard_findings={1: [_finding("only shard 1", "a.py")],
                        2: [_finding("only shard 2", "b.py")]},
        default=_engineering_payload([]),
        shard_payload=lambda fs: _findings_only_payload(fs),
        synthesis=_engineering_payload([], decision="approve", total=91),
    )
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert {f.title for f in rep.findings} == {"only shard 1", "only shard 2"}
    assert rep.parse_error is None
    # synthesis owns the global judgement
    assert rep.decision == "approve"
    assert rep.total_score == _dim_sum(GOOD_SCORES)


def test_synthesis_owns_the_scores_even_when_a_shard_reports_its_own():
    """Shards may still emit scores; the global synthesis must win."""
    shard_scores = {k: {"score": 1, "max": m, "reason": "r"}
                    for k, m in SCORE_DIMENSIONS.items()}
    synth_scores = {k: {"score": max(1, m // 2), "max": m, "reason": "r"}
                    for k, m in SCORE_DIMENSIONS.items()}
    adapter = _Adapter(
        shard_findings={1: [_finding("f", "a.py")]},
        shard_payload=lambda fs: _engineering_payload(
            fs, decision="request_changes", scores=shard_scores),
        default=_engineering_payload([]),
        synthesis=_engineering_payload([], decision="approve", scores=synth_scores),
    )
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert rep.decision == "approve"                      # synthesis decides
    assert rep.total_score == _dim_sum(synth_scores)      # ...and its dims win
    assert rep.total_score != _dim_sum(shard_scores)


def test_fallback_declares_missing_scores_when_shards_report_findings_only():
    """A scoreless fallback must SAY so — silently scoreless is the hollow-report
    failure mode this pipeline already guards against elsewhere."""
    adapter = _Adapter(
        shard_findings={1: [_finding("kept", "a.py")]},
        shard_payload=lambda fs: _findings_only_payload(fs),
        synthesis=None,                      # synthesis unavailable
    )
    rep = asyncio.run(_reviewer(adapter).execute({
        "issue": "i", "pr_title": "t", "pr_diff": BIG}))
    assert [f.title for f in rep.findings] == ["kept"]
    assert "scores are unavailable" in rep.summary["overall_assessment"]
    assert rep.decision == "request_changes"   # strictest shard decision preserved
