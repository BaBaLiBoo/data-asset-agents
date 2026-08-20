# 数据资产总控智能体

当前总控采用受控工作流：

```text
用户请求
→ 从当前用户、当前会话读取最近数据库消息并构建有限记忆
→ 大模型结合当前消息和历史摘要进行需求结构化（不可用时规则降级）
→ 业务参数完整性检查
→ 信息不足时追问
→ 信息完整时生成白名单显式任务计划
→ 严格按计划选择并调用一个专业 Tool
→ 检查专业服务状态与结果
→ 大模型只读反思指标、维度、时间、表字段或查重证据覆盖情况
→ PostgreSQL 持久化会话、任务和结果版本
→ 返回统一结果
→ 用户反馈（采纳 / 修改后采纳 / 不满意重试）
→ 更新有效状态或在同一任务下生成下一版本
```

任务一和任务二相互独立。总控不计算相似度、不生成 SQL、不校验 SQL，一次请求最多调用一个 Tool。任务一当前接入真实服务的资产检索能力，返回可复用候选并要求人工复核，不把召回分数当作正式重复结论。

本分支只包含可独立运行的总控服务；完整工作流和接口约束以本文档为准。

## 专业服务对接

总控通过 Adapter 调用真实专业服务，而不是把总控统一契约直接透传：

```text
check_asset_duplicate
→ AssetHttpTaskAgent
→ POST {ASSET_SERVICE_URL}/api/v1/task1/search-by-text
→ Task1候选响应转换为AssetServiceResponse

generate_sql
→ SqlHttpTaskAgent
→ POST {SQL_SERVICE_URL}/api/v1/query
→ Text2SQL响应转换为SqlServiceResponse
```

Task1 的当前 Demo 入参不是完整 `AssetInput`，因此这里只做自然语言及可选 SQL 增强检索。要执行正式三层判重，必须先补齐业务域、粒度、表字段元数据和血缘，再调用 Task1 的 `/api/v1/task1/search-duplicates`。

`text2sql-mvp` 固定使用 `query_mode=ontology` 和 `sql_asset_enabled=true`，只依赖 FastAPI，不调用或依赖 Streamlit 前端。任务三仍沿用既有血缘服务协议。

专业服务根地址配置在 `.env`：

```dotenv
ASSET_SERVICE_URL=http://127.0.0.1:8001
SQL_SERVICE_URL=http://127.0.0.1:8002
LINEAGE_SERVICE_URL=http://127.0.0.1:8003
```

## 总控入口

建议使用 Python 3.11 或更高版本。克隆 `demo` 分支后，在仓库根目录安装依赖并启动总控：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m alembic upgrade head
python -m uvicorn supervisor_main:app --reload --port 8000
```

Swagger：

- 总控：http://127.0.0.1:8000/docs

## 模型配置

复制 `.env.example` 为 `.env` 后填写模型配置。配置模型后：

- `AUTO` 请求由模型判断专业场景并提取结构化参数；
- 显式场景只向模型开放指定的一个 Tool，用模型提取参数但不允许改选任务；
- 同一会话内，模型会收到最近需求、场景、澄清和专业结果摘要，用于理解
  “改成180天”“沿用刚才的表”和澄清后的补充；
- 模型不可用时自动降级到显式场景或关键词规则；
- 模型不可用时，如果当前消息依赖“刚才/沿用/改成”等历史指代，总控会追问
  完整条件，不会猜测历史参数；
- 无论是否使用模型，信息不足都先返回追问，不调用专业服务。
- 专业结果合法返回后，模型通过结构化输出生成只读检查意见，不绑定专业Tool。

修改 `.env` 后请重启总控进程。

会话记忆窗口可配置：

```dotenv
MEMORY_MAX_MESSAGES=12
MEMORY_MAX_CHARS=6000
```

记忆只读取当前 `X-User-Id` 对应的当前 `conversationId`。新会话返回空记忆；
其他用户的会话和已软删除会话会在调用大模型或专业 Tool 前被拒绝。摘要保留用户
需求、专业场景、状态、追问信息和推荐表/字段等必要结果要点，不把完整 SQL 放入
Prompt。

## 显式任务规划

信息完整后，Supervisor会在专业服务调用前生成 `READY` 状态的受控计划。计划只
允许固定步骤和 `check_asset_duplicate`、`generate_sql` 两个专业Tool，实际执行
必须与计划中的 `selectedTool` 一致。

最终响应的 `executionPlan` 包含计划状态、步骤状态和以下硬约束：

```text
maxProfessionalToolCalls = 1
professionalToolCalls = 0 或 1
allowCrossScene = false
requireHumanReview = true
```

澄清时计划停在 `WAITING_INPUT` 且调用数为0；专业结果返回后进入 `REVIEW`；
用户采纳后推进为 `COMPLETED`。计划随任务结果JSON持久化，无需新增数据库表。

## 结果反思与检查

专业结果通过确定性检查后，`ResultReflectionAgent` 会结合原始需求、结构化参数、
当前会话有限记忆和专业结果，输出 `reflection`：

- SQL场景检查指标、维度、粒度、时间范围、过滤条件、推荐表字段和风险；
- 资产场景检查语义、逻辑、血缘证据、候选、阈值和复用建议；
- 状态为 `PASSED`、`WARNING` 或 `UNAVAILABLE`；
- 反思不修改专业结果、不自动重试，也不调用任务一或任务二；
- 模型不可用时，计划中的 `REFLECT` 为 `SKIPPED`，专业结果仍等待人工确认。

反思使用独立的模型客户端，并默认通过 `REFLECTION_ENABLE_THINKING=false`
关闭思考模式。这是因为当前通义思考模式不支持LangChain函数式结构化输出内部使用的
`tool_choice=required`。`REFLECTION_MODEL` 未配置时沿用 `DASHSCOPE_MODEL`，
需求理解模型的思考模式和单Tool路由不受影响。

只有用户提交 `REJECT` 且 `retry=true` 时，现有反馈闭环才会调用原专业Tool生成
同一任务的下一版本。反思字段随现有结果JSON持久化，不需要Alembic迁移。

前端可以把 `WARNING` 检查项映射成反馈原因码并生成可编辑草稿。生成草稿本身
不会请求总控；只有用户确认提交后，才通过现有 `/api/v1/feedback` 进入重试。
后端继续校验反馈版本和任务状态，重复或过期提交返回409。

## 反馈闭环与任务存储

配置 `DATABASE_URL` 后，总控将以下信息持久化到PostgreSQL：

```text
用户、会话、消息、任务、结果版本、反馈事件
```

连接配置只写入 `.env`：

```dotenv
DATABASE_URL=postgresql+psycopg://asset_agent_user:password@127.0.0.1:5432/asset_agent
```

首次运行和代码升级后执行：

```powershell
python -m alembic upgrade head
```

未配置 `DATABASE_URL` 时回退到 `TASK_DATABASE_PATH` 指定的SQLite，主要用于
离线开发；自动化测试使用SQLite内存数据库。

提交反馈：

```text
POST /api/v1/feedback
```

```json
{
  "taskId": "task_xxx",
  "resultId": "result_xxx",
  "decision": "ACCEPT",
  "reasonCodes": [],
  "comment": "",
  "retry": false
}
```

- `ACCEPT`：采纳当前结果，任务有效状态变为 `SUCCESS`，不重新执行。
- `EDIT_AND_ACCEPT`：将 `editedContent` 保存为最终采用内容，状态变为
  `SUCCESS`，不重新执行。
- `REJECT`：必须填写原因；当 `retry=true` 时，携带纠错上下文在同一个
  `taskId` 下生成下一版本。

查询任务当前有效结果：

```text
GET /api/v1/tasks/{task_id}
```

查询任务全部原始结果版本：

```text
GET /api/v1/tasks/{task_id}/results
```

查询当前用户会话历史：

```text
GET /api/v1/conversations
X-User-Id: 当前用户标识
```

查询指定会话消息：

```text
GET /api/v1/conversations/{conversation_id}/messages
X-User-Id: 当前用户标识
```

逻辑删除单条会话或清空当前用户的会话历史：

```text
DELETE /api/v1/conversations/{conversation_id}
DELETE /api/v1/conversations
X-User-Id: 当前用户标识
```

删除后的会话不再出现在历史列表中，也不能继续读取消息或写入新任务；
任务、结果和反馈审计记录不会被物理删除。

同一个 `conversationId` 不能被不同用户复用，其他用户查询该会话时返回404。

采纳不会覆盖专业智能体返回的原始结果。任务查询接口返回闭环后的有效状态，
版本历史接口保留原始 `REVIEW_REQUIRED` 结果，便于审计。

## 测试

```powershell
python -m pytest tests -q
```

集成测试使用 ASGI 内存传输，不要求手动启动三个服务。

当前测试覆盖总控路由、HTTP 专业智能体调用、采纳、修改后采纳、拒绝重试、
同任务版本递增、重复反馈冲突、会话历史、多轮需求修订、澄清续接、记忆窗口、
会话隔离、受控计划、计划与路由一致性、单Tool计数和软删除调用前拦截等场景。
