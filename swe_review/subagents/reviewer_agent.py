"""
Reviewer SubAgent — 审查 SubAgent

负责执行代码审查，生成结构化审查报告。

Hard rule: reviewer 不得接收 golden_patch / hidden tests / oracle —— 这些信息会
污染判断。Verifier 可以接收 oracle（仅做评测指标聚合），但永远不进 review 链路。

Output schemas (two flavors selectable at serialization time):
  - to_dict() (default)        — flat  : {decision: "approve|request_changes",
                                            confidence: float, summary{}, defects[]}
  - to_dict(deep=True)         — nested: {decision: {recommendation, confidence},
                                            ...} with defects[].location as
                                    {path, start_line, end_line}
  The nested form is convenient for downstream automated SFT / evaluation pipelines.

Prompt styles:
  - "engineering" (default) — senior code-review agent: judges design quality,
                            maintainability, consistency, simplicity, readability,
                            testability, risk and change scope (8-dimension 100-point
                            scoring + P0–P4 evidence-bound findings + 4-level decision
                            APPROVE / APPROVE_WITH_SUGGESTIONS / REQUEST_CHANGES / BLOCK).
                            Review target is the CHANGE, not the bug.
  - "concise"             — legacy bug-fix-centric review, practical daily use.
  - "detailed"            — legacy bug-fix-centric: Step 1→6 workflow + root-cause
                            tracing + symptom-fix detection 4 rules (经验法则).
"""

import json
import asyncio
import re
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime

from . import engineering_prompt
from .analyzer_agent import detect_repeated_added_blocks
from .diff_sharding import (
    DEFAULT_SHARD_BUDGET_CHARS,
    DEFAULT_SHARD_CONCURRENCY,
    DiffShard,
    plan_shards,
    should_shard,
)
from .engineering_prompt import parse_diff_files, strip_fences, truncate_json_text
from ..tools.host_adapter import HostTurnRequired


# Defect severity & category enumerations
DEFECT_SEVERITIES = ("high", "medium", "low")
DEFECT_CATEGORIES = (
    "correctness", "compatibility", "security",
    "performance", "maintainability",
)

# Engineering-style findings use P0-P4 severity (see Severity 定义 in the
# engineering system prompt). Mapped onto legacy defect severities so that
# downstream consumers (reviser / loop) keep working unchanged.
FINDING_SEVERITIES = ("P0", "P1", "P2", "P3", "P4")
FINDING_TO_DEFECT_SEVERITY = {
    "P0": "high", "P1": "high", "P2": "medium", "P3": "low", "P4": "low",
}

# Decision vocabulary. Legacy styles emit approve/request_changes; engineering
# style additionally distinguishes approve_with_suggestions and block.
DECISION_APPROVING = ("approve", "approve_with_suggestions")
DECISION_REJECTING = ("request_changes", "block")
DECISION_CHOICES = DECISION_APPROVING + DECISION_REJECTING

# Engineering scoring dimensions and their max points (total = 100).
SCORE_DIMENSIONS = {
    "design_quality": 20,
    "maintainability": 15,
    "consistency": 15,
    "simplicity": 10,
    "readability": 10,
    "testability": 10,
    "risk": 10,
    "change_scope": 10,
}


@dataclass
class Defect:
    """缺陷数据结构

    flat schema (default): location is a string "path:line".
    deep schema: location is {"path", "start_line", "end_line"}.
    Both rendered via `to_dict(deep=...)` on ReviewReport.
    """
    severity: str  # "high", "medium", "low"
    description: str
    location: Any  # str (flat) | {"path", "start_line", "end_line"} (deep)
    suggestion: str
    category: str = "correctness"


@dataclass
class Finding:
    """Engineering-style review finding (P0–P4, evidence-bound).

    Fields mirror the review-framework requirement:
    Location / Observation / Why It Matters / Evidence / Recommendation /
    Severity / Confidence.
    """
    severity: str            # "P0".."P4"
    title: str
    location: Any            # str (flat) | {"path", "start_line", "end_line"} (deep)
    observation: str = ""
    why_it_matters: str = ""
    evidence: str = ""
    recommendation: str = ""
    confidence: str = "medium"  # "high" | "medium" | "low"
    # Optional SWE-Gate review-constraint category tag (e.g. "scope_generalization");
    # empty string when the finding maps to no constraint category.
    constraint_category: str = ""

    def to_dict(self, deep: bool = False) -> Dict[str, Any]:
        loc = (_normalize_location_deep(self.location) if deep
               else _normalize_location_flat(self.location))
        return {
            "severity": self.severity,
            "title": self.title,
            "location": loc,
            "observation": self.observation,
            "why_it_matters": self.why_it_matters,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "constraint_category": self.constraint_category,
        }

    def to_defect(self) -> "Defect":
        """Map onto the legacy Defect shape for downstream revise/loop consumers.

        Carries why_it_matters / evidence into the description so the reviser
        still receives evidence-rich feedback (the whole point of routing
        engineering reviews to the detailed revision prompt).
        """
        description = self.title
        if self.observation:
            description += f" — {self.observation}"
        if self.why_it_matters:
            description += f" [impact: {self.why_it_matters}]"
        if self.evidence:
            description += f" [evidence: {self.evidence}]"
        return Defect(
            severity=FINDING_TO_DEFECT_SEVERITY.get(self.severity, "medium"),
            description=description,
            location=self.location,
            suggestion=self.recommendation,
            category="maintainability" if self.severity in ("P2", "P3", "P4") else "correctness",
        )


# Strictness ordering used when shard verdicts must be reconciled deterministically.
DECISION_STRICTNESS = {d: i for i, d in enumerate(DECISION_CHOICES)}


def _finding_location_parts(location: Any) -> Tuple[str, Optional[int], Optional[int]]:
    """`(path, start_line, end_line)` from either location shape."""
    if isinstance(location, dict):
        s, e = location.get("start_line"), location.get("end_line")
        return (str(location.get("path") or ""),
                s if isinstance(s, int) else None,
                e if isinstance(e, int) else None)
    if isinstance(location, str):
        m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", location.strip())
        if m:
            start = int(m.group(2))
            return (m.group(1), start, int(m.group(3)) if m.group(3) else start)
        return (location.strip(), None, None)
    return ("", None, None)


def merge_findings(groups: List[List["Finding"]]) -> List["Finding"]:
    """Deterministically union the findings reported by shard reviews.

    Deterministic on purpose: the shards are the only place the evidence exists,
    so a model must never be the thing that decides whether a finding survives.
    Deduped on (path, line range, severity, normalised title) so two shards
    reporting the same cross-cutting issue collapse into one, then ordered by
    severity and location for a stable, diffable report.
    """
    order = {sev: i for i, sev in enumerate(FINDING_SEVERITIES)}
    seen = set()
    merged: List[Finding] = []
    for group in groups or []:
        for f in group or []:
            path, start, end = _finding_location_parts(getattr(f, "location", ""))
            key = (path, start, end, getattr(f, "severity", ""),
                   re.sub(r"\s+", " ", (getattr(f, "title", "") or "").strip().lower())[:70])
            if key in seen:
                continue
            seen.add(key)
            merged.append(f)

    def sort_key(f: Finding):
        path, start, _ = _finding_location_parts(getattr(f, "location", ""))
        return (order.get(getattr(f, "severity", ""), len(FINDING_SEVERITIES)),
                path, start or 0)

    merged.sort(key=sort_key)
    return merged


@dataclass
class ReviewReport:
    """审查报告数据结构"""
    decision: str  # "approve" | "approve_with_suggestions" | "request_changes" | "block"
    confidence: float
    summary: Dict[str, str] = field(default_factory=dict)
    defects: List[Defect] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)       # engineering style
    scores: Dict[str, Any] = field(default_factory=dict)        # engineering style
    total_score: Optional[float] = None                         # engineering style
    hard_gate: Dict[str, Any] = field(default_factory=dict)     # engineering style
    whats_good: List[str] = field(default_factory=list)          # engineering style (optional)
    recommended_actions: List[str] = field(default_factory=list)  # engineering style (optional)
    parse_error: Optional[str] = None  # set when the model output was not parseable
    truncated_repair: bool = False  # JSON only parsed via truncation repair (tail lost)
    raw_response: str = ""
    timestamp: str = ""
    token_usage: Optional[Dict[str, int]] = None
    exploration_steps: int = 0
    # Set when the explorer hit MAX_RELATED_FILES / max_file_bytes and the
    # reviewer saw a truncated evidence base — loop logs use it as the
    # zero-cost proxy for evidence_missing failures.
    exploration_truncated: bool = False
    prompt_style: str = "engineering"  # "engineering" | "concise" | "detailed"

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    # ----------------------------------------------------------------
    # Serialization
    # ----------------------------------------------------------------
    def to_dict(self, deep: bool = False) -> Dict[str, Any]:
        """Serialize the report.

        deep=False (default): flat shape — easy to read in normal use.
        deep=True: nested `decision:{recommendation, confidence}` shape with
                   defect locations as {path, start_line, end_line}.
        """
        if deep:
            decision = {
                "recommendation": self.decision,
                "confidence": self.confidence,
            }
            defects_payload = []
            for d in self.defects:
                defects_payload.append({
                    "severity": d.severity,
                    "category": d.category,
                    "location": _normalize_location_deep(d.location),
                    "description": d.description,
                    "suggestion": d.suggestion,
                })
        else:
            decision = self.decision
            defects_payload = [
                {
                    "severity": d.severity,
                    "description": d.description,
                    "location": _normalize_location_flat(d.location),
                    "suggestion": d.suggestion,
                }
                for d in self.defects
            ]

        findings_payload = [f.to_dict(deep=deep) for f in self.findings]

        return {
            "decision": decision,
            "confidence": self.confidence,
            "summary": self.summary,
            "defects": defects_payload,
            "findings": findings_payload,
            "scores": self.scores,
            "total_score": self.total_score,
            "hard_gate": self.hard_gate,
            "whats_good": self.whats_good,
            "recommended_actions": self.recommended_actions,
            "parse_error": self.parse_error,
            "truncated_repair": self.truncated_repair,
            "timestamp": self.timestamp,
            "token_usage": self.token_usage,
            "exploration_steps": self.exploration_steps,
            "exploration_truncated": self.exploration_truncated,
            "prompt_style": self.prompt_style,
        }

    def to_json(self, indent: int = 2, deep: bool = False) -> str:
        return json.dumps(self.to_dict(deep=deep), indent=indent, ensure_ascii=False)


def _normalize_location_flat(loc: Any) -> str:
    """Always return a "path:line" string for flat consumers."""
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        path = loc.get("path", "")
        sl = loc.get("start_line")
        el = loc.get("end_line")
        if sl is not None and el is not None and sl == el:
            return f"{path}:{sl}"
        if sl is not None:
            return f"{path}:{sl}" + (f"-{el}" if el is not None and el != sl else "")
    return str(loc)


def _normalize_location_deep(loc: Any) -> Dict[str, Any]:
    """Always return {path, start_line, end_line, function?, ...grounding} for nested-schema consumers."""
    if isinstance(loc, dict):
        out = {
            "path": loc.get("path", ""),
            "start_line": loc.get("start_line"),
            "end_line": loc.get("end_line"),
            "function": loc.get("function"),
        }
        # Preserve deterministic grounding annotations (verified /
        # relocated_from / line_verified / file_lines) added by
        # location_grounding.ground_report_locations.
        for k in ("verified", "relocated_from", "line_verified", "file_lines"):
            if k in loc:
                out[k] = loc[k]
        return out
    if isinstance(loc, str):
        # Parse "path:line" or "path:line-line"
        m = re.match(r"^([^:]+):(\d+)(?:-(\d+))?$", loc)
        if m:
            return {
                "path": m.group(1),
                "start_line": int(m.group(2)),
                "end_line": int(m.group(3)) if m.group(3) else int(m.group(2)),
                "function": None,
            }
        return {"path": loc, "start_line": None, "end_line": None, "function": None}
    return {"path": str(loc), "start_line": None, "end_line": None, "function": None}


# ============================================================================
# Prompts
# ============================================================================

PROMPT_STYLE_CHOICES = ("engineering", "concise", "detailed")


def _system_prompt_concise() -> str:
    return (
        "You are an expert code reviewer for software engineering issue resolution. "
        "Your task is to inspect the repository, the issue, and a candidate PR, then "
        "produce a STRICT JSON report. Output ONLY valid JSON — no markdown fences, "
        "no prose before/after.\n\n"
        "## Output Schema (FLAT)\n"
        "{\n"
        '  "decision": "approve" | "request_changes",\n'
        '  "confidence": float (0.0-1.0),\n'
        '  "summary": {\n'
        '    "problem": "Root cause you identified (file + function + what is wrong)",\n'
        '    "solution": "What the candidate patch actually does",\n'
        '    "overall_assessment": "1-2 sentence judgment"\n'
        "  },\n"
        '  "defects": [\n'
        '    {"severity":"high|medium|low", "category":"correctness|compatibility|security|performance|maintainability",\n'
        '     "description":"self-contained; dev can understand the bug from this alone",\n'
        '     "location":"path:line (repo-relative)",\n'
        '     "suggestion":"what to do instead — point at direction, never copy a known fix"}\n'
        "  ]\n"
        "}\n\n"
        "## Decision Rules\n"
        "- approve  →  patch resolves the root cause for the issue; no regressions.\n"
        "- request_changes  →  at least one high/medium defect, OR the patch only suppresses a symptom.\n"
        "- confidence: 0.85-0.95 when approve; 0.70-0.95 for request_changes.\n"
        "- When approve: defects may be empty [] OR only low-severity items.\n"
        "- When request_changes: defects must contain ≥1 high/medium entry.\n\n"
        "## Critical Constraints\n"
        "- You do NOT see the ground-truth fix or hidden tests — judge purely from issue, "
        "diff, and repository context.\n"
        "- For nonlocal bugs: trace the call chain. A classic symptom-fix pattern is "
        "a downstream module adding a guard while the real bug lives in an "
        "upstream caller — your defect must point at the upstream location.\n"
        "- Redundancy check: if the patch copy-pastes logic that already exists "
        "elsewhere in the repository, or repeats the same block across "
        "files/functions, instead of reusing the existing helper or extracting a "
        "common function, report a `maintainability` defect (high when the "
        "duplication is substantial). Use `repeated_added_blocks` in Patch "
        "Analysis as evidence.\n"
        "- Suggestions MUST point at the right direction without copying a known fix."
    )


# Detailed prompt: emphasizes Step 1→6 workflow + symptom-fix detection 4 rules.
# Useful for non-trivial PRs where blind approval is risky. Rooted in software-
# engineering review best practice (trace root cause vs patch location; do not
# suppress symptoms; check broken dependencies; verify behavior not just absence
# of exceptions).
def _system_prompt_detailed() -> str:
    return (
        "# Code Review Task\n\n"
        "You are reviewing an AI-generated patch. Your job: determine if the patch "
        "**correctly fixes the root cause** of the bug.\n\n"
        "## Data Files (provided as repo context)\n"
        "- `problem_statement.txt` content is in the user message (section \"Issue\")\n"
        "- `predicted.json` field \"patch\" is in the user message (section \"Candidate Patch\")\n"
        "- `summary.json` task metadata may not be provided\n\n"
        "## Process\n\n"
        "> **Time budget**: simulate ≤100 iterations. Spend ≤10 on reading+hypothesis, "
        "≤30 on exploration, ≤20 on testing, ≤10 on writing the report. Past 70 iterations, "
        "skip remaining exploration and write the report based on what you know.\n\n"
        "### Step 1: Read the Problem (NOT the patch yet)\n"
        "Read the Issue. Answer two questions precisely:\n"
        "1. **What does the user expect?** (exact: no error, specific output, specific return value)\n"
        "2. **What actually happens?** (exact: error type + message, wrong output, wrong value)\n\n"
        "Do NOT read the Candidate Patch yet.\n\n"
        "### Step 2: Find the Root Cause\n"
        "Trace from the entry point down through the call stack:\n"
        "- Where is the bug triggered? (What does the user call?)\n"
        "- Follow the chain: entry → function → callee → callee's callee…\n"
        "- **Root cause = where the wrong value is created or wrong logic runs** — "
        "NOT where it first crashes or surfaces.\n"
        "- For bugs where output is wrong or missing: trace ALL code paths that produce "
        "output (not just the path that crashes).\n"
        "- Read at most 8-10 source files. After that, commit your best guess. A "
        "committed guess is better than a timeout.\n\n"
        "### Step 3: Write YOUR Proposed Fix (BEFORE reading the patch)\n"
        "Write what you would change based on your root cause. **This is a commitment.** "
        "You will compare the actual patch to this proposal.\n\n"
        "### Step 4: Read and Apply the Patch\n"
        "Now look at the Candidate Patch.\n\n"
        "**CRITICAL: Do NOT modify any source files.** Your role is to evaluate the patch "
        "as-is; report `request_changes` if any test would fail.\n\n"
        "Compare to your Step 3 proposal: same file? same function? same call-stack depth? "
        "Even if the patch matches, do the side-effect checks below.\n\n"
        "### Step 4b: Check for Broken Dependencies\n"
        "- Does the patch change function signatures, replace @lru_cache functions, or "
        "rename callables? If so, `grep -rn` for callers and confirm none depend on "
        "attributes the new version lacks (`.cache_clear()`, `.cache_info()`).\n"
        "- If a dependency is broken → high severity defect → request_changes.\n\n"
        "### Step 5: Verify Complete Behavior\n"
        "If context allows, write & run tests that cover bug reproduction, full-output "
        "assertion (not just \"no exception\"), and regression coverage. If tests fail → "
        "request_changes. **If a test fails: IMMEDIATELY write `request_changes`. Do NOT "
        "investigate the failure.**\n\n"
        "### Step 6: Write Report\n"
        "STOP all exploration. Write the JSON report NOW based on what you've found.\n\n"
        "## Decision Rules\n\n"
        "**Approve** only if ALL:\n"
        "1. Tests pass including full output/behavior verification (or no test runner available)\n"
        "2. The patch changes the ROOT CAUSE location identified in Step 2\n"
        "3. No high-severity defects\n\n"
        "**Request changes** if ANY:\n"
        "- Any test fails (including output verification)\n"
        "- The patch is a symptom fix (Step 4 HARD RULE triggered below)\n"
        "- The patch fixes only part of the affected code paths\n"
        "- A `high`-severity defect exists\n"
        "- Empty patch\n\n"
        "## Defect Severity Guide\n\n"
        "**High** (always → request_changes):\n"
        "- Incorrect result/output for any input\n"
        "- Non-deterministic output where determinism is expected\n"
        "- Missing sibling fix (same bug in related code path not patched)\n"
        "- Exception or crash introduced\n"
        "- Security vulnerability\n\n"
        "**Medium**: Performance regression, less efficient but functionally correct; "
        "substantial copy-paste of logic that already exists in the repository (or "
        "self-repetition across files/functions) where an existing helper should be "
        "reused or a common function extracted — corroborate with "
        "`repeated_added_blocks` in Patch Analysis\n\n"
        "**Low**: Style, variable naming, comments, minor refactoring (no behavior change)\n\n"
        "## HARD RULE — Symptom fix detection\n"
        "If ANY of these match, it is a symptom fix → `request_changes` "
        "(tests passing does NOT override this):\n"
        "- The patch is at a different / shallower location than your root cause\n"
        "- You find yourself thinking \"this doesn't fix the root cause but it prevents the crash\"\n"
        "- The patch adds a guard / check / conversion at the point of USE instead of "
        "fixing the point of CREATION\n"
        "- The patch fixes ONE manifestation but the root cause can trigger the same "
        "issue via other paths\n\n"
        "## Confidence\n"
        "- 0.9+ if you ran tests and they passed (or none were needed)\n"
        "- 0.7-0.89 if partial testing\n"
        "- below 0.7 if no test runner\n\n"
        "## Output Format (JSON, no markdown fences, no text before/after)\n\n"
        "{\n"
        '  "decision": {\n'
        '    "recommendation": "approve" | "request_changes",\n'
        '    "confidence": 0.0-1.0\n'
        "  },\n"
        '  "summary": {\n'
        '    "problem": "1 sentence: root cause (file + function + what\'s wrong)",\n'
        '    "solution": "2-4 sentences: what the patch changes and whether it matches the root cause",\n'
        '    "overall_assessment": "1-2 sentences: verdict with reason"\n'
        "  },\n"
        '  "defects": [\n'
        "    {\n"
        '      "severity": "high|medium|low",\n'
        '      "category": "correctness|compatibility|security|performance|maintainability",\n'
        '      "location": {"path": "exact/file/path.py", "start_line": N, "end_line": N},\n'
        '      "description": "What is wrong: specific code, specific condition, specific failure",\n'
        '      "suggestion": "In file.py function foo(), change X to Y"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Save this JSON as your deliverable. Output ONLY the JSON object."
    )


# User-message template for detailed style. Differs from concise in that it
# nudges the model to NOT read the patch during Step 1, forces Step 3 (own
# proposed fix) before reading the candidate, and emphasizes Step 4b / 5 checks.
def _user_prompt_detailed(issue, pr_title, pr_body, pr_diff, repo_context, analysis):
    ctx_json = truncate_json_text(json.dumps(repo_context, indent=2, ensure_ascii=False))
    return (
        "## Issue Description\n"
        f"{issue}\n\n"
        "## PR Metadata\n"
        f"**Title**: {pr_title}\n"
        f"**Description**: {pr_body or 'N/A'}\n\n"
        "## Repository Context (collected by explorer)\n"
        f"{ctx_json}\n\n"
        "## Patch Analysis\n"
        f"{json.dumps(analysis, indent=2, ensure_ascii=False)}\n\n"
        "---\n\n"
        "## Step 1+2 hint (do NOT jump to the patch yet)\n"
        "Form the user-expectation vs actual-behavior split. Then trace the call chain "
        "to the root-cause location.\n\n"
        "## Step 3 hint\n"
        "State your proposed fix (file + function + what to change) BEFORE examining "
        "the candidate patch.\n\n"
        "## Candidate Patch (Step 4 — apply only conceptually; do not modify source)\n"
        "```diff\n"
        f"{pr_diff}\n"
        "```\n\n"
        "## Step 4b / 5\n"
        "Use the repository context to check for broken dependencies and (if applicable) "
        "to mentally run reproduction / regression tests. If any test would fail, write "
        "`request_changes` immediately.\n\n"
        "## Step 6\n"
        "Output ONLY the JSON object per schema."
    )


# ============================================================================
# SubAgent
# ============================================================================

class ReviewerSubAgent:
    """审查 SubAgent — review a candidate PR and emit a structured JSON report.

    prompt_style:
      - "engineering" (default): senior code-quality review (8 dimensions, P0-P4
                                 findings, 4-level decision).
      - "concise"              : legacy bug-fix-centric review.
      - "detailed"             : legacy bug-fix-centric Step 1→6 workflow.
    """

    def __init__(
        self,
        tool_adapter: Any = None,
        explorer: Any = None,
        config: Optional[Dict[str, Any]] = None,
        prompt_style: str = "engineering",
        explore_timeout: int = 180,
        max_regen_attempts: int = 3,
        regen_backoff_seconds: float = 2.0,
        shard_large_diffs: bool = True,
        shard_budget_chars: int = DEFAULT_SHARD_BUDGET_CHARS,
        shard_concurrency: int = DEFAULT_SHARD_CONCURRENCY,
    ):
        if prompt_style not in PROMPT_STYLE_CHOICES:
            raise ValueError(
                f"prompt_style must be one of {PROMPT_STYLE_CHOICES!r}, got {prompt_style!r}"
            )
        self.tool_adapter = tool_adapter
        self.explorer = explorer
        self.config = config or {}
        self.prompt_style = prompt_style
        self.explore_timeout = explore_timeout
        # Long-form structured answers intermittently come back empty or
        # unparseable from hosted gateways (observed twice in self-loop runs).
        # Each fresh attempt still gets the one-shot JSON repair round.
        self.max_regen_attempts = max(1, int(max_regen_attempts))
        self.regen_backoff_seconds = max(0.0, float(regen_backoff_seconds))
        # Large-change fan-out. `pr_diff` was the one prompt input with no budget
        # at all (repo_context 60k, exploration 40k, reviser diff 50k), so a big
        # change both blew the context and serialised into one slow request.
        self.shard_large_diffs = bool(shard_large_diffs)
        self.shard_budget_chars = max(1_000, int(shard_budget_chars))
        self.shard_concurrency = max(1, int(shard_concurrency))
        self.name = "reviewer"
        self.capabilities = [
            "analyze_diff",
            "explore_repository",
            "generate_report",
            "decision_making",
            "detailed_prompts",
        ]

    async def execute(self, context: Dict[str, Any]) -> ReviewReport:
        """执行审查。

        context 字段（合法）：
          - issue: str                   # required
          - pr_title: str                # required
          - pr_body: str                 # optional
          - pr_diff: str                 # required
          - repo_path: str               # optional
          - max_exploration_steps: int   # optional, default 8
          - focus_files: List[str]       # optional hint
          - prompt_style: str            # optional override of constructor value

        Forbidden (raises ValueError):
          - golden_patch, test_info, oracle  (forbidden)
        """
        for forbidden in ("golden_patch", "test_info", "oracle"):
            if forbidden in context:
                raise ValueError(
                    f"'{forbidden}' is not allowed in review context. "
                    "These fields must not appear in review context."
                )

        prompt_style = context.get("prompt_style") or self.prompt_style
        if prompt_style not in PROMPT_STYLE_CHOICES:
            raise ValueError(
                f"prompt_style must be one of {PROMPT_STYLE_CHOICES!r}, got {prompt_style!r}"
            )

        issue = context.get("issue", "")
        pr_title = context.get("pr_title", "")
        pr_body = context.get("pr_body", "")
        pr_diff = context.get("pr_diff", "")
        repo_path = context.get("repo_path")
        max_steps = int(context.get("max_exploration_steps", 8))
        focus_files = context.get("focus_files") or []

        # 1) Explore
        repo_context: Dict[str, Any] = {}
        exploration_steps = 0
        if repo_path and self.explorer:
            repo_context, exploration_steps = await self._explore(
                repo_path=repo_path,
                issue=issue,
                pr_diff=pr_diff,
                focus_files=focus_files,
                max_steps=max_steps,
            )
        elif repo_path:
            repo_context = self._static_diff_context(pr_diff)

        # 2) Patch analysis
        analysis = self._analyze_patch(pr_diff, repo_context)

        # 3) Prompts
        if prompt_style == "engineering":
            # Trim the language-specific checklist (section 十六) to the
            # languages actually present in the diff — fixed prompt token cost
            # was flagged as a P3 finding in self-review.
            system_prompt = engineering_prompt.system_prompt(
                languages=engineering_prompt.detect_languages(pr_diff)
            )
            user_prompt = engineering_prompt.user_prompt(
                issue=issue,
                pr_title=pr_title,
                pr_body=pr_body,
                pr_diff=pr_diff,
                repo_context=repo_context,
                analysis=analysis,
            )
        elif prompt_style == "detailed":
            system_prompt = _system_prompt_detailed()
            user_prompt = _user_prompt_detailed(
                issue=issue,
                pr_title=pr_title,
                pr_body=pr_body,
                pr_diff=pr_diff,
                repo_context=repo_context,
                analysis=analysis,
            )
        else:
            system_prompt = _system_prompt_concise()
            user_prompt = self._build_user_prompt_concise(
                issue=issue,
                pr_title=pr_title,
                pr_body=pr_body,
                pr_diff=pr_diff,
                repo_context=repo_context,
                analysis=analysis,
            )

        # 4) AI call. A change that does not fit one shard's budget is reviewed as
        # concurrent file-aligned shards; anything smaller keeps the single-call
        # path byte-for-byte (zero regression for normal-sized changes).
        #
        # engineering-only on purpose: the shard prompt is built from the
        # engineering template, and quietly imposing it on the legacy
        # concise/detailed styles would change their semantics without saying so.
        if (self.shard_large_diffs
                and prompt_style == "engineering"
                and should_shard(pr_diff, self.shard_budget_chars)):
            report = await self._review_sharded(
                issue=issue,
                pr_title=pr_title,
                pr_body=pr_body,
                pr_diff=pr_diff,
                repo_context=repo_context,
                analysis=analysis,
            )
        else:
            # engineering reports (8 scored dimensions + findings) need more
            # completion budget than the legacy bug-centric styles.
            max_tokens = 8192 if prompt_style == "engineering" else 4096
            report, _usage, response = await self._review_once(
                system_prompt, user_prompt, max_tokens, prompt_style
            )
            report.raw_response = response

        report.exploration_steps = exploration_steps
        report.exploration_truncated = bool(repo_context.get("truncated"))
        return report

    async def _review_once(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        prompt_style: str,
    ) -> Tuple[ReviewReport, Dict[str, int], str]:
        """One review request: call → parse, with bounded regen + one JSON repair.

        Extracted so the sharded path reuses the *exact* recovery behaviour rather
        than duplicating it — the flaky-network handling is the last thing that
        should exist in two versions.

        Empty/unparseable long-form answers flake intermittently on hosted
        gateways (self-loop observation): retry FRESH full calls up to
        max_regen_attempts; every attempt still gets the one-shot JSON repair
        round. A wasted empty answer costs the full prompt anyway, so retrying is
        cheaper than letting the loop die.
        """
        report: Optional[ReviewReport] = None
        response = ""
        token_usage: Dict[str, int] = {}
        for attempt in range(1, self.max_regen_attempts + 1):
            response, call_usage = await self._call_ai_tool_with_retries(
                system_prompt, user_prompt, max_tokens=max_tokens
            )
            # Regen retries must not silently drop earlier attempts' cost.
            token_usage = _merge_token_usage(token_usage, call_usage)
            report = self._parse_response(response, token_usage, prompt_style=prompt_style)
            was_truncated = report.truncated_repair
            if (report.parse_error or report.truncated_repair) and self.tool_adapter:
                # One repair round for malformed model JSON (unescaped quotes, missing
                # commas) or truncation (repaired parse lost the tail — possibly
                # findings, possibly a P0). Bounded: exactly one extra call.
                repaired, extra_usage = await self._call_ai_tool_with_retries(
                    _JSON_REPAIR_SYSTEM,
                    _json_repair_user(response, report.parse_error or "truncated JSON"),
                    max_tokens=max_tokens,
                )
                token_usage = _merge_token_usage(token_usage, extra_usage)
                retry_report = self._parse_response(
                    repaired, token_usage, prompt_style=prompt_style
                )
                if not retry_report.parse_error and not retry_report.truncated_repair:
                    report = retry_report
                    response = repaired
                    if was_truncated:
                        # Trust state follows content provenance, not parse method: the
                        # repair round can close structure but cannot prove the lost
                        # tail's findings were recovered. Keep the final report
                        # untrusted so downstream gates (loop withhold-approve) hold.
                        report.truncated_repair = True
                else:
                    # Repair round failed — keep the best parse we have, but do not
                    # lose the repair call's token cost.
                    report.token_usage = token_usage
            if not report.parse_error or not self.tool_adapter:
                break
            if attempt < self.max_regen_attempts:
                await asyncio.sleep(self.regen_backoff_seconds * attempt)

        assert report is not None
        return report, token_usage, response

    async def _review_sharded(
        self,
        *,
        issue: str,
        pr_title: str,
        pr_body: str,
        pr_diff: str,
        repo_context: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> ReviewReport:
        """Map-reduce review of a large change (engineering style only).

        MAP  — one concurrent review per file-aligned shard, each with a bounded
               prompt and an explicit scope header (see `_shard_scope_section`).
        REDUCE — findings are merged by **deterministic code** (union, deduped,
               severity-ordered) so no shard's evidence can be dropped by a model,
               then ONE synthesis call assigns the global scores / decision /
               summary. If that call fails, a deterministic worst-case aggregation
               keeps the review usable instead of losing it.
        """
        shards = plan_shards(pr_diff, self.shard_budget_chars)
        system_prompt = engineering_prompt.system_prompt(
            languages=engineering_prompt.detect_languages(pr_diff)
        )
        max_tokens = 8192

        if len(shards) <= 1:
            # Budget arithmetic said shard, the splitter disagrees (e.g. one
            # oversized file): review it as-is rather than reporting nothing.
            user_prompt = engineering_prompt.user_prompt(
                issue=issue, pr_title=pr_title, pr_body=pr_body, pr_diff=pr_diff,
                repo_context=repo_context, analysis=analysis,
            )
            report, _u, response = await self._review_once(
                system_prompt, user_prompt, max_tokens, "engineering"
            )
            report.raw_response = response
            return report

        semaphore = asyncio.Semaphore(max(1, self.shard_concurrency))
        token_usage: Dict[str, int] = {}

        async def review_shard(shard: DiffShard) -> Tuple[DiffShard, ReviewReport, str]:
            async with semaphore:
                prompt = engineering_prompt.user_prompt(
                    issue=issue, pr_title=pr_title, pr_body=pr_body,
                    pr_diff=shard.diff,
                    repo_context=_context_for_shard(repo_context, shard),
                    analysis=analysis, shard=shard.to_dict(),
                )
                rep, _usage, raw = await self._review_once(
                    system_prompt, prompt, max_tokens, "engineering"
                )
                return shard, rep, raw

        # One shard raising must not sink the whole review — same reasoning as
        # best_of_n's `return_exceptions=True`.
        outcomes = await asyncio.gather(
            *[review_shard(s) for s in shards], return_exceptions=True
        )

        reports: List[ReviewReport] = []
        failed: List[str] = []
        for shard, outcome in zip(shards, outcomes):
            if isinstance(outcome, BaseException):
                failed.append(f"shard {shard.index}: {type(outcome).__name__}: {outcome}")
                continue
            _shard, rep, _raw = outcome
            token_usage = _merge_token_usage(token_usage, rep.token_usage or {})
            if rep.parse_error:
                failed.append(f"shard {shard.index}: {rep.parse_error}")
                continue
            reports.append(rep)

        if not reports:
            # No usable shard: report untrusted (withhold-approve holds downstream)
            # rather than inventing an approve.
            return ReviewReport(
                decision="request_changes",
                confidence=0.0,
                summary={"overall_assessment":
                         f"Sharded review produced no usable shard report "
                         f"({len(shards)} shards attempted)."},
                scores={},
                findings=[],
                defects=[],
                token_usage=token_usage,
                prompt_style="engineering",
                parse_error="sharded_review_all_shards_failed: " + "; ".join(failed[:6]),
            )

        # Findings come from the deterministic merge, never from a model. A shard
        # whose own report is untrusted (truncated repair) still contributes its
        # findings, but the merged report stays untrusted so the loop's
        # withhold-approve gate keeps holding.
        merged_findings = merge_findings([r.findings for r in reports])
        untrusted = [r for r in reports if r.truncated_repair]

        synthesis, synth_usage = await self._synthesize_shards(
            issue=issue, pr_title=pr_title, pr_body=pr_body,
            findings=merged_findings, shards=shards, analysis=analysis,
            system_prompt=f"{system_prompt}\n\n{_SYNTHESIS_SYSTEM}",
            max_tokens=max_tokens,
        )
        token_usage = _merge_token_usage(token_usage, synth_usage)

        base = synthesis or self._worst_case_report(reports)
        base.findings = merged_findings
        base.defects = [f.to_defect() for f in merged_findings]
        base.token_usage = token_usage
        base.prompt_style = "engineering"
        base.exploration_steps = 0
        # Provenance goes in `summary` because that is what reaches the caller —
        # `raw_response` is not part of `to_dict()`, so writing it there made the
        # shard plan invisible in every serialized report.
        base.summary = dict(base.summary or {})
        base.summary["sharded_review"] = (
            f"{len(shards)} shards ({sum(1 for s in shards if s.oversized)} oversized), "
            f"{len(reports)} usable, {len(failed)} failed, "
            f"{len(merged_findings)} findings merged"
            + ("" if synthesis else "; synthesis unavailable → deterministic aggregation")
        )
        if failed:
            base.summary["degraded_shards"] = "; ".join(failed[:6])
        if untrusted:
            base.truncated_repair = True
        base.raw_response = base.summary["sharded_review"]
        return base

    async def _synthesize_shards(
        self,
        *,
        issue: str,
        pr_title: str,
        pr_body: str,
        findings: List[Finding],
        shards: List[DiffShard],
        analysis: Dict[str, Any],
        system_prompt: str,
        max_tokens: int,
    ) -> Tuple[Optional[ReviewReport], Dict[str, int]]:
        """Turn per-shard findings into a global verdict in ONE call.

        Returns `(report_or_None, token_usage)`. `None` means "unavailable" and the
        caller falls back to deterministic aggregation — a synthesis outage must
        never discard a review that already holds real evidence.
        """
        if not self.tool_adapter:
            return None, {}
        prompt = _synthesis_user_prompt(issue, pr_title, pr_body, findings, shards,
                                        analysis)
        token_usage: Dict[str, int] = {}
        for attempt in range(1, self.max_regen_attempts + 1):
            response, usage = await self._call_ai_tool_with_retries(
                # The engineering system prompt carries the JSON schema and the
                # dimension definitions. Sending only the task framing produced a
                # hollow-but-parseable answer (no scores, no summary) that the
                # sanity gate accepts when the `scores` key is absent entirely.
                system_prompt, prompt, max_tokens=max_tokens
            )
            token_usage = _merge_token_usage(token_usage, usage)
            report = self._parse_response(response, token_usage,
                                          prompt_style="engineering")
            numeric = sum(1 for e in (report.scores or {}).values()
                          if isinstance(e.get("score"), (int, float)))
            if not report.parse_error and numeric >= 4:
                # total_score is the report's contract; derive it when the model
                # omitted it so a valid-but-partial synthesis stays usable.
                if report.total_score is None and report.scores:
                    vals = [e.get("score") for e in report.scores.values()
                            if isinstance(e.get("score"), (int, float))]
                    if vals:
                        report.total_score = float(sum(vals))
                report.findings = []
                report.defects = []
                return report, token_usage
            if attempt < self.max_regen_attempts:
                await asyncio.sleep(self.regen_backoff_seconds * attempt)
        return None, token_usage

    def _worst_case_report(self, reports: List[ReviewReport]) -> ReviewReport:
        """Deterministic aggregation used when synthesis is unavailable.

        Conservative by construction: the STRICTEST shard decision wins and every
        dimension takes the WORST score any shard gave — a shard that found a
        design problem must not be averaged away by shards that did not.
        """
        decision = "approve"
        confidence = 0.0
        scores: Dict[str, Dict[str, Any]] = {}
        for rep in reports:
            if (DECISION_STRICTNESS.get(rep.decision, 0)
                    > DECISION_STRICTNESS.get(decision, 0)):
                decision = rep.decision
            confidence = max(confidence, rep.confidence or 0.0)
            for dim, entry in (rep.scores or {}).items():
                if not isinstance(entry, dict):
                    continue
                score = entry.get("score")
                if not isinstance(score, (int, float)):
                    continue
                current = scores.get(dim)
                if current is None or score < current["score"]:
                    scores[dim] = {"score": score,
                                   "max": entry.get("max", SCORE_DIMENSIONS.get(dim, 10)),
                                   "reason": entry.get("reason", "")}
        for dim, maximum in SCORE_DIMENSIONS.items():
            scores.setdefault(dim, {"score": None, "max": maximum, "reason": ""})
        total = [s["score"] for s in scores.values() if isinstance(s["score"], (int, float))]
        # Shards normally report findings only, so a fallback usually has no
        # dimension scores to aggregate. Say so explicitly rather than shipping a
        # silently scoreless report (that is the hollow-report failure mode).
        detail = ("strictest decision, worst score per dimension" if total else
                  "strictest decision; dimension scores are unavailable because "
                  "shards report findings only")
        return ReviewReport(
            decision=decision,
            confidence=confidence,
            summary={"overall_assessment":
                     f"Synthesis step unavailable; aggregated {len(reports)} shard "
                     f"reports deterministically ({detail})."},
            scores=scores,
            total_score=float(sum(total)) if total else None,
            hard_gate={"triggered": False, "reason": ""},
            prompt_style="engineering",
        )

    # ------------------------------------------------------------------
    async def _explore(self, repo_path, issue, pr_diff, focus_files, max_steps):
        try:
            result = await asyncio.wait_for(
                self.explorer.execute({
                    "repo_path": repo_path,
                    "issue": issue,
                    "pr_diff": pr_diff,
                    "focus_files": focus_files,
                    "max_steps": max_steps,
                }),
                timeout=self.explore_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            return self._static_diff_context(pr_diff), 0

        if hasattr(result, "to_dict"):
            ctx = result.to_dict()
            steps = getattr(result, "steps", 0)
        elif isinstance(result, dict):
            ctx = result
            steps = result.get("steps", 0)
        else:
            ctx = {"raw": str(result)}
            steps = 0
        return ctx, steps

    def _static_diff_context(self, pr_diff: str) -> Dict[str, Any]:
        return {
            "repo_path": None,
            "files_modified": parse_diff_files(pr_diff),
            "keywords": [],
            "exploration_steps": 0,
            "note": "explorer not injected; only diff context used",
        }

    def _analyze_patch(self, pr_diff: str, repo_context: Dict[str, Any]) -> Dict[str, Any]:
        lines = pr_diff.split("\n")
        additions = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        return {
            "total_additions": additions,
            "total_deletions": deletions,
            "total_changes": additions + deletions,
            "files_changed": len(repo_context.get("files_modified", [])),
            # Static copy-paste redundancy evidence (same added lines repeated
            # across hunks/files) — the prompt instructs the model to judge
            # reuse / common-function extraction based on this.
            "repeated_added_blocks": detect_repeated_added_blocks(pr_diff),
        }

    def _build_user_prompt_concise(self, issue, pr_title, pr_body, pr_diff, repo_context, analysis):
        ctx_json = truncate_json_text(json.dumps(repo_context, indent=2, ensure_ascii=False))
        return (
            f"## Issue\n{issue}\n\n"
            f"## PR Metadata\n- Title: {pr_title}\n- Description: {pr_body or 'N/A'}\n\n"
            f"## Candidate Patch (to review)\n```diff\n{pr_diff}\n```\n\n"
            f"## Repository Context (collected by explorer)\n{ctx_json}\n\n"
            f"## Patch Analysis\n{json.dumps(analysis, indent=2, ensure_ascii=False)}\n\n"
            "## Your Task\n"
            "1. Read the issue and the patch.\n"
            "2. Use the repository context to verify the patch addresses the ROOT cause.\n"
            "3. If the patch looks like a symptom fix, trace upstream call chain "
            "(use `caller_chain` and `related_files` from context) and place a defect "
            "at the real buggy location.\n"
            "4. Emit the JSON review report per schema.\n\n"
            "Output ONLY the JSON object."
        )

    async def _call_ai_tool(self, system_prompt: str, user_prompt: str,
                            max_tokens: int = 4096):
        if self.tool_adapter:
            return await self.tool_adapter.chat(
                system=system_prompt, user=user_prompt,
                max_tokens=max_tokens, temperature=0.1,
            )
        return "{}", {"prompt_tokens": 0, "completion_tokens": 0}

    async def _call_ai_tool_with_retries(self, system_prompt: str, user_prompt: str,
                                         max_tokens: int = 4096, attempts: int = 3):
        """_call_ai_tool with bounded retry on transient adapter errors
        (gateway 429/budget blips, CLI timeouts). The last failure re-raises
        so hard outages still surface loudly."""
        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                return await self._call_ai_tool(system_prompt, user_prompt,
                                                max_tokens=max_tokens)
            except (FileNotFoundError, HostTurnRequired):
                # Deterministic (CLI missing / host turn required) — retrying cannot
                # help: a host turn only succeeds once the host writes its answer, so
                # burning regenerations would just delay the same exception.
                raise
            except Exception as exc:  # adapter errors vary by CLI
                last_exc = exc
                if i < attempts - 1:
                    await asyncio.sleep(self.regen_backoff_seconds * (i + 1))
        raise last_exc

    def _parse_response(self, response, token_usage, prompt_style):
        data, repaired = _best_effort_json(strip_fences(response))
        if data is None or not isinstance(data, dict):
            return ReviewReport(
                decision="request_changes",
                confidence=0.5,
                summary={"overall_assessment": "Failed to parse model response as JSON"},
                defects=[],
                token_usage=token_usage,
                prompt_style=prompt_style,
                parse_error="not valid JSON" if isinstance(response, str) and response.strip()
                            else "empty response",
            )

        if _is_empty_review_payload(data):
            return ReviewReport(
                decision="request_changes",
                confidence=0.5,
                summary={"overall_assessment": "Model returned JSON without any review content"},
                defects=[],
                token_usage=token_usage,
                prompt_style=prompt_style,
                parse_error="empty JSON payload (no review content)",
            )

        if prompt_style == "engineering":
            report = _parse_engineering_payload(data, token_usage)
        else:
            report = self._parse_legacy_payload(data, token_usage, prompt_style)
        report.truncated_repair = repaired
        return report

    def _parse_legacy_payload(self, data, token_usage, prompt_style):
        decision, confidence = _coerce_decision(data)
        defects_payload = data.get("defects", [])
        defects = [
            Defect(
                severity=pick_enum(d.get("severity", "medium"), DEFECT_SEVERITIES, "medium"),
                category=pick_enum(d.get("category", "correctness"), DEFECT_CATEGORIES, "correctness"),
                description=d.get("description", ""),
                location=d.get("location", ""),
                suggestion=d.get("suggestion", ""),
            )
            for d in defects_payload
        ]
        return ReviewReport(
            decision=decision,
            confidence=confidence,
            summary=data.get("summary", {}) or {},
            defects=defects,
            token_usage=token_usage,
            prompt_style=prompt_style,
        )

    def get_capabilities(self):
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": "reviewer",
            "prompt_style": self.prompt_style,
            "explore_timeout": self.explore_timeout,
            "capabilities": self.capabilities,
            "tool_adapter": type(self.tool_adapter).__name__ if self.tool_adapter else None,
            "explorer": type(self.explorer).__name__ if self.explorer else None,
        }


# ---------------------------------------------------------------------------
# Lenient JSON loading — LLM output deviates from STRICT JSON often enough
# (unescaped quotes inside strings, missing commas, truncated tails) that a
# parse failure must not silently throw away an entire review.
# ---------------------------------------------------------------------------

def _best_effort_json(text: str) -> tuple:
    """Parse JSON with fallbacks: direct load → brace-span extraction →
    truncated-tail repair. Returns (obj, repaired_via_truncation_heuristic);
    (None, False) when nothing works."""
    if not isinstance(text, str):
        return None, False
    text = text.strip()
    if not text:
        return None, False
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand), False
        except json.JSONDecodeError:
            continue
    for cand in reversed(candidates):
        repaired = _repair_truncated_json(cand)
        if repaired is not None:
            return repaired, True
    return None, False


def _repair_truncated_json(text: str) -> Optional[Any]:
    """Best-effort repair of truncated LLM JSON: close an unterminated string and
    any open brackets; if the tail is a partial key/value, trim back to earlier
    comma boundaries and try again (most-complete candidate first)."""
    for candidate in _truncation_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _truncation_candidates(text: str):
    """Yield candidate repairs for possibly-truncated JSON text."""
    yield _close_json(text)
    comma_positions = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == ",":
            comma_positions.append(i)
    for pos in reversed(comma_positions[-50:]):
        yield _close_json(text[:pos])


def _close_json(text: str) -> str:
    """Close an unterminated string and any open brackets at end of text."""
    stack: List[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    return text + ('"' if in_str else "") + "".join(reversed(stack))


# ---------------------------------------------------------------------------
# LLM JSON repair round (single retry, only when deterministic parsing fails)
# ---------------------------------------------------------------------------

_JSON_REPAIR_SYSTEM = (
    "You repair malformed JSON emitted by another model. The user message "
    "contains a review report that failed to parse. Output the SAME report as "
    "ONE valid JSON object — no markdown fences, no commentary, no text "
    "before/after. Preserve every field and value exactly; fix only the syntax "
    "(escape embedded quotes, add missing commas/brackets, complete a truncated "
    "tail without inventing new findings or scores)."
)


def _json_repair_user(raw_response: str, parse_error: str) -> str:
    snippet = raw_response[:60_000]
    return (
        f"The following JSON failed to parse (error: {parse_error}).\n\n"
        f"{snippet}\n\n"
        "Output ONLY the corrected, valid JSON object."
    )


# ---------------------------------------------------------------------------
# Sharded-review synthesis round
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are the SYNTHESIS step of a sharded code review. A large change was "
    "split by file and reviewed in parallel; you receive the union of every "
    "shard's findings plus the shape of the whole change.\n\n"
    "The findings are FINAL: do not re-litigate, add, or remove any of them — a "
    "deterministic merge already collected them and they are appended to your "
    "output by the caller. Your job is the GLOBAL judgement no single shard could "
    "make: judge consistency ACROSS the files it could not see, then assign the "
    "8-dimension scores, the 4-level decision, the hard gate, and a summary of the "
    "change as a whole.\n\n"
    "You MUST emit every one of the 8 dimension scores with a numeric `score` — a "
    "response without them is discarded and the review degrades to a worse "
    "fallback, so an incomplete answer is strictly worse than a cautious one.\n\n"
    "Output ONLY one JSON object — no prose, no markdown fences."
)


def _synthesis_user_prompt(issue, pr_title, pr_body, findings, shards,
                           analysis) -> str:
    payload = []
    for f in findings:
        path, start, end = _finding_location_parts(getattr(f, "location", ""))
        payload.append({
            "severity": getattr(f, "severity", ""),
            "title": getattr(f, "title", ""),
            "location": {"path": path, "start_line": start, "end_line": end},
            "observation": getattr(f, "observation", ""),
            "why_it_matters": getattr(f, "why_it_matters", ""),
            "evidence": getattr(f, "evidence", ""),
            "recommendation": getattr(f, "recommendation", ""),
            "constraint_category": getattr(f, "constraint_category", ""),
        })
    shape = {
        "shards": len(shards),
        "files": [f for s in shards for f in s.files],
        "largest_shard_chars": max((s.chars for s in shards), default=0),
        "oversized_shards": [s.index for s in shards if s.oversized],
        "total_additions": analysis.get("total_additions"),
        "total_deletions": analysis.get("total_deletions"),
    }
    return (
        "## Change Context / Intent\n"
        f"{issue}\n\n"
        "## PR Metadata\n"
        f"**Title**: {pr_title}\n"
        f"**Description**: {pr_body or 'N/A'}\n\n"
        "## Shape of the whole change\n"
        f"{json.dumps(shape, indent=2, ensure_ascii=False)}\n\n"
        "## Findings collected from all shards (FINAL — do not add or remove)\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
        "## Your Task\n"
        "1. Judge the change as a whole: design coherence, consistency ACROSS the "
        "files listed above, maintainability, risk introduced by the change, scope.\n"
        "2. Score all 8 dimensions ("
        + ", ".join(f"{k} 0-{v}" for k, v in SCORE_DIMENSIONS.items())
        + ").\n"
        "3. Give total_score out of 100.\n"
        "4. Decide: approve | approve_with_suggestions | request_changes | block.\n"
        "5. Write summary (overall_assessment etc.), whats_good, "
        "recommended_actions.\n"
        "6. hard_gate.triggered = true only if the change must not land.\n\n"
        "Do NOT emit a `findings` array (it is merged from the shards already). "
        "Output ONLY the JSON object."
    )


def _context_for_shard(repo_context: Dict[str, Any],
                       shard: DiffShard) -> Dict[str, Any]:
    """Narrow the shared repo context to the files this shard actually reviews.

    The context is re-sent in every shard prompt, so leaving the whole
    `file_contents` map in each one multiplies the prompt overhead by the shard
    count — measured as ~2x total tokens versus one call. A shard needs the
    contents of its own files; the global signals (keywords, root hints, call
    chain, test files) are small and stay so cross-cutting judgement is not lost.
    """
    contents = repo_context.get("file_contents") or {}
    if not contents:
        return repo_context
    keep = set(shard.files)
    narrowed = {k: v for k, v in contents.items() if k in keep}
    if len(narrowed) == len(contents):
        return repo_context
    out = dict(repo_context)
    out["file_contents"] = narrowed
    out["note"] = (f"file_contents narrowed to this shard's {len(narrowed)} "
                   f"file(s) out of {len(contents)} in the change")
    return out


def _merge_token_usage(a: Optional[Dict[str, int]], b: Optional[Dict[str, int]]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for usage in (a, b):
        if not usage:
            continue
        for k, v in usage.items():
            merged[k] = merged.get(k, 0) + (v or 0)
    return merged


def pick_enum(value: Any, choices: tuple, default: str) -> str:
    if isinstance(value, str) and value in choices:
        return value
    return default


def _coerce_decision(data: Dict[str, Any]) -> tuple[str, float]:
    """Accept BOTH flat and nested decision shapes; emit (flat decision, confidence).

    Normalizes the full 4-level vocabulary (engineering style emits uppercase):
    APPROVE / APPROVE_WITH_SUGGESTIONS / REQUEST_CHANGES / BLOCK.
    """
    d = data.get("decision")
    if isinstance(d, dict):
        # nested `decision.recommendation`-style shape
        rec = d.get("recommendation", "request_changes")
        conf = d.get("confidence", data.get("confidence", 0.5))
    elif isinstance(d, str):
        # flat shape
        rec = d
        conf = data.get("confidence", 0.5)
    else:
        rec = "request_changes"
        conf = data.get("confidence", 0.5)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.5
    rec = _normalize_decision(rec)
    return rec, conf


def _normalize_decision(rec: Any) -> str:
    """Map arbitrary decision spellings onto the canonical 4-value vocabulary."""
    if not isinstance(rec, str):
        return "request_changes"
    key = rec.strip().upper().replace(" ", "_").replace("-", "_")
    mapping = {
        "APPROVE": "approve",
        "APPROVE_WITH_SUGGESTIONS": "approve_with_suggestions",
        "REQUEST_CHANGES": "request_changes",
        "BLOCK": "block",
    }
    return mapping.get(key, "request_changes")


def _is_empty_review_payload(data: Dict[str, Any]) -> bool:
    """True when the parsed JSON carries no review content at all (e.g. the
    model answered `{}` or only noise fields).

    Such payloads used to degrade silently into a default request_changes@0.5
    with zero findings — wasting a whole loop iteration (regression found by
    a self-loop run). They are now flagged as parse errors, which triggers
    ReviewerSubAgent's one-shot internal JSON-repair retry; if that still
    yields nothing, the loop stops with `review_unparseable` (blind revision
    cannot converge).
    """
    if not data:
        return True
    if data.get("decision") is not None:
        return False
    if data.get("findings") or data.get("defects"):
        return False
    if data.get("summary"):
        return False
    scores = data.get("scores")
    if isinstance(scores, dict) and any(
        (isinstance(v, dict) and v.get("score") is not None)
        or isinstance(v, (int, float))
        for v in scores.values()
    ):
        return False
    hard_gate = data.get("hard_gate")
    if isinstance(hard_gate, dict) and hard_gate.get("triggered"):
        return False
    if data.get("total_score") is not None:
        return False
    return True


def _parse_engineering_payload(data: Dict[str, Any], token_usage) -> ReviewReport:
    """Parse the engineering-style STRICT JSON payload (scores + P0-P4 findings).

    Findings are additionally mapped onto legacy Defect objects so downstream
    consumers (reviser / loop) keep working without schema changes.
    """
    decision, confidence = _coerce_decision(data)

    findings: List[Finding] = []
    for f in data.get("findings", []) or []:
        if not isinstance(f, dict):
            continue
        sev_raw = str(f.get("severity", "") or "").strip().upper()
        # Conservative default for unrecognized severity: P1, never silently
        # demote an unknown signal below the block gate (P0/P1 territory).
        severity = sev_raw if sev_raw in FINDING_SEVERITIES else "P1"
        findings.append(Finding(
            severity=severity,
            title=str(f.get("title", "") or ""),
            location=f.get("location", ""),
            observation=str(f.get("observation", "") or ""),
            why_it_matters=str(f.get("why_it_matters", "") or ""),
            evidence=str(f.get("evidence", "") or ""),
            recommendation=str(f.get("recommendation", "") or ""),
            confidence=pick_enum(
                str(f.get("confidence", "medium") or "medium").lower(),
                ("high", "medium", "low"), "medium",
            ),
            constraint_category=pick_enum(
                str(f.get("constraint_category", "") or "").strip(),
                engineering_prompt.CONSTRAINT_CATEGORIES, "",
            ),
        ))

    scores_raw = data.get("scores", {}) or {}
    scores: Dict[str, Any] = {}
    for dim, max_pts in SCORE_DIMENSIONS.items():
        entry = scores_raw.get(dim, {})
        if isinstance(entry, dict):
            scores[dim] = {
                "score": entry.get("score"),
                "max": max_pts,
                "reason": entry.get("reason", ""),
            }
        else:  # tolerate bare number
            scores[dim] = {"score": entry, "max": max_pts, "reason": ""}

    total_score = data.get("total_score")
    try:
        total_score = float(total_score) if total_score is not None else None
    except (TypeError, ValueError):
        total_score = None

    # Consistency: total must equal the sum of the 8 dimensions; the
    # per-dimension scores are the evidence-based source of truth. When the
    # model under-reports dimensions, never trust its self-reported total.
    dim_values = [entry.get("score") for entry in scores.values()]
    if len(dim_values) == len(SCORE_DIMENSIONS) and all(
        isinstance(v, (int, float)) for v in dim_values
    ):
        computed = float(sum(dim_values))
        if total_score is None or abs(total_score - computed) > 0.01:
            total_score = computed
    else:
        total_score = None

    hard_gate = data.get("hard_gate", {}) or {}
    if not isinstance(hard_gate, dict):
        hard_gate = {"triggered": bool(hard_gate), "reason": ""}
    hard_gate.setdefault("triggered", False)
    hard_gate.setdefault("reason", "")

    # Consistency guards: enforce the prompt's own decision rules even when the
    # model output is internally inconsistent.
    #   - hard gate triggered          ⇒ block
    #   - any P0 finding present       ⇒ block   (P0 must block merge)
    #   - any P1 finding + approving   ⇒ request_changes
    severities = {f.severity for f in findings}
    if hard_gate.get("triggered") or "P0" in severities:
        decision = "block"
    elif "P1" in severities and decision in DECISION_APPROVING:
        decision = "request_changes"

    defects = [f.to_defect() for f in findings]

    def _str_list(key: str) -> List[str]:
        """Tolerate a bare string or a list for the optional engineering fields."""
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if x]

    # Fallback: if the model occasionally answers in the legacy shape
    # (defects[] but no findings[]), preserve the feedback instead of
    # dropping it on the floor.
    if not defects:
        for d in data.get("defects", []) or []:
            if not isinstance(d, dict):
                continue
            defects.append(Defect(
                severity=pick_enum(d.get("severity", "medium"), DEFECT_SEVERITIES, "medium"),
                category=pick_enum(d.get("category", "correctness"), DEFECT_CATEGORIES, "correctness"),
                description=d.get("description", ""),
                location=d.get("location", ""),
                suggestion=d.get("suggestion", ""),
            ))

    # Content-sanity gate: a structurally valid but hollow payload is as
    # unusable as malformed JSON (observed in self-review: one finding with
    # all evidence fields empty, 7/8 dimension scores null, one out-of-range
    # score). Treat it as a parse failure so the regen/repair retries kick in
    # and downstream keeps the report untrusted instead of revising on
    # hallucinated feedback.
    sanity_problems: List[str] = []
    for dim, entry in scores.items():
        v = entry.get("score")
        if isinstance(v, (int, float)) and not (0 <= v <= entry["max"]):
            sanity_problems.append(
                f"{dim} score {v} out of range [0,{entry['max']}]")
            entry["score"] = None  # out-of-range scores are not evidence
    numeric_scores = sum(1 for e in scores.values()
                         if isinstance(e.get("score"), (int, float)))
    # Only enforce score completeness when the model ATTEMPTED scores (key
    # present) but delivered mostly nulls/out-of-range — a minimal approve
    # that omits the scores key entirely stays a valid legacy-tolerant answer.
    if data.get("scores") and numeric_scores < 4:
        sanity_problems.append(
            f"only {numeric_scores}/{len(SCORE_DIMENSIONS)} dimension scores present")
    if findings and all(not (f.observation or f.evidence) for f in findings):
        sanity_problems.append("all findings lack observation/evidence")
    if sanity_problems:
        return ReviewReport(
            decision=decision,
            confidence=confidence,
            summary=data.get("summary", {}) or {},
            defects=defects,
            findings=findings,
            scores=scores,
            total_score=None,
            hard_gate=hard_gate,
            token_usage=token_usage,
            prompt_style="engineering",
            parse_error="incomplete_content: " + "; ".join(sanity_problems),
        )

    return ReviewReport(
        decision=decision,
        confidence=confidence,
        summary=data.get("summary", {}) or {},
        defects=defects,
        findings=findings,
        scores=scores,
        total_score=total_score,
        hard_gate=hard_gate,
        whats_good=_str_list("whats_good"),
        recommended_actions=_str_list("recommended_actions"),
        token_usage=token_usage,
        prompt_style="engineering",
    )
