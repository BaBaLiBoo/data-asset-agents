import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export async function GET(request: Request) {
  const taskId = decodeURIComponent(
    new URL(request.url).pathname.split("/").filter(Boolean).at(-1) || "",
  );
  if (!taskId) {
    return Response.json({ message: "缺少任务编号。" }, { status: 400 });
  }

  const apiMode = (
    request.headers.get("x-agent-api-mode")
    || process.env.API_MODE
    || "real"
  ).toLowerCase();
  if (apiMode !== "real") {
    return Response.json({ message: "Mock模式没有服务端任务记录。" }, { status: 404 });
  }

  const supervisorUrl = supervisorApiUrl();
  try {
    const upstream = await fetch(
      `${supervisorUrl}/api/v1/tasks/${encodeURIComponent(taskId)}`,
      {
        headers: {
          "X-User-Id": supervisorUserId(request),
        },
      },
    );
    const payload = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      const detail =
        typeof payload.detail === "string"
          ? payload.detail
          : "任务查询失败。";
      return Response.json({ message: detail }, { status: upstream.status });
    }
    return Response.json(payload);
  } catch {
    return Response.json({ message: "总控服务暂时不可用。" }, { status: 503 });
  }
}
