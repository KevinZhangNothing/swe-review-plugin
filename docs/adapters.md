# Adapters 文档

每个 Adapter 把 LLM 调用统一收敛到 `chat(system, user) → (text, token_usage)`。

## 当前实现的 6 个

| Adapter | CLI binary | Headless 命令 | 备注 |
|--------|-----------|-------------|-----|
| `ClaudeCodeAdapter` | `claude` | `claude -p --output-format json ...` | 解析 `--output-format json` 为 token 用量 |
| `CursorAdapter`     | `agent` | `agent --print --trust ...` | Cursor 终端 agent headless |
| `OpenCodeAdapter`   | `opencode` | `opencode run ...` | 自动读 `~/.claude/skills/` |
| `PiAdapter`         | `pi` | `pi --mode print --no-tools --no-skills … --system-prompt <sys> -p <user>` | 纯文本生成：`--no-tools` 加上关闭全部扩展/技能/上下文注入，避免模型在大 prompt 下输出 `<tool_call>`。SKILL.md 由 `install_skills()` 复制/软链到 `~/.pi/agent/skills/swe-review/`，**不**通过 `--skill` 旗标 |
| `HostAdapter`       | (无) | — | **不 spawn 任何 CLI**：把 prompt 写到磁盘，由「正在运行的 agent」自己回答（见下） |
| `ShellTools`        | (无) | — | 占位 JSON，无 LLM 调用，仅离线测试 |

注册表在 `swe_review/tools/__init__.py`（`ADAPTER_NAMES` / `build_adapter`），
是 CLI `--tool` 的唯一事实来源 —— 加一个 adapter 只需在那里加一行，`review` /
`revise` / `loop` 三处 `--tool` 自动跟上。

## HostAdapter：用「当前 agent」当 LLM（`--tool host`）

SKILL.md 是给宿主的指令，不是能回调宿主的代码，所以把一次 LLM 回答变成一次
磁盘往返（记忆化重放）：

```
<host-dir>/requests/<key>.json    # 待答 prompt（key = sha256(system, user) 前 16 位）
<host-dir>/responses/<key>.json   # 宿主答案 {"text": ..., "usage": {...}}（或 .txt）
```

1. `chat()` 未命中 → 写 request 并抛 `HostTurnRequired`；CLI 捕获后输出 JSON 信封并以
   **退出码 3**（`EXIT_AWAITING_HOST`）结束。
2. 宿主 agent 读 prompt，用**自己的模型**回答（不 spawn 其他 CLI），
   `swe-review host answer --key <key> --text-file <file>` 写回。
3. 原样重跑同一命令：已答 prompt 命中缓存，确定性代码重放到下一个未答 prompt。

因为 loop 最终只调 `adapter.chat()`，`LoopSubAgent` 无需任何改造 —— 三种 strategy、
早停、hard gate 全部自动兼容。适合交互式 agent 使用；无人值守（CI / 批量评测）仍用
4 个 CLI adapter（宿主 agent 在场时它们并不存在）。

```bash
swe-review review --issue "..." --pr-diff p.diff --tool host --host-dir .swe-host
swe-review host pending --host-dir .swe-host --show
swe-review host answer --key <key> --text-file answer.json   # 裸答案或 {"text":...}
swe-review review --issue "..." --pr-diff p.diff --tool host --host-dir .swe-host
```

CLI 二进制路径可用环境变量覆盖：`CLAUDE_CODE_BIN` / `CURSOR_AGENT_BIN` /
`OPENCODE_BIN` / `PI_BIN`（旧名 `CLI_BIN_*` 仍被接受）。

## 调用契约

```python
adapter = SomeAdapter()  # 一般无参；可接受 cli_path（不接受 model —— 循环不指定模型）
text, tok = await adapter.chat(system=..., user=..., max_tokens=4096, temperature=0.1)
```

- `text` 总是字符串；如果是 JSON，**剥掉** markdown ```json``` 围栏后给上层 parse。
- `tok` 总是 `{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}`。
- CLI 不直接给 token 时（比如 `--mode print` 文本输出），按字符数 /4 估算（粗近似，仅用于聚合）。

## PiAdapter 的 Skill 自动安装

```python
from swe_review import PiAdapter
a = PiAdapter(skills_source_dir="skills_md")  # 默认从根目录 skills_md/
await a.install_skills()  # 写到 ~/.pi/agent/skills/swe-review/<name>/
```

CLI 等价：`swe-review install-skills --source ...`。

## 怎么写一个新的 Adapter（假设未来要支持 Codex CLI）

```python
# swe_review/tools/codex_adapter.py
from .base import BaseAdapter
class CodexAdapter(BaseAdapter):
    name = "codex"
    async def chat(self, system, user, **kw):
        import subprocess, json, re
        proc = subprocess.run(["codex", "exec", "--quiet", f"{system}\n\n{user}"],
                              capture_output=True, text=True, timeout=600)
        text = proc.stdout
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        return (m.group(1).strip() if m else text.strip()), {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}
```

再在 `tools/__init__.py` 与 `swe_review/__init__.py` 加 export，更新 `skill-manifest.yaml` 即可。
