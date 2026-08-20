import type {
  AgentResponse,
  AgentResult,
  AgentScene,
  AgentStatus,
  ChatRequest,
  DuplicateCandidate,
  ExecutionPlan,
  ExecutionStep,
  LineageEdge,
  LineageGraph,
  LineageGraphSummary,
  LineageMetrics,
  LineageNode,
  LineagePath,
  LineageResult,
  PreferredScene,
  ResultReflection,
  SqlResult,
} from "./mock-agent";

type JsonRecord = Record<string, unknown>;

const scenes = new Set<AgentScene>(["ASSET_DUPLICATE", "SQL_GENERATION", "LINEAGE_PARSING", "CLARIFICATION"]);
const statuses = new Set<AgentStatus>([
  "PROCESSING",
  "SUCCESS",
  "NEED_MORE_INFORMATION",
  "REVIEW_REQUIRED",
  "FAILED",
]);
const planStatuses = new Set<ExecutionPlan["status"]>([
  "WAITING_INPUT",
  "READY",
  "RUNNING",
  "REVIEW",
  "FAILED",
  "COMPLETED",
]);
const stepStatuses = new Set<ExecutionStep["status"]>([
  "RUNNING",
  "SUCCESS",
  "WAITING",
  "SKIPPED",
  "FAILED",
]);
const planStepIds: Record<string, string> = {
  UNDERSTAND: "understand",
  ROUTE: "route",
  VALIDATE: "validate",
  EXECUTE: "professional-service",
  INSPECT: "review",
  REFLECT: "reflect",
  AWAIT_USER: "await-user",
  AWAIT_REVIEW: "await-review",
  COMPLETE: "complete",
};
const reflectionStatuses = new Set<ResultReflection["status"]>([
  "PASSED",
  "WARNING",
  "UNAVAILABLE",
]);
const reflectionItemStatuses = new Set<
  ResultReflection["items"][number]["status"]
>(["PASSED", "WARNING", "UNKNOWN"]);
const reflectionCategories = new Set<
  ResultReflection["items"][number]["category"]
>([
  "METRIC",
  "DIMENSION",
  "GRAIN",
  "TIME_RANGE",
  "FILTER",
  "TABLE_FIELD",
  "ASSET_EVIDENCE",
  "RISK",
]);
const reflectionRiskLevels = new Set<ResultReflection["riskLevel"]>([
  "LOW",
  "MEDIUM",
  "HIGH",
]);

function record(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : null;
}

function text(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function sceneValue(value: unknown): AgentScene {
  return typeof value === "string" && scenes.has(value as AgentScene)
    ? value as AgentScene
    : "CLARIFICATION";
}

function statusValue(value: unknown): AgentStatus {
  return typeof value === "string" && statuses.has(value as AgentStatus)
    ? value as AgentStatus
    : "FAILED";
}

function normalizeExecutionPlan(value: unknown): ExecutionPlan | null {
  const plan = record(value);
  const constraints = record(plan?.constraints);
  if (
    !plan
    || typeof plan.planId !== "string"
    || typeof plan.scene !== "string"
    || !scenes.has(plan.scene as AgentScene)
    || typeof plan.status !== "string"
    || !planStatuses.has(plan.status as ExecutionPlan["status"])
    || !Array.isArray(plan.steps)
    || constraints?.maxProfessionalToolCalls !== 1
    || (constraints.professionalToolCalls !== 0 && constraints.professionalToolCalls !== 1)
    || constraints.allowCrossScene !== false
    || constraints.requireHumanReview !== true
  ) {
    return null;
  }
  const selectedTool =
    plan.selectedTool === "check_asset_duplicate"
      || plan.selectedTool === "generate_sql"
      || plan.selectedTool === "parse_regulatory_lineage"
      ? plan.selectedTool
      : null;
  const steps = plan.steps.map((value): ExecutionStep | null => {
    const step = record(value);
    if (
      !step
      || typeof step.code !== "string"
      || !planStepIds[step.code]
      || typeof step.label !== "string"
      || typeof step.detail !== "string"
      || typeof step.status !== "string"
      || !stepStatuses.has(step.status as ExecutionStep["status"])
    ) {
      return null;
    }
    const tool =
      step.tool === "check_asset_duplicate"
        || step.tool === "generate_sql"
        || step.tool === "parse_regulatory_lineage"
        ? step.tool
        : undefined;
    return {
      id: planStepIds[step.code],
      label: step.label,
      detail: step.detail,
      status: step.status as ExecutionStep["status"],
      ...(tool ? { tool } : {}),
    };
  }).filter((step): step is ExecutionStep => Boolean(step));
  if (steps.length !== plan.steps.length) return null;
  if (constraints.professionalToolCalls === 1 && !selectedTool) return null;
  return {
    planId: plan.planId,
    scene: plan.scene as AgentScene,
    selectedTool,
    status: plan.status as ExecutionPlan["status"],
    steps,
    constraints: {
      maxProfessionalToolCalls: 1,
      professionalToolCalls: constraints.professionalToolCalls as 0 | 1,
      allowCrossScene: false,
      requireHumanReview: true,
    },
  };
}

function normalizeReflection(value: unknown): ResultReflection | null {
  const reflection = record(value);
  if (
    !reflection
    || typeof reflection.status !== "string"
    || !reflectionStatuses.has(reflection.status as ResultReflection["status"])
    || typeof reflection.summary !== "string"
    || !reflection.summary.trim()
    || !Array.isArray(reflection.items)
    || typeof reflection.riskLevel !== "string"
    || !reflectionRiskLevels.has(
      reflection.riskLevel as ResultReflection["riskLevel"],
    )
  ) {
    return null;
  }
  const items = reflection.items.map((value) => {
    const item = record(value);
    if (
      !item
      || typeof item.category !== "string"
      || !reflectionCategories.has(
        item.category as ResultReflection["items"][number]["category"],
      )
      || typeof item.status !== "string"
      || !reflectionItemStatuses.has(
        item.status as ResultReflection["items"][number]["status"],
      )
      || typeof item.requirement !== "string"
      || !item.requirement.trim()
      || typeof item.observation !== "string"
      || !item.observation.trim()
      || typeof item.suggestion !== "string"
    ) {
      return null;
    }
    return {
      category:
        item.category as ResultReflection["items"][number]["category"],
      status: item.status as ResultReflection["items"][number]["status"],
      requirement: item.requirement,
      observation: item.observation,
      suggestion: item.suggestion,
    };
  }).filter(
    (item): item is ResultReflection["items"][number] => Boolean(item),
  );
  if (items.length !== reflection.items.length || items.length > 20) return null;
  return {
    status: reflection.status as ResultReflection["status"],
    summary: reflection.summary,
    items,
    riskLevel: reflection.riskLevel as ResultReflection["riskLevel"],
  };
}

function normalizeCandidate(value: unknown): DuplicateCandidate | null {
  const candidate = record(value);
  const scores = record(candidate?.scores);
  if (!candidate || !scores) return null;
  const recommendation = text(candidate.recommendation, "REVIEW");
  return {
    assetId: text(candidate.assetId, "UNKNOWN_ASSET"),
    assetName: text(candidate.assetName, "未命名资产"),
    businessDomain: text(candidate.businessDomain, "待确认"),
    owner: text(candidate.owner, "待确认"),
    totalScore: numberValue(candidate.totalScore),
    scores: {
      semantic: numberValue(scores.semantic),
      logic: numberValue(scores.logic),
      lineage: numberValue(scores.lineage),
    },
    recommendation:
      recommendation === "REUSE" || recommendation === "NO_DUPLICATE"
        ? recommendation
        : "REVIEW",
    evidence: stringArray(candidate.evidence),
  };
}

function normalizeSqlResult(rawResult: JsonRecord): SqlResult {
  const template = record(rawResult.template);
  const tables = Array.isArray(rawResult.recommendedTables)
    ? rawResult.recommendedTables.map((item) => {
        const table = record(item);
        return table ? text(table.tableName) : text(item);
      }).filter(Boolean)
    : [];
  const fields = Array.isArray(rawResult.recommendedFields)
    ? rawResult.recommendedFields.map((item) => {
        const field = record(item);
        if (!field) return text(item);
        const tableName = text(field.tableName);
        const fieldName = text(field.fieldName);
        return tableName && fieldName ? `${tableName}.${fieldName}` : fieldName;
      }).filter(Boolean)
    : [];
  const validation = record(rawResult.validation);
  const checks = Array.isArray(validation?.checks) ? validation.checks : [];
  const warnings = stringArray(validation?.warnings);
  return {
    kind: "sql",
    template: template
      ? {
          templateId: text(template.templateId),
          templateName: text(template.templateName, "未提供模板名称"),
          similarity: numberValue(template.similarity),
          owner: text(template.owner, "待确认"),
        }
      : null,
    sqlDialect: text(rawResult.sqlDialect, "UNKNOWN"),
    sql: text(rawResult.sql),
    recommendedTables: tables,
    recommendedFields: fields,
    explanation: text(rawResult.explanation),
    assumptions: stringArray(rawResult.assumptions),
    validation: {
      passed: validation?.passed === true,
      riskLevel:
        validation?.riskLevel === "HIGH" || validation?.riskLevel === "MEDIUM"
          ? validation.riskLevel
          : "LOW",
      items: [
        ...checks.map((item) => {
          const check = record(item);
          return {
            label: text(check?.type, "校验项"),
            status: check?.passed === true ? "PASSED" as const : "WARNING" as const,
            detail: text(check?.message),
          };
        }),
        ...warnings.map((warning) => ({
          label: "人工确认",
          status: "WARNING" as const,
          detail: warning,
        })),
      ],
    },
  };
}

function normalizeLineageNode(value: unknown): LineageNode | null {
  const node = record(value);
  if (
    !node
    || typeof node.nodeId !== "string"
    || typeof node.type !== "string"
    || typeof node.label !== "string"
  ) {
    return null;
  }
  return {
    nodeId: node.nodeId,
    type: node.type as LineageNode["type"],
    label: node.label,
    table: text(node.table) || null,
    field: text(node.field) || null,
    parentNodeId: text(node.parentNodeId) || null,
    nestingLevel: typeof node.nestingLevel === "number" ? node.nestingLevel : undefined,
    expression: text(node.expression) || null,
    sqlLineStart: typeof node.sqlLineStart === "number" ? node.sqlLineStart : null,
    sqlLineEnd: typeof node.sqlLineEnd === "number" ? node.sqlLineEnd : null,
    description: text(node.description) || null,
    confidence: typeof node.confidence === "number" ? node.confidence : null,
    warning: text(node.warning) || null,
  };
}

function normalizeLineageEdge(value: unknown): LineageEdge | null {
  const edge = record(value);
  if (
    !edge
    || typeof edge.edgeId !== "string"
    || typeof edge.source !== "string"
    || typeof edge.target !== "string"
    || typeof edge.relation !== "string"
  ) {
    return null;
  }
  return {
    edgeId: edge.edgeId,
    source: edge.source,
    target: edge.target,
    relation: edge.relation as LineageEdge["relation"],
    pathIds: stringArray(edge.pathIds),
    expression: text(edge.expression) || null,
    confidence: typeof edge.confidence === "number" ? edge.confidence : null,
  };
}

function normalizeLineagePath(value: unknown): LineagePath | null {
  const path = record(value);
  if (
    !path
    || typeof path.pathId !== "string"
    || typeof path.sourceNodeId !== "string"
    || typeof path.targetNodeId !== "string"
    || !Array.isArray(path.nodeIds)
    || !Array.isArray(path.keyRules)
  ) {
    return null;
  }
  return {
    pathId: path.pathId,
    sourceNodeId: path.sourceNodeId,
    targetNodeId: path.targetNodeId,
    nodeIds: stringArray(path.nodeIds),
    keyRules: stringArray(path.keyRules),
    confidence: numberValue(path.confidence),
    isMain: path.isMain === true,
  };
}

function normalizeLineageGraph(value: unknown): LineageGraph | null {
  const graph = record(value);
  if (!graph) return null;
  return {
    nodes: Array.isArray(graph.nodes)
      ? graph.nodes.map(normalizeLineageNode).filter((n): n is LineageNode => Boolean(n))
      : [],
    edges: Array.isArray(graph.edges)
      ? graph.edges.map(normalizeLineageEdge).filter((e): e is LineageEdge => Boolean(e))
      : [],
    paths: Array.isArray(graph.paths)
      ? graph.paths.map(normalizeLineagePath).filter((p): p is LineagePath => Boolean(p))
      : [],
  };
}

function normalizeLineageMetrics(value: unknown): LineageMetrics | null {
  const metrics = record(value);
  if (!metrics) return null;
  return {
    sourceTables: numberValue(metrics.sourceTables),
    sourceFields: numberValue(metrics.sourceFields),
    sourcePaths: numberValue(metrics.sourcePaths),
    maxNestingDepth: numberValue(metrics.maxNestingDepth),
    operatorCount: numberValue(metrics.operatorCount),
    joinCount: numberValue(metrics.joinCount),
    caseCount: numberValue(metrics.caseCount),
  };
}

function normalizeLineageGraphSummary(value: unknown): LineageGraphSummary | null {
  const summary = record(value);
  if (!summary) return null;
  return {
    totalNodes: numberValue(summary.totalNodes),
    totalEdges: numberValue(summary.totalEdges),
    totalPaths: numberValue(summary.totalPaths),
  };
}

function normalizeLineageResult(rawResult: JsonRecord): LineageResult {
  return {
    kind: "lineage",
    jobId: text(rawResult.jobId, "UNKNOWN_JOB"),
    scriptId: text(rawResult.scriptId, "UNKNOWN_SCRIPT"),
    targetField: text(rawResult.targetField, "UNKNOWN_TARGET"),
    sqlDialect: text(rawResult.sqlDialect) || null,
    metrics: normalizeLineageMetrics(rawResult.metrics),
    paths: Array.isArray(rawResult.paths)
      ? rawResult.paths.map(normalizeLineagePath).filter((p): p is LineagePath => Boolean(p))
      : [],
    graph: normalizeLineageGraph(rawResult.graph),
    graphSummary: normalizeLineageGraphSummary(rawResult.graphSummary),
    summary: text(rawResult.summary, "血缘解析完成。"),
    confidence: typeof rawResult.confidence === "number" ? rawResult.confidence : null,
    warnings: stringArray(rawResult.warnings),
  };
}

function normalizeResult(
  scene: AgentScene,
  value: unknown,
): AgentResult | null {
  const rawResult = record(value);
  if (!rawResult) return null;

  if (scene === "ASSET_DUPLICATE" && rawResult.kind === "duplicate") {
    return {
      kind: "duplicate",
      canReuse: rawResult.canReuse === true,
      recommendation: text(rawResult.recommendation),
      threshold: numberValue(rawResult.threshold),
      candidates: Array.isArray(rawResult.candidates)
        ? rawResult.candidates
            .map(normalizeCandidate)
            .filter((item): item is DuplicateCandidate => Boolean(item))
        : [],
    };
  }

  if (scene === "SQL_GENERATION" && rawResult.kind === "sql") {
    return normalizeSqlResult(rawResult);
  }

  if (scene === "LINEAGE_PARSING" && rawResult.kind === "lineage") {
    return normalizeLineageResult(rawResult);
  }

  if (rawResult.kind === "clarification") {
    const supported = stringArray(rawResult.supportedScenes);
    const suggestions = supported
      .filter((item): item is Exclude<PreferredScene, "AUTO"> =>
        item === "ASSET_DUPLICATE" || item === "SQL_GENERATION" || item === "LINEAGE_PARSING")
      .map((item) => ({
        label:
          item === "ASSET_DUPLICATE"
            ? "检索可复用资产"
            : item === "LINEAGE_PARSING"
              ? "解析字段血缘"
              : "生成研发SQL",
        message:
          item === "ASSET_DUPLICATE"
            ? "请检索客户月度交易汇总表可能复用的已有资产候选"
            : item === "LINEAGE_PARSING"
              ? "请解析 EAST.BD_ODS_BNWYWDBHTB::DBHTH 字段的血缘"
              : "请生成近90天各分行新增客户数SQL",
        scene: item,
      }));
    return {
      kind: "clarification",
      question: text(rawResult.question, "请确认需要使用的专业能力。"),
      suggestions,
    };
  }
  return null;
}

function resolvedProfessionalScene(
  scene: AgentScene,
  result: AgentResult | null,
  preferredScene: PreferredScene,
): Exclude<PreferredScene, "AUTO"> | null {
  if (scene === "ASSET_DUPLICATE" || scene === "SQL_GENERATION" || scene === "LINEAGE_PARSING") return scene;
  if (preferredScene !== "AUTO") return preferredScene;
  if (result?.kind === "clarification" && result.suggestions.length === 1) {
    return result.suggestions[0].scene;
  }
  return null;
}

function sceneName(scene: Exclude<PreferredScene, "AUTO">) {
  if (scene === "SQL_GENERATION") return "SQL生成";
  if (scene === "LINEAGE_PARSING") return "血缘解析";
  return "资产复用检索";
}

function professionalTool(scene: Exclude<PreferredScene, "AUTO">) {
  if (scene === "SQL_GENERATION") return "generate_sql";
  if (scene === "LINEAGE_PARSING") return "parse_regulatory_lineage";
  return "check_asset_duplicate";
}

export function supervisorSteps(
  scene: AgentScene,
  status: AgentStatus,
  result: AgentResult | null,
  preferredScene: PreferredScene = "AUTO",
  errorMessage = "",
): ExecutionStep[] {
  const targetScene = resolvedProfessionalScene(scene, result, preferredScene);
  const steps: ExecutionStep[] = [
    {
      id: "understand",
      label: "理解并结构化需求",
      detail: "识别用户意图并提取明确提供的业务参数",
      status: "SUCCESS",
    },
  ];

  if (status === "NEED_MORE_INFORMATION" && scene === "CLARIFICATION") {
    if (targetScene) {
      steps.push({
        id: "route",
        label: `确认${sceneName(targetScene)}场景`,
        detail: "已锁定专业能力，模型不能改变用户指定的任务类型",
        status: "SUCCESS",
      });
      steps.push({
        id: "validate",
        label: "检查必要信息",
        detail: "业务参数不完整，尚未调用专业服务",
        status: "WAITING",
      });
    } else {
      steps.push({
        id: "route",
        label: "确认任务场景",
        detail: "当前描述不足以确定资产复用检索、SQL生成或血缘解析",
        status: "WAITING",
      });
    }
    steps.push({
      id: "await-user",
      label: "等待用户补充",
      detail:
        result?.kind === "clarification"
          ? result.question
          : "补充信息后将从需求检查阶段继续",
      status: "WAITING",
    });
    return steps;
  }

  if (targetScene) {
    steps.push(
      {
        id: "validate",
        label: "检查必要信息",
        detail: "参数格式和最低业务信息检查通过",
        status: "SUCCESS",
      },
      {
        id: "professional-service",
        label: `调用${sceneName(targetScene)}智能体`,
        detail: "通过受控HTTP接口执行单一专业任务",
        tool: professionalTool(targetScene),
        status:
          status === "FAILED"
            ? "FAILED"
            : status === "PROCESSING"
              ? "RUNNING"
              : "SUCCESS",
      },
    );
  }

  if (status === "FAILED") {
    const failedStep = steps.at(-1);
    if (failedStep && failedStep.id === "professional-service") {
      failedStep.detail = errorMessage || "专业服务或总控工作流执行失败";
    } else {
      steps.push({
        id: "failed",
        label: "工作流执行失败",
        detail: errorMessage || "总控工作流执行失败",
        status: "FAILED",
      });
    }
    return steps;
  }

  if (status === "PROCESSING") return steps;

  if (status === "NEED_MORE_INFORMATION") {
    steps.push(
      {
        id: "review",
        label: "检查专业结果",
        detail: "专业智能体需要更多业务信息才能继续",
        status: "WAITING",
      },
      {
        id: "await-user",
        label: "等待用户补充",
        detail:
          result?.kind === "clarification"
            ? result.question
            : "补充信息后将继续当前专业任务",
        status: "WAITING",
      },
    );
    return steps;
  }

  steps.push({
    id: "review",
    label: "检查专业结果",
    detail: "场景、状态、结果内容和来源信息检查通过",
    status: "SUCCESS",
  });
  if (status === "REVIEW_REQUIRED") {
    steps.push({
      id: "await-review",
      label: "等待人工确认",
      detail: "专业结果已生成，上线或复用前需要用户确认",
      status: "WAITING",
    });
  } else {
    steps.push({
      id: "complete",
      label: "任务完成",
      detail: "结果已通过总控检查并返回",
      status: "SUCCESS",
    });
  }
  return steps;
}

export function normalizeSupervisorResponse(
  value: unknown,
  request: ChatRequest,
): AgentResponse {
  const raw = record(value);
  if (!raw) throw new Error("总控没有返回JSON对象");
  const scene = sceneValue(raw.scene);
  const status = statusValue(raw.status);
  const provenance = record(raw.provenance);
  const error = record(raw.error);
  const result = normalizeResult(scene, raw.result);
  const executionPlan = normalizeExecutionPlan(raw.executionPlan);
  const reflection = normalizeReflection(raw.reflection);
  const errorMessage = text(error?.message);
  const response: AgentResponse = {
    conversationId: text(raw.conversationId, request.conversationId),
    taskId: text(raw.taskId),
    resultId: text(raw.resultId),
    scene,
    status,
    version: Math.max(1, numberValue(raw.version, 1)),
    summary: text(raw.summary, status === "FAILED" ? "总控调用失败" : "任务已完成"),
    ...(executionPlan ? { executionPlan } : {}),
    ...(reflection ? { reflection } : {}),
    executionTrace:
      executionPlan?.steps
      ?? supervisorSteps(
        scene,
        status,
        result,
        request.preferredScene || "AUTO",
        errorMessage,
      ),
    result,
    provenance: {
      agentVersion: text(provenance?.serviceVersion, "UNKNOWN"),
      modelVersion: text(provenance?.modelVersion, "UNKNOWN"),
      promptVersion: text(provenance?.promptVersion, "UNKNOWN"),
      metadataVersion: text(provenance?.metadataVersion, "UNKNOWN"),
    },
    createdAt: text(raw.createdAt, new Date().toISOString()),
  };
  if (error) {
    response.error = {
      code: text(error.code, "INTERNAL_ERROR"),
      message: text(error.message, response.summary),
      retryable: error.retryable === true,
    };
  }
  return response;
}
