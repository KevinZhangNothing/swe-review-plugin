"""`tools.base.accum_usage` — the single token-usage accumulator.

Three near-identical copies of this arithmetic once existed and had already
drifted (one raised TypeError on a non-numeric value, the others stored it). The
unification is only worth anything if the contract is pinned, so every clause in
its docstring gets an assertion here.
"""

import pytest

from swe_review.tools.base import accum_usage


def test_accumulates_integers():
    total = {"prompt_tokens": 10}
    accum_usage(total, {"prompt_tokens": 5, "completion_tokens": 7})
    assert total == {"prompt_tokens": 15, "completion_tokens": 7}


def test_accumulates_in_place_across_calls():
    total: dict = {}
    for usage in ({"total_tokens": 3}, {"total_tokens": 4}, {"total_tokens": 5}):
        accum_usage(total, usage)
    assert total == {"total_tokens": 12}


def test_missing_and_empty_inputs_are_noops():
    total = {"total_tokens": 1}
    for empty in (None, {}):
        accum_usage(total, empty)
    assert total == {"total_tokens": 1}


@pytest.mark.parametrize("value", [True, False])
def test_bool_is_rejected_despite_being_an_int_subclass(value):
    """`isinstance(True, int)` is True — without the explicit guard a stray
    `{"cached": true}` would inflate a token total by 1."""
    total: dict = {}
    accum_usage(total, {"total_tokens": value})
    assert total == {}


@pytest.mark.parametrize("value", ["42", None, [1], {"n": 1}, object()])
def test_non_numeric_values_are_ignored(value):
    """Usage arrives as untrusted JSON from a CLI, not a typed contract."""
    total: dict = {}
    accum_usage(total, {"total_tokens": value})
    assert total == {}


@pytest.mark.parametrize("value,expected", [
    (0.7, 1),    # rounds, does not truncate toward zero
    (0.4, 0),    # rounds down at the boundary
    (1.5, 2),    # banker's rounding would give 2 here; int(round(1.5)) == 2
    (2.5, 2),    # Python's round() is half-to-even: 2.5 -> 2
    (10.0, 10),
])
def test_floats_are_rounded_not_truncated(value, expected):
    """Fractional counts must not be truncated toward zero.

    `round()` is Python's builtin — half-to-EVEN, not half-up — so 2.5 -> 2.
    That behaviour is inherited rather than chosen (a token total is approximate
    either way); the property under test is only that 0.7 does not become 0.
    """
    total: dict = {}
    accum_usage(total, {"total_tokens": value})
    assert total["total_tokens"] == expected
    assert isinstance(total["total_tokens"], int)
    assert not isinstance(total["total_tokens"], bool)


def test_float_and_int_accumulate_together():
    total = {"total_tokens": 1}
    accum_usage(total, {"total_tokens": 0.7, "completion_tokens": 2.3})
    accum_usage(total, {"total_tokens": 10})
    assert total == {"total_tokens": 12, "completion_tokens": 2}
    # Still integers: token counts are integral everywhere downstream.
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for v in total.values())


def test_mixed_payload_keeps_the_valid_keys():
    """One bad key must not discard the good ones from the same dict."""
    total: dict = {}
    accum_usage(total, {
        "prompt_tokens": 11,
        "cached": True,          # rejected
        "note": "n/a",           # rejected
        "completion_tokens": 3,
    })
    assert total == {"prompt_tokens": 11, "completion_tokens": 3}


def test_rounding_is_approximate_not_equivalent_to_the_estimator():
    """Pins the ±1 difference the docstring now admits.

    The fallback estimator uses `len(text) // 4` (floor). `round()` disagrees
    wherever the fraction is >= 0.5. This test exists so the docstring's
    "NOT bit-for-bit equivalent" claim cannot silently become a lie again.
    """
    from swe_review.tools.base import accum_usage

    disagreements = []
    for value in (0.6, 1.5, 1.6, 2.7):
        total: dict = {}
        accum_usage(total, {"total_tokens": value})
        accumulated = total["total_tokens"]
        if accumulated != int(value):
            disagreements.append((value, accumulated, int(value)))

    assert disagreements, (
        "if round() and floor division ever agree everywhere, the docstring "
        "caveat should be revisited")
    # Every disagreement is exactly one token — an accepted approximation.
    for value, round_value, floor_value in disagreements:
        assert abs(round_value - floor_value) == 1


# ---------------------------------------------------------------------------
# Adapter contract — every registered backend subclasses BaseAdapter
# ---------------------------------------------------------------------------
# Found by a whole-package self-review: all six adapters were independent classes
# that merely happened to define `chat`/`get_status`. Nothing in the type system
# said so, so a renamed method on one adapter would only surface at call time —
# and `diagnose` was even less uniform (five adapters had it, ShellTools did not).
#
# BaseAdapter deliberately stays instantiable (Skill falls back to `BaseAdapter()`
# as a no-tool stand-in), so this is a structural test rather than an ABC.

def test_every_adapter_subclasses_base():
    from swe_review.tools.base import BaseAdapter
    from swe_review.tools.claude_code_adapter import ClaudeCodeAdapter
    from swe_review.tools.cursor_adapter import CursorAdapter
    from swe_review.tools.host_adapter import HostAdapter
    from swe_review.tools.opencode_adapter import OpenCodeAdapter
    from swe_review.tools.pi_adapter import PiAdapter
    from swe_review.tools.shell_tools import ShellTools

    adapters = (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter,
                HostAdapter, ShellTools)
    for cls in adapters:
        assert issubclass(cls, BaseAdapter), f"{cls.__name__} must subclass BaseAdapter"


def test_every_adapter_implements_chat_and_get_status():
    """The two members of the contract."""
    from swe_review.tools.base import BaseAdapter
    from swe_review.tools.claude_code_adapter import ClaudeCodeAdapter
    from swe_review.tools.cursor_adapter import CursorAdapter
    from swe_review.tools.host_adapter import HostAdapter
    from swe_review.tools.opencode_adapter import OpenCodeAdapter
    from swe_review.tools.pi_adapter import PiAdapter
    from swe_review.tools.shell_tools import ShellTools

    base_chat = BaseAdapter.__dict__["chat"]
    base_status = BaseAdapter.__dict__["get_status"]
    for cls in (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter,
                HostAdapter, ShellTools):
        assert cls.__dict__.get("chat") is not base_chat, \
            f"{cls.__name__} must override chat()"
        assert cls.__dict__.get("get_status") is not base_status, \
            f"{cls.__name__} must override get_status()"


def test_every_adapter_implements_diagnose():
    """`diagnose` is optional in the ABC but now uniform across shipped adapters."""
    from swe_review.tools.claude_code_adapter import ClaudeCodeAdapter
    from swe_review.tools.cursor_adapter import CursorAdapter
    from swe_review.tools.host_adapter import HostAdapter
    from swe_review.tools.opencode_adapter import OpenCodeAdapter
    from swe_review.tools.pi_adapter import PiAdapter
    from swe_review.tools.shell_tools import ShellTools

    for cls in (ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter,
                HostAdapter, ShellTools):
        assert callable(getattr(cls, "diagnose", None)), \
            f"{cls.__name__} must provide diagnose()"


def test_base_adapter_stays_instantiable():
    """Skill falls back to `BaseAdapter()`; an ABC here would break that path."""
    from swe_review.tools.base import BaseAdapter

    adapter = BaseAdapter()
    assert adapter.name == "base"
    assert adapter.get_status()["configured"] is False


def test_pty_runner_debug_switch_is_off_by_default(capsys):
    """Swallowed OSErrors stay quiet unless SWE_REVIEW_PTY_DEBUG is set.

    These failures are non-fatal by design (teardown paths legitimately fail), so
    normal runs must not get noisier — but they were previously impossible to
    attribute when the child's output came back truncated.
    """
    from swe_review.tools import _pty_runner

    assert _pty_runner._PTY_DEBUG is False
    _pty_runner._note_ignored(OSError("boom"), "unit test")
    assert capsys.readouterr().err == ""
