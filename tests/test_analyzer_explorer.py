"""Regression tests for the Analyzer and Explorer SubAgents.

Neither SubAgent's core output was previously asserted anywhere: the suite
exercised `detect_repeated_added_blocks` (the analyzer's extracted helper) 15
times but never instantiated `AnalyzerSubAgent`, and never asserted
`related_files` / `test_files` at all. That blind spot is how both defects below
survived: a public-API detector with a 100% false-positive rate on a real diff,
and an explorer that returned 600+ vendored `node_modules` paths while finding
zero test files.
"""
import asyncio

from swe_review.subagents import explorer_agent
from swe_review.subagents.analyzer_agent import AnalyzerSubAgent
from swe_review.subagents.explorer_agent import (
    MAX_RELATED_FILES, ExplorerSubAgent,
)


# ---------------------------------------------------------------------------
# AnalyzerSubAgent — public API detection
# ---------------------------------------------------------------------------

# A textual diff (the analyzer never parses Python, it scans added lines).
_DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
@@ -1,3 +1,12 @@
 existing = 1
+def added_public():
+    return 1
+class AddedPublic:
+    def method(self):
+        return isinstance(
+            self, dict)
+        super().__init__(
+except (ValueError, TypeError):
+        assert (a, b) == (1, 2)
+def _private_helper():
"""


def _analyze(pr_diff):
    return asyncio.run(AnalyzerSubAgent().execute(
        {"pr_diff": pr_diff, "old_content": {}, "new_content": {}})).to_dict()


def test_public_api_changes_only_counts_top_level_definitions():
    res = _analyze(_DIFF)
    changes = res["public_api_changes"]
    assert any("added_public" in c for c in changes)
    assert any("AddedPublic" in c for c in changes)


def test_public_api_changes_ignores_indented_and_expression_lines():
    """Regression: `lstrip()` used to erase indentation, so *any* added line
    starting with `identifier(` was reported as a public API change — including
    `except (`, `isinstance(`, `super().__init__(` and test continuations."""
    changes = _analyze(_DIFF)["public_api_changes"]
    for bogus in ("isinstance(", "super().__init__(", "except (", "assert ("):
        assert not any(bogus in c for c in changes), f"{bogus!r} falsely reported"
    # the nested method is indented -> not part of the module's public surface
    assert not any("method" in c for c in changes)


def test_public_api_changes_excludes_private_names():
    assert not any("_private_helper" in c for c in _analyze(_DIFF)["public_api_changes"])


def test_public_api_changes_skips_test_files():
    """Added test functions are not changes to the shipped public surface; and
    since API_NORMALIZER is 5, five of them alone saturate complexity_score."""
    diff = (
        "diff --git a/tests/test_thing.py b/tests/test_thing.py\n"
        "@@ -1 +1,4 @@\n"
        "+def test_one():\n"
        "+    pass\n"
        "+def test_two():\n"
        "+    pass\n"
    )
    res = _analyze(diff)
    assert res["public_api_changes"] == []
    assert res["complexity_score"] < 1.0


def test_complexity_score_not_saturated_by_expression_lines():
    """20 bogus API changes used to saturate the API term of complexity_score."""
    noisy = "diff --git a/t.py b/t.py\n@@ -1 +1,12 @@\n" + "".join(
        f"+        assert (x, y) == ({i}, {i})\n" for i in range(10)
    )
    res = _analyze(noisy)
    assert res["public_api_changes"] == []
    assert res["complexity_score"] < 1.0


# ---------------------------------------------------------------------------
# ExplorerSubAgent — related-file search hygiene
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_related_files_search_excludes_vendored_dirs(tmp_path, monkeypatch):
    """The grep must be told to skip vendored/build dirs, and any path that
    still reaches one must be dropped."""
    ex = ExplorerSubAgent(str(tmp_path))
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeProc("\n".join([
            str(tmp_path / "swe_review" / "cli.py"),
            str(tmp_path / "node_modules" / "pkg" / "index.js"),
            str(tmp_path / ".opencode" / "node_modules" / "zod" / "core.js"),
            str(tmp_path / "swe_review" / "skill.py"),
        ]))

    monkeypatch.setattr(explorer_agent.subprocess, "run", fake_run)
    related = ex._search_related_files(["review"], time_budget=4)

    argv = " ".join(captured["args"])
    assert "--exclude-dir" in argv and "node_modules" in argv
    assert related == ["swe_review/cli.py", "swe_review/skill.py"]


def test_related_files_cap_binds_within_one_keyword(tmp_path, monkeypatch):
    """Regression: the cap was only checked *between* keywords, so a single
    high-frequency keyword could return hundreds of paths."""
    ex = ExplorerSubAgent(str(tmp_path))
    many = "\n".join(str(tmp_path / "pkg" / f"m{i}.py") for i in range(200))
    monkeypatch.setattr(explorer_agent.subprocess, "run",
                        lambda args, **kw: _FakeProc(many))
    related = ex._search_related_files(["review", "work", "tree"], time_budget=6)
    assert len(related) == MAX_RELATED_FILES


# ---------------------------------------------------------------------------
# ExplorerSubAgent — test discovery
# ---------------------------------------------------------------------------

def _repo_with_separate_tests(tmp_path):
    (tmp_path / "swe_review").mkdir()
    (tmp_path / "swe_review" / "cli.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text("def test_x(): pass\n")
    (tmp_path / "tests" / "test_cli_verify.py").write_text("def test_y(): pass\n")
    return tmp_path


def test_find_test_files_searches_conventional_test_root(tmp_path):
    """Regression: co-located-only lookup returned [] for this very repository,
    whose tests live in `tests/` — so the reviewer never saw a test file."""
    ex = ExplorerSubAgent(str(_repo_with_separate_tests(tmp_path)))
    found = ex._find_test_files(["swe_review/cli.py"])
    assert "tests/test_cli.py" in found
    assert "tests/test_cli_verify.py" in found  # test_<stem>_<aspect>.py


def test_find_test_files_still_finds_colocated_tests(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "test_mod_a.py").write_text("def test_a(): pass\n")
    ex = ExplorerSubAgent(str(tmp_path))
    assert ex._find_test_files(["pkg/mod_a.py"]) == ["pkg/test_mod_a.py"]


def test_discovered_tests_are_read_into_context(tmp_path):
    """Test files must outrank grepped `related_files` for the read budget."""
    repo = _repo_with_separate_tests(tmp_path)
    res = asyncio.run(ExplorerSubAgent(str(repo)).execute({
        "repo_path": str(repo), "issue": "fix the cli", "pr_diff": "",
        "focus_files": ["swe_review/cli.py"], "max_steps": 4,
    })).to_dict()
    assert "tests/test_cli.py" in res["test_files"]
    assert "tests/test_cli.py" in res["file_contents"]
