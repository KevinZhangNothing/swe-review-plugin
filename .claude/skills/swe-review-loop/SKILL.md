---
name: swe-review-loop
description: Run the Generate → Review → (Revise | Regenerate) → Verify loop on a software issue. Supports `review_guided`, `best_of_n`, and `hybrid` strategies. Tracks iterations, accumulates token usage, supports early stopping.
---

# swe-review-loop

> Wraps `swe_review.LoopSkill` and `LoopSubAgent`.

## When to use

Use when the user wants end-to-end closed-loop processing: a generator produces a candidate, the reviewer evaluates it, the reviser fixes defects, optionally a verifier runs tests. Iterate until approve, or budget exhaustion.

## Strategies

- `review_guided` (default): single candidate + iterative revision via review defects.
- `best_of_n`: generate N candidates, review each, pick the first `approve` or highest-confidence.
- `hybrid`: `best_of_n(n=3)` first; if none approved, fall back to `review_guided`.

## Hard constraints

1. **NEVER** inject `golden_patch`, `gold_patch`, or `oracle` through the loop to a reviewer/reviser/generator. If you have a gold patch, gate it through `VerifySkill.execute(..., oracle=...)` after the loop.
2. The loop returns the chosen `final_pr_diff` and a structured `LoopResult` you can serialize to JSON.
3. Default `max_iterations=5`; respect early_stop unless explicitly disabled.
4. **NEVER** pin or request a specific model anywhere in the loop (no `--model`, no `*_MODEL` env, no `model=` arg). Every subagent inherits whatever model the host CLI/environment is configured with.
5. **Subagent adapter calls are pure text generation.** Never grant the host CLI write/edit/bash tools to a subagent call (pi: `--no-tools`). Exploration is done locally by `ExplorerSubAgent`; a review/reviser with tool access can mutate the repo under review and corrupt the verifier baseline.

## How to invoke

```python
import asyncio
from swe_review import (
    LoopSkill, ReviewSkill, ReviseSkill, GenerateSkill, VerifySkill,
    ClaudeCodeAdapter,
)

tool = ClaudeCodeAdapter()
review = ReviewSkill(tool_adapter=tool)
revise = ReviseSkill(tool_adapter=tool)
generate = GenerateSkill(tool_adapter=tool)
verify = VerifySkill(repo_path="/path/to/repo")

loop = LoopSkill(
    review_skill=review,
    revise_skill=revise,
    generator_skill=generate,
    verify_skill=verify,
    strategy="hybrid",
    max_iterations=5,
)

async def main():
    res = await loop.execute(
        issue="Matrix determinant returns NaN",
        repo_path="/path/to/repo",
        strategy="hybrid",
    )
    print(res.payload["success"], res.payload["total_iterations"])
    print(res.payload["final_pr_diff"])

asyncio.run(main())
```

## Output keys

- `success: bool`, `final_decision: str`, `final_pr_diff: str`
- `total_iterations`, `iterations: [{iteration, phase, decision, confidence, defects_count, ...}]`
- `resolve_rate: float` (verifier signal, 0 if not run)
- `token_usage_total: dict`
- `strategy: "review_guided" | "best_of_n" | "hybrid"`
- `elapsed_seconds: float`
