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
4. Keep changes minimal; do not refactor unrelated code. The `minimal` perspective walks a simplicity ladder before adding anything: needed at all? repo already has an equivalent? stdlib ships it? one line? New code only when all fail.

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

## Host mode — answer with the agent running this skill (preferred interactively)

`--tool host` does **not** spawn `claude`/`pi`/`opencode`/`agent`. It writes the
generation prompt to disk and expects **you** to answer it with your own model:

```bash
# 1) run; exits 3 with a JSON envelope naming the prompt that needs an answer
swe-review loop --issue "..." --strategy review_guided --tool host --host-dir .swe-host

# 2) read the prompt, answer it with YOUR OWN model (never spawn another CLI)
swe-review host pending --host-dir .swe-host --show
swe-review host answer --key <key> --text-file <your answer file>

# 3) re-run the exact same command; answered prompts replay from cache and the
#    loop advances to its next phase (review -> revise/verify ...)
swe-review loop --issue "..." --strategy review_guided --tool host --host-dir .swe-host
```

**What to answer:** the candidate-PR JSON below (`title`/`body`/`diff`/`rationale`,
diff must be `git apply`-compatible). Your raw model output is accepted directly,
or `{"text": ..., "usage": {...}}` to also record token usage. Exit code 3 =
awaiting host, 0 = done.

## Output keys

- `title`, `body`, `diff` (unified diff text), `rationale`, `confidence` (0..1)
