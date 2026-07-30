# Ontology Demo UI Audit

审计时间：2026-07-30  
审计范围：`apps/web/app.py`、`apps/api/main.py` 以及本轮要求列出的本体构建、Draft、发布、版本激活、Object Graph 和 Query API。

## 现状结论

1. `ontology_builder_page` 的旧式 Concept / Mapping / Join 工作台仍在旧主导航中作为“本体构建与审核”入口使用。它调用 legacy `/api/v1/ontology/build`、`/api/v1/ontology/candidates`、`/api/v1/ontology/publish`，仍以 Concept / Mapping / Join 为主，不是对象优先的主流程。
2. `ontology_construction_page` 可以创建 RAW_METADATA Snapshot、创建 Construction Run、生成候选、查看 raw evaluation、用 JSON 修改候选、提交 ACCEPT / MODIFY / REJECT / MERGE / DEFER，并调用 promote-to-draft。
3. `ontology_manager_page` 可以创建空 Draft、从对象种子创建 Draft、复制当前发布版本、迁移 legacy YAML、编辑 Object / Property / Metric / Dimension / Binding / Business Link / Physical Join，运行 Validate / Submit / Approve / Publish，查看 Diff / Impact / Drift / Index / Object Explorer。
4. 两个页面之间主要依赖 `st.session_state["construction_snapshot"]`、`st.session_state["construction_run"]`、`st.session_state["manager_draft"]`、`st.session_state["manager_revision"]`。这些 key 分散在页面内部，没有统一定义。
5. Promote 后不能直接打开对应 Draft 的原因是 Construction 页面只显示 `run.promoted_draft_id`，没有写入 Manager 使用的 `manager_draft`，也没有切换页面或步骤；刷新后也只能通过 run 详情看到 ID。
6. 候选审核当前主要通过“修改后的 JSON”完成。ObjectType、Property、Metric、Dimension、Binding、LinkType、PhysicalJoin 都缺少面向普通业务用户的完整结构化候选表单。
7. 缺少的结构化控件：
   - ObjectType：复数名称、对象分类、主键属性、标题属性、生命周期、同义词和证据摘要没有完整表单。
   - Property：描述、nullable、sensitive、groupable、filterable、unit、synonyms、profiling / SQL 证据没有统一编辑和展示。
   - Metric：固定过滤条件只能以 JSON 修改，缺少 Property / Operator / Value 列表编辑。
   - Dimension：缺少 time grain 和使用频率 / 基数 / 枚举证据展示。
   - LinkType：业务关系和 Physical Join 的职责提示不足，关联 join 缺少产品化选择。
   - Binding：旧 Manager 一次只保存单属性绑定，容易覆盖已有 `property_bindings`。
   - PhysicalJoin：旧 Manager 使用文本框填写表和列，普通用户可能输入不存在的列。
8. 发布后在线 OntologyService 的刷新目前由后端 `_activate_runtime` 完成；UI 只显示成功信息，没有集中展示 Artifact、Index、SQLAsset Build、Object Graph 和基础查询检查。
9. 查询页旧版只展示 Text-to-SQL 执行概览，没有明确、固定地展示当前本体展示名、Version ID、Bundle Hash、Artifact Status 和 SQLAsset 状态。
10. 本轮新增/拆分/修改文件清单：
    - 新增 `apps/web/api_client.py`
    - 新增 `apps/web/state.py`
    - 新增 `apps/web/navigation.py`
    - 新增 `apps/web/components/*`
    - 新增 `apps/web/pages/*`
    - 新建简化 `apps/web/app.py`
    - 保留旧页面为 `apps/web/legacy_app.py`
    - 修改 `apps/api/main.py`，新增 `GET /api/v1/ontology/demo-status`
    - 修改 `src/data_asset_agents/core/config.py`
    - 修改 `.env.example` 和 `docker-compose.yml`
    - 新增 `docs/ontology-demo-workflow.md`
    - 新增 `docs/ontology-demo-script.md`
    - 新增 `scripts/demo_flow_acceptance.ps1`

## 本轮 UI 调整原则

旧的三个并列本体入口不再放在主导航。普通演示用户从“演示首页”进入“本体构建与治理”，按数据源扫描、自动构建、候选审核、草稿完善、校验发布、完成六步走完。旧 Builder、旧 Construction、旧 Manager 仍保留在“高级管理”中用于工程回归和调试。
