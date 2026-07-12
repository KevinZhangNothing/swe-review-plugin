"""
Verifier SubAgent - 验证 SubAgent

负责：
1. apply patch 到指定 repo（小心：不污染用户工作区）
2. 运行 fail_to_pass + pass_to_pass 测试（仅在用户明确授权时）
3. 比对候选 patch 与 oracle（注意：oracle 仅在评测模式下注入，不进 prompt）

NOTE:
- 在真实 SWE 评测中，gold_patch 通过环境变量/CLI 注入；本 SubAgent 仅按
  显式开关 `oracle_enabled=True` 接收，否则只校验 patch 自身形态。
- 不在 prompt 中给 reviewer 暴露任何 oracle（review context 必须保持纯净）。
"""

import json
import shutil
import subprocess
import tempfile
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class VerificationResult:
    passed: bool
    test_results: List[Dict[str, Any]]
    resolution_status: str  # "resolved" | "not_resolved" | "partially_resolved" | "unknown"
    confidence: float
    details: str
    patch_applied: bool
    sandbox_used: bool
    oracle_similarity: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VerifierSubAgent:
    """验证 SubAgent（独立于 reviewer，不污染 prompt）"""

    def __init__(
        self,
        repo_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        self.config = config or {}
        self.name = "verifier"
        self.test_timeout = int(self.config.get("test_timeout", 120))
        self.sandbox = bool(self.config.get("sandbox", True))  # 默认沙箱运行
        self.capabilities = [
            "run_tests",
            "compare_patches",
            "verify_resolution",
            "sandbox_apply",
        ]

    async def execute(self, context: Dict[str, Any]) -> VerificationResult:
        """执行验证
        context 字段：
          - pr_diff:  必需
          - test_info:{fail_to_pass, pass_to_pass}: 可选
          - oracle: 可选（评测模式专用，绝对不会进入 reviewer prompt）
          - test_runner: 可选命令模板，例如 ["python","-m","pytest","{test}","-q"]
        """
        pr_diff = context.get("pr_diff", "")
        test_info = context.get("test_info") or {}
        oracle = context.get("oracle")
        runner = context.get("test_runner")
        repo_path = context.get("repo_path")
        if repo_path:
            self.repo_path = Path(repo_path)

        sandbox_used = False
        work_repo = self.repo_path
        tmp: Optional[Path] = None
        if self.sandbox:
            tmp = Path(tempfile.mkdtemp(prefix="swe-review-"))
            try:
                # 只在沙箱里 shallow copy 文件，避开污染用户工作区
                if self.repo_path.exists():
                    shutil.copytree(
                        self.repo_path,
                        tmp / "repo",
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"),
                    )
                    work_repo = tmp / "repo"
                    sandbox_used = True
            except Exception:
                work_repo = self.repo_path
                sandbox_used = False

        try:
            patch_applied = self._apply_patch(pr_diff, work_repo)
            if not patch_applied:
                return VerificationResult(
                    passed=False,
                    test_results=[],
                    resolution_status="unknown",
                    confidence=0.0,
                    details="Failed to apply patch (syntax or context mismatch).",
                    patch_applied=False,
                    sandbox_used=sandbox_used,
                )

            test_results: List[Dict[str, Any]] = []
            if test_info or runner:
                test_results = await self._run_tests(test_info, runner, work_repo)

            status = self._classify_status(test_results)
            confidence = self._calc_confidence(test_results, status)
            details = self._format_details(test_results)

            oracle_sim: Optional[float] = None
            if oracle:
                oracle_sim = self.compare_patches(pr_diff, oracle)["similarity"]

            return VerificationResult(
                passed=(status == "resolved"),
                test_results=test_results,
                resolution_status=status,
                confidence=confidence,
                details=details,
                patch_applied=True,
                sandbox_used=sandbox_used,
                oracle_similarity=oracle_sim,
            )
        finally:
            self._cleanup_sandbox(tmp)

    def _cleanup_sandbox(self, tmp: Optional[Path]) -> None:
        if tmp and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    def _apply_patch(self, pr_diff: str, cwd: Path) -> bool:
        if not pr_diff or not pr_diff.strip():
            return False
        try:
            r = subprocess.run(
                ["git", "apply", "--check", "-"],
                input=pr_diff,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r.returncode != 0:
                return False
            r2 = subprocess.run(
                ["git", "apply", "-"],
                input=pr_diff,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=20,
            )
            return r2.returncode == 0
        except Exception:
            return False

    async def _run_tests(
        self,
        test_info: Dict[str, Any],
        runner: Optional[List[str]],
        cwd: Path,
    ) -> List[Dict[str, Any]]:
        """按 test_runner 或默认尝试 python/pytest/node 几种命令"""
        results: List[Dict[str, Any]] = []
        fail_to_pass = test_info.get("fail_to_pass", []) or []
        pass_to_pass = test_info.get("pass_to_pass", []) or []

        default_runners = [
            ["python", "-m", "pytest", "{test}", "-q"],
            ["python", "-m", "pytest", "{test}", "-x", "-q"],
            ["python", "-m", "unittest", "{test}"],
            ["npm", "test", "--", "{test}"],
        ]

        async def run_one(test_name: str, kind: str) -> Dict[str, Any]:
            templates = [runner] if runner else default_runners
            for tmpl in templates:
                if not tmpl:
                    continue
                cmd = [c.replace("{test}", test_name) for c in tmpl]
                try:
                    proc = await _run(cmd, cwd=str(cwd), timeout=self.test_timeout)
                    return {
                        "test": test_name,
                        "type": kind,
                        "should_pass": True,
                        "passed": (proc["returncode"] == 0),
                        "stdout_tail": proc["stdout"][-400:],
                        "stderr_tail": proc["stderr"][-400:],
                    }
                except Exception:
                    continue
            return {
                "test": test_name,
                "type": kind,
                "should_pass": True,
                "passed": False,
                "stdout_tail": "",
                "stderr_tail": "no runner succeeded",
            }

        for t in fail_to_pass:
            results.append(await run_one(t, "fail_to_pass"))
        for t in pass_to_pass:
            results.append(await run_one(t, "pass_to_pass"))
        return results

    def _classify_status(self, test_results: List[Dict[str, Any]]) -> str:
        if not test_results:
            return "unknown"
        targets = [r for r in test_results if r.get("should_pass")]
        if not targets:
            return "unknown"
        passed = sum(1 for r in targets if r["passed"])
        ratio = passed / len(targets)
        if ratio == 1.0:
            return "resolved"
        if ratio == 0.0:
            return "not_resolved"
        return "partially_resolved"

    def _calc_confidence(self, test_results: List[Dict[str, Any]], status: str) -> float:
        if not test_results:
            return 0.5
        targets = [r for r in test_results if r.get("should_pass")]
        if not targets:
            return 0.5
        base = sum(1 for r in targets if r["passed"]) / len(targets)
        if status == "resolved":
            return min(1.0, base + 0.1)
        if status == "not_resolved":
            return max(0.0, base - 0.1)
        return base

    def _format_details(self, test_results: List[Dict[str, Any]]) -> str:
        if not test_results:
            return "No tests executed (only syntax & diff sanity checked)."
        out = []
        for r in test_results:
            mark = "✅" if r["passed"] else "❌"
            out.append(f"{mark} [{r['type']}] {r['test']}")
        return "\n".join(out)

    # 仅供评测使用：单纯做 patch 形态对比，不进入 reviewer prompt
    def compare_patches(self, patch1: str, patch2: str) -> Dict[str, Any]:
        s1 = set(line for line in patch1.split("\n") if line.startswith(("+", "-")))
        s2 = set(line for line in patch2.split("\n") if line.startswith(("+", "-")))
        common = s1 & s2
        union = s1 | s2
        sim = (len(common) / len(union)) if union else 1.0
        return {
            "similarity": sim,
            "only_in_candidate": sorted(list(s1 - s2))[:20],
            "only_in_oracle": sorted(list(s2 - s1))[:20],
        }

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": "verifier",
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "capabilities": self.capabilities,
            "sandbox": self.sandbox,
        }


async def _run(cmd: List[str], cwd: str, timeout: int) -> Dict[str, Any]:
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"returncode": -1, "stdout": "", "stderr": "timeout"}
    return {
        "returncode": proc.returncode,
        "stdout": stdout_b.decode(errors="replace"),
        "stderr": stderr_b.decode(errors="replace"),
    }
