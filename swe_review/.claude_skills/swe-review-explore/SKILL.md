---
name: swe-review-explore
description: Collect repository context for a code-review task. Returns modified files, related files, file contents (truncated), call chain hints, keyword extraction, and a `root_hint` list to focus attention. Use when starting a review or revision that needs repository-grounded evidence.
---

# swe-review-explore

> Wraps `swe_review.ExploreSkill` and `ExplorerSubAgent`.

## When to use

When you have an `issue` plus optional `pr_diff` and want fast, structured repository context (without a full LLM review).

## What it does

1. Parses the diff to identify modified files.
2. Extracts keywords from the issue.
3. Greps the repo for related files.
4. Finds likely test files for each modified source file.
5. Reads contents (truncated by `max_file_bytes`, default 30KB per file).
6. Extracts simple call-chain (`def` + invoked names).
7. Emits `root_hint` from changed function names + issue keywords.

## Hard constraints

- Read-only; never modifies the repo.
- Aggressive size caps to keep downstream prompts manageable.
- No oracle / golden patch injection.

## How to invoke

```python
from swe_review import ExploreSkill
skill = ExploreSkill(repo_path=".")
res = await skill.execute(
    issue="Matrix determinant returns NaN",
    pr_diff=open("diff.patch").read(),
    max_steps=8,
)
print(res.payload["files_modified"])
print(res.payload["related_files"])
print(res.payload["root_hint"])
```

## Output keys

- `repo_path`, `files_modified`, `related_files`, `test_files`
- `file_contents: {path: str}`, `call_chain: [{file, function, defined_in}]`
- `keywords`, `root_hint`, `steps`, `truncated`
