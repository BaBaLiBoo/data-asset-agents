# 80-case Live Evaluation 错误分析

实验 Git SHA：`705b017f00b0526cd244ab117ebe8cd02dfc03e7`
Benchmark Hash：`e3045a0b12c567078aed8f80670e059062ef883583868e2e6387ea75a31ee779`
Database Snapshot Hash：`8fdbc08b6f19006be80513e7d7a91882e87570dd9593ad35064cdf44ea1cb04f`

本分析来自四份逐案例 CSV，不根据聚合指标反推 Case 根因。百分比均以每组 80 个
Case 为分母；Result Accuracy 的正式分母仍以有 Gold Result Hash 的 74 个成功类
Case 为准。

## 错误分布

| 错误类型 | Schema | Physical RAG | Ontology No Asset | Ontology Full |
|---|---:|---:|---:|---:|
| SEMANTIC_PARSE_ERROR | 0 | 0 | 0 | 0 |
| TABLE_SELECTION_ERROR | 0 | 1 | 0 | 0 |
| COLUMN_SELECTION_ERROR | 0 | 13 | 0 | 0 |
| JOIN_ERROR | 0 | 0 | 0 | 0 |
| BUSINESS_POLICY_ERROR | 74 | 1 | 0 | 0 |
| SQL_PARSE_ERROR | 0 | 0 | 0 | 0 |
| EXPLAIN_ERROR | 0 | 0 | 0 | 0 |
| EXECUTION_ERROR | 0 | 0 | 0 | 0 |
| RESULT_MISMATCH | 0 | 31 | 10 | 6 |
| CLARIFICATION_ERROR | 3 | 3 | 0 | 0 |
| UNSUPPORTED_CLASSIFICATION_ERROR | 3 | 3 | 0 | 0 |
| TEMPLATE_INCOMPATIBLE | 0 | 0 | 0 | 0 |
| PROVIDER_ERROR | 0 | 0 | 0 | 1 |
| TIMEOUT | 0 | 0 | 0 | 0 |

`TEMPLATE_INCOMPATIBLE=0` 不表示所有模板兼容。Ontology Full 对不兼容候选执行了
安全回退，因此最终 Case 错误被记录为实际的 Result Mismatch，而拒绝原因单独保存在
`template_rejection_reasons` 中。

## 逐类型根因

| 错误类型 | 数量与占比（主要组） | 主要 Case ID | 真实根因 | 归属 | 是否需要修复 |
|---|---|---|---|---|---|
| TABLE_SELECTION_ERROR | 1，1.25%（RAG） | `basic-01` | 物理检索/生成选择了额外表；RAG 没有正式本体表集合硬约束 | Semantic Parser / Physical RAG | 作为消融边界保留 |
| COLUMN_SELECTION_ERROR | 13，16.25%（RAG） | `basic-03/04/05`、`join-02/05/07/08/10`、`filter-01`、`complex-03/06/09`、`synonym-07` | 物理文档能召回字段但不能稳定恢复指标聚合角色与输出列契约 | Semantic Parser / SQL Builder | 后续改进 RAG，不修改本轮 Gold |
| BUSINESS_POLICY_ERROR | 74，92.5%（Schema）；1，1.25%（RAG） | Schema 的 74 个成功类 Case；RAG `synonym-01` | Schema 组按设计不可访问指标必需过滤；RAG 仅有物理证据，不能保证 `POSTED`、`CREDIT` 等口径 | Ontology Definition（缺失于消融组）/ Validator | 不修复消融组；这是本体收益证据 |
| RESULT_MISMATCH | 31，38.75%（RAG）；10，12.5%（No Asset）；6，7.5%（Full） | Full：`complex-02/03/05/06/08/09` | `02/05/08` 需要“高于整体平均值”的子查询，但 Semantic Query 丢失该谓词且候选缺少兼容 subquery；`03/06/09` 需要累计窗口，候选缺少匹配的 transaction-date running-total 模板，确定性回退只生成每日汇总 | Semantic Parser / SQLAsset / SQL Builder | 需要，但本轮冻结后不再改代码 |
| CLARIFICATION_ERROR | 3，3.75%（Schema、RAG） | `clarification-01/02/03` | 直接生成组不能稳定执行本体定义的澄清分类；Ontology 两组除一次 Provider 解析失败外均正确 | Semantic Parser | 本体链路已解决；基线组不修 |
| UNSUPPORTED_CLASSIFICATION_ERROR | 3，3.75%（Schema、RAG） | `unsupported-01/02/03` | Schema/RAG 缺少受支持业务概念边界 | Semantic Parser / Ontology Definition | 本体链路已解决 |
| PROVIDER_ERROR | 1，1.25%（Full） | `clarification-02` | DeepSeek 返回的结构化参数中 `intent: aggregate` 未加 JSON 引号，LangChain Structured Output 解析失败；原始错误已保留，未隐藏、未选择性重跑 | Provider / Semantic Parser | 需要后续增强结构化解析重试；本轮如实保留 |

其余错误类型为 0：没有 SQLGlot Parse、PostgreSQL EXPLAIN、只读执行、Join、
Timeout 或最终 Validator 故障。No Asset 的 10 个 Result Mismatch 全部是
`complex-01` 至 `complex-10`；Full 采用模板后修复了其中 4 个。

## SQLAsset 专项统计

统计口径：

- Candidate Recall：74 个成功类 Case 中是否至少召回一个认证候选。
- Compatible Rate：成功类 Case 中是否至少有一个候选通过兼容检查。
- Adoption Rate：与框架指标一致，为采用模板数 / 有兼容模板的 Case 数。

| 指标 | 结果 |
|---|---:|
| Candidate Recall | 74/74，100% |
| Template Compatible | 18/74，24.32% |
| Template Adoption（兼容 Case 分母） | 4/18，22.22% |
| Template Adoption（全部成功类 Case 分母） | 4/74，5.41% |
| 平均 selected_template_rank | 2.00 |
| 确定性 fallback | 56 |
| Rewrite validation failure | 0 |
| Rewrite EXPLAIN failure | 0 |
| 采用模板后的 Result Accuracy | 4/4，100% |
| 未采用模板的 Result Accuracy | 64/70，91.43% |

另有 14 个简单 Case 选中了兼容 Asset，但没有进入复杂 AST Rewrite 路径，故不计
template adoption：`dimension-02/06/10`、`top-01` 至 `top-08`、
`synonym-01/02/05`。这避免把“检索到候选”或“简单确定性 SQL”误报为模板采用。

出现次数最多的拒绝原因：

| 拒绝原因 | 次数 |
|---|---:|
| 模板 Join 与当前 Join Plan 不一致 | 77 |
| 缺少 `transaction_count` 指标 | 63 |
| 目标只需事实表、模板额外包含分行表 | 53 |
| 模板包含未请求的 `branch` 维度 | 46 |
| 缺少 `transaction_amount` 指标 | 42 |
| 缺少 `transaction_id` 物理字段覆盖 | 29 |
| 模板包含未请求的 `transaction_channel` | 23 |
| 缺少 `transaction_date` 物理字段覆盖 | 23 |
| 缺少 `window` 结构标签 | 10 |
| 缺少 `subquery` 结构标签 | 9 |

## complex-01 至 complex-10

| Case | 正确结构 Asset 是否召回 | Rank | Compatible | Adopted | CTE | Window | Subquery | Result Hash |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| complex-01 | 是，`sqlasset-branch-credit-window` | 2 | 是 | 是 | 保留 | 保留 | 不要求 | 正确 |
| complex-02 | 否，无匹配“整体平均值”子查询资产 | — | 否 | 否 | 否 | 否 | 缺失 | 错误 |
| complex-03 | 否，无匹配“每日累计金额”窗口资产 | — | 否 | 否 | 缺失 | 缺失 | 不要求 | 错误 |
| complex-04 | 是，`sqlasset-branch-credit-window` | 2 | 是 | 是 | 保留 | 保留 | 不要求 | 正确 |
| complex-05 | 否，无匹配“整体平均值”子查询资产 | — | 否 | 否 | 否 | 否 | 缺失 | 错误 |
| complex-06 | 否，无匹配“每日累计金额”窗口资产 | — | 否 | 否 | 缺失 | 缺失 | 不要求 | 错误 |
| complex-07 | 是，`sqlasset-branch-credit-window` | 2 | 是 | 是 | 保留 | 保留 | 不要求 | 正确 |
| complex-08 | 否，无匹配“整体平均值”子查询资产 | — | 否 | 否 | 否 | 否 | 缺失 | 错误 |
| complex-09 | 否，无匹配“每日累计金额”窗口资产 | — | 否 | 否 | 缺失 | 缺失 | 不要求 | 错误 |
| complex-10 | 是，`sqlasset-branch-credit-window` | 2 | 是 | 是 | 保留 | 保留 | 不要求 | 正确 |

## 结论

Ontology Full 相对 No Asset 把复杂类别 Result Accuracy 从 0% 提高到 40%，总体
Result Accuracy 从 86.49% 提高到 91.89%，即提高 5.40 个百分点。代价是平均延迟
从 1761.82 ms 增至 2202.75 ms，增加 440.93 ms（约 25.03%），并在本次运行中
出现 1 个 Provider Structured Output 解析错误。

本次实验可以证明受控 CTE + Window 模板被真实采用并在 4 个 Case 上得到正确结果；
不能证明任意 SQLAsset 都有效。子查询和累计窗口的覆盖仍不足，且没有重复实验或统计
检验，仍需更多重复实验验证。
