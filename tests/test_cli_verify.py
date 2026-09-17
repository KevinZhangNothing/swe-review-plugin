"""CLI verify must forward --test-runner to the VerifySkill layer.

These tests deliberately build their namespace through ``build_parser()``
rather than hand-constructing a ``SimpleNamespace``. ``--test-runner`` is
declared with ``type=_test_runner_template``, so **argparse** is what turns the
raw flag value into a template list; a hand-built namespace passing the raw
string hid a real crash (``_cmd_verify`` split the already-split list, raising
``AttributeError: 'list' object has no attribute 'read'`` on every invocation).
"""
import asyncio

import pytest

from swe_review import VerifySkill
from swe_review.cli import _cmd_verify, build_parser


@pytest.fixture()
def verify_capture(monkeypatch):
    captured = {}

    async def fake_execute(self, **kwargs):
        captured.update(kwargs)
        from swe_review.subagents.verifier_agent import VerificationResult
        from swe_review.skill import SkillResult
        return SkillResult(ok=True, payload=VerificationResult(
            passed=True, test_results=[], resolution_status="unknown",
            confidence=0.5, details="fake", patch_applied=True,
            sandbox_used=True).to_dict(), raw=None)

    monkeypatch.setattr(VerifySkill, "execute", fake_execute)
    return captured


def _verify_args(repo, argv):
    """Parse `verify` argv exactly the way `main()` does."""
    return build_parser().parse_args(
        ["verify", "--repo-path", str(repo), *argv]
    )


def _diff_file(repo, minimal_diff):
    path = repo / "p.diff"
    path.write_text(minimal_diff)
    return str(path)


def test_test_runner_is_forwarded_as_a_template_list(repo_with_hello, minimal_diff,
                                                     verify_capture):
    args = _verify_args(repo_with_hello, [
        "--pr-diff", _diff_file(repo_with_hello, minimal_diff),
        "--test-runner", "python -m pytest {test} -q",
    ])
    asyncio.run(_cmd_verify(args))
    assert verify_capture["test_runner"] == ["python", "-m", "pytest", "{test}", "-q"]


def test_no_test_runner_flag_passes_none(repo_with_hello, minimal_diff, verify_capture):
    args = _verify_args(repo_with_hello, [
        "--pr-diff", _diff_file(repo_with_hello, minimal_diff),
    ])
    asyncio.run(_cmd_verify(args))
    assert verify_capture["test_runner"] is None


def test_quoted_token_with_spaces_survives_to_the_subagent(repo_with_hello, minimal_diff,
                                                           verify_capture):
    """A quoted token containing a space must stay ONE element end to end.

    Regression for the double-split bug: a second `shlex.split` in `_cmd_verify`
    would fragment `"my tests/{test}"` into two argv words and silently run the
    wrong command.
    """
    args = _verify_args(repo_with_hello, [
        "--pr-diff", _diff_file(repo_with_hello, minimal_diff),
        "--test-runner", 'python -m pytest "my tests/{test}" -q',
    ])
    asyncio.run(_cmd_verify(args))
    assert verify_capture["test_runner"] == [
        "python", "-m", "pytest", "my tests/{test}", "-q",
    ]


def test_runner_without_test_placeholder_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["verify", "--pr-diff", "-", "--test-runner", "python -m pytest"])


def test_test_runner_placeholder_reaches_subagent(repo_with_hello, minimal_diff):
    """End to end: {test} in the CLI template is substituted per test name."""
    res = asyncio.run(VerifySkill(repo_path=str(repo_with_hello)).execute(
        pr_diff=minimal_diff,
        test_info={"pass_to_pass": ["one"]},
        test_runner=["python", "-c", "import sys; sys.exit(0)"],
    ))
    entry = res.payload["test_results"][0]
    assert res.payload["passed"] is True
    assert entry["passed"] is True
    assert entry["test"] == "one"
