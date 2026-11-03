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

## Host mode — answer with the agent running this skill (preferred interactively)

`--tool host` does **not** spawn `claude`/`pi`/`opencode`/`agent`. It writes the exact
prompt to disk and expects **you** (the agent running this command) to answer it with
your own model:

```bash
# 1) run; exits 3 with a JSON envelope naming the prompt that needs an answer
swe-review review --issue "..." --pr-diff pr.diff --tool host --host-dir .swe-host

# 2) read the prompt, answer it with YOUR OWN model (never spawn another CLI)
swe-review host pending --host-dir .swe-host --show
swe-review host answer --key <key> --text-file <your answer file>

# 3) re-run the exact same command; answered prompts replay from cache
swe-review review --issue "..." --pr-diff pr.diff --tool host --host-dir .swe-host
```

**What to answer:** the review report JSON documented below (engineering or
concise schema, matching `--prompt-style`). Your raw model output is accepted
directly — no envelope needed — or use `{"text": ..., "usage": {...}}` to also
record token usage. `--text -` reads stdin. Exit code 3 = awaiting host,
0 = done; anything else is a real failure.

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
candidates (safe-delete vs defer-with-plan), SOLID smells, a simplicity ladder for every
new abstraction/helper/dependency in the diff (needed at all? repo already has it? stdlib
ships it? one line? — violations are tagged `constraint_category=simplicity_overengineering`
and must name the concrete replacement), and language-specific checks
(JS/TS, Python, Go, Rust, SQL — auto-trimmed to the languages present in the diff to
control fixed prompt token cost).

### Anti-bloat / patch-stacking check

The review explicitly targets **additive problem solving** — resolving a local symptom by
ADDING a bool, a flag, a guard, a compatibility branch or a wrapper layer instead of
adjusting the logic that is already there. `Patch Analysis` carries a `bloat_signals`
array computed statically (no LLM) by `detect_bloat_signals`:

| signal | fires when |
|---|---|
| `additive_only_change` | an **existing** file gained ≥8 real lines and lost none |
| `flag_accumulation` | ≥2 new boolean-ish state variables |
| `guard_stacking` | ≥3 added conditionals with **no** conditional removed |
| `compat_layer` | ≥2 added fallback/legacy/workaround markers |
| `wrapper_growth` | a ≤40-line additive change to an existing file adds a new top-level def/class |
| `field_list_duplication` | ≥2 field names appear BOTH as declarations and as mapping keys, in different files |

`field_list_duplication` is a different kind of check from the five above: it is
not a per-line pattern. A new model field (`final bool stereoCharging;`) and the
mapper entry that serializes it (`'stereoCharging': o.stereoCharging,`) are
different text, so identical-line detection sees nothing and no line pattern
knows they are one fact written twice. The judge is therefore set overlap: it
fires only when the same set of ≥2 field names is added on both the declaration
side and the mapping side, in two different files. That co-occurrence is what
makes the list duplicated — the next field must be remembered in both places or
it silently stops round-tripping, with no compile error and often no test.

Deliberately conservative: one new struct with several fields and no mapper
produces nothing; a co-located `fromMap` in the same file produces nothing; and
renamed keys (`providerId` → `provider_id`) share no exact name, so the text
layer stays silent rather than guessing. That last one is a known limit, stated
in the prompt so the model checks it in context instead of trusting the signal.

Thresholds sit deliberately above one: a single necessary variable or guard is ordinary
engineering and is never reported. Signals are **evidence, not verdicts** — a signal the
model judges inapplicable must be dismissed with a reason, not silently ignored.

Prose is never read as code. Comments, docstrings/block literals (including a docstring
opened by an unchanged context line), prompt string literals, symbol definitions that
merely name a concept, and non-code files (`*.md`, `*.toml`, `*.yaml`, …) are all
excluded from signal matching. Each of those exclusions fixes a real false positive found
by running the detector against its own source; `additive_only_change` still counts every
real added line, prose included, because growth is growth.

The prompt also ranks revision strategies (modify existing logic > reuse existing
symbols > simplify structure > add one variable > add several states > add a parallel
mechanism) and requires a mandatory quantitative self-report in the output:

```json
"complexity_delta": {
  "new_state_vars": 2, "new_functions": 0,
  "derivable_state": "has_fallback",
  "removable_old_code": "legacy_charge branch",
  "net_complexity": "reduced | neutral | increased"
}
```

`net_complexity: increased` without a corresponding finding or action is an unsupported
scoring claim. `constraint_category` is propagated into the derived `defects[]` so the
reviser can tell "delete the redundant flag" from "add the missing branch".

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
