# Data Asset Agents

Data Asset Agents is a MiniBank demo for data-source-driven ontology construction and ontology-grounded Text-to-SQL. The Streamlit product surface now has exactly two user pages:

- **本体构建**: choose the MiniBank PostgreSQL data source, confirm or initialize fictional demo data, scan metadata, build O-C/O-D ontology candidates, review seven resource types, promote to Draft, validate, submit, approve, publish, and explicitly activate a published ontology.
- **智能体 Demo**: choose the database, choose a READY published ontology, confirm the AI mode, ask natural-language business questions, and inspect the business answer, result table, ontology reasoning, physical mapping, Join plan, SQLAsset hit, generated SQL, and validation status.

SQLAsset, Evaluation, Drift, Audit, Object Explorer, and lower-level ontology APIs remain available through FastAPI, CLI, scripts, tests, and reports. They are no longer exposed as separate Streamlit product pages.

All MiniBank data, schema, SQL, ontology resources, and examples in this repository are fictional demo assets. Do not commit real bank data, private schemas, private SQL, credentials, or API keys.

## Local Run

```powershell
Copy-Item .env.example .env
# Fill DeepSeek and DashScope keys in .env only when running Live AI locally.

$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
docker compose up --build -d
```

Open:

- Streamlit: <http://localhost:8501>
- FastAPI: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>

Default CI and demo automation use mock mode and do not call external models. For local Live AI, configure:

```env
LLM_MODE=live
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=

EMBEDDING_PROVIDER=aliyun
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=
EMBEDDING_DIMENSIONS=1024
DEMO_REQUIRE_LIVE_AI=true
```

Keys are read only from environment variables, never returned to the frontend, and never stored in the repository.

## Demo Flow

1. Open **本体构建**.
2. Select the MiniBank PostgreSQL data source.
3. Confirm the demo data status or run the controlled MiniBank initializer.
4. Scan the database and create a RAW_METADATA snapshot.
5. Run O-C rules construction, or O-D Live AI construction when DeepSeek and DashScope are configured.
6. Review ObjectType, Property, Metric, Dimension, Business Link, Physical Join, and Binding candidates with structured forms.
7. Promote reviewed candidates to a Draft.
8. Edit the Draft, validate it, submit it, approve it, publish it, and explicitly activate the version.
9. Open **智能体 Demo**.
10. Select MiniBank, select the published ontology, confirm AI mode, and ask demo questions.

Demo questions:

- 查询交易金额
- 按客户类型统计交易笔数
- 查询近30天各分行信用卡交易金额
- 查询各分行交易金额和排名
- 查询各渠道信用卡交易金额
- 查询活跃客户数

## Acceptance

```powershell
$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
.\scripts\demo_flow_acceptance.ps1 -TimeoutSeconds 900 `
  -ComposeProject "data-asset-agents-demo-flow"
```

Fresh acceptance:

```powershell
$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
.\scripts\acceptance.ps1 -TimeoutSeconds 600 `
  -ComposeProject "data-asset-agents-local-closure"
```

Existing-volume upgrade acceptance:

```powershell
$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
.\scripts\upgrade_acceptance.ps1 -TimeoutSeconds 300 `
  -ComposeProject "data-asset-agents-upgrade"
```

Optional local Live AI smoke, run only after filling keys:

```powershell
.\scripts\live_ai_smoke.ps1 -ApiBaseUrl http://127.0.0.1:8000
```

## Documentation

- [Two-surface demo](docs/two-surface-demo.md)
- [Ontology builder guide](docs/ontology-builder-guide.md)
- [Agent demo guide](docs/agent-demo-guide.md)
- [Two-surface UI audit](docs/two-surface-ui-audit.md)
- [Ontology runtime](docs/ontology-runtime.md)
- [Evaluation protocol](docs/evaluation.md)
