import assert from "node:assert/strict";
import test from "node:test";

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

test("streams a MySQL generation result through SSE", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      conversationId: "CONV_TEST_SQL",
      message: "生成近90天各分行新增客户数SQL",
      preferredScene: "SQL_GENERATION",
      context: { sqlDialect: "MySQL", schemaScope: ["retail_dm"] },
    }),
  }));
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /text\/event-stream/);
  const body = await response.text();
  assert.match(body, /event: task/);
  assert.match(body, /event: step/);
  assert.match(body, /event: result/);
  assert.match(body, /SQL_GENERATION/);
  assert.match(body, /DATE_SUB\(CURDATE\(\), INTERVAL 90 DAY\)/);
});

test("streams an asset duplicate result", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      conversationId: "CONV_TEST_ASSET",
      message: "检查客户月度交易汇总表是否存在重复资产",
      preferredScene: "ASSET_DUPLICATE",
    }),
  }));
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /ASSET_DUPLICATE/);
  assert.match(body, /ADS_CUST_MONTH_TRADE_STAT/);
  assert.match(body, /semantic/);
  assert.match(body, /lineage/);
});

test("requires an explanation for negative feedback", async () => {
  const response = await fetchFromBuiltApp(new Request("http://localhost/api/feedback", {
    method: "POST",
    headers: { "content-type": "application/json" },
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
