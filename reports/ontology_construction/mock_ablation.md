# Ontology Construction Mock Ablation

本报告由公开 MiniBank DDL、虚构 seed 和认证历史 SQL 确定性生成；Gold 仅在候选生成完成后用于模拟审核和评分。

| Run | Catalog | Object F1 | Dimension F1 | Metric F1 | 接受率 | 修改率 | 拒绝率 |
|---|---|---:|---:|---:|---:|---:|---:|
| mock-O-A-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.000 | 0.000 | 0.543 | 0.457 |
| mock-O-B-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.000 | 0.000 | 0.543 | 0.457 |
| mock-O-C-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.800 | 0.000 | 0.562 | 0.438 |
| mock-O-D-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.800 | 0.000 | 0.562 | 0.438 |
| mock-O-C-governed_catalog | GOVERNED_CATALOG | 1.000 | 1.000 | 0.800 | 0.000 | 0.562 | 0.438 |

## 解释

- O-A 只验证 schema 对对象、属性和绑定骨架的贡献。
- O-B 加入画像，当前规则主要改善枚举/敏感性建议；不会凭画像创造业务指标。
- O-C 的认证 SQL AST 才能提出聚合、固定过滤、时间字段和支持维度。
- O-D 在本报告中使用 mock 确定性语义增强，因此不声称存在 live LLM 增益。
- 对象边界、Link 业务命名、冲突过滤和指标口径仍需人工确认。
