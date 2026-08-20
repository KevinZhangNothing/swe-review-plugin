"""
Explorer SubAgent - 仓库探索 SubAgent

让 Reviewer 按 Step 1→6 workflow 做"循调用链追踪 root cause"：
1) 读 diff 锁定修改文件
2) 从 issue 抽取可疑关键词 / 模块
3) grep + 读源文件
4) 追踪 caller-callee 关系

输出 ExplorationResult：
  - files_modified, related_files, test_files
  - file_contents (修改文件 + 相关文件，含 caller_chain 摘要)
  - call_chain
  - keywords, root_hint  (供 reviewer 推断 root cause)
  - steps  (实际执行的步骤数，便于观测)
"""

import os
import re
import subprocess
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class ExplorationResult:
    repo_path: Optional[str]
    files_modified: List[str] = field(default_factory=list)
    related_files: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    file_contents: Dict[str, str] = field(default_factory=dict)
    call_chain: List[Dict[str, str]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    root_hint: List[str] = field(default_factory=list)
    steps: int = 0
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "this", "that", "these", "those", "in", "on", "at", "to", "for",
    "with", "by", "of", "and", "or", "but", "not", "as", "it",
    "from", "into", "when", "then", "than", "so", "such",
}


class ExplorerSubAgent:
    """探索代码仓库的 SubAgent。"""

    def __init__(
        self,
        repo_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        self.config = config or {}
        self.name = "explorer"
        self.max_depth = int(self.config.get("max_depth", 5))
        self.max_files_read = int(self.config.get("max_files_read", 30))
        self.max_file_bytes = int(self.config.get("max_file_bytes", 30_000))
        self.grep_timeout = int(self.config.get("grep_timeout", 8))
        self.capabilities = [
            "file_search",
            "code_analysis",
            "dependency_tracking",
            "test_discovery",
        ]

    async def execute(self, context: Dict[str, Any]) -> ExplorationResult:
        repo_path = context.get("repo_path")
        if repo_path:
            self.repo_path = Path(repo_path)
        issue: str = context.get("issue", "")
        pr_diff: str = context.get("pr_diff", "")
        focus_files: List[str] = context.get("focus_files") or []
        max_steps: int = int(context.get("max_steps", 8))

        result = ExplorationResult(repo_path=str(self.repo_path) if self.repo_path else None)

        # 1) diff → 修改文件
        result.files_modified = self._parse_modified_files(pr_diff)
        if focus_files:
            for f in focus_files:
                if f not in result.files_modified:
                    result.files_modified.append(f)
        result.steps += 1

        # 2) issue → 关键词
        result.keywords = self._extract_keywords(issue)
        result.steps += 1

        # 3) 关键词 grep 找相关文件（cap by max_steps）
        if result.keywords and max_steps > result.steps:
            related = self._search_related_files(result.keywords, time_budget=max_steps - result.steps)
            for p in related:
                if p not in result.related_files and p not in result.files_modified:
                    result.related_files.append(p)
            result.steps += 1

        # 4) 找测试文件
        result.test_files = self._find_test_files(result.files_modified)
        result.steps += 1

        # 5) 读关键文件内容（含截断）
        priority = result.files_modified + result.related_files[:10]
        priority = list(dict.fromkeys(priority))[: self.max_files_read]
        if priority:
            result.file_contents, truncated = self._read_key_files(priority)
            result.truncated = truncated
        result.steps += 1

        # 6) 调用链追踪（简单函数级 def / call）
        if result.files_modified and max_steps > result.steps:
            result.call_chain = self._trace_call_chain(result.files_modified)
            result.steps += 1

        # 7) root_hint：从 diff 抽出函数名 + 关键词，提示 reviewer 关注
        result.root_hint = self._build_root_hint(pr_diff, result.keywords)
        result.steps += 1

        return result

    # ------------------------------------------------------------------
    def _parse_modified_files(self, pr_diff: str) -> List[str]:
        files: List[str] = []
        for line in pr_diff.split("\n"):
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 3:
                    f = parts[2].removeprefix("a/").removeprefix("b/")
                    if f not in files:
                        files.append(f)
        return files

    def _extract_keywords(self, issue: str) -> List[str]:
        if not issue:
            return []
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]+", issue.lower())
        seen, out = set(), []
        for w in words:
            if w in STOPWORDS or len(w) <= 3:
                continue
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
            if len(out) >= 20:
                break
        return out

    def _search_related_files(self, keywords: List[str], time_budget: int) -> List[str]:
        """shell grep 找包含关键词的相关文件"""
        related: List[str] = []
        if not keywords or not self.repo_path.exists():
            return related
        # 限制时间预算 + 文件数量
        per_kw = max(2, min(5, time_budget * 2))
        for kw in keywords[:per_kw]:
            try:
                r = subprocess.run(
                    ["grep", "-r", "-l", "-I",
                     "--include=*.py", "--include=*.js", "--include=*.ts",
                     "--include=*.java", "--include=*.go", "--include=*.rs",
                     "--include=*.cpp", "--include=*.c", "--include=*.h",
                     kw, str(self.repo_path)],
                    capture_output=True, text=True, timeout=self.grep_timeout,
                )
                for fp in r.stdout.strip().split("\n"):
                    if not fp:
                        continue
                    try:
                        rel = str(Path(fp).relative_to(self.repo_path))
                    except ValueError:
                        rel = fp
                    if rel not in related and rel not in self._parse_modified_files(""):  # ok
                        related.append(rel)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            if len(related) >= 25:
                break
        return related

    def _find_test_files(self, source_files: List[str]) -> List[str]:
        tests: List[str] = []
        for src in source_files:
            base = Path(src).stem
            parent = Path(src).parent
            patterns = [
                f"test_{base}.py", f"{base}_test.py",
                f"{base}.test.js", f"{base}.spec.js",
                f"{base}_test.go", f"Test{base.title().replace('_','')}.java",
            ]
            for pat in patterns:
                tp = self.repo_path / parent / pat
                if tp.exists() and tp.is_file():
                    rel = str(tp.relative_to(self.repo_path))
                    if rel not in tests:
                        tests.append(rel)
        return tests

    def _read_key_files(self, files: List[str]) -> Tuple[Dict[str, str], bool]:
        contents: Dict[str, str] = {}
        truncated = False
        for rel in files:
            if not self.repo_path:
                continue
            fp = self.repo_path / rel
            try:
                if not fp.exists() or not fp.is_file():
                    continue
                size = fp.stat().st_size
                if size > 1_000_000:  # 跳过超大文件
                    continue
                text = fp.read_text(errors="replace")
                if len(text) > self.max_file_bytes:
                    text = text[: self.max_file_bytes] + "\n... (truncated)"
                    truncated = True
                contents[rel] = text
            except Exception:
                continue
        return contents, truncated

    def _trace_call_chain(self, files: List[str]) -> List[Dict[str, str]]:
        """在每个修改文件里找 def 名称 + 调用名称，做简单 mapping"""
        chain: List[Dict[str, str]] = []
        for rel in files:
            fp = self.repo_path / rel
            try:
                content = fp.read_text(errors="replace")
            except Exception:
                continue
            defs = re.findall(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content, re.MULTILINE)
            calls = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
            for c in calls[:15]:
                # 过滤掉 self.xxx / builtins
                if c in {"self", "cls", "print", "len", "range", "str", "int", "dict",
                         "list", "set", "tuple", "open", "isinstance", "hasattr",
                         "getattr", "setattr", "super", "type"}:
                    continue
                if c in defs:
                    chain.append({
                        "file": rel,
                        "function": c,
                        "defined_in": rel,
                    })
                else:
                    chain.append({"file": rel, "function": c, "defined_in": "?"})
        return chain

    def _build_root_hint(self, pr_diff: str, keywords: List[str]) -> List[str]:
        """给 reviewer 的快速 hint：从 diff 里抽出函数名 + 涉及到的关键词"""
        hints: List[str] = []
        if pr_diff:
            funcs = re.findall(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", pr_diff)
            seen_funcs: List[str] = []
            for f in funcs:
                if f not in seen_funcs:
                    seen_funcs.append(f)
                if len(seen_funcs) >= 5:
                    break
            hints.extend(f"function:{f}" for f in seen_funcs)
        hints.extend(f"keyword:{k}" for k in keywords[:5])
        return hints

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": "explorer",
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "capabilities": self.capabilities,
        }
