from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 2


class LessonStoreError(RuntimeError):
    pass


class SessionConflict(LessonStoreError):
    pass


class IdempotencyConflict(LessonStoreError):
    pass


def canonical_curriculum_skill(track_id: str, module_id: str, topic_id: str) -> str:
    return f"curriculum:track:{track_id}:module:{module_id}:topic:{topic_id}"


def canonical_study_skill(project_id: str, section_id: str) -> str:
    return f"study:{project_id}:sec:{section_id}"


def canonical_custom_skill(slug: str) -> str:
    return f"custom:{slug.strip().lower().replace(' ', '-')}"


class LessonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # Concurrent writers (web + CLI, or two requests) would otherwise get an
        # immediate "database is locked"; wait up to 5s for the write lock.
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise LessonStoreError(f"unsupported lessons.db schema version: {version}")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    session_kind TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    srs_topic TEXT NOT NULL,
                    track_id TEXT,
                    module_id TEXT,
                    topic_id TEXT,
                    study_project_id TEXT,
                    section_id TEXT,
                    phase TEXT NOT NULL,
                    phase_version INTEGER NOT NULL DEFAULT 0,
                    active_item_id TEXT,
                    mastery_stage TEXT NOT NULL DEFAULT 'unstarted',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    idempotency_key TEXT,
                    UNIQUE(session_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS assessment_items (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    skill_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    bloom TEXT NOT NULL,
                    response_type TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    options TEXT NOT NULL,
                    rubric TEXT NOT NULL,
                    reference_evidence TEXT NOT NULL,
                    reference_answer TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    generator_version TEXT NOT NULL,
                    scorer_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(item_id, version)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    item_id TEXT NOT NULL,
                    item_version INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    hints_used INTEGER NOT NULL DEFAULT 0,
                    score_by_criterion TEXT NOT NULL,
                    scoring_status TEXT NOT NULL,
                    scorer_version TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    next_action TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_applications (
                    idempotency_key TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    srs_topic TEXT NOT NULL,
                    requested_grade TEXT NOT NULL,
                    application_status TEXT NOT NULL,
                    scheduler_result TEXT NOT NULL,
                    error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    skill_id TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    srs_topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(card_id, version)
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    agent_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    duration_ms REAL,
                    error TEXT,
                    output_hash TEXT,
                    provenance TEXT NOT NULL
                );
                """
            )
            self._migrate(db, version)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self, db: sqlite3.Connection, from_version: int) -> None:
        """Apply ordered, idempotent migrations from an existing db's version up
        to SCHEMA_VERSION. A fresh db (version 0) is already at the current shape
        via the CREATE-IF-NOT-EXISTS script above, so only pre-existing versions
        need work."""
        if 1 <= from_version < 2:
            # v1 carried UNIQUE(session_id, item_id, item_version) on attempts,
            # which made a post-fail retry impossible: the correct resubmit
            # collided and save_attempt returned the stale failed row, trapping
            # the learner in an unwinnable drill loop. attempts is append-only
            # and idempotency is enforced by session_events(idempotency_key), so
            # rebuild the table without the constraint, preserving history.
            db.executescript(
                """
                ALTER TABLE attempts RENAME TO attempts_legacy_v1;
                CREATE TABLE attempts (
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    item_id TEXT NOT NULL,
                    item_version INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    hints_used INTEGER NOT NULL DEFAULT 0,
                    score_by_criterion TEXT NOT NULL,
                    scoring_status TEXT NOT NULL,
                    scorer_version TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    next_action TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                INSERT INTO attempts SELECT * FROM attempts_legacy_v1;
                DROP TABLE attempts_legacy_v1;
                """
            )

    @staticmethod
    def _json(value: dict | list) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for key in (
            "options",
            "rubric",
            "provenance",
            "score_by_criterion",
            "scheduler_result",
            "payload",
        ):
            if key in result:
                result[key] = json.loads(result[key])
        return result

    def create_session(self, session: dict, *, idempotency_key: str | None = None) -> dict:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session["session_id"],
                    session["status"],
                    session["session_kind"],
                    session["skill_id"],
                    session["srs_topic"],
                    session.get("track_id"),
                    session.get("module_id"),
                    session.get("topic_id"),
                    session.get("study_project_id"),
                    session.get("section_id"),
                    session["phase"],
                    0,
                    None,
                    "unstarted",
                    now,
                    now,
                ),
            )
            self._event(
                db, session["session_id"], "created", {"phase": session["phase"]}, idempotency_key
            )
        return self.get_session(session["session_id"]) or {}

    def get_session(self, session_id: str) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            )

    def current_session(self) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE status IN ('active','paused') ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return self._row(row)

    def events(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            return [
                self._row(row) or {}
                for row in db.execute(
                    "SELECT * FROM session_events WHERE session_id=? ORDER BY event_id",
                    (session_id,),
                )
            ]

    def transition(
        self,
        session_id: str,
        phase: str,
        *,
        expected_version: int | None = None,
        status: str | None = None,
        event_type: str = "phase_changed",
        payload: dict | None = None,
    ) -> dict:
        now = time.time()
        with self._connect() as db:
            current = db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if current is None:
                raise LessonStoreError(f"unknown session: {session_id}")
            if expected_version is not None and current["phase_version"] != expected_version:
                raise SessionConflict(
                    f"session {session_id} is at version {current['phase_version']}"
                )
            next_status = status or current["status"]
            base_version = current["phase_version"]
            new_version = base_version + 1
            # Compare-and-swap: the WHERE re-checks the version the SELECT read,
            # so two writers that both passed the check above still serialize —
            # the loser's UPDATE matches zero rows and raises, instead of a
            # silent lost update with no 409.
            cursor = db.execute(
                "UPDATE sessions SET phase=?, status=?, phase_version=?, updated_at=? "
                "WHERE session_id=? AND phase_version=?",
                (phase, next_status, new_version, now, session_id, base_version),
            )
            if cursor.rowcount != 1:
                raise SessionConflict(f"session {session_id} changed concurrently")
            self._event(db, session_id, event_type, payload or {"phase": phase}, None)
        return self.get_session(session_id) or {}

    def set_mastery_stage(self, session_id: str, stage: str) -> dict:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET mastery_stage=?, updated_at=? WHERE session_id=?",
                (stage, time.time(), session_id),
            )
            self._event(db, session_id, "mastery_stage_changed", {"mastery_stage": stage}, None)
        return self.get_session(session_id) or {}

    def set_active_item(
        self, session_id: str, item_id: str, *, expected_version: int | None = None
    ) -> dict:
        now = time.time()
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise LessonStoreError(f"unknown session: {session_id}")
            if expected_version is not None and row["phase_version"] != expected_version:
                raise SessionConflict(f"session {session_id} is at version {row['phase_version']}")
            base_version = row["phase_version"]
            version = base_version + 1
            cursor = db.execute(
                "UPDATE sessions SET phase='awaiting_attempt', active_item_id=?, phase_version=?, updated_at=? "
                "WHERE session_id=? AND phase_version=?",
                (item_id, version, now, session_id, base_version),
            )
            if cursor.rowcount != 1:
                raise SessionConflict(f"session {session_id} changed concurrently")
            self._event(db, session_id, "item_issued", {"item_id": item_id}, None)
        return self.get_session(session_id) or {}

    def save_item(self, item: dict) -> dict:
        with self._connect() as db:
            db.execute(
                """INSERT INTO assessment_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["item_id"],
                    item["version"],
                    item["skill_id"],
                    item["kind"],
                    item["bloom"],
                    item["response_type"],
                    item["prompt"],
                    self._json(item.get("options", [])),
                    self._json(item.get("rubric", {})),
                    item.get("reference_evidence", ""),
                    item.get("reference_answer", ""),
                    self._json(item.get("provenance", [])),
                    item.get("generator_version", "manual"),
                    item.get("scorer_version", "deterministic-v1"),
                    time.time(),
                ),
            )
        return item

    def get_item(self, item_id: str, version: int) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute(
                    "SELECT * FROM assessment_items WHERE item_id=? AND version=?",
                    (item_id, version),
                ).fetchone()
            )

    def get_latest_item(self, item_id: str) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute(
                    "SELECT * FROM assessment_items WHERE item_id=? ORDER BY version DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
            )

    def save_attempt(self, attempt: dict) -> dict:
        # Append-only: every submission is its own attempt row (attempt_id PK).
        # A post-fail retry MUST record a new scored attempt — deduping on
        # (session,item,version) here is what trapped learners in an unwinnable
        # drill loop. Request idempotency is enforced upstream by the
        # session_events(idempotency_key) guard in LessonController.record_attempt.
        with self._connect() as db:
            db.execute(
                """INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt["attempt_id"],
                    attempt["session_id"],
                    attempt["item_id"],
                    attempt["item_version"],
                    attempt["response"],
                    attempt.get("hints_used", 0),
                    self._json(attempt.get("score_by_criterion", {})),
                    attempt["scoring_status"],
                    attempt.get("scorer_version", "deterministic-v1"),
                    attempt.get("feedback", ""),
                    attempt.get("next_action", "retry"),
                    time.time(),
                ),
            )
        return self.get_attempt(attempt["attempt_id"]) or {}

    def get_attempt(self, attempt_id: str) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            )

    def save_review_application(self, application: dict) -> dict:
        with self._connect() as db:
            existing = db.execute(
                "SELECT * FROM review_applications WHERE idempotency_key=?",
                (application["idempotency_key"],),
            ).fetchone()
            if existing:
                return self._row(existing) or {}
            db.execute(
                "INSERT INTO review_applications VALUES (?,?,?,?,?,?,?,?)",
                (
                    application["idempotency_key"],
                    application["attempt_id"],
                    application["srs_topic"],
                    application["requested_grade"],
                    application["application_status"],
                    self._json(application.get("scheduler_result", {})),
                    application.get("error"),
                    time.time(),
                ),
            )
        return self.get_review_application(application["idempotency_key"]) or {}

    def get_review_application(self, idempotency_key: str) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute(
                    "SELECT * FROM review_applications WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
            )

    def save_card(self, card: dict) -> dict:
        with self._connect() as db:
            db.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card["card_id"],
                    card["version"],
                    card["skill_id"],
                    card["source_item_id"],
                    card["question"],
                    card["answer"],
                    card["card_type"],
                    self._json(card.get("provenance", [])),
                    card["srs_topic"],
                    card.get("status", "active"),
                    time.time(),
                ),
            )
        return self.get_card(card["card_id"], card["version"]) or {}

    def get_card(self, card_id: str, version: int | None = None) -> dict | None:
        with self._connect() as db:
            if version is None:
                row = db.execute(
                    "SELECT * FROM cards WHERE card_id=? ORDER BY version DESC LIMIT 1", (card_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM cards WHERE card_id=? AND version=?", (card_id, version)
                ).fetchone()
            return self._row(row)

    def list_cards(self, *, skill_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            if skill_id:
                rows = db.execute(
                    "SELECT * FROM cards WHERE skill_id=? ORDER BY card_id, version", (skill_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM cards ORDER BY card_id, version").fetchall()
            return [self._row(row) or {} for row in rows]

    def update_card_status(self, card_id: str, version: int, status: str) -> dict:
        if status not in {"draft", "active", "retired", "superseded"}:
            raise LessonStoreError(f"invalid card status: {status}")
        with self._connect() as db:
            db.execute(
                "UPDATE cards SET status=? WHERE card_id=? AND version=?",
                (status, card_id, version),
            )
        return self.get_card(card_id, version) or {}

    def schema_version(self) -> int:
        with self._connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    def migration_report(self) -> dict:
        with self._connect() as db:
            names = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {
                name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }
        return {
            "database": str(self.path),
            "schema_version": self.schema_version(),
            "tables": counts,
        }

    def start_agent_run(
        self,
        session_id: str | None,
        agent_name: str,
        model: str,
        request_type: str,
        provenance: list[dict] | None = None,
    ) -> dict:
        import uuid

        run_id = uuid.uuid4().hex
        started = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    session_id,
                    agent_name,
                    model,
                    request_type,
                    "running",
                    started,
                    None,
                    None,
                    None,
                    None,
                    self._json(provenance or []),
                ),
            )
        return self.get_agent_run(run_id) or {}

    def finish_agent_run(
        self,
        run_id: str,
        status: str,
        *,
        output: str = "",
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT started_at FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise LessonStoreError(f"unknown agent run: {run_id}")
            ended = time.time()
            elapsed = duration_ms if duration_ms is not None else (ended - row["started_at"]) * 1000
            digest = hashlib.sha256(output.encode("utf-8")).hexdigest() if output else None
            db.execute(
                "UPDATE agent_runs SET status=?, ended_at=?, duration_ms=?, error=?, output_hash=? WHERE run_id=?",
                (status, ended, elapsed, error, digest, run_id),
            )
        return self.get_agent_run(run_id) or {}

    def get_agent_run(self, run_id: str) -> dict | None:
        with self._connect() as db:
            return self._row(
                db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            )

    def analytics(self) -> dict:
        with self._connect() as db:
            attempts = {
                row["scoring_status"]: row["count"]
                for row in db.execute(
                    "SELECT scoring_status, COUNT(*) AS count FROM attempts GROUP BY scoring_status"
                )
            }
            hints = int(
                db.execute("SELECT COALESCE(SUM(hints_used), 0) FROM attempts").fetchone()[0]
            )
            sessions = {
                row["mastery_stage"]: row["count"]
                for row in db.execute(
                    "SELECT mastery_stage, COUNT(*) AS count FROM sessions GROUP BY mastery_stage"
                )
            }
            run_status = {
                row["status"]: row["count"]
                for row in db.execute(
                    "SELECT status, COUNT(*) AS count FROM agent_runs GROUP BY status"
                )
            }
            total_sessions = int(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        return {
            "attempts": attempts,
            "hint_usage": hints,
            "sessions": {"total": total_sessions, "mastery_stages": sessions},
            "agent_runs": run_status,
        }

    def _event(
        self,
        db: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload: dict,
        idempotency_key: str | None,
    ) -> None:
        db.execute(
            "INSERT OR IGNORE INTO session_events(session_id,event_type,payload,created_at,idempotency_key) VALUES (?,?,?,?,?)",
            (session_id, event_type, self._json(payload), time.time(), idempotency_key),
        )

    def _event_for_controller(
        self, session_id: str, event_type: str, payload: dict, idempotency_key: str | None
    ) -> None:
        with self._connect() as db:
            self._event(db, session_id, event_type, payload, idempotency_key)
