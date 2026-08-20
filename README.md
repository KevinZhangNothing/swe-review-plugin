# SWE-Review Plugin

**Agentic code review on top of Claude Code / Cursor / OpenCode / Pi.**

SWE-Review 把"代码审查"做成 **Generate → Review → (Revise | Regenerate) → Verify** 的闭环：先让 AI 生成候选 PR，再用一个 repository-grounded 审查者探索代码、追踪调用链、产出结构化反馈；reviser 拿着反馈去修；verifier 在沙箱里跑测试。可作为 4 款 CLI 的 `SKILL.md` 安装到 `~/.claude/skills/` 与 `~/.pi/agent/skills/swe-review/`。

---

## 目录

- [1. 系统架构](#1-系统架构)
- [2. 闭环流程](#2-闭环流程)
- [3. 数据模型](#3-数据模型)
- [4. 快速开始](#4-快速开始)
- [5. Skill / SubAgent / Adapter 三层](#5-skill--subagent--adapter-三层)
- [6. CLI 命令](#6-cli-命令)
- [7. 在 Claude Code / Cursor / OpenCode / Pi 中使用](#7-在-claude-code--cursor--opencode--pi-中使用)
- [8. 评测指标](#8-评测指标)
- [9. 安全约束与不变式](#9-安全约束与不变式)
- [10. 参考与许可](#10-参考与许可)

---

## 1. 系统架构

### 三层次总览

```mermaid
flowchart TB
    subgraph User["👤 User / LLM Agent"]
        CLI["swe-review CLI<br/><i>argparse → asyncio</i>"]
        SK["/skill swe-review-*<br/><i>Claude Code / OpenCode / Pi</i>"]
    end

    subgraph Skills["🧠 Skill Layer (skill.py)"]
        direction LR
        ES[ExploreSkill<br/><i>收集仓库上下文</i>]
        AS[AnalyzeSkill<br/><i>静态 diff 分析</i>]
        GS[GenerateSkill<br/><i>生成候选 PR</i>]
        RS[ReviewSkill<br/><i>审查候选 PR</i>]
        RVS[ReviseSkill<br/><i>按反馈修订</i>]
        VS[VerifySkill<br/><i>沙箱验证</i>]
        LS[LoopSkill<br/><i>编排闭环</i>]
    end

    subgraph SubAgents["⚙️ SubAgent Layer (subagents/)"]
        direction LR
        E[ExplorerSubAgent<br/><i>grep + 调用链追踪</i>]
        A[AnalyzerSubAgent<br/><i>hunk 统计 + API 检测</i>]
        G[GeneratorSubAgent<br/><i>LLM 生成 diff</i>]
        R[ReviewerSubAgent<br/><i>LLM 审查 + JSON 报告</i>]
        RV[ReviserSubAgent<br/><i>LLM 修订 diff</i>]
        V[VerifierSubAgent<br/><i>git apply + 测试</i>]
        L[LoopSubAgent<br/><i>状态机 + 策略分发</i>]
    end

    subgraph Adapters["🔌 Adapter Layer (tools/)"]
        CCA[ClaudeCodeAdapter<br/><i>claude -p</i>]
        CA[CursorAdapter<br/><i>agent --print</i>]
        OA[OpenCodeAdapter<br/><i>opencode run</i>]
        PA[PiAdapter<br/><i>pi --mode print</i>]
        ST[ShellTools<br/><i>离线占位</i>]
    end

    User --> CLI & SK
    CLI --> Skills
    SK --> Skills
    Skills --> SubAgents
    SubAgents -.->|chat / call LLM| Adapters
```

### 数据流

```mermaid
flowchart LR
    subgraph Input["📥 输入"]
        I[Issue 描述]
        D[PR diff]
        R[仓库路径]
    end

    subgraph Flow["🔄 处理流"]
        direction TB
        EXP[Explorer<br/><i>grep 关键词<br/>追踪调用链<br/>读取源文件</i>]
        ANL[Analyzer<br/><i>统计 hunk<br/>检测 public API<br/>计算复杂度</i>]
        REV[Reviewer<br/><i>LLM 审查<br/>输出 structured JSON</i>]
        FIX[Reviser<br/><i>LLM 修订<br/>生成新 diff</i>]
        VER[Verifier<br/><i>git apply<br/>运行测试<br/>可选 oracle 对比</i>]
    end

    subgraph Output["📤 输出"]
        O1["ReviewReport<br/><i>decision + defects list</i>"]
        O2[RevisedPR<br/><i>新 diff</i>]
        O3[VerificationResult<br/><i>pass/fail + 测试结果</i>]
        O4[LoopResult<br/><i>迭代记录 + token 用量</i>]
    end

    Input --> Flow
    Flow --> Output
```

---

## 2. 闭环流程

SWE-Review 提供 **3 种闭环策略**，由 `LoopSkill` 统一编排：

### 2.1 策略总览

```mermaid
flowchart TB
    START([Issue]) --> CHOOSE{strategy?}

    CHOOSE -->|review_guided| RG["Review‑Guided<br/><i>生成 → 审查 → 修订 → 验证</i>"]
    CHOOSE -->|best_of_n| BON["Best‑of‑N<br/><i>生成 N 个 → 审查每个 → 选最优</i>"]
    CHOOSE -->|hybrid| HY["Hybrid<br/><i>先 best_of_n(3)<br/>未通过 → review_guided</i>"]

    RG --> DONE([Done: LoopResult])
    BON --> DONE
    HY --> DONE
```

### 2.2 Review-Guided（默认）

```mermaid
flowchart TB
    START([Issue]) --> GEN{有 initial_pr?}
    GEN -->|No| GEN1[GeneratorSubAgent<br/><i>LLM 生成候选 diff</i>]
    GEN -->|Yes| REVIEW
    GEN1 --> REVIEW

    REVIEW[ReviewerSubAgent<br/><i>Explore → LLM → ReviewReport</i>]

    REVIEW --> APPROVED{decision?}

    APPROVED -->|approve / approve_with_suggestions| VERIFY[VerifierSubAgent<br/><i>sandbox + git apply + tests</i>]
    APPROVED -->|request_changes / block| MAX2{i = max_iter?}

    VERIFY --> PASSED{passed?}
    PASSED -->|Yes| SUCCESS([✅ APPROVED])
    PASSED -->|No| MAX2

    MAX2 -->|No| REVISE["ReviserSubAgent<br/><i>LLM 按 defects list 修订</i>"]
    MAX2 -->|Yes| FAIL([❌ REJECTED<br/>max iterations])

    REVISE --> REVISED{新 diff 有效?}
    REVISED -->|Yes| REVIEW
    REVISED -->|No| FAIL
```

### 2.3 Best-of-N

```mermaid
flowchart LR
    START([Issue]) --> LOOP["for k = 1..N<br/>----------------<br/>GeneratorSubAgent → 候选 diff"]
    LOOP --> REVIEW_EACH[ReviewerSubAgent<br/>审查每个候选]
    REVIEW_EACH --> CHECK{approved?}

    CHECK -->|Yes| APPROVE_IMMED([✅ 立即 approve])
    CHECK -->|No| NEXT{k < N?}

    NEXT -->|Yes| LOOP
    NEXT -->|No| PICK_BEST["选 confidence 最高的<br/>VerifierSubAgent 验证"]

    PICK_BEST --> DONE([Done])
```

### 2.4 Hybrid

```mermaid
flowchart TB
    START([Issue]) --> BON3["Best‑of‑N (n=3)"]
    BON3 --> BON_OK{approved?}

    BON_OK -->|Yes| DONE([✅ 完成])
    BON_OK -->|No| SEED["取最佳候选作为 seed_pr"]
    SEED --> RG_FALLBACK["Review‑Guided(seed_pr, max_iter)"]
    RG_FALLBACK --> DONE
```

### 2.5 完整序列（review_guided 策略）

```mermaid
sequenceDiagram
    participant U as User / CLI
    participant LS as LoopSubAgent
    participant GS as GeneratorSubAgent
    participant RS as ReviewerSubAgent
    participant ES as ExplorerSubAgent
    participant AS as AnalyzerSubAgent
    participant RV as ReviserSubAgent
    participant VS as VerifierSubAgent

    U->>LS: execute(issue, repo_path, initial_pr, max_iter=5)

    alt initial_pr 为空且有 generator
        LS->>GS: generate(issue, repo_path)
        GS-->>LS: GeneratedPR{diff, title, confidence}
    end

    Note over LS: token_total = {prompt:0, completion:0, total:0}

    loop i = 1..max_iter
        LS->>RS: review(issue, current_pr, repo_path)

        rect rgb(240, 248, 255)
            Note over RS: Reviewer 内部
            RS->>ES: explore(repo_path, issue, pr_diff, max_steps)
            ES-->>RS: ExplorationResult{files, contents, call_chain}
            RS->>AS: analyze(pr_diff)
            AS-->>RS: AnalyzerResult{complexity, api_changes}
            RS->>RS: _call_ai_tool(system_prompt, user_prompt)
        end

        RS-->>LS: ReviewReport{decision, confidence, defects[]}
        LS->>LS: _accum_tokens()

        alt decision == "approve"
            LS->>VS: verify(current_pr, repo_path)
            VS->>VS: _apply_patch(git apply --check)
            VS->>VS: _run_tests(fail_to_pass, pass_to_pass)
            VS-->>LS: VerificationResult{passed, resolve_rate}

            alt passed
                LS-->>U: LoopResult{success:True, final_decision:"approve"}
            else
                Note over LS: 继续下一轮
            end

        else decision == "request_changes"

            alt i >= max_iter 且 early_stop
                LS-->>U: LoopResult{success:False, "max iterations"}
            else
                LS->>RV: revise(issue, current_pr, defects[], repo_path)
                RV-->>LS: RevisedPR{new diff, changes_summary}
                Note over LS: current_pr = new diff
            end
        end
    end
```

---

## 3. 数据模型

核心 dataclass 关系：

```mermaid
classDiagram
    class LoopResult {
        +bool success
        +str final_decision
        +str final_pr_diff
        +int total_iterations
        +List~LoopIteration~ iterations
        +float resolve_rate
        +Dict token_usage_total
        +str strategy
        +float elapsed_seconds
        +str message
    }

    class LoopIteration {
        +int iteration
        +str phase
        +str decision
        +float confidence
        +int defects_count
        +str timestamp
        +Dict token_usage
        +str notes
    }

    class ReviewReport {
        +str decision
        +float confidence
        +Dict summary
        +List~Defect~ defects
        +List~Finding~ findings
        +Dict scores
        +float total_score
        +Dict hard_gate
        +str raw_response
        +str timestamp
        +Dict token_usage
        +int exploration_steps
        +str prompt_style
    }

    class Finding {
        +str severity
        +str title
        +Any location
        +str observation
        +str why_it_matters
        +str evidence
        +str recommendation
        +str confidence
    }

    class Defect {
        +str severity
        +str description
        +Any location
        +str suggestion
        +str category
    }

    class VerificationResult {
        +bool passed
        +List~Dict~ test_results
        +str resolution_status
        +float confidence
        +str details
        +bool patch_applied
        +bool sandbox_used
        +float oracle_similarity
    }

    class ExplorationResult {
        +str repo_path
        +List~str~ files_modified
        +List~str~ related_files
        +List~str~ test_files
        +Dict~str,str~ file_contents
        +List~Dict~ call_chain
        +List~str~ keywords
        +List~str~ root_hint
        +int steps
        +bool truncated
    }

    class AnalyzerResult {
        +int total_hunks
        +int total_additions
        +int total_deletions
        +List~str~ files_changed
        +List~str~ public_api_changes
        +List~Dict~ suspicious_spots
        +float complexity_score
    }

    LoopResult *-- LoopIteration
    ReviewReport *-- Defect
    ReviewReport ..> ExplorationResult : depends on
    ReviewReport ..> AnalyzerResult : depends on
```

---

## 4. 快速开始

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

### 完整测试周期

`install.sh` 提供 5 个子命令。默认是 `install`：

| 子命令 | 说明 |
|-------|------|
| `./install.sh install` | 安装 swe-review 包 + 复制 SKILL.md 到 `~/.claude/skills/` 和 `~/.pi/agent/skills/` |
| `./install.sh uninstall` | 卸载 swe-review 包 + 清除所有已安装的 SKILL.md + 删除 `.env.local` |
| `./install.sh verify` | 运行单元测试（pytest）+ `swe-review list-tools` |
| `./install.sh test-all` | `install` → 4 个 CLI smoke → `uninstall` 闭环 |
| `./install.sh test-tool pi` | `install` → 单个 CLI 跑一次 `review` → `uninstall` 闭环 |

`test-all` 输出末尾会给出各 adapter 的健康状态：

```
==== Phase 2 总结 ====
  claude-code    PASS  ok
  cursor         FAIL  RuntimeError  （Cursor 服务端 quota，与代码无关）
  opencode       PASS  ok
  pi             PASS  ok
```

每个 adapter 都提供 `diagnose()` 方法，将错误原因反馈到 `swe-review health`。

### Python API

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
    print(res.payload)  # → SkillResult {ok, payload: ReviewReport.to_dict()}

asyncio.run(main())
```

---

## 5. Skill / SubAgent / Adapter 三层

### 5.1 Skill 层（`swe_review/skill.py`）

每个 Skill 是给上游 LLM/用户调用的"一段可复用能力"，负责准备上下文、触发 SubAgent、封装对外结果。

```mermaid
flowchart LR
    subgraph Skills["7 个 Skill"]
        ES2[ExploreSkill]
        AS2[AnalyzeSkill]
        GS2[GenerateSkill]
        RS2[ReviewSkill]
        RVS2[ReviseSkill]
        VS2[VerifySkill]
        LS2[LoopSkill]
    end

    subgraph Delegates["委托 SubAgent"]
        E2[ExplorerSubAgent]
        A2[AnalyzerSubAgent]
        G2[GeneratorSubAgent]
        R2[ReviewerSubAgent]
        RV2[ReviserSubAgent]
        V2[VerifierSubAgent]
        L2[LoopSubAgent]
    end

    ES2 --> E2
    AS2 --> A2
    GS2 --> G2
    RS2 -.->|+ ExploreSkill| R2
    RS2 -.->|+ AnalyzeSkill| R2
    RVS2 --> RV2
    VS2 --> V2
    LS2 -->|orchestrates| L2
    L2 -.->|delegates to| RS2
    L2 -.->|delegates to| RVS2
    L2 -.->|delegates to| GS2
    L2 -.->|delegates to| VS2
```

| Skill | 用途 | 对应 SubAgent | 额外依赖 |
|------|------|---------------|----------|
| `ExploreSkill` | 收集仓库上下文 | `ExplorerSubAgent` | — |
| `AnalyzeSkill` | 静态 diff 分析 | `AnalyzerSubAgent` | — |
| `GenerateSkill` | 基于 issue 生成候选 PR | `GeneratorSubAgent` | `ExploreSkill` |
| `ReviewSkill` | 审查候选 PR | `ReviewerSubAgent` | `ExploreSkill` + `AnalyzeSkill` |
| `ReviseSkill` | 按审查反馈修订 PR | `ReviserSubAgent` | — |
| `VerifySkill` | 沙箱 apply + 测试 | `VerifierSubAgent` | — |
| `LoopSkill` | 编排闭环 | `LoopSubAgent` | 上述全部 |

#### Reviewer prompt styles（`prompt_style`）

| style | 定位 |
|---|---|
| `engineering`（默认） | **高级代码审查**：审查对象是 Change 而非 Bug。8 维度 100 分制评分（Design 20 / Maintainability 15 / Consistency 15 / Simplicity 10 / Readability 10 / Testability 10 / Risk 10 / Change Scope 10）+ P0–P4 证据绑定 findings + Hard Gate + 4 档决策（APPROVE / APPROVE_WITH_SUGGESTIONS / REQUEST_CHANGES / BLOCK）。先理解再评价，Project Convention > Generic Best Practice，禁止低价值评论。 |
| `concise` | legacy bug-fix-centric：判断 patch 是否修复 issue 根因。 |
| `detailed` | legacy bug-fix-centric：Step 1→6 workflow + symptom-fix detection。 |

engineering 报告同时把 findings 映射为 legacy `defects`（P0/P1→high，P2→medium，P3/P4→low），revise/loop 下游无需改动；`approve_with_suggestions` 与 `approve` 一样终止闭环为成功，`block`（Hard Gate / P0）与 `request_changes` 触发修订。

### 5.2 SubAgent 层（`swe_review/subagents/`）

执行单元，全部有 `name / capabilities / get_status` 元信息。

| SubAgent | capabilities | 核心方法 |
|----------|-------------|----------|
| `ExplorerSubAgent` | `file_search, code_analysis, dependency_tracking, test_discovery` | `execute(context)` → `ExplorationResult` |
| `AnalyzerSubAgent` | `static_diff_analysis, api_surface_check, complexity_estimate` | `execute(context)` → `AnalyzerResult` |
| `GeneratorSubAgent` | `generate_patch, diff_formatting` | `execute(context)` → `GeneratedPR` |
| `ReviewerSubAgent` | `analyze_diff, explore_repository, generate_report, decision_making` | `execute(context)` → `ReviewReport` |
| `ReviserSubAgent` | `parse_feedback, generate_fix, validate_diff` | `execute(context)` → `RevisedPR` |
| `VerifierSubAgent` | `run_tests, compare_patches, verify_resolution, sandbox_apply` | `execute(context)` → `VerificationResult` |
| `LoopSubAgent` | `orchestrate_loop, track_iterations, early_stopping, best_of_n` | `execute(context)` → `LoopResult` |

所有 SubAgent 接收的 `context` 字典是显式 schema —— **拒绝** `golden_patch`、`test_info`、`oracle` 等污染字段（验证器除外）。

### 5.3 Adapter 层（`swe_review/tools/`）

只负责 `chat(system, user) → (text, token_usage)`。所有 adapter 共享 `BaseAdapter` 接口：

```python
class BaseAdapter:
    async def chat(self, system: str, user: str,
                   max_tokens: int = 4096, temperature: float = 0.1
                   ) -> Tuple[str, Dict[str, int]]: ...
    def get_status(self) -> Dict[str, Any]: ...
    def diagnose(self) -> Dict[str, Any]: ...
```

```mermaid
flowchart TB
    subgraph Adapters["5 个 Adapter"]
        CCA["ClaudeCodeAdapter<br/><i>子进程: claude -p</i>"]
        CA["CursorAdapter<br/><i>子进程: agent --print --trust</i>"]
        OA["OpenCodeAdapter<br/><i>子进程: opencode run</i>"]
        PA["PiAdapter<br/><i>子进程: pi --mode print -p</i>"]
        ST["ShellTools<br/><i>离线 JSON 占位<br/>无 LLM 调用</i>"]
    end

    subgraph Common["公共基础设施"]
        BP[_pty_runner.py<br/><i>PTY 子进程管理</i>]
        SB[shelL_tools.py<br/><i>shell 命令工具</i>]
    end

    Adapters --> Common
```

| Adapter | CLI 二进制 | Headless 模式 | 技能自动安装 |
|---------|-----------|---------------|-------------|
| `ClaudeCodeAdapter` | `claude` | `claude -p --output-format json` | ❌ |
| `CursorAdapter` | `agent` | `agent --print --trust` | ❌ |
| `OpenCodeAdapter` | `opencode` | `opencode run` | ❌ |
| `PiAdapter` | `pi` | `pi --mode print -p --skill <path>` | ✅ → `~/.pi/agent/skills/` |
| `ShellTools` | (无) | 返回占位 JSON | ❌ |

---

## 6. CLI 命令

```
swe-review list-tools                                  # 列出 adapter 健康状况
swe-review health                                      # 每个 adapter 发一次 trivial prompt
swe-review install-skills [--source ...] [--pi-skills-dir ...]

swe-review review    --issue ... --pr-diff ... \
                     [--tool pi] [--prompt-style engineering|concise|detailed] [--deep]

swe-review revise    --issue ... --pr-diff ... \
                     --review-report ... [--feedback-level full_feedback]

swe-review loop      --issue ... --repo-path . \
                     [--strategy hybrid] [--max-iterations 5] [--n-best-of 3]

swe-review verify    --pr-diff ... \
                     [--repo-path .] [--sandbox] [--test-info ...] [--oracle ...]
```

完整参数见 `swe-review -h`。

### CLI 层架构

```mermaid
flowchart LR
    ARGV["sys.argv"] --> PARSER[argparse]
    PARSER --> DISPATCH{cmd}

    DISPATCH -->|list-tools| CMD_LT["列出 adapter 状态"]
    DISPATCH -->|health| CMD_H["运行健康检测"]
    DISPATCH -->|review| CMD_R["ReviewSkill.execute()"]
    DISPATCH -->|revise| CMD_RV["ReviseSkill.execute()"]
    DISPATCH -->|loop| CMD_L["LoopSkill.execute()"]
    DISPATCH -->|verify| CMD_V["VerifySkill.execute()"]

    CMD_R --> EMIT[json.dumps → stdout]
    CMD_RV --> EMIT
    CMD_L --> EMIT
    CMD_V --> EMIT
```

---

## 7. 在 Claude Code / Cursor / OpenCode / Pi 中使用

### 7.1 Claude Code / OpenCode

`./install.sh` 把每个 Skill 复制到 `~/.claude/skills/swe-review-*/`（软链接到 `~/.agents/skills/`）。OpenCode 默认加载该目录：

```
/skill swe-review-review  --issue "..." --pr-diff ./patch.diff
/skill swe-review-loop    --issue "..." --repo-path . --strategy hybrid
```

### 7.2 Pi

```
/skill:swe-review-review --issue "..." --pr-diff ./patch.diff
/skill:swe-review-revise --issue "..." --pr-diff ./patch.diff --review-report ./report.json
```

Pi 把 `~/.pi/agent/skills/` 当成发现根目录。`PiAdapter` 还会按需把 SKILL.md 复制到该路径下。

### 7.3 Cursor

Cursor 终端无原生 `skill` 概念，但 `agent --print --trust` 会被 `CursorAdapter` 包装成 headless 执行。建议在 Cursor 终端直接调用：

```bash
swe-review review --issue "..." --pr-diff ./fix.patch --tool cursor
```

### Skill 发现路径

```mermaid
flowchart LR
    subgraph Install["install.sh 安装"]
        SRC["项目 .claude/skills/<br/>swe-review-*/SKILL.md"] -->|cp -R| CC["~/.claude/skills/<br/>swe-review-*/<br/>← Claude Code / OpenCode"]
        SRC -->|cp -R| PI["~/.pi/agent/skills/<br/>swe-review/swe-review-*/<br/>← Pi Agent"]
    end

    subgraph Runtime["运行时发现"]
        CC -->|自动加载| CLAUDE["Claude Code<br/>↳ /skill swe-review-*"]
        CC -->|自动加载| OPENCODE["OpenCode<br/>↳ /skill swe-review-*"]
        PI -->|自动发现| PI_AGENT["Pi Agent<br/>↳ /skill:swe-review-*"]
    end
```

---

## 8. 评测指标

| Metric | 定义 | 本插件在哪收集 |
|--------|------|---------------|
| **CR (Completion Rate)** | reviewer 输出可解析 JSON 的比例 | `ReviewerSubAgent._parse_response` 成功分支 |
| **DA (Decision Accuracy)** | approve/request-changes 与 ground-truth 一致 | `VerifierSubAgent` 提供 oracle 对照（**不进** review prompt） |
| **RRR (Resolve Rate after Revision)** | approved 后实际解决率 | `LoopSkill` 的 `resolve_rate`（= verifier 状态 → 1.0 / 0.5 / 0.0） |

### 评测模式的数据隔离

```mermaid
flowchart LR
    subgraph Clean["🟢 干净链路（review / revise）"]
        RV_CTX["Reviewer context<br/>{issue, pr_diff, repo_path}"]
        REV_CTX["Reviser context<br/>{issue, pr_diff, review_report}"]
    end

    subgraph Oracle["🔶 Oracle 仅在验证器"]
        V_CTX["Verifier context<br/>{pr_diff, oracle, test_info}"]
    end

    RV_CTX -->|❌ blocked| ORACLE_IN_RV["golden_patch<br/>test_info<br/>oracle"]
    ORACLE_IN_RV --> ERR["ValueError"]

    ORACLE --> VERIFIER_ONLY["仅用于评分<br/>不进 LLM prompt"]
```

注：本仓库不下载 SWE-bench 数据集；评测脚本请按你本地 SWE-Review-Bench 接入即可。

---

## 9. 安全约束与不变式

### 不变式矩阵

| 不变式 | 执行者 | 违反后果 |
|--------|--------|---------|
| `golden_patch` 不得出现在 review 上下文 | `ReviewerSubAgent.execute()` | `raise ValueError` |
| `oracle` / `test_info` 不得出现在 review 上下文 | `ReviewerSubAgent.execute()` | `raise ValueError` |
| `golden_patch` / `oracle` 不得出现在 revise 上下文 | `ReviserSubAgent.execute()` | `raise ValueError` |
| `golden_patch` / `oracle` 不得出现在 loop 上下文 | `LoopSubAgent.execute()` | `raise ValueError` |
| diff 必须可通过 `git apply` 校验 | `ReviserSubAgent._parse_response()` | `status = "failed"` |
| Verifier 默认使用沙箱 | `VerifierSubAgent.__init__(sandbox=True)` | 污染用户工作区 |
| Reviewer 输出必须是可解析 JSON | `ReviewerSubAgent._parse_response()` | fallback `request_changes` |

### Verifier 沙箱流程

```mermaid
flowchart TB
    START([VerifySkill.execute]) --> SANDBOX{sandbox=True?}
    SANDBOX -->|Yes| MKTMP["mkdtemp(prefix=swe-review-)"]
    MKTMP --> COPY["shutil.copytree(repo → tmp/repo)<br/>ignore .git / __pycache__ / node_modules"]
    COPY -->|成功| WORK["work_repo = tmp/repo"]
    COPY -->|失败| FALLBACK["work_repo = original repo<br/>sandbox_used = False"]

    SANDBOX -->|No| ORIG["work_repo = original repo"]

    WORK --> APPLY["git apply --check → git apply"]
    FALLBACK --> APPLY
    ORIG --> APPLY

    APPLY --> OK{成功?}
    OK -->|No| ERR_RET(["return VerificationResult<br/>patch_applied=False"])

    OK -->|Yes| TESTS{"有 test_info<br/>或 test_runner?"}
    TESTS -->|Yes| RUN["_run_tests()<br/>pytest / unittest / npm"]
    TESTS -->|No| NO_TESTS["仅做语法检查"]

    RUN --> CLASSIFY["_classify_status → resolved / partial / not_resolved"]
    NO_TESTS --> UNKNOWN["resolution_status = unknown"]

    CLASSIFY --> CLEANUP_TRY["try:"]
    UNKNOWN --> CLEANUP_TRY

    CLEANUP_TRY --> RET(["return VerificationResult"])
    RET --> FINALLY["finally:<br/>_cleanup_sandbox(tmp)"]
```

---

## 10. 参考与许可

- 论文：*SWE-Review: Closing the Loop on Issue Resolution with Agentic Code Review*（项目内 `SWE-Review-2607.06065.pdf`）
- Claude Code：`https://code.claude.com`
- Cursor：`https://cursor.com`
- OpenCode：`https://opencode.ai`
- Pi：`https://github.com/badlogic/pi-mono`
- License: MIT
