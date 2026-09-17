"""
HostAdapter — 让「当前正在运行的 agent」自己当 LLM。

为什么需要它
------------
SKILL.md 是给宿主的**指令**，不是能回调宿主的代码：swe_review 没有任何办法同步
调用「正在运行我的那个 agent 的模型」。所以这里把一次 LLM 回答变成一次磁盘往返
（记忆化重放 / memoized replay）：

  1) ``chat(system, user)`` 用 ``sha256(len+system, len+user)`` 算出稳定 key；
  2) 命中 ``<dir>/responses/<key>.json|.txt`` → 直接返回，不 spawn 任何 CLI；
  3) 未命中 → 原子写出 ``<dir>/requests/<key>.json``，抛 ``HostTurnRequired``。

宿主 agent 的循环：

    $ swe-review loop --tool host ...        # 退出码 3：等你回答
    $ swe-review host pending --show         # 取出待答 prompt
    # 用你自己的模型作答（或派 subagent），把结果写回
    $ swe-review host answer --key <k> --text-file answer.json
    $ swe-review loop --tool host ...        # 原样重跑：已答的命中缓存，
                                             # 确定性代码重放到下一个未答 prompt

因为整个 loop 最终都只是调用 ``adapter.chat()``，``LoopSubAgent`` **一行都不用改**：
``review_guided`` / ``best_of_n`` / ``hybrid`` / 早停 / hard gate 全部自动兼容。

与 4 个 CLI adapter 的关系（并存，不是替代）
-------------------------------------------
explore / analyze / verify / loop 编排本来就是 LLM-free 的本地确定性代码；只有
review / revise / generate 需要模型。CLI adapter 服务于本 adapter 覆盖不了的两类
场景：

* **无人值守** —— CI、SWE-Bench 批量评测时没有宿主 agent 在场，只有子进程能跑；
* **上下文隔离** —— engineering system prompt 单次上万 token，塞进宿主主对话会
  污染上下文甚至撑爆窗口；独立 CLI 提供的是 fresh context。

所以 ``--tool`` 增加 ``host``，其余保持原样（默认仍是 ``shell``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Default rendezvous dir when neither --host-dir nor HostAdapter(work_dir=) is given.
DEFAULT_HOST_DIR = ".swe-host"

#: Bumped when the on-disk request/response shape changes.
FILE_VERSION = 1

_ANSWER_EXTS = (".json", ".txt")

#: prompt_key() always emits 16 lowercase hex chars. Keys reach filesystem paths
#: (answer() builds responses/<key>.json from CLI input), so anything else is
#: rejected instead of trusted — "../../x" must never become a path component.
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


class HostTurnRequired(RuntimeError):
    """宿主必须回答一个 prompt 后才能继续。

    **确定性异常**：在宿主写出 response 文件之前，重试同一个调用不可能成功。
    因此 reviewer 的 regen/retry 包装层会立刻重新抛出它（同 ``FileNotFoundError``），
    而不是白白烧掉重试次数。
    """

    def __init__(self, key: str, request_path: Path, pending_count: int = 1):
        self.key = key
        self.request_path = str(request_path)
        self.pending_count = pending_count
        super().__init__(
            f"host turn required: prompt {key} has no answer yet "
            f"(request: {self.request_path}; {pending_count} pending). "
            "Answer it with your own model, then re-run the same command."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "awaiting_host",
            "key": self.key,
            "request_path": self.request_path,
            "pending_count": self.pending_count,
        }


def prompt_key(system: str, user: str) -> str:
    """``(system, user)`` 的稳定 id：同一 prompt 在任何进程/任何次运行里都是同一 key。

    长度前缀让边界无歧义 —— ``("a","bc")`` 与 ``("ab","c")`` 不会撞 key。
    """
    h = hashlib.sha256()
    for part in (system or "", user or ""):
        raw = part.encode("utf-8")
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _estimate_usage(prompt_text: str, answer_text: str) -> Dict[str, int]:
    """chars/4 粗估 —— 与 CLI adapter 在后端不给 usage 时用的是同一套规则。

    仅用于聚合统计，不用于计费。
    """
    p = max(1, len(prompt_text or "") // 4)
    c = max(0, len(answer_text or "") // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class HostRequest:
    """一条等待宿主回答的 prompt。"""

    key: str
    system: str = ""
    user: str = ""
    max_tokens: int = 4096
    temperature: float = 0.1
    agent: str = "current-agent"
    created_at: str = ""
    path: Optional[str] = None

    @property
    def prompt_chars(self) -> int:
        return len(self.system) + len(self.user)

    def brief(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "agent": self.agent,
            "created_at": self.created_at,
            "prompt_chars": self.prompt_chars,
            "request_path": self.path,
        }

    def to_dict(self, include_prompt: bool = False) -> Dict[str, Any]:
        out = self.brief()
        if include_prompt:
            out["max_tokens"] = self.max_tokens
            out["temperature"] = self.temperature
            out["system"] = self.system
            out["user"] = self.user
        return out


class HostAdapter:
    """Answers come from the agent that is currently running — not from a
    spawned ``claude``/``pi``/``opencode``/``agent`` subprocess."""

    name = "host"

    def __init__(
        self,
        work_dir: Optional[str] = None,
        agent: Optional[str] = None,
        timeout: int = 0,
    ):
        self.work_dir = Path(work_dir or DEFAULT_HOST_DIR).expanduser()
        self.requests_dir = self.work_dir / "requests"
        self.responses_dir = self.work_dir / "responses"
        self.agent = agent or os.environ.get("SWE_REVIEW_AGENT") or "current-agent"
        # No subprocess is spawned; kept for adapter-contract symmetry.
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Adapter contract
    # ------------------------------------------------------------------
    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Tuple[str, Dict[str, int]]:
        """Memoized disk round-trip; `async` only to satisfy the BaseAdapter contract.

        The body is deliberately synchronous — it computes a key, reads a small
        JSON file, and otherwise raises. There is no I/O worth awaiting and no
        subprocess to manage, so an event loop turns nothing into an advantage
        here. Keep it this way: introducing await points would make the
        deterministic-raise contract (see `HostTurnRequired`) harder to reason
        about.
        """
        key = prompt_key(system, user)
        prompt_text = f"{system}\n{user}"

        loaded = self._load_response(key, prompt_text)
        if loaded is not None:
            return loaded

        path = self._write_request(key, system, user, max_tokens, temperature)
        raise HostTurnRequired(key, path, pending_count=len(self.pending()))

    def get_status(self) -> Dict[str, Any]:
        return self.status()

    def diagnose(self) -> Dict[str, Any]:
        out = self.status()
        out["tool"] = self.name
        out["hint"] = (
            "No external CLI is involved. Run with --tool host, then answer the "
            "pending prompts yourself (swe-review host pending --show), write each "
            "answer back (swe-review host answer --key <key> --text-file <file>), "
            "and re-run the exact same command — answered prompts are replayed from "
            "cache, so the run advances to the next unanswered prompt."
        )
        return out

    # ------------------------------------------------------------------
    # Host-side protocol (used by the CLI `host` subcommand and by agents)
    # ------------------------------------------------------------------
    def pending(self) -> List[HostRequest]:
        """Requests with no (non-empty) answer yet, oldest first."""
        out: List[HostRequest] = []
        if not self.requests_dir.is_dir():
            return out
        for path in sorted(self.requests_dir.glob("*.json")):
            key = path.stem
            if self._is_answered(key):
                continue
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                obj = {}
            if not isinstance(obj, dict):
                obj = {}
            out.append(HostRequest(
                key=key,
                system=str(obj.get("system", "")),
                user=str(obj.get("user", "")),
                # Coerce per-field so one hand-edited (or older-version) request
                # file can never break `host pending` for every other prompt
                # (P4 fix from self-review).
                max_tokens=_coerce_int(obj.get("max_tokens"), 4096),
                temperature=_coerce_float(obj.get("temperature"), 0.1),
                agent=str(obj.get("agent") or "current-agent"),
                created_at=str(obj.get("created_at") or ""),
                path=str(path),
            ))
        return out

    def answer(
        self,
        key: str,
        text: str,
        usage: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Record the host's answer for ``key`` and return the response path."""
        if not _KEY_RE.match(key or ""):
            # The key is a path component below responses/; only accept what
            # prompt_key() can emit so CLI input can never traverse out.
            raise ValueError(
                f"invalid prompt key {key!r}: expected 16 lowercase hex chars "
                "(copy it from `swe-review host pending`)"
            )
        request_path = self.requests_dir / f"{key}.json"
        if not request_path.is_file():
            # Name the rendezvous dir: the usual cause is passing a different
            # `--host-dir` to `host answer` than to the run that created the
            # prompt, and "no request at <path>" alone makes that invisible.
            raise KeyError(
                f"unknown prompt key {key!r}: no request at {request_path}. "
                f"This adapter's rendezvous dir is {self.work_dir} — check that "
                "`--host-dir` matches the run that created the prompt. "
                "Run `swe-review host pending` to list valid keys."
            )
        if not str(text or "").strip():
            raise ValueError(
                f"refusing to record an empty answer for {key!r} "
                "(an empty answer would just be re-asked)"
            )
        payload: Dict[str, Any] = {
            "version": FILE_VERSION,
            "key": key,
            "agent": self.agent,
            "answered_at": _now(),
            "text": text,
        }
        if isinstance(usage, dict):
            payload["usage"] = usage
        path = self.responses_dir / f"{key}.json"
        self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    def answer_raw(self, key: str, raw: str) -> Path:
        """Accept either the full ``{"text": ..., "usage": ...}`` envelope or the
        bare answer.

        This means the host can simply save **whatever its model returned** —
        e.g. the review JSON itself — without hand-wrapping it.

        Envelope detection is strict on purpose: a bare answer that happens to
        contain a top-level ``text`` member must NOT be mistaken for an envelope
        (that would silently drop the rest of the object). Our writer always
        emits ``text`` together with at least one of ``usage``/``version``/
        ``key`` (P3 fix from self-review).
        """
        stripped = (raw or "").strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except ValueError:
                obj = None
            if (
                isinstance(obj, dict)
                and isinstance(obj.get("text"), str)
                and any(m in obj for m in ("usage", "version", "key"))
            ):
                usage = obj.get("usage")
                return self.answer(
                    key, obj["text"], usage if isinstance(usage, dict) else None
                )
        return self.answer(key, raw or "")

    def status(self) -> Dict[str, Any]:
        requests = 0
        answered = 0
        if self.requests_dir.is_dir():
            for path in self.requests_dir.glob("*.json"):
                requests += 1
                if self._is_answered(path.stem):
                    answered += 1
        pending = self.pending()
        return {
            "name": self.name,
            "configured": True,
            "agent": self.agent,
            "work_dir": str(self.work_dir),
            "requests": requests,
            "answered": answered,
            "pending": len(pending),
            "pending_keys": [r.key for r in pending],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write via tmp + os.replace so a concurrent re-run never reads a half file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _is_answered(self, key: str) -> bool:
        for ext in _ANSWER_EXTS:
            f = self.responses_dir / f"{key}{ext}"
            try:
                if f.is_file() and f.read_text(encoding="utf-8", errors="replace").strip():
                    return True
            except OSError:
                continue
        return False

    def _load_response(
        self, key: str, prompt_text: str
    ) -> Optional[Tuple[str, Dict[str, int]]]:
        js = self.responses_dir / f"{key}.json"
        if js.is_file():
            try:
                obj = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"malformed response file {js}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"malformed response file {js}: expected a JSON object")
            text = obj.get("text")
            if not isinstance(text, str):
                raise ValueError(
                    f'malformed response file {js}: missing string field "text"'
                )
            if not text.strip():
                return None  # empty answer == still pending
            return text, self._normalize_usage(obj.get("usage"), prompt_text, text)

        txt = self.responses_dir / f"{key}.txt"
        if txt.is_file():
            try:
                text = txt.read_text(encoding="utf-8")
            except OSError:
                return None
            if not text.strip():
                return None
            return text, _estimate_usage(prompt_text, text)

        return None

    @staticmethod
    def _normalize_usage(
        usage: Any, prompt_text: str, answer_text: str
    ) -> Dict[str, int]:
        if not isinstance(usage, dict):
            return _estimate_usage(prompt_text, answer_text)
        out: Dict[str, int] = {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                out[k] = max(0, int(usage.get(k)))
            except (TypeError, ValueError):
                out[k] = 0
        if not out["total_tokens"]:
            if not out["prompt_tokens"] and not out["completion_tokens"]:
                return _estimate_usage(prompt_text, answer_text)
            out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
        return out

    def _write_request(
        self,
        key: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> Path:
        """Persist the prompt so replays never re-generate it.

        Idempotent in CONTENT, not in writes: two processes that both miss the
        cache can both write the same key, because the is_file() check and the
        write are not one atomic step. That is harmless — the bytes are
        identical and `_atomic_write` uses os.replace, so no reader ever sees a
        half-written file. (Serialising it would need O_EXCL or a lock for no
        practical gain.)
        """
        path = self.requests_dir / f"{key}.json"
        if not path.is_file():
            payload = {
                "version": FILE_VERSION,
                "key": key,
                "agent": self.agent,
                "created_at": _now(),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "user": user,
            }
            self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
        return path
