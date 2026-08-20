import { supervisorUserId } from "@/lib/server-user";
import { supervisorApiUrl } from "@/lib/backend-config";

interface FeedbackRequest {
  conversationId: string;
  taskId: string;
  resultId: string;
  scene: string;
  decision: "ACCEPT" | "EDIT_AND_ACCEPT" | "REJECT";
  rating?: number;
  reasonCodes: string[];
  comment?: string;
  editedContent?: string;
  retry: boolean;
}

function validateFeedback(body: FeedbackRequest): string | null {
  if (!body.conversationId || !body.taskId || !body.resultId || !body.decision) {
    return "缺少反馈关联信息。";
  }
  if (body.retry && body.decision !== "REJECT") {
    return "只有不满意反馈可以触发重新生成。";
  }
  if (body.decision === "REJECT" && (!body.reasonCodes?.length || !body.comment?.trim())) {
    return "不满意反馈必须填写原因和说明。";
  }
  if (
    body.decision === "EDIT_AND_ACCEPT"
    && !body.editedContent?.trim()
    && !body.comment?.trim()
  ) {
    return "修改后采纳必须提供修改内容或说明。";
  }
  return null;
}

function mockFeedback(body: FeedbackRequest) {
  return Response.json({
    feedbackId: `feedback_${Date.now()}`,
    accepted: true,
    retryTriggered: body.decision === "REJECT" && body.retry,
    nextResultVersion:
      body.decision === "REJECT" && body.retry ? 2 : null,
    taskStatus:
      body.decision === "ACCEPT" || body.decision === "EDIT_AND_ACCEPT"
        ? "SUCCESS"
        : "REVIEW_REQUIRED",
    result: null,
    storedAt: new Date().toISOString(),
    mock: true,
  });
}

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as FeedbackRequest;
    const validationMessage = validateFeedback(body);
    if (validationMessage) {
      return Response.json({ message: validationMessage }, { status: 400 });
    }

    const apiMode = (
      request.headers.get("x-agent-api-mode")
      || process.env.API_MODE
      || "real"
    ).toLowerCase();
    if (apiMode !== "real") return mockFeedback(body);

    const supervisorUrl = supervisorApiUrl();
    const upstream = await fetch(`${supervisorUrl}/api/v1/feedback`, {
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
        typeof payload.detail === "string"
          ? payload.detail
          : "反馈提交失败。";
      return Response.json({ message: detail }, { status: upstream.status });
    }
    return Response.json(payload);
  } catch {
    return Response.json({ message: "反馈提交失败。" }, { status: 400 });
  }
}
