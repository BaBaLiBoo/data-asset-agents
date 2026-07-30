# Ontology Demo Workflow

MiniBank 演示工作台把自动构建、人工治理和智能问数串成一条可演示流程。

## 自动构建与正式本体

自动构建只产生候选资源。O-C 使用元数据、字段画像和认证历史 SQL；O-D 在 O-C 基础上增加 LLM 语义建议。候选资源带证据和置信度，但仍然可能有对象边界、指标口径、Link 语义或物理绑定问题，所以不能直接上线。

正式本体来自 OntologyDraft 的发布产物。Draft 是 ObjectType、Property、Metric、Dimension、Business Link、Object Binding 和 Physical Join 的原子变更集。只有通过 Validate、Submit、Approve、Publish 的 Draft 才能成为可激活的 OntologyVersion。

## 人工审核可以修改什么

候选审核支持 ACCEPT、MODIFY、REJECT、MERGE、DEFER。业务人员可以通过结构化表单修改七类资源：

- ObjectType：名称、复数名称、业务边界、对象分类、主键属性、标题属性、生命周期、同义词。
- Property：所属对象、名称、描述、数据类型、Semantic Role、可空、敏感、可分组、可过滤、单位、同义词。
- Metric：名称、口径、聚合方式、Measure Property、固定过滤条件、Time Property、支持维度、同义词、生命周期。
- Dimension：名称、描述、Property、Time Grain、同义词、生命周期。
- Business Link：业务关系名称、源对象、目标对象、角色、基数、关联 Physical Join、生命周期。
- Physical Join：左右表字段、关系、生命周期和审核证据。
- Binding：Object 到数据源表的绑定、主键列和 Property -> Column 映射表。

Stable ID 只读。高级 JSON 编辑被保留在折叠区，用于调试，不是普通用户主入口。

## Promote to Draft

候选全部进入最终状态后，工作台显示“生成可编辑本体草稿”。调用 `POST /api/v1/ontology/construction-runs/{run_id}/promote-to-draft` 后，前端记录 `promoted_draft_id`，把它设为当前 Draft，并自动进入“草稿完善”。浏览器刷新后，工作台会从 Construction Run 或 SessionState 恢复当前 Draft。

## Validate / Submit / Approve / Publish

所有 Draft 写操作都携带 If-Match Revision。Revision 冲突时前端提示刷新，不会静默覆盖。

发布链路为：

```text
Validate -> Submit -> Approve -> Publish -> Activate
```

未校验修改只能先 Validate；Validate 失败不能 Submit；Draft 未进入 IN_REVIEW 不能 Approve；Draft 未进入 VALIDATED 不能 Publish；Breaking Change 必须确认并填写变更工单。

## OntologyService 如何加载正式本体

发布或激活版本时，后端复用现有 `_activate_runtime`：重新加载 OntologyService、QueryExecutor、SQLAssetService、Text-to-SQL LangGraph、EvaluationPolicyInspector，并重建本体搜索索引。Demo Status 会展示当前版本、Compiled Artifact、Ontology Index、SQLAsset Build、Object Graph 和示例查询检查。

## Text-to-SQL 如何调用正式本体

智能问数页调用 `/api/v1/query`。Ontology 模式读取当前激活的 OntologyService：先识别业务指标和维度，再解析过滤与时间范围，然后使用正式本体的 Property Binding 和 Physical Join 编译 SQL。SQLAsset 只作为认证模板参与召回和 AST 改写，不能绕过本体映射。

## O-C / O-D 与正式演示版本

Reviewed O-C 是“自动候选审核版本”，用于说明基于元数据、画像和认证历史 SQL 的候选质量。Reviewed O-D 是“LLM 增强候选审核版本”，用于说明大模型只提供隔离建议。`quality-v2-gold-independent` 在 UI 中展示为“MiniBank 正式业务本体 v1.0”，是演示中一键启用的正式版本。
