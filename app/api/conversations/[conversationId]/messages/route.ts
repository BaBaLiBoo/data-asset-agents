import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export const runtime = "edge";

export async function GET(request: Request) {
  const segments = new URL(request.url).pathname.split("/").filter(Boolean);
  const messagesIndex = segments.lastIndexOf("messages");
  const conversationId = decodeURIComponent(
    messagesIndex > 0 ? segments[messagesIndex - 1] : "",
  );
  if (!conversationId) {
    return Response.json({ message: "缺少会话编号。" }, { status: 400 });
  }

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
    const upstream = await fetch(
      `${supervisorUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        headers: { "X-User-Id": supervisorUserId(request) },
        cache: "no-store",
      },
    );
    const payload = await upstream.json() as unknown;
    if (!upstream.ok) {
      return Response.json(
        { message: "会话消息查询失败。" },
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
