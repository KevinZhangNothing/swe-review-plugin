"""Shared pytest fixtures for swe-review.

Imports rely on `pip install -e .` having placed the `swe_review` package
on the Python path; no explicit sys.path manipulation is needed.
"""

import pytest


@pytest.fixture()
def sample_diff() -> str:
    return (
        "diff --git a/foo.py b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    if a is None or b is None:\n"
        "+        return 0\n"
        "+    return a + b\n"
    )


#: Minimal *applicable* patch (note the ---/+++ headers `sample_diff` lacks):
#: it turns a one-line `hello.py` from `value = 1` into `value = 2`. Shared by
#: every test that needs a real `git apply`-able diff rather than a diff-shaped
#: string, so the canonical fixture cannot drift between test modules.
MINIMAL_DIFF = """--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


@pytest.fixture()
def minimal_diff() -> str:
    return MINIMAL_DIFF


@pytest.fixture()
def repo_with_hello(tmp_path):
    """Throwaway repo dir holding `hello.py` with `value = 1` (patch target)."""
    (tmp_path / "hello.py").write_text("value = 1\n")
    return tmp_path
