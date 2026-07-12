from __future__ import annotations

import pytest

from salient_tutor.lesson import LessonController
from salient_tutor.lesson_store import (
    LessonStore,
    SessionConflict,
    canonical_curriculum_skill,
    canonical_custom_skill,
    canonical_study_skill,
)


def test_canonical_skill_ids_are_stable() -> None:
    assert (
        canonical_curriculum_skill("blue", "m1", "t1") == "curriculum:track:blue:module:m1:topic:t1"
    )
    assert canonical_study_skill("p1", "s2") == "study:p1:sec:s2"
    assert canonical_custom_skill("DNS Lookup") == "custom:dns-lookup"


def test_session_reopen_preserves_pending_server_owned_item(tmp_path) -> None:
    store = LessonStore(tmp_path / "work" / "lessons.db")
    controller = LessonController(store)
    session = controller.create_session("custom:photosynthesis")
    controller.advance(session["session_id"], expected_version=0)
    controller.advance(session["session_id"], expected_version=1)
    issued = controller.issue_item(session["session_id"], expected_version=2)
    reopened = LessonController(LessonStore(tmp_path / "work" / "lessons.db")).get_session(
        session["session_id"]
    )
    assert reopened["active_item_id"] == issued["item"]["item_id"]
    assert reopened["phase"] == "awaiting_attempt"
    assert "reference_answer" not in issued["item"]


def test_stale_session_version_is_rejected(tmp_path) -> None:
    controller = LessonController(LessonStore(tmp_path / "lessons.db"))
    session = controller.create_session("custom:x")
    controller.pause(session["session_id"], expected_version=0)
    with pytest.raises(SessionConflict):
        controller.resume(session["session_id"], expected_version=0)


def test_duplicate_attempt_is_idempotent_and_chat_cannot_advance(tmp_path) -> None:
    controller = LessonController(LessonStore(tmp_path / "lessons.db"))
    session = controller.create_session("custom:x")
    controller.advance(session["session_id"])
    controller.advance(session["session_id"])
    issued = controller.issue_item(session["session_id"])
    item_id = issued["item"]["item_id"]
    first = controller.record_attempt(session["session_id"], item_id, 1, "custom:x", "same-key")
    second = controller.record_attempt(
        session["session_id"], item_id, 1, "wrong replacement", "same-key"
    )
    assert first["attempt"]["attempt_id"] == second["attempt"]["attempt_id"]
    assert second["session"]["phase"] == "anchor"
    assert len(controller.store.events(session["session_id"])) == 7


def test_review_application_is_idempotent(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons.db")
    store.save_review_application(
        {
            "idempotency_key": "review-1",
            "attempt_id": "attempt-1",
            "srs_topic": "custom:x",
            "requested_grade": "good",
            "application_status": "applied",
            "scheduler_result": {"mastery": 1},
        }
    )
    duplicate = store.save_review_application(
        {
            "idempotency_key": "review-1",
            "attempt_id": "tampered",
            "srs_topic": "other",
            "requested_grade": "again",
            "application_status": "failed",
            "scheduler_result": {},
        }
    )
    assert duplicate["attempt_id"] == "attempt-1"
    assert duplicate["scheduler_result"] == {"mastery": 1}


def test_failed_attempt_can_be_retried_and_passed(tmp_path) -> None:
    # Regression: the attempts UNIQUE(session,item,version) constraint used to
    # return the stale failed row on retry, trapping the learner in an unwinnable
    # drill loop. A correct resubmit (new key) after a fail must record a fresh
    # passing attempt and advance to anchor.
    controller = LessonController(LessonStore(tmp_path / "lessons.db"))
    session = controller.create_session("custom:x")
    controller.advance(session["session_id"])
    controller.advance(session["session_id"])
    item_id = controller.issue_item(session["session_id"])["item"]["item_id"]
    failed = controller.record_attempt(session["session_id"], item_id, 1, "wrong", "attempt-1")
    assert failed["attempt"]["scoring_status"] == "fail"
    assert failed["session"]["phase"] == "drill"
    controller.advance(session["session_id"])  # drill -> awaiting_attempt, same item
    passed = controller.record_attempt(session["session_id"], item_id, 1, "custom:x", "attempt-2")
    assert passed["attempt"]["scoring_status"] == "pass"
    assert passed["attempt"]["attempt_id"] != failed["attempt"]["attempt_id"]
    assert passed["session"]["phase"] == "anchor"


def test_free_text_scores_with_confident_judge_result(tmp_path) -> None:
    # A confident judge verdict must actually score a free_text item (the HTTP
    # path can now forward judge_result) — not be stuck at "unscored".
    controller = LessonController(LessonStore(tmp_path / "lessons.db"))
    session = controller.create_session("custom:x")
    controller.advance(session["session_id"])
    controller.advance(session["session_id"])
    issued = controller.issue_item(
        session["session_id"],
        item={
            "item_id": "free-1",
            "version": 1,
            "skill_id": "custom:x",
            "kind": "check",
            "bloom": "apply",
            "response_type": "free_text",
            "prompt": "Explain x.",
            "options": [],
            "rubric": {"criteria": [{"id": "core", "description": "explains x", "required": True}]},
            "reference_evidence": "evidence",
            "reference_answer": "x",
            "provenance": [],
            "generator_version": "test",
            "scorer_version": "judge-v1",
        },
    )
    result = controller.record_attempt(
        session["session_id"],
        issued["item"]["item_id"],
        1,
        "a full correct explanation",
        "judge-ok",
        judge_result={
            "status": "pass",
            "confidence": 0.9,
            "criteria": [{"id": "core", "score": 1.0}],
        },
    )
    assert result["attempt"]["scoring_status"] == "pass"
    assert result["session"]["phase"] == "anchor"


def test_free_text_requires_confident_judge_result(tmp_path) -> None:
    controller = LessonController(LessonStore(tmp_path / "lessons.db"))
    session = controller.create_session("custom:x")
    controller.advance(session["session_id"])
    controller.advance(session["session_id"])
    issued = controller.issue_item(
        session["session_id"],
        item={
            "item_id": "free-1",
            "version": 1,
            "skill_id": "custom:x",
            "kind": "check",
            "bloom": "apply",
            "response_type": "free_text",
            "prompt": "Explain x.",
            "options": [],
            "rubric": {"criteria": [{"id": "core", "description": "explains x", "required": True}]},
            "reference_evidence": "evidence",
            "reference_answer": "x",
            "provenance": [],
            "generator_version": "test",
            "scorer_version": "judge-v1",
        },
    )
    result = controller.record_attempt(
        session["session_id"], issued["item"]["item_id"], 1, "x", "judge-1"
    )
    assert result["attempt"]["scoring_status"] == "unscored"
    assert result["session"]["mastery_stage"] == "unstarted"


def test_card_versions_and_retirement_preserve_history(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons.db")
    store.save_card(
        {
            "card_id": "card-1",
            "version": 1,
            "skill_id": "custom:x",
            "source_item_id": "item-1",
            "question": "q1",
            "answer": "a1",
            "card_type": "basic",
            "provenance": [],
            "srs_topic": "custom:x",
            "status": "active",
        }
    )
    store.update_card_status("card-1", 1, "retired")
    store.save_card(
        {
            "card_id": "card-1",
            "version": 2,
            "skill_id": "custom:x",
            "source_item_id": "item-1",
            "question": "q2",
            "answer": "a2",
            "card_type": "basic",
            "provenance": [],
            "srs_topic": "custom:x",
            "status": "active",
        }
    )
    cards = store.list_cards(skill_id="custom:x")
    assert [card["version"] for card in cards] == [1, 2]
    assert cards[0]["status"] == "retired"
    assert store.get_card("card-1")["version"] == 2


def test_agent_runs_and_analytics_are_persisted(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons.db")
    session = LessonController(store).create_session("custom:x")
    run = store.start_agent_run(session["session_id"], "judge", "judge-v1", "assessment")
    finished = store.finish_agent_run(run["run_id"], "completed", output="pass", duration_ms=12)
    assert finished["status"] == "completed"
    assert finished["output_hash"]
    analytics = store.analytics()
    assert analytics["agent_runs"]["completed"] == 1
    assert analytics["sessions"]["total"] == 1


def test_migration_report_is_read_only(tmp_path) -> None:
    from salient_tutor.lesson_store import SCHEMA_VERSION

    store = LessonStore(tmp_path / "lessons.db")
    before = store.schema_version()
    report = store.migration_report()
    assert report["schema_version"] == before == SCHEMA_VERSION
    assert report["database"] == str(tmp_path / "lessons.db")


def test_v1_to_v2_migration_drops_attempts_unique_and_preserves_rows(tmp_path) -> None:
    # A legacy v1 db carried UNIQUE(session_id,item_id,item_version) on attempts.
    # Opening it must migrate to v2 (constraint gone) while preserving history.
    import sqlite3

    path = tmp_path / "lessons.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT, session_kind TEXT,
            skill_id TEXT, srs_topic TEXT, track_id TEXT, module_id TEXT, topic_id TEXT,
            study_project_id TEXT, section_id TEXT, phase TEXT, phase_version INTEGER,
            active_item_id TEXT, mastery_stage TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, session_id TEXT, item_id TEXT,
            item_version INTEGER, response TEXT, hints_used INTEGER, score_by_criterion TEXT,
            scoring_status TEXT, scorer_version TEXT, feedback TEXT, next_action TEXT,
            created_at REAL, UNIQUE(session_id, item_id, item_version));
        INSERT INTO sessions VALUES ('s1','active','lesson','custom:x','custom:x',NULL,NULL,NULL,
            NULL,NULL,'drill',3,'q1','unstarted',0,0);
        INSERT INTO attempts VALUES ('a1','s1','q1',1,'wrong',0,'{}','fail','v1','','remediate',0);
        PRAGMA user_version = 1;
    """)
    con.commit()
    con.close()

    store = LessonStore(path)
    assert store.schema_version() == 2
    assert store.get_attempt("a1")["scoring_status"] == "fail"  # legacy row preserved
    # The dropped constraint now allows a second attempt for the same tuple.
    store.save_attempt(
        {
            "attempt_id": "a2",
            "session_id": "s1",
            "item_id": "q1",
            "item_version": 1,
            "response": "right",
            "hints_used": 0,
            "score_by_criterion": {},
            "scoring_status": "pass",
            "scorer_version": "deterministic-v1",
            "feedback": "",
            "next_action": "advance",
        }
    )
    assert store.get_attempt("a2")["scoring_status"] == "pass"
