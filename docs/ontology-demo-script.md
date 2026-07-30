# MiniBank 5-8 分钟演示稿

## 第一步：扫描 MiniBank 数据源

打开 Streamlit，先看“演示首页”。这里能看到当前 API、PostgreSQL、正式本体版本、Artifact、Index 和 SQLAsset 状态。进入“本体构建与治理”，第一步选择 `public` schema，点击“创建 RAW_METADATA Snapshot”。页面会展示表数量、字段、主键、外键、索引和字段画像。这里的数据都是 MiniBank 的虚构演示数据，不包含真实银行信息。

## 第二步：自动生成候选

进入“自动构建”。默认选择 O-C，因为它同时使用元数据、字段画像和认证历史 SQL。Catalog Mode 选择 `GOVERNED_CATALOG`，LLM Mode 保持 mock。创建 Construction Run 后点击“生成候选”。生成完成后能看到 Run ID、Evidence Mode、Catalog Mode、候选数、排除表、Warning 和 Error。

## 第三步：人工修改一个指标、一个 Link、一个 Property

进入“候选审核”。页面是三栏布局：左边是物理证据，中间是结构化编辑表单，右边是最终资源预览和 Diff。

先筛选 Metric，选择一个交易金额相关指标，把描述补成“仅统计已入账交易金额”，用结构化过滤条件补充状态过滤，然后选择“修改后接受”。

再筛选 LinkType，打开交易到分行的业务关系，确认页面提示 Business Link 表达业务关系、Physical Join 表达字段连接。把业务关系名称改成“交易归属分行”，关联已审核的 Physical Join，然后修改后接受。

最后筛选 Property，选择客户类型或交易状态字段，补充中文描述、可分组或可过滤标记，再修改后接受。对明显不该进入本体的候选可以拒绝，说明原因；重复候选可以合并；暂时无法判断的可以延期。

## 第四步：提升到 Draft

当候选都完成最终决定后，点击“生成可编辑本体草稿”。系统会调用 Promote API，把审核后的候选变成 OntologyDraft，并自动进入“草稿完善”。这里会显示 Draft ID、Revision、Resource Hash、资源数量和来源 Construction Run。

## 第五步：校验并发布

在“草稿完善”里可以继续新增、编辑或删除对象、属性、指标、维度、业务关系、数据源绑定和 Physical Join。所有修改都会让 Validation 过期。

进入“校验发布”，先点 Validate。通过后再 Submit，进入审核态后点 Approve，最后 Publish。如果出现 Breaking Change，需要勾选确认并填写变更工单。发布成功后可以在“完成”页看到版本列表、Artifact、Index 和 SQLAsset 状态。

## 第六步：激活 MiniBank 正式业务本体 v1.0

回到“演示首页”。如果配置的 `quality-v2-gold-independent` 版本存在但不是当前版本，页面会显示“启用正式演示本体”。点击后系统会激活它，并重新读取 Object Graph、刷新 OntologyService、Index 和 SQLAsset。UI 中不要把它叫 Gold，而是展示为“MiniBank 正式业务本体 v1.0”。

## 第七步：执行三条智能问数

进入“智能问数”，先确认页面顶部显示当前正式本体名称、Version ID、Bundle Hash、Artifact 和 SQLAsset 状态。

第一条问：“查询交易金额”。看系统如何识别交易金额指标、编译 SQL、校验并返回结果。

第二条问：“按客户类型统计交易笔数”。重点看维度识别为客户类型，映射到客户维表字段，并通过 Join Plan 连接交易和客户。

第三条问：“查询近30天各分行信用卡交易金额”。重点看时间过滤、信用卡过滤、分行维度和 SQLAsset 是否命中。打开“为什么这样查询”，可以看到指标口径、维度、物理字段映射和 Join 依据。技术细节默认收起，只有需要解释 LangGraph State 或原始 JSON 时再展开。
