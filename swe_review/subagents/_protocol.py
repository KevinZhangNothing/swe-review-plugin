"""Which exceptions mean "this batch never really ran" — one definition.

Concurrent waves (`asyncio.gather(return_exceptions=True)`) must isolate ONE bad
candidate without sinking the batch. That isolation is wrong for a class of
errors the caller cannot retry away: they did not fail *as a candidate*, they
failed *before any candidate existed*, and folding them into a per-item failure
produces a confident-looking result built on nothing.

Two such errors, and until now each layer decided separately:

* ``HostTurnRequired`` — ``--tool host`` left a prompt on disk with no answer.
  `loop_agent` learned this the hard way (it printed "no candidate
  approved+verified" and exited 0 while prompts sat unanswered) and re-raised;
  `reviewer_agent`'s sharded path still swallowed it and reported
  ``review_unparseable``.
* ``FileNotFoundError`` — the CLI binary is missing. `reviewer_agent`'s retry
  wrapper already re-raised it; `loop_agent`'s wave isolation did not, so a
  missing ``claude``/``pi`` could read as "every candidate failed review".

Keeping the rule in one dependency-free module is the point: the previous
duplication is exactly why the two layers disagreed. `host_adapter` imports
`base`, and `loop_agent` imports `reviewer_agent`, so neither of those modules
can host this without creating a cycle.
"""

from __future__ import annotations

from ..tools.host_adapter import HostTurnRequired


def is_protocol_signal(exc: BaseException) -> bool:
    """True when `exc` must escape per-item error isolation.

    Retrying cannot help: a host turn needs a written answer, and a missing
    binary needs an install. Both must reach the caller so it can say what to do.
    """
    return isinstance(exc, (HostTurnRequired, FileNotFoundError))


def raise_protocol_signals(results) -> None:
    """Re-raise the first protocol signal found in a ``gather`` result list."""
    for item in results:
        if isinstance(item, BaseException) and is_protocol_signal(item):
            raise item