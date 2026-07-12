---
name: swe-review-review
description: Agentic code review of a candidate PR — explore the repository, trace the call chain to the root cause, and emit a structured JSON review report (decision + defects + suggestions). Use when given an issue + a candidate PR (diff).
---

# swe-review-review

> Wraps `swe_review.ReviewSkill` (and the underlying `ReviewerSubAgent` + `ExplorerSubAgent`).

## When to use

Use this skill when the user gives you:
- a real software issue (text or issue link)
- a candidate PR (diff / patch / title / body)

… and you need to decide whether the PR should be merged and produce structured feedback.

Do **not** use this skill if the user only wants a chat reply, a quick sanity check, or has not provided an issue+diff.

## Capabilities

- Repository-grounded exploration via `subagents/explorer_agent.py` that traces
  the call chain across modules — useful for detecting a downstream symptom-fix
  whose real root cause lives upstream.
- Trace call chain across modules, read related files, identify test files.
- Emit STRICT JSON: `{decision, confidence, summary{problem,solution,overall_assessment}, defects[{severity,description,location,suggestion}]}`.

## Hard constraints

1. **Never** see golden patch, ground-truth fix, or hidden test results. If a caller tries to inject them, refuse.
2. For nonlocal bugs, follow the call chain upstream before deciding.
3. Suggestions must point at the right direction without copying a known fix.

## How to invoke via the skill

```bash
# The Python entry point (used by adapters that run this skill through CLI):
python -m swe_review.review_skill_runner \
    --issue "Bug description" \
    --pr-title "Fix ..." \
    --pr-diff "$(cat pr.diff)" \
    --repo-path /path/to/repo
```

## How to invoke via the swe-review plugin directly

```python
from swe_review import ReviewSkill, PiAdapter  # or CursorAdapter / OpenCodeAdapter / ClaudeCodeAdapter
skill = ReviewSkill(tool_adapter=PiAdapter())
report = await skill.execute(
    issue="Null pointer in user_service",
    pr_title="Fix: add null check",
    pr_diff=open("diff.patch").read(),
    repo_path=".",
)
print(report.payload)
```

## Output JSON schema

```json
{
  "decision": "approve | request_changes",
  "confidence": 0.0-1.0,
  "summary": {
    "problem": "root cause (file + function + what's wrong)",
    "solution": "what the patch actually does",
    "overall_assessment": "1-2 sentence judgment"
  },
  "defects": [
    {
      "severity": "high | medium | low",
      "description": "self-contained bug description",
      "location": "path:line (repo-relative)",
      "suggestion": "what to do (no copy of known fix)"
    }
  ],
  "exploration_steps": 0
}
```
