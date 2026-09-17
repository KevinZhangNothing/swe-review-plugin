# 闭环验证失败门禁实现计划

**目标：** review_guided 及 hybrid 回退阶段不得在验证明确失败时报告成功。

**方案：** 保留现有 `_verify` 弱通过归一化，仅在批准分支检查其结果。失败时返回 `verification_failed`，保留 diff、审查记录及验证详情；hybrid 拼接而非覆盖回退阶段消息。不改变 best_of_n、无验证器及弱通过语义。

**技术栈：** Python 标准库、pytest、unittest.mock；不新增依赖。

1. 新增 `tests/test_loop_verification_gate.py`：覆盖两种批准决策、明确失败、沙箱准备失败、正常通过、弱通过、无验证器、hybrid 回退及 LoopSkill.ok。
2. 运行 `python -m pytest tests/test_loop_verification_gate.py -q`，确认旧实现失败。
3. 修改 `swe_review/subagents/loop_agent.py` 的批准结果判定及 hybrid 消息保留。
4. 更新 `README.md` 对应不变式。
5. 在仓库根目录运行 `python -m pytest -q` 和 `git diff --check`；检查最终差异，不自动提交。
