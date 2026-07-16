# Data Asset Agents

Data Asset Agents 是一个面向企业数据资产研发场景的多智能体项目。本分支在稳定的 Text-to-SQL、本体离线构建与安全发布链路上，新增了**认证历史 SQL 资产的结构化管理、混合检索、重排与 SQLGlot AST 模板改写**。当前发布本体仍是在线语义事实源，历史 SQL 只能作为通过安全门槛后的结构模板，不能绕过物理映射、Validator 或 PostgreSQL `EXPLAIN`。

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

## 第二阶段本体构建能力

- PostgreSQL 表、字段、类型、注释、主键、外键和索引抽取
- 行数、空值率、唯一率、最小/最大值、高频值及限量脱敏样例 Profiling
- 表名、Schema 和字段标识符校验；动态查询通过 SQLAlchemy 表达式构建
- SQLGlot 批量解析历史 SQL 的表、字段、Join、过滤、聚合、Group By 和时间字段
- 表共现、字段热度与 Join 使用次数统计，并保存结构化结果
- 基于 LangChain Structured Output 的候选语义生成；mock 模式无 Key 稳定运行
- `CANDIDATE → VERIFIED/REJECTED` 人工审核状态机和不可变审核记录
- PostgreSQL 事务化版本发布，正式概念、指标、维度、属性、映射、Join、表生命周期和规则分表保存
- 在线查询优先读取当前已发布 Physical Mapping；YAML 作为初始化种子和故障回退
- Streamlit 提供元数据、历史 SQL 证据、候选编辑审核和版本发布工作台

## 发布安全与在线语义能力

- `PhysicalMapping.column_bindings` 明确描述语义角色到物理字段的绑定，不再按 `columns` 位置猜测；旧 YAML 可自动迁移
- `OntologyContractValidator` 在发布前检查实际表字段、生命周期、Metric/Dimension 完整性、Join 连通性、候选审核状态和快照一致性
- 发布 Dry Run 会构造候选 Bundle、编译固定基准问题、执行 SQLGlot 校验和 PostgreSQL `EXPLAIN`
- 正式版本通过 `ontology_version_candidate` 保留审核候选来源；Dry Run 失败时不会进入发布事务
- 支持激活或回滚任一已发布版本；切换后同步刷新 OntologyService、QueryExecutor、SQLValidator、概念索引和 LangGraph，并执行健康问题
- mock/live 均输出强类型 Semantic Query；live 使用 LangChain Structured Output，模型只能选择业务语义 ID 或名称
- 低置信度或有歧义的领域问题返回 `clarification_required`，不进入物理资产解析和 SQL 生成
- 当前 `PUBLISHED` 版本的概念、指标、维度、语义属性支持名称、同义词、PostgreSQL 关键词及 pgvector 混合检索
- mock 模式使用固定 1024 维 CPU 哈希向量；live 模式使用配置的 Embedding 客户端

## 认证 SQL 资产能力

- `SQLAsset` 保存问题、业务摘要、认证等级、SQLGlot 结构、指标/维度、表字段、Join、CTE、子查询、窗口函数、AST 节点分布与规范化指纹
- 离线构建结合当前已发布本体检查 Physical Mapping、字段存在性、表生命周期和审核 Join，并以 PostgreSQL `EXPLAIN` 作为进入召回池的硬门槛
- PostgreSQL 同时保存结构化 JSONB、全文索引和 pgvector；mock 使用稳定 CPU 向量，live 复用现有 Embedding 客户端
- 检索仅考虑认证、解析成功、生命周期有效、字段有效、Join 已审核且 EXPLAIN 成功的资产
- 通过自然语言、指标、维度、表字段、Join、AST、认证和生命周期八项分数组合重排，并返回每项得分与证据
- 简单查询继续使用确定性编译器；只有带 CTE、子查询或窗口函数的复杂认证模板才尝试 SQLGlot AST 改写
- 改写通过 SQLValidator 和 PostgreSQL `EXPLAIN` 后才可进入后续执行；任一步失败都会显式记录原因并回退确定性 SQL

重排总分为：自然语言向量/全文 `25%` + 指标 `15%` + 维度 `10%` +
表字段覆盖 `15%` + Join `10%` + AST 标签 `5%` + 认证等级 `10%` +
生命周期 `10%`。该公式不参与硬门槛判定；任一硬门槛失败的资产不会因得分高而返回。

离线与在线边界：

```text
数据库 + 历史 SQL → 元数据快照/结构化证据 → LLM 候选(CANDIDATE)
    → 人工 VERIFIED/REJECTED → 显式发布 OntologyVersion
    → 在线查询只读取当前 PUBLISHED 版本
```

## 核心链路

```text
问题 → 已发布概念混合召回 → 候选约束的 Semantic Query → 正式物理资产
    → Join Graph → 认证 SQL 资产混合检索 → 确定性编译/AST 改写
    → SQLGlot 校验 → EXPLAIN/只读执行 → 解释
```

向量检索只用于业务概念候选和认证历史 SQL 候选，不允许绕过本体映射直接搜索并选择物理表。

## 目录

```text
apps/api/                    FastAPI
apps/web/                    Streamlit
src/data_asset_agents/
  ontology/                  模型、YAML 仓库、发布器与服务
    builder/                 Profiling 证据编排、候选生成与发布服务
    repository/              YAML 种子和 PostgreSQL 审核/版本仓库
  text2sql/                  State、Graph、节点和 Join 工具
  sql_assets/                SQLAsset 解析、存储、混合重排和 AST 改写
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
git switch feature/sql-asset-retrieval
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

如果需要保留已有演示数据，可在更新 Compose 容器后依次执行增量 DDL：

```powershell
docker compose up -d postgres
docker compose exec postgres psql -U minibank -d minibank `
  -f /docker-entrypoint-initdb.d/002_ontology_builder.sql
docker compose exec postgres psql -U minibank -d minibank `
  -f /docker-entrypoint-initdb.d/003_release_safety_and_search.sql
docker compose exec postgres psql -U minibank -d minibank `
  -f /docker-entrypoint-initdb.d/004_sql_assets.sql
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

`LLM_MODE=live` 会启用在线 Semantic Query Structured Output 和离线候选生成；SQL 仍由已发布本体确定性编译，不允许模型输出物理表、字段或 SQL。系统不具备任意问题生成能力；域外问题返回 `unsupported`，有业务相关词但含义不足的问题返回 `clarification_required`。

第二阶段在 `LLM_MODE=live` 时通过现有 `ModelFactory` 创建 OpenAI 兼容 Chat Model，并使用 `with_structured_output` 生成候选语义；输出仍是 `CANDIDATE`，必须人工审核和显式发布。mock 模式使用确定性规则生成同结构候选，不需要 API Key。

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
| POST | `/api/v1/sql-assets/build` | 批量解析、校验、EXPLAIN 并索引受控历史 SQL |
| GET | `/api/v1/sql-assets` | 查看全部 SQL 资产及安全状态 |
| POST | `/api/v1/sql-assets/search` | 按语义与结构混合检索认证 SQL |
| GET | `/api/v1/sql-assets/{id}` | 查看 SQL 资产结构化详情 |
| POST | `/api/v1/ontology/build` | 抽取物理知识、解析历史 SQL 并生成候选 |
| GET | `/api/v1/ontology/candidates` | 按类型和状态查看候选 |
| GET | `/api/v1/ontology/candidates/{id}` | 查看候选及证据 |
| POST | `/api/v1/ontology/candidates/{id}/verify` | 编辑并审核通过候选 |
| POST | `/api/v1/ontology/candidates/{id}/reject` | 拒绝候选 |
| POST | `/api/v1/ontology/publish` | 将已审核内容发布为正式版本 |
| POST | `/api/v1/ontology/publish/validate` | 发布 Dry Run：契约、SQLGlot、EXPLAIN |
| GET | `/api/v1/ontology/versions` | 查看正式本体版本 |
| POST | `/api/v1/ontology/versions/{version}/activate` | 激活或回滚已发布版本 |
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

## 本体构建、审核与发布

打开 Streamlit 后，在左侧“工作台”切换到“本体构建与审核”。标准操作顺序：

1. 开始离线构建，查看元数据快照、字段统计、脱敏样例和历史 SQL 证据。
2. 分别审核 Concept、Mapping 和 Join 候选；可编辑业务名称、语义属性和同义词。
3. 只有所选元数据快照中的 `VERIFIED` 候选具备发布资格；不同快照的审核结果不会混入同一版本，`CANDIDATE` 和 `REJECTED` 永远不会被在线查询读取。
4. 先执行 Dry Run；只有契约、SQLGlot 和 PostgreSQL `EXPLAIN` 全部通过才能正式发布。
5. 发布或回滚后，API 切换当前版本，重建概念向量索引、Validator 和 Text-to-SQL Graph，并运行基础健康问题。

以下旧脚本仅用于把 YAML 种子概念同步到兼容表，不等同于第二阶段版本发布：

容器启动后，可把审核后的概念写入 PostgreSQL 运行时表：

```powershell
docker compose exec api python scripts/publish_ontology.py
```

YAML 是初始化种子和故障回退；在线事实源是 PostgreSQL 中当前 `PUBLISHED` 的 `OntologyVersion`。

## 当前边界

- 仅保证已发布指标、维度及其同义表达的闭环；域外问题返回 unsupported，低置信度问题要求澄清，不会生成占位 SQL。
- `ontology` 是第一阶段唯一真实实现的查询模式；`rag`、`schema` 仅保留接口和前端选项，当前复用同一 ontology 链路，不代表独立能力。
- 概念向量只匹配当前发布的标准业务语义，绝不直接选择物理表；SQL 资产向量只召回已认证模板，物理表仍由本体映射确定。
- AST 改写支持受控的表/字段角色映射、外层时间与筛选条件、Group By、Order By、Limit，并保留模板中的 CTE、子查询和窗口表达式；跨事实表、相关子查询语义迁移和任意结构合成不在本轮范围内。
- SQL 资产认证状态由受控源文件提供；本轮没有实现多人签名认证工作流。索引在 API 启动和显式 build 时重建，尚未实现增量 CDC。
- 本轮实现的是受控离线构建，不是无人值守的“自动本体”：业务骨架仍来自人工定义，LLM 只生成候选，发布必须人工审核。
- 自动修复仅支持补齐校验器明确指出的必要指标过滤条件；字段错误、未审核 Join、禁用表和数据库执行错误不会被猜测性修改。
- 已实现版本激活和回滚，但尚未实现细粒度权限、多人审批、版本签名、完整审计检索和评测平台。
- 不实现 schema/rag/ontology 完整对照、多事实表 SQL、历史 SQL 自由 AST 合成、复杂 LLM SQL 修复、OWL/RDF/SPARQL/Neo4j 或其他 Agent。
