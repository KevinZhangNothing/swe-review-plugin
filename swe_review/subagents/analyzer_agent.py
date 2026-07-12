"""
Analyzer SubAgent - 静态分析

为 Reviewer / Generator 提供二级信号：
- diff hunk 复杂度（行数 / 嵌套 / 函数嵌套）
- 怀疑的回归点（修改的 public API）
- 调用影响范围粗算

不读 LLM，纯规则。
"""

import re
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AnalyzerSubAgent:
    """静态分析 SubAgent"""

    PUBLIC_NAME_RE = re.compile(r"^(?!_)([A-Za-z_][A-Za-z0-9_]*)\s*\(")
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
            for line in h["lines"]:
                if line.startswith("+") and self.PUBLIC_NAME_RE.match(line.lstrip("+").lstrip()):
                    sig = line.lstrip("+").strip()
                    public_api_changes.append(f"{h['file']}::{sig[:120]}")
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
        )

    def _split_hunks(self, pr_diff: str) -> List[Dict[str, Any]]:
        hunks: List[Dict[str, Any]] = []
        current_file = ""
        current_header = ""
        current_lines: List[str] = []
        cur_add, cur_del = 0, 0

        def flush():
            nonlocal current_file, current_header, current_lines, cur_add, cur_del
            if current_header or current_lines:
                hunks.append({
                    "file": current_file,
                    "header": current_header,
                    "lines": current_lines,
                    "additions": cur_add,
                    "deletions": cur_del,
                })
            current_file = ""
            current_header = ""
            current_lines = []
            cur_add, cur_del = 0, 0

        for line in pr_diff.split("\n"):
            if line.startswith("diff --git"):
                flush()
                parts = line.split()
                if len(parts) >= 3:
                    current_file = parts[2].replace("a/", "").replace("b/", "")
            elif line.startswith("@@"):
                if current_header or current_lines:
                    flush()
                current_header = line
            elif current_file:
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
