# Two-Surface DataBuddy Demo

This demo has two Streamlit pages only: `本体构建` and `智能体 Demo`.

## Part 1: 本体构建

1. Select `MiniBank PostgreSQL`.
2. Check the data status. If the page says the fictional data is not ready, confirm and click `导入 MiniBank 演示数据`.
3. Click `扫描数据结构并生成快照`. The page shows tables, columns, primary keys, foreign keys, row counts, and profiling summary.
4. Choose construction mode:
   - `O-C`: metadata, field profiling, and certified historical SQL.
   - `O-D`: O-C plus real AI semantic suggestions.
5. For O-D, confirm the AI status shows Live provider and model. Missing keys block O-D Live.
6. Generate candidates.
7. Review candidates with structured forms for ObjectType, Property, Metric, Dimension, Business Link, Physical Join, and Binding.
8. Modify one metric description and one business relationship, then accept the reviewed candidates.
9. Promote the reviewed run to a Draft.
10. Continue editing the Draft resources if needed.
11. Validate, submit, approve, publish, and explicitly activate the published ontology.

## Part 2: 智能体 Demo

1. Select `MiniBank PostgreSQL`.
2. Select a READY published ontology, normally `MiniBank 正式业务本体 v1.0` or the just-published version.
3. Confirm the AI mode. Live shows the configured DeepSeek chat model and DashScope embedding model; mock is marked as mock.
4. If the selected ontology is not active, confirm and click `启用所选本体`.
5. Ask three questions:
   - 查询交易金额
   - 按客户类型统计交易笔数
   - 查询近30天各分行信用卡交易金额
6. For each answer, show the business answer first, then the result table, then `为什么这样查询`.
7. Expand `查看 SQL` only when the audience wants to see the generated SQL.
8. Keep `技术详情` folded unless debugging is needed.

## Talk Track

“我们先从数据库开始，而不是从手写 JSON 开始。这里的 MiniBank 是一套纯虚构的演示数据源。系统扫描 PostgreSQL，抽取表结构、主外键、索引和字段画像，然后结合认证历史 SQL 生成本体候选。

候选不会直接上线。业务专家在这里逐条看证据、改名称、改口径、改关系，最后明确接受、修改后接受、拒绝、合并或暂缓。全部处理完以后，系统才把候选提升成正式 Draft。

Draft 是可以继续完善的正式变更集。这里的对象、属性、指标、维度、业务关系、物理 Join 和绑定都可以继续增删改。发布前必须经过 Validate、Submit、Approve、Publish。发布后也不会静默上线，必须显式激活。

进入智能体 Demo 后，我们选择数据库、选择已发布本体，再确认 AI 模型。问题会固定走正式本体语义层，系统先识别业务指标和维度，再解析物理表字段和 Join，最后生成并校验 SQL。页面默认展示业务答案和为什么这样查，技术细节折叠起来。”
