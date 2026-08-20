"""反馈保存、任务完成和同任务结果重生成。"""

from __future__ import annotations

from ..models.api import (
    AgentScene,
    ChatRequest,
    FeedbackDecision,
    FeedbackRequest,
    FeedbackResponse,
    PreferredScene,
)
from ..repositories import TaskRepository
from .supervisor import SupervisorService


class FeedbackService:
    def __init__(
        self,
        repository: TaskRepository,
        supervisor: SupervisorService,
    ) -> None:
        self._repository = repository
        self._supervisor = supervisor

    async def submit(
        self,
        request: FeedbackRequest,
    ) -> FeedbackResponse:
        snapshot = self._repository.get_task(request.task_id)
        feedback_id, stored_at = self._repository.record_feedback(request)

        if request.decision in (
            FeedbackDecision.ACCEPT,
            FeedbackDecision.EDIT_AND_ACCEPT,
        ):
            summary = (
                "用户已修改并采纳当前结果"
                if request.decision is FeedbackDecision.EDIT_AND_ACCEPT
                else "用户已确认并采纳当前结果"
            )
            result = self._repository.accept_task(
                request.task_id,
                summary=summary,
                edited_content=request.edited_content,
            )
            return FeedbackResponse(
                feedback_id=feedback_id,
                accepted=True,
                retry_triggered=False,
                next_result_version=None,
                task_status=result.status,
                result=result,
                stored_at=stored_at,
            )

        if not request.retry:
            current = self._repository.get_task(request.task_id).effective_result
            return FeedbackResponse(
                feedback_id=feedback_id,
                accepted=True,
                retry_triggered=False,
                next_result_version=None,
                task_status=current.status,
                result=current,
                stored_at=stored_at,
            )

        previous = snapshot.raw_result
        next_version = previous.version + 1
        correction_context = [
            {
                "previousResultId": previous.result_id,
                "previousVersion": previous.version,
                "decision": FeedbackDecision.REJECT.value,
                "reasonCodes": request.reason_codes,
                "comment": request.comment or "",
                "editedContent": request.edited_content,
                "previousResult": previous.result or {},
                "retryIndex": previous.version,
                "feedbackHistory": [feedback_id],
            }
        ]
        retry_request = ChatRequest(
            conversation_id=snapshot.original_request.conversation_id,
            message=self._retry_message(snapshot.original_request, request),
            preferred_scene=self._preferred_scene(previous.scene),
            context=snapshot.original_request.context,
        )
        result = await self._supervisor.handle(
            retry_request,
            user_id=snapshot.user_id,
            task_id=request.task_id,
            result_version=next_version,
            correction_context=correction_context,
        )
        self._repository.save_retry_result(result)
        return FeedbackResponse(
            feedback_id=feedback_id,
            accepted=True,
            retry_triggered=True,
            next_result_version=next_version,
            task_status=result.status,
            result=result,
            stored_at=stored_at,
        )

    @staticmethod
    def _preferred_scene(scene: AgentScene) -> PreferredScene:
        if scene is AgentScene.ASSET_DUPLICATE:
            return PreferredScene.ASSET_DUPLICATE
        if scene is AgentScene.SQL_GENERATION:
            return PreferredScene.SQL_GENERATION
        raise ValueError("需求澄清结果不能提交专业反馈")

    @staticmethod
    def _retry_message(
        original_request: ChatRequest,
        feedback: FeedbackRequest,
    ) -> str:
        reason_codes = "、".join(feedback.reason_codes)
        return (
            f"{original_request.message}\n"
            f"用户对上一版结果不满意，原因码：{reason_codes}。"
            f"修正要求：{feedback.comment or '请根据反馈重新检查'}"
        )
