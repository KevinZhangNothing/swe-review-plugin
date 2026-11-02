"""
BaseAdapter - 所有 tool adapter 的抽象基类
"""

from typing import Dict, Any, Tuple, Optional

#: get_status() model placeholder — the swe loop never pins a model; model
#: choice is inherited from the host CLI/environment (swe-review-loop hard
#: constraint #4).
MODEL_INHERITED_NOTE = "(inherited from CLI — never pinned by swe-review)"


def accum_usage(total: Dict[str, int], add: Optional[Dict[str, int]]) -> None:
    """Add one adapter's usage dict into ``total`` in place.

    Single implementation on purpose: the reviewer, the reviser and the loop all
    accumulate token usage, and three copy-pasted versions had already drifted
    (one would raise TypeError on a non-numeric value, the others stored it).

    Numeric contract — token counts are integers everywhere downstream:

    * non-numeric values (including strings and ``None``) are ignored, because a
      usage dict arriving from a CLI is untrusted JSON, not a typed contract;
    * ``bool`` is rejected even though Python makes it an ``int`` subclass, so a
      stray ``"cached": true`` can never inflate a token total;
    * floats are accepted and converted with Python's ``round()`` (round-half-to-
      EVEN, not half-up), then to ``int``. The point is only that a fractional
      count is never truncated toward zero: the fallback estimator emits
      ``len(text) // 4``, and dropping a fraction would lose the sub-token part
      entirely. Half-to-even is inherited from ``round()``, not chosen.

      This is NOT bit-for-bit equivalence with the estimator, and the comment
      must not claim otherwise: ``round()`` and floor division disagree wherever
      the fraction is >= 0.5 (measured: 1.5 -> 2 vs 1, 0.6 -> 1 vs 0). Token
      totals are approximate either way, so a ±1 difference on an ESTIMATED
      count is accepted rather than "corrected". Real adapter usage is already
      integral and passes through unchanged. The result is an ``int`` because
      token counts are integral everywhere downstream.

    ``total`` is mutated so callers can accumulate across attempts without
    rebuilding the dict each time.
    """
    if not add:
        return
    for key, value in add.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total[key] = total.get(key, 0) + int(round(value))


class BaseAdapter:
    name: str = "base"

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Tuple[str, Dict[str, int]]:
        raise NotImplementedError

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "configured": False}
