import {
  createMockResponse,
  detectScene,
  getSteps,
  type ChatRequest,
} from "@/lib/mock-agent";
import {
  normalizeSupervisorResponse,
} from "@/lib/supervisor-adapter";
import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export const runtime = "edge";

const encoder = new TextEncoder();

function event(name: string, data: unknown) {
  return encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
}

function wait(milliseconds: number) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function streamResponse(stream: ReadableStream<Uint8Array>) {
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}

async function upstreamError(response: Response) {
  try {
    const payload = await response.json() as {
      detail?: string | Array<{ loc?: unknown[]; msg?: string }>;
      message?: string;
    };
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) {
      const details = payload.detail
        .map((item) => {
          const field = Array.isArray(item.loc) ? item.loc.join(".") : "请求";
          return item.msg ? `${field}: ${item.msg}` : "";
        })
        .filter(Boolean);
      if (details.length) return details.join("；");
    }
    if (payload.message) return payload.message;
  } catch {
    // Upstream may return a non-JSON proxy error page.
  }
  return `总控返回HTTP ${response.status}`;
}

function mockStream(body: ChatRequest) {
  const response = createMockResponse(body);
  const scene = detectScene(body.message, body.preferredScene);
  const steps = getSteps(scene);
  return streamResponse(new ReadableStream({
    async start(controller) {
      controller.enqueue(event("task", {
        taskId: response.taskId,
        resultId: response.resultId,
        scene: response.scene,
        status: "PROCESSING",
        version: response.version,
      }));
      for (const step of steps) {
        controller.enqueue(event("step", { ...step, status: "RUNNING" }));
        await wait(80);
        controller.enqueue(event("step", step));
      }
      controller.enqueue(event("result", response));
      controller.enqueue(event("done", { status: response.status }));
      controller.close();
    },
  }));
}

function supervisorStream(request: Request, body: ChatRequest) {
  const supervisorUrl = supervisorApiUrl();
  return streamResponse(new ReadableStream({
    async start(controller) {
      try {
        const upstream = await fetch(`${supervisorUrl}/api/v1/chat/stream`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-User-Id": supervisorUserId(request),
          },
          body: JSON.stringify({
            conversationId: body.conversationId,
            message: body.message,
            preferredScene: body.preferredScene || "AUTO",
            context: body.context || {},
            correctionContext: body.correctionContext || [],
          }),
        });
        if (!upstream.ok || !upstream.body) {
          throw new Error(await upstreamError(upstream));
        }
        const reader = upstream.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split("\n\n");
          buffer = blocks.pop() ?? "";
          for (const block of blocks) {
            let eventName = "message";
            let data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) eventName = line.slice(6).trim();
              if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (!data) continue;
            const parsed = JSON.parse(data) as unknown;
            if (eventName === "result") {
              controller.enqueue(event("result", normalizeSupervisorResponse(parsed, body)));
            } else {
              controller.enqueue(event(eventName, parsed));
            }
          }
        }
      } catch (error) {
        controller.enqueue(event("error", {
          code: "SUPERVISOR_UNAVAILABLE",
          message: error instanceof Error ? error.message : "总控服务调用失败",
          retryable: true,
        }));
        controller.enqueue(event("done", { status: "FAILED" }));
      } finally {
        controller.close();
      }
    },
  }));
}

function realStream(request: Request, body: ChatRequest) {
  return supervisorStream(request, body);
}

export async function POST(request: Request) {
  let body: ChatRequest;
  try {
    body = await request.json() as ChatRequest;
  } catch {
    return Response.json({ message: "请求格式不正确。" }, { status: 400 });
  }
  if (!body.message?.trim() || !body.conversationId) {
    return Response.json({ message: "缺少会话编号或用户需求。" }, { status: 400 });
  }

  const apiMode = (request.headers.get("x-agent-api-mode") || process.env.API_MODE || "real").toLowerCase();
  return apiMode === "real" ? realStream(request, body) : mockStream(body);
}
