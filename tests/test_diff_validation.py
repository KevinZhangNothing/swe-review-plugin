"""Structural diff validation, cross-checked against `git apply`.

Context: self-running this project's own loop twice produced a reviser patch that
review APPROVED (79/100) and verify then rejected with `corrupt patch at line 39`.
8 of its 9 hunks declared line counts that did not match their bodies. Review cannot
catch that (it never applies anything) and verify only sees it at the very end of
the loop, where no attempt is left to fix it.

These tests pin the check that catches it at the point of production.
"""
import asyncio
import json
import subprocess

import pytest

from swe_review.subagents.diff_validation import (
    is_applicable_shape,
    validate_unified_diff,
)

# A hunk whose header over-declares: exactly the shape of the real failure
# (`@@ -116,10 +132,10 @@` with a body of -7 +8).
INCONSISTENT = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
    "@@ -10,10 +10,10 @@ def f():\n"
    " keep\n"
    "-old\n"
    "+new\n"
    " tail\n"
)

TRUNCATED = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
    "@@ -1,5 +1,5 @@\n"
    "-a\n"
    "+b\n"
)

# `@@ -1,3 +1,3 @@`: two context lines + one removal = 3 old; two context + one
# addition = 3 new. (An earlier draft of this fixture declared 3/3 with only 3 body
# lines, i.e. actually 2/2 — the validator correctly called it corrupt.)
VALID = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
    "@@ -1,3 +1,3 @@\n"
    " keep1\n"
    "-old\n"
    "+new\n"
    " keep2\n"
)

VALID_NEW_FILE = (
    "diff --git a/n.py b/n.py\n"
    "new file mode 100644\n"
    "index 0000000..1234567\n"
    "--- /dev/null\n"
    "+++ b/n.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+one\n"
    "+two\n"
)

# Arithmetic-valid but NOT applicable: a created file without the `new file mode`
# header. Pins the documented scope — this is not a full applicability check.
ARITHMETIC_OK_NOT_APPLICABLE = (
    "diff --git a/n.py b/n.py\n"
    "--- /dev/null\n"
    "+++ b/n.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+one\n"
    "+two\n"
)

# A removed line whose content starts with `--` renders as `--- ...`, which is
# indistinguishable from a file header without looking at the following line.
VALID_DASH_CONTENT = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
    "@@ -1,2 +1,1 @@\n"
    "--- comment\n"
    "-value\n"
    "+value\n"
)


def _git_verdict(tmp_path, text, target="f.py"):
    """Verdict from `git apply --check`, with the work tree matching the patch so
    the only thing left for git to judge is the patch's STRUCTURE."""
    for name in ("f.py", "n.py"):
        p = tmp_path / name
        p.write_text("keep1\nold\nkeep2\n", encoding="utf-8")
    (tmp_path / "n.py").unlink()          # patches that CREATE it need it gone
    p = tmp_path / "candidate.diff"
    p.write_text(text, encoding="utf-8")
    r = subprocess.run(["git", "apply", "--check", str(p)], cwd=str(tmp_path),
                       capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.parametrize("text,expected", [
    ("", False),
    ("diff --git a/x b/x\n--- a/x\n+++ b/x\n", False),        # no hunk
    (INCONSISTENT, False),
    (TRUNCATED, False),
    (VALID, True),
    (VALID.rstrip("\n"), True),                               # no trailing newline
    (VALID_NEW_FILE, True),
    (VALID_DASH_CONTENT, True),
    ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n"
     "\\ No newline at end of file\n+b\n", True),
])
def test_shape_verdicts(text, expected):
    assert is_applicable_shape(text) is expected


@pytest.mark.parametrize("text", [VALID, VALID_NEW_FILE, INCONSISTENT, TRUNCATED])
def test_verdict_agrees_with_git_apply(tmp_path, text):
    """The validator must not guess: its verdict has to match git's."""
    assert is_applicable_shape(text) == _git_verdict(tmp_path, text)


def test_scope_is_necessary_but_not_sufficient(tmp_path):
    """Documented limit: hunk arithmetic is checked, applicability is not.

    A created file with no `new file mode` header is arithmetic-valid but git
    refuses it. The validator deliberately accepts it — catching that class needs
    the work tree, which is the verifier's job.
    """
    assert is_applicable_shape(ARITHMETIC_OK_NOT_APPLICABLE) is True
    assert _git_verdict(tmp_path, ARITHMETIC_OK_NOT_APPLICABLE) is False


def test_reason_names_the_offending_hunk():
    reason = validate_unified_diff(INCONSISTENT)
    assert "line 4" in reason
    assert "-10 +10" in reason and "-3 +3" in reason     # declared vs actual


# ---------------------------------------------------------------------------
# integration: the reviser and the generator must not emit a corrupt diff
# ---------------------------------------------------------------------------

MALFORMED = {"title": "t", "body": "b", "diff": INCONSISTENT,
             "changes_summary": "s", "addressed_defect_indices": [0]}


@pytest.fixture
def reviser_toolchain():
    class Adapter:
        def __init__(self, payload):
            self.payload = json.dumps(payload)
            self.calls = 0

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            self.calls += 1
            return self.payload, {"total_tokens": 1}

    return Adapter


def _revise(adapter_factory):
    from swe_review.subagents.reviser_agent import ReviserSubAgent
    adapter = adapter_factory(MALFORMED)
    rev = ReviserSubAgent(tool_adapter=adapter, prompt_style="concise",
                          max_regen_attempts=1, regen_backoff_seconds=0)
    res = asyncio.run(rev.execute({
        "issue": "i", "original_pr_title": "t", "original_pr_body": "",
        "original_pr_diff": VALID,
        "review_report": {"decision": "request_changes",
                          "defects": [{"severity": "high", "description": "d"}]},
    }))
    return res, adapter


def test_reviser_rejects_a_malformed_diff(reviser_toolchain):
    res, _adapter = _revise(reviser_toolchain)
    assert res.status == "failed"
    assert res.diff == ""                     # must not propagate
    assert "malformed diff" in res.changes_summary
    assert "line 4" in res.changes_summary


def test_reviser_retries_when_the_diff_is_malformed(reviser_toolchain):
    """With a budget > 1 the malformed answer is retried rather than accepted."""
    from swe_review.subagents.reviser_agent import ReviserSubAgent
    adapter = reviser_toolchain(MALFORMED)
    rev = ReviserSubAgent(tool_adapter=adapter, prompt_style="concise",
                          max_regen_attempts=3, regen_backoff_seconds=0)
    asyncio.run(rev.execute({
        "issue": "i", "original_pr_title": "t", "original_pr_body": "",
        "original_pr_diff": VALID,
        "review_report": {"decision": "request_changes", "defects": []},
    }))
    assert adapter.calls == 3                 # exhausted the budget, never accepted


def test_reviser_accepts_a_valid_diff(reviser_toolchain):
    from swe_review.subagents.reviser_agent import ReviserSubAgent
    good = dict(MALFORMED, diff=VALID)
    adapter = reviser_toolchain(good)
    rev = ReviserSubAgent(tool_adapter=adapter, prompt_style="concise",
                          regen_backoff_seconds=0)
    res = asyncio.run(rev.execute({
        "issue": "i", "original_pr_title": "t", "original_pr_body": "",
        "original_pr_diff": VALID,
        "review_report": {"decision": "request_changes",
                          "defects": [{"severity": "high", "description": "d"}]},
    }))
    assert res.status == "success" and res.diff == VALID


def test_generator_drops_a_malformed_diff(reviser_toolchain):
    from swe_review.subagents.generator_agent import GeneratorSubAgent
    adapter = reviser_toolchain(dict(MALFORMED, rationale="r", confidence=0.9))
    gen = GeneratorSubAgent(tool_adapter=adapter)
    res = asyncio.run(gen.execute({"issue": "i", "hint": ""}))
    d = res.to_dict()
    assert d["diff"] == ""
    assert d["confidence"] == 0.0
    assert "malformed diff rejected" in d["rationale"]
