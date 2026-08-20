import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export const runtime = "edge";

export async function DELETE(request: Request) {
  const segments = new URL(request.url).pathname.split("/").filter(Boolean);
  const conversationId = decodeURIComponent(segments.at(-1) || "");
  if (!conversationId) {
    return Response.json({ message: "缺少会话编号。" }, { status: 400 });
  }

  const apiMode = (
    request.headers.get("x-agent-api-mode")
    || process.env.API_MODE
    || "real"
  ).toLowerCase();
  if (apiMode !== "real") {
    return Response.json({
      conversationId,
      deleted: true,
      deletedAt: new Date().toISOString(),
    });
  }

  const supervisorUrl = supervisorApiUrl();
  try {
    const upstream = await fetch(
      `${supervisorUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}`,
      {
        method: "DELETE",
        headers: { "X-User-Id": supervisorUserId(request) },
        cache: "no-store",
      },
    );
    const payload = await upstream.json() as unknown;
    if (!upstream.ok) {
      return Response.json(
        { message: "删除会话失败。" },
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
