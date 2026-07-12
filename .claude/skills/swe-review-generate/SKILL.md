---
name: swe-review-generate
description: Generate a candidate PR (unified diff) for a given software issue, with optional exploration context. Used by `best_of_n` and `hybrid` strategies in the loop. The output must be a `git apply`-compatible unified diff.
---

# swe-review-generate

> Wraps `swe_review.GenerateSkill` and `GeneratorSubAgent`.

## When to use

When you need a fresh candidate PR for an issue, typically before a `review_guided` or `best_of_n` loop starts. The generator optionally receives exploration context to ground the patch in real file contents and call chains.

## Hard constraints

1. `diff` MUST be `git apply`-compatible unified diff.
2. Output JSON only: `{title, body, diff, rationale, confidence}`.
3. No oracle / golden patch injection — the generator sees only `issue`, optional `hint`, optional repo exploration.
4. Keep changes minimal; do not refactor unrelated code.

## How to invoke

```python
from swe_review import GenerateSkill, ExploreSkill, OpenCodeAdapter
explore = ExploreSkill(repo_path="/path/to/repo")
gen = GenerateSkill(tool_adapter=OpenCodeAdapter(), explore_skill=explore)
res = await gen.execute(
    issue="Adding tests for null deref case.",
    repo_path="/path/to/repo",
)
open("candidate.diff", "w").write(res.payload["diff"])
```

## Output keys

- `title`, `body`, `diff` (unified diff text), `rationale`, `confidence` (0..1)
