# Security Model

## 1. Reviewer Privacy

> 论文 §3.1: *"The reviewer does not receive the golden patch or hidden test results."*

代码层强制（`generator_agent.py:GeneratorSubAgent.execute` 与
`reviser_agent.py:ReviserSubAgent.execute` 入口均有同样的守卫）：
```python
for forbidden in ("golden_patch", "gold_patch", "oracle", "test_info"):
    if forbidden in context:
        raise ValueError(...)
```

命中的字段一律 ValueError：

Verifier **可以**接收 oracle，因为它跑在 review 链路外，只做分数聚合。

## 2. Patch Application Safety

`VerifierSubAgent` 默认 sandbox：
- `tempfile.mkdtemp(prefix="swe-review-")`
- 用 `shutil.copytree(..., ignore=(".git","__pycache__","node_modules"))` 只读复制源码
- 在复制出的工作目录中执行 `git apply --check`，通过才 `git apply -`
- 仓库路径不是目录、临时目录创建或复制失败时，返回 `passed=False`、`patch_applied=False`、`sandbox_used=False`、`resolution_status="unknown"`，不应用补丁、不运行构建或测试
- 准备失败、后续失败或正常结束都会进入 `finally`，清理已创建的临时目录
- 默认模式不会因准备失败回退到原仓库；显式 `sandbox=False` 仍会原地应用补丁

这里的沙箱是工作目录副本，不是操作系统级隔离；构建和测试命令仍以当前用户权限运行。

## 3. Diff 输出格式

`ReviserSubAgent` / `GeneratorSubAgent` 输出后做轻校验：
```python
ok = diff.startswith(("diff ", "diff --git")) and "@@" in diff
```
不合法时 `status="failed"` 或 `confidence=0.0`。下游 Verifier 用 `git apply --check` 二次校验。

## 4. Token / API Key

- 不在仓库内出现任何 API key。
- `install.sh` 创建 `.env.local` 模板，由用户填充。
- Adapter 不读也不传任何 model 名称：swe 循环不指定具体模型，模型选择完全交给宿主 CLI/环境（不读 `*_MODEL` 环境变量，不传 `--model`）。
- Subagent 调用是**纯文本生成**，不给宿主 CLI 放 write/edit/bash 工具（pi 用 `--no-tools`）。探索由本地 ExplorerSubAgent 完成；放开工具会让 review 子进程改动目标仓库（实测曾污染 baseline worktree 导致 verify 失败）。已知残留风险：cursor `--trust`、opencode `run` 暂无干净的 no-tools 旗标，调用前请确保目标仓库无未提交变更。

## 5. Pi Skill Installation

`PiAdapter.install_skills()`：
- 只写到 `~/.pi/agent/skills/swe-review/<name>/`
- 先 `shutil.rmtree(dest, ignore_errors=True)` 再 copy，避免残留半成品
- **不**触碰 `~/.pi/agent/skills/` 之外的目录

## 6. Shell 工具信任

`ShellTools` 不调任何 LLM，仅返回固定占位 JSON。它的存在意义是：
- 在没有 LLM CLI 的环境里也能跑通 e2e 管线
- CI 单元测试时不需要 mock LLM

## 7. Host mode 的落盘内容（`--tool host`）

`--tool host` 不 spawn 任何 CLI，而是把 **完整的 system + user prompt 明文**写到
`<host-dir>/requests/<key>.json`，答案再由宿主写进 `<host-dir>/responses/<key>.json`
（文件布局见 `docs/adapters.md`）。

**这意味着 issue 全文与未脱敏的原始 PR diff 会落在磁盘上。** 边界与要求：

- 默认 `.swe-host/` 已在 `.gitignore` 里——**不要提交、不要放进共享目录或同步盘**；
  这些文件里就是完整的候选补丁与 issue 描述（可能含内部代码、未公开的修复）。
- 与 §1 的「Reviewer Privacy」是两件事：**模型看不到** oracle/golden_patch/test_info，
  但 host 模式**会把 prompt 写到本机磁盘**。若 `--host-dir` 指向共享位置，等于把补丁
  与 issue 一起共享出去。
- 多用户机器上建议 `chmod 700 <host-dir>`；无人值守场景用 4 个 CLI adapter（不落盘）。
- `responses/` 里是宿主模型的原始答案，同样按内部材料对待；跑完可整目录删除。

（这条是本插件自检时由它自己提出的 —— 见 `docs/` 与 README 的不变式矩阵。）
