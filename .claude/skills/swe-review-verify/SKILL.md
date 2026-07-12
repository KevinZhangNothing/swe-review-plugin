---
name: swe-review-verify
description: Verify a candidate PR by applying the patch (in a sandbox tempdir if enabled) and optionally running `fail_to_pass` and `pass_to_pass` tests. Returns a structured pass/fail result. Sandbox by default to avoid polluting the user's working tree.
---

# swe-review-verify

> Wraps `swe_review.VerifySkill` and `VerifierSubAgent`.

## When to use

When you need to know whether a given PR resolves an issue, beyond what the LLM reviewer says.

Do **not** use inside the review/revise prompt — the verifier CAN see oracle (ground-truth patch) for offline evaluation, but **the reviewer MUST NOT**.

## Hard constraints

1. Sandbox by default: clones the repo into a `tempfile.mkdtemp` and applies the patch there. User's working tree is untouched.
2. Optional `oracle` (gold patch) is only used for offline similarity scoring, never injected to a review prompt.
3. Test runner template uses `{test}` placeholder; default tries `pytest -q`, `pytest -x -q`, `unittest`, `npm test`.

## How to invoke

```python
from swe_review import VerifySkill
verify = VerifySkill(repo_path=".")
res = await verify.execute(
    pr_diff=open("pr.diff").read(),
    test_info={
        "fail_to_pass": ["tests/test_x.py::test_foo"],
        "pass_to_pass": ["tests/test_x.py::test_bar"],
    },
    # oracle is optional AND restricted to evaluator-only paths:
    oracle=open("gold.diff").read(),
)
print(res.payload["resolution_status"], res.payload["confidence"])
```

## Output keys

- `passed: bool`
- `test_results: [{test, type, should_pass, passed, stdout_tail, stderr_tail}]`
- `resolution_status`: resolved | not_resolved | partially_resolved | unknown
- `confidence: float`
- `details: str`
- `patch_applied: bool`, `sandbox_used: bool`
- `oracle_similarity: float | None`
