"""HostAdapter — the current agent answers prompts itself (memoized replay).

Covers: stable prompt keys, request persistence, response replay (json/txt),
empty-answer-is-still-pending, usage normalization, protocol methods
(pending/answer/answer_raw/status), and the end-to-end review flow.
"""
import asyncio
import json
from pathlib import Path

import pytest

from swe_review import HostAdapter, HostTurnRequired, prompt_key
from swe_review.tools.host_adapter import _estimate_usage


@pytest.fixture()
def adapter(tmp_path):
    return HostAdapter(work_dir=str(tmp_path / "host"), agent="test-agent")


# ---------------------------------------------------------------------------
# prompt_key
# ---------------------------------------------------------------------------

def test_prompt_key_is_stable_and_boundary_safe():
    assert prompt_key("a", "b") == prompt_key("a", "b")
    assert prompt_key("a", "b") != prompt_key("a", "c")
    # length-prefix: ("a","bc") and ("ab","c") must not collide
    assert prompt_key("a", "bc") != prompt_key("ab", "c")
    assert len(prompt_key("x", "y")) == 16


# ---------------------------------------------------------------------------
# chat: miss -> request + HostTurnRequired; hit -> replay, no exception
# ---------------------------------------------------------------------------

def test_chat_miss_writes_request_and_raises(adapter):
    with pytest.raises(HostTurnRequired) as ei:
        asyncio.run(adapter.chat(system="sys", user="usr"))
    req = Path(ei.value.request_path)
    assert req.is_file()
    obj = json.loads(req.read_text())
    assert obj["system"] == "sys" and obj["user"] == "usr"
    assert obj["agent"] == "test-agent"
    assert ei.value.pending_count == 1
    # adapter contract
    assert ei.value.to_dict()["status"] == "awaiting_host"
    assert "host turn required" in str(ei.value)


def test_chat_replay_from_json_response(adapter):
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    adapter.answer(prompt_key("s", "u"), "ANSWER",
                   usage={"prompt_tokens": 10, "completion_tokens": 2})
    text, tok = asyncio.run(adapter.chat("s", "u"))
    assert text == "ANSWER"
    assert tok == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def test_chat_replay_from_txt_response_estimates_usage(adapter):
    key = prompt_key("s", "u")
    adapter.responses_dir.mkdir(parents=True)
    (adapter.responses_dir / f"{key}.txt").write_text("plain answer")
    text, tok = asyncio.run(adapter.chat("s", "u"))
    assert text == "plain answer"
    assert tok == _estimate_usage("s\nu", "plain answer")


def test_empty_answer_is_still_pending(adapter):
    key = prompt_key("s", "u")
    adapter.responses_dir.mkdir(parents=True)
    (adapter.responses_dir / f"{key}.json").write_text('{"text": "   "}')
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))


def test_request_file_is_idempotent_across_replays(adapter):
    for _ in range(3):
        with pytest.raises(HostTurnRequired):
            asyncio.run(adapter.chat("s", "u"))
    assert len(list(adapter.requests_dir.glob("*.json"))) == 1


def test_malformed_response_file_raises_clearly(adapter):
    key = prompt_key("s", "u")
    adapter.responses_dir.mkdir(parents=True)
    (adapter.responses_dir / f"{key}.json").write_text('{"nope": 1}')
    with pytest.raises(ValueError, match="malformed response file"):
        asyncio.run(adapter.chat("s", "u"))


# ---------------------------------------------------------------------------
# protocol: pending / answer / answer_raw / status
# ---------------------------------------------------------------------------

def test_pending_filters_answered(adapter):
    """Order on disk is by hash key, so assert on membership, not sequence."""
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("first", "u1"))
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("second", "u2"))
    assert {r.system for r in adapter.pending()} == {"first", "second"}
    adapter.answer(prompt_key("first", "u1"), "ok")
    assert {r.system for r in adapter.pending()} == {"second"}


def test_answer_validates_key_and_empty_text(adapter):
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    key = prompt_key("s", "u")
    # Well-formed but unknown key (no request on disk) -> KeyError
    with pytest.raises(KeyError):
        adapter.answer("0" * 16, "x")
    with pytest.raises(ValueError, match="refusing to record an empty answer"):
        adapter.answer(key, "   ")


def test_answer_raw_accepts_envelope_or_bare(adapter):
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    key = prompt_key("s", "u")
    # bare: raw model output (e.g. the review JSON itself) is stored as text
    adapter.answer_raw(key, '{"decision": "approve"}')
    text, _ = asyncio.run(adapter.chat("s", "u"))
    assert text == '{"decision": "approve"}'
    # envelope with usage wins
    adapter2 = HostAdapter(work_dir=str(adapter.work_dir.parent / "h2"))
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter2.chat("s", "u"))
    adapter2.answer_raw(
        prompt_key("s", "u"),
        json.dumps({"text": "wrapped", "usage": {"total_tokens": 77}}))
    text, tok = asyncio.run(adapter2.chat("s", "u"))
    assert (text, tok["total_tokens"]) == ("wrapped", 77)


def test_usage_normalization_fills_total(adapter):
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    adapter.answer(prompt_key("s", "u"), "a",
                   usage={"prompt_tokens": 5, "completion_tokens": 7})
    _, tok = asyncio.run(adapter.chat("s", "u"))
    assert tok["total_tokens"] == 12


def test_status_counts(adapter):
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    st = adapter.status()
    assert (st["requests"], st["answered"], st["pending"]) == (1, 0, 1)
    assert st["pending_keys"] == [prompt_key("s", "u")]
    adapter.answer(prompt_key("s", "u"), "done")
    st = adapter.status()
    assert (st["requests"], st["answered"], st["pending"]) == (1, 1, 0)


def test_get_status_matches_status(adapter):
    assert adapter.get_status() == adapter.status()


# ---------------------------------------------------------------------------
# end-to-end: ReviewSkill with a host adapter (real prompt path)
# ---------------------------------------------------------------------------

def test_review_skill_completes_after_host_answers(tmp_path, sample_diff):
    """Full loop: miss -> answer -> re-run returns the parsed review."""
    from swe_review import ReviewSkill
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n")
    a = HostAdapter(work_dir=str(tmp_path / "host"))
    skill = ReviewSkill(tool_adapter=a, prompt_style="concise")

    with pytest.raises(HostTurnRequired):
        asyncio.run(skill.execute(issue="Null check", pr_title="t",
                                  pr_diff=sample_diff, repo_path=str(tmp_path)))
    pend = a.pending()
    assert len(pend) == 1
    review = json.dumps({
        "decision": "approve", "confidence": 0.9,
        "summary": {"problem": "p", "solution": "s",
                    "overall_assessment": "ok"},
        "defects": [],
    })
    a.answer(pend[0].key, review)
    res = asyncio.run(skill.execute(issue="Null check", pr_title="t",
                                    pr_diff=sample_diff, repo_path=str(tmp_path)))
    assert res.ok is True
    assert res.payload["decision"] == "approve"


def test_review_retry_wrapper_does_not_burn_retries_on_host_turn(tmp_path, sample_diff):
    """ReviewerSubAgent._call_ai_tool_with_retries re-raises HostTurnRequired
    immediately (deterministic), instead of retrying 3x."""
    from swe_review import ReviewSkill  # noqa: F401  (package import sanity)
    from swe_review.subagents.reviewer_agent import ReviewerSubAgent
    a = HostAdapter(work_dir=str(tmp_path / "host"))
    agent = ReviewerSubAgent(tool_adapter=a)
    calls = {"n": 0}
    real = agent._call_ai_tool

    async def counting(*ar, **kw):
        calls["n"] += 1
        return await real(*ar, **kw)

    agent._call_ai_tool = counting
    with pytest.raises(HostTurnRequired):
        asyncio.run(agent.execute({
            "issue": "i", "pr_title": "t", "pr_diff": sample_diff,
            "max_exploration_steps": 0,
        }))
    # Exactly one attempt — this is the assertion that gives the test its teeth:
    # `pytest.raises` alone would still pass if the wrapper burned the full
    # max_regen_attempts budget before surfacing the (deterministic) exception.
    assert calls["n"] == 1  # no retry storm


# ---------------------------------------------------------------------------
# Self-review findings (request_changes, score 86) — regression tests
# ---------------------------------------------------------------------------

def test_answer_rejects_traversal_and_malformed_keys(adapter):
    """P2: --key is a path component; only prompt_key()-shaped keys are allowed."""
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    for bad in ("../../etc/passwd", "/tmp/x", "../" + "a" * 14,
                "not-hex-key!!", "G" * 16, "a" * 15, "a" * 17):
        with pytest.raises(ValueError, match="invalid prompt key"):
            adapter.answer(bad, "x")


def test_unknown_key_error_names_the_rendezvous_dir(adapter):
    """A `--host-dir` mismatch is the usual cause of an unknown key; the error
    must say which dir this adapter actually reads, not just "no request at"."""
    with pytest.raises(KeyError) as exc:
        adapter.answer("a" * 16, "x")  # valid key shape, no matching request
    msg = str(exc.value)
    assert str(adapter.work_dir) in msg
    assert "--host-dir" in msg


def test_answer_raw_does_not_mistake_bare_text_member_for_envelope(adapter):
    """P3: a bare answer whose top-level `text` member exists but has no
    envelope marker must be stored verbatim, not silently truncated."""
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    key = prompt_key("s", "u")
    bare = json.dumps({"text": "payload", "extra": "keep me", "note": "x"})
    adapter.answer_raw(key, bare)
    text, _ = asyncio.run(adapter.chat("s", "u"))
    assert text == bare  # whole object preserved


def test_pending_survives_hand_edited_numeric_fields(adapter):
    """P4: one hand-edited request file must not crash pending() for all keys."""
    with pytest.raises(HostTurnRequired):
        asyncio.run(adapter.chat("s", "u"))
    req = next(adapter.requests_dir.glob("*.json"))
    obj = json.loads(req.read_text())
    obj["max_tokens"] = "not-a-number"
    obj["temperature"] = {"weird": True}
    req.write_text(json.dumps(obj))
    pend = adapter.pending()
    assert len(pend) == 1
    # What matters is resilience, not the literal default values: a corrupted
    # field must fall back to a usable value of the right type so one hand-edited
    # request file cannot break `pending()` for every other prompt.
    assert isinstance(pend[0].max_tokens, int)
    assert isinstance(pend[0].temperature, float)


