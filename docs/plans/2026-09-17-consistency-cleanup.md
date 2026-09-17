# 一致性收尾实现计划

**目标：** 统一验证失败的迭代标签；打通 CLI 到 SubAgent 的 test_runner 透传。

**方案：**
1. `loop_agent.py` 中 `best_of_n` 的两个 verify 迭代点（approved 分支约 422 行、rejected tie-break 约 445 行）把失败标签 `review_failed` / `request_changes` 统一为 `verification_failed`，仅改字符串，不改判定逻辑。
2. CLI `verify` 子命令新增 `--test-runner`（字符串，`shlex.split` 解析为模板列表，要求含 `{test}` 占位符否则 argparse 报错），`_cmd_verify` 透传给 `VerifySkill.execute(test_runner=...)`。

**技术栈：** argparse、shlex、pytest；不新增依赖。

任务：
1. 新增 `tests/test_loop_verification_gate.py` 中 best_of_n 标签回归测试（旧实现失败）。
2. 运行确认失败；修改两处标签；重跑通过。
3. 新增 `tests/test_cli_verify.py`：`--test-runner` 解析与透传（解析 `build_parser()` 的 args、mock `VerifySkill.execute` 捕获 kwargs）；缺 `{test}` 报 SystemExit。
4. 运行确认失败；修改 `swe_review/cli.py`；重跑通过。
5. `python -m pytest -q` 全量 + `git diff --check`；检查差异，不提交。
