import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

export async function GET(request: Request) {
  const jobId = decodeURIComponent(
    new URL(request.url).pathname.split("/").filter(Boolean).at(-1) || "",
  );
  if (!jobId) {
    return Response.json({ message: "缺少任务编号。" }, { status: 400 });
  }

  const supervisorUrl = supervisorApiUrl();
  try {
    const upstream = await fetch(
      `${supervisorUrl}/api/v1/lineage/jobs/${encodeURIComponent(jobId)}`,
      { headers: { "X-User-Id": supervisorUserId(request) } },
    );
    const payload = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      const detail =
        typeof payload.detail === "string" ? payload.detail : "任务查询失败。";
      return Response.json({ message: detail }, { status: upstream.status });
    }
    return Response.json(payload);
  } catch {
    return Response.json({ message: "总控服务暂时不可用。" }, { status: 503 });
  }
}
