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
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict, field
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
    build_results: List[Dict[str, Any]] = field(default_factory=list)

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
        self.build_timeout = int(self.config.get("build_timeout", 600))
        self.build_check = bool(self.config.get("build_check", True))
        self.sandbox = bool(self.config.get("sandbox", True))  # 默认沙箱运行
        self.capabilities = [
            "run_tests",
            "build_check",
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

            build_results: List[Dict[str, Any]] = []
            if self.build_check:
                checks = self._detect_build_checks(self._changed_files(pr_diff), work_repo)
                build_results = await self._run_build_checks(checks, work_repo)
                failed = next(
                    (r for r in build_results if not r["passed"] and not r.get("skipped")),
                    None,
                )
                if failed:
                    return VerificationResult(
                        passed=False,
                        test_results=[],
                        resolution_status="not_resolved",
                        confidence=0.9,
                        details=self._format_details([], build_results),
                        patch_applied=True,
                        sandbox_used=sandbox_used,
                        build_results=build_results,
                    )

            test_results: List[Dict[str, Any]] = []
            if test_info or runner:
                test_results = await self._run_tests(test_info, runner, work_repo)

            status = self._classify_status(test_results)
            confidence = self._calc_confidence(test_results, status)
            details = self._format_details(test_results, build_results)

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
                build_results=build_results,
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

    # ---- build check: 在 apply patch 之后、跑测试之前，先做编译/依赖检查 ----

    @staticmethod
    def _changed_files(pr_diff: str) -> List[str]:
        files: List[str] = []
        for line in pr_diff.splitlines():
            if line.startswith("+++ b/"):
                p = line[len("+++ b/"):].strip()
                if p and p != "/dev/null" and p not in files:
                    files.append(p)
        return files

    def _detect_build_checks(self, changed: List[str], cwd: Path) -> List[Dict[str, Any]]:
        """显式 resolve_cmd/compile_cmd 优先；否则按改动文件类型自动检测。
        iOS xcodeproj 工程无法可靠推断 workspace/scheme，须显式给 compile_cmd。"""
        checks: List[Dict[str, Any]] = []
        for key, name in (("resolve_cmd", "resolve"), ("compile_cmd", "compile")):
            raw = self.config.get(key)
            if raw:
                cmd = shlex.split(raw) if isinstance(raw, str) else list(raw)
                checks.append({"name": name, "cmd": cmd})
        if checks:
            return checks

        py = [f for f in changed if f.endswith(".py")]
        if py:
            # 内存 compile，不写 __pycache__，不污染工作区；sys.executable 保证存在
            checks.append({
                "name": "python_syntax",
                "cmd": [sys.executable, "-c",
                        "import sys,pathlib\nfor f in sys.argv[1:]:\n"
                        " compile(pathlib.Path(f).read_text(),f,'exec')"] + py,
            })

        basenames = {Path(f).name for f in changed}
        if {"Podfile", "Podfile.lock"} & basenames and shutil.which("pod"):
            # --deployment: 只校验 Podfile 与 lock 一致性，不拉 repo，秒级
            checks.append({"name": "pod_resolve", "cmd": ["pod", "install", "--deployment"]})
        if {"Package.swift", "Package.resolved"} & basenames and shutil.which("swift"):
            checks.append({"name": "spm_resolve", "cmd": ["swift", "package", "resolve"]})
        elif (any(f.endswith(".swift") for f in changed)
              and (cwd / "Package.swift").exists() and shutil.which("swift")):
            checks.append({"name": "spm_build", "cmd": ["swift", "build"]})

        if (any(f.endswith((".kt", ".java")) for f in changed)
                and (cwd / "gradlew").exists()):
            checks.append({
                "name": "gradle_compile",
                "cmd": ["./gradlew", "--console=plain", "-q", "compileDebugKotlin"],
                # 任务不存在时按顺序回退（纯 Java / 非 Android 工程）
                "fallbacks": ["compileDebugJavaWithJavac", "classes"],
            })

        if (any(f.endswith(".dart") for f in changed)
                and (cwd / "pubspec.yaml").exists()):
            tool = shutil.which("flutter") or shutil.which("dart")
            if tool:
                checks.append({"name": "pub_get", "cmd": [tool, "pub", "get"]})
                checks.append({"name": "dart_analyze", "cmd": [tool, "analyze"]})
        return checks

    async def _run_build_checks(
        self, checks: List[Dict[str, Any]], cwd: Path
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for chk in checks:
            base, task = chk["cmd"][:-1], chk["cmd"][-1]
            proc: Optional[Dict[str, Any]] = None
            used = task
            for t in [task] + chk.get("fallbacks", []):
                used = t
                try:
                    proc = await _run(base + [t], cwd=str(cwd), timeout=self.build_timeout)
                except FileNotFoundError:
                    proc = None
                    break
                output = proc["stdout"] + proc["stderr"]
                if proc["returncode"] != 0 and "not found" in output.lower():
                    continue  # gradle 任务在本工程不存在，尝试回退任务
                break
            if proc is None:
                results.append({"name": chk["name"], "cmd": " ".join(chk["cmd"]),
                                "passed": True, "skipped": True,
                                "stdout_tail": "",
                                "stderr_tail": f"tool not found: {chk['cmd'][0]}"})
                continue
            passed = proc["returncode"] == 0
            results.append({"name": chk["name"], "cmd": " ".join(base + [used]),
                            "passed": passed,
                            "stdout_tail": proc["stdout"][-400:],
                            "stderr_tail": proc["stderr"][-400:]})
            if not passed:
                break  # fail fast：依赖/编译挂了就不再往下跑
        return results

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

    def _format_details(
        self,
        test_results: List[Dict[str, Any]],
        build_results: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        out = []
        for r in build_results or []:
            mark = "⏭️" if r.get("skipped") else ("✅" if r["passed"] else "❌")
            out.append(f"{mark} [build:{r['name']}]")
        if not test_results:
            out.append("No tests executed (only syntax & diff sanity checked).")
            return "\n".join(out)
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
