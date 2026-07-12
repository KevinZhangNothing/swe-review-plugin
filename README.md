# SWE-Review Plugin

**Agentic code review on top of Claude Code / Cursor / OpenCode / Pi.**

把"代码审查"做成 Generate → Review → (Revise | Regenerate) → Verify 的闭环：先用底层的 AI 工具生成候选 PR，再用一个 repository-grounded 的审查者探索代码、追踪调用链、产出结构化反馈；revise 拿着反馈去修；verifier 在沙箱里跑测试。可作为 4 款 CLI 的 `SKILL.md` 安装到 `~/.claude/skills/` 与 `~/.pi/agent/skills/swe-review/`。

---

## 目录

- [1. 系统架构](#1-系统架构)
- [2. 快速开始](#2-快速开始)
- [3. Skill / SubAgent / Adapter 三层](#3-skill--subagent--adapter-三层)
- [4. CLI 命令](#4-cli-命令)
- [5. 在 Claude Code / Cursor / OpenCode / Pi 中使用](#5-在-claude-code--cursor--opencode--pi-中使用)
- [6. 评测指标](#6-评测指标)
- [7. 安全约束](#7-安全约束)
- [8. 参考与许可](#8-参考与许可)

---

## 1. 系统架构

```
                ┌──────────────────────────────────┐
                │  Skills (面向 LLM 的能力包装)    │
                │   review / revise / explore /    │
                │   verify / analyze / generate /  │
                │   loop                            │
                └────────────────┬─────────────────┘
                                 │ orchestrates
                ┌────────────────▼─────────────────┐
                │  SubAgents (执行单元)            │
                │   Reviewer / Reviser / Explorer  │
                │   Verifier / Generator /        │
                │   Analyzer / Loop                │
                └────────────────┬─────────────────┘
                                 │ chat(system, user)
                ┌────────────────▼─────────────────┐
                │  Adapters (CLI 子进程执行)        │
                │   ClaudeCodeAdapter              │
                │   CursorAdapter                  │
                │   OpenCodeAdapter                │
                │   PiAdapter (auto-installs SKILL)│
                │   ShellTools                     │
                └──────────────────────────────────┘
```

闭环：

```
Issue ─▶ Generator ─▶ PR ─▶ Reviewer (Explore → LLM → JSON)
                                │ defects[] or approve
                                ▼
                          Reviser (LLM 重新生成 diff)
                                │
                                ▼
                          Verifier (sandbox + tests + optional oracle)
                                │
                                ▼
                  approve ? merge : next iteration ≤ max_iter
```

---

## 2. 快速开始

```bash
# 1. 安装
cd swe-review-plugin
./install.sh
source .env.local

# 2. 看工具可用性
swe-review list-tools

# 3. 跑一次 review
swe-review review \
    --issue "Bug description" \
    --pr-title "Fix ..." \
    --pr-diff ./sample.diff \
    --repo-path /path/to/repo \
    --tool pi

# 4. 跑 review_guided 闭环
swe-review loop \
    --issue "Bug" \
    --repo-path /path/to/repo \
    --strategy hybrid \
    --max-iterations 5

# 5. 安装 SKILL.md 到 Pi（install.sh 已经做过；这里手动重做）
swe-review install-skills
```

### 全流程测试（install → 4-CLI smoke → uninstall 闭环）

`install.sh` 提供 5 个子命令。默认是 `install`：

| 子命令 | 说明 |
|-------|------|
| `./install.sh install` | 安装 swe-review 包 + 复制 SKILL.md 到 `~/.claude/skills/` 和 `~/.pi/agent/skills/` |
| `./install.sh uninstall` | 卸载 swe-review 包 + 清除所有已安装的 SKILL.md + 删除 `.env.local` |
| `./install.sh verify` | 运行单元测试（pytest）+ `swe-review list-tools` |
| `./install.sh test-all` | `install` → 4 个 CLI smoke（按 SKILL §1 不指定 model）→ `uninstall` 闭环 |
| `./install.sh test-tool pi` | `install` → 单个 CLI 跑一次 `review` → `uninstall` 闭环 |

`test-all` / `test-tool` 输出末尾会给类似总结：

```
==== Phase 2 总结 ====
  claude-code    PASS  ok
  cursor         FAIL  RuntimeError  （Cursor 服务端 quota，与代码无关）
  opencode       PASS  ok
  pi             PASS  ok
```

每个 adapter 都提供 `diagnose()` 方法，将错误原因反馈到 `swe-review health`。

也可用作 Python 包：

```python
import asyncio
from swe_review import ReviewSkill, ClaudeCodeAdapter

async def main():
    skill = ReviewSkill(tool_adapter=ClaudeCodeAdapter())
    res = await skill.execute(
        issue="NullPointer in user_service.get",
        pr_title="Add null check",
        pr_diff=open("pr.diff").read(),
        repo_path=".",
    )
    print(res.payload)

asyncio.run(main())
```

---

## 3. Skill / SubAgent / Adapter 三层

### Skill 层（`swe_review/skill.py`）

每个 Skill 是给上游 LLM/用户调用的"一段可复用能力"，负责：
- 准备上下文（必要时委托给 explorer / analyzer）
- 触发 SubAgent
- 把 SubAgent 的结果封装成对外的 SkillResult

| Skill | 用途 | 对应 SubAgent |
|------|------|---------------|
| `ExploreSkill` | 收集仓库上下文 | `ExplorerSubAgent` |
| `AnalyzeSkill` | 静态 diff 分析 | `AnalyzerSubAgent` |
| `GenerateSkill` | 生成候选 PR | `GeneratorSubAgent` |
| `ReviewSkill`  | 审查候选 PR | `ReviewerSubAgent` (+ `ExploreSkill`) |
| `ReviseSkill`  | 按反馈修订 PR | `ReviserSubAgent` |
| `VerifySkill`  | 沙箱 + 测试验证 | `VerifierSubAgent` |
| `LoopSkill`    | 编排闭环 | `LoopSubAgent` (+上述全部) |

### SubAgent 层（`swe_review/subagents/`）

执行单元，全部有 `name/capabilities/get_status` 元信息；接收的 `context` 字典是显式 schema（拒绝 `golden_patch` / `test_info` / `oracle` 字段）。

### Adapter 层（`swe_review/tools/`）

只负责 `chat(system, user) → (text, token_usage)`。当前实现的 4 款 CLI 严格走 headless 子进程：

| Adapter | CLI | Headless |
|--------|-----|---------|
| `ClaudeCodeAdapter` | `claude` | `claude -p --output-format json ...` |
| `CursorAdapter`     | `agent` | `agent --print --trust ...` |
| `OpenCodeAdapter`   | `opencode` | `opencode run ...` |
| `PiAdapter`         | `pi` | `pi --mode print -p ... --skill <path>` |
| `ShellTools`        | (无) | 占位 JSON，仅用于离线/单元测试 |

---

## 4. CLI 命令

```
swe-review list-tools                                  # adapter 健康检查
swe-review install-skills [--source ...] [--pi-skills-dir ...]
swe-review review    --issue ... --pr-diff ... --tool pi
swe-review revise    --issue ... --pr-diff ... --review-report ...
swe-review loop      --issue ... --repo-path . --strategy hybrid
swe-review verify    --pr-diff ... [--test-info ...] [--oracle ...]
```

完整参数见 `swe-review -h`。

---

## 5. 在 Claude Code / Cursor / OpenCode / Pi 中使用

### 5.1 Claude Code / OpenCode

`./install.sh` 把每个 Skill 复制到 `~/.claude/skills/swe-review-*/`。OpenCode 默认就加载这个目录：

```
/skill swe-review-review  <args>
/skill swe-review-loop    <args>
```

### 5.2 Pi

```
/skill:swe-review-review <args>
/skill:swe-review-revise <args>
```

Pi 把 `~/.pi/agent/skills/` 当成发现根目录。`PiAdapter` 还会按需把 SKILL.md 复制到该路径下。

### 5.3 Cursor

Cursor 终端无原生 `skill` 概念，但 `cursor --print --trust` 会被 `CursorAdapter` 包成 headless 执行。建议在 Cursor 终端直接调用：

```bash
swe-review review --issue "..." --pr-diff ./fix.patch --tool cursor
```

---

## 6. 评测指标（论文 §3.1）

| Metric | 定义 | 本插件在哪收集 |
|--------|-----|---------------|
| **CR (Completion Rate)** | reviewer 输出可解析 JSON 的比例 | `ReviewerSubAgent._parse_response` 成功分支 |
| **DA (Decision Accuracy)** | approve/request-changes 与 ground-truth 一致 | `VerifierSubAgent` 提供 oracle 对照（**不进** review prompt） |
| **RRR (Resolve Rate after Revision)** | approved 后实际解决率 | `LoopSkill` 的 `resolve_rate`（= verifier 状态 → 1.0/0.5/0.0） |

注：本仓库不下载 SWE-bench 数据集；评测脚本请按你本地 SWE-Review-Bench 接入即可。

---

## 7. 安全约束

- **Reviewer 永远看不到 golden_patch / hidden tests**：`ReviewerSubAgent.execute()` 显式 `raise ValueError`。
- **Reviser / Generator 同上**：任何 `oracle / golden_patch / test_info` 注入会抛错。
- **Verifier 默认沙箱**：把 patch apply 到 `tempfile.mkdtemp()`，不污染用户工作区。
- **Diff 必须 git apply 可用**：revise 的输出会校验是否含 `diff --git` 和 `@@`。
- **Pi 端 Skill 自动安装**：`PiAdapter.install_skills()` 写到 `~/.pi/agent/skills/swe-review/`，不覆盖无关目录。

---

## 8. 参考与许可

- 论文：*SWE-Review: Closing the Loop on Issue Resolution with Agentic Code Review*（项目内 `SWE-Review-2607.06065.pdf`）
- Claude Code：`https://code.claude.com`
- Cursor：`https://cursor.com`
- OpenCode：`https://opencode.ai`
- Pi：`https://github.com/badlogic/pi-mono`
- License: MIT
