# SWE-Review Plugin 架构

## 1. 分层

```
Skills (swe_review/skill.py)
  └─ orchestrates ─▶ SubAgents (swe_review/subagents/*.py)
                       └─ chat() ─▶ Adapters (swe_review/tools/*.py)
                                         └─ subprocess ─▶ external CLI: claude / agent / opencode / pi
```

## 2. 闭环数据流（论文 §3/§4.3）

```
Issue
   │
   ▼
Generator (GenerateSkill)
   │ unified diff
   ▼
Reviewer (ReviewSkill → ReviewerSubAgent + ExplorerSubAgent)
   │ {decision, defects[]}
   ▼ (request_changes)
Reviser (ReviseSkill → ReviserSubAgent)
   │ unified diff
   ▼
Verifier (VerifySkill → VerifierSubAgent)
   │ {passed, resolution_status}
   ▼
approve ? merge : next iteration (≤ max_iterations)
```

## 3. 强约束（论文 §3.1）

- Reviewer 不得接收 `golden_patch` 或 `test_info`。三者都显式 `raise ValueError`：
  - `ReviewerSubAgent.__init__`
  - `ReviserSubAgent.__init__`
  - `GeneratorSubAgent.__init__`
- Verifier 可以接收 oracle（评测用），但**绝不**进入 review/revise 上下文。
- Verifier 默认 sandbox（`tempfile.mkdtemp`）：无效仓库或沙箱准备失败即返回失败，不回退到原仓库；已创建的临时目录在 `finally` 中清理。显式 `sandbox=False` 仍原地应用补丁。工作目录副本不提供操作系统级隔离。

## 4. 与四款 CLI 的对接

| Adapter | CLI binary | Discovery | 安装策略 |
|--------|-----------|-----------|---------|
| `ClaudeCodeAdapter` | `claude` | `~/.claude/skills/`（OpenCode 共享） | `install.sh` 复制 |
| `CursorAdapter` | `agent` | — | `install.sh` 检测 PATH |
| `OpenCodeAdapter` | `opencode` | `~/.claude/skills/` + opencode.json | 自动 |
| `PiAdapter` | `pi` | `~/.pi/agent/skills/<name>/SKILL.md` | `install.sh` 复制 + `install-skills` 子命令 |
| `HostAdapter` | (无) | — | `--tool host`：prompt 落盘，由当前 agent 自己回答（见 docs/adapters.md） |

## 5. 测试 & 验证

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio
pytest -q
```

覆盖率目标：subagent（forbidden-context 守卫必测）+ adapter construction + skill flow + verifier sandbox。
