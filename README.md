# Data Asset Agents

Data Asset Agents 是一个面向企业数据资产研发场景的多智能体项目。本分支交付第一阶段 **Text-to-SQL MVP**：以人工审核的本体语义层为事实源，使用 LangGraph 编排“概念理解、确定性资产映射、Join 规划、SQL 生成、校验、执行与解释”闭环。

> 安全声明：仓库中的 MiniBank、表结构、业务概念、SQL 和数据均为公开演示目的自行构造，与任何真实银行或工商银行内部系统无关。禁止向本仓库提交真实数据、内部表结构、内部 SQL、内部规则或 API Key。

## MVP 能力

- YAML 人工审核本体；PostgreSQL 运行时语义仓库结构；pgvector 扩展接口
- “自然语言 → 标准业务概念 → 确定性映射 → 表/字段/Join”的选表链路
- 11 节点 LangGraph，可独立运行，也可作为 Subgraph 接入总控 Agent
- 生命周期策略排除 `DEPRECATED`、`TEMPORARY`、`TEST` 资产
- NetworkX 基于审核 Join Graph 规划路径
- SQLGlot 单语句、只读、允许表校验与 PostgreSQL 格式化
- PostgreSQL `EXPLAIN` + 只读事务 + 超时 + 最大行数保护
- FastAPI 接口和 Streamlit 汇报页面
- DeepSeek/Qwen OpenAI 兼容模型配置；无 Key 时默认确定性 mock 模式
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

要求：Docker Desktop 已启动，并启用 Docker Compose v2。无需本地 Python，也无需 API Key。

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

## 模型配置

默认 `LLM_MODE=mock`，目标演示问题完全依赖审核本体和规则运行。切换真实模型时只修改 `.env`：

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

## 发布 YAML 到运行时仓库

容器启动后，可把审核后的概念写入 PostgreSQL 运行时表：

```powershell
docker compose exec api python scripts/publish_ontology.py
```

YAML 始终是人工审核事实源；数据库是运行时副本。正式环境应在发布前增加审核状态、版本签名、回滚和审计记录。

## 当前边界

- 仅保证目标用例及同义表达的确定性 mock 闭环；通用问题解析和 SQL 生成尚未接入 live LLM Structured Output。
- pgvector 表结构与模型适配已准备，MVP 的概念/历史 SQL 排序仍采用本地规则。
- 离线流程已提供元数据抽取和本体发布骨架，样例值剖析、历史 SQL 批量解析、候选语义审核 UI 尚未实现。
- `rag`、`schema` 模式保留稳定接口用于后续对照实验，当前仍复用 ontology 主链路。
- 尚未实现认证 SQL 管理、语义版本回滚、权限隔离、审计和完整评测平台。
