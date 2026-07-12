from __future__ import annotations

from fastapi.testclient import TestClient

from salient_tutor import web
from salient_tutor.daemon import TutorDaemon


def test_session_api_is_resumable_and_server_owned(tmp_path, monkeypatch) -> None:
    daemon = TutorDaemon(work_root=tmp_path / "work")
    monkeypatch.setattr(web, "daemon", daemon)
    client = TestClient(web.app)
    created = client.post("/api/sessions", json={"skill_id": "custom:photosynthesis"})
    assert created.status_code == 200
    session = created.json()
    session_id = session["session_id"]
    assert (tmp_path / "work" / "lessons.db").exists()

    assert (
        client.post(f"/api/sessions/{session_id}/advance", json={"expected_version": 0}).status_code
        == 200
    )
    assert (
        client.post(f"/api/sessions/{session_id}/advance", json={"expected_version": 1}).status_code
        == 200
    )
    item_response = client.post(f"/api/sessions/{session_id}/items", json={"expected_version": 2})
    item = item_response.json()["item"]
    assert "reference_answer" not in item_response.text

    attempt = client.post(
        f"/api/sessions/{session_id}/attempts",
        json={
            "item_id": item["item_id"],
            "item_version": 1,
            "response": "custom:photosynthesis",
            "idempotency_key": "attempt-1",
        },
    )
    assert attempt.status_code == 200
    assert attempt.json()["attempt"]["scoring_status"] == "pass"
    duplicate = client.post(
        f"/api/sessions/{session_id}/attempts",
        json={
            "item_id": item["item_id"],
            "item_version": 1,
            "response": "tampered",
            "idempotency_key": "attempt-1",
        },
    )
    assert duplicate.json()["attempt"]["attempt_id"] == attempt.json()["attempt"]["attempt_id"]

    conflict = client.post(f"/api/sessions/{session_id}/pause", json={"expected_version": 2})
    assert conflict.status_code == 409
