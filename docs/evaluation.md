# Text-to-SQL 对照实验设计

## 四个严格隔离实验组

| 组别 | 可见知识 | 禁止访问 | 生成方式 |
|---|---|---|---|
| Schema | 表、字段、类型、主外键、可空性 | 本体、Profile、样例、历史 SQL、生命周期、业务规则 | 相同 Chat Model 直接生成 |
| Physical RAG | Schema、注释、Profile、脱敏样例、原始历史 SQL | Concept、Metric/Dimension ID、Mapping、审核 Join、认证与版本 | 物理检索上下文约束生成 |
| Ontology without SQLAsset | 已发布概念、Mapping、审核 Join、指标规则 | SQLAsset 召回、重排、模板和 AST 改写 | 确定性编译 |
| Ontology full | 上述本体知识及通过硬门槛的认证 SQLAsset | 未审核或非 READY 资产 | 确定性编译或安全 AST 改写 |

Schema 和 RAG Strategy 的构造函数只接受 `DatabaseCatalog`，无法取得
`OntologyService`。Physical RAG 索引构建时只读取原始历史 SQL 的问题、摘要和
SQL 文本，并主动丢弃认证、指标 ID、本体版本及 SQLAsset 解析结果。

## 校验器边界

- `CommonSQLSafetyValidator`：单语句、只读、危险操作、注释、多 Schema、物理表字段、SQL 长度。四组共用。
- `OntologyPolicyValidator`：生命周期、Physical Mapping、审核 Join、必要过滤、聚合角色、时间维度和支持维度。仅本体组使用。
- `EvaluationPolicyInspector`：对所有输出做隐藏评分，但不会返回 SQL、修改 SQL、修复 SQL或重新提示 Schema/RAG。

最终执行统一由 `QueryExecutor` 在只读事务内完成，并使用相同 statement timeout、
最大返回行数和 PostgreSQL `EXPLAIN`。

## PhysicalRAGIndex

每个文档包含：`document_id`、`document_type`、`table`、`column`、`data_type`、
`comment`、`pk_fk_summary`、`profile_summary`、`masked_samples`、`raw_sql`、
`search_text`、`embedding`、`source_hash` 和 `build_id`。

索引只表达物理证据。数据库外键和原始历史 SQL 可以提供 Join 线索，但 RAG Strategy
不能调用 Ontology JoinPlanner 或 SQLAssetService。

## Benchmark 与 Gold

`minibank-text2sql-v1` 包含 80 条完全虚构案例：

| 类别 | 数量 |
|---|---:|
| 基础指标 | 10 |
| 单维度、双维度 | 10 |
| 多表 Join | 10 |
| 时间、用户过滤和 IN | 10 |
| Top N 和排序 | 8 |
| CTE、窗口函数和子查询 | 10 |
| 同义词与金额字段歧义 | 8 |
| 生命周期、汇总粒度干扰 | 8 |
| clarification / unsupported | 6 |

Gold SQL 由 `scripts/generate_benchmark.py` 中受控模板生成，不调用任何待评测模型或
Strategy。审核链路为：受控模板 → SQLGlot → Common Validator → Ontology Policy →
PostgreSQL EXPLAIN → 实际执行 → ResultNormalizer → SHA-256。
固定 Seed 的审核 Hash 单独保存在 `data/benchmark/text2sql_v1_hashes.json`；重新生成
Benchmark 会复用这些值，`materialize_benchmark_hashes.py --check` 只验证、不改写。

## 结果规范化

列名统一为小写；Decimal 转规范十进制字符串；float 使用稳定有效位；日期时间使用
ISO 8601；NULL 保留 JSON `null`；布尔值保持布尔类型。无顺序语义时按整行紧凑 JSON
稳定排序；Top N、排名和窗口案例保留数据库结果顺序。最终对包含列定义与行数据的紧凑
JSON 计算 SHA-256。

## 指标

- `status_accuracy = 状态预测正确数 / 全部案例`
- `semantic_query_accuracy = 指标、维度、语义过滤和时间范围全部正确数 / 有 Gold Semantic Query 案例`
- `table_recall_at_k = Top-K 物理表与 Gold 表交集 / Gold 表`（仅 Physical RAG）
- `column_recall_at_k = Top-K 物理字段与 Gold 字段交集 / Gold 字段`（仅 Physical RAG）
- `table_exact_match = 表集合完全一致数 / success 案例`
- `column_exact_match = 字段集合完全一致数 / success 案例`
- `join_exact_match = 无向 Join 边集合一致数 / 有 Gold Join 案例`
- `deprecated_table_false_selection_rate = 生命周期误选数 / 干扰案例`
- `business_policy_accuracy = 业务口径全部正确数 / business_policy 案例`
- `sql_parse_rate = SQLGlot 可解析数 / success 案例`
- `sql_execution_rate = Common、EXPLAIN、执行均成功数 / success 案例`
- `result_accuracy = 结果 Hash 一致数 / 有 Gold Hash 案例`
- `template_adoption_rate = 采用兼容模板数 / 可采用模板的 ontology full 案例`
- 延迟报告 average、P50、P95，并单独统计失败类型分布。

不适用指标返回 `null`，实际适用但全部失败返回 `0`。

## 可复现字段

每次 `EvaluationRun` 固化 run ID/kind、Strategy Variant、Provider/Model、temperature、
输出 Token、请求超时、Git SHA、Strategy/Prompt 版本、数据库快照 Hash、Benchmark
Version/Hash、Physical RAG Build、Ontology Version/Bundle Hash、SQLAsset Build、随机种子、
并发数以及开始/完成时间。

`compare` 对 run kind、Provider/Model、temperature、输出 Token、超时、Git SHA、Benchmark
Version/Hash、数据库快照、Strategy/Prompt 版本、随机种子、并发数和案例上限做强制一致性
检查；两个 Ontology 组还必须使用相同 Ontology Version 与 Bundle Hash。任一关键字段不一致
或缺失都会拒绝比较，旧 `allow_mismatch` 参数仅为调用兼容保留，不能绕过硬门槛。Smoke 与
live 不能混合。

## CLI

```powershell
python -m data_asset_agents.evaluation.cli validate-benchmark
python -m data_asset_agents.evaluation.cli run --mode schema --run-kind live
python -m data_asset_agents.evaluation.cli run --mode rag --run-kind live
python -m data_asset_agents.evaluation.cli run --mode ontology --sql-assets disabled --run-kind live
python -m data_asset_agents.evaluation.cli run --mode ontology --sql-assets enabled --run-kind live
python -m data_asset_agents.evaluation.cli compare
python -m data_asset_agents.evaluation.cli export --run-id RUN_ID --format csv
```

正式运行应显式传入四个 Run ID，并使用 `compare --output comparison.json
--manifest-output manifest.json` 固化聚合结果与完整 Run Manifest。CLI 导出默认清除
`raw_model_output`；只有未提交的本地诊断文件才可显式使用 `--include-sensitive-debug`。

完整 80 条 live 实验应优先使用 CLI；CI 只运行少量无 API Key 的 smoke 案例。

## Live 正式本体与 Git SHA 前置条件

Live Evaluation 不允许回退 `yaml-seed-*`。运行前先把当前 40 位提交 SHA 注入 Docker
构建和运行环境，并幂等准备正式本体：

```powershell
$env:GIT_COMMIT_SHA = git rev-parse HEAD
docker compose build api
docker compose up -d postgres
docker compose run --rm api python scripts/prepare_live_evaluation.py
docker compose up -d api web
```

`prepare_live_evaluation.py` 只使用允许的 Direct Object Seed：已有正式版本时复用；
没有时依次执行 Draft、Validate、Submit、Approve、Publish，再验证 READY
CompiledOntologyArtifact、非空 resource/bundle hash、compiler version，并构建或复用
READY Ontology IndexBuild 与 SQLAssetBuild。脚本中的 actor 是审计身份和自动化实验执行
主体，不等同于真实用户在 UI 中完成独立人工审批；生产治理仍要求真实审核人显式批准。

Live runtime 缺少 PUBLISHED 版本、READY 编译产物、READY Ontology Index，或 Full 组
缺少同版本 READY SQLAssetBuild 时直接失败，不回退 YAML。mock/smoke 仍允许无 API Key
和 YAML Seed fallback。

`compare` 会拒绝空 SHA、`unknown`、非 40 位十六进制 SHA 或四组不同 SHA，错误为：

```text
Fairness mismatch: valid git_commit_sha provenance is missing
```

2026-07-23 的正式四组 80-case 结果、公平性证据和逐案例错误分析见
[`evaluation-results.md`](evaluation-results.md) 与
[`../artifacts/evaluation/705b017f00b0526cd244ab117ebe8cd02dfc03e7/error-analysis.md`](../artifacts/evaluation/705b017f00b0526cd244ab117ebe8cd02dfc03e7/error-analysis.md)。
