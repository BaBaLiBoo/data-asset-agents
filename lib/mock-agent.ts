export type PreferredScene = "AUTO" | "ASSET_DUPLICATE" | "SQL_GENERATION" | "LINEAGE_PARSING";
export type AgentScene = Exclude<PreferredScene, "AUTO"> | "CLARIFICATION";
export type AgentStatus =
  | "PROCESSING"
  | "SUCCESS"
  | "NEED_MORE_INFORMATION"
  | "REVIEW_REQUIRED"
  | "FAILED";

export interface ChatRequest {
  conversationId: string;
  message: string;
  preferredScene?: PreferredScene;
  context?: {
    sqlDialect?: string;
    schemaScope?: string[];
    scriptId?: string;
    targetField?: string;
  };
  correctionContext?: Array<{
    previousResultId: string;
    decision: "EDIT_AND_ACCEPT" | "REJECT";
    reasonCode: string;
    comment: string;
    editedSql?: string;
  }>;
}

export interface ExecutionStep {
  id: string;
  label: string;
  detail: string;
  tool?: string;
  status: "RUNNING" | "SUCCESS" | "WAITING" | "SKIPPED" | "FAILED";
}

export type ExecutionPlanStatus =
  | "WAITING_INPUT"
  | "READY"
  | "RUNNING"
  | "REVIEW"
  | "FAILED"
  | "COMPLETED";

export type ProfessionalToolName = "check_asset_duplicate" | "generate_sql" | "parse_regulatory_lineage";

export type ReflectionStatus = "PASSED" | "WARNING" | "UNAVAILABLE";
export type ReflectionItemStatus = "PASSED" | "WARNING" | "UNKNOWN";
export type ReflectionCategory =
  | "METRIC"
  | "DIMENSION"
  | "GRAIN"
  | "TIME_RANGE"
  | "FILTER"
  | "TABLE_FIELD"
  | "ASSET_EVIDENCE"
  | "RISK";
export type ReflectionRiskLevel = "LOW" | "MEDIUM" | "HIGH";

export interface ResultReflection {
  status: ReflectionStatus;
  summary: string;
  items: Array<{
    category: ReflectionCategory;
    status: ReflectionItemStatus;
    requirement: string;
    observation: string;
    suggestion: string;
  }>;
  riskLevel: ReflectionRiskLevel;
}

export interface ExecutionPlan {
  planId: string;
  scene: AgentScene;
  selectedTool: ProfessionalToolName | null;
  status: ExecutionPlanStatus;
  steps: ExecutionStep[];
  constraints: {
    maxProfessionalToolCalls: 1;
    professionalToolCalls: 0 | 1;
    allowCrossScene: false;
    requireHumanReview: true;
  };
}

export interface DuplicateCandidate {
  assetId: string;
  assetName: string;
  businessDomain: string;
  owner: string;
  totalScore: number;
  scores: {
    semantic: number;
    logic: number;
    lineage: number;
  };
  recommendation: "REUSE" | "REVIEW" | "NO_DUPLICATE";
  evidence: string[];
}

export interface DuplicateResult {
  kind: "duplicate";
  draftAsset?: {
    assetId: string;
    assetName: string;
    businessDomain: string;
    grain: string;
  };
  canReuse: boolean;
  recommendation: string;
  threshold: number;
  candidates: DuplicateCandidate[];
}

export interface SqlResult {
  kind: "sql";
  template: {
    templateId: string;
    templateName: string;
    similarity: number;
    owner: string;
  } | null;
  sqlDialect: string;
  sql: string;
  recommendedTables: string[];
  recommendedFields: string[];
  explanation: string;
  assumptions: string[];
  validation: {
    passed: boolean;
    riskLevel: "LOW" | "MEDIUM" | "HIGH";
    items: Array<{
      label: string;
      status: "PASSED" | "WARNING";
      detail: string;
    }>;
  };
}

export interface ClarificationResult {
  kind: "clarification";
  question: string;
  suggestions: Array<{
    label: string;
    message: string;
    scene: Exclude<PreferredScene, "AUTO">;
  }>;
}

export type LineageNodeType =
  | "TABLE"
  | "COLUMN"
  | "WHERE"
  | "JOIN"
  | "CASE"
  | "AGG"
  | "UNION"
  | "SUBQUERY"
  | "TEMP_TABLE"
  | "TARGET";

export type LineageEdgeRelation =
  | "SOURCE"
  | "FILTER"
  | "JOIN"
  | "TRANSFORM"
  | "AGGREGATE"
  | "OUTPUT"
  | "SUBQUERY_REF";

export interface LineageNode {
  nodeId: string;
  type: LineageNodeType;
  label: string;
  table?: string | null;
  field?: string | null;
  parentNodeId?: string | null;
  nestingLevel?: number;
  expression?: string | null;
  sqlLineStart?: number | null;
  sqlLineEnd?: number | null;
  description?: string | null;
  confidence?: number | null;
  warning?: string | null;
}

export interface LineageEdge {
  edgeId: string;
  source: string;
  target: string;
  relation: LineageEdgeRelation;
  pathIds?: string[];
  expression?: string | null;
  confidence?: number | null;
}

export interface LineagePath {
  pathId: string;
  sourceNodeId: string;
  targetNodeId: string;
  nodeIds: string[];
  keyRules: string[];
  confidence: number;
  isMain?: boolean;
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: LineageEdge[];
  paths: LineagePath[];
}

export interface LineageMetrics {
  sourceTables: number;
  sourceFields: number;
  sourcePaths: number;
  maxNestingDepth: number;
  operatorCount: number;
  joinCount: number;
  caseCount: number;
}

export interface LineageGraphSummary {
  totalNodes: number;
  totalEdges: number;
  totalPaths: number;
}

export interface LineageResult {
  kind: "lineage";
  jobId: string;
  scriptId: string;
  targetField: string;
  sqlDialect?: string | null;
  metrics?: LineageMetrics | null;
  paths: LineagePath[];
  graph?: LineageGraph | null;
  graphSummary?: LineageGraphSummary | null;
  summary: string;
  confidence?: number | null;
  warnings: string[];
}

export type AgentResult = DuplicateResult | SqlResult | LineageResult | ClarificationResult;

export interface AgentResponse {
  conversationId: string;
  taskId: string;
  resultId: string;
  scene: AgentScene;
  status: AgentStatus;
  version: number;
  summary: string;
  executionPlan?: ExecutionPlan;
  reflection?: ResultReflection;
  executionTrace: ExecutionStep[];
  result: AgentResult | null;
  provenance: {
    agentVersion: string;
    modelVersion: string;
    promptVersion: string;
    metadataVersion: string;
  };
  error?: {
    code: string;
    message: string;
    retryable: boolean;
  };
  createdAt: string;
}

function makeId(prefix: string) {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  return `${prefix}_${stamp}_${Math.floor(Math.random() * 90 + 10)}`;
}

export function detectScene(message: string, preferredScene: PreferredScene = "AUTO"): AgentScene {
  if (preferredScene !== "AUTO") return preferredScene;
  const text = message.toLowerCase();
  const duplicateWords = ["重复", "查重", "相似资产", "资产复用", "冗余资产", "是否已经存在"];
  const lineageWords = ["血缘", "来源", "上游", "下游", "算子", "路径", "溯源"];
  const sqlWords = ["sql", "查询", "统计", "汇总", "取数", "报表", "字段", "生成"];
  if (duplicateWords.some((word) => text.includes(word))) return "ASSET_DUPLICATE";
  if (lineageWords.some((word) => text.includes(word))) return "LINEAGE_PARSING";
  if (sqlWords.some((word) => text.includes(word))) return "SQL_GENERATION";
  return "CLARIFICATION";
}

export function getSteps(scene: AgentScene): ExecutionStep[] {
  if (scene === "ASSET_DUPLICATE") {
    return [
      { id: "understand", label: "理解资产信息", detail: "提取资产名称、口径与粒度", status: "SUCCESS" },
      { id: "retrieve", label: "混合检索候选资产", detail: "使用语义、关键词与可选SQL增强召回", tool: "check_asset_duplicate", status: "SUCCESS" },
      { id: "review", label: "整理候选证据", detail: "输出召回候选，不产生正式重复结论", status: "SUCCESS" },
      { id: "recommend", label: "等待人工复核", detail: "完整三层判重需要补充资产画像、SQL和血缘", status: "SUCCESS" },
    ];
  }
  if (scene === "SQL_GENERATION") {
    return [
      { id: "understand", label: "理解业务需求", detail: "提取指标、维度、时间范围与过滤条件", status: "SUCCESS" },
      { id: "retrieve", label: "检索存量模板", detail: "在资产脚本库中匹配最优加工模板", tool: "search_sql_template", status: "SUCCESS" },
      { id: "generate", label: "生成目标SQL", detail: "依据已发布本体生成PostgreSQL查询", tool: "generate_sql", status: "SUCCESS" },
      { id: "validate", label: "执行安全校验", detail: "检查语法、元数据与危险操作", tool: "validate_sql", status: "SUCCESS" },
    ];
  }
  if (scene === "LINEAGE_PARSING") {
    return [
      { id: "understand", label: "理解解析需求", detail: "识别目标字段与脚本上下文", status: "SUCCESS" },
      { id: "preprocess", label: "脚本预处理", detail: "切分脚本并提取目标字段上下文", status: "SUCCESS" },
      { id: "extract", label: "算子提取", detail: "提取WHERE/JOIN/CASE/AGG算子", tool: "parse_regulatory_lineage", status: "SUCCESS" },
      { id: "infer", label: "路径推理", detail: "穿透嵌套与关联推理来源路径", status: "SUCCESS" },
      { id: "build", label: "生成血缘DAG", detail: "构建算子级血缘图与口径", status: "SUCCESS" },
    ];
  }
  return [
    { id: "understand", label: "理解用户需求", detail: "当前描述不足以确定专业场景", status: "SUCCESS" },
  ];
}

const duplicateResult: DuplicateResult = {
  kind: "duplicate",
  draftAsset: {
    assetId: "DRAFT_CUST_MTH_TRADE_SUM",
    assetName: "客户月度交易汇总表",
    businessDomain: "零售客户域",
    grain: "客户-月",
  },
  canReuse: true,
  recommendation: "发现可复用资产候选，需补充完整资产画像后人工复核。",
  threshold: 0.85,
  candidates: [
    {
      assetId: "ADS_CUST_MONTH_TRADE_STAT",
      assetName: "客户月交易统计表",
      businessDomain: "零售客户域",
      owner: "零售数据团队",
      totalScore: 0.92,
      scores: { semantic: 0.94, logic: 0.89, lineage: 0 },
      recommendation: "REVIEW",
      evidence: ["语义及SQL增强召回分较高；尚未执行正式三层重复判定"],
    },
    {
      assetId: "DM_CUST_TRADE_MONTHLY_RPT",
      assetName: "个人客户交易月报",
      businessDomain: "经营分析域",
      owner: "数据分析中心",
      totalScore: 0.82,
      scores: { semantic: 0.86, logic: 0.79, lineage: 0 },
      recommendation: "REVIEW",
      evidence: ["业务描述相近但口径可能不同，需人工确认"],
    },
    {
      assetId: "ADS_BRANCH_MTH_TRADE_SUM",
      assetName: "机构月度交易汇总表",
      businessDomain: "机构经营域",
      owner: "机构数据团队",
      totalScore: 0.72,
      scores: { semantic: 0.71, logic: 0.76, lineage: 0 },
      recommendation: "NO_DUPLICATE",
      evidence: ["召回分低于复核阈值，仅作为低相关候选展示"],
    },
  ],
};

function buildSqlResult(correction?: ChatRequest["correctionContext"]): SqlResult {
  const isCorrection = Boolean(correction?.length);
  return {
    kind: "sql",
    template: {
      templateId: "SQL_TPL_2026_0186",
      templateName: "分行新增客户统计模板",
      similarity: 0.93,
      owner: "零售数据团队",
    },
    sqlDialect: "PostgreSQL",
    sql: `${isCorrection ? "-- 已根据用户反馈重新生成\n" : "-- 基于已发布本体生成（PostgreSQL）\n"}SELECT
    c.branch_id,
    COUNT(DISTINCT c.customer_id) AS new_customer_count
FROM retail_dm.customer_info AS c
WHERE c.first_open_date >= CURRENT_DATE - INTERVAL '90 days'
  AND c.customer_status = 'ACTIVE'
GROUP BY c.branch_id
ORDER BY new_customer_count DESC;`,
    recommendedTables: ["retail_dm.customer_info"],
    recommendedFields: ["branch_id", "customer_id", "first_open_date", "customer_status"],
    explanation: isCorrection
      ? "已结合反馈重新检查日期字段、客户状态过滤条件和分行统计粒度。"
      : "检索到分行新增客户统计模板，并完成近90天时间窗口、客户状态和分行维度映射。",
    assumptions: ["新增客户按首次开户日期统计", "仅统计状态为ACTIVE的有效客户", "时间范围采用PostgreSQL当前日期"],
    validation: {
      passed: true,
      riskLevel: "LOW",
      items: [
        { label: "PostgreSQL语法", status: "PASSED", detail: "Mock语法检查通过" },
        { label: "元数据检查", status: "PASSED", detail: "推荐表与字段均存在" },
        { label: "危险操作", status: "PASSED", detail: "未发现DDL/DML高风险操作" },
        { label: "业务口径", status: "WARNING", detail: "请人工确认ACTIVE客户口径" },
      ],
    },
  };
}

const lineageResult: LineageResult = {
  kind: "lineage",
  jobId: "JOB_LINEAGE_MOCK",
  scriptId: "SCRIPT_EAST_DBHTH",
  targetField: "EAST.BD_ODS_BNWYWDBHTB::DBHTH",
  sqlDialect: "Oracle",
  metrics: {
    sourceTables: 38,
    sourceFields: 62,
    sourcePaths: 13,
    maxNestingDepth: 5,
    operatorCount: 126,
    joinCount: 21,
    caseCount: 17,
  },
  paths: [
    {
      pathId: "path_main",
      sourceNodeId: "src_htbh",
      targetNodeId: "tgt_dbhtbh",
      nodeIds: ["src_htbh", "filter_valid", "join_ywxx", "case_dbhtbh", "agg_group", "tgt_dbhtbh"],
      keyRules: ["有效记录过滤", "合同业务关联", "合同号口径映射", "去重归并"],
      confidence: 0.94,
      isMain: true,
    },
    {
      pathId: "path_zhth",
      sourceNodeId: "src_zhth",
      targetNodeId: "tgt_dbhtbh",
      nodeIds: ["src_zhth", "join_ywxx", "case_dbhtbh", "agg_group", "tgt_dbhtbh"],
      keyRules: ["银团贷款合同号映射", "去重归并"],
      confidence: 0.88,
      isMain: false,
    },
  ],
  graph: {
    nodes: [
      { nodeId: "src_htbh", type: "COLUMN", label: "HTBH", table: "ODS_BNWY_HTXX", field: "HTBH", nestingLevel: 0, expression: "T1.HTBH", sqlLineStart: 18, sqlLineEnd: 18, description: "贷款合同主表合同编号", confidence: 1, warning: null },
      { nodeId: "filter_valid", type: "WHERE", label: "有效合同过滤", nestingLevel: 1, expression: "T1.DEL_FLAG = '0' AND T1.HTZT IN ('1','2')", sqlLineStart: 24, sqlLineEnd: 27, description: "仅保留有效合同记录", confidence: 0.98, warning: null },
      { nodeId: "join_ywxx", type: "JOIN", label: "合同与业务明细关联", nestingLevel: 1, expression: "LEFT JOIN ODS_BNWY_YWXX T7 ON T1.JJBH = T7.JJBH", sqlLineStart: 31, sqlLineEnd: 34, description: "通过借据编号关联业务明细表", confidence: 0.96, warning: null },
      { nodeId: "case_dbhtbh", type: "CASE", label: "合同编号口径映射", nestingLevel: 2, expression: "CASE WHEN YWLX='YTDK' THEN ZHTH WHEN HTBH IS NOT NULL THEN HTBH ELSE JJBH END", sqlLineStart: 52, sqlLineEnd: 58, description: "银团贷款优先取主合同号", confidence: 0.93, warning: "存在硬编码业务类型 YTDK" },
      { nodeId: "agg_group", type: "AGG", label: "合同编号去重归并", nestingLevel: 3, expression: "GROUP BY JGDM, DBHTH", sqlLineStart: 73, sqlLineEnd: 74, description: "按机构和合同编号归并", confidence: 0.91, warning: null },
      { nodeId: "tgt_dbhtbh", type: "TARGET", label: "DBHTH", table: "EAST.BD_ODS_BNWYWDBHTB", field: "DBHTH", nestingLevel: 3, expression: "DBHTH", sqlLineStart: 81, sqlLineEnd: 81, description: "EAST业务委托表担保合同号", confidence: 0.97, warning: null },
    ],
    edges: [
      { edgeId: "e1", source: "src_htbh", target: "filter_valid", relation: "FILTER", pathIds: ["path_main"], confidence: 0.98 },
      { edgeId: "e2", source: "filter_valid", target: "join_ywxx", relation: "JOIN", pathIds: ["path_main"], confidence: 0.96 },
      { edgeId: "e3", source: "join_ywxx", target: "case_dbhtbh", relation: "TRANSFORM", pathIds: ["path_main"], confidence: 0.93 },
      { edgeId: "e4", source: "case_dbhtbh", target: "agg_group", relation: "AGGREGATE", pathIds: ["path_main"], confidence: 0.91 },
      { edgeId: "e5", source: "agg_group", target: "tgt_dbhtbh", relation: "OUTPUT", pathIds: ["path_main"], confidence: 0.97 },
    ],
    paths: [
      {
        pathId: "path_main",
        sourceNodeId: "src_htbh",
        targetNodeId: "tgt_dbhtbh",
        nodeIds: ["src_htbh", "filter_valid", "join_ywxx", "case_dbhtbh", "agg_group", "tgt_dbhtbh"],
        keyRules: ["有效记录过滤", "合同业务关联", "合同号口径映射", "去重归并"],
        confidence: 0.94,
        isMain: true,
      },
    ],
  },
  graphSummary: { totalNodes: 126, totalEdges: 131, totalPaths: 13 },
  summary: "已完成 DBHTH 字段算子级血缘解析，识别 38 张来源表、13 条来源路径、5 层嵌套。",
  confidence: 0.94,
  warnings: ["存在硬编码业务类型 YTDK，建议确认与一表通口径是否一致"],
};

export function createMockResponse(input: ChatRequest): AgentResponse {
  const scene = detectScene(input.message, input.preferredScene);
  const version = input.correctionContext?.length ? 2 : 1;
  const taskId = makeId(scene === "ASSET_DUPLICATE" ? "TASK_ASSET" : scene === "SQL_GENERATION" ? "TASK_SQL" : scene === "LINEAGE_PARSING" ? "TASK_LINEAGE" : "TASK_CHAT");
  const resultId = makeId(scene === "ASSET_DUPLICATE" ? "ASSET_RESULT" : scene === "SQL_GENERATION" ? "SQL_RESULT" : scene === "LINEAGE_PARSING" ? "LINEAGE_RESULT" : "CHAT_RESULT");
  let result: AgentResult;
  let summary: string;
  let status: AgentStatus = "REVIEW_REQUIRED";

  if (scene === "ASSET_DUPLICATE") {
    result = duplicateResult;
    summary = version > 1 ? "已根据反馈重新检索可复用资产候选" : "已完成资产候选混合检索，等待人工复核";
  } else if (scene === "SQL_GENERATION") {
    result = buildSqlResult(input.correctionContext);
    summary = version > 1 ? "已根据修改意见生成新的PostgreSQL版本" : "已基于本体生成经过只读校验的PostgreSQL查询";
  } else if (scene === "LINEAGE_PARSING") {
    result = lineageResult;
    summary = "已完成算子级血缘解析，等待人工确认";
  } else {
    status = "NEED_MORE_INFORMATION";
    summary = "还需要确认您希望使用哪一种研发能力";
    result = {
      kind: "clarification",
      question: "您希望检索可复用资产、执行本体智能查询，还是解析脚本字段的血缘？",
      suggestions: [
        { label: "检索可复用资产", message: "请检索客户月度交易汇总表可能复用的已有资产候选", scene: "ASSET_DUPLICATE" },
        { label: "智能SQL查询", message: "查询近90天各分行新增客户数", scene: "SQL_GENERATION" },
        { label: "解析字段血缘", message: "请解析 EAST.BD_ODS_BNWYWDBHTB::DBHTH 字段的血缘", scene: "LINEAGE_PARSING" },
      ],
    };
  }

  return {
    conversationId: input.conversationId,
    taskId,
    resultId,
    scene,
    status,
    version,
    summary,
    executionTrace: getSteps(scene),
    result,
    provenance: {
      agentVersion: scene === "ASSET_DUPLICATE" ? "asset-agent-mock-1.0" : scene === "SQL_GENERATION" ? "sql-agent-mock-1.0" : scene === "LINEAGE_PARSING" ? "lineage-agent-mock-1.0" : "supervisor-mock-1.0",
      modelVersion: "MOCK_MODEL_V1",
      promptVersion: version > 1 ? "PROMPT_V1_CORRECTION" : "PROMPT_V1",
      metadataVersion: "META_2026_07",
    },
    createdAt: new Date().toISOString(),
  };
}
