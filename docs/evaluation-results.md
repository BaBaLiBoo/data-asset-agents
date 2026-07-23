# Text-to-SQL Live Evaluation Results

> Status: Completed. 本报告只描述 Git SHA
> `705b017f00b0526cd244ab117ebe8cd02dfc03e7` 上一次四组 80-case Live
> 实验；没有重复实验或统计检验，不使用“显著提升”。

## 1. 实验信息

| 字段 | 值 |
|---|---|
| Git SHA | `705b017f00b0526cd244ab117ebe8cd02dfc03e7` |
| 日期 | 2026-07-23 |
| Provider / Model | DeepSeek / `deepseek-chat` |
| Embedding | Aliyun / `text-embedding-v4` |
| Temperature | 0 |
| Max Tokens / Timeout | 2048 / 30 s |
| Random Seed / Concurrency | `20260716` / 1 |
| Benchmark Version | `minibank-text2sql-v1`，80 cases |
| Benchmark Hash | `e3045a0b12c567078aed8f80670e059062ef883583868e2e6387ea75a31ee779` |
| Database Snapshot Hash | `8fdbc08b6f19006be80513e7d7a91882e87570dd9593ad35064cdf44ea1cb04f` |
| Ontology Version | `version_f906ab4cf5164742b44ddcb29c02ac54` |
| Resource Hash | `58c0c6dd1daa88e4dc641fdb6c2045e574e66d9dde484bfe7db3278954c51248` |
| Bundle Hash / Compiler | `0dd064baa8a7a9cc330ab91969355cb94129065e27076605b0ac836fc1cbdd45` / `1` |
| Ontology IndexBuild | `ontology-index-2339b1a35f274d88b1bcd092411dd6c6` |
| SQLAsset Build | `sqlbuild-39aa7df587884846ba05bb33e85810ec` |
| Schema Run | `898801a9-686c-494c-a378-80d1d7b59f27` |
| Physical RAG Run | `140ac26c-948e-4977-a61c-f3b3ba94c174` |
| Ontology No Asset Run | `4c917697-575c-4c03-b3ff-0917e95e6d14` |
| Ontology Full Run | `1772b89e-0d41-4d97-aa9d-3674b3cb5aec` |
| Compare | `warnings=[]` |

四组 Case ID、Git SHA、Provider、Model、Temperature、Max Tokens、Timeout、
Concurrency、Random Seed、Benchmark Hash 和 Database Snapshot Hash 完全一致；两个
Ontology 组使用同一个正式 Ontology Version 与 Bundle Hash。Ontology Full 使用与
该版本绑定的 READY SQLAsset Build。正式版本不以 `yaml-seed-` 开头。

## 2. 总体结果

| 指标 | Schema | Physical RAG | Ontology No Asset | Ontology Full |
|---|---:|---:|---:|---:|
| status_accuracy | 92.50% | 91.25% | 100.00% | 98.75% |
| semantic_query_accuracy | N/A | N/A | 93.24% | 93.24% |
| table_exact_match | 93.24% | 98.65% | 100.00% | 100.00% |
| column_exact_match | 0.00% | 79.73% | 100.00% | 100.00% |
| join_exact_match | 100.00% | 100.00% | 100.00% | 100.00% |
| business_policy_accuracy | 0.00% | 97.30% | 100.00% | 100.00% |
| sql_parse_rate | 100.00% | 98.65% | 100.00% | 100.00% |
| sql_execution_rate | 100.00% | 98.65% | 100.00% | 100.00% |
| result_accuracy | 1.35% | 41.89% | 86.49% | 91.89% |
| deprecated_table_false_selection_rate | 0.00% | 0.00% | 0.00% | 0.00% |
| template_adoption_rate | N/A | N/A | N/A | 22.22% |
| average_latency_ms | 1334.14 | 1516.72 | 1761.82 | 2202.75 |
| p50_latency_ms | 1309.85 | 1520.44 | 1744.44 | 2177.08 |
| p95_latency_ms | 1750.12 | 1813.56 | 2081.74 | 2884.71 |

Ontology No Asset 相对 Schema：

- Table Exact Match 提高 6.76 个百分点；
- Column Exact Match 提高 100.00 个百分点；
- Join Exact Match 持平；
- Business Policy Accuracy 提高 100.00 个百分点；
- Result Accuracy 提高 85.14 个百分点；
- 平均延迟增加 427.68 ms（约 32.06%）。

Ontology No Asset 相对 Physical RAG：

- Table Exact Match 提高 1.35 个百分点；
- Column Exact Match 提高 20.27 个百分点；
- Join Exact Match 持平；
- Business Policy Accuracy 提高 2.70 个百分点；
- Result Accuracy 提高 44.60 个百分点；
- 平均延迟增加 245.10 ms（约 16.16%）。

Ontology Full 相对 No Asset：

- Result Accuracy 提高 5.40 个百分点；
- 复杂查询 Result Accuracy 从 0% 提高到 40%；
- 平均延迟增加 440.93 ms（约 25.03%）；
- 本次 Full Run 出现 1 个 Provider Structured Output 解析错误。

## 3. 分类结果

下表为 Result Accuracy；“分类”一行报告 status accuracy，因为这些 Case 不生成 SQL。

| 类别（Case 数） | Schema | Physical RAG | Ontology No Asset | Ontology Full |
|---|---:|---:|---:|---:|
| 基础指标（10） | 10% | 60% | 100% | 100% |
| 单维度、双维度（10） | 0% | 10% | 100% | 100% |
| 多表 Join（10） | 0% | 50% | 100% | 100% |
| 时间、用户过滤和 IN（10） | 0% | 10% | 100% | 100% |
| Top N 和排序（8） | 0% | 100% | 100% | 100% |
| CTE、窗口函数和子查询（10） | 0% | 0% | 0% | 40% |
| 同义词与金额字段歧义（8） | 0% | 25% | 100% | 100% |
| 生命周期、汇总粒度干扰（8） | 0% | 100% | 100% | 100% |
| clarification / unsupported（6，status） | 0% | 0% | 100% | 83.33% |

Full 组分类下降来自 `clarification-02` 的单次 Provider JSON 解析错误，不是本体把
问题错误分类。该失败保留在正式结果中，没有选择性重跑。

## 4. 错误分布

| 组别 | 主要错误 |
|---|---|
| Schema | `BUSINESS_POLICY_ERROR` 74；`CLARIFICATION_ERROR` 3；`UNSUPPORTED_CLASSIFICATION_ERROR` 3 |
| Physical RAG | `RESULT_MISMATCH` 31；`COLUMN_SELECTION_ERROR` 13；`TABLE_SELECTION_ERROR` 1；`BUSINESS_POLICY_ERROR` 1；两类分类错误各 3 |
| Ontology No Asset | `RESULT_MISMATCH` 10，全部为 `complex-01` 至 `complex-10` |
| Ontology Full | `RESULT_MISMATCH` 6；`PROVIDER_ERROR` 1 |

详细 Case ID、根因、责任层与是否需要修复见
[`error-analysis.md`](../artifacts/evaluation/705b017f00b0526cd244ab117ebe8cd02dfc03e7/error-analysis.md)。

## 5. SQLAsset 采用情况

| 指标 | 结果 |
|---|---:|
| Candidate Recall | 74/74，100% |
| Template Compatible | 18/74，24.32% |
| Template Adoption | 4/18，22.22% |
| 全部成功类 Case 中采用 | 4/74，5.41% |
| 平均 selected_template_rank | 2.00 |
| Deterministic fallback | 56 |
| Rewrite validation / EXPLAIN failure | 0 / 0 |
| 采用模板后的 Result Accuracy | 4/4，100% |
| 未采用模板的 Result Accuracy | 64/70，91.43% |

`complex-01/04/07/10` 采用排名第 2 的
`sqlasset-branch-credit-window`，保留 CTE 与 `DENSE_RANK`，SQLValidator、
PostgreSQL EXPLAIN 和结果 Hash 全部通过。`complex-02/05/08` 缺少匹配的整体平均值
子查询资产；`complex-03/06/09` 缺少匹配的每日累计金额窗口资产，因此回退确定性 SQL
后仍 Result Mismatch。

Template Adoption 大于 0，因而可以确认 SQLAsset 在这 4 个受控 CTE + Window Case
上产生真实收益；不能据此声称对子查询、任意窗口或所有复杂 SQL 有效。

## 6. 典型案例

成功：

- `basic-01`：Ontology 两组从正式 Metric 定义恢复 `POSTED` 业务过滤和 amount
  物理角色，Result Hash 正确；Schema/RAG 分别出现业务口径或选表错误。
- `top-01`：Ontology 两组确定性构建 Top N，Full 虽召回兼容 Asset，但未把简单
  fallback 误记为 template adoption。
- `complex-01`：Full 采用 CTE + `DENSE_RANK` 模板并得到正确结果；No Asset 结果不一致。
- `complex-10`：同一窗口模板在重复复杂契约上稳定采用，Rank 2，结果正确。

失败：

- `complex-02`：问题要求“高于整体平均值”，Semantic Query 没有表达该子查询谓词，
  候选又缺少 subquery 结构；回退 SQL 丢失该条件。
- `complex-03`：问题要求“每日金额和累计金额”，回退 SQL 只生成每日汇总，没有
  running total 窗口。
- `clarification-02`：Provider 返回非法 JSON（`intent: aggregate` 未加引号），
  Structured Output 解析失败；没有 SQL 被执行。

## 7. 最终结论

本次 80-case Benchmark 结果表明，正式本体最明确的收益是把指标、字段角色、审核
Join、必需业务过滤和分类边界变成确定性硬约束：相对 Schema 与 Physical RAG，表、
字段和业务策略指标均表现更好，最终 Result Accuracy 分别提高 85.14 和 44.60 个
百分点。

SQLAsset 的真实收益较窄但可验证：4 个 CTE + Window Case 从错误变为正确，总体再提高
5.40 个百分点，代价是约 25.03% 的平均延迟增加。当前主要不足是 Semantic Query
不能表达整体平均值子查询和累计窗口意图、匹配模板覆盖不足，以及一次 Provider
Structured Output 解析失败。没有重复实验和统计检验，仍需更多重复实验验证。

## 8. 项目边界

- 单一虚构 MiniBank 数据域；
- 单一 PostgreSQL；
- 仅只读查询，无写回；
- 无 ActionType、Function、Automate；
- 无多租户和细粒度权限；
- 无真实银行数据、真实内部 Schema、SQL 或规则。
