# Data Asset Agents

Data Asset Agents 是一个面向企业数据资产研发场景的多智能体项目。本分支交付第一阶段 **Text-to-SQL MVP**：以人工审核的本体语义层为事实源，使用 LangGraph 编排“概念理解、确定性资产映射、Join 规划、SQL 生成、校验、执行与解释”闭环。

> 安全声明：仓库中的 MiniBank、表结构、业务概念、SQL 和数据均为公开演示目的自行构造，与任何真实机构无关。禁止向本仓库提交真实数据、非公开表结构、非公开 SQL、非公开规则或 API Key。

## MVP 能力

- YAML 人工审核本体；PostgreSQL 运行时语义仓库结构；pgvector 扩展接口
- “自然语言 → 标准业务概念 → 确定性映射 → 表/字段/Join”的选表链路
- 11 节点 LangGraph，可独立运行，也可作为 Subgraph 接入总控 Agent
- 生命周期策略排除 `DEPRECATED`、`TEMPORARY`、`TEST` 资产
- NetworkX 基于审核 Join Graph 规划路径
- SQLGlot 单语句、只读、字段归属、生命周期、审核 Join 和指标过滤校验
- 仅对“缺失必要指标过滤条件”执行确定性修复；不可修复或 SQL 未变化时停止
- PostgreSQL `EXPLAIN` + 只读事务 + 超时 + 最大行数保护
- FastAPI 接口和 Streamlit 汇报页面
- DeepSeek/Qwen OpenAI 兼容客户端配置；无 Key 时默认确定性 mock 模式
- 固定随机种子 `20260715` 生成的纯虚构 MiniBank 数据

## 核心链路

```text
问题 → Semantic Query → 标准概念 → 正式物理资产 → Join Graph
    → 认证 SQL → SQL 生成 → SQLGlot 校验 → EXPLAIN/只读执行 → 解释
```

向量检索只用于业务概念候选和认证历史 SQL 候选，不允许绕过本体映射直接搜索并选择物理表。

## 目录

```text
apps/api/                    FastAPI
apps/web/                    Streamlit
src/data_asset_agents/
  ontology/                  模型、YAML 仓库、发布器与服务
  text2sql/                  State、Graph、节点和 Join 工具
  validation/                SQLGlot 安全校验
  execution/                 PostgreSQL 只读执行器
  metadata/                  离线元数据抽取骨架
ontology/retail_banking/     人工审核语义定义源
data/ddl/                    MiniBank DDL
data/seed/                   固定种子生成器及生成 SQL
data/historical_sql/         认证历史 SQL 示例
tests/                       核心路径测试
```

## Windows + Docker Desktop 启动

要求：Docker Desktop 已启动，并启用 Docker Compose v2。默认 mock 验收无需本地 Python 和 API Key。

```powershell
git clone https://github.com/BaBaLiBoo/data-asset-agents.git
cd data-asset-agents
git switch feature/text2sql-mvp
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

访问：

- Streamlit：http://localhost:8501
- FastAPI 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

运行目标用例：

```powershell
$body = @{
  question = "查询近30天各分行信用卡交易金额和交易笔数。"
  query_mode = "ontology"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/query `
  -ContentType "application/json" -Body $body
```

停止并保留数据：

```powershell
docker compose down
```

需要重新执行初始化 SQL 时删除纯演示卷：

```powershell
docker compose down -v
docker compose up --build -d
```

## 本地 Python 开发

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest
uvicorn apps.api.main:app --reload
```

另开终端启动前端：

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run apps/web/app.py
```

如需重新生成固定种子数据：

```powershell
python data/seed/generate_seed.py
```

## mock / live 配置

Docker Compose 使用 `${VARIABLE:-default}` 把 `.env` 中的配置传入容器，不再强制覆盖 `LLM_MODE`。默认配置如下：

```dotenv
LLM_MODE=mock
LLM_API_KEY=
EMBEDDING_API_KEY=
```

此时目标演示问题完全依赖审核本体和确定性规则，不调用外部模型。切换 OpenAI 兼容客户端配置时修改本地 `.env`：

```dotenv
LLM_MODE=live
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-key

EMBEDDING_PROVIDER=aliyun
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=your-key
```

业务代码只依赖 LangChain 的 OpenAI 兼容适配器。`.env` 已被 Git 忽略，仓库只保留无密钥的 `.env.example`。

注意：第一阶段在线查询节点尚未启用通用 live LLM 生成。`LLM_MODE=live` 只代表模型客户端配置可被创建，不代表系统已经具备任意问题生成能力；不支持的问题仍返回 HTTP 422 和 `status=unsupported`。

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | API 与数据库健康状态 |
| POST | `/api/v1/query` | 执行完整 LangGraph |
| POST | `/api/v1/semantic/parse` | 仅解析 Semantic Query |
| POST | `/api/v1/semantic/search` | 搜索标准业务概念 |
| POST | `/api/v1/semantic/resolve` | 确定性解析表、字段与规则 |
| GET | `/api/v1/ontology/concepts` | 查看业务概念 |
| GET | `/api/v1/ontology/metrics` | 查看指标口径 |
| GET | `/api/v1/ontology/tables` | 查看表资产与生命周期 |
| GET | `/api/v1/graph` | 查看节点与边 |

不支持问题的响应示例：

```json
{
  "status": "unsupported",
  "error_code": "UNSUPPORTED_QUERY",
  "detail": "未识别到第一阶段本体支持的业务指标"
}
```

## 完整验收

Windows PowerShell 一键验收脚本会校验 Compose、构建并启动容器、等待健康检查、调用目标查询、检查非空结果、输出容器状态，并在结束或失败后执行 `docker compose down`：

```powershell
Copy-Item .env.example .env
.\scripts\acceptance.ps1
```

手工执行等价步骤：

```powershell
docker compose config --quiet
docker compose up --build -d
Invoke-RestMethod http://localhost:8000/health

$body = @{
  question = "查询近30天各分行信用卡交易金额和交易笔数。"
  query_mode = "ontology"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/query `
  -ContentType "application/json; charset=utf-8" -Body $body

docker compose ps
docker compose down
```

代码检查：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest
```

真实 PostgreSQL 集成测试由 GitHub Actions 自动初始化 pgvector PostgreSQL、执行 DDL 和 Seed，并设置 `RUN_POSTGRES_INTEGRATION=1`。本地运行集成测试时，应先启动并初始化 Docker 数据库，再设置相同环境变量。

## 发布 YAML 到运行时仓库

容器启动后，可把审核后的概念写入 PostgreSQL 运行时表：

```powershell
docker compose exec api python scripts/publish_ontology.py
```

YAML 始终是人工审核事实源；数据库是运行时副本。正式环境应在发布前增加审核状态、版本签名、回滚和审计记录。

## 当前边界

- 仅保证已审核指标、维度及其同义表达的确定性 mock 闭环；未识别问题会明确返回 unsupported，不会生成占位 SQL。
- `ontology` 是第一阶段唯一真实实现的查询模式；`rag`、`schema` 仅保留接口和前端选项，当前复用同一 ontology 链路，不代表独立能力。
- pgvector 表结构与模型适配已准备，但概念与历史 SQL 仍使用本地规则排序，尚未实现真实向量召回。
- 尚未开始本体自动构建。当前仅有人工审核 YAML、基础元数据抽取和发布骨架，没有样例值剖析、LLM 候选语义生成或审核 UI。
- 自动修复仅支持补齐校验器明确指出的必要指标过滤条件；字段错误、未审核 Join、禁用表和数据库执行错误不会被猜测性修改。
- 尚未实现通用 live LLM Structured Output、认证 SQL 管理、语义版本回滚、权限隔离、审计和完整评测平台。
