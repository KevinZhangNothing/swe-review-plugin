"""
swe-review CLI - 不依赖外部 LLM 库也能本地跑通

子命令：
  list-tools           列出已安装的 adapter + 健康状态
  install-skills       把 swe-review SKILL.md 安装到 ~/.pi/agent/skills/swe-review/
  review               对一段 diff 执行 review
  revise               对一段 diff 执行 revise
  loop                 执行 review_guided / best_of_n / hybrid
  verify               对一段 diff 执行 verify（沙箱 + 可选 oracle）
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional


def _read(p: Optional[str]) -> str:
    if not p or p == "-":
        return sys.stdin.read()
    return Path(p).read_text()


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


async def _cmd_list_tools(_args) -> None:
    from .tools.claude_code_adapter import ClaudeCodeAdapter
    from .tools.cursor_adapter import CursorAdapter
    from .tools.opencode_adapter import OpenCodeAdapter
    from .tools.pi_adapter import PiAdapter
    from .tools.shell_tools import ShellTools

    rows = []
    for a in (ClaudeCodeAdapter(), CursorAdapter(), OpenCodeAdapter(), PiAdapter(), ShellTools()):
        rows.append(a.get_status())
    _emit({"tools": rows})


async def _cmd_health(_args) -> None:
    """Spin up each tool (in parallel where safe) and call it once with a trivial prompt.
    Reports OK / external-error per tool so users can see what's working."""
    import asyncio
    from .tools.claude_code_adapter import ClaudeCodeAdapter
    from .tools.cursor_adapter import CursorAdapter
    from .tools.opencode_adapter import OpenCodeAdapter
    from .tools.pi_adapter import PiAdapter

    async def probe(label, adapter):
        diag = adapter.diagnose() if hasattr(adapter, "diagnose") else {}
        try:
            text, _tok = await asyncio.wait_for(
                adapter.chat(system="Reply with the single word OK and nothing else.",
                              user="OK"),
                timeout=120,
            )
            return label, "ok", text.strip()[:80], diag
        except Exception as e:
            msg = str(e).replace("\n", " ")[:200]
            return label, type(e).__name__, msg, diag

    results = await asyncio.gather(
        probe("claude-code", ClaudeCodeAdapter()),
        probe("cursor", CursorAdapter()),
        probe("opencode", OpenCodeAdapter()),
        probe("pi", PiAdapter()),
    )
    _emit({"health": [
        {"tool": label, "status": status, "sample_or_error": text, "diagnose": diag}
        for (label, status, text, diag) in results
    ]})


async def _cmd_install_skills(args) -> None:
    from .tools.pi_adapter import PiAdapter
    a = PiAdapter(skills_dir=args.pi_skills_dir, skills_source_dir=args.source)
    installed = await a.install_skills()
    _emit({"installed": installed})


async def _cmd_review(args) -> None:
    from . import ReviewSkill, ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter, ShellTools
    adapters = {
        "claude-code": ClaudeCodeAdapter(),
        "cursor": CursorAdapter(),
        "opencode": OpenCodeAdapter(),
        "pi": PiAdapter(),
        "shell": ShellTools(),
    }
    tool = adapters[args.tool]
    skill = ReviewSkill(tool_adapter=tool, prompt_style=args.prompt_style)
    res = await skill.execute(
        issue=args.issue,
        pr_title=args.pr_title or "",
        pr_diff=_read(args.pr_diff),
        pr_body=args.pr_body or "",
        repo_path=args.repo_path,
        max_exploration_steps=args.max_steps,
        prompt_style=args.prompt_style,
        deep=args.deep,
    )
    _emit(res.payload)


async def _cmd_revise(args) -> None:
    from . import ReviseSkill, ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter, ShellTools
    adapters = {
        "claude-code": ClaudeCodeAdapter(),
        "cursor": CursorAdapter(),
        "opencode": OpenCodeAdapter(),
        "pi": PiAdapter(),
        "shell": ShellTools(),
    }
    tool = adapters[args.tool]
    skill = ReviseSkill(tool_adapter=tool, prompt_style=args.prompt_style,
                          feedback_level=args.feedback_level)
    report = json.loads(_read(args.review_report))
    res = await skill.execute(
        issue=args.issue,
        original_pr_title=args.pr_title or "",
        original_pr_diff=_read(args.pr_diff),
        review_report=report,
        repo_path=args.repo_path,
        prompt_style=args.prompt_style,
        feedback_level=args.feedback_level,
    )
    _emit(res.payload)


async def _cmd_loop(args) -> None:
    from . import (
        LoopSkill, ReviewSkill, ReviseSkill, GenerateSkill, VerifySkill,
        ClaudeCodeAdapter, CursorAdapter, OpenCodeAdapter, PiAdapter, ShellTools,
    )
    adapters = {
        "claude-code": ClaudeCodeAdapter(timeout=args.timeout),
        "cursor": CursorAdapter(timeout=args.timeout),
        "opencode": OpenCodeAdapter(timeout=args.timeout),
        "pi": PiAdapter(timeout=args.timeout),
        "shell": ShellTools(),
    }
    tool = adapters[args.tool]
    review = ReviewSkill(tool_adapter=tool, prompt_style=args.prompt_style)
    revise = ReviseSkill(tool_adapter=tool, prompt_style=args.prompt_style,
                          feedback_level=args.feedback_level)
    generate = GenerateSkill(tool_adapter=tool)
    verify = VerifySkill(repo_path=args.repo_path, config=_build_check_config(args))
    loop = LoopSkill(
        review_skill=review, revise_skill=revise,
        generator_skill=generate, verify_skill=verify,
        strategy=args.strategy, max_iterations=args.max_iterations,
        output_dir=args.output_dir,
        prompt_style=args.prompt_style,
        revision_feedback_level=args.feedback_level,
    )
    initial_pr = None
    if args.initial_pr_diff:
        initial_pr = {
            "title": args.initial_pr_title or "",
            "body": args.initial_pr_body or "",
            "diff": _read(args.initial_pr_diff),
        }
    res = await loop.execute(
        issue=args.issue,
        repo_path=args.repo_path,
        initial_pr=initial_pr,
        strategy=args.strategy,
        n_best_of=args.n_best_of,
        prompt_style=args.prompt_style,
        revision_feedback_level=args.feedback_level,
    )
    _emit(res.payload)


def _build_check_config(args) -> dict:
    cfg = {
        "sandbox": getattr(args, "sandbox", True),
        "build_check": not getattr(args, "no_build_check", False),
        "build_timeout": getattr(args, "build_timeout", 600),
        "resolve_cmd": getattr(args, "resolve_cmd", None),
        "compile_cmd": getattr(args, "compile_cmd", None),
    }
    return {k: v for k, v in cfg.items() if v is not None}


def _add_build_check_args(p) -> None:
    p.add_argument("--no-build-check", action="store_true",
                   help="skip the build/dependency check phase after applying the patch")
    p.add_argument("--resolve-cmd", default=None,
                   help="explicit dependency-resolution command (overrides auto-detect), "
                        "e.g. 'pod install --deployment' or 'xcodebuild -resolvePackageDependencies ...'")
    p.add_argument("--compile-cmd", default=None,
                   help="explicit compile command (needed for iOS xcodeproj; auto-detect covers "
                        "python/gradle/flutter/SPM). Pair with --no-sandbox for incremental builds.")
    p.add_argument("--build-timeout", type=int, default=600)


async def _cmd_verify(args) -> None:
    from . import VerifySkill
    skill = VerifySkill(repo_path=args.repo_path, config=_build_check_config(args))
    test_info = None
    if args.test_info:
        test_info = json.loads(_read(args.test_info))
    oracle = _read(args.oracle) if args.oracle else None
    res = await skill.execute(
        pr_diff=_read(args.pr_diff),
        repo_path=args.repo_path,
        test_info=test_info,
        oracle=oracle,
    )
    _emit(res.payload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("swe-review")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-tools", help="list installed adapters + status")
    sub.add_parser("health", help="live-test each tool with a trivial prompt")

    p_ins = sub.add_parser("install-skills", help="copy swe-review SKILL.md dirs to Pi")
    p_ins.add_argument("--source", default="skills_md", help="source dir with SKILL.md subdirs")
    p_ins.add_argument("--pi-skills-dir", default="~/.pi/agent/skills", help="destination root")

    p_rev = sub.add_parser("review", help="review a candidate PR")
    p_rev.add_argument("--issue", required=True)
    p_rev.add_argument("--pr-title", default="")
    p_rev.add_argument("--pr-body", default="")
    p_rev.add_argument("--pr-diff", required=True, help="path or '-' for stdin")
    p_rev.add_argument("--repo-path", default=".")
    p_rev.add_argument("--tool", default="shell",
                       choices=["claude-code", "cursor", "opencode", "pi", "shell"])
    p_rev.add_argument("--max-steps", type=int, default=8)
    p_rev.add_argument("--prompt-style", default="engineering",
                       choices=["engineering", "concise", "detailed"],
                       help="engineering (default): senior code-quality review — "
                            "8-dimension scoring + P0-P4 findings + 4-level decision. "
                            "concise/detailed: legacy bug-fix-centric review.")
    p_rev.add_argument("--deep", action="store_true",
                       help="Emit the deep nested schema "
                            "(decision:{recommendation,confidence}, "
                            "defects[].location as {path,start_line,end_line}).")
    p_rev.set_defaults(handler=_cmd_review)

    p_rvs = sub.add_parser("revise", help="revise a candidate PR by review feedback")
    p_rvs.add_argument("--issue", required=True)
    p_rvs.add_argument("--pr-title", default="")
    p_rvs.add_argument("--pr-diff", required=True, help="path or '-' for stdin")
    p_rvs.add_argument("--review-report", required=True,
                       help="JSON file with {decision, defects[]} or path")
    p_rvs.add_argument("--repo-path", default=".")
    p_rvs.add_argument("--tool", default="shell",
                       choices=["claude-code", "cursor", "opencode", "pi", "shell"])
    p_rvs.add_argument("--prompt-style", default="concise",
                       choices=["engineering", "concise", "detailed"],
                       help="engineering is routed to the detailed revision prompt.")
    p_rvs.add_argument("--feedback-level", default="full_feedback",
                       choices=["full_feedback", "minimal_feedback", "baseline"],
                       help="How much feedback the reviser is given.")
    p_rvs.set_defaults(handler=_cmd_revise)

    p_loop = sub.add_parser("loop", help="Generate-Review-Revise-Verify loop")
    p_loop.add_argument("--issue", required=True)
    p_loop.add_argument("--repo-path", default=".")
    p_loop.add_argument("--strategy", default="review_guided",
                        choices=["review_guided", "best_of_n", "hybrid"])
    p_loop.add_argument("--max-iterations",
                        type=lambda v: _positive_int(v, 1, 20), default=5)
    p_loop.add_argument("--n-best-of",
                        type=lambda v: _positive_int(v, 1, 10), default=3)
    p_loop.add_argument("--output-dir", default=None)
    p_loop.add_argument("--tool", default="shell",
                        choices=["claude-code", "cursor", "opencode", "pi", "shell"])
    p_loop.add_argument("--prompt-style", default="engineering",
                        choices=["engineering", "concise", "detailed"])
    p_loop.add_argument("--feedback-level", default="full_feedback",
                        choices=["full_feedback", "minimal_feedback", "baseline"])
    p_loop.add_argument("--initial-pr-diff", default=None,
                        help="path or '-' to an existing candidate diff; "
                             "when given, the loop reviews/revises it instead of "
                             "generating a candidate first (self-review mode)")
    p_loop.add_argument("--initial-pr-title", default="")
    p_loop.add_argument("--initial-pr-body", default="")
    p_loop.add_argument("--timeout", type=int, default=600,
                        help="per-call adapter timeout in seconds; engineering "
                             "reviews take ~6-9 min, so 900+ is safer for pi")
    _add_build_check_args(p_loop)
    p_loop.set_defaults(handler=_cmd_loop)

    p_ver = sub.add_parser("verify", help="verify a patch via sandbox + tests")
    p_ver.add_argument("--pr-diff", required=True, help="path or '-' for stdin")
    p_ver.add_argument("--repo-path", default=".")
    p_ver.add_argument("--test-info", default=None,
                       help="path or '-' to JSON {fail_to_pass,pass_to_pass}")
    p_ver.add_argument("--oracle", default=None,
                       help="optional gold patch (evaluation only, never injected to review prompt)")
    p_ver.add_argument("--sandbox", dest="sandbox", action="store_true", default=True,
                       help="apply patch in tempdir (default; safe)")
    p_ver.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                       help="apply patch in the real worktree (needed for iOS incremental "
                            "builds that reuse Pods/DerivedData)")
    _add_build_check_args(p_ver)
    p_ver.set_defaults(handler=_cmd_verify)

    return p


def _positive_int(value: str, min_val: int = 1, max_val: int = 20) -> int:
    """Validate that an argument is an integer in [min_val, max_val]."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid integer")
    if ivalue < min_val or ivalue > max_val:
        raise argparse.ArgumentTypeError(
            f"'{value}' must be in range [{min_val}, {max_val}]"
        )
    return ivalue


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "list-tools":
        asyncio.run(_cmd_list_tools(args))
        return 0
    if args.cmd == "health":
        asyncio.run(_cmd_health(args))
        return 0
    if args.cmd == "install-skills":
        asyncio.run(_cmd_install_skills(args))
        return 0
    if hasattr(args, "handler"):
        asyncio.run(args.handler(args))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
