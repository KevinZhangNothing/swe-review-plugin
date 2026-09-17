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
