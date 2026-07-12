"""Basic example: review a candidate PR end-to-end."""

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swe_review import ReviewSkill, PiAdapter  # noqa: E402

# A trivial invalid diff — Reviewer will flag that the patch does not touch any
# real files. Replace with a real candidate PR (and a real tool adapter) to
# exercise the full path.
SAMPLE_DIFF = """
diff --git a/sympy/core/exprtools.py b/sympy/core/exprtools.py
@@ -1176,7 +1176,7 @@ def _keep_on_factoring(expr):
-    if all(a.as_coeff_Mul()[0] < 0 for a in list_args):
+    if all(c.is_finite and c.is_negative for c in coeffs):
+        for c in (a.as_coeff_Mul()[0] for a in list_args):
"""


async def main() -> None:
    skill = ReviewSkill(tool_adapter=PiAdapter())
    report = await skill.execute(
        issue="Matrix determinant returns NaN for matrices with symbolic entries",
        pr_title="Fix: prevent NaN comparison error with symbolic matrices",
        pr_diff=SAMPLE_DIFF,
        repo_path=".",
    )
    print(json.dumps(report.payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
