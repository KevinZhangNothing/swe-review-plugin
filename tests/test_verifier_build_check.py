"""Build-check phase tests for VerifierSubAgent."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from swe_review import VerifySkill

GOOD = """--- a/hello.py
+++ b/hello.py
@@ -1,2 +1,3 @@
 def f():
     return 1
+def g(): return 2
"""
BAD = GOOD.replace("def g(): return 2", "def g( return")


def _make_repo() -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "hello.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "init"], cwd=tmp, capture_output=True)
    return tmp


class TestBuildCheck(unittest.TestCase):
    def test_good_diff_passes_build_check(self):
        res = asyncio.run(VerifySkill(repo_path=str(_make_repo())).execute(pr_diff=GOOD))
        p = res.payload
        self.assertTrue(p["patch_applied"])
        self.assertEqual([r["name"] for r in p["build_results"]], ["python_syntax"])
        self.assertTrue(p["build_results"][0]["passed"])
        # no tests configured -> legacy "unknown" semantics unchanged
        self.assertEqual(p["resolution_status"], "unknown")

    def test_syntax_error_fails_fast_not_resolved(self):
        res = asyncio.run(VerifySkill(repo_path=str(_make_repo())).execute(pr_diff=BAD))
        p = res.payload
        self.assertFalse(p["passed"])
        self.assertEqual(p["resolution_status"], "not_resolved")
        self.assertEqual(p["confidence"], 0.9)
        self.assertFalse(p["build_results"][0]["passed"])
        self.assertEqual(p["test_results"], [])  # fail fast: tests never ran

    def test_build_check_disabled(self):
        res = asyncio.run(
            VerifySkill(repo_path=str(_make_repo()), config={"build_check": False})
            .execute(pr_diff=BAD)
        )
        p = res.payload
        self.assertEqual(p["build_results"], [])
        self.assertEqual(p["resolution_status"], "unknown")  # bad syntax slips through

    def test_skipped_entry_has_cmd(self):
        # explicit resolve_cmd pointing at a missing tool -> skipped, schema intact
        res = asyncio.run(
            VerifySkill(repo_path=str(_make_repo()),
                        config={"resolve_cmd": "definitely-not-a-real-tool --check"})
            .execute(pr_diff=GOOD)
        )
        r = res.payload["build_results"][0]
        self.assertTrue(r.get("skipped"))
        self.assertTrue(r["passed"])
        self.assertIn("cmd", r)


if __name__ == "__main__":
    unittest.main()
