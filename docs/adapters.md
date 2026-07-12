# Adapters 文档

每个 Adapter 把 LLM 调用统一收敛到 `chat(system, user) → (text, token_usage)`。

## 当前实现的 5 个

| Adapter | CLI binary | Headless 命令 | 备注 |
|--------|-----------|-------------|-----|
| `ClaudeCodeAdapter` | `claude` | `claude -p --output-format json ...` | 解析 `--output-format json` 为 token 用量 |
| `CursorAdapter`     | `agent` | `agent --print --trust ...` | Cursor 终端 agent headless |
| `OpenCodeAdapter`   | `opencode` | `opencode run ...` | 自动读 `~/.claude/skills/` |
| `PiAdapter`         | `pi` | `pi --mode print -p ... --skill <path>` | 暴露 `--skill` 自动安装 |
| `ShellTools`        | (无) | — | 占位 JSON，无 LLM 调用，仅离线测试 |

## 调用契约

```python
adapter = SomeAdapter()  # 一般无参；可接受 cli_path / model
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
