# 对象语义运行时与变更治理

本文描述 MiniBank 演示域的对象语义运行时。它借鉴企业本体平台的治理思想，但不声称复刻任何商业平台；所有表、规则、SQL 与数据均为仓库自行构造的虚构内容。

## 0. 对象优先的构建入口

默认构建不再从旧分析 YAML 反推对象。三个受控入口最终都进入同一种 `OntologyDraft`：

```text
Direct Object Seed ───────────────┐
Blank Draft + manual modelling ───┼→ Draft → Validate/Dry Run → Review → Publish
MetadataSnapshot + SQL evidence ──┘        ↑
                  review-only candidates ──┘
```

直接种子位于 `ontology/retail_banking/object_model/`，分别保存 Object、Property、Binding、Business Link 与 Physical Join。加载器只接受应用配置的 seed 名称，执行强类型及跨资源引用校验，不访问 LLM、不发布、也不回退到 Legacy migrator。元数据候选生成器仅对 `CANONICAL_OBJECT` 和合适的 `EVENT` 表生成建议；汇总、技术、测试和废弃表会以排除原因留在证据报告中。已有 Draft 资源不会被机器候选覆盖，Binding 冲突会显式失败。

LLM Structured Output 只允许影响候选的业务名称、边界描述、属性/Link 名称、证据摘要和置信度。物理表字段、Physical Join、生命周期、指标口径、安全策略和发布状态均来自确定性元数据、受控配置及人工审核。候选导入后 Draft 仍是 `DRAFT`，且必须重新 Validate。

`LegacyOntologyObjectMigrator` 只服务于旧配置的一次性导入和兼容回归。它不会被直接 seed、默认候选生成、候选导入、Streamlit 默认流程或 Docker acceptance 调用。

## 1. 为什么对象模型是运行时事实源

旧链路以审核 YAML 中的物理表达式为事实源，对象、属性和 Link 主要服务于审核与展示。现在，当前版本存在 PUBLISHED 对象资源时，物理实现沿以下稳定引用编译：

```text
Metric / Dimension → Property ID → Published Binding → Physical Column
Business Link → Published Physical Join → JoinDefinition → Join Plan
```

`ObjectSemanticCompiler` 读取当前版本的 `ObjectType`、`PropertyDefinition`、`ObjectDataSourceBinding`、`LinkType` 与 `PhysicalJoinDefinition`，生成在线 `OntologyBundle` 所需的 `PhysicalMapping`、Metric 表达式/过滤、Dimension 表字段和 JoinDefinition。在线 LangGraph、SQLValidator 与 QueryExecutor 只消费该编译结果。

## 2. Metric 与 Dimension 的 Property 引用

Metric 保留旧 `expression/base_table/required_filters` 字段用于 YAML 回退，同时增加：

- `measure_property_id` 与 `aggregation`：确定聚合的业务属性和函数；
- `filter_predicates`：用 Property ID 表达必要业务过滤；
- `time_property_id`：确定相对或绝对时间过滤字段；
- `supported_dimension_property_ids`：约束可组合维度。

Dimension 通过 `property_id` 指向唯一业务属性。例如：

```text
transaction.amount → dwd_card_transaction.txn_amount_cny
transaction.status → dwd_card_transaction.transaction_status
transaction.event_time → dwd_card_transaction.transaction_date
branch.name → dim_branch.branch_name
```

编译器要求 `SUM/AVG/MIN/MAX` 的 Measure Property 角色为 `MEASURE`，过滤与时间 Property 必须绑定到相同事实表，支持维度必须存在正式 Dimension。对象绑定与旧兼容字段不一致时，Validator 返回 `OBJECT_LEGACY_SEMANTIC_CONFLICT` 并阻断发布；不会静默采用旧值。

当前版本没有对象资源时，运行时继续读取审核 YAML。这一回退用于首次启动和旧版本兼容，不允许覆盖已发布对象绑定。

## 3. Business Link 与 Physical Join

Business Link 只表达对象关系；Physical Join 是其版本化物理实现。Join 在 Draft 内独立创建、编辑、删除和校验，必须记录数据源、Schema、两端表字段、基数、置信度、证据和生命周期。Link 只能引用同 Draft 中存在、启用且端点与对象 Binding 一致的 Join。

发布事务将 Join 写入 `published_physical_join`，并在 `ontology_version_object_resource` 中记录 `PHYSICAL_JOIN` 来源。版本激活或回滚时重新读取对应版本资源，因此未发布 Join 不会进入 NetworkX Join Graph。

## 4. 动态 Dry Run

固定 SQL 仅作为数据库健康探针。每次 Draft 校验会用候选对象资源编译临时 Bundle，并实际运行四个核心问题：分行信用卡金额/笔数、分行金额排名、渠道交易金额和分行活跃客户数。每项报告包含 Property/Binding/Join 解析、生成 SQL、SQLGlot、业务策略与 PostgreSQL `EXPLAIN` 结果；任一失败即阻断审核或发布。

## 5. Draft Diff 与 Breaking Change

`OntologyChangeSet` 将 Draft 与 `base_version_id` 对应的不可变正式资源比较。名称、描述、同义词等展示性变更为 `NON_BREAKING`；删除 ACTIVE 资源、修改主键/数据类型/Binding/Physical Join 等为 `BREAKING`；其余语义行为变化为 `POTENTIALLY_BREAKING`。

`OntologyImpactReport` 追踪受影响的 Metric、Dimension、Physical Mapping、Link Path、SQLAsset 和 Benchmark，并给出是否需要重建概念索引、SQLAsset 与 Gold Hash。Breaking Change 必须同时提交 `acknowledge_breaking_changes=true` 与非空 `change_ticket`，但确认不能绕过 Validator、Dry Run 或 Drift 门槛。

## 6. Metadata Drift 生命周期

Metadata Sync 只读检查正式 Binding 的表字段、类型、主键与 Join 端点：

- 仅新增未绑定字段：`ADDITIVE`，当前版本继续服务；
- 删除绑定字段、修改类型/主键/Join 端点：`BREAKING`，状态页告警并阻止基于该版本的新发布；
- 无变化：`NONE`。

Drift 报告和 Sync Run 持久化保存，不自动修改 Binding，也不自动发布。修复必须创建新 Draft 并重新审核。

## 7. Ontology IndexBuild 原子切换

每个索引构建绑定 `ontology_version_id + index_type`，状态为 `BUILDING / READY / FAILED / STALE`。构建写入新的 `ontology_search_document` 集合并校验文档和向量后，在同一事务中撤销旧 `is_current`、激活新 READY Build。BUILDING/FAILED 不参与在线查询；失败不会删除旧 READY。回滚版本时优先复用该版本最近的 READY Build，没有时回退到确定性概念检索。live Embedding 失败只标记构建失败，不阻塞 API 启动。

## 8. Object Explorer 安全边界

Object Explorer 只查询当前 PUBLISHED 版本：

- 列表、主键详情和审核 Link 导航均由 Binding 生成；
- 物理标识符必须来自发布白名单，值全部使用 SQLAlchemy 参数绑定；
- 仅支持 `EQ/IN/GT/GTE/LT/LTE` 的可筛选 Property，limit 最大 100；
- 敏感 Property 固定返回 `***MASKED***`，不存在绕过参数；
- 不接受 SQL、任意字段、任意 Join、写操作、Secret 或动态数据库地址。

## 9. 当前边界

当前没有实现 ActionType、Function、Automate、SharedProperty、Interface、对象写回、行列级用户权限、多数据库动态连接或增量 CDC。索引和 Sync 第一版同步运行；复杂分析仍受单事实表、审核 Metric/Dimension 和现有 SQLAsset AST 改写能力约束。

当前编译目标仍是兼容现有 LangGraph、SQLAsset 与 Evaluation 的 `OntologyBundle`。因此 `Metric.expression/base_table/required_filters` 和 `Dimension.table/column` 尚未删除，但一旦存在发布对象资源，Property Binding 是权威物理来源；兼容字段冲突会阻断发布，而不是静默回退。
