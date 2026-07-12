"""Verify example: apply a patch in a sandbox tempdir and (optionally) run tests."""

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swe_review import VerifySkill  # noqa: E402


SAMPLE_DIFF = """
diff --git a/sample.py b/sample.py
new file mode 100644
--- /dev/null
+++ b/sample.py
@@ -0,0 +1,3 @@
+def add(a, b):
+    return a + b
+
"""


async def main() -> None:
    verify = VerifySkill(repo_path=".", config={"sandbox": True})
    res = await verify.execute(
        pr_diff=SAMPLE_DIFF,
        test_info={"fail_to_pass": [], "pass_to_pass": []},
    )
    print(json.dumps(res.payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
