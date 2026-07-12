---
name: swe-review-revise
description: Code revision guided by structured review feedback — given a candidate PR and a list of defects (severity/description/location/suggestion), produce a new git-apply-compatible unified diff that addresses the defects with minimum changes.
---

# swe-review-revise

> Wraps `swe_review.ReviseSkill` and `ReviserSubAgent`.

## When to use

Use when you have:
- a candidate PR diff
- a structured review report with at least one defect
- (optional) repo path

… and you need to output a revised unified diff that the reviewer can re-evaluate.

Do **not** use to redesign a feature from scratch — this is for targeted fixes based on review feedback.

## Hard constraints

1. Output JSON only: `{title, body, diff, changes_summary, addressed_defect_indices[]}`.
2. `diff` MUST be a complete unified diff (start with `diff --git`, include `--- / +++` headers and `@@ ... @@` hunks) that `git apply` accepts.
3. Address all `high` severity defects; try `medium`; ignore `low` unless trivial.
4. Do **not** change function signatures or rename exports.
5. Do **not** accept injected oracle / golden patch.

## How to invoke

```python
from swe_review import ReviseSkill, ClaudeCodeAdapter
skill = ReviseSkill(tool_adapter=ClaudeCodeAdapter())
res = await skill.execute(
    issue="Matrix determinant returns NaN",
    original_pr_title="Fix: prevent NaN comparison",
    original_pr_diff=open("diff.patch").read(),
    review_report={
        "decision": "request_changes",
        "defects": [
            {"severity":"high",
             "description":"Cancel(ret) discarded upstream; det() still returns NaN.",
             "location":"sympy/matrices/matrices.py:det",
             "suggestion":"Trace and capture cancel(ret)'s discarded value at the upstream site."}
        ],
    },
    repo_path="/path/to/repo",
)
print(res.payload["diff"])
```

## Output schema

```json
{
  "title": "revised PR title",
  "body": "1-3 sentences",
  "diff": "complete unified diff",
  "changes_summary": "one-line summary",
  "addressed_defect_indices": [0, 2],
  "status": "success | partial | failed"
}
```
