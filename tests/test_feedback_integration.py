"""任务结果、反馈决策和同任务版本重生成集成测试。"""

from fastapi.testclient import TestClient

from asset_supervisor.api import (
    create_app,
    get_supervisor,
    get_task_repository,
)
from asset_supervisor.repositories import SqliteTaskRepository
from tests.test_http_integration import build_integrated_service


def build_feedback_client() -> tuple[TestClient, SqliteTaskRepository]:
    supervisor = build_integrated_service()
    repository = SqliteTaskRepository(":memory:")
    app = create_app()
    app.dependency_overrides[get_supervisor] = lambda: supervisor
    app.dependency_overrides[get_task_repository] = lambda: repository
    return TestClient(app), repository


def create_sql_task(
    client: TestClient,
    conversation_id: str,
    user_id: str = "u_feedback",
) -> dict:
    response = client.post(
        "/api/v1/chat",
        json={
            "conversationId": conversation_id,
            "message": "统计最近90天各分行新增客户数",
            "preferredScene": "SQL_GENERATION",
            "context": {
                "sqlDialect": "HIVE_SQL",
                "schemaScope": ["card_dm"],
            },
        },
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "REVIEW_REQUIRED"
    assert payload["reflection"]["status"] == "UNAVAILABLE"
    return payload


def feedback_payload(
    result: dict,
    *,
    decision: str,
    reason_codes: list[str] | None = None,
    comment: str | None = None,
    edited_content: str | None = None,
    retry: bool = False,
) -> dict:
    return {
        "conversationId": result["conversationId"],
        "taskId": result["taskId"],
        "resultId": result["resultId"],
        "scene": result["scene"],
        "decision": decision,
        "reasonCodes": reason_codes or [],
        "comment": comment,
        "editedContent": edited_content,
        "retry": retry,
    }


def test_accept_completes_task_but_preserves_raw_result() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_accept")

    response = client.post(
        "/api/v1/feedback",
        json=feedback_payload(original, decision="ACCEPT"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["retryTriggered"] is False
    assert payload["taskStatus"] == "SUCCESS"
    assert payload["result"]["status"] == "SUCCESS"
    assert payload["result"]["taskId"] == original["taskId"]
    assert payload["result"]["executionPlan"]["status"] == "COMPLETED"
    assert payload["result"]["reflection"] == original["reflection"]
    assert payload["result"]["executionPlan"]["steps"][-1]["code"] == "COMPLETE"
    assert (
        payload["result"]["executionPlan"]["constraints"][
            "professionalToolCalls"
        ]
        == 1
    )

    current = client.get(f"/api/v1/tasks/{original['taskId']}").json()
    raw_results = client.get(
        f"/api/v1/tasks/{original['taskId']}/results"
    ).json()
    assert current["status"] == "SUCCESS"
    assert current["executionPlan"]["status"] == "COMPLETED"
    assert raw_results[0]["status"] == "REVIEW_REQUIRED"
    assert raw_results[0]["executionPlan"]["status"] == "REVIEW"
    assert raw_results[0]["reflection"] == original["reflection"]
    repository.close()


def test_edit_and_accept_returns_user_edited_sql_without_retry() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_edit_accept")
    edited_sql = "SELECT branch_id, COUNT(DISTINCT customer_id) FROM edited_table"

    response = client.post(
        "/api/v1/feedback",
        json=feedback_payload(
            original,
            decision="EDIT_AND_ACCEPT",
            reason_codes=["USER_EDIT"],
            comment="采用我修改后的SQL",
            edited_content=edited_sql,
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["retryTriggered"] is False
    assert payload["taskStatus"] == "SUCCESS"
    assert payload["result"]["version"] == 1
    assert payload["result"]["result"]["sql"] == edited_sql
    repository.close()


def test_reject_retry_creates_v2_under_the_same_task() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_reject_retry")

    response = client.post(
        "/api/v1/feedback",
        json=feedback_payload(
            original,
            decision="REJECT",
            reason_codes=[
                "TIME_RANGE_MISMATCH",
                "TABLE_FIELD_MISMATCH",
            ],
            comment=(
                "按智能检查意见重试：时间范围应为180天，"
                "并确认客户主题表和日期字段"
            ),
            retry=True,
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    regenerated = payload["result"]
    assert payload["retryTriggered"] is True
    assert payload["nextResultVersion"] == 2
    assert regenerated["taskId"] == original["taskId"]
    assert regenerated["resultId"] != original["resultId"]
    assert regenerated["version"] == 2
    assert regenerated["status"] == "REVIEW_REQUIRED"
    assert regenerated["reflection"]["status"] == "UNAVAILABLE"
    assert (
        regenerated["executionPlan"]["constraints"]["professionalToolCalls"]
        == 1
    )
    assert regenerated["executionPlan"]["selectedTool"] == "generate_sql"

    results = client.get(
        f"/api/v1/tasks/{original['taskId']}/results"
    ).json()
    assert [item["version"] for item in results] == [1, 2]
    repository.close()


def test_duplicate_or_stale_feedback_returns_conflict() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_conflict")
    payload = feedback_payload(original, decision="ACCEPT")

    assert client.post("/api/v1/feedback", json=payload).status_code == 200
    response = client.post("/api/v1/feedback", json=payload)

    assert response.status_code == 409
    repository.close()


def test_conversation_history_is_persisted_and_user_isolated() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_history")

    conversations = client.get(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_feedback"},
    )
    assert conversations.status_code == 200
    assert conversations.json()[0]["conversationId"] == "conv_history"
    assert conversations.json()[0]["messageCount"] == 2

    messages = client.get(
        "/api/v1/conversations/conv_history/messages",
        headers={"X-User-Id": "u_feedback"},
    )
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()] == [
        "USER",
        "ASSISTANT",
    ]
    assert (
        messages.json()[1]["metadata"]["response"]["resultId"]
        == original["resultId"]
    )

    other_user = client.get(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_other"},
    )
    assert other_user.json() == []
    hidden_messages = client.get(
        "/api/v1/conversations/conv_history/messages",
        headers={"X-User-Id": "u_other"},
    )
    assert hidden_messages.status_code == 404
    repository.close()


def test_feedback_is_added_to_conversation_history() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_feedback_history")

    feedback = client.post(
        "/api/v1/feedback",
        json=feedback_payload(original, decision="ACCEPT"),
    )
    assert feedback.status_code == 200

    messages = client.get(
        "/api/v1/conversations/conv_feedback_history/messages",
        headers={"X-User-Id": "u_feedback"},
    ).json()
    assert [item["role"] for item in messages] == [
        "USER",
        "ASSISTANT",
        "USER",
    ]
    assert messages[1]["metadata"]["response"]["status"] == "SUCCESS"
    assert messages[2]["metadata"]["feedback"]["decision"] == "ACCEPT"
    repository.close()


def test_delete_conversation_hides_history_but_preserves_task_audit() -> None:
    client, repository = build_feedback_client()
    original = create_sql_task(client, "conv_delete")

    hidden_from_other_user = client.delete(
        "/api/v1/conversations/conv_delete",
        headers={"X-User-Id": "u_other"},
    )
    assert hidden_from_other_user.status_code == 404

    response = client.delete(
        "/api/v1/conversations/conv_delete",
        headers={"X-User-Id": "u_feedback"},
    )
    assert response.status_code == 200
    assert response.json()["conversationId"] == "conv_delete"
    assert response.json()["deleted"] is True
    assert response.json()["deletedAt"]

    conversations = client.get(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_feedback"},
    )
    assert conversations.json() == []
    messages = client.get(
        "/api/v1/conversations/conv_delete/messages",
        headers={"X-User-Id": "u_feedback"},
    )
    assert messages.status_code == 404

    task = client.get(f"/api/v1/tasks/{original['taskId']}")
    results = client.get(
        f"/api/v1/tasks/{original['taskId']}/results"
    )
    assert task.status_code == 200
    assert results.status_code == 200
    assert results.json()[0]["resultId"] == original["resultId"]

    second_delete = client.delete(
        "/api/v1/conversations/conv_delete",
        headers={"X-User-Id": "u_feedback"},
    )
    assert second_delete.status_code == 404
    repository.close()


def test_clear_history_only_hides_current_users_conversations() -> None:
    client, repository = build_feedback_client()
    create_sql_task(client, "conv_clear_1")
    create_sql_task(client, "conv_clear_2")
    create_sql_task(client, "conv_other", user_id="u_other")

    response = client.delete(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_feedback"},
    )
    assert response.status_code == 200
    assert response.json()["deletedCount"] == 2
    assert response.json()["deletedAt"]

    current_user = client.get(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_feedback"},
    )
    other_user = client.get(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_other"},
    )
    assert current_user.json() == []
    assert [item["conversationId"] for item in other_user.json()] == [
        "conv_other"
    ]

    empty_clear = client.delete(
        "/api/v1/conversations",
        headers={"X-User-Id": "u_feedback"},
    )
    assert empty_clear.status_code == 200
    assert empty_clear.json()["deletedCount"] == 0
    repository.close()
