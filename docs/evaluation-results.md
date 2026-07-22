# Text-to-SQL Live Evaluation Results

> Status: Live evaluation pending external model credentials. 本文件只提供可复现报告结构，
> 在四组 80-case live 运行和强制公平性检查完成前不填写或推测任何指标数字。

## 1. 实验信息

| 字段 | 值 |
|---|---|
| Git SHA | Pending |
| 日期 | Pending |
| Provider | Pending |
| Model | Pending |
| Temperature | Pending |
| Max Tokens | Pending |
| Timeout | Pending |
| Random Seed / Concurrency | Pending |
| Benchmark Version / Hash | Pending |
| Database Snapshot Hash | Pending |
| Ontology Version / Bundle Hash | Pending |
| SQLAsset Build ID | Pending |
| Physical RAG Build ID | Pending |
| Schema Run ID | Pending |
| Physical RAG Run ID | Pending |
| Ontology No Asset Run ID | Pending |
| Ontology Full Run ID | Pending |

## 2. 实验组

| 组别 | 可见知识 | 禁止访问 |
|---|---|---|
| Schema | Schema、字段、类型、主外键 | Ontology、Mapping、审核 Join、SQLAsset、业务策略 |
| Physical RAG | Schema、物理元数据、脱敏 Profile、原始历史 SQL | OntologyService、Metric/Dimension ID、审核 Mapping/Join、SQLAsset |
| Ontology No Asset | 当前发布本体、确定性 Mapping/Join、业务策略 | SQLAsset 搜索、模板选择、AST Rewrite |
| Ontology Full | 上述本体知识和当前版本 READY SQLAssetBuild | 跨版本、生命周期或业务口径不合格资产 |

## 3. 总体结果

| 指标 | Schema | Physical RAG | Ontology No Asset | Ontology Full |
|---|---:|---:|---:|---:|
| status_accuracy | Pending | Pending | Pending | Pending |
| semantic_query_accuracy | N/A | N/A | Pending | Pending |
| table_exact_match | Pending | Pending | Pending | Pending |
| column_exact_match | Pending | Pending | Pending | Pending |
| join_exact_match | Pending | Pending | Pending | Pending |
| business_policy_accuracy | Pending | Pending | Pending | Pending |
| sql_parse_rate | Pending | Pending | Pending | Pending |
| sql_execution_rate | Pending | Pending | Pending | Pending |
| result_accuracy | Pending | Pending | Pending | Pending |
| deprecated_table_false_selection_rate | Pending | Pending | Pending | Pending |
| template_adoption_rate | N/A | N/A | N/A | Pending |
| average_latency_ms | Pending | Pending | Pending | Pending |
| p50_latency_ms | Pending | Pending | Pending | Pending |
| p95_latency_ms | Pending | Pending | Pending | Pending |

## 4. 分类结果

正式报告分别填写：基础指标；单维度、双维度；多表 Join；时间和用户过滤；Top N 和排序；
CTE / Window / 子查询；同义词和金额字段歧义；生命周期和汇总粒度干扰；clarification /
unsupported。每类同时报告案例数、四组结果准确率与主要错误，不只给总体平均值。

## 5. 错误类型

按逐案例事实统计：`SEMANTIC_PARSE_ERROR`、`TABLE_SELECTION_ERROR`、
`COLUMN_SELECTION_ERROR`、`JOIN_ERROR`、`BUSINESS_POLICY_ERROR`、`SQL_PARSE_ERROR`、
`EXPLAIN_ERROR`、`EXECUTION_ERROR`、`RESULT_MISMATCH`、`CLARIFICATION_ERROR`、
`UNSUPPORTED_CLASSIFICATION_ERROR`、`TEMPLATE_INCOMPATIBLE`、`TIMEOUT`、
`PROVIDER_ERROR`。如果实现内部错误码名称不同，在报告中给出明确映射，不能合并隐藏失败。

## 6. 典型案例

每组至少填写 2 个成功、2 个失败；全报告至少包含 1 个体现本体价值和 1 个本体仍未解决
的案例。每个案例展示：用户问题、Strategy、召回上下文摘要、Semantic Query、Generated
SQL、Gold SQL、Validator 结果、Result Hash 是否一致和错误原因。所有内容仅来自虚构
MiniBank，不展示 Secret、完整 Provider 响应或未脱敏数据。

## 7. 结论

只基于本次真实结果回答：Ontology 是否改善表选择、Join 和业务过滤；SQLAsset 是否改善
复杂 SQL；准确率与延迟如何权衡；哪些类型仍失败。没有重复实验或统计检验时，只使用
“提高了 X 个百分点”“在本次 80-case Benchmark 中表现更好”“当前结果表明”“仍需更多
重复实验验证”，不使用“显著提升”。
