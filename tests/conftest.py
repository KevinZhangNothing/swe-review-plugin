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
