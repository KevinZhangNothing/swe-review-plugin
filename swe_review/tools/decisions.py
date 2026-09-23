"""
decisions — 可选的 typed-decision 决策层(laya,本地 MLX 或云端网关)。

laya 是决策编码器(choice / score / noul, 1024 token 总上下文),**不是**
生成式 LLM,因此不进 adapter 注册表、不触碰「swe 循环永不指定模型」的
红线 —— 它只用于 loop 内部的短判定(revise vs regenerate 等)。

可选性契约:后端不可用、配置缺失、或推理抛错时,所有公开函数返回
``None``,调用方回落到既有启发式 —— 无决策模型的环境零行为变化。

配置文件(``SWE_DECISIONS_CONFIG`` 环境变量,默认
``~/.config/swe-review/decisions.json``)::

    {"backend": "auto"}                       # 默认:有 laya_mlx 用本地,否则有 key 用云端
    {"backend": "off"}                        # 彻底关闭
    {"backend": "local",                      # 强制本地 MLX
     "local":  {"model_id": "aac6fef/laya-multilingual-mlx"}}
    {"backend": "cloud",                      # 强制云端(经 LiteLLM 网关 passthrough)
     "cloud":  {"base_url": "http://llm.nothing.local/laya",
                "api_key_env": "LLM_GATEWAY_KEY",   # key 从该环境变量读
                "timeout": 10}}
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_CONFIG_ENV = "SWE_DECISIONS_CONFIG"
_DEFAULT_CONFIG_PATH = Path("~/.config/swe-review/decisions.json")

_LOCAL_MODEL_ID = "aac6fef/laya-multilingual-mlx"
_CLOUD_BASE_URL = "http://llm.nothing.local/laya"
_CLOUD_KEY_ENV = "LLM_GATEWAY_KEY"

#: 1024-token 上下文预算里问题与选项也要占位,state 只给 ~3000 字符
#: (云端硬上限 8000 字符,截断不报错 —— 两边取更严的)。
_STATE_CHAR_BUDGET = 3000

_backend: Optional[str] = None   # "local" | "cloud" | None(禁用/不可用)
_cfg: Dict[str, Any] = {}
_agent: Any = None               # local 后端的模型句柄(首次 predict 时才加载)
_load_attempted = False


def _load_config() -> Dict[str, Any]:
    path = os.environ.get(_CONFIG_ENV)
    p = Path(path).expanduser() if path else _DEFAULT_CONFIG_PATH.expanduser()
    try:
        cfg = json.loads(p.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _local_importable() -> bool:
    try:
        import laya_mlx  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _cloud_key(cfg: Dict[str, Any]) -> str:
    cloud = cfg.get("cloud") or {}
    return os.environ.get(cloud.get("api_key_env", _CLOUD_KEY_ENV), "")


def _resolve() -> None:
    """确定后端,只执行一次。显式后端不可用时记为 None(回落启发式),
    不做隐式降级 —— 用户选了 cloud 却连不上时,静默切到 local 更难排查。"""
    global _backend, _cfg, _load_attempted
    if _load_attempted:
        return
    _load_attempted = True
    _cfg = _load_config()
    backend = _cfg.get("backend", "auto")
    if backend == "off":
        return
    if backend == "auto":
        if _local_importable():
            _backend = "local"
        elif _cloud_key(_cfg):
            _backend = "cloud"
        return
    if backend == "local" and _local_importable():
        _backend = "local"
    elif backend == "cloud" and _cloud_key(_cfg):
        _backend = "cloud"


def backend_name() -> Optional[str]:
    _resolve()
    return _backend


def available() -> bool:
    return backend_name() is not None


def _predict(state: str, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _resolve()
    state = state[:_STATE_CHAR_BUDGET]
    if _backend == "local":
        global _agent
        if _agent is None:
            try:
                import laya_mlx as laya  # type: ignore
                model_id = (_cfg.get("local") or {}).get("model_id", _LOCAL_MODEL_ID)
                _agent = laya.load(model_id)
            except Exception:
                return None
        try:
            return _agent.predict(state, spec)["answers"]
        except Exception:
            return None
    if _backend == "cloud":
        cloud = _cfg.get("cloud") or {}
        base = cloud.get("base_url", _CLOUD_BASE_URL).rstrip("/")
        req = urllib.request.Request(
            base + "/predict",
            data=json.dumps({"state": state, "questions": spec}).encode(),
            headers={"Authorization": f"Bearer {_cloud_key(_cfg)}",
                     "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                    req, timeout=float(cloud.get("timeout", 10))) as r:
                return json.loads(r.read())["answers"]
        except Exception:
            return None
    return None


def findings_digest(review: Dict[str, Any], limit: int = 5) -> List[str]:
    """findings 的紧凑摘要(["sev:title", ...]) —— loop_agent._failure_summary
    与本模块共用,避免两处各写一遍。"""
    findings = review.get("findings") or review.get("defects") or []
    parts: List[str] = []
    for f in findings[:limit]:
        if isinstance(f, dict):
            sev = f.get("severity", "")
            title = f.get("title") or f.get("description", "")
            if title:
                parts.append(f"{sev}:{title}"[:160])
    return parts


#: 只有这些严重度才允许触发 regenerate —— 低危 finding 重生成是纯浪费。
_HIGH_SEVERITIES = ("p0", "p1", "high", "critical", "blocker")


def _has_high_severity(review: Dict[str, Any]) -> bool:
    findings = review.get("findings") or review.get("defects") or []
    for f in findings:
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in _HIGH_SEVERITIES:
            return True
    return False


def choose_revision_action(review: Dict[str, Any]) -> Optional[str]:
    """review 被拒后选 "revise" 还是 "regenerate";不可用/异常时返回 None。

    state 复用 failure-memory 的紧凑形态(decision + top findings,不含
    diff),天然适配 1024 token 上下文。
    """
    # 廉价前置护栏:无高危 finding 时 revise 显然是对的,不消费模型调用
    # (实测 laya 对 P3 级琐事也倾向 regenerate,置信度又低,不能放行)。
    if not _has_high_severity(review):
        return None
    decision = review.get("decision", "request_changes")
    digest = "; ".join(findings_digest(review))
    state = (
        f"A submitted code patch was reviewed and not approved. "
        f"Review decision: {decision}. "
        f"Findings ({len(review.get('findings') or review.get('defects') or [])}): "
        f"{digest or 'none recorded'}."
    )
    answers = _predict(state, {
        "action": {
            "type": "choice",
            "instructions": (
                "The patch needs more work. Choose 'regenerate' only when the "
                "findings show the overall approach is fundamentally wrong "
                "(wrong layer, wrong design, contradicts requirements) and "
                "starting over is cheaper than patching; choose 'revise' when "
                "the approach is sound and the findings are fixable by "
                "targeted edits."
            ),
            "criteria": ["revise", "regenerate"],
        },
    })
    if not answers:
        return None
    # choice 型答案是嵌套 dict:{..., 'choice': 'revise', 'probabilities': {...}}
    raw = answers.get("action")
    action = raw.get("choice") if isinstance(raw, dict) else raw
    return action if action in ("revise", "regenerate") else None
