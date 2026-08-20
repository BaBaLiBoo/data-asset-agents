# 数研智枢统一智能研发前端

React 工作台通过同源接口调用 FastAPI 总控，浏览器不直接访问任务一、任务二服务。

## 后端调用链

浏览器和前端 API Route 只调用 `demo` Supervisor，不直接访问专业服务：

```text
前端
→ Supervisor
  ├─ AssetHttpTaskAgent → task1 /api/v1/task1/search-by-text
  ├─ SqlHttpTaskAgent   → text2sql-mvp /api/v1/query
  └─ LineageClient      → 血缘服务
```

任务一当前返回可复用资产候选，候选需要人工复核，不代表已经完成正式三层判重。
任务二基于已发布本体生成 PostgreSQL，并只在受控演示库执行只读查询。

## 前端配置

复制 `.env.example` 为 `.env.local`：

```dotenv
API_MODE=real
BACKEND_TARGET=local
LOCAL_SUPERVISOR_API_URL=http://127.0.0.1:8000
REMOTE_SUPERVISOR_API_URL=http://129.211.23.161/supervisor
FRONTEND_USER_ID=frontend-user
```

只需修改 `BACKEND_TARGET` 一行即可切换后端：`local` 使用本机 Supervisor，
`remote` 使用远程 Ubuntu Supervisor。修改后需要重新启动前端开发服务器。

应用默认使用真实总控。`mock` 模式仅用于离线演示和自动化测试。
本地开发时，`FRONTEND_USER_ID` 是会话历史的用户隔离标识；聊天、反馈、
任务查询和历史查询必须使用同一个值。

真实模式使用以下同源接口：

- `POST /api/chat/stream`：提交研发需求；
- `POST /api/feedback`：提交采纳、修改后采纳或不满意反馈；
- `GET /api/tasks/{taskId}`：查询任务闭环后的有效状态。
- `GET /api/conversations`：查询当前用户的数据库会话列表；
- `GET /api/conversations/{conversationId}/messages`：恢复完整会话消息；
- `DELETE /api/conversations/{conversationId}`：逻辑删除当前用户的一条会话；
- `DELETE /api/conversations`：逻辑清空当前用户的会话历史。

删除操作只会让会话从当前用户的历史列表中消失，不会物理删除任务、结果和审计记录。

## 启动

建议使用 Node.js 22.13 或更高版本。克隆 `frontend` 分支后，在仓库根目录执行：

```powershell
Copy-Item .env.example .env.local
npm.cmd ci
npm.cmd run dev
```

默认页面：

```text
http://localhost:5173
```

## 反馈闭环

- `采纳`：当前结果直接变为成功状态，执行轨迹显示“人工确认结果”和“任务已完成”。
- `修改后采纳`：保存编辑后的 SQL 或文本作为最终内容，不触发重新生成。
- `不满意`：提交原因并触发重试；新结果沿用原 `taskId`，版本从 `v1` 递增到 `v2`。

任务、反馈和完整会话消息由后端 PostgreSQL 持久化。页面启动时优先从后端
恢复会话、结果版本和反馈状态；`localStorage` 仅在后端不可用或Mock模式下
作为离线缓存，不再是历史记录的唯一可信来源。

执行轨迹优先使用总控返回的 `executionPlan.steps`，并展示计划锁定的唯一专业
Tool。旧历史记录没有 `executionPlan` 时，前端保留原有状态推导作为兼容回退。

专业结果带有 `reflection` 时，前端在反馈区之前展示独立“智能检查”卡片，包括
检查状态、风险等级和分类意见，并明确提示“不修改结果、不自动重试”。旧历史没有
该字段时不显示卡片；`UNAVAILABLE` 不影响用户查看和确认专业结果。

当反思为 `WARNING` 且存在可处理检查项时，卡片显示“按检查意见重试”。点击只会
生成可编辑的 `REJECT` 反馈草稿，不会立即请求后端；用户点击“确认重试”后才提交
现有反馈接口。检查类别会映射为稳定原因码，最多合并8项、反馈说明最多3000字符。
新结果保持原 `taskId` 并增加版本，只调用原专业Tool一次，仍不会自动产生下一版。

## 验证

```powershell
npm.cmd test
npx.cmd tsc -p tsconfig.app.json --noEmit
```

## 当前边界

- 当前历史接口一次加载当前用户的全部会话和消息，尚未增加分页；
- 本地使用 PostgreSQL，生产环境仍需替换数据库凭据和连接；
- 尚未接入真实登录鉴权与银行统一身份体系；
- 系统不连接生产数据库；生成的 SQL 仅允许在受控演示库中以只读方式执行。
