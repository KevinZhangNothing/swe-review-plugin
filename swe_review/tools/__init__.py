"""Tool adapters — LLM 出口 + adapter 注册表。

``ADAPTER_NAMES`` / ``build_adapter`` 是 CLI ``--tool`` 的**唯一事实来源**。
此前 ``cli.py`` 里三份 ``adapters = {...}`` dict 加三处硬编码 ``choices=[...]``
各写一遍，加一个 adapter 要改 7 个地方且极易漏；现在只需在 ``_ADAPTER_REGISTRY``
里加一行 —— ``ADAPTER_NAMES`` 直接从该表派生，两者不可能再漂移。

本文件同时补上 ``swe_review/tools/__init__.py``：此前 ``tools/`` 是 PEP 420
命名空间包，``find_packages()`` 不含它。保留本文件作为**显式**包发现 + 命名空间
卫生（与 ``subagents/`` 的处理保持一致）。注：用当前工具链（setuptools>=68）
实测构建 wheel，命名空间子包的模块**仍会被打进包**，故「wheel 丢整个 adapter 层」
在当前工具链下未能复现；保留该文件是为了不再依赖这种隐式行为。
"""

import importlib
import shutil
from typing import Any, Dict, Optional

from .host_adapter import DEFAULT_HOST_DIR  # noqa: F401  (re-exported)

#: name -> (module, class, accepts_timeout).
#: Imports stay lazy (resolved inside `build_adapter`) so `--help` / `list-tools`
#: remain cheap and a missing CLI never breaks an unrelated command.
_ADAPTER_REGISTRY: Dict[str, tuple] = {
    "claude-code": ("claude_code_adapter", "ClaudeCodeAdapter", True),
    "cursor": ("cursor_adapter", "CursorAdapter", True),
    "opencode": ("opencode_adapter", "OpenCodeAdapter", True),
    "pi": ("pi_adapter", "PiAdapter", True),
    "host": ("host_adapter", "HostAdapter", False),
    "shell": ("shell_tools", "ShellTools", False),
}

#: Every value accepted by `--tool`. Order is the order shown by `list-tools`.
ADAPTER_NAMES = tuple(_ADAPTER_REGISTRY)

#: Sentinel values that mean "pick a backend for me" rather than naming one.
#: Keeping this explicit beats `if name and name != "auto"`: a future third
#: sentinel, or a caller passing "" by accident, then falls out of the same
#: branch deliberately instead of silently sliding into detection.
#: Declared here (not near its use) because `build_adapter` rejects these values
#: with a pointed message and must not appear to accept them.
AUTO_SENTINELS = ("auto", None)

#: Adapters that spawn an external CLI. `host` (you answer) and `shell` (offline
#: placeholder) are not CLIs, so they are excluded from `health` probing.
CLI_ADAPTER_NAMES = ("claude-code", "cursor", "opencode", "pi")


def build_adapter(
    name: str,
    *,
    timeout: Optional[int] = None,
    host_dir: Optional[str] = None,
    agent: Optional[str] = None,
) -> Any:
    """Construct an adapter by registry name.

    ``timeout`` is forwarded only when given *and* the adapter accepts it, so each
    keeps its own default (claude-code/opencode 1800s, pi/cursor 600s).
    ``host_dir`` / ``agent`` apply to the host adapter only.
    """
    try:
        module_name, class_name, takes_timeout = _ADAPTER_REGISTRY[name]
    except KeyError:
        # `--tool` declares "auto" as a valid choice, but "auto" is a REQUEST to
        # choose, not an adapter — `resolve_adapter_name` must run first. Say so
        # instead of listing it among the known adapters, which would invite a
        # caller to retry with the same wrong value.
        if name in AUTO_SENTINELS:
            raise ValueError(
                "--tool auto is a request to detect a backend, not an adapter. "
                "Call resolve_adapter_name() first, or name one of: "
                + ", ".join(ADAPTER_NAMES)
            ) from None
        raise ValueError(
            f"unknown adapter {name!r}; known: {', '.join(ADAPTER_NAMES)}"
        ) from None

    module = importlib.import_module(f".{module_name}", __package__)
    kwargs: Dict[str, Any] = {}
    if timeout and takes_timeout:
        kwargs["timeout"] = timeout
    if name == "host":
        kwargs.update(work_dir=host_dir, agent=agent)
    return getattr(module, class_name)(**kwargs)


#: Preference order for `--tool auto` / the no-flag default. Mirrors
#: `CLI_ADAPTER_NAMES` — `host` and `shell` are never auto-selected: `host`
#: requires an agent to answer prompts on disk, and `shell` calls no LLM at all,
#: so picking either automatically would silently change what a review means.
AUTO_DETECT_ORDER = CLI_ADAPTER_NAMES


#: Memoized `_resolved_cli_paths()` result. Adapter construction touches the
#: filesystem while resolving (ClaudeCodeAdapter reads ~/.claude/settings.json),
#: and `resolve_adapter_name` consulted it twice per call — once via
#: `detect_adapter`, once to build the ToolNotFound advice. Cheap today (~0.8 ms)
#: but side effects should not run twice for one question.
#:
#: Concurrency note: this is a module-level write, but the write is IDEMPOTENT
#: and the computation is pure with respect to observable state — two threads
#: racing here produce the same map, and each publishes a fully-built dict (the
#: temporary is local until the final assignment). Worst case is duplicate work,
#: never a torn read. Detection runs once per process on a synchronous CLI path,
#: so a lock would be more machinery than the risk warrants; keep the assignment
#: a single statement if this ever grows.
#:
#: `None` = not resolved yet; `_reset_detection_cache()` clears it for tests and
#: for callers that install a CLI mid-process.
_CLI_PATHS_CACHE: Optional[Dict[str, str]] = None


def _reset_detection_cache() -> None:
    """Drop the memoized CLI-path map (tests, or a CLI installed mid-process)."""
    global _CLI_PATHS_CACHE
    _CLI_PATHS_CACHE = None


def _resolved_cli_paths() -> Dict[str, str]:
    """Map each auto-detectable adapter to the CLI path it would actually exec.

    One fact source: both `detect_adapter` (which one is usable) and the
    `ToolNotFound` message (what was looked for) read from here, so the advice
    can never drift from the thing that was actually probed — the failure this
    file already hit once with opencode's default-install-directory fallback.

    Memoized for the process lifetime (see `_reset_detection_cache`): the answer
    is a property of the installed environment, and re-deriving it per call ran
    every adapter's construction twice. Deliberately NOT re-probed on each call
    — a user who installs a CLI mid-run can `_reset_detection_cache()` or simply
    starts a new process.
    """
    global _CLI_PATHS_CACHE
    if _CLI_PATHS_CACHE is not None:
        return dict(_CLI_PATHS_CACHE)

    resolved: Dict[str, str] = {}
    for name in AUTO_DETECT_ORDER:
        try:
            adapter = build_adapter(name)
        except Exception:
            # A broken adapter must not abort detection for the others.
            continue
        cli = getattr(adapter, "cli_path", None)
        if cli:
            resolved[name] = cli
    _CLI_PATHS_CACHE = resolved
    return dict(resolved)


def detect_adapter() -> Optional[str]:
    """Return the first CLI adapter that would actually be able to launch.

    Detection is deliberately *availability only* — no network probe, no trial
    prompt. That keeps an auto-detected run cheap and deterministic; a binary
    that exists but is misconfigured still surfaces the adapter's own error,
    which is far more actionable than "no tool found".

    Availability is judged on the adapter's OWN resolved ``cli_path``, not on a
    hardcoded binary name. Those two disagreed: `opencode` resolves
    ``~/.opencode/bin/opencode`` when that file exists, but a copy on $PATH
    otherwise — so accepting "either the resolved path OR the bare name" could
    pick a backend whose resolved path does not run. One question, one answer:
    can the path this adapter will exec actually be found?

    Returns `None` when nothing is installed, so the caller can fail with a real
    message instead of falling back to the offline stub.
    """
    for adapter_name, cli_path in _resolved_cli_paths().items():
        if shutil.which(cli_path):
            return adapter_name
    return None


class ToolNotFound(LookupError):
    """No LLM CLI could be resolved for ``--tool auto``.

    A dedicated type rather than a bare ``ValueError`` because the CLI must
    distinguish "nothing is installed" (print advice, exit 2) from every other
    ``ValueError`` (a real bug — let it surface). Matching on the message text
    would make the exit path break silently the moment the wording changes.
    """

    def __init__(self, message: str, looked_for: tuple = ()):
        self.looked_for = looked_for
        super().__init__(message)


def resolve_adapter_name(name: Optional[str]) -> str:
    """Resolve `--tool` (including ``auto``/omitted) to a concrete adapter name.

    Two paths only: a named adapter passes through untouched; a sentinel (or an
    empty string) triggers detection. Raises `ToolNotFound` (a `LookupError`,
    not a `ValueError`) when nothing is installed — never silently falls back to
    `shell`, which returns a fixed payload and would make an unconfigured run
    look like a completed review.
    """
    if name and name not in AUTO_SENTINELS:
        return name
    detected = detect_adapter()
    if detected:
        return detected
    looked_for = tuple(_resolved_cli_paths().values()) or AUTO_DETECT_ORDER
    raise ToolNotFound(
        "no LLM CLI found on PATH (looked for: "
        + ", ".join(looked_for)
        + "). Install one, pass --tool explicitly, or use --tool host to answer "
        "the prompts with the agent running this command.",
        looked_for=looked_for,
    )
