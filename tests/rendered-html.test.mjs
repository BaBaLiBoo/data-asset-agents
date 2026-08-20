import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { restoreConversation } from "../lib/conversation-history.ts";
import {
  normalizeSupervisorResponse,
  supervisorSteps,
} from "../lib/supervisor-adapter.ts";
import {
  buildReflectionRetryDraft,
} from "../lib/reflection-retry.ts";

async function fetchFromBuiltApp(request) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${Math.random()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    request,
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the Shuyan Zhishu chat workbench", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/", { headers: { accept: "text/html" } }));
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /统一智能研发对话/);
  assert.match(html, /数研智枢/);
  assert.match(html, /数据中台资产智能研发助手/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("exposes single-delete and clear-history controls", async () => {
  const source = await readFile(
    new URL("../app/AgentWorkbench.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /aria-label="清空会话历史"/);
  assert.match(source, /aria-label={`删除会话：\$\{conversation\.title\}`}/);
  assert.match(source, /计划编号/);
  assert.match(source, /professionalToolCalls/);
  assert.match(source, /message\.response\.executionPlan\?\.steps/);
  assert.match(source, /response\.executionPlan\.status === "COMPLETED"/);
  assert.match(source, /智能检查/);
  assert.match(source, /不修改结果 · 不自动重试/);
  assert.match(source, /按检查意见重试/);
  assert.match(source, /确认提交后才由同一个专业智能体生成新版本/);
  assert.match(source, /aria-label="调整左侧栏宽度"/);
  assert.match(source, /aria-label="调整右侧栏宽度"/);
  assert.match(source, /aria-label=\{leftPanelCollapsed \? "展开左侧栏" : "收起左侧栏"\}/);
  assert.match(source, /aria-label=\{collapsed \? "展开右侧栏" : "收起右侧栏"\}/);
  assert.match(source, /COLLAPSED_PANEL_WIDTH/);
  assert.match(source, /研发体验用户/);
  assert.match(source, /数据中台 · 资产研发组/);
  assert.match(source, /资产复用检索/);
  assert.match(source, /以下结果仅为召回候选/);
  assert.match(source, /基于已发布本体生成PostgreSQL查询/);
  assert.doesNotMatch(source, /建议优先复用存量资产/);
  assert.doesNotMatch(source, /比较语义、逻辑与数据血缘/);
  assert.match(source, /aria-label="复制这条用户输入"/);
  assert.match(source, /navigator\.clipboard\.writeText\(message\.content\)/);
  assert.match(source, /streaming-step-card/);
  assert.match(source, /步骤 \{index \+ 1\}/);
  assert.match(source, /\{message\.streaming && \(/);
  assert.doesNotMatch(source, /message\.streaming \|\| Boolean\(message\.liveSteps/);
  assert.doesNotMatch(source, /模板相似度/);
  assert.doesNotMatch(source, /<header className="topbar">/);
});

test("real chat relays the supervisor SSE endpoint for every scene", async () => {
  const source = await readFile(
    new URL("../app/api/chat/stream/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /\/api\/v1\/chat\/stream/);
  assert.doesNotMatch(source, /\/api\/v1\/chat`/);
  assert.match(source, /correctionContext: body\.correctionContext \|\| \[\]/);
});

test("allows lineage submission from the required fields without a chat draft", async () => {
  const source = await readFile(
    new URL("../app/AgentWorkbench.tsx", import.meta.url),
    "utf8",
  );
  const graphSource = await readFile(
    new URL("../app/LineageGraph.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /const lineageReady = Boolean\(lineageTargetField\.trim\(\) && lineageScript\.trim\(\)\)/);
  assert.match(source, /isLineageMode \? lineageReady : Boolean\(draft\.trim\(\)\)/);
  assert.match(source, /disabled=\{!canSend\}/);
  assert.match(source, /aria-label=\{isLineageMode \? "开始血缘解析" : "发送消息"\}/);
  assert.match(source, /字段与脚本将随请求发送/);
  assert.match(source, /aria-label=\{composerCollapsed \? "展开血缘输入框" : "收起血缘输入框"\}/);
  assert.match(source, /if \(scene === "LINEAGE_PARSING"\) setComposerCollapsed\(true\)/);
  assert.doesNotMatch(source, /整体置信度|selectedNode\.confidence|path\.confidence|result\.warnings/);
  assert.doesNotMatch(graphSource, /node\.confidence/);
  assert.doesNotMatch(source, /path\.isMain|主路径|次路径/);
  assert.doesNotMatch(graphSource, /isMain/);
  assert.match(source, /aria-label=\{pathsCollapsed \? "展开来源路径" : "收起来源路径"\}/);
  assert.match(graphSource, /下载 SVG/);
  assert.match(graphSource, /image\/svg\+xml/);
  assert.match(graphSource, /URL\.createObjectURL/);
});

test("streams a PostgreSQL generation result through SSE", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json", "x-agent-api-mode": "mock" },
    body: JSON.stringify({
      conversationId: "CONV_TEST_SQL",
      message: "生成近90天各分行新增客户数SQL",
      preferredScene: "SQL_GENERATION",
      context: { sqlDialect: "PostgreSQL", schemaScope: [] },
    }),
  }));
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /text\/event-stream/);
  const body = await response.text();
  assert.match(body, /event: task/);
  assert.match(body, /event: step/);
  assert.match(body, /event: result/);
  assert.match(body, /SQL_GENERATION/);
  assert.match(body, /CURRENT_DATE - INTERVAL/);
  assert.match(body, /PostgreSQL/);
});

test("streams asset reuse candidates without a formal duplicate claim", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json", "x-agent-api-mode": "mock" },
    body: JSON.stringify({
      conversationId: "CONV_TEST_ASSET",
      message: "检索客户月度交易汇总表可能复用的已有资产候选",
      preferredScene: "ASSET_DUPLICATE",
    }),
  }));
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /ASSET_DUPLICATE/);
  assert.match(body, /ADS_CUST_MONTH_TRADE_STAT/);
  assert.match(body, /REVIEW/);
  assert.match(body, /尚未执行正式三层重复判定/);
});

test("requires an explanation for negative feedback", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/feedback", {
    method: "POST",
    headers: { "content-type": "application/json", "x-agent-api-mode": "mock" },
    body: JSON.stringify({
      conversationId: "CONV_TEST",
      taskId: "TASK_TEST",
      resultId: "RESULT_TEST",
      scene: "SQL_GENERATION",
      decision: "REJECT",
      retry: true,
    }),
  }));
  assert.equal(response.status, 400);
});

test("accepts a positive feedback decision without triggering retry", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/feedback", {
    method: "POST",
    headers: { "content-type": "application/json", "x-agent-api-mode": "mock" },
    body: JSON.stringify({
      conversationId: "CONV_TEST",
      taskId: "TASK_TEST",
      resultId: "RESULT_TEST",
      scene: "SQL_GENERATION",
      decision: "ACCEPT",
      reasonCodes: [],
      retry: false,
    }),
  }));
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.taskStatus, "SUCCESS");
  assert.equal(payload.retryTriggered, false);
});

test("accepts reflection-derived reasons only after explicit retry submission", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/feedback", {
    method: "POST",
    headers: { "content-type": "application/json", "x-agent-api-mode": "mock" },
    body: JSON.stringify({
      conversationId: "CONV_REFLECTION_RETRY",
      taskId: "TASK_REFLECTION_RETRY",
      resultId: "RESULT_REFLECTION_RETRY",
      scene: "SQL_GENERATION",
      decision: "REJECT",
      reasonCodes: ["TIME_RANGE_MISMATCH", "TABLE_FIELD_MISMATCH"],
      comment: "按智能检查意见重试：时间范围和字段需要修正",
      retry: true,
    }),
  }));
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.retryTriggered, true);
  assert.equal(payload.nextResultVersion, 2);
});

test("builds an editable retry draft from warning and unknown reflection items", () => {
  const draft = buildReflectionRetryDraft({
    status: "WARNING",
    summary: "存在两项问题",
    riskLevel: "MEDIUM",
    items: [
      {
        category: "TIME_RANGE",
        status: "WARNING",
        requirement: "最近180天",
        observation: "SQL使用最近90天",
        suggestion: "改为180天",
      },
      {
        category: "TABLE_FIELD",
        status: "UNKNOWN",
        requirement: "沿用客户主题表",
        observation: "无法确认当前推荐表",
        suggestion: "确认表和字段",
      },
      {
        category: "METRIC",
        status: "PASSED",
        requirement: "新增客户数",
        observation: "已按客户去重",
        suggestion: "",
      },
    ],
  });

  assert.deepEqual(draft?.reasonCodes, [
    "TIME_RANGE_MISMATCH",
    "TABLE_FIELD_MISMATCH",
  ]);
  assert.match(draft?.comment ?? "", /需求：最近180天/);
  assert.match(draft?.comment ?? "", /观察：无法确认当前推荐表/);
  assert.doesNotMatch(draft?.comment ?? "", /新增客户数/);

  const boundedItemsDraft = buildReflectionRetryDraft({
    status: "WARNING",
    summary: "大量检查项",
    riskLevel: "HIGH",
    items: Array.from({ length: 12 }, (_, index) => ({
      category: "TIME_RANGE",
      status: "WARNING",
      requirement: `第${index + 1}项需求`,
      observation: "需要检查",
      suggestion: "请修正",
    })),
  });
  assert.equal(
    (boundedItemsDraft?.comment.match(/\d+\. \[时间范围\]/g) ?? []).length,
    8,
  );
  const boundedCommentDraft = buildReflectionRetryDraft({
    status: "WARNING",
    summary: "超长检查意见",
    riskLevel: "HIGH",
    items: Array.from({ length: 8 }, () => ({
      category: "TIME_RANGE",
      status: "WARNING",
      requirement: "需求".repeat(300),
      observation: "观察".repeat(500),
      suggestion: "建议".repeat(300),
    })),
  });
  assert.ok((boundedCommentDraft?.comment.length ?? 0) <= 3000);
});

test("does not offer a retry draft for passed or unavailable reflection", () => {
  for (const status of ["PASSED", "UNAVAILABLE"]) {
    assert.equal(buildReflectionRetryDraft({
      status,
      summary: "无需自动生成反馈",
      riskLevel: "LOW",
      items: [],
    }), null);
  }
});

test("keeps browser history as fallback while mock mode is active", async () => {
  const conversations = await fetchFromBuiltApp(new Request(
    "http://localhost/api/conversations",
    { headers: { "x-agent-api-mode": "mock" } },
  ));
  assert.equal(conversations.status, 404);

  const messages = await fetchFromBuiltApp(new Request(
    "http://localhost/api/conversations/CONV_TEST/messages",
    { headers: { "x-agent-api-mode": "mock" } },
  ));
  assert.equal(messages.status, 404);
});

test("deletes one conversation through the frontend API", async () => {
  const response = await fetchFromBuiltApp(new Request(
    "http://localhost/api/conversations/CONV_TEST",
    {
      method: "DELETE",
      headers: { "x-agent-api-mode": "mock" },
    },
  ));
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.conversationId, "CONV_TEST");
  assert.equal(payload.deleted, true);
  assert.ok(payload.deletedAt);
});

test("clears conversation history through the frontend API", async () => {
  const response = await fetchFromBuiltApp(new Request(
    "http://localhost/api/conversations",
    {
      method: "DELETE",
      headers: { "x-agent-api-mode": "mock" },
    },
  ));
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.deletedCount, 0);
  assert.ok(payload.deletedAt);
});

function storedSqlResponse({
  resultId,
  status,
  version,
  summary,
}) {
  return {
    conversationId: "CONV_HISTORY",
    taskId: "TASK_HISTORY",
    traceId: `TRACE_${version}`,
    resultId,
    scene: "SQL_GENERATION",
    status,
    version,
    summary,
    result: {
      kind: "sql",
      template: null,
      sqlDialect: "PostgreSQL",
      sql: `SELECT ${version}`,
      recommendedTables: [],
      recommendedFields: [],
      explanation: "历史结果",
      assumptions: [],
      validation: {
        passed: true,
        riskLevel: "LOW",
        checks: [],
        warnings: [],
      },
    },
    provenance: {
      serviceVersion: "test",
      modelVersion: "test",
      promptVersion: "test",
      metadataVersion: "test",
    },
    createdAt: `2026-07-29T10:0${version}:00+08:00`,
  };
}

const storedSummary = {
  conversationId: "CONV_HISTORY",
  userId: "frontend-user",
  title: "历史SQL任务",
  preferredScene: "SQL_GENERATION",
  messageCount: 3,
  createdAt: "2026-07-29T10:00:00+08:00",
  updatedAt: "2026-07-29T10:03:00+08:00",
};

test("restores an accepted database result without an extra feedback bubble", () => {
  const restored = restoreConversation(storedSummary, [
    {
      messageId: "M1",
      conversationId: "CONV_HISTORY",
      role: "USER",
      content: "生成SQL",
      sequenceNumber: 1,
      taskId: "TASK_HISTORY",
      resultId: null,
      metadata: {},
      createdAt: "2026-07-29T10:00:00+08:00",
    },
    {
      messageId: "M2",
      conversationId: "CONV_HISTORY",
      role: "ASSISTANT",
      content: "已采纳",
      sequenceNumber: 2,
      taskId: "TASK_HISTORY",
      resultId: "RESULT_V1",
      metadata: {
        response: storedSqlResponse({
          resultId: "RESULT_V1",
          status: "SUCCESS",
          version: 1,
          summary: "用户已确认并采纳当前结果",
        }),
      },
      createdAt: "2026-07-29T10:01:00+08:00",
    },
    {
      messageId: "M3",
      conversationId: "CONV_HISTORY",
      role: "USER",
      content: "采纳当前结果",
      sequenceNumber: 3,
      taskId: "TASK_HISTORY",
      resultId: "RESULT_V1",
      metadata: {
        feedback: { decision: "ACCEPT" },
      },
      createdAt: "2026-07-29T10:02:00+08:00",
    },
  ]);

  assert.equal(restored.messages.length, 2);
  assert.equal(restored.messages[1].feedback, "ACCEPT");
  assert.equal(restored.messages[1].response.status, "SUCCESS");
});

test("restores rejected v1 feedback and the regenerated v2", () => {
  const restored = restoreConversation(
    { ...storedSummary, messageCount: 4 },
    [
      {
        messageId: "M1",
        conversationId: "CONV_HISTORY",
        role: "USER",
        content: "生成SQL",
        sequenceNumber: 1,
        taskId: "TASK_HISTORY",
        resultId: null,
        metadata: {},
        createdAt: "2026-07-29T10:00:00+08:00",
      },
      {
        messageId: "M2",
        conversationId: "CONV_HISTORY",
        role: "ASSISTANT",
        content: "SQL v1",
        sequenceNumber: 2,
        taskId: "TASK_HISTORY",
        resultId: "RESULT_V1",
        metadata: {
          response: storedSqlResponse({
            resultId: "RESULT_V1",
            status: "REVIEW_REQUIRED",
            version: 1,
            summary: "SQL v1",
          }),
        },
        createdAt: "2026-07-29T10:01:00+08:00",
      },
      {
        messageId: "M3",
        conversationId: "CONV_HISTORY",
        role: "USER",
        content: "字段选择不正确",
        sequenceNumber: 3,
        taskId: "TASK_HISTORY",
        resultId: "RESULT_V1",
        metadata: {
          feedback: { decision: "REJECT" },
        },
        createdAt: "2026-07-29T10:02:00+08:00",
      },
      {
        messageId: "M4",
        conversationId: "CONV_HISTORY",
        role: "ASSISTANT",
        content: "SQL v2",
        sequenceNumber: 4,
        taskId: "TASK_HISTORY",
        resultId: "RESULT_V2",
        metadata: {
          response: storedSqlResponse({
            resultId: "RESULT_V2",
            status: "REVIEW_REQUIRED",
            version: 2,
            summary: "SQL v2",
          }),
        },
        createdAt: "2026-07-29T10:03:00+08:00",
      },
    ],
  );

  assert.equal(restored.messages.length, 4);
  assert.equal(restored.messages[1].feedback, "REJECT");
  assert.match(restored.messages[2].content, /不满意反馈/);
  assert.equal(restored.messages[3].response.version, 2);
});

test("stops the execution trace before professional service when information is missing", () => {
  const steps = supervisorSteps(
    "CLARIFICATION",
    "NEED_MORE_INFORMATION",
    {
      kind: "clarification",
      question: "请补充需要计算的指标。",
      suggestions: [
        {
          label: "生成研发SQL",
          message: "请生成新增客户数SQL",
          scene: "SQL_GENERATION",
        },
      ],
    },
    "SQL_GENERATION",
  );

  assert.deepEqual(
    steps.map((step) => step.label),
    [
      "理解并结构化需求",
      "确认SQL生成场景",
      "检查必要信息",
      "等待用户补充",
    ],
  );
  assert.equal(steps.at(-1)?.status, "WAITING");
  assert.equal(steps.some((step) => step.tool === "generate_sql"), false);
});

test("shows professional execution and human review for review-required results", () => {
  const steps = supervisorSteps(
    "SQL_GENERATION",
    "REVIEW_REQUIRED",
    null,
    "SQL_GENERATION",
  );

  assert.equal(
    steps.find((step) => step.id === "professional-service")?.tool,
    "generate_sql",
  );
  assert.equal(steps.at(-1)?.label, "等待人工确认");
  assert.equal(steps.at(-1)?.status, "WAITING");
});

test("uses the backend controlled execution plan when present", () => {
  const response = normalizeSupervisorResponse(
    {
      conversationId: "CONV_PLAN",
      taskId: "TASK_PLAN",
      resultId: "RESULT_PLAN",
      scene: "SQL_GENERATION",
      status: "REVIEW_REQUIRED",
      version: 1,
      summary: "等待人工确认",
      result: null,
      executionPlan: {
        planId: "PLAN_1",
        scene: "SQL_GENERATION",
        selectedTool: "generate_sql",
        status: "REVIEW",
        steps: [
          {
            code: "UNDERSTAND",
            label: "后端计划：理解需求",
            detail: "读取当前消息和历史摘要",
            status: "SUCCESS",
            tool: null,
          },
          {
            code: "EXECUTE",
            label: "后端计划：调用任务二",
            detail: "仅调用一次",
            status: "SUCCESS",
            tool: "generate_sql",
          },
          {
            code: "REFLECT",
            label: "后端计划：反思结果",
            detail: "时间范围需要确认",
            status: "SUCCESS",
            tool: null,
          },
          {
            code: "AWAIT_REVIEW",
            label: "后端计划：等待确认",
            detail: "需要人工确认",
            status: "WAITING",
            tool: null,
          },
        ],
        constraints: {
          maxProfessionalToolCalls: 1,
          professionalToolCalls: 1,
          allowCrossScene: false,
          requireHumanReview: true,
        },
      },
      reflection: {
        status: "WARNING",
        summary: "SQL仍使用90天，需要确认是否改为180天。",
        items: [
          {
            category: "TIME_RANGE",
            status: "WARNING",
            requirement: "最近180天",
            observation: "SQL使用最近90天",
            suggestion: "确认后通过反馈闭环重新生成",
          },
        ],
        riskLevel: "MEDIUM",
      },
      provenance: {
        serviceVersion: "test",
        modelVersion: "test",
        promptVersion: "test",
        metadataVersion: "test",
      },
      createdAt: "2026-07-30T10:00:00+08:00",
    },
    {
      conversationId: "CONV_PLAN",
      message: "生成SQL",
      preferredScene: "SQL_GENERATION",
    },
  );

  assert.equal(response.executionPlan?.selectedTool, "generate_sql");
  assert.equal(response.executionPlan?.constraints.professionalToolCalls, 1);
  assert.equal(response.reflection?.status, "WARNING");
  assert.equal(response.reflection?.items[0]?.category, "TIME_RANGE");
  assert.deepEqual(
    response.executionTrace.map((step) => step.label),
    [
      "后端计划：理解需求",
      "后端计划：调用任务二",
      "后端计划：反思结果",
      "后端计划：等待确认",
    ],
  );
});

test("stops the execution trace at a failed professional service", () => {
  const steps = supervisorSteps(
    "ASSET_DUPLICATE",
    "FAILED",
    null,
    "ASSET_DUPLICATE",
    "任务一服务不可用",
  );

  assert.equal(steps.at(-1)?.status, "FAILED");
  assert.equal(steps.at(-1)?.detail, "任务一服务不可用");
  assert.equal(steps.some((step) => step.id === "review"), false);
});
