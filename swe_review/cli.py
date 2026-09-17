"""
swe-review CLI - 不依赖外部 LLM 库也能本地跑通

子命令：
  list-tools           列出已安装的 adapter + 健康状态
  install-skills       把 swe-review SKILL.md 安装到 ~/.pi/agent/skills/swe-review/
  review               对一段 diff 执行 review
  revise               对一段 diff 执行 revise
  loop                 执行 review_guided / best_of_n / hybrid
  verify               对一段 diff 执行 verify（沙箱 + 可选 oracle）
  host                 host 驱动：用「当前 agent」回答待答 prompt（pending/answer/status）

--tool host：不再 spawn claude/pi/opencode，而是把 prompt 写到磁盘、由**正在运行本
命令的 agent** 自己回答。未答的 prompt 会以退出码 3（EXIT_AWAITING_HOST）+ JSON
信封返回；答完原样重跑同一命令即可继续（已答 prompt 从缓存重放）。详见
swe_review/tools/host_adapter.py。
"""

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Optional

from .tools import ADAPTER_NAMES, CLI_ADAPTER_NAMES, DEFAULT_HOST_DIR
from .tools.host_adapter import HostTurnRequired
from .subagents.diff_sharding import (
    DEFAULT_SHARD_BUDGET_CHARS,
    DEFAULT_SHARD_CONCURRENCY,
)

#: Returned when a `--tool host` run needs the host agent to answer a prompt first.
EXIT_AWAITING_HOST = 3


def _read(p: Optional[str]) -> str:
    if not p or p == "-":
        return sys.stdin.read()
    return Path(p).read_text()


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _adapter_from_args(args):
    """Build the adapter named by `--tool` via the single-source registry."""
    from .tools import build_adapter
    return build_adapter(
        args.tool,
        timeout=getattr(args, "timeout", None),
        host_dir=getattr(args, "host_dir", None),
        agent=getattr(args, "agent", None),
    )


async def _cmd_list_tools(_args) -> None:
    from .tools import build_adapter

    rows = []
    for name in ADAPTER_NAMES:
        try:
            rows.append(build_adapter(name).get_status())
        except Exception as exc:  # a broken adapter must not break the listing
            rows.append({"name": name, "configured": False, "error": str(exc)[:200]})
    _emit({"tools": rows})


async def _cmd_health(_args) -> None:
    """Spin up each tool (in parallel where safe) and call it once with a trivial prompt.
    Reports OK / external-error per tool so users can see what's working.

    Only real CLI adapters are probed — `host` (you answer) and `shell` (offline
    placeholder) have no external process to health-check.
    """
    import asyncio
    from .tools import build_adapter

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
        *[probe(name, build_adapter(name)) for name in CLI_ADAPTER_NAMES]
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
    from . import ReviewSkill
    tool = _adapter_from_args(args)
    skill = ReviewSkill(tool_adapter=tool, prompt_style=args.prompt_style,
                        shard_large_diffs=args.shard,
                        shard_budget_chars=args.shard_budget,
                        shard_concurrency=args.shard_concurrency)
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
    from . import ReviseSkill
    tool = _adapter_from_args(args)
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
    )
    tool = _adapter_from_args(args)
    review = ReviewSkill(tool_adapter=tool, prompt_style=args.prompt_style,
                         shard_large_diffs=args.shard,
                         shard_budget_chars=args.shard_budget,
                         shard_concurrency=args.shard_concurrency)
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
    test_info = json.loads(_read(args.test_info)) if args.test_info else None
    res = await loop.execute(
        issue=args.issue,
        repo_path=args.repo_path,
        initial_pr=initial_pr,
        strategy=args.strategy,
        n_best_of=args.n_best_of,
        prompt_style=args.prompt_style,
        revision_feedback_level=args.feedback_level,
        # Verifier-only: without these the loop's verify can only check that the
        # patch applies, so resolve_rate (RRR) is structurally 0.0.
        test_info=test_info,
        test_runner=shlex.split(args.test_runner) if isinstance(args.test_runner, str)
        else args.test_runner,
    )
    _emit(res.payload)


def _host_adapter(args):
    from .tools.host_adapter import HostAdapter
    return HostAdapter(work_dir=getattr(args, "host_dir", None),
                       agent=getattr(args, "agent", None))


async def _cmd_host_pending(args) -> None:
    a = _host_adapter(args)
    items = a.pending()
    if args.key:
        items = [r for r in items if r.key == args.key]
    _emit({
        "work_dir": str(a.work_dir),
        "pending_count": len(items),
        "requests": [r.to_dict(include_prompt=args.show) for r in items],
        "instruction": (
            "Answer each request with YOUR OWN model (do not spawn claude/pi/opencode), "
            "then record it: swe-review host answer --key <key> --text-file <file>. "
            "Finally re-run the exact same command you were running."
        ),
    })


async def _cmd_host_answer(args) -> None:
    a = _host_adapter(args)
    # '-' means stdin, matching every other input flag in this CLI. Checked
    # explicitly so `--text ""` cannot silently fall through to a stdin read.
    source = args.text_file if args.text_file else args.text
    if source == "-":
        raw = sys.stdin.read()
    elif args.text_file:
        raw = Path(args.text_file).read_text()
    else:
        raw = source or ""
    usage = json.loads(args.usage_json) if args.usage_json else None
    path = a.answer(args.key, raw, usage) if usage is not None else a.answer_raw(args.key, raw)
    _emit({
        "answered": args.key,
        "response_path": str(path),
        **{k: v for k, v in a.status().items() if k != "name"},
    })


async def _cmd_host_status(args) -> None:
    a = _host_adapter(args)
    _emit(a.status())


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
    # `--test-runner` already went through `_test_runner_template` (declared as
    # argparse `type=`), which validates the `{test}` placeholder and returns a
    # template LIST. Splitting it a second time raised
    # `AttributeError: 'list' object has no attribute 'read'` on every use of the
    # flag; a raw string is tolerated only for programmatic callers that build the
    # namespace themselves.
    runner = args.test_runner
    if isinstance(runner, str):
        runner = shlex.split(runner)
    res = await skill.execute(
        pr_diff=_read(args.pr_diff),
        repo_path=args.repo_path,
        test_info=test_info,
        oracle=oracle,
        test_runner=runner or None,
    )
    _emit(res.payload)


def _add_shard_args(p) -> None:
    """Fan-out knobs for large diffs (see subagents/diff_sharding.py)."""
    p.add_argument("--no-shard", dest="shard", action="store_false", default=True,
                   help="never split a large diff: send it as ONE review request. "
                        "By default a diff larger than --shard-budget is reviewed as "
                        "concurrent file-aligned shards.")
    p.add_argument("--shard-budget", type=int, default=DEFAULT_SHARD_BUDGET_CHARS,
                   help="per-shard diff character budget (default: "
                        f"{DEFAULT_SHARD_BUDGET_CHARS}; values below 1000 are "
                        "raised to 1000)")
    p.add_argument("--shard-concurrency", type=int,
                   default=DEFAULT_SHARD_CONCURRENCY,
                   help="max concurrent shard reviews (default: "
                        f"{DEFAULT_SHARD_CONCURRENCY})")


def _add_tool_arg(p) -> None:
    """`--tool` is registry-driven (single source of truth: swe_review/tools)."""
    p.add_argument("--tool", default="shell", choices=list(ADAPTER_NAMES),
                   help="LLM backend. 'host' = answer with the agent that is "
                        "running this command (no external CLI is spawned); "
                        "'shell' = offline placeholder (no LLM).")


def _add_host_args(p) -> None:
    """Rendezvous dir + agent label for `--tool host`."""
    p.add_argument("--host-dir", default=None,
                   help="where --tool host stores requests/ and responses/ "
                        f"(default: {DEFAULT_HOST_DIR})")
    p.add_argument("--agent", default=None,
                   help="label recorded in host requests, e.g. 'cline', "
                        "'claude-code', 'opencode' (default: $SWE_REVIEW_AGENT "
                        "or 'current-agent')")


def _add_timeout_arg(p) -> None:
    """Per-call adapter timeout for `review` / `revise`.

    Omitted → the adapter keeps its own default (claude-code/opencode 1800s,
    pi/cursor 600s). An engineering review of a large diff runs 6-9 minutes,
    which sits uncomfortably close to pi's 600s default, so `--timeout 1200` is
    a sensible choice there. `loop` already declares its own `--timeout`.
    """
    p.add_argument("--timeout", type=int, default=None,
                   help="per-call adapter timeout in seconds (default: the "
                        "adapter's own — e.g. pi/cursor 600, claude-code/opencode 1800)")


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
    _add_tool_arg(p_rev)
    _add_host_args(p_rev)
    _add_timeout_arg(p_rev)
    _add_shard_args(p_rev)
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
    _add_tool_arg(p_rvs)
    _add_host_args(p_rvs)
    _add_timeout_arg(p_rvs)
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
    _add_tool_arg(p_loop)
    _add_host_args(p_loop)
    _add_shard_args(p_loop)
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
    p_loop.add_argument("--test-info", default=None,
                        help="path or '-' to JSON {fail_to_pass,pass_to_pass}; reaches the "
                             "VERIFIER only (never the review/revise prompts) and is what "
                             "makes the loop's resolve_rate meaningful")
    p_loop.add_argument("--test-runner", default=None, type=_test_runner_template,
                        help="explicit test command template containing {test}, e.g. "
                             "\"python -m pytest {test} -q\"; forwarded to the verifier")
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
    p_ver.add_argument("--test-runner", default=None, type=_test_runner_template,
                       help="explicit test command template containing {test}, "
                            "e.g. \"python -m pytest {test} -q\"; overrides the "
                            "default python/pytest/node runner chain")
    p_ver.add_argument("--oracle", default=None,
                       help="optional gold patch (evaluation only, never injected to review prompt)")
    p_ver.add_argument("--sandbox", dest="sandbox", action="store_true", default=True,
                       help="apply patch in tempdir (default; safe)")
    p_ver.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                       help="apply patch in the real worktree (needed for iOS incremental "
                            "builds that reuse Pods/DerivedData)")
    _add_build_check_args(p_ver)
    p_ver.set_defaults(handler=_cmd_verify)

    p_host = sub.add_parser(
        "host",
        help="host-driver: answer pending prompts with your own model "
             "(--tool host)")
    host_sub = p_host.add_subparsers(dest="host_cmd", required=True)

    p_hp = host_sub.add_parser("pending", help="list prompts still waiting for an answer")
    _add_host_args(p_hp)
    p_hp.add_argument("--key", default=None, help="only this prompt key")
    p_hp.add_argument("--show", action="store_true",
                      help="include the full system/user prompt text")
    p_hp.set_defaults(handler=_cmd_host_pending)

    p_ha = host_sub.add_parser("answer", help="record your answer for one prompt key")
    _add_host_args(p_ha)
    p_ha.add_argument("--key", required=True)
    src = p_ha.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", default=None,
                     help="the answer itself ('-' reads stdin)")
    src.add_argument("--text-file", default=None,
                     help="file containing the answer; may be the full "
                          '{"text":...} envelope or your model\'s raw output')
    p_ha.add_argument("--usage-json", default=None,
                      help='optional token usage, e.g. \'{"total_tokens": 12345}\'')
    p_ha.set_defaults(handler=_cmd_host_answer)

    p_hs = host_sub.add_parser("status", help="how many prompts are pending / answered")
    _add_host_args(p_hs)
    p_hs.set_defaults(handler=_cmd_host_status)

    return p


def _test_runner_template(value: str) -> list:
    """--test-runner must be a command template containing a {test} placeholder,
    e.g. "python -m pytest {test} -q" or 'pytest "tests/unit/{test}.py" -q'.

    The placeholder may be embedded inside a longer token: the verifier
    substitutes it with `part.replace("{test}", name)` per argv element, so
    `tests/unit/{test}.py` is a legitimate template.
    """
    parts = shlex.split(value)
    if not parts or not any("{test}" in part for part in parts):
        raise argparse.ArgumentTypeError(
            f"'{value}' must contain a '{{test}}' placeholder, "
            "e.g. \"python -m pytest {test} -q\"")
    return parts


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


def _awaiting_host_envelope(exc: HostTurnRequired) -> dict:
    """Machine-readable 'I need you (the host agent) to answer' payload."""
    return {
        **exc.to_dict(),
        "instruction": (
            "A prompt has no answer yet. Answer it with YOUR OWN model — do NOT "
            "spawn claude/pi/opencode. Then record the answer and re-run the exact "
            "same command; answered prompts are replayed from cache."
        ),
        "how_to": [
            f"swe-review host pending --host-dir {Path(exc.request_path).parent.parent} --show",
            "swe-review host answer --key <key> --text-file <your answer file>",
            "<re-run the same command you just ran>",
        ],
    }


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
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
    except HostTurnRequired as exc:
        # `--tool host`: this run cannot continue until the host agent answers.
        _emit(_awaiting_host_envelope(exc))
        return EXIT_AWAITING_HOST

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
