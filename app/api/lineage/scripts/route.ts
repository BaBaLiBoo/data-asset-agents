import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export async function POST(request: Request) {
  const supervisorUrl = supervisorApiUrl();

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ message: "请求格式不正确。" }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${supervisorUrl}/api/v1/lineage/scripts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-Id": supervisorUserId(request),
      },
      body: JSON.stringify(body),
    });
    const payload = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      const detail =
        typeof payload.detail === "string" ? payload.detail : "脚本上传失败。";
      return Response.json({ message: detail }, { status: upstream.status });
    }
    return Response.json(payload);
  } catch {
    return Response.json({ message: "总控服务暂时不可用。" }, { status: 503 });
  }
}
