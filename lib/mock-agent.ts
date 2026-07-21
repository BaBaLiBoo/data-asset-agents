export type PreferredScene = "AUTO" | "ASSET_DUPLICATE" | "SQL_GENERATION";
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
  status: "RUNNING" | "SUCCESS";
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
  draftAsset: {
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
  };
  sqlDialect: "MySQL";
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

export type AgentResult = DuplicateResult | SqlResult | ClarificationResult;

export interface AgentResponse {
  conversationId: string;
  taskId: string;
  resultId: string;
  scene: AgentScene;
  status: AgentStatus;
  version: number;
  summary: string;
  executionTrace: ExecutionStep[];
  result: AgentResult;
  provenance: {
    agentVersion: string;
    modelVersion: string;
    promptVersion: string;
    metadataVersion: string;
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
  const sqlWords = ["sql", "查询", "统计", "汇总", "取数", "报表", "字段", "生成"];
  if (duplicateWords.some((word) => text.includes(word))) return "ASSET_DUPLICATE";
  if (sqlWords.some((word) => text.includes(word))) return "SQL_GENERATION";
  return "CLARIFICATION";
}

export function getSteps(scene: AgentScene): ExecutionStep[] {
  if (scene === "ASSET_DUPLICATE") {
    return [
      { id: "understand", label: "理解资产信息", detail: "提取资产名称、口径与粒度", status: "SUCCESS" },
      { id: "retrieve", label: "检索候选资产", detail: "召回业务域内的相似存量资产", tool: "asset_catalog_search", status: "SUCCESS" },
      { id: "compare", label: "三维相似度分析", detail: "比较语义、SQL逻辑与数据血缘", tool: "check_asset_duplicate", status: "SUCCESS" },
      { id: "recommend", label: "形成复用建议", detail: "输出候选资产、证据与建议动作", status: "SUCCESS" },
    ];
  }
  if (scene === "SQL_GENERATION") {
    return [
      { id: "understand", label: "理解业务需求", detail: "提取指标、维度、时间范围与过滤条件", status: "SUCCESS" },
      { id: "retrieve", label: "检索存量模板", detail: "在资产脚本库中匹配最优加工模板", tool: "search_sql_template", status: "SUCCESS" },
      { id: "generate", label: "生成目标SQL", detail: "完成MySQL方言映射和逻辑改写", tool: "generate_sql", status: "SUCCESS" },
      { id: "validate", label: "执行安全校验", detail: "检查语法、元数据与危险操作", tool: "validate_sql", status: "SUCCESS" },
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
  recommendation: "发现1项高相似存量资产，建议优先复用“客户月交易统计表”，避免新增同类汇总资产。",
  threshold: 0.85,
  candidates: [
    {
      assetId: "ADS_CUST_MONTH_TRADE_STAT",
      assetName: "客户月交易统计表",
      businessDomain: "零售客户域",
      owner: "零售数据团队",
      totalScore: 0.92,
      scores: { semantic: 0.94, logic: 0.89, lineage: 0.91 },
      recommendation: "REUSE",
      evidence: ["统计主体和自然月粒度一致", "核心分组字段及聚合逻辑一致", "共同来源于客户交易明细表"],
    },
    {
      assetId: "DM_CUST_TRADE_MONTHLY_RPT",
      assetName: "个人客户交易月报",
      businessDomain: "经营分析域",
      owner: "数据分析中心",
      totalScore: 0.82,
      scores: { semantic: 0.86, logic: 0.79, lineage: 0.82 },
      recommendation: "REVIEW",
      evidence: ["业务口径相近但仅统计个人客户", "包含额外客户等级与渠道维度", "部分上游来源相同"],
    },
    {
      assetId: "ADS_BRANCH_MTH_TRADE_SUM",
      assetName: "机构月度交易汇总表",
      businessDomain: "机构经营域",
      owner: "机构数据团队",
      totalScore: 0.72,
      scores: { semantic: 0.71, logic: 0.76, lineage: 0.69 },
      recommendation: "NO_DUPLICATE",
      evidence: ["聚合主体为机构而非客户", "使用额外机构层级映射表", "服务对象与使用场景不同"],
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
    sqlDialect: "MySQL",
    sql: `${isCorrection ? "-- 已根据用户反馈重新生成\n" : "-- 基于存量模板改写（MySQL）\n"}SELECT
    c.branch_id,
    COUNT(DISTINCT c.customer_id) AS new_customer_count
FROM retail_dm.customer_info AS c
WHERE c.first_open_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
  AND c.customer_status = 'ACTIVE'
GROUP BY c.branch_id
ORDER BY new_customer_count DESC;`,
    recommendedTables: ["retail_dm.customer_info"],
    recommendedFields: ["branch_id", "customer_id", "first_open_date", "customer_status"],
    explanation: isCorrection
      ? "已结合反馈重新检查日期字段、客户状态过滤条件和分行统计粒度。"
      : "检索到分行新增客户统计模板，并完成近90天时间窗口、客户状态和分行维度映射。",
    assumptions: ["新增客户按首次开户日期统计", "仅统计状态为ACTIVE的有效客户", "时间范围采用MySQL服务器当前日期"],
    validation: {
      passed: true,
      riskLevel: "LOW",
      items: [
        { label: "MySQL语法", status: "PASSED", detail: "Mock语法检查通过" },
        { label: "元数据检查", status: "PASSED", detail: "推荐表与字段均存在" },
        { label: "危险操作", status: "PASSED", detail: "未发现DDL/DML高风险操作" },
        { label: "业务口径", status: "WARNING", detail: "请人工确认ACTIVE客户口径" },
      ],
    },
  };
}

export function createMockResponse(input: ChatRequest): AgentResponse {
  const scene = detectScene(input.message, input.preferredScene);
  const version = input.correctionContext?.length ? 2 : 1;
  const taskId = makeId(scene === "ASSET_DUPLICATE" ? "TASK_ASSET" : scene === "SQL_GENERATION" ? "TASK_SQL" : "TASK_CHAT");
  const resultId = makeId(scene === "ASSET_DUPLICATE" ? "ASSET_RESULT" : scene === "SQL_GENERATION" ? "SQL_RESULT" : "CHAT_RESULT");
  let result: AgentResult;
  let summary: string;
  let status: AgentStatus = "REVIEW_REQUIRED";

  if (scene === "ASSET_DUPLICATE") {
    result = duplicateResult;
    summary = version > 1 ? "已根据反馈重新完成资产相似度识别" : "已完成语义、逻辑和血缘三维相似度分析";
  } else if (scene === "SQL_GENERATION") {
    result = buildSqlResult(input.correctionContext);
    summary = version > 1 ? "已根据修改意见生成新的MySQL版本" : "已检索存量模板并生成经过基础校验的MySQL脚本";
  } else {
    status = "NEED_MORE_INFORMATION";
    summary = "还需要确认您希望使用哪一种研发能力";
    result = {
      kind: "clarification",
      question: "您希望检查存量资产是否重复，还是希望根据业务需求生成SQL？",
      suggestions: [
        { label: "检查重复资产", message: "请检查客户月度交易汇总表是否存在重复资产", scene: "ASSET_DUPLICATE" },
        { label: "生成研发SQL", message: "请生成近90天各分行新增客户数SQL", scene: "SQL_GENERATION" },
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
      agentVersion: scene === "ASSET_DUPLICATE" ? "asset-agent-mock-1.0" : scene === "SQL_GENERATION" ? "sql-agent-mock-1.0" : "supervisor-mock-1.0",
      modelVersion: "MOCK_MODEL_V1",
      promptVersion: version > 1 ? "PROMPT_V1_CORRECTION" : "PROMPT_V1",
      metadataVersion: "META_2026_07",
    },
    createdAt: new Date().toISOString(),
  };
}
