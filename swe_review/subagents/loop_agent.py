"""
Loop SubAgent - Generate → Review → (Revise | Regenerate) → Verify 闭环

Generate → Review → (Revise | Regenerate) → Verify 主流程：
- review_guided：先用初始 PR（或 generator 生成），每轮拿到 defects 后修订
- best_of_n：生成 N 个候选 + reviewer 评测，选最佳或首个 approve
- hybrid：先 best_of_n(n=3)，未 approve 时切到 review_guided

接口约定：
- 严格不接收 oracle / golden_patch / test_info 作为 reviewer/reviser context
- verifier 可以接收 oracle（仅评测用，不进 review prompt）
"""

import json
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from .reviewer_agent import DECISION_APPROVING


def _unwrap_skill(out: Any) -> Dict[str, Any]:
    """SkillResult 信封解包：execute() 返回 {ok, payload, raw, message}，
    循环编排需要的是扁平 payload。同时兼容已解包 dict / 裸 payload dict。
    判定依据：同时含 'ok' 与 dict 型 'payload' 才视为信封，避免误拆
    业务 payload 中恰好名为 'payload' 的字段。"""
    if hasattr(out, "to_dict"):
        d = out.to_dict()
    elif isinstance(out, dict):
        d = out
    else:
        return {}
    if isinstance(d, dict) and "ok" in d and isinstance(d.get("payload"), dict):
        return d["payload"]
    return d


def _failure_summary(gen: Dict[str, Any], rev: Dict[str, Any]) -> Dict[str, Any]:
    """Compact per-candidate failure memory fed to later generation waves.

    Deliberately excludes the full diff — approach rationale + review verdict
    + top findings is enough to steer the next candidate away from a dead end
    without blowing up the prompt.
    """
    findings = rev.get("findings") or rev.get("defects") or []
    tops: List[str] = []
    for f in findings[:3]:
        if isinstance(f, dict):
            sev = f.get("severity", "")
            title = f.get("title") or f.get("description", "")
            if title:
                tops.append(f"{sev}:{title}"[:160])
    return {
        "approach": (gen.get("rationale") or "")[:300],
        "review_decision": rev.get("decision", ""),
        "top_findings": tops,
    }


@dataclass
class LoopIteration:
    iteration: int
    phase: str  # "generate" | "review" | "revise" | "verify"
    decision: str
    confidence: float
    defects_count: int
    timestamp: str
    token_usage: Optional[Dict[str, int]] = None
    notes: Optional[str] = None
    # review 阶段的完整报告（findings/score 等）；聚合值之外的明细不再丢失。
    review_payload: Optional[Dict[str, Any]] = None


@dataclass
class LoopResult:
    success: bool
    final_decision: str
    final_pr_diff: str
    total_iterations: int
    iterations: List[LoopIteration] = field(default_factory=list)
    resolve_rate: float = 0.0  # 来自 verifier（如提供 oracle）
    token_usage_total: Optional[Dict[str, int]] = None
    strategy: str = "review_guided"
    elapsed_seconds: float = 0.0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LoopSubAgent:
    """闭环 SubAgent"""

    def __init__(
        self,
        review_skill: Any = None,
        revise_skill: Any = None,
        generator_skill: Any = None,
        verifier_skill: Any = None,
        max_iterations: int = 5,
        early_stop: bool = True,
        strategy: str = "review_guided",
        n_best_of: int = 3,
        output_dir: Optional[str] = None,
    ):
        self.review_skill = review_skill
        self.revise_skill = revise_skill
        self.generator_skill = generator_skill
        self.verifier_skill = verifier_skill
        self.max_iterations = max_iterations
        self.early_stop = early_stop
        self.strategy = strategy
        self.n_best_of = n_best_of
        self.output_dir = Path(output_dir) if output_dir else None
        self.name = "loop"
        self.capabilities = [
            "orchestrate_loop",
            "track_iterations",
            "early_stopping",
            "best_of_n",
            "review_guided",
        ]
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    async def execute(self, context: Dict[str, Any]) -> LoopResult:
        """统一入口"""
        t0 = datetime.now()
        # 显式剔除潜在 oracle
        for forbidden in ("golden_patch", "gold_patch", "oracle"):
            if forbidden in context:
                raise ValueError(
                    f"'{forbidden}' must not be passed to loop. "
                    "Provide it ONLY to a verifier after the loop, never to review/revise."
                )

        issue = context.get("issue", "")
        if not issue:
            raise ValueError("[LoopSubAgent] context.issue is required.")

        repo_path = context.get("repo_path")
        initial_pr = context.get("initial_pr")
        max_iter = int(context.get("max_iterations", self.max_iterations))
        strategy = context.get("strategy", self.strategy)
        n_best = int(context.get("n_best_of", self.n_best_of))
        prompt_style = context.get("prompt_style")
        feedback_level = context.get("revision_feedback_level")

        if strategy == "best_of_n":
            r = await self._run_best_of_n(issue, repo_path, n_best, t0,
                                          prompt_style=prompt_style)
        elif strategy == "hybrid":
            r = await self._run_hybrid(issue, repo_path, initial_pr, n_best, t0,
                                       max_iter=max_iter,
                                       prompt_style=prompt_style,
                                       feedback_level=feedback_level)
        else:
            r = await self._run_review_guided(issue, repo_path, initial_pr, max_iter, t0,
                                              prompt_style=prompt_style,
                                              feedback_level=feedback_level)

        r.elapsed_seconds = (datetime.now() - t0).total_seconds()
        if self.output_dir:
            self.save_log(r)
        return r

    # ==================================================================
    # Strategy: review_guided
    # ==================================================================
    async def _run_review_guided(
        self,
        issue: str,
        repo_path: Optional[str],
        initial_pr: Optional[Dict[str, Any]],
        max_iter: int,
        t0: datetime,
        prompt_style: Optional[str] = None,
        feedback_level: Optional[str] = None,
    ) -> LoopResult:
        iterations: List[LoopIteration] = []
        token_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        current_pr = initial_pr or {}

        # 如果没有 initial_pr 且有 generator，则先生成一个
        if not current_pr.get("diff") and self.generator_skill:
            gen = await self._generate(issue, repo_path)
            iterations.append(self._mk_iter(1, "generate", "n/a", gen.get("confidence", 0.5), 0,
                                            notes=gen.get("rationale", "")))
            current_pr = {
                "title": gen.get("title", ""),
                "body": gen.get("body", ""),
                "diff": gen.get("diff", ""),
            }

        for i in range(1, max_iter + 1):
            # 1) review
            rev = await self._review(issue, current_pr, repo_path,
                                     prompt_style=prompt_style)
            rev_unreliable = bool(rev.get("truncated_repair") or rev.get("parse_error"))
            it = self._mk_iter(i, "review",
                               rev.get("decision", "request_changes"),
                               rev.get("confidence", 0.5),
                               len(rev.get("defects", [])),
                               token_usage=rev.get("token_usage"),
                               notes=("untrusted review output "
                                      "(truncated_repair/parse_error); "
                                      "approve withheld") if rev_unreliable else None)
            it.review_payload = rev
            iterations.append(it)
            self._accum_tokens(token_total, rev.get("token_usage"))

            # Completely unparseable review output carries no actionable feedback;
            # blind revision cannot converge — stop early with an explicit verdict.
            # (truncated_repair alone still holds partial findings, so it goes
            # through the normal revise path instead.)
            if rev.get("parse_error"):
                return LoopResult(
                    success=False,
                    final_decision="review_unparseable",
                    final_pr_diff=current_pr.get("diff", ""),
                    total_iterations=len(iterations),
                    iterations=iterations,
                    resolve_rate=0.0,
                    token_usage_total=token_total,
                    strategy="review_guided",
                    message="review output unparseable; no actionable feedback — loop stopped",
                )

            # A truncated or unparseable-then-failed review cannot guarantee the
            # lost tail held no P0 — withhold approve and force another revision.
            if not rev_unreliable and rev.get("decision") in DECISION_APPROVING:
                # 如有 verifier，跑一次验证作为 RRR 信号
                rr = await self._verify(current_pr, repo_path)
                if rr:
                    iterations.append(self._mk_iter(i, "verify",
                                                    "approve" if rr["passed"] else "review_failed",
                                                    rr["confidence"], 0, notes=rr["details"]))
                return LoopResult(
                    success=True,
                    final_decision=rev.get("decision", "approve"),
                    final_pr_diff=current_pr.get("diff", ""),
                    total_iterations=len(iterations),
                    iterations=iterations,
                    resolve_rate=rr["resolve_rate"] if rr else 0.0,
                    token_usage_total=token_total,
                    strategy="review_guided",
                    message=f"approved at iteration {i}",
                )

            # 2) reach max, 提前 stop
            if self.early_stop and i >= max_iter:
                break

            # 3) revise
            if not self.revise_skill:
                break
            new_pr = await self._revise(issue, current_pr, rev.get("defects", []), repo_path,
                                        decision=rev.get("decision", "request_changes"),
                                        prompt_style=prompt_style,
                                        feedback_level=feedback_level)
            if not new_pr or not new_pr.get("diff"):
                why = (new_pr or {}).get("status") or "no_output"
                iterations.append(self._mk_iter(
                    i, "revise", "failed", 0.0, 0,
                    notes=f"empty diff (revise status={why})"))
                break
            iterations.append(self._mk_iter(i, "revise", "ok",
                                            0.5, len(rev.get("defects", [])), notes=new_pr.get("changes_summary", "")))
            current_pr = new_pr

        # 未通过：跑 verifier（如提供）以拿到 RRR
        rr = await self._verify(current_pr, repo_path)
        if rr:
            iterations.append(self._mk_iter(len(iterations) + 1, "verify",
                                            "approve" if rr["passed"] else "request_changes",
                                            rr["confidence"], 0, notes=rr["details"]))

        return LoopResult(
            success=False,
            final_decision="request_changes",
            final_pr_diff=current_pr.get("diff", ""),
            total_iterations=len(iterations),
            iterations=iterations,
            resolve_rate=rr["resolve_rate"] if rr else 0.0,
            token_usage_total=token_total,
            strategy="review_guided",
            message=f"max iterations reached ({max_iter})",
        )

    # ==================================================================
    # Strategy: best_of_n
    # ==================================================================
    async def _run_best_of_n(
        self,
        issue: str,
        repo_path: Optional[str],
        n: int,
        t0: datetime,
        prompt_style: Optional[str] = None,
    ) -> LoopResult:
        """Wave-based best-of-N (swarm-style).

        Wave 1: min(3, n) perspective-diverse candidates generated and reviewed
        IN PARALLEL over ONE shared exploration (previously each candidate ran
        its own explorer — pure redundancy for the same repo + issue).
        Wave 2+: serial expansion, one candidate at a time, fed a compact
        failure memory of everything rejected so far.
        Adjudication is evidence-first: an approved candidate only succeeds
        when verify passes; review confidence is used for ordering, never as
        a substitute for executable evidence.
        """
        from .generator_agent import PERSPECTIVE_ROTATION

        iterations: List[LoopIteration] = []
        token_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if not self.generator_skill:
            return LoopResult(
                success=False, final_decision="no_generator",
                final_pr_diff="", total_iterations=0, iterations=[],
                token_usage_total=token_total, strategy="best_of_n",
                message="generator_skill is required for best_of_n",
            )

        # Shared exploration: run once, reused by every candidate.
        explore_fn = getattr(self.generator_skill, "explore", None)
        exploration = (await explore_fn(issue, repo_path)) if callable(explore_fn) else None

        candidates: List[Dict[str, Any]] = []
        prior_failures: List[Dict[str, Any]] = []

        k = 0
        while k < n:
            wave = min(3, n) if k == 0 else 1
            wave = min(wave, n - k)
            gens_raw = await asyncio.gather(*[
                self._generate(
                    issue, repo_path,
                    perspective=PERSPECTIVE_ROTATION[(k + j) % len(PERSPECTIVE_ROTATION)],
                    # snapshot the memory: later waves keep appending to the
                    # live list, and a shared reference would mutate what an
                    # earlier call was recorded/awaited with
                    prior_failures=list(prior_failures) if prior_failures else None,
                    exploration=exploration,
                )
                for j in range(wave)
            ], return_exceptions=True)
            # Error isolation: one candidate's failure must not sink the wave
            # (self-review P1). Failed generates become empty candidates that
            # skip review; failed reviews become untrusted rejections.
            gens: List[Dict[str, Any]] = [
                g if not isinstance(g, Exception) else {
                    "title": "", "body": "", "diff": "",
                    "rationale": f"generate error: {g}", "confidence": 0.0,
                }
                for g in gens_raw
            ]
            prs = [{"title": g.get("title", ""), "body": g.get("body", ""),
                    "diff": g.get("diff", "")} for g in gens]

            async def _review_or_skip(pr: Dict[str, Any]) -> Dict[str, Any]:
                if not pr.get("diff"):
                    return {"decision": "request_changes", "confidence": 0.0,
                            "defects": [], "findings": [],
                            "parse_error": "skipped: empty generated diff",
                            "token_usage": None}
                return await self._review(issue, pr, repo_path,
                                          prompt_style=prompt_style)

            revs_raw = await asyncio.gather(
                *[_review_or_skip(pr) for pr in prs], return_exceptions=True)
            revs: List[Dict[str, Any]] = [
                r if not isinstance(r, Exception) else {
                    "decision": "request_changes", "confidence": 0.0,
                    "defects": [], "findings": [],
                    "parse_error": f"review error: {r}", "token_usage": None,
                }
                for r in revs_raw
            ]
            for j, (gen, pr, rev) in enumerate(zip(gens, prs, revs)):
                idx = k + j + 1
                iterations.append(self._mk_iter(
                    idx, "generate", "n/a", gen.get("confidence", 0.5), 0,
                    notes=gen.get("rationale", "")))
                rev_unreliable = bool(rev.get("truncated_repair") or rev.get("parse_error"))
                it = self._mk_iter(
                    idx, "review", rev.get("decision", "request_changes"),
                    rev.get("confidence", 0.5), len(rev.get("defects", [])),
                    token_usage=rev.get("token_usage"),
                    notes=("untrusted review output; "
                           "approve withheld") if rev_unreliable else None)
                it.review_payload = rev
                iterations.append(it)
                self._accum_tokens(token_total, rev.get("token_usage"))
                approved = (not rev_unreliable
                            and rev.get("decision") in DECISION_APPROVING)
                candidates.append({
                    "pr": pr, "idx": idx, "approved": approved,
                    "decision": rev.get("decision", "request_changes"),
                    "confidence": rev.get("confidence", 0.0),
                })
                if not approved:
                    prior_failures.append(_failure_summary(gen, rev))
            k += wave
            if any(c["approved"] for c in candidates):
                break  # progressive swarm: stop expanding once approvable

        # ---- Adjudication: evidence-first ----
        resolve_rate = 0.0
        approved = sorted((c for c in candidates if c["approved"]),
                          key=lambda c: -c["confidence"])
        for c in approved:
            rr = await self._verify(c["pr"], repo_path)
            if rr:
                iterations.append(self._mk_iter(
                    len(iterations) + 1, "verify",
                    "approve" if rr["passed"] else "review_failed",
                    rr["confidence"], 0, notes=rr["details"]))
                resolve_rate = max(resolve_rate, rr["resolve_rate"])
            if rr and rr["passed"]:
                return LoopResult(
                    success=True, final_decision=c["decision"],
                    final_pr_diff=c["pr"].get("diff", ""),
                    total_iterations=len(iterations), iterations=iterations,
                    resolve_rate=resolve_rate, token_usage_total=token_total,
                    strategy="best_of_n",
                    message=f"candidate {c['idx']}/{len(candidates)} approved+verified",
                )

        # No approved candidate survived verify. Evidence tie-break among the
        # rejected: verify the top-2 by confidence; a verify-pass is recorded
        # as the final diff but does NOT flip success (review was not passed).
        rest = sorted((c for c in candidates if not c["approved"]),
                      key=lambda c: -c["confidence"])
        best_diff = rest[0]["pr"]["diff"] if rest else ""
        for c in rest[:2]:
            if not c["pr"].get("diff"):
                continue
            rr = await self._verify(c["pr"], repo_path)
            if rr:
                iterations.append(self._mk_iter(
                    len(iterations) + 1, "verify",
                    "approve" if rr["passed"] else "request_changes",
                    rr["confidence"], 0, notes=rr["details"]))
                resolve_rate = max(resolve_rate, rr["resolve_rate"])
            if rr and rr["passed"]:
                return LoopResult(
                    success=False, final_decision="reject",
                    final_pr_diff=c["pr"]["diff"],
                    total_iterations=len(iterations), iterations=iterations,
                    resolve_rate=resolve_rate, token_usage_total=token_total,
                    strategy="best_of_n",
                    message=(f"candidate {c['idx']} verify-passed but "
                             "review-rejected; returning as evidence-ranked best"),
                )

        return LoopResult(
            success=False, final_decision="reject",
            final_pr_diff=best_diff, total_iterations=len(iterations),
            iterations=iterations, resolve_rate=resolve_rate,
            token_usage_total=token_total, strategy="best_of_n",
            message=f"no candidate approved+verified in {len(candidates)}",
        )

    # ==================================================================
    # Strategy: hybrid (best_of_n(3) → if no approve, switch to review_guided)
    # ==================================================================
    async def _run_hybrid(
        self,
        issue: str,
        repo_path: Optional[str],
        initial_pr: Optional[Dict[str, Any]],
        n: int,
        t0: datetime,
        max_iter: Optional[int] = None,
        prompt_style: Optional[str] = None,
        feedback_level: Optional[str] = None,
    ) -> LoopResult:
        n = min(n, 3)
        bon = await self._run_best_of_n(issue, repo_path, n, t0,
                                        prompt_style=prompt_style)
        if bon.success:
            bon.strategy = "hybrid"
            bon.message = f"hybrid: best_of_n approved in {n} candidates"
            return bon
        # 用 best candidate 作为初始 PR，跑 review_guided
        seed_pr = {"diff": bon.final_pr_diff, "title": "Hybrid seed", "body": ""}
        rg = await self._run_review_guided(issue, repo_path, seed_pr,
                                           max_iter or self.max_iterations, t0,
                                           prompt_style=prompt_style,
                                           feedback_level=feedback_level)
        rg.strategy = "hybrid"
        rg.iterations = bon.iterations + rg.iterations
        rg.total_iterations = len(rg.iterations)
        rg.message = "hybrid: best_of_n failed → review_guided took over"
        return rg

    # ==================================================================
    # Primitive wrappers (skill 层也可能实现)
    # ==================================================================
    async def _review(self, issue: str, pr: Dict[str, Any], repo_path: Optional[str],
                       prompt_style: Optional[str] = None) -> Dict[str, Any]:
        if not self.review_skill:
            return {"decision": "request_changes", "confidence": 0.5,
                    "defects": [], "token_usage": None}
        out = await self.review_skill.execute(
            issue=issue,
            pr_title=pr.get("title", ""),
            pr_body=pr.get("body", ""),
            pr_diff=pr.get("diff", ""),
            repo_path=repo_path,
            prompt_style=prompt_style,
        )
        # SkillResult 信封解包（修复：此前直接读信封顶层导致 decision
        # 恒为默认值 request_changes、defects 恒空、revise 恒 empty diff）
        d = _unwrap_skill(out)
        # Robustness: ensure required keys exist (both flat and nested shapes pass through)
        d.setdefault("decision", "request_changes")
        d.setdefault("confidence", 0.5)
        d.setdefault("defects", [])
        d.setdefault("token_usage", None)
        return d

    async def _revise(
        self, issue: str, pr: Dict[str, Any], defects: List[Dict[str, Any]],
        repo_path: Optional[str],
        prompt_style: Optional[str] = None,
        feedback_level: Optional[str] = None,
        decision: str = "request_changes",
    ) -> Optional[Dict[str, Any]]:
        if not self.revise_skill:
            return None
        out = await self.revise_skill.execute(
            issue=issue,
            original_pr_title=pr.get("title", ""),
            original_pr_body=pr.get("body", ""),
            original_pr_diff=pr.get("diff", ""),
            review_report={"defects": defects, "decision": decision},
            repo_path=repo_path,
            prompt_style=prompt_style,
            feedback_level=feedback_level,
        )
        d = _unwrap_skill(out)
        if d:
            return {"title": d.get("title", ""), "body": d.get("body", ""),
                    "diff": d.get("diff", ""),
                    "changes_summary": d.get("changes_summary", ""),
                    "status": d.get("status", "")}
        return None

    async def _generate(
        self, issue: str, repo_path: Optional[str],
        perspective: Optional[str] = None,
        prior_failures: Optional[List[Dict[str, Any]]] = None,
        exploration: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.generator_skill:
            return {"title": "", "body": "", "diff": "", "rationale": "", "confidence": 0.0}
        kwargs: Dict[str, Any] = {"issue": issue, "repo_path": repo_path}
        if perspective is not None:
            kwargs["perspective"] = perspective
        if prior_failures is not None:
            kwargs["prior_failures"] = prior_failures
        if exploration is not None:
            kwargs["exploration"] = exploration
        out = await self.generator_skill.execute(**kwargs)
        return _unwrap_skill(out)

    async def _verify(
        self, pr: Dict[str, Any], repo_path: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not self.verifier_skill:
            return None
        # 注意：verifier 可以接收 oracle（评测用），但不允许来自 prompt 链路
        out = await self.verifier_skill.execute(
            pr_diff=pr.get("diff", ""),
            repo_path=repo_path,
        )
        d = _unwrap_skill(out)
        status = d.get("resolution_status")
        # Without test_info the verifier can only apply the patch — that is a
        # weak PASS signal (syntax/context sanity), not a failure; labeling it
        # "review_failed" misled loop iteration records (self-loop round 5).
        passed = d.get("passed", False) or (
            d.get("patch_applied", False) and status in (None, "unknown", "")
        )
        return {
            "passed": passed,
            "confidence": d.get("confidence", 0.0) or (0.5 if passed else 0.0),
            "details": d.get("details", ""),
            "resolve_rate": 1.0 if status == "resolved" else
                           (0.5 if status == "partially_resolved" else 0.0),
        }

    # ==================================================================
    def _mk_iter(
        self, i: int, phase: str, decision: str, conf: float,
        defects_count: int,
        token_usage: Optional[Dict[str, int]] = None,
        notes: Optional[str] = None,
    ) -> LoopIteration:
        return LoopIteration(
            iteration=i,
            phase=phase,
            decision=decision,
            confidence=conf,
            defects_count=defects_count,
            timestamp=datetime.now().isoformat(),
            token_usage=token_usage,
            notes=notes,
        )

    def _accum_tokens(self, total: Dict[str, int], add: Optional[Dict[str, int]]) -> None:
        if not add:
            return
        for k, v in add.items():
            total[k] = total.get(k, 0) + (v or 0)

    def save_log(self, result: LoopResult, filename: Optional[str] = None) -> Path:
        if not self.output_dir:
            raise ValueError("output_dir not set")
        if filename is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"loop_{ts}.json"
        path = self.output_dir / filename
        with open(path, "w") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": "loop",
            "strategy": self.strategy,
            "max_iterations": self.max_iterations,
            "early_stop": self.early_stop,
            "capabilities": self.capabilities,
        }
