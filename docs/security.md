# Security Model

## 1. Reviewer Privacy

> 论文 §3.1: *"The reviewer does not receive the golden patch or hidden test results."*

代码层强制：
```python
# swe_review/subagents/reviewer_agent.py:ReviserSubAgent.execute
for forbidden in ("golden_patch", "test_info"):
    if forbidden in context:
        raise ValueError(...)
```

`ReviserSubAgent` 与 `GeneratorSubAgent` 也同样：
- `golden_patch` / `gold_patch` → ValueError
- `oracle` / `test_info` → ValueError

Verifier **可以**接收 oracle，因为它跑在 review 链路外，只做分数聚合。

## 2. Patch Application Safety

`VerifierSubAgent` 默认 sandbox：
- `tempfile.mkdtemp(prefix="swe-review-")`
- 用 `shutil.copytree(..., ignore=(".git","__pycache__","node_modules"))` 只读复制源码
- `git apply --check` 先 dry-run，通过才 `git apply -`
- 跑完测试后 `shutil.rmtree(tmp)`
- 用户工作区永远不被原地修改

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

## 5. Pi Skill Installation

`PiAdapter.install_skills()`：
- 只写到 `~/.pi/agent/skills/swe-review/<name>/`
- 先 `shutil.rmtree(dest, ignore_errors=True)` 再 copy，避免残留半成品
- **不**触碰 `~/.pi/agent/skills/` 之外的目录

## 6. Shell 工具信任

`ShellTools` 不调任何 LLM，仅返回固定占位 JSON。它的存在意义是：
- 在没有 LLM CLI 的环境里也能跑通 e2e 管线
- CI 单元测试时不需要 mock LLM
