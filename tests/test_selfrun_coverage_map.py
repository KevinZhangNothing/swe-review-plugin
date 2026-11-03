"""Acceptance tests for the explorer's changed-file → tests map (self-run)."""
import asyncio
from pathlib import Path

import pytest

from swe_review.subagents.explorer_agent import ExplorerSubAgent


def _touch(rel: str) -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,2 +1,3 @@\n"
        " def bar():\n"
        "     return 1\n"
        "+    # touched\n"
    )


def _run(repo, diff, issue="test coverage map", focus=None):
    return asyncio.run(ExplorerSubAgent(str(repo)).execute({
        "repo_path": str(repo),
        "issue": issue,
        "pr_diff": diff,
        "focus_files": focus or [],
    })).to_dict()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_name_convention_test_is_mapped(tmp_path):
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "tests" / "test_foo.py", "def test_x():\n    assert True\n")
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"] == {"pkg/foo.py": ["tests/test_foo.py"]}
    assert "tests/test_foo.py" in out["test_files"]


@pytest.mark.parametrize("import_line", [
    "import pkg.foo\n",
    "import pkg.foo as foo\n",
    "from pkg.foo import bar\n",
    "from pkg.foo import (bar, baz)\n",
    "from pkg import foo\n",
])
def test_importing_test_is_mapped(tmp_path, import_line):
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "tests" / "test_suite.py", import_line + "def test_x():\n    assert True\n")
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"]["pkg/foo.py"] == ["tests/test_suite.py"]


def test_unrelated_tests_are_not_mapped(tmp_path):
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "pkg" / "other.py", "def baz():\n    return 2\n")
    _write(tmp_path / "tests" / "test_other.py", "from pkg.other import baz\n")
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"] == {"pkg/foo.py": []}


def test_multiple_changed_files_map_independently(tmp_path):
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "pkg" / "baz.py", "def qux():\n    return 2\n")
    _write(tmp_path / "tests" / "test_foo.py", "def test_x():\n    assert True\n")
    _write(tmp_path / "tests" / "test_baz_extra.py", "def test_y():\n    assert True\n")
    diff = _touch("pkg/foo.py") + _touch("pkg/baz.py")
    out = _run(tmp_path, diff)
    assert out["test_coverage_map"] == {
        "pkg/foo.py": ["tests/test_foo.py"],
        "pkg/baz.py": ["tests/test_baz_extra.py"],
    }


def test_missing_files_do_not_raise(tmp_path):
    out = _run(tmp_path, _touch("pkg/does_not_exist.py"))
    assert out["test_coverage_map"] == {"pkg/does_not_exist.py": []}


# ---------------------------------------------------------------------------
# Regression tests for the review findings on the first implementation
# ---------------------------------------------------------------------------

def test_trailing_comment_does_not_break_import_detection(tmp_path):
    """A regex scanner turned "import pkg.foo  # noqa" into a bogus module
    string, so the test file was silently not mapped."""
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "tests" / "test_suite.py",
           "import pkg.foo  # noqa: F401\n")
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"]["pkg/foo.py"] == ["tests/test_suite.py"]


def test_import_text_inside_docstring_is_ignored(tmp_path):
    """Docstring prose that looks like an import is not an import."""
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "tests" / "test_suite.py",
           '"""\nfrom pkg.foo import bar\n"""\n\n\ndef test_x():\n    assert True\n')
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"] == {"pkg/foo.py": []}


def test_parenthesised_multiline_from_import_is_detected(tmp_path):
    """A line-based scanner only saw "from pkg import (" and missed pkg.foo."""
    _write(tmp_path / "pkg" / "foo.py", "def bar():\n    return 1\n")
    _write(tmp_path / "tests" / "test_suite.py",
           "from pkg import (\n    foo,\n)\n")
    out = _run(tmp_path, _touch("pkg/foo.py"))
    assert out["test_coverage_map"]["pkg/foo.py"] == ["tests/test_suite.py"]


def test_each_test_file_is_read_once(tmp_path, monkeypatch):
    """Regression: every changed file used to re-read the whole test tree
    (575 reads / 7.03 MB for a 25-file diff instead of 23 reads)."""
    for i in range(4):
        _write(tmp_path / "pkg" / f"m{i}.py", "def f():\n    return 1\n")
    for i in range(3):
        _write(tmp_path / "tests" / f"test_t{i}.py", "import pkg.m0\n")
    ex = ExplorerSubAgent(str(tmp_path))
    reads = {}
    real_read_text = Path.read_text

    def counting_read(self, *args, **kwargs):
        reads[self.name] = reads.get(self.name, 0) + 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    mapping = ex._map_test_coverage([f"pkg/m{i}.py" for i in range(4)])

    assert len(reads) == 3
    assert all(count == 1 for count in reads.values())
    assert mapping["pkg/m0.py"] == [
        "tests/test_t0.py", "tests/test_t1.py", "tests/test_t2.py"]


def test_non_python_source_gets_an_empty_entry(tmp_path):
    """Non-Python changes must not gain name-matched non-Python tests."""
    _write(tmp_path / "web" / "widget.js", "export const x = 1;\n")
    _write(tmp_path / "web" / "widget.test.js", "test('x', () => {});\n")
    out = _run(tmp_path, _touch("web/widget.js"))
    assert out["test_coverage_map"] == {"web/widget.js": []}


def test_no_modified_files_skips_the_test_scan(tmp_path, monkeypatch):
    _write(tmp_path / "tests" / "test_suite.py", "import pkg.foo\n")
    reads = []
    real_read_text = Path.read_text

    def counting_read(self, *args, **kwargs):
        reads.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    out = _run(tmp_path, "")
    assert out["test_coverage_map"] == {}
    assert reads == []
