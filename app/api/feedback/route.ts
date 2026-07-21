interface FeedbackRequest {
  conversationId: string;
  taskId: string;
  resultId: string;
  scene: string;
  decision: "ACCEPT" | "EDIT_AND_ACCEPT" | "REJECT";
  reasonCode?: string;
  comment?: string;
  editedSql?: string;
  retry: boolean;
}

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as FeedbackRequest;
    if (!body.conversationId || !body.taskId || !body.resultId || !body.decision) {
      return Response.json({ message: "缺少反馈关联信息。" }, { status: 400 });
    }
    if (body.decision !== "ACCEPT" && !body.comment?.trim()) {
      return Response.json({ message: "请填写修改或拒绝原因。" }, { status: 400 });
    }

    return Response.json({
      feedbackId: `FEEDBACK_${Date.now()}`,
      accepted: true,
      retry: body.retry,
      storedAt: new Date().toISOString(),
      mock: true,
    });
  } catch {
    return Response.json({ message: "反馈提交失败。" }, { status: 400 });
  }
}
