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
    strategy="hybrid",          # review_guided | best_of_n | hybrid
    max_iterations=5,
)

async def main():
    res = await loop.execute(
        issue="Matrix determinant returns NaN",
        repo_path="/path/to/repo",
        strategy="hybrid",                       # per-run override of the above
        # Everything below is OPTIONAL. `test_info` is the one that matters most:
        # without it the verifier can only check that the patch applies, so the
        # run has no real resolution signal and `resolve_rate` stays 0.0.
        test_info={"fail_to_pass": ["tests/test_x.py::test_y"], "pass_to_pass": []},
        test_runner=["python", "-m", "pytest", "{test}", "-q"],
        # Self-review mode: review/revise THIS candidate instead of generating one.
        initial_pr={"title": "...", "body": "", "diff": candidate_diff},
        max_iterations=3,
        prompt_style="engineering",              # engineering | concise | detailed
        revision_feedback_level="full_feedback",  # full_feedback | minimal_feedback | baseline
        n_best_of=3,                             # best_of_n / hybrid only
    )
    print(res.payload["success"], res.payload["total_iterations"])
    print(res.payload["final_pr_diff"])

asyncio.run(main())
```

Constructor vs `execute`: `LoopSkill(...)` sets the defaults for a reusable loop;
every one of them can be overridden per call through `execute(...)`.

The CLI (`swe-review loop ...`) exposes the same surface — see `swe-review loop -h`.

## Host mode — the current agent drives the whole loop (preferred interactively)

`--tool host` makes **the agent running this command** the LLM of every phase
(review / revise / generate). No `claude`/`pi`/`opencode` subprocess is ever
spawned, and `LoopSubAgent` needs no changes — iteration control, early stop,
`best_of_n` / `hybrid` and hard gates all keep working, because they only ever
call `adapter.chat()`.

```bash
LOOP="swe-review loop --issue 'Fix NaN determinant' --repo-path /path/to/repo \
      --tool host --host-dir .swe-host --max-iterations 3"

$LOOP                      # 1) exits 3: 'awaiting_host' + key + request_path
swe-review host pending --host-dir .swe-host --show   # 2) read the prompt
swe-review host answer --key <key> --text-file answer.json   # 3) answer yourself
$LOOP                      # 4) same command again: cache replay advances the loop
```

Per-phase answer contract (check the pending prompt to tell which phase):

| prompt asks for | answer |
|---|---|
| review report | review JSON schema (see swe-review-review) |
| "repair malformed JSON" | the SAME report as one valid JSON object |
| revised PR | revised-PR JSON with unified `diff` (see swe-review-revise) |
| candidate PR | `title`/`body`/`diff`/`rationale` (see swe-review-generate) |

Rules: answer with your own model — do **not** spawn another CLI; exit code
3 = awaiting host, 0 = done. `--host-dir` is the rendezvous dir
(`requests/` + `responses/`); reuse it across the rounds of one run.
While answering, do **not** use write/edit/bash tools against the repo under
review — same reason as hard constraint #5: an answer is pure text, and mutating
the repo would corrupt the verifier baseline.

## Output keys

- `success: bool`, `final_decision: str`, `final_pr_diff: str`
- `total_iterations`, `iterations: [{iteration, phase, decision, confidence, defects_count, notes, review_payload?}]`
- `resolve_rate: float` — **read this carefully.** It is derived from the verifier's
  `resolution_status` (`resolved` → 1.0, `partially_resolved` → 0.5, else 0.0).
  It is 0.0 not only when the verifier did not run, but also whenever no
  `test_info` was supplied: the verifier can then only prove that the patch
  applies, which is a weak pass and cannot resolve anything. A `resolve_rate` of
  0.0 therefore means "unmeasured", **not** "the tests failed". Pass `test_info`
  (and a `test_runner` when the default command does not fit) to get a real signal.
- `token_usage_total: dict`
- `strategy: "review_guided" | "best_of_n" | "hybrid"`
- `elapsed_seconds: float`
- `message: str` — why the loop stopped, in words. It distinguishes
  `max iterations reached (N)` from `revise produced no usable diff at iteration i
  (reviser status=…)` and `no reviser configured`. A `revise produced no usable
  diff` stop often means the model emitted a structurally corrupt diff, which is
  rejected on purpose at the point of production rather than being handed to the
  verifier — see `swe_review/subagents/diff_validation.py`. Check `message` before
  concluding the loop "hit its budget".

## Notes that bite in practice

- **`final_pr_diff` is not validated for you.** The reviser/generator drop a diff
  whose hunk arithmetic is wrong (it would otherwise be approved by review and only
  rejected by verify at the very end). If a revision was dropped, `message` says so.
- **Grading and benchmarking require unique prompts.** The `pi` CLI caches responses
  per prompt (measured ~9x faster on an identical repeat), so any A/B comparison that
  reuses a prompt is meaningless — put a nonce in the `issue`.
- **Two passes can disagree on scores.** A change big enough to be reviewed in
  parallel shards is scored globally from the merged findings, so its `total_score`
  is not directly comparable with the single-call path. Pin one mode when the score
  is used as a gate.
