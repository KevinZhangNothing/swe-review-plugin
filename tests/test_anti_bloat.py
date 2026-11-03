"""Regression tests for the Anti-Bloat / Patch-Stacking review capability.

The failure mode under test is "additive problem solving": a change that
resolves a local symptom by ADDING a bool, a flag, a guard, a compatibility
branch or a wrapper layer, instead of adjusting the logic that is already
there. Left undetected the codebase's complexity grows monotonically with every
change, which is the most expensive engineering debt there is.

Three surfaces are pinned here:
  1. `detect_bloat_signals` — the static evidence layer (deterministic, no LLM).
  2. Its wiring into `AnalyzerResult` and the reviewer's Patch Analysis, because
     evidence the reviewer never receives cannot influence a review.
  3. The prompt contracts — a rule the model is never told is a rule that does
     not exist.
"""
import asyncio

from swe_review.subagents import engineering_prompt
from swe_review.subagents.analyzer_agent import (
    AnalyzerSubAgent,
    detect_bloat_signals,
)
from swe_review.subagents.generator_agent import GeneratorSubAgent
from swe_review.subagents.reviser_agent import ReviserSubAgent
from swe_review.subagents.reviewer_agent import (
    ReviewerSubAgent,
    _normalize_complexity_delta,
)


def kinds(diff: str) -> set:
    return {s["kind"] for s in detect_bloat_signals(diff)}


# ---------------------------------------------------------------------------
# The five signals fire on representative bloat
# ---------------------------------------------------------------------------

#: The canonical "patch stacking" diff: one fix grows two booleans, several guards
#: AND two distinct compatibility mechanisms (legacy path + fallback path), while
#: the original branch is never rewritten.
STACKED_DIFF = """diff --git a/svc.py b/svc.py
@@ -1,3 +1,19 @@
 def run(order):
-    return charge(order)
+    self.is_retry = False
+    self.has_fallback = False
+    if order is None:
+        return None
+    if order.amount == 0:
+        return None
+    if not order.user:
+        self.is_retry = True
+        return None
+    if self.legacy_mode:
+        return legacy_charge(order)
+    if self.fallback_path:
+        return fallback_charge(order)
+    self.has_fallback = True
+    return charge(order)
"""


def test_flag_accumulation_detected():
    signals = {s["kind"]: s for s in detect_bloat_signals(STACKED_DIFF)}
    assert "flag_accumulation" in signals
    sig = signals["flag_accumulation"]
    # Two distinct booleans, each assigned twice — count is per assignment.
    assert sig["count"] >= 2
    assert "svc.py" in sig["files"]
    assert any("is_retry" in e for e in sig["evidence"])
    # The signal must tell the reviewer WHAT to check, not just that something happened.
    assert "推导" in sig["note"]


def test_guard_stacking_detected():
    signals = {s["kind"]: s for s in detect_bloat_signals(STACKED_DIFF)}
    assert "guard_stacking" in signals
    assert signals["guard_stacking"]["count"] >= 3


def test_compat_layer_detected():
    signals = {s["kind"]: s for s in detect_bloat_signals(STACKED_DIFF)}
    assert "compat_layer" in signals
    evidence = " ".join(signals["compat_layer"]["evidence"])
    assert "legacy" in evidence and "fallback" in evidence


def test_one_compat_concept_repeated_is_not_a_layer():
    """`compat_layer` means SEVERAL different mechanisms, not one repeated.

    Regression from this feature's own self-review: the detector reported itself
    because `_COMPAT_RE.search`, `compat.append` and `len(compat)` are five hits
    of a single concept. Requiring >=2 distinct mechanisms removed the
    self-report while keeping genuine dual-path compatibility detectable.
    """
    diff = (
        "diff --git a/s.py b/s.py\n@@ -1,3 +1,8 @@\n def run(o):\n"
        "-    return charge(o)\n"
        "+    x = self.legacy_mode\n"
        "+    y = legacy_charge(o)\n"
        "+    z = compute_legacy(o)\n"
        "+    return z\n"
    )
    assert "compat_layer" not in kinds(diff)


def test_additive_only_change_detected_on_existing_file():
    diff = ("diff --git a/x.py b/x.py\n@@ -1,2 +1,12 @@\n def f():\n"
            + "".join(f"+    step{i} = compute({i})\n" for i in range(10)))
    signals = {s["kind"]: s for s in detect_bloat_signals(diff)}
    assert "additive_only_change" in signals
    assert signals["additive_only_change"]["files"] == ["x.py"]


def test_wrapper_growth_detected_on_small_additive_change():
    diff = (
        "diff --git a/w.py b/w.py\n@@ -1,2 +1,8 @@\n def f(x):\n"
        "+class OrderHelper:\n"
        "+    def run(self, x):\n"
        "+        return compute(x)\n"
        "+    def run2(self, y):\n"
        "+        return compute(y)\n"
        "-    return x\n"
        "+    return OrderHelper().run(x)\n"
    )
    assert "wrapper_growth" in kinds(diff)


# ---------------------------------------------------------------------------
# Thresholds: normal engineering must NOT be reported
# ---------------------------------------------------------------------------
# Every guard below pins a deliberate design decision: the thresholds sit above
# ONE because a single necessary variable or guard is ordinary work. A detector
# that flags that is the low-value noise the review prompt explicitly forbids.

def test_single_flag_is_not_reported():
    diff = ("diff --git a/y.py b/y.py\n@@ -1,2 +1,4 @@\n def f():\n"
            "-    return 1\n+    self.is_ready = False\n+    return 1\n")
    assert "flag_accumulation" not in kinds(diff)


def test_two_guards_are_not_reported():
    # Threshold is 3: adding a null check plus one edge case is normal.
    diff = ("diff --git a/z.py b/z.py\n@@ -1,2 +1,5 @@\n def f(x):\n"
            "-    return x\n+    if x is None:\n+        return 0\n+    return x\n")
    assert "guard_stacking" not in kinds(diff)


def test_rewriting_the_existing_branch_is_not_guard_stacking():
    """Removing a conditional means the branch was REWRITTEN, not stacked."""
    diff = (
        "diff --git a/r.py b/r.py\n@@ -1,6 +1,8 @@\n def f(x):\n"
        "-    if x is None:\n"
        "-        return 0\n"
        "+    if not valid(x):\n"
        "+        return 0\n"
        "+    if x.expired:\n"
        "+        return 0\n"
        "+    if x.blocked:\n"
        "+        return 0\n"
        "     return compute(x)\n"
    )
    assert "guard_stacking" not in kinds(diff)


def test_new_file_additions_are_not_additive_only():
    """A new file has no deletions by definition — that is not patch stacking."""
    diff = ("diff --git a/new.py b/new.py\n@@ -0,0 +1,10 @@\n"
            + "".join(f"+v{i} = compute({i})\n" for i in range(10)))
    assert detect_bloat_signals(diff) == []


def test_balanced_change_is_clean():
    diff = (
        "diff --git a/b.py b/b.py\n@@ -1,4 +1,4 @@\n def f(x):\n"
        "-    return 1\n-    log(x)\n-    done()\n"
        "+    return compute(x)\n+    emit(x)\n"
    )
    assert detect_bloat_signals(diff) == []


def test_structural_lines_are_not_counted_as_logic():
    """A diff of only closers/comments must not look like new logic.

    Regression: the first version of `_STRUCTURAL_ONLY_RE` let its closer set
    match the empty string, so EVERY added line was classified as structural and
    `additive_only_change` could never fire. That silent disable is exactly the
    kind of failure a threshold test has to catch.
    """
    diff = ("diff --git a/s.py b/s.py\n@@ -1,2 +1,12 @@\n def f():\n"
            + "".join("+}\n+    # note\n" for _ in range(6)))
    assert "additive_only_change" not in kinds(diff)


def test_clean_diff_returns_empty_list():
    assert detect_bloat_signals("") == []
    assert detect_bloat_signals("   ") == []


# ---------------------------------------------------------------------------
# Prose must never be read as code
# ---------------------------------------------------------------------------
# All three cases below are real regressions found by running the detector
# against its own source: documenting this feature made it report that feature.

def test_comment_mentioning_compat_keywords_is_not_a_compat_layer():
    diff = (
        "diff --git a/m.py b/m.py\n@@ -1,2 +1,6 @@\n x = 1\n"
        "+# this fallback is only used for legacy clients\n"
        "+# workaround kept for backward compatibility\n"
        "+# deprecated graceful path\n"
        "+y = 2\n"
        "-x = 1\n+z = 3\n"
    )
    assert "compat_layer" not in kinds(diff)


def test_docstring_prose_is_not_scanned():
    """A docstring that lists the signals must not report those signals."""
    diff = (
        "diff --git a/d.py b/d.py\n@@ -1,2 +1,10 @@\n def f():\n"
        '+    """Signals:\n'
        "+    - ``compat_layer`` — fallback / legacy markers\n"
        "+    - ``guard_stacking`` — stacked guards\n"
        '+    """\n'
        "+    return 1\n"
        "-    return 0\n"
    )
    found = kinds(diff)
    assert "compat_layer" not in found
    assert "guard_stacking" not in found


def test_docstring_opened_by_context_line_is_tracked():
    """The opening delimiter often arrives as an unchanged context line.

    Regression: advancing literal state only on `+` lines desynchronised it, so
    prose further down the docstring was matched as code.
    """
    diff = (
        "diff --git a/d.py b/d.py\n@@ -1,4 +1,6 @@\n def f():\n"
        '     """Existing docstring.\n'
        "     More prose describing the fallback and legacy behaviour.\n"
        "     \"\"\"\n"
        "+    return fallback_path(1)\n"
        "-    return 0\n"
    )
    # The prose lines are context (never added), and the single added line is
    # one occurrence — below the compat threshold of 2 either way.
    assert "compat_layer" not in kinds(diff)


def test_markdown_and_config_files_are_not_bloat_judged():
    """Docs/config grow by addition by nature; that is not code bloat."""
    for path in ("README.md", "docs/guide.md", "pyproject.toml", "conf.yaml"):
        diff = (f"diff --git a/{path} b/{path}\n@@ -1,1 +1,12 @@\n x\n"
                + "".join(f"+line {i} describing the fallback behaviour\n"
                          for i in range(10)))
        assert detect_bloat_signals(diff) == [], f"{path} should not be judged"


def test_symbol_definition_is_not_a_compat_layer():
    """Naming the concept is not using it."""
    diff = (
        "diff --git a/r.py b/r.py\n@@ -1,2 +1,5 @@\n import re\n"
        "+_COMPAT_RE = re.compile(r\"legacy|fallback\")\n"
        "+_LEGACY_RE = re.compile(r\"workaround\")\n"
        "-x = 1\n+y = 2\n"
    )
    assert "compat_layer" not in kinds(diff)


def test_real_compat_usage_still_detected_after_prose_filter():
    """The filter must not swallow genuine compat layers."""
    diff = (
        "diff --git a/s.py b/s.py\n@@ -1,3 +1,9 @@\n def run(o):\n"
        "-    return charge(o)\n"
        "+    if self.legacy_mode:\n"
        "+        return legacy_charge(o)\n"
        "+    if self.fallback_path:\n"
        "+        return fallback_charge(o)\n"
        "+    return charge(o)\n"
    )
    assert "compat_layer" in kinds(diff)


# ---------------------------------------------------------------------------
# Precision defects found by running the plugin's OWN review on this change
# ---------------------------------------------------------------------------
# A self-review (Generate-Review-Revise-Verify with `--tool host`) flagged these
# as P1/P2. Each was a real defect: the detector silently missed, or falsely
# reported, the exact patterns the feature exists to catch.

def test_local_boolean_flag_is_detected():
    """P1: a local `is_ready = False` was skipped as a "symbol definition".

    `_SYMBOL_DEF_RE` matches any bare assignment, and it was consulted BEFORE the
    flag pattern — so only `self.`-prefixed flags were ever counted. Function-local
    flags are the most common form of the "add a bool to control it" pattern.
    """
    diff = (
        "diff --git a/m.py b/m.py\n@@ -1,2 +1,8 @@\n def f(x):\n"
        "+    is_ready = False\n"
        "+    has_result = False\n"
        "+    if x:\n"
        "+        is_ready = True\n"
        "+    if x > 1:\n"
        "+        has_result = True\n"
        "     return x\n"
    )
    assert "flag_accumulation" in kinds(diff)


def test_comment_only_addition_is_not_additive_only():
    """P1: appending 10 comment lines reported `additive_only_change`.

    Comments are prose. They add no logic, so a documentation pass must not read
    as "this change only adds code instead of adjusting the existing logic".
    """
    diff = ("diff --git a/m.py b/m.py\n@@ -1,2 +1,12 @@\n def f(x):\n"
            + "".join(f"+# comment line {i}\n" for i in range(10)))
    assert "additive_only_change" not in kinds(diff)


def test_new_self_contained_function_is_not_guard_stacking():
    """P2: adding one self-contained helper reported guard_stacking.

    Guards inside a function the change CREATES are that function's own logic.
    Only guards layered onto EXISTING code indicate stacking.
    """
    diff = (
        "diff --git a/m.py b/m.py\n@@ -1,2 +1,9 @@\n def f(x):\n"
        "+def helper(a, b, c):\n"
        "+    if a:\n"
        "+        return 1\n"
        "+    if b:\n"
        "+        return 2\n"
        "+    if c:\n"
        "+        return 3\n"
        "+    return 0\n"
        "     return x\n"
    )
    assert "guard_stacking" not in kinds(diff)


def test_guards_stacked_onto_existing_function_still_detected():
    """The new-def exclusion must not swallow genuine stacking."""
    diff = (
        "diff --git a/svc.py b/svc.py\n@@ -1,3 +1,12 @@\n def run(order):\n"
        "-    return charge(order)\n"
        "+    if order is None:\n"
        "+        return None\n"
        "+    if order.amount == 0:\n"
        "+        return None\n"
        "+    if not order.user:\n"
        "+        return None\n"
        "+    return charge(order)\n"
    )
    assert "guard_stacking" in kinds(diff)


def test_test_files_are_not_bloat_judged():
    """A test named `test_compat_layer_detected` asserts ABOUT a concept.

    Found by this feature's own self-review: the test module's function names and
    assertion strings were matched as real compat layers, inflating the signal.
    Test files are legitimately additive and branch-heavy, so they are excluded
    wholesale — reusing `_TEST_PATH_RE`, the same predicate the analyzer already
    uses to keep test definitions out of the public-API term.
    """
    for path in ("tests/test_anti_bloat.py", "src/foo_test.py",
                 "spec/charge.spec.ts", "pkg/tests/deep/test_x.py"):
        diff = (
            f"diff --git a/{path} b/{path}\n@@ -1,2 +1,10 @@\n def f():\n"
            "+def test_compat_layer_detected():\n"
            "+    if legacy_mode:\n"
            "+        return fallback_path(1)\n"
            "+    if backward_compat:\n"
            "+        return workaround(2)\n"
            "+    assert is_flag\n"
            "-    pass\n+x = 1\n"
        )
        assert detect_bloat_signals(diff) == [], f"{path} should not be judged"


def test_created_def_scope_survives_a_blank_line():
    """A top-level `def` sits at indent 0, so the blank line right after it would
    otherwise compare equal and immediately close the new-def scope.

    Regression: the scope tracker reset on the blank line, so every guard in the
    new function was counted as stacking onto existing code.
    """
    diff = (
        "diff --git a/n.py b/n.py\n@@ -1,2 +1,12 @@\n def f(x):\n"
        "+def helper(a, b, c):\n"
        "+\n"
        "+    body = prepare(a)\n"
        "+    if b:\n"
        "+        return 1\n"
        "+    if c:\n"
        "+        return 2\n"
        "+    if body:\n"
        "+        return 3\n"
        "+    return 0\n"
        "     return x\n"
    )
    assert "guard_stacking" not in kinds(diff)


# ---------------------------------------------------------------------------
# Wiring: the evidence must actually reach the reviewer
# ---------------------------------------------------------------------------

def test_analyzer_result_exposes_bloat_signals():
    result = asyncio.run(AnalyzerSubAgent().execute({"pr_diff": STACKED_DIFF}))
    assert result.bloat_signals
    assert "bloat_signals" in result.to_dict()


def test_reviewer_patch_analysis_carries_bloat_signals():
    """Patch Analysis is how the reviewer sees static evidence — it must ship."""
    sub = ReviewerSubAgent(tool_adapter=None)
    analysis = sub._analyze_patch(STACKED_DIFF, {"files_modified": ["svc.py"]})
    assert analysis["bloat_signals"]
    assert any(s["kind"] == "flag_accumulation"
               for s in analysis["bloat_signals"])


def test_reviewer_prompt_includes_bloat_analysis():
    """End-to-end: the static signal text must be in the user prompt."""
    captured = {}

    class CaptureAdapter:
        name = "capture"

        async def chat(self, system, user, max_tokens=4096, temperature=0.1):
            captured["user"] = user
            captured["system"] = system
            return ('{"decision":"approve","confidence":0.9,'
                    '"summary":{},"findings":[],"scores":{},'
                    '"total_score":null,"hard_gate":{"triggered":false}}'), {}

    sub = ReviewerSubAgent(tool_adapter=CaptureAdapter(), prompt_style="engineering")
    asyncio.run(sub.execute({
        "issue": "charge fails for guest orders",
        "pr_title": "fix",
        "pr_diff": STACKED_DIFF,
    }))
    assert "bloat_signals" in captured["user"]
    assert "flag_accumulation" in captured["user"]


# ---------------------------------------------------------------------------
# Prompt contracts
# ---------------------------------------------------------------------------

def test_engineering_prompt_declares_anti_bloat_check():
    sp = engineering_prompt.system_prompt(["Python"])
    for marker in (
        "Anti-Bloat / Patch-Stacking Deep Check",
        "Flag 堆积",
        "分支叠加",
        "只加不删",
        "兼容层滥用",
        "包装层增殖",
        "修改策略优先级",
        "克制（Restraint）",
    ):
        assert marker in sp, f"missing from system prompt: {marker}"


def test_engineering_prompt_requires_quantitative_self_report():
    """The four questions the reviewer must answer after judging a change."""
    sp = engineering_prompt.system_prompt(["Python"])
    assert "complexity_delta" in sp
    for field in ("new_state_vars", "new_functions", "derivable_state",
                  "removable_old_code", "net_complexity"):
        assert field in sp, f"missing complexity_delta field: {field}"


def test_engineering_prompt_names_the_signal_kinds():
    """The prompt must reference the exact signal names Patch Analysis emits."""
    sp = engineering_prompt.system_prompt(["Python"])
    for kind in ("additive_only_change", "flag_accumulation", "guard_stacking",
                 "compat_layer", "wrapper_growth"):
        assert kind in sp, f"prompt does not reference signal: {kind}"


def test_engineering_prompt_keeps_restraint_balanced():
    """Anti-bloat must not become 'shortest diff wins'."""
    sp = engineering_prompt.system_prompt(["Python"])
    assert "可读性、正确性、类型安全" in sp
    assert "不得仅因" in sp  # one necessary variable is NOT a finding


def test_reviser_prompt_prefers_subtraction():
    skill = ReviserSubAgent(tool_adapter=None, prompt_style="detailed")
    # The detailed prompt is what engineering reviews route to.
    from swe_review.subagents.reviser_agent import _system_prompt_detailed
    sp = _system_prompt_detailed()
    assert "Anti-Bloat Rules" in sp
    assert "DELETE" in sp
    assert "adjust the existing condition instead" in sp
    assert skill.prompt_style == "detailed"


def test_generator_prompt_has_anti_bloat_rules_for_every_perspective():
    sub = GeneratorSubAgent(tool_adapter=None)
    for perspective in (None, "minimal", "alternative", "constraint_aware"):
        sp = sub._system_prompt(perspective=perspective)
        assert "Anti-Bloat Rules" in sp, f"missing for perspective={perspective}"


def test_finding_carries_constraint_category_into_defect():
    """A reviser that cannot see WHY a finding was filed stacks more code."""
    from swe_review.subagents.reviewer_agent import _parse_engineering_payload
    report = _parse_engineering_payload({
        "decision": "REQUEST_CHANGES", "confidence": 0.8,
        "findings": [{
            "severity": "P1", "title": "redundant flag",
            "location": "a.py:3", "observation": "o", "evidence": "e",
            "constraint_category": "simplicity_overengineering",
        }],
    }, None)
    defect = report.defects[0]
    assert "simplicity_overengineering" in defect.description


# ---------------------------------------------------------------------------
# complexity_delta parsing
# ---------------------------------------------------------------------------

def test_complexity_delta_parsed_and_normalized():
    delta = _normalize_complexity_delta({
        "new_state_vars": 2, "new_functions": 1,
        "derivable_state": "is_retry",
        "removable_old_code": "legacy_charge",
        "net_complexity": "increased",
    })
    assert delta == {
        "new_state_vars": 2, "new_functions": 1,
        "derivable_state": "is_retry",
        "removable_old_code": "legacy_charge",
        "net_complexity": "increased",
    }


def test_complexity_delta_absent_stays_empty():
    """Absence must stay distinguishable from an honest 'neutral'."""
    assert _normalize_complexity_delta(None) == {}
    assert _normalize_complexity_delta("nonsense") == {}


def test_complexity_delta_rejects_bogus_values():
    delta = _normalize_complexity_delta({
        "new_state_vars": -3,           # negative count is not a count
        "new_functions": True,          # bool is an int subclass — reject
        "net_complexity": "much_worse",  # not in the enum
    })
    assert delta["new_state_vars"] == 0
    assert delta["new_functions"] == 0
    assert delta["net_complexity"] == ""


def test_complexity_delta_reaches_serialized_report():
    from swe_review.subagents.reviewer_agent import _parse_engineering_payload
    report = _parse_engineering_payload({
        "decision": "approve", "confidence": 0.9, "findings": [],
        "scores": {dim: {"score": mx, "reason": "ok"}
                   for dim, mx in (("design_quality", 20), ("maintainability", 15),
                                   ("consistency", 15), ("simplicity", 10),
                                   ("readability", 10), ("testability", 10),
                                   ("risk", 10), ("change_scope", 10))},
        "complexity_delta": {"new_state_vars": 1, "net_complexity": "neutral"},
    }, None)
    assert report.to_dict()["complexity_delta"]["new_state_vars"] == 1


def test_generated_mirror_is_not_reported_as_duplication():
    """A committed mirror repeats every line by design.

    Found by this feature's own self-review: all 10 repeated blocks were
    `.claude/skills/…/SKILL.md` vs its `swe_review/.claude_skills/` copy produced
    by `scripts/sync_skill_sources.py`. That is the mirror working, not
    copy-pasted logic.
    """
    from swe_review.subagents.analyzer_agent import detect_repeated_added_blocks

    body = ("+    total = price * quantity * (1 - discount)\n"
            "+    logger.info('computed total for order %s', order_id)\n")
    mirror = (
        "diff --git a/.claude/skills/x/SKILL.md b/.claude/skills/x/SKILL.md\n"
        "@@ -1,1 +1,3 @@\n x\n" + body +
        "diff --git a/swe_review/.claude_skills/x/SKILL.md "
        "b/swe_review/.claude_skills/x/SKILL.md\n"
        "@@ -1,1 +1,3 @@\n x\n" + body
    )
    assert detect_repeated_added_blocks(mirror) == []


def test_real_cross_file_duplication_still_detected():
    """The mirror exclusion must not swallow genuine copy-paste."""
    from swe_review.subagents.analyzer_agent import detect_repeated_added_blocks

    line = "+    total = price * quantity * (1 - discount)\n"
    diff = (
        "diff --git a/a.py b/a.py\n@@ -1,1 +1,2 @@\n x\n" + line +
        "diff --git b/b.py b/b.py\n@@ -1,1 +1,2 @@\n y\n" + line
    )
    blocks = detect_repeated_added_blocks(diff)
    assert blocks and blocks[0]["count"] >= 2


def test_additive_only_detected_across_multiple_files():
    """Additions split across files are still "add without adjusting".

    Found by the plugin's own review loop as a P3: the judge was per-file, so 28
    added lines spread over 4 existing files (7 each) slipped under the per-file
    threshold of 8 while the change as a whole deleted nothing.
    """
    diff = ""
    for name in ("a", "b", "c", "d"):
        diff += (f"diff --git a/{name}.py b/{name}.py\n"
                 f"@@ -1,1 +1,8 @@\n def f():\n")
        diff += "".join(f"+    step{i} = compute({i})\n" for i in range(7))
    signals = {s["kind"]: s for s in detect_bloat_signals(diff)}
    assert "additive_only_change" in signals
    assert signals["additive_only_change"]["count"] == 28
    assert len(signals["additive_only_change"]["files"]) == 4


def test_small_additive_change_is_not_reported():
    """Two added lines are ordinary work, not a stacking pattern."""
    diff = ("diff --git a/s.py b/s.py\n@@ -1,1 +1,3 @@\n def f():\n"
            "+    a = 1\n"
            "+    b = 2\n")
    assert "additive_only_change" not in kinds(diff)
