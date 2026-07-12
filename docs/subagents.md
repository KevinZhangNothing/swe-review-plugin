# SubAgents 文档

每个 SubAgent 是一个**纯执行单元**，接收明确的 context schema，返回有 `to_dict()` 的 dataclass 或 dict。

| SubAgent | 文件 | 输入 schema | 输出 schema |
|---------|-----|-----------|-----------|
| `ExplorerSubAgent` | `swe_review/subagents/explorer_agent.py` | `{repo_path, issue, pr_diff, focus_files?, max_steps?}` | `ExplorationResult` |
| `AnalyzerSubAgent` | `swe_review/subagents/analyzer_agent.py` | `{pr_diff, old_content?, new_content?}` | `AnalyzerResult` |
| `GeneratorSubAgent` | `swe_review/subagents/generator_agent.py` | `{issue, hint?, repo_path?, exploration?}` | `GeneratedPR` |
| `ReviewerSubAgent` | `swe_review/subagents/reviewer_agent.py` | `{issue, pr_title, pr_body?, pr_diff, repo_path?, max_exploration_steps?, focus_files?}` | `ReviewReport` |
| `ReviserSubAgent` | `swe_review/subagents/reviser_agent.py` | `{issue, original_pr_title, original_pr_body?, original_pr_diff, review_report{defects[]}, repo_path?}` | `RevisedPR` |
| `VerifierSubAgent` | `swe_review/subagents/verifier_agent.py` | `{pr_diff, repo_path?, test_info?, oracle?, test_runner?}` | `VerificationResult` |
| `LoopSubAgent` | `swe_review/subagents/loop_agent.py` | `{issue, repo_path?, initial_pr?, max_iterations?, strategy?, n_best_of?}` | `LoopResult` |

## 强制 schema 守卫

`ReviewerSubAgent` / `ReviserSubAgent` / `GeneratorSubAgent` 的 `execute()` 对以下字段**显式抛错**：

- `golden_patch` (or `gold_patch`)
- `test_info` (or `hidden_tests`)
- `oracle`

任何调用方传进来，立刻 `raise ValueError(...)`。这是为了兑现论文 §3.1 的不变式（reviewer 不能看到 ground-truth）。

## 单元测试

`tests/test_subagents.py` 已覆盖：
- `golden_patch` 注入 → ValueError
- `test_info` 注入 → ValueError
- `oracle` 注入到 reviser → ValueError
- `ExplorationResult.files_modified` 解析
- `AnalyzerResult.total_additions` ≥ 1
