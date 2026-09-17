"""
Analyzer SubAgent - 静态分析

为 Reviewer / Generator 提供二级信号：
- diff hunk 复杂度（行数 / 嵌套 / 函数嵌套）
- 怀疑的回归点（修改的 public API）
- 调用影响范围粗算

不读 LLM，纯规则。
"""

import re
from collections import Counter
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class AnalyzerResult:
    total_hunks: int
    total_additions: int
    total_deletions: int
    files_changed: List[str]
    public_api_changes: List[str]
    suspicious_spots: List[Dict[str, str]]
    complexity_score: float
    # Copy-paste redundancy signal: identical added lines repeated across
    # multiple hunks/files — evidence for "extract a common function instead
    # of duplicating". Fed to the reviewer inside Patch Analysis.
    repeated_added_blocks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Lines that repeat for idiomatic reasons, not because of copy-paste
# redundancy: imports, comments, bare closers/keywords.
_REDUNDANCY_SKIP_RE = re.compile(
    r"^(import |from |#|//|/\*|\*|\"\"\"|'''"
    r"|[}\])]+;?$"
    r"|return$|raise$|pass$|continue$|break$|else:?$|try:?$)"
)


#: Paths that hold tests rather than shipped code. A new `def test_*` or fixture
#: is not a change to the product's public surface; counting it both misleads the
#: reviewer and saturates the API term of `complexity_score` — API_NORMALIZER is
#: 5, so five added test functions alone push that term to 1.0.
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|spec)/"
    r"|(^|/)test_[^/]*$"
    r"|(^|/)[^/]*_test\.[A-Za-z0-9]+$"
    r"|\.(test|spec)\.[A-Za-z0-9]+$"
)


def detect_repeated_added_blocks(
    pr_diff: str,
    min_line_len: int = 12,
    min_count: int = 2,
    cap: int = 10,
) -> List[Dict[str, Any]]:
    """Static redundancy signal: identical added lines repeated across the diff.

    Reports added lines that occur >= `min_count` times in >=2 distinct
    (file, hunk) locations — i.e. copy-pasted logic that should likely be
    extracted into a shared/common function or reuse an existing one. This is
    EVIDENCE for the reviewer, not a verdict: short/idiomatic lines are
    filtered out to keep noise low.

    Returns a list capped at `cap` entries (most repeated first):
      {"line": str, "count": int, "files": [..], "hunks": int}
    """
    counts: Counter = Counter()
    locations: Dict[str, set] = {}
    current_file = ""
    hunk_idx = 0
    for line in pr_diff.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            current_file = (
                parts[2].removeprefix("a/").removeprefix("b/")
                if len(parts) >= 3 else ""
            )
            continue
        if line.startswith("@@"):
            hunk_idx += 1
            continue
        if line.startswith("+++") or not line.startswith("+"):
            continue
        text = line[1:].strip()
        if len(text) < min_line_len or _REDUNDANCY_SKIP_RE.match(text):
            continue
        counts[text] += 1
        locations.setdefault(text, set()).add((current_file, hunk_idx))

    out: List[Dict[str, Any]] = []
    for text, cnt in counts.most_common():
        if cnt < min_count:
            break
        locs = locations[text]
        # Require repetition ACROSS hunks/files; repeats inside a single hunk
        # are too likely to be incidental (test assertions, similar branches).
        if len(locs) < 2:
            continue
        out.append({
            "line": text[:120],
            "count": cnt,
            "files": sorted({f for f, _ in locs if f})[:5],
            "hunks": len(locs),
        })
        if len(out) >= cap:
            break
    return out


class AnalyzerSubAgent:
    """静态分析 SubAgent"""

    #: An added **top-level** public definition. The match is anchored at column 0
    #: on purpose: the previous pattern was applied to `line.lstrip("+").lstrip()`,
    #: which erased indentation, so every nested call and every added line that
    #: merely *started* with `identifier(` — `except (`, `isinstance(`, `super().__init__(`,
    #: `assert (`, wrapped test continuations — was reported as a public API change
    #: (20/20 false positives on a real self-review) and saturated the API term of
    #: `complexity_score`.
    PUBLIC_NAME_RE = re.compile(
        r"^(?:async\s+)?(?:def|class)\s+(?!_)([A-Za-z][A-Za-z0-9_]*)"
    )
    TODO_RE = re.compile(r"#\s*TODO|XXX|FIXME", re.IGNORECASE)
    EXCEP_RE = re.compile(r"\b(except|raise)\b")

    # 复杂度评分公式:
    #   score = diff_impact * DIFF_WEIGHT + api_impact * API_WEIGHT
    #   diff_impact = min(1.0, (additions + deletions) / DIFF_NORMALIZER)
    #   api_impact  = min(1.0, len(public_api_changes) / API_NORMALIZER)
    # DIFF_NORMALIZER: 超过 400 行变更即 diff_impact 达到 1.0
    # API_NORMALIZER:  超过 5 个 public API 变更即 api_impact 达到 1.0
    DIFF_NORMALIZER: float = 400.0
    API_NORMALIZER: float = 5.0
    DIFF_WEIGHT: float = 0.6
    API_WEIGHT: float = 0.4

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.name = "analyzer"
        self.capabilities = [
            "static_diff_analysis",
            "api_surface_check",
            "complexity_estimate",
        ]

    async def execute(self, context: Dict[str, Any]) -> AnalyzerResult:
        pr_diff = context.get("pr_diff", "") or ""
        old_content = context.get("old_content") or {}  # path -> str
        new_content = context.get("new_content") or {}
        hunks = self._split_hunks(pr_diff)
        additions = sum(h["additions"] for h in hunks)
        deletions = sum(h["deletions"] for h in hunks)
        files = list({h["file"] for h in hunks if h["file"]})

        public_api_changes: List[str] = []
        suspicious: List[Dict[str, str]] = []
        for h in hunks:
            # Test definitions are not part of the shipped public surface — see
            # _TEST_PATH_RE for why they must not feed complexity_score either.
            in_test_file = bool(_TEST_PATH_RE.search(h["file"] or ""))
            for line in h["lines"]:
                # NOTE: `line[1:]` (not .lstrip()) — indentation is load-bearing
                # for PUBLIC_NAME_RE, which only accepts top-level definitions.
                body = line[1:] if line.startswith("+") else line
                if (line.startswith("+") and not in_test_file
                        and self.PUBLIC_NAME_RE.match(body)):
                    public_api_changes.append(f"{h['file']}::{body.strip()[:120]}")
                if line.startswith("+") and self.TODO_RE.search(line):
                    suspicious.append({"file": h["file"], "hunk": h["header"], "kind": "todo_in_diff"})
                if line.startswith("+") and self.EXCEP_RE.search(line):
                    suspicious.append({"file": h["file"], "hunk": h["header"], "kind": "new_exception_path"})

        score = min(1.0, (additions + deletions) / self.DIFF_NORMALIZER) * self.DIFF_WEIGHT \
              + min(1.0, len(public_api_changes) / self.API_NORMALIZER) * self.API_WEIGHT

        return AnalyzerResult(
            total_hunks=len(hunks),
            total_additions=additions,
            total_deletions=deletions,
            files_changed=files,
            public_api_changes=public_api_changes[:20],
            suspicious_spots=suspicious[:20],
            complexity_score=round(score, 3),
            repeated_added_blocks=detect_repeated_added_blocks(pr_diff),
        )

    def _split_hunks(self, pr_diff: str) -> List[Dict[str, Any]]:
        hunks: List[Dict[str, Any]] = []
        current_file = ""
        current_header = ""
        current_lines: List[str] = []
        cur_add, cur_del = 0, 0

        def flush():
            # NOTE: do NOT reset current_file here — a file spans multiple hunks,
            # and a @@-triggered flush must keep the file context for following lines.
            nonlocal current_header, current_lines, cur_add, cur_del
            if current_header:
                hunks.append({
                    "file": current_file,
                    "header": current_header,
                    "lines": current_lines,
                    "additions": cur_add,
                    "deletions": cur_del,
                })
            current_header = ""
            current_lines = []
            cur_add, cur_del = 0, 0

        for line in pr_diff.split("\n"):
            if line.startswith("diff --git"):
                flush()
                parts = line.split()
                current_file = parts[2].removeprefix("a/").removeprefix("b/") if len(parts) >= 3 else ""
            elif line.startswith("@@"):
                flush()
                current_header = line
            elif current_file and current_header:
                if line.startswith("+++") or line.startswith("---"):
                    continue
                current_lines.append(line)
                if line.startswith("+"):
                    cur_add += 1
                elif line.startswith("-"):
                    cur_del += 1
        flush()
        return hunks

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "analyzer", "capabilities": self.capabilities}
