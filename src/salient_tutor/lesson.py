from __future__ import annotations

import re
import uuid
from typing import Final

from salient_tutor.lesson_store import LessonStore, LessonStoreError

PHASES: Final[tuple[str, ...]] = (
    "diagnose",
    "objective",
    "model",
    "awaiting_attempt",
    "anchor",
    "drill",
    "reflect",
    "cards",
    "elaborate",
)
_NEXT: Final[dict[str, str]] = {
    "diagnose": "objective",
    "objective": "model",
    "model": "awaiting_attempt",
    "anchor": "reflect",
    "drill": "awaiting_attempt",
    "reflect": "cards",
    "cards": "elaborate",
    "elaborate": "completed",
}
_GRADES: Final[frozenset[str]] = frozenset({"again", "hard", "good", "easy"})


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "topic"


class LessonController:
    def __init__(self, store: LessonStore) -> None:
        self.store = store

    def create_session(
        self,
        skill_id: str,
        *,
        srs_topic: str | None = None,
        session_kind: str = "lesson",
        **bindings: str | None,
    ) -> dict:
        if not skill_id.strip():
            raise LessonStoreError("skill_id is required")
        session_id = uuid.uuid4().hex
        return self.store.create_session(
            {
                "session_id": session_id,
                "status": "active",
                "session_kind": session_kind,
                "skill_id": skill_id,
                "srs_topic": srs_topic or skill_id,
                "phase": "diagnose",
                **bindings,
            },
            idempotency_key=f"create:{session_id}",
        )

    def get_session(self, session_id: str) -> dict:
        session = self.store.get_session(session_id)
        if session is None:
            raise LessonStoreError(f"unknown session: {session_id}")
        return session

    def current_session(self) -> dict | None:
        return self.store.current_session()

    def pause(self, session_id: str, expected_version: int | None = None) -> dict:
        return self.store.transition(
            session_id,
            self.get_session(session_id)["phase"],
            expected_version=expected_version,
            status="paused",
            event_type="paused",
        )

    def resume(self, session_id: str, expected_version: int | None = None) -> dict:
        return self.store.transition(
            session_id,
            self.get_session(session_id)["phase"],
            expected_version=expected_version,
            status="active",
            event_type="resumed",
        )

    def abandon(self, session_id: str, expected_version: int | None = None) -> dict:
        return self.store.transition(
            session_id,
            "abandoned",
            expected_version=expected_version,
            status="abandoned",
            event_type="abandoned",
        )

    def advance(self, session_id: str, expected_version: int | None = None) -> dict:
        session = self.get_session(session_id)
        phase = session["phase"]
        if phase == "awaiting_attempt":
            raise LessonStoreError("submit the active assessment item before advancing")
        if phase == "assessing":
            raise LessonStoreError("assessment is already being processed")
        next_phase = _NEXT.get(phase)
        if next_phase is None:
            raise LessonStoreError(f"phase cannot advance: {phase}")
        status = "completed" if next_phase == "completed" else "active"
        return self.store.transition(
            session_id,
            next_phase,
            expected_version=expected_version,
            status=status,
            event_type="completed" if status == "completed" else "phase_changed",
        )

    def issue_item(
        self, session_id: str, *, item: dict | None = None, expected_version: int | None = None
    ) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active":
            raise LessonStoreError("session is not active")
        if session["active_item_id"]:
            # Resolve the actual stored version rather than assuming 1 — an item
            # authored at version != 1 would otherwise read as missing and get
            # silently replaced by a fresh default item, orphaning the in-flight
            # assessment.
            existing = self.store.get_latest_item(session["active_item_id"])
            if existing:
                return {"item": self._learner_item(existing), "session": session}
        source = item or {
            "item_id": f"item-{uuid.uuid4().hex}",
            "version": 1,
            "skill_id": session["skill_id"],
            "kind": "retrieval" if session["session_kind"] == "delayed_retrieval" else "check",
            "bloom": "understand",
            "response_type": "cloze",
            "prompt": f"In your own words, what is the key idea behind {session['skill_id']}?",
            "options": [],
            "rubric": {
                "criteria": [
                    {"id": "core", "description": "states the core idea", "required": True}
                ]
            },
            "reference_evidence": "server-authored lesson objective",
            "reference_answer": session["skill_id"],
            "provenance": [],
            "generator_version": "controller-v1",
            "scorer_version": "deterministic-v1",
        }
        self._validate_item(source)
        self.store.save_item(source)
        snapshot = self.store.set_active_item(
            session_id, source["item_id"], expected_version=expected_version
        )
        return {"item": self._learner_item(source), "session": snapshot}

    def record_attempt(
        self,
        session_id: str,
        item_id: str,
        item_version: int,
        response: str,
        idempotency_key: str,
        *,
        hints_used: int = 0,
        judge_result: dict | None = None,
    ) -> dict:
        session = self.get_session(session_id)
        for event in self.store.events(session_id):
            if event.get("idempotency_key") == idempotency_key:
                attempt_id = event.get("payload", {}).get("attempt_id")
                if attempt_id:
                    attempt = self.store.get_attempt(attempt_id)
                    if attempt:
                        return {"attempt": attempt, "session": session}
        if session["phase"] != "awaiting_attempt" or session["active_item_id"] != item_id:
            raise LessonStoreError("the submitted item is not the active assessment")
        item = self.store.get_item(item_id, item_version)
        if item is None:
            raise LessonStoreError("unknown assessment item version")
        score = self._score(item, response, judge_result)
        attempt = self.store.save_attempt(
            {
                "attempt_id": uuid.uuid4().hex,
                "session_id": session_id,
                "item_id": item_id,
                "item_version": item_version,
                "response": response,
                "hints_used": hints_used,
                **score,
            }
        )
        self.store._event_for_controller(
            session_id, "attempt_recorded", {"attempt_id": attempt["attempt_id"]}, idempotency_key
        )
        passed = attempt["scoring_status"] == "pass"
        next_phase = "anchor" if passed else "drill"
        snapshot = self.store.transition(
            session_id,
            next_phase,
            event_type="assessment_scored",
            payload={"status": attempt["scoring_status"]},
        )
        stage = (
            "durable_mastery"
            if passed and session["session_kind"] == "delayed_retrieval"
            else (
                "provisional_mastery"
                if passed
                else "remediation_queued"
                if session["session_kind"] == "delayed_retrieval"
                else "unstarted"
            )
        )
        snapshot = self.store.set_mastery_stage(session_id, stage)
        return {"attempt": attempt, "session": snapshot}

    @staticmethod
    def _learner_item(item: dict) -> dict:
        return {
            key: value
            for key, value in item.items()
            if key not in {"reference_answer", "reference_evidence"}
        }

    @staticmethod
    def _validate_item(item: dict) -> None:
        required = {
            "item_id",
            "version",
            "skill_id",
            "kind",
            "bloom",
            "response_type",
            "prompt",
            "rubric",
            "reference_answer",
        }
        missing = required - item.keys()
        if missing:
            raise LessonStoreError(f"assessment item missing fields: {', '.join(sorted(missing))}")
        if item["response_type"] not in {"free_text", "multiple_choice", "cloze"}:
            raise LessonStoreError("unsupported assessment response type")
        if not isinstance(item["rubric"], dict) or not isinstance(
            item["rubric"].get("criteria", []), list
        ):
            raise LessonStoreError("assessment rubric must contain criteria")

    @staticmethod
    def _score(item: dict, response: str, judge_result: dict | None = None) -> dict:
        if item["response_type"] == "free_text":
            return LessonController._score_judge(judge_result)
        answer = str(item["reference_answer"]).strip().casefold()
        actual = response.strip().casefold()
        status = "pass" if actual and actual == answer else "fail"
        return {
            "score_by_criterion": {"core": 1.0 if status == "pass" else 0.0},
            "scoring_status": status,
            "scorer_version": "deterministic-v1",
            "feedback": "Correct retrieval."
            if status == "pass"
            else "Try the item again after reviewing the model.",
            "next_action": "advance" if status == "pass" else "remediate",
        }

    @staticmethod
    def _score_judge(result: dict | None) -> dict:
        if not isinstance(result, dict) or result.get("status") not in {
            "pass",
            "partial",
            "fail",
            "ambiguous",
            "unscored",
        }:
            return {
                "score_by_criterion": {},
                "scoring_status": "unscored",
                "scorer_version": "judge-v1",
                "feedback": "This response could not be scored yet.",
                "next_action": "retry",
            }
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < 0.80:
            status = "unscored"
        else:
            status = result["status"]
        criteria = result.get("criteria")
        if not isinstance(criteria, list):
            criteria = []
        scores = {
            str(row["id"]): float(row["score"])
            for row in criteria
            if isinstance(row, dict)
            and row.get("id")
            and isinstance(row.get("score"), (int, float))
        }
        return {
            "score_by_criterion": scores,
            "scoring_status": status,
            "scorer_version": "judge-v1",
            "feedback": str(result.get("feedback") or ""),
            "next_action": str(
                result.get("next_action") or ("advance" if status == "pass" else "retry")
            ),
        }

    @staticmethod
    def grade_for_attempt(attempt: dict) -> str | None:
        if attempt["scoring_status"] in {"unscored", "ambiguous", "partial"}:
            return None
        if attempt["scoring_status"] != "pass":
            return "again"
        if attempt.get("hints_used", 0):
            return "hard"
        return "good"
