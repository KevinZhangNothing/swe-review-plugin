---
name: swe-review-analyze
description: Static analysis of a candidate PR diff — counts additions/deletions/hunks, surfaces public-API changes, flags TODO/FIXME and new exception paths, computes a complexity score. Useful as a cheap signal before/alongside an LLM review.
---

# swe-review-analyze

> Wraps `swe_review.AnalyzeSkill` and `AnalyzerSubAgent`.

## When to use

When you want fast, deterministic metadata about a diff without paying for an LLM call. Good for:
- Pre-review routing (skip review if complexity is near zero).
- Logging / observability of loop iterations.
- A reviewer context augmentation.

## What it does

- Splits diff into hunks, counts +/-
- Detects `def name(` changes (heuristic for public API surface)
- Detects `# TODO`, `XXX`, `FIXME` inside new lines
- Detects `except`/`raise` patterns (new failure paths)
- Returns a 0..1 `complexity_score`

## How to invoke

```python
from swe_review import AnalyzeSkill
skill = AnalyzeSkill()
res = await skill.execute(pr_diff=open("pr.diff").read())
print(res.payload["total_additions"], res.payload["complexity_score"])
print(res.payload["public_api_changes"])
```

## Output keys

- `total_hunks`, `total_additions`, `total_deletions`, `files_changed`
- `public_api_changes: [str]`
- `suspicious_spots: [{file, hunk, kind}]`
- `complexity_score: float` (0.0–1.0)
