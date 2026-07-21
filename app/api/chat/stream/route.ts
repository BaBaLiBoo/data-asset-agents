import { createMockResponse, detectScene, getSteps, type ChatRequest } from "@/lib/mock-agent";

export const runtime = "edge";

const encoder = new TextEncoder();

function event(name: string, data: unknown) {
  return encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
}

function wait(milliseconds: number) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as ChatRequest;
    if (!body.message?.trim() || !body.conversationId) {
      return Response.json({ message: "缺少会话编号或用户需求。" }, { status: 400 });
    }

    const response = createMockResponse(body);
    const scene = detectScene(body.message, body.preferredScene);
    const steps = getSteps(scene);

    const stream = new ReadableStream({
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
          await wait(260);
          controller.enqueue(event("step", step));
          await wait(110);
        }

        controller.enqueue(event("result", response));
        controller.enqueue(event("done", { status: response.status }));
        controller.close();
      },
    });

    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  } catch {
    return Response.json({ message: "请求格式不正确。" }, { status: 400 });
  }
}
