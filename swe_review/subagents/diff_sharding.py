"""Deterministic sharding of a large unified diff — no LLM involved.

Why this exists: a review prompt is dominated by the raw diff (measured at 98% of
a 182 KB prompt for a 180 KB change). Sending a large change as ONE call has two
costs the project was paying:

  1. the prompt grows without bound — nothing capped `pr_diff` while
     `repo_context`, the generator's exploration and the reviser's diff all had
     explicit budgets;
  2. the whole review serialises into a single request, so wall time grows with
     change size even though the work is embarrassingly parallel.

Splitting the diff into file-aligned shards lets the shards be reviewed
concurrently with a bounded per-request prompt.

Rules — all deterministic, because orchestration must not be left to a model:

  - **a file's hunks are never split**: a file's change is one semantic unit, and
    a reviewer that sees half of it invents "missing" findings;
  - **a hunk is never split**: a partial hunk is not a reviewable unit;
  - files are packed **in diff order** (stable and predictable), up to
    `budget_chars`;
  - a single file bigger than the budget becomes **its own oversized shard**
    rather than being split (flagged so callers can surface it).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

#: Per-shard character budget — and therefore the threshold above which a diff is
#: split at all (see `should_shard`).
#:
#: Set as a CONTEXT-SAFETY threshold, not a speed knob. Cache-proof measurements
#: (every prompt carrying a unique nonce — see the note below) on a real 271 KB /
#: 42-file change:
#:
#:     single call   35.5 s,  2,341 completion tokens
#:     sharded @60k  71.5 s,  8,424 completion tokens   (5 shards + synthesis)
#:
#: Sharding is ~2x SLOWER in wall time here, and the reason is structural: fan-out
#: does not divide the work, it ADDS it. Each shard writes its own findings report
#: and a further serial synthesis step writes a full report on top. Output
#: generation (not prompt size) is what costs wall time, so 3.6x the output tokens
#: cannot be hidden by parallelism.
#:
#: What parallelism DOES buy is a bounded per-request prompt, which matters when a
#: single call would otherwise overflow the context window — there, the alternative
#: is failure or a much slower request. Hence: 400k chars (~100k tokens of diff,
#: ~120k with system/context, i.e. near a 128k window) is the point at which
#: sharding starts to be the safer choice rather than a pure cost.
#:
#: Lower it deliberately when you want bounded prompts more than wall time
#: (`--shard-budget 60000`), or disable it entirely with `--no-shard`.
#:
#: BENCHMARKING NOTE: the `pi` CLI caches responses for identical prompts (measured
#: 2.86 s first call vs 0.32 s on an identical repeat, 9x). Any A/B measurement that
#: reuses a prompt is therefore meaningless — always put a nonce in the prompt.
DEFAULT_SHARD_BUDGET_CHARS = 400_000

#: Concurrent shard requests. Bounded on purpose: each one spawns a CLI process.
DEFAULT_SHARD_CONCURRENCY = 4

_DIFF_HEADER = "diff --git "


@dataclass
class DiffShard:
    """One file-aligned slice of a diff."""

    index: int                      # 1-based shard number
    total: int
    files: List[str] = field(default_factory=list)
    diff: str = ""
    chars: int = 0
    oversized: bool = False         # one file alone exceeded the budget

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "total": self.total,
            "files": list(self.files),
            "chars": self.chars,
            "oversized": self.oversized,
        }


def split_file_blocks(pr_diff: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Split a unified diff into per-file blocks.

    Returns `(preamble, [(path, block), ...])`. The preamble is everything before
    the first `diff --git` header (usually empty, but `git format-patch` output
    has one) and is preserved rather than dropped.
    """
    if not pr_diff:
        return "", []

    blocks: List[Tuple[str, str]] = []
    preamble_lines: List[str] = []
    current_path: str = ""
    current_lines: List[str] = []

    def flush() -> None:
        if current_path or current_lines:
            blocks.append((current_path, "\n".join(current_lines)))

    for line in pr_diff.split("\n"):
        if line.startswith(_DIFF_HEADER):
            if current_path or current_lines:
                flush()
            current_path = _path_from_header(line)
            current_lines = [line]
        elif current_path:
            current_lines.append(line)
        else:
            preamble_lines.append(line)

    if current_path or current_lines:
        flush()

    preamble = "\n".join(preamble_lines)
    return preamble, blocks


def _path_from_header(line: str) -> str:
    """`diff --git a/x/y.py b/x/y.py` -> `x/y.py` (falls back to the raw argv)."""
    parts = line.split()
    if len(parts) >= 4:
        return parts[2][2:] if parts[2].startswith(("a/", "b/")) else parts[2]
    return parts[-1] if parts else ""


def plan_shards(pr_diff: str, budget_chars: int = DEFAULT_SHARD_BUDGET_CHARS) -> List[DiffShard]:
    """Pack the diff's files into shards of at most ~`budget_chars` each."""
    budget = max(1, int(budget_chars))
    preamble, blocks = split_file_blocks(pr_diff)
    if not blocks:
        # Nothing file-shaped (e.g. a bare hunk): treat the whole thing as one
        # shard so callers never get an empty plan.
        whole = DiffShard(index=1, total=1, files=[], diff=pr_diff,
                          chars=len(pr_diff), oversized=len(pr_diff) > budget)
        return [whole] if pr_diff else []

    grouped: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    current_chars = 0
    for path, block in blocks:
        size = len(block) + 1        # + the newline the join will re-add
        if current and current_chars + size > budget:
            grouped.append(current)
            current, current_chars = [], 0
        current.append((path, block))
        current_chars += size
    if current:
        grouped.append(current)

    total = len(grouped)
    shards: List[DiffShard] = []
    for i, group in enumerate(grouped, start=1):
        text = "\n".join(block for _, block in group)
        if i == 1 and preamble:
            # keep the preamble (format-patch header etc.) reachable
            text = f"{preamble}\n{text}"
        chars = len(text)
        files = [path for path, _ in group if path]
        shards.append(DiffShard(index=i, total=total, files=files, diff=text,
                                chars=chars, oversized=chars > budget))
    return shards


def should_shard(pr_diff: str, budget_chars: int = DEFAULT_SHARD_BUDGET_CHARS) -> bool:
    """True when the diff does not fit in a single shard's budget.

    Deliberately the same comparison as the per-shard budget: "does it fit in one
    request" is exactly the question the budget answers, so there is one number to
    reason about rather than a threshold plus a size.
    """
    return len(pr_diff or "") > max(1, int(budget_chars))
