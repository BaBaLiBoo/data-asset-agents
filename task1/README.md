# task1_v1：资产检索与三层重复资产识别

`task1_v1`是供总控Agent调用的FastAPI服务，提供两种严格分离的能力：

```text
正式判重
asset_id或完整AssetInput
→ 候选召回
→ 语义、SQL逻辑、血缘三层比较
→ DUPLICATE / SUSPECTED_DUPLICATE / NOT_DUPLICATE

资产检索
自然语言query＋可选sql_text
→ 文本混合检索＋可选SQL增强排序
→ Top-K可复用候选
→ 不输出正式重复结论
```

当前版本不包含本体、LLM短语提取、Task2联动、临时资产画像、多轮查询状态和真实银行系统Gateway。

## 1. 环境配置

安装Python依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

配置一个兼容OpenAI `POST /embeddings`协议的在线Embedding服务：

```powershell
.\configure.ps1 `
  -EmbeddingApiKey "你的API Key" `
  -EmbeddingBaseUrl "https://你的服务地址/v1" `
  -EmbeddingModel "你的Embedding模型" `
  -EmbeddingDimension 1024
```

上面的`1024`只是命令示例，不是代码写死的维度，应填写所选模型实际输出维度。如果服务不接受请求体中的`dimensions`参数，增加：

```powershell
-SendDimensions:$false
```

`configure.ps1`在本地生成`.env`。该文件包含部署密钥，已被`.gitignore`排除，不得提交到Git仓库。总控部署脚本也可以直接设置同名进程环境变量后启动Task1。

## 2. 常用命令

```powershell
# 启动FastAPI服务
.\run.ps1

# 运行单元测试和一次真实Embedding连通性测试
.\test.ps1

# 正式判重评测：关键词基线与三层模型
.\evaluate.ps1

# 自然语言检索评测：四种检索模式
.\evaluate-retrieval.ps1
```

服务启动后打开Swagger：

```text
http://127.0.0.1:8000/docs
```

## 3. 总控需要调用的接口

```text
GET  /health
POST /api/v1/task1/search-duplicates
POST /api/v1/task1/search-by-text
```

资产目录管理接口：

```text
POST /api/v1/task1/assets
POST /api/v1/task1/assets/batch
GET  /api/v1/task1/assets
GET  /api/v1/task1/assets/{asset_id}
POST /api/v1/task1/scan-all
```

`POST /api/v1/task1/compare`是内部两两比较/调试接口，总控正常流程不需要调用。

## 4. 场景A：正式判重

### 4.1 已有资产ID

```http
POST /api/v1/task1/search-duplicates
Content-Type: application/json
```

```json
{
  "asset_id": "IND_DEP_BAL_MONTH_ALL",
  "candidate_top_k": 20,
  "result_top_k": 10,
  "business_domain_only": true,
  "include_not_duplicate": false
}
```

Task1从SQLite加载完整资产画像，再执行候选召回和三层判重。`asset_id`不存在时返回HTTP 404。

### 4.2 完整资产画像

调用方也可以把完整`AssetInput`放入请求的`asset`字段。资产画像至少包含：

```text
asset_id、asset_name、description、business_domain
sql_text、sql_dialect、declared_grain
表字段元数据、血缘
```

该查询资产只用于本次比较，不会自动写入SQLite。

正式判重结果重点字段：

```text
results[].candidate.asset_id
results[].candidate.recall_score
results[].comparison.decision
results[].comparison.semantic.score
results[].comparison.logic.score
results[].comparison.lineage.score
results[].comparison.fusion_score
results[].comparison.evidence_coverage
results[].comparison.explanation
results[].comparison.warnings
```

## 5. 场景B：自然语言资产检索

```http
POST /api/v1/task1/search-by-text
Content-Type: application/json
```

### 5.1 只有自然语言

```json
{
  "query": "我想开发个人客户月均存款，看看有没有已有资产可以复用",
  "top_k": 5
}
```

处理流程：

```text
保守规则清洗
→ 原文和清洗文本双路Embedding，取最大相似度
→ 查询词与资产词集合Jaccard
→ 0.80×Embedding＋0.20×关键词
→ 最低阈值过滤
→ Top-K候选摘要
```

### 5.2 自然语言＋SQL

```json
{
  "query": "我想开发个人客户月均存款",
  "business_domain": "deposit",
  "sql_text": "SELECT client_id, AVG(balance) FROM dwd_customer_deposit GROUP BY client_id",
  "sql_dialect": "spark",
  "top_k": 5
}
```

SQLGlot解析成功后，排序初始公式为：

```text
0.50×Embedding
+ 0.10×关键词
+ 0.30×SQL逻辑
+ 0.10×表字段标识符
```

SQL解析失败不会中止请求，而是返回HTTP 200、模式`TEXT_ONLY_FALLBACK`，并在`warnings`中说明本次只使用文本检索。Embedding服务失败仍返回HTTP 502。

### 5.3 检索响应

主要字段：

```text
status                       MATCHED或NO_MATCH
retrieval_mode               TEXT_ONLY / TEXT_SQL_ENHANCED / TEXT_ONLY_FALLBACK
original_query
normalized_query
normalization_method
business_domain_applied
catalog_size
eligible_assets
min_score
returned_candidates
candidates[].asset_id
candidates[].asset_name
candidates[].description
candidates[].business_domain
candidates[].recall_score
candidates[].score_components
warnings
```

检索接口只返回候选摘要，不返回完整SQL、完整元数据、完整血缘、三层融合分或`decision`。没有候选达到阈值时返回HTTP 200：

```json
{
  "status": "NO_MATCH",
  "returned_candidates": 0,
  "candidates": []
}
```

`business_domain`提供且存在时准确过滤；不存在时回退全目录并返回warning。

## 6. 总控路由规则

```text
用户提供asset_id
→ /search-duplicates
→ 正式判重

用户或上游系统提供完整AssetInput
→ /search-duplicates
→ 正式判重

用户只表达新需求
→ /search-by-text
→ 返回可能复用的存量资产

用户表达新需求并携带SQL
→ /search-by-text
→ SQL增强检索，仍不作正式判重
```

总控只负责意图识别、接口路由和自然语言组织；Task1内部负责查询清洗、资产目录检索、SQL增强和正式三层判重。

## 7. 数据与SQLite

项目有两套仿真数据：

- `data/assets.jsonl`和`data/pairs.csv`：64资产/60对快速回归集；
- `data/evaluation_v3/`：360资产/270对独立扩充集。

服务首次启动时，默认将V3的360个完整`AssetInput`导入`data/asset_catalog.db`，作为模拟存量资产目录。SQLite数据库和Embedding缓存由运行时生成，均不提交Git。

这些数据只包含仿真元数据、SQL和血缘，不含真实客户或交易记录。它们可用于功能和工程预评测，不能证明银行生产环境准确率。

## 8. 两套独立评测

### 8.1 正式判重

```powershell
.\evaluate.ps1
```

比较：

```text
KEYWORD_BASELINE
THREE_LAYER
```

报告Accuracy、Precision、Recall和F1；阈值只在DEV选择，TEST只做最终评测。

### 8.2 自然语言检索

```powershell
.\evaluate-retrieval.ps1
```

脚本自动：

1. 从360资产家族生成标准、同义、口语、近义干扰、多答案、无匹配和SQL查询；
2. 按资产家族隔离DEV和TEST；
3. 比较`KEYWORD_ONLY`、`EMBEDDING_ONLY`、`TEXT_HYBRID`和`TEXT_SQL_ENHANCED`；
4. 仅在DEV选择权重和最低阈值；
5. 将校准结果写入`data/retrieval_thresholds.json`；
6. 在TEST上生成一次报告。

稳定输出：

```text
reports/retrieval/latest.json
reports/retrieval/latest.md
```

指标包括Recall@1、Recall@5、MRR、无匹配识别率、错误推荐率、匹配F1和分查询类型结果。该评测与正式判重评测不得混为同一个准确率结论。

## 9. 配置优先级

自然语言检索权重和阈值的优先级：

```text
data/retrieval_thresholds.json中的DEV校准结果
→ .env或进程环境变量
→ 代码默认值
```

正式三层权重和阈值未被本次升级修改。

## 10. 错误处理

| HTTP状态 | 含义 |
|---|---|
| 200 | 正常结果，也包括`NO_MATCH`和SQL解析失败降级 |
| 404 | 正式判重的`asset_id`不存在 |
| 422 | 请求字段为空、`top_k`越界或模型校验失败 |
| 502 | 在线Embedding服务调用失败 |
| 503 | SQLite资产目录不可用 |

## 11. 关键文件

```text
app/query_normalizer.py                  查询保守清洗
app/retrieval.py                         文本和SQL增强召回
app/sql_logic.py                         SQLGlot指纹与逻辑比较
app/service.py                           两类业务流程
app/api.py                               FastAPI接口
data/evaluation_v3/assets.jsonl          360个模拟存量资产
data/retrieval_evaluation/queries.jsonl  自动生成检索评测集
data/retrieval_thresholds.json           DEV校准后的检索配置
scripts/generate_retrieval_dataset.py    检索评测集生成
scripts/evaluate_retrieval.py            独立检索评测
```
