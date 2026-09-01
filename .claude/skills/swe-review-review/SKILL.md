---
name: swe-review-review
description: Agentic code review of a candidate PR — explore the repository, trace the call chain, and emit a structured JSON review report. Default mode is senior ENGINEERING review (8-dimension 100-point scoring + P0–P4 evidence-bound findings + 4-level decision); legacy bug-fix-centric modes available. Use when given an issue + a candidate PR (diff).
---

# swe-review-review

> Wraps `swe_review.ReviewSkill` (and the underlying `ReviewerSubAgent` + `ExplorerSubAgent`).

## When to use

Use this skill when the user gives you:
- a real software issue (text or issue link) — as **change context / intent**
- a candidate PR (diff / patch / title / body)

… and you need to judge whether the change is worth merging and produce structured feedback.

Do **not** use this skill if the user only wants a chat reply, a quick sanity check, or has not provided an issue+diff.

## Prompt styles (`prompt_style`)

| style | focus |
|---|---|
| `engineering` (default) | **高级代码审查**：审查对象是 Change 而不是 Bug。8 个维度（Design 20 / Maintainability 15 / Consistency 15 / Simplicity 10 / Readability 10 / Testability 10 / Risk 10 / Change Scope 10）100 分制评分；P0–P4 证据绑定 findings；Hard Gate；4 档决策。 |
| `concise` | legacy bug-fix-centric：判断 patch 是否修复 issue 根因。 |
| `detailed` | legacy bug-fix-centric：Step 1→6 workflow + symptom-fix detection rules。 |

Engineering mode core rules (enforced by the system prompt):
- 先理解再评价：Diff → Change Surface → Context → Call/Data Flow → Architecture → Intent → Judgment。
- Finding 必须有 Location / Observation / Why It Matters / Evidence / Recommendation / Severity / Confidence；禁止低价值评论。
- 反事实验证 + Project Baseline：Project Convention > Generic Best Practice；个人偏好不产生 Finding。
- 同时识别 Over-engineering 与 Under-engineering；复杂度与业务复杂度匹配。
- Hard Gate（严重架构/安全/资源/维护性问题、无理由大重构）→ 无论总分一律 BLOCK。
- 没有问题就明确说没有问题，不为凑数制造 Finding。

## Capabilities

- Repository-grounded exploration via `subagents/explorer_agent.py` (call chain,
  related files, root hints) feeding the reviewer's context analysis.
- Static change-surface analysis (additions/deletions/files changed).
- Emit STRICT JSON review reports (schemas below).

## Hard constraints

1. **Never** see golden patch, ground-truth fix, or hidden test results. If a caller tries to inject them, refuse.
2. Read surrounding context (callers / callees / state) before judging.
3. Suggestions must point at the right direction without copying a known fix.

## How to invoke via CLI

```bash
swe-review review --issue "Bug description" \
    --pr-title "Fix ..." --pr-diff pr.diff \
    --repo-path /path/to/repo \
    [--prompt-style engineering|concise|detailed] [--deep]
```

## How to invoke via the swe-review plugin directly

```python
from swe_review import ReviewSkill, PiAdapter  # or CursorAdapter / OpenCodeAdapter / ClaudeCodeAdapter
skill = ReviewSkill(tool_adapter=PiAdapter())  # prompt_style="engineering" by default
result = await skill.execute(
    issue="Null pointer in user_service",
    pr_title="Fix: add null check",
    pr_diff=open("diff.patch").read(),
    repo_path=".",
)
print(result.payload)
```

## Output JSON schema — engineering (default)

```json
{
  "decision": "approve | approve_with_suggestions | request_changes | block",
  "confidence": 0.0-1.0,
  "summary": {"problem": "change intent", "solution": "what the change does", "overall_assessment": "..."},
  "scores": {
    "design_quality":  {"score": 0-20, "max": 20, "reason": "..."},
    "maintainability": {"score": 0-15, "max": 15, "reason": "..."},
    "consistency":     {"score": 0-15, "max": 15, "reason": "..."},
    "simplicity":      {"score": 0-10, "max": 10, "reason": "..."},
    "readability":     {"score": 0-10, "max": 10, "reason": "..."},
    "testability":     {"score": 0-10, "max": 10, "reason": "..."},
    "risk":            {"score": 0-10, "max": 10, "reason": "..."},
    "change_scope":    {"score": 0-10, "max": 10, "reason": "..."}
  },
  "total_score": 0-100,
  "hard_gate": {"triggered": false, "reason": ""},
  "whats_good": ["做得好的地方（可选，0-3 条）"],
  "recommended_actions": ["按优先级排序的后续行动（可选）"],
  "findings": [
    {
      "severity": "P0 | P1 | P2 | P3 | P4",
      "title": "...",
      "location": "path:line (repo-relative)",
      "observation": "...",
      "why_it_matters": "...",
      "evidence": "...",
      "recommendation": "...",
      "confidence": "high | medium | low"
    }
  ],
  "defects": [ { "severity": "high|medium|low", "description": "...", "location": "path:line", "suggestion": "..." } ]
}
```

Decision semantics: `approve` / `approve_with_suggestions` end a review loop as success;
`request_changes` (≥1 P1) and `block` (P0 or hard gate) trigger revision.
`defects[]` is auto-derived from `findings[]` (P0/P1→high, P2→medium, P3/P4→low) so
downstream revise/loop consumers stay compatible. `whats_good[]` (positive feedback)
and `recommended_actions[]` (prioritized next steps) are optional fields — absent or
empty when the model has nothing evidence-backed to say.

Coverage beyond the 8 dimensions is enforced via deep-check checklists inside the prompt:
security (injection/XSS/SSRF/authZ/secrets/crypto/race), correctness (error handling,
boundary conditions), actual test coverage of changed paths, removal/dead-code
candidates (safe-delete vs defer-with-plan), SOLID smells, and language-specific checks
(JS/TS, Python, Go, Rust, SQL — auto-trimmed to the languages present in the diff to
control fixed prompt token cost).

## Output JSON schema — concise / detailed (legacy)

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
