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
