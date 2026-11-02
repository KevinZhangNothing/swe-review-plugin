"""Shared pytest fixtures for swe-review.

Imports rely on `pip install -e .` having placed the `swe_review` package
on the Python path; no explicit sys.path manipulation is needed.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_detection_cache():
    """Reset the memoized CLI-path map around every test.

    `swe_review.tools._resolved_cli_paths()` caches for the process lifetime
    (adapter construction touches the filesystem). Without this, a test that
    monkeypatches `build_adapter` would poison the cache for every later test —
    which is exactly what happened before this fixture existed.
    """
    from swe_review.tools import _reset_detection_cache

    _reset_detection_cache()
    yield
    _reset_detection_cache()


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


# ---------------------------------------------------------------------------
# Shared loop-skill stubs
# ---------------------------------------------------------------------------
# `LoopSubAgent` accepts any object exposing `execute(**kwargs)` returning a
# `SkillResult`-shaped dict, so regression tests drive it with these instead of a
# real LLM. They live here because two test modules needed the same four shapes;
# when a stub's payload contract changes, it must change in ONE place.

#: A diff that `git apply` (and `validate_unified_diff`) accepts.
SEED_DIFF = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"

USAGE_GEN = {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
USAGE_REVIEW = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
USAGE_REVISE = {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15}


#: Sentinel meaning "use the default exploration payload". A plain `None`
#: default cannot express "`explore()` returns None", which is a case tests need.
_DEFAULT_EXPLORATION = object()


class StubGenerator:
    """Generator skill stub: records calls, returns a valid diff."""

    def __init__(self, diff: str = SEED_DIFF, usage=USAGE_GEN,
                 exploration=_DEFAULT_EXPLORATION):
        self.diff = diff
        self.usage = usage
        #: What `explore()` returns — lets a test drive best_of_n's shared
        #: exploration with a real payload, `{}`, or `None`.
        self._exploration = (
            {"files_modified": ["x"], "steps": 1}
            if exploration is _DEFAULT_EXPLORATION else exploration)
        self.calls = 0

    async def explore(self, issue, repo_path=None):
        return self._exploration

    async def execute(self, **kwargs):
        self.calls += 1
        return {"ok": True, "payload": {
            "title": "t", "body": "", "diff": self.diff,
            "rationale": "r", "confidence": 0.5, "token_usage": self.usage}}


class StubReview:
    """Review skill stub: records the kwargs it was called with."""

    def __init__(self, decision: str = "request_changes", usage=USAGE_REVIEW,
                 defects=None):
        self.decision = decision
        self.usage = usage
        self.defects = defects if defects is not None else [{"description": "x"}]
        self.kwargs_seen: list = []

    async def execute(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        return {"ok": True, "payload": {
            "decision": self.decision, "confidence": 0.5,
            "defects": self.defects, "findings": [],
            "token_usage": self.usage}}


class StubRevise:
    """Reviser skill stub: counts calls, returns a valid diff."""

    def __init__(self, diff: str = SEED_DIFF, usage=USAGE_REVISE,
                 status: str = "success"):
        self.diff = diff
        self.usage = usage
        self.status = status
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return {"ok": True, "payload": {
            "title": "t", "body": "", "diff": self.diff,
            "changes_summary": "s", "status": self.status,
            "token_usage": self.usage}}


class StubVerify:
    """Verifier skill stub: counts calls, passes the patch."""

    def __init__(self, passed: bool = True, resolution_status: str = "unknown"):
        self.passed = passed
        self.resolution_status = resolution_status
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return {"ok": True, "payload": {
            "passed": self.passed, "patch_applied": True,
            "resolution_status": self.resolution_status,
            "confidence": 0.5, "details": "stub"}}


class StubAdapter:
    """Adapter stub returning a fixed JSON payload without calling any LLM.

    Four near-identical versions of this existed across the regression tests;
    they differ only in `name` and the payload body, so they share one shape
    here. Subclass and override `payload()` for a different report.

    The constructor parameter is `adapter_name`, not `name`: a parameter sharing
    the class attribute's name reads as shadowing (`self.name = name` inside
    `__init__`), which invites the reader to hunt for a bug that is not there.
    """

    name = "stub-adapter"

    def __init__(self, adapter_name: str | None = None,
                 payload: dict | None = None):
        if adapter_name is not None:
            self.name = adapter_name
        self._payload = payload

    def payload(self) -> dict:
        return self._payload if self._payload is not None else {
            "decision": "approve", "confidence": 0.9,
            "summary": {"overall_assessment": "stub"},
            "scores": {}, "findings": [], "defects": [],
        }

    async def chat(self, system, user, max_tokens: int = 4096,
                   temperature: float = 0.1):
        import json
        return json.dumps(self.payload()), {"total_tokens": 10}
