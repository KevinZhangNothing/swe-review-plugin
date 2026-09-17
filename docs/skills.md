# Skills 文档

每个 Skill 是一个"面向 LLM 的能力包装"。CLI/agent 通过 SKILL.md frontmatter 发现它们。

| Skill 名称 | 触发场景 | SKILL.md |
|----------|---------|---------|
| `swe-review-explore` | 收集仓库上下文 | `.claude/skills/swe-review-explore/SKILL.md` |
| `swe-review-analyze` | 静态 diff 分析 | `.claude/skills/swe-review-analyze/SKILL.md` |
| `swe-review-generate` | 生成候选 PR | `.claude/skills/swe-review-generate/SKILL.md` |
| `swe-review-review`  | 审查候选 PR | `.claude/skills/swe-review-review/SKILL.md` |
| `swe-review-revise`  | 按反馈修订 | `.claude/skills/swe-review-revise/SKILL.md` |
| `swe-review-verify`  | 沙箱 + 测试 | `.claude/skills/swe-review-verify/SKILL.md` |
| `swe-review-loop`    | 编排闭环 | `.claude/skills/swe-review-loop/SKILL.md` |

## Frontmatter 规范

按 [Agent Skills specification](https://agentskills.io/specification)：
- `name` 必填（lowercase a-z, 0-9, `-`，最长 64 字符）
- `description` 必填（≤ 1024 字符，必须明确"什么时候用"）

## 安装

`./install.sh` 为仓库 `.claude/skills/`（唯一源）创建符号链接到三处发现路径（幂等，可重复执行）：
- `~/.agents/skills/swe-review-*`
- `~/.claude/skills/swe-review-*`（Claude Code + OpenCode 自动加载）
- `~/.pi/agent/skills/swe-review/swe-review-*`（Pi 自动加载）

## 调用

### Host mode（推荐：用当前 agent 自己回答，不 spawn 任何 CLI）
```
swe-review review --issue "..." --pr-diff p.diff --tool host --host-dir .swe-host
# 退出码 3 → swe-review host pending --show → host answer --key <k> --text-file ans.json
# 原样重跑同一命令，已答 prompt 从缓存重放
```

### Claude Code / OpenCode（共享 ~/.claude/skills）
```
/skill swe-review-review   <prompt>
```

### Pi
```
/skill:swe-review-review   <prompt>
```

### CLI 直接调用（不依赖 Skill 系统）
```
swe-review review --issue "..." --pr-diff ./pr.diff --tool pi
```

## 编写规范

- 不要把"调用 LLM 拿 token 用量"做成 Skill 自带逻辑 —— 那是 Adapter 的职责。
- Skill 调用 SubAgent 时，禁止在 `context` 中塞入 `golden_patch`/`test_info`/`oracle`，否则 SubAgent 显式抛错。
- 想接 SWE-Review-Bench 评测：使用 `VerifySkill.execute(..., oracle=open('gold.diff').read())`，再让 loop 之外做指标聚合。
