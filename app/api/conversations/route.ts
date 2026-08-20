import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export const runtime = "edge";

export async function GET(request: Request) {
  const apiMode = (
    request.headers.get("x-agent-api-mode")
    || process.env.API_MODE
    || "real"
  ).toLowerCase();
  if (apiMode !== "real") {
    return Response.json(
      { message: "Mock模式使用浏览器本地历史。" },
      { status: 404 },
    );
  }

  const supervisorUrl = supervisorApiUrl();
  try {
    const upstream = await fetch(`${supervisorUrl}/api/v1/conversations`, {
      headers: { "X-User-Id": supervisorUserId(request) },
      cache: "no-store",
    });
    const payload = await upstream.json() as unknown;
    if (!upstream.ok) {
      return Response.json(
        { message: "会话历史查询失败。" },
        { status: upstream.status },
      );
    }
    return Response.json(payload, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json(
      { message: "总控服务暂时不可用。" },
      { status: 503 },
    );
  }
}

export async function DELETE(request: Request) {
  const apiMode = (
    request.headers.get("x-agent-api-mode")
    || process.env.API_MODE
    || "real"
  ).toLowerCase();
  if (apiMode !== "real") {
    return Response.json({
      deletedCount: 0,
      deletedAt: new Date().toISOString(),
    });
  }

  const supervisorUrl = supervisorApiUrl();
  try {
    const upstream = await fetch(`${supervisorUrl}/api/v1/conversations`, {
      method: "DELETE",
      headers: { "X-User-Id": supervisorUserId(request) },
      cache: "no-store",
    });
    const payload = await upstream.json() as unknown;
    if (!upstream.ok) {
      return Response.json(
        { message: "清空会话历史失败。" },
        { status: upstream.status },
      );
    }
    return Response.json(payload, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json(
      { message: "总控服务暂时不可用。" },
      { status: 503 },
    );
  }
}
