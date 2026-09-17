# 测试执行诊断信息实现计划

**目标：** 区分"测试失败"与"测试运行器从未启动"，让验证失败可诊断。

**方案：** `VerifierSubAgent._run_tests.run_one` 记录运行器启动失败的具体原因（每个尝试模板的错误聚合进 `stderr_tail`），运行器能启动但超时标记 `error="timeout"`；所有结果无法启动时 `runner_ok=False`。`_format_details` 在"没有任何运行器成功启动"时输出显式警告行。失败仍 fail-closed：`passed=False`、`resolution_status` 分类逻辑与闭环验证失败门禁均不变；不新增字段到 `VerificationResult`，诊断信息经 `test_results[].runner_ok/stderr_tail` 与 `details` 暴露。

**技术栈：** Python 标准库、pytest、asyncio；不新增依赖。

任务：
1. 新增 `tests/test_verifier_testrunner_diagnostics.py`：显式运行器不存在、默认运行器全失败、超时、真实运行器通过四类用例。
2. 运行新测试确认旧实现失败（缺 `runner_ok`/具体原因）。
3. 修改 `swe_review/subagents/verifier_agent.py` 的 `run_one` 与 `_format_details`。
4. 运行 `python -m pytest -q` 与 `git diff --check`；检查差异，不提交 Git。
