"""Web server for the tutor — serves the modal + drives the tutor over a socket.

FastAPI app that serves the static web modal and exposes:

* ``WS  /ws/tutor``            — streaming turn-by-turn tail of the tutor agent
                                 (thinking / text / tool-calls / done), the way
                                 the operator console streams ``/ws/agent/<name>``.
* ``POST /api/prompt``         — blocking single-shot fallback (non-streaming).
* ``GET  /api/learner/profile``— bucketed gradebook for the skill-map rail.
* ``GET  /api/kg/search``      — knowledge-base substring search for the KB rail.
* ``GET  /api/context/usage``  — context-window usage for the Context bar.
* ``POST /api/tts`` + ``/api/tts/voices`` — MiniMax read-aloud (when a key is set).
* ``/api/study/*`` + ``/api/pedagogy/*`` — study projects + mnemonic KG.

Usage:
    python -m salient_tutor.web                    # default :8000
    python -m salient_tutor.web --port 9000        # custom port
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    import httpx

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from salient_core import semantic_recall

from salient_tutor import diagrams, illustrations, image_cloud, minimax_tts
from salient_tutor.daemon import TutorDaemon
from salient_tutor.lesson_store import LessonStoreError, SessionConflict

_log = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATIC_DIR = _REPO_ROOT / "web" / "static"


# Pointer to the last-used workspace (autoload). Lives at the repo root, outside
# any single workspace since it points across them; gitignored.
_LAST_WS_FILE = _REPO_ROOT / ".salient-tutor-workspace"


def _read_last_workspace() -> Path | None:
    """The workspace remembered from the previous run, or None if never set /
    unreadable. Best-effort — a missing or corrupt pointer just falls through to
    the default."""
    try:
        raw = _LAST_WS_FILE.read_text().strip()
    except OSError:
        return None
    return Path(raw).resolve() if raw else None


def _remember_workspace(path: Path) -> None:
    """Persist ``path`` as the last-used workspace so the next plain launch
    autoloads it. Best-effort; failure to write is non-fatal."""
    with suppress(OSError):
        _LAST_WS_FILE.write_text(str(path))


def _resolve_work_root() -> Path:
    """The active workspace ("schoolbag") directory — holds this profile's chats,
    knowledge graph, gradebook, review logs, agent configs, and images.

    Precedence: explicit ``--work-root`` / ``TUTOR_WORK_ROOT`` (any path; relative
    resolves against the repo root, so it's independent of the launch cwd) >
    the **last-used** workspace remembered from the previous run (autoload) >
    the default ``<repo>/work``. Absolute so nothing is ever silently split
    across two locations by a different launch cwd. Whichever is chosen is
    persisted at startup (see :func:`_remember_workspace`), so an explicit
    ``--work-root`` also becomes the new autoload target."""
    env = os.environ.get("TUTOR_WORK_ROOT")
    if env:
        p = Path(env).expanduser()
        return p.resolve() if p.is_absolute() else (_REPO_ROOT / p).resolve()
    last = _read_last_workspace()
    if last is not None:
        return last
    return _REPO_ROOT / "work"


# Machine-turn markers (contract with prompts/tutor.md + web/static/js/tutor.js).
# These bypass the judge leakage filter — they aren't learner problem-answers.
_SENTINELS = ("__EXPORT_LESSON__", "__FIX_DIAGRAM__", "__DRILL__", "__STUDY__")
# Pedagogy-filter strictness dial values the WS layer accepts (default socratic).
_STRICTNESS_LEVELS = ("explain", "socratic", "bare")
# Diagram-engine preference the WS layer accepts. "auto" (default) lets the tutor
# pick per diagram semantics; the rest force one fence dialect. "mermaid" renders
# client-side; dot/d2/plantuml render server-side via salient_tutor.diagrams.
_DIAGRAM_ENGINES = ("auto", "mermaid", "dot", "d2", "plantuml")
# Instruction appended to a turn when a specific engine is forced, so the tutor
# emits the matching fence. Empty for "auto"/"mermaid" (mermaid is the model's
# default fence already; forcing it needs no directive).
_DIAGRAM_DIRECTIVES = {
    "dot": "For any diagram in your reply, use a ```dot fenced code block (Graphviz DOT).",
    "d2": "For any diagram in your reply, use a ```d2 fenced code block (D2 syntax).",
    "plantuml": "For any diagram in your reply, use a ```plantuml fenced code block.",
}
# Draft event kinds suppressed on a reviewed (gated) turn so the raw answer never
# streams before the judge clears it.
_GATED_SUPPRESS = ("text", "thinking", "tool-call", "tool-result")
# Appended to a turn when the learner has diffusion illustrations enabled, so the
# tutor knows it MAY emit a ```image fence this turn. Absent by default → the
# IMAGES prompt section keeps images opt-in and behavior is normal diagramming.
_IMAGE_DIRECTIVE = (
    "Illustrations are enabled: for a purely mnemonic/evocative concept you may "
    "include ONE ```image fence (a diffusion scene description — never for "
    "diagrams, labels, or anything a learner must read)."
)
# Strip stray ```image fences from any text (used on council replies so a
# multi-agent fan-out can't kick off several slow GPU jobs at once — images stay
# a single-tutor-turn affordance).
_IMAGE_FENCE_RE = re.compile(r"```image\b[\s\S]*?```", re.IGNORECASE)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global daemon
    work_root = _resolve_work_root()
    _remember_workspace(work_root)  # autoload this one on the next plain launch
    daemon = TutorDaemon(work_root=work_root)
    # Co-locate generated images with the rest of this workspace's data (unless an
    # explicit TUTOR_IMAGE_CACHE override is set), so each profile has its own
    # images and a workspace is fully self-contained under one directory.
    if not os.environ.get("TUTOR_IMAGE_CACHE"):
        illustrations.configure_cache(work_root / "images")
    _log.info("workspace: %s", work_root)
    _log.info("image generation: %s", illustrations.provider_summary())
    await daemon.start()
    try:
        yield
    finally:
        if daemon:
            await daemon.stop()


app = FastAPI(title="salient-tutor", lifespan=lifespan)
daemon: TutorDaemon | None = None


class PromptRequest(BaseModel):
    message: str
    agent: str = "tutor"


class PromptResponse(BaseModel):
    reply: str
    error: str | None = None


class SecondOpinionRequest(BaseModel):
    question: str


class QuizRequest(BaseModel):
    topic: str


class QuizGradeRequest(BaseModel):
    topic: str
    question: str = ""
    answer: str = ""
    learner_answer: str = ""


class ReviewRequest(BaseModel):
    topic: str
    grade: str


class SessionRequest(BaseModel):
    source: str = "custom"
    session_kind: str = "lesson"
    skill_id: str = ""
    track_id: str | None = None
    module_id: str | None = None
    topic_id: str | None = None
    study_project_id: str | None = None
    section_id: str | None = None
    srs_topic: str | None = None


class SessionWriteRequest(BaseModel):
    expected_version: int | None = None


class AssessmentItemRequest(BaseModel):
    item: dict | None = None
    expected_version: int | None = None


class AttemptRequest(BaseModel):
    item_id: str
    item_version: int
    response: str
    idempotency_key: str
    hints_used: int = 0
    # Structured judge verdict for free_text items (status/confidence/criteria).
    # Deterministic item types ignore it; free_text stays "unscored" until a
    # confident judge result arrives, so the field must be forwardable.
    judge_result: dict | None = None


class ReviewApplyRequest(BaseModel):
    attempt_id: str
    idempotency_key: str


class CardRequest(BaseModel):
    card_id: str | None = None
    version: int = 1
    source_item_id: str | None = None
    question: str
    answer: str
    card_type: str = "basic"
    provenance: list[dict] = []
    status: str = "active"


class CardStatusRequest(BaseModel):
    version: int
    status: str


class DiagramRequest(BaseModel):
    engine: str
    source: str


class DiagramResponse(BaseModel):
    svg: str | None = None
    error: str | None = None


class ImageRequest(BaseModel):
    source: str
    model: str | None = None
    mode: str | None = None


class ImageResponse(BaseModel):
    url: str | None = None
    cached: bool = False
    model: str | None = None
    error: str | None = None


def _require_daemon() -> TutorDaemon:
    if daemon is None:
        raise HTTPException(status_code=503, detail="daemon not started")
    return daemon


def _lesson_error(error: Exception) -> NoReturn:
    if isinstance(error, SessionConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, LessonStoreError):
        raise HTTPException(status_code=400, detail=str(error)) from error
    raise error


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ── Streaming: tail one tutor turn over a WebSocket ──────────────────────────
@app.websocket("/ws/tutor")
async def ws_tutor(ws: WebSocket) -> None:
    """Bidirectional socket: client sends ``{cmd:"prompt", message}``; server
    streams the runner's live events (``thinking``/``text``/``tool-call``/…) and
    a synthetic ``done`` when the turn's future resolves.

    Events are filtered to the in-flight job so a burst of background activity on
    the shared runner never bleeds into the client's transcript. The operator's
    own ``user_message`` echo is dropped (the page shows it locally on send).
    """
    await ws.accept()
    if daemon is None:
        with suppress(Exception):
            await ws.send_json({"kind": "error", "text": "daemon not started"})
        await ws.close()
        return

    # Tutor family the client may talk to (default + any TUTOR_VARIANT_MODEL
    # shadow). The client picks one via {cmd:"select"} or per-prompt `agent`.
    tutor_names = {t["name"] for t in daemon.tutors()} or {"tutor"}
    # gated_job: the id of an in-flight turn whose raw draft is being reviewed by
    # the judge and must NOT stream live (its final text arrives via wait_done).
    # attempt_pending: the judge asked the learner to attempt first; their next
    # message IS the attempt, so the following turn must not re-gate.
    state: dict[str, object] = {
        "agent": "tutor",
        "job": None,
        "gated_job": None,
        "attempt_pending": False,
        "session_id": None,
    }

    async def ensure(agent: str):
        runner = daemon._make_runner(agent)
        if runner.status not in ("running", "idle"):
            await runner.start()
        return runner

    await ensure("tutor")
    # Tap the daemon-wide hub so switching tutor variants is just a filter
    # change (no re-subscribe). Only the selected agent's live turn is forwarded.
    queue, _snapshot = daemon.subscribe_events()

    async def forward() -> None:
        while True:
            evt = await queue.get()
            if evt.get("replay"):
                continue
            if evt.get("agent") != state["agent"]:
                continue
            if evt.get("kind") == "user_message":
                continue  # already echoed by the page on send
            if evt.get("kind") == "done":
                continue  # runner's per-turn diagnostic; wait_done sends the
                # authoritative terminal `done` with the reply text
            jid = evt.get("job_id")
            if state["job"] is not None and jid is not None and jid != state["job"]:
                continue
            # Reviewed turn: suppress the raw draft's content events so the leak
            # never reaches the learner — only the judge-cleared final is shown.
            if jid is not None and jid == state["gated_job"] and evt.get("kind") in _GATED_SUPPRESS:
                continue
            with suppress(Exception):
                await ws.send_json(evt)

    async def heartbeat() -> None:
        """App-level WS keepalive: send a no-op ping every ~15s so the
        connection stays warm during long turns (a local-model parse or a slow
        judge review can run minutes with no event to forward). Without this the
        connection sits idle from the server's perspective and an intermediary
        (proxy / load-balancer / uvicorn's own ping watchdog) closes it with a
        'keepalive ping timeout' (1011), killing the in-flight turn. The client
        ignores unknown event kinds, so this is a silent heartbeat."""
        while True:
            await asyncio.sleep(15)
            with suppress(Exception):
                await ws.send_json({"kind": "ping"})

    async def wait_done(
        fut: asyncio.Future,
        job_id: int,
        agent: str,
        *,
        question: str = "",
        gated: bool = False,
        strictness: str = "socratic",
        attempt_pending: bool = False,
    ) -> None:
        try:
            result_job = await fut
            draft = result_job.result or ""
            hinted = False
            awaiting = None
            # Reviewed turn: run the judge pedagogy filter (attempt-first gate +
            # leakage filter) before the answer is shown. Degrades to passthrough
            # on any judge failure.
            if gated and draft and not result_job.error:
                with suppress(Exception):
                    await ws.send_json(
                        {"kind": "phase", "agent": agent, "job_id": job_id, "text": "reviewing"}
                    )
                res = await daemon.pedagogy_filter(
                    question, draft, strictness=strictness, attempt_pending=attempt_pending
                )
                draft = res.get("revised") or draft
                if res.get("needs_attempt"):
                    awaiting = "attempt"
                else:
                    hinted = bool(res.get("leaked"))
                # The next turn is the learner's attempt iff we just asked for one.
                state["attempt_pending"] = bool(res.get("needs_attempt"))
            payload = {
                "kind": "done",
                "agent": agent,
                "job_id": job_id,
                "text": draft or result_job.error or "",
                "error": result_job.error,
                "hinted": hinted,
                "awaiting": awaiting,
                "session_id": state["session_id"],
            }
        except Exception as e:  # noqa: BLE001 — surface any failure to the client
            payload = {"kind": "error", "agent": agent, "job_id": job_id, "text": str(e)}
        finally:
            if state["gated_job"] == job_id:
                state["gated_job"] = None
        with suppress(Exception):
            await ws.send_json(payload)

    forwarder = asyncio.create_task(forward())
    heartbeat_task = asyncio.create_task(heartbeat())
    waiters: list[asyncio.Task] = []
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "attach":
                requested = (msg.get("session_id") or "").strip()
                try:
                    snapshot = await asyncio.to_thread(daemon.get_session, requested)
                    events = await asyncio.to_thread(daemon.session_events, requested)
                except Exception as error:
                    # A bad/stale session id must not tear down the whole socket
                    # (raising HTTPException here would): reply with an error
                    # frame and keep the connection alive, like the submit path.
                    await ws.send_json(
                        {"kind": "conflict", "session_id": requested, "text": str(error)}
                    )
                    continue
                state["session_id"] = requested
                await ws.send_json({"kind": "session", "session": snapshot, "events": events})
                continue
            if cmd == "select":
                agent = msg.get("agent")
                if agent in tutor_names:
                    await ensure(agent)
                    state["agent"] = agent
                continue
            if cmd != "prompt":
                continue
            text = (msg.get("message") or "").strip()
            if not text:
                continue
            agent = msg.get("agent") if msg.get("agent") in tutor_names else state["agent"]
            runner = await ensure(agent)
            state["agent"] = agent
            # Reviewed turn: gate a real (non-sentinel) tutor turn through the
            # judge leakage filter when a judge is configured.
            strictness = msg.get("strictness")
            if strictness not in _STRICTNESS_LEVELS:
                strictness = "socratic"
            sentinel = text.startswith(_SENTINELS)
            gated = daemon.judge_enabled() and not sentinel
            was_pending = bool(state["attempt_pending"])
            # Forced diagram engine: prepend a fence directive to the SUBMITTED
            # prompt only. `question` (fed to the judge + echoed) stays the raw
            # learner text; the client renders its own echo, so augmenting the
            # submission doesn't change what the learner sees.
            engine = msg.get("diagram_engine")
            directive = _DIAGRAM_DIRECTIVES.get(engine) if engine in _DIAGRAM_ENGINES else None
            # Diffusion illustrations are opt-in per turn: the client sends the
            # chosen model only when the learner has the feature toggled on. Add
            # the image directive when the server can actually render.
            img_model = msg.get("image_model")
            image_on = (
                not sentinel and img_model in illustrations.models() and illustrations.available()
            )
            parts = [
                p
                for p in (
                    directive if not sentinel else None,
                    _IMAGE_DIRECTIVE if image_on else None,
                )
                if p
            ]
            submit_text = "\n\n".join([*parts, text]) if parts else text
            attached_session = msg.get("session_id") or state["session_id"]
            if attached_session:
                try:
                    snapshot = await asyncio.to_thread(daemon.get_session, attached_session)
                except Exception as error:
                    await ws.send_json(
                        {"kind": "conflict", "session_id": attached_session, "text": str(error)}
                    )
                    continue
                state["session_id"] = attached_session
                submit_text = (
                    f"SERVER SESSION STATE: phase={snapshot['phase']}; skill_id={snapshot['skill_id']}; "
                    "Only structured assessment actions advance the lesson.\n\n" + submit_text
                )
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            # cfg max_turns as the per-job budget — arms the runner's hard cap
            # for backend-seam providers (codex) with no native turn limit.
            job = runner.submit(submit_text, future=fut, max_turns_hint=runner.cfg.get("max_turns"))
            state["job"] = job.id
            state["gated_job"] = job.id if gated else None
            waiters.append(
                asyncio.create_task(
                    wait_done(
                        fut,
                        job.id,
                        agent,
                        question=text,
                        gated=gated,
                        strictness=strictness,
                        attempt_pending=was_pending,
                    )
                )
            )
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        _log.exception("ws_tutor loop failed")
    finally:
        forwarder.cancel()
        heartbeat_task.cancel()
        for w in waiters:
            w.cancel()
        with suppress(Exception):
            daemon.unsubscribe_events(queue)


@app.post("/api/prompt", response_model=PromptResponse)
async def prompt(req: PromptRequest) -> PromptResponse:
    """Blocking single-shot fallback used for export/fix/study sentinel turns."""
    if not daemon:
        return PromptResponse(reply="", error="daemon not started")
    try:
        reply = await daemon.prompt(req.agent, req.message)
        return PromptResponse(reply=reply)
    except Exception as e:
        return PromptResponse(reply="", error=str(e))


@app.post("/api/second_opinion")
async def second_opinion(req: SecondOpinionRequest) -> dict:
    """Ask the tutor panel (tutor + tutor_alt) the same question via the
    kernel's ask_consensus; blocking like /api/prompt — the legs themselves
    run concurrently inside the consensus tool."""
    if not daemon:
        return {"ok": False, "error": "daemon not started"}
    q = (req.question or "").strip()
    if not q:
        return {"ok": False, "error": "no question"}
    try:
        return _strip_image_fences(await daemon.second_opinion(q))
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/sessions")
def create_session(req: SessionRequest) -> dict:
    service = _require_daemon()
    skill_id = req.skill_id.strip()
    if not skill_id:
        if req.study_project_id and req.section_id:
            skill_id = f"study:{req.study_project_id}:sec:{req.section_id}"
        elif req.track_id and req.module_id and req.topic_id:
            skill_id = (
                f"curriculum:track:{req.track_id}:module:{req.module_id}:topic:{req.topic_id}"
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="skill_id or a complete curriculum/study binding is required",
            )
    try:
        return service.create_session(
            skill_id,
            session_kind=req.session_kind,
            srs_topic=req.srs_topic,
            track_id=req.track_id,
            module_id=req.module_id,
            topic_id=req.topic_id,
            study_project_id=req.study_project_id,
            section_id=req.section_id,
        )
    except Exception as error:
        _lesson_error(error)


@app.get("/api/sessions/current")
def current_session() -> dict:
    session = _require_daemon().current_session()
    return {"session": session}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    try:
        return {"session": _require_daemon().get_session(session_id)}
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/pause")
def pause_session(session_id: str, req: SessionWriteRequest) -> dict:
    try:
        return {"session": _require_daemon().pause_session(session_id, req.expected_version)}
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/resume")
def resume_session(session_id: str, req: SessionWriteRequest) -> dict:
    try:
        return {"session": _require_daemon().resume_session(session_id, req.expected_version)}
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/abandon")
def abandon_session(session_id: str, req: SessionWriteRequest) -> dict:
    try:
        return {"session": _require_daemon().abandon_session(session_id, req.expected_version)}
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/advance")
def advance_session(session_id: str, req: SessionWriteRequest) -> dict:
    try:
        return {"session": _require_daemon().advance_phase(session_id, req.expected_version)}
    except Exception as error:
        _lesson_error(error)


@app.get("/api/sessions/{session_id}/events")
def session_events(session_id: str) -> dict:
    try:
        service = _require_daemon()
        service.get_session(session_id)
        return {"events": service.session_events(session_id)}
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/items")
def issue_item(session_id: str, req: AssessmentItemRequest) -> dict:
    try:
        if req.item and "reference_answer" in req.item:
            raise HTTPException(status_code=422, detail="reference answers are server-owned")
        return _require_daemon().issue_assessment_item(session_id, req.item, req.expected_version)
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/attempts")
def record_attempt(session_id: str, req: AttemptRequest) -> dict:
    try:
        return _require_daemon().record_attempt(
            session_id,
            req.item_id,
            req.item_version,
            req.response,
            req.idempotency_key,
            req.hints_used,
            judge_result=req.judge_result,
        )
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/reviews")
def apply_attempt_review(session_id: str, req: ReviewApplyRequest) -> dict:
    try:
        return {
            "review": _require_daemon().apply_attempt_review(
                session_id, req.attempt_id, req.idempotency_key
            )
        }
    except Exception as error:
        _lesson_error(error)


@app.post("/api/sessions/{session_id}/cards")
def create_card(session_id: str, req: CardRequest) -> dict:
    try:
        return {"card": _require_daemon().create_card(session_id, req.model_dump())}
    except Exception as error:
        _lesson_error(error)


@app.get("/api/cards")
def list_cards(skill_id: str | None = None) -> dict:
    return {"cards": _require_daemon().list_cards(skill_id)}


@app.post("/api/cards/{card_id}/status")
def update_card_status(card_id: str, req: CardStatusRequest) -> dict:
    try:
        return {"card": _require_daemon().update_card_status(card_id, req.version, req.status)}
    except Exception as error:
        _lesson_error(error)


@app.get("/api/analytics")
def analytics() -> dict:
    return _require_daemon().analytics()


@app.get("/api/migrations/report")
def migration_report() -> dict:
    return _require_daemon().migration_report()


def _strip_image_fences(obj):
    """Recursively drop ```image fences from council text so a multi-agent
    fan-out never enqueues several slow GPU jobs. Diagram fences are left alone
    (they render cheaply client-side)."""
    if isinstance(obj, str):
        return _IMAGE_FENCE_RE.sub("", obj)
    if isinstance(obj, dict):
        return {k: _strip_image_fences(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_image_fences(v) for v in obj]
    return obj


@app.post("/api/quiz")
async def quiz(req: QuizRequest) -> dict:
    """Generate one retrieval question for a due topic (tutor-authored)."""
    if not daemon:
        return {"error": "daemon not started"}
    topic = (req.topic or "").strip()
    if not topic:
        return {"error": "no topic"}
    try:
        return await daemon.quiz(topic)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/quiz/grade")
async def quiz_grade(req: QuizGradeRequest) -> dict:
    """Grade the learner's retrieval answer and record the SM-2 review."""
    if not daemon:
        return {"error": "daemon not started"}
    topic = (req.topic or "").strip()
    if not topic:
        return {"error": "no topic"}
    try:
        return await daemon.grade_quiz(topic, req.question, req.answer, req.learner_answer)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/review")
async def review(req: ReviewRequest) -> dict:
    """Record an SM-2 review for an arbitrary topic and return the new schedule.

    Used by the memory-palace recall ladder to grade one locus (topic
    ``loci:<palaceId>/<locusId>``): palace grades ride the SAME gradebook as
    quizzes — there is no parallel SRS. ``record_review`` is deterministic (no
    LLM) and accepts any topic string, so this is a thin passthrough."""
    if not daemon:
        return {"error": "daemon not started"}
    topic = (req.topic or "").strip()
    if not topic:
        return {"error": "no topic"}
    try:
        return daemon.record_review(topic, req.grade)
    except ValueError as e:  # unknown grade → 200 with error, like the quiz routes
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/skillmap/graph")
async def skillmap_graph() -> dict:
    """Prerequisite-DAG view of the gradebook (persisted curriculum: edges)."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        return await daemon.skill_graph()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/skillmap/graph/rebuild")
async def skillmap_graph_rebuild() -> dict:
    """Regenerate the prerequisite DAG (clears + re-infers the curriculum: edges)."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        return await daemon.skill_graph(rebuild=True)
    except Exception as e:
        return {"error": str(e)}


# ── Server-side diagram rendering (dot / d2 / plantuml → SVG) ─────────────────
@app.post("/api/diagram", response_model=DiagramResponse)
async def diagram(req: DiagramRequest) -> DiagramResponse:
    """Render a non-Mermaid diagram to inline SVG via a local engine binary.

    Mermaid renders in the browser; this covers the server-side engines. All
    failures come back as ``error`` (never a 500) so the client shows the same
    parse-error card + ``__FIX_DIAGRAM__`` repair button it uses for Mermaid."""
    svg, err = await diagrams.render(req.engine, req.source)
    return DiagramResponse(svg=svg, error=err)


@app.post("/api/image", response_model=ImageResponse)
async def image(req: ImageRequest) -> ImageResponse:
    """Render a fenced ``image`` block to a branded diffusion PNG (ComfyUI).

    Blocking like ``/api/diagram`` — the client mounts a shimmer placeholder and
    swaps in the ``<img>`` when this resolves. All failures come back as
    ``error`` (never a 500) so the card degrades to its textual caption. Slow
    (seconds–minutes) and serialized on one GPU inside the illustrations module;
    identical specs are cache hits and return instantly."""
    result, err = await illustrations.render(
        req.source, model=req.model, mode=req.mode or "mnemonic"
    )
    if err:
        return ImageResponse(error=err)
    return ImageResponse(url=result.url, cached=result.cached, model=result.model)


@app.get("/api/image/{name}")
async def image_file(name: str) -> Response:
    """Serve a cached illustration PNG by ``<hash>.png`` (content-addressed)."""
    digest = name[:-4] if name.endswith(".png") else name
    path = illustrations.cache_file(digest)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ── Learner / knowledge base / context (rail data) ───────────────────────────
@app.get("/api/learner/profile")
async def learner_profile() -> dict:
    """Bucketed gradebook (due/strong/weak/misconceptions) for the skill-map."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        return daemon.learner_profile()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/review-log")
async def review_log(limit: int = 20) -> dict:
    """Read-only scheduling telemetry (Phase-0 review log): headline recall-at-due
    rate + grade distribution + the most recent `limit` events, for the rail."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        from salient_tutor import reviewlog

        events = daemon.review_log(limit=None)  # all, for accurate aggregates
        summary = reviewlog.summarize(events)
        summary["recent"] = events[-max(0, limit) :] if limit else []
        return summary
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/kg/search")
async def kg_search(q: str = "", limit: int = 60) -> dict:
    """Substring search of the knowledge base for the KB rail."""
    if not daemon:
        return {"results": []}
    try:
        return {"results": daemon.kg_search(q, limit=limit)}
    except Exception as e:
        return {"results": [], "error": str(e)}


@app.get("/api/context/usage")
async def context_usage() -> dict:
    """Latest context-window usage for the tutor (Context bar)."""
    if not daemon:
        return {}
    try:
        return daemon.context_usage("tutor")
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/tutors")
async def tutors() -> dict:
    """Tutor family (default + shadow variant) for the variant picker."""
    if not daemon:
        return {"tutors": []}
    return {"tutors": daemon.tutors()}


@app.get("/api/config")
async def config() -> dict:
    """Runtime feature flags for the client (judge/pedagogy-filter availability).

    ``judge`` true means turns are reviewed before display and the strictness
    dial should show; false means live streaming as usual.
    """
    # Mermaid always renders client-side; the rest are gated on their binary
    # being installed (and enabled) so the engine toggle only offers what works.
    diagram_engines = {"mermaid": True, **diagrams.available_engines()}
    # Diffusion illustrations: advertise availability + the models actually
    # installed on the box so the client shows/hides the image control and can't
    # offer an un-downloaded model. Probed off the event loop, fail-safe. Empty
    # /false → normal diagramming.
    img_available = illustrations.available()
    img_models = await asyncio.to_thread(illustrations.installed_models) if img_available else []
    images = {
        "available": img_available,
        "models": img_models,
        # Which of `models` are cloud (MiniMax/GLM) vs local (ComfyUI box) — the
        # dial marks cloud ones. Single source of truth: image_cloud's registry.
        "cloud": [m for m in img_models if m in set(image_cloud.configured_models())],
        # Prefer the verified default (flux-dev) over a possibly-broken first
        # entry like an incompletely-installed flux-schnell.
        "default": illustrations.default_model(img_models),
    }
    workspace = _resolve_work_root().name
    if not daemon:
        return {
            "judge": False,
            "strictness_default": "socratic",
            "diagram_engines": diagram_engines,
            "images": images,
            "workspace": workspace,
        }
    return {
        "judge": daemon.judge_enabled(),
        "strictness_default": "socratic",
        "diagram_engines": diagram_engines,
        "images": images,
        "workspace": workspace,
    }


@app.get("/api/history")
async def history(limit: int = 30) -> dict:
    """Recent transcript turns to replay when the page (re)loads."""
    if not daemon:
        return {"turns": []}
    try:
        return daemon.history("tutor", limit=limit)
    except Exception as e:
        return {"turns": [], "error": str(e)}


# ── TTS (read-aloud) — only live when MINIMAX_API_KEY is set ─────────────────
@app.get("/api/tts/voices")
async def tts_voices() -> dict:
    """Voice catalog + defaults; ``available:false`` hides the read-aloud UI."""
    if not minimax_tts.available():
        return {"available": False, "voices": [], "defaults": minimax_tts.tts_defaults()}
    return {
        "available": True,
        "voices": minimax_tts.supported_voices(),
        "models": list(minimax_tts.SUPPORTED_MODELS),
        "defaults": minimax_tts.tts_defaults(),
    }


def _clamp(val, lo, hi, default):
    try:
        v = type(default)(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@app.post("/api/tts")
async def tts(req: dict) -> Response:
    """Synthesize *text* to audio bytes (cached on disk when TUTOR_TTS_CACHE=1)."""
    if not minimax_tts.available():
        return JSONResponse({"error": "TTS not configured"}, status_code=503)
    text = (req.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    d = minimax_tts.tts_defaults()
    voice = req.get("voice") or d["voice"]
    model = req.get("model") or d["model"]
    fmt = (req.get("format") or d["format"]).lower()
    if fmt not in minimax_tts.SUPPORTED_FORMATS:
        fmt = d["format"]
    speed = _clamp(req.get("speed", 1.0), *minimax_tts.SPEED_RANGE, 1.0)
    pitch = _clamp(req.get("pitch", 0), *minimax_tts.PITCH_RANGE, 0)
    vol = _clamp(req.get("vol", 1.0), *minimax_tts.VOL_RANGE, 1.0)
    try:
        sample_rate = int(req.get("sample_rate") or d["sample_rate"])
    except (TypeError, ValueError):
        sample_rate = d["sample_rate"]
    if sample_rate not in minimax_tts.SUPPORTED_SAMPLE_RATES:
        sample_rate = d["sample_rate"]
    try:
        bitrate = int(req.get("bitrate") or d["bitrate"])
    except (TypeError, ValueError):
        bitrate = d["bitrate"]
    if bitrate not in minimax_tts.SUPPORTED_BITRATES:
        bitrate = d["bitrate"]

    key = minimax_tts.cache_key(
        text,
        voice=voice,
        model=model,
        fmt=fmt,
        speed=speed,
        pitch=pitch,
        vol=vol,
        sample_rate=sample_rate,
        bitrate=bitrate,
    )
    audio = await asyncio.to_thread(minimax_tts.cache_lookup, key, fmt)
    if audio is None:
        try:
            audio = await minimax_tts.synthesize(
                text,
                voice=voice,
                model=model,
                fmt=fmt,
                speed=speed,
                pitch=pitch,
                vol=vol,
                sample_rate=sample_rate,
                bitrate=bitrate,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=502)
        await asyncio.to_thread(minimax_tts.cache_store, key, fmt, audio)
    return Response(content=audio, media_type=minimax_tts.mime_for(fmt))


# ── Pedagogy (mnemonic KG) ───────────────────────────────────────────────────
@app.get("/api/pedagogy/status")
async def pedagogy_status() -> dict:
    """Count of pedagogy: facts in the KG."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        facts = daemon.kg.export_by_subject_prefix("pedagogy:")
        return {
            "facts": len(facts),
            "seeded": daemon._pedagogy_seeded,
            "subjects": len({f.get("subject") for f in facts}),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/pedagogy/probe")
async def pedagogy_probe(req: dict) -> dict:
    """Semantic query of the pedagogy: namespace (needs an embedder configured)."""
    if not daemon:
        return {"error": "daemon not started"}
    try:
        text = req.get("text", "")
        top_k = req.get("top_k", 5)
        results = await semantic_recall(
            daemon.kg, daemon.profile, text, subject_prefix="pedagogy:", top_k=top_k
        )
        return {
            "results": [
                {
                    "subject": f.subject,
                    "predicate": f.predicate,
                    "object": f.object[:200],
                    "score": round(score, 3),
                }
                for f, score in results
            ]
        }
    except Exception as e:
        return {"error": str(e)}


# ── Study projects ───────────────────────────────────────────────────────────
@app.get("/api/study/list")
async def study_list() -> dict:
    """All study projects, each annotated with its KG fact count under the
    ``study:<id>:`` namespace (doc nodes + passage chunks + sec scaffold)."""
    if not daemon:
        return {"error": "daemon not started"}
    projects = daemon.study_list()
    for p in projects:
        facts = daemon.kg.export_by_subject_prefix(f"study:{p['project_id']}:")
        p["facts"] = len(facts)
    return {"projects": projects}


@app.get("/api/curricula/list")
def curricula_list() -> dict:
    """All curriculum tracks seeded into the ``curriculum:track:`` KG namespace
    at daemon startup (from private ``data/curricula/*.json``)."""
    if not daemon:
        return {"error": "daemon not started"}
    return {"tracks": daemon.curricula_list()}


@app.post("/api/study/create")
async def study_create(req: dict) -> dict:
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.study_create(req.get("title", "Untitled"), req.get("subject", "cyber"))


@app.get("/api/study/{project_id}")
async def study_show(project_id: str) -> dict:
    if not daemon:
        return {"error": "daemon not started"}
    result = daemon.study_show(project_id)
    return result or {"error": f"unknown project {project_id}"}


@app.post("/api/study/{project_id}/upload")
async def study_upload(project_id: str, req: dict) -> dict:
    if not daemon:
        return {"error": "daemon not started"}
    data = base64.b64decode(req.get("data", ""))
    return daemon.study_upload(project_id, req.get("filename", "upload"), data)


@app.post("/api/study/{project_id}/extract")
async def study_extract(project_id: str, req: dict) -> dict:
    if not daemon:
        return {"error": "daemon not started"}
    return await daemon.study_extract(project_id, doc_sha=req.get("doc_sha"))


# Human-readable progress line for a librarian event during extraction. The
# librarian streams the same thinking/tool-call/text events as the tutor (it just
# runs off the study panel, not the chat WS), so we relabel a few kinds into
# something the Library UI can show live. Returns None for events we don't surface.
def _librarian_progress(evt: dict) -> str | None:
    kind = evt.get("kind")
    if kind == "thinking":
        return "🧠 reading & structuring the document…"
    if kind == "text":
        return "✍️ writing the structured sections…"
    if kind == "phase":
        return "⏳ " + str(evt.get("text") or "working")
    if kind == "tool-call":
        name = (
            str(evt.get("text") or "").split("(")[0].strip().split()[0] if evt.get("text") else ""
        )
        return "🔧 " + name if name else None
    if kind in ("tool-error", "error", "refusal"):
        return "⚠️ " + str(evt.get("text") or kind)[:120]
    return None


@app.get("/api/study/{project_id}/extract/stream")
async def study_extract_stream(project_id: str, doc_sha: str = "") -> StreamingResponse:
    """SSE variant of extract: runs the same daemon.study_extract but streams the
    librarian's live activity (reading / tool-calls / writing) as ``progress``
    events, then a terminal ``done`` carrying the same result dict as the POST.
    Gives the Library panel real-time visibility instead of a minute of silence."""

    async def gen():
        if not daemon:
            yield f"data: {json.dumps({'kind': 'done', 'result': {'error': 'daemon not started'}})}\n\n"
            return
        queue, _snap = daemon.subscribe_events()
        task = asyncio.create_task(daemon.study_extract(project_id, doc_sha=doc_sha or None))
        yield f"data: {json.dumps({'kind': 'progress', 'text': '📄 extracting text from the document…'})}\n\n"
        last = None
        pending = {asyncio.ensure_future(queue.get()), task}
        try:
            while True:
                done, pending = await asyncio.wait(
                    pending, timeout=12, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:  # idle → keep the connection warm through a proxy
                    yield f"data: {json.dumps({'kind': 'ping'})}\n\n"
                    continue
                if task in done:
                    try:
                        result = task.result()
                    except Exception as e:  # noqa: BLE001 — surface as a failed result
                        result = {"status": "failed", "error": str(e)}
                    yield f"data: {json.dumps({'kind': 'done', 'result': result})}\n\n"
                    return
                evt = next(f for f in done if f is not task).result()
                if evt.get("agent") == "librarian" and not evt.get("replay"):
                    line = _librarian_progress(evt)
                    if line and line != last:
                        last = line
                        yield f"data: {json.dumps({'kind': 'progress', 'text': line})}\n\n"
                pending = {asyncio.ensure_future(queue.get()), task}
        finally:
            for f in pending:
                if f is not task:
                    f.cancel()
            daemon.unsubscribe_events(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/study/{project_id}")
async def study_delete(project_id: str, req: dict) -> dict:
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.study_delete(project_id, confirm=req.get("confirm", False))


@app.delete("/api/study/{project_id}/doc/{sha}")
async def study_delete_doc(project_id: str, sha: str, req: dict) -> dict:
    """Delete ONE document + its passage facts from a project (confirm-gated)."""
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.study_delete_doc(project_id, sha, confirm=req.get("confirm", False))


# ── Embeddings config + model picker (LM Studio / OpenAI-compatible) ─────────
@app.get("/api/embed/config")
async def embed_config() -> dict:
    """Resolved embeddings config (model/base_url present, api_key masked) +
    KG coverage. Never does a live HTTP call — use /api/embed/models to probe."""
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.embed_config()


@app.post("/api/embed/config")
async def set_embed_config(req: dict) -> dict:
    """Set (or clear, when all fields empty) the embeddings endpoint + model.

    The model must be operator-selected (e.g. picked from /api/embed/models);
    nothing is auto-applied. An all-empty body reverts to ``SALIENT_EMBED_*`` env."""
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.set_embed_config(
        base_url=req.get("base_url", ""),
        model=req.get("model", ""),
        api_key=req.get("api_key", ""),
    )


@app.get("/api/embed/models")
async def embed_models(base_url: str = "", api_key: str = "") -> dict:
    """List the models the embed server has loaded, for the picker dropdown.

    Probes ``GET {base_url}/v1/models`` (the OpenAI/LM Studio listing). Uses the
    saved config when ``base_url`` is omitted, so the operator can refresh the
    list after save. FAIL-SAFE: any transport/parse error returns
    ``{reachable: false, error}`` — the caller (modal) shows a warning and the
    app keeps working without embeddings (the backfill loop never raises,
    semantic_recall degrades to []). Short timeout so a down server doesn't hang
    the modal."""
    if not daemon:
        return {"error": "daemon not started"}
    import httpx
    from salient_core.memory.embeddings import resolve_config

    # Prefer the query override (test a not-yet-saved URL); else saved config.
    if base_url:
        url = base_url.rstrip("/")
        key = (api_key or "").strip()
    else:
        cfg = resolve_config(daemon.profile)
        if cfg is None:
            return {"reachable": False, "error": "no base_url configured", "models": []}
        url, key = cfg.base_url, cfg.api_key
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{url}/v1/models", headers=headers)
        if resp.status_code != 200:
            return {
                "reachable": False,
                "error": f"server returned HTTP {resp.status_code}",
                "models": [],
            }
        data = resp.json().get("data")
        models = (
            [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
            if isinstance(data, list)
            else []
        )
        return {"reachable": True, "models": models, "base_url": url}
    except Exception as e:  # noqa: BLE001 — server-down must NOT 500 the modal
        return {"reachable": False, "error": str(e), "models": []}


# ── Librarian / parser provider (Claude ↔ local LM Studio) ───────────────────
@app.get("/api/librarian/config")
async def librarian_config() -> dict:
    """Resolved librarian provider for the gear modal's parser section."""
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.librarian_config()


@app.post("/api/librarian/config")
async def set_librarian_config(req: dict) -> dict:
    """Set (or clear, with provider=claude) the librarian parser provider."""
    if not daemon:
        return {"error": "daemon not started"}
    return daemon.set_librarian_config(
        provider=req.get("provider", "claude"),
        base_url=req.get("base_url", ""),
        model=req.get("model", ""),
        api_key=req.get("api_key", ""),
        auth_style=req.get("auth_style", "api_key"),
    )


@app.get("/api/librarian/models")
async def librarian_models(base_url: str = "", api_key: str = "") -> dict:
    """List the chat models a local server has loaded AND confirm it serves the
    Anthropic Messages shape (/v1/messages) the Claude SDK requires.

    The Claude Agent SDK speaks only POST {base_url}/v1/messages — so a server
    that only offers OpenAI's /v1/chat/completions is NOT usable as a parser.
    We probe /v1/models for the loaded chat models, then send a minimal
    /v1/messages request to confirm the Anthropic shape is actually served.

    FAIL-SAFE: any transport/parse error (server down, wrong shape, refused)
    returns ``{reachable: false, anthropic: false, error}`` — never a 500 — so
    the modal can show a warning and keep the operator on Claude. Short timeout
    so a down server doesn't hang the modal."""
    if not daemon:
        return {"error": "daemon not started"}
    import httpx

    url = base_url.strip().rstrip("/")
    if not url:
        return {"reachable": False, "anthropic": False, "error": "no base_url", "models": []}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            # 1. loaded models (OpenAI/LM Studio listing shape).
            resp = await client.get(f"{url}/v1/models", headers=headers)
            if resp.status_code != 200:
                return {
                    "reachable": False,
                    "anthropic": False,
                    "error": f"server returned HTTP {resp.status_code}",
                    "models": [],
                }
            data = resp.json().get("data")
            models = (
                [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
                if isinstance(data, list)
                else []
            )
            # 2. confirm the Anthropic Messages shape is served (the SDK needs it).
            #    A tiny messages request; a 4xx for bad model or a 200 both prove
            #    the endpoint exists and speaks Anthropic. Anything else → not usable.
            anthropic = False
            try:
                probe = await client.post(
                    f"{url}/v1/messages",
                    json={
                        "model": models[0] if models else "probe",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "."}],
                    },
                    headers=headers,
                )
                # 200 (it answered) or 404-on-bad-model/400 (endpoint exists,
                # just rejected the payload) both indicate the shape is served.
                anthropic = probe.status_code in (200, 400, 404, 422)
            except Exception:
                anthropic = False
        return {
            "reachable": True,
            "anthropic": anthropic,
            "models": models,
            "base_url": url,
        }
    except Exception as e:  # noqa: BLE001 — server-down must NOT 500 the modal
        return {"reachable": False, "anthropic": False, "error": str(e), "models": []}


# ── LM Studio model management (native REST API: list/load/unload) ────────────
def _lms_resolve(base_url: str, api_key: str) -> tuple[str, str] | None:
    """Resolve the LM Studio base_url + api_key from the override params, falling
    back to the saved embed config's base_url (same server, by convention). Use
    the embed config because it's the operator's confirmed LM Studio endpoint;
    the librarian config (when local) shares it. Returns None when nothing is
    configured — the caller surfaces 'no base_url'."""
    if base_url:
        return base_url.strip().rstrip("/"), (api_key or "").strip()
    from salient_core.memory.embeddings import resolve_config

    cfg = resolve_config(daemon.profile) if daemon else None
    if cfg is not None:
        return cfg.base_url, cfg.api_key
    return None


# ── Per-agent provider/model config (the 🤖 Agents tab) ──────────────────────
@app.get("/api/agents/config")
async def agents_config() -> dict:
    """Every agent's resolved runtime config + the provider registry (for the
    Agents tab's dropdowns). Always 200 — there's nothing to fail-safe against
    (it's a pure read of in-memory state)."""
    if not daemon:
        return {"error": "daemon not started"}
    from salient_tutor.daemon import _OPTIONAL_AGENTS
    from salient_tutor.providers import PROVIDERS

    return {
        "agents": daemon.all_agent_configs(),
        # Optional agents (judge/tutor_alt) that aren't live yet but CAN be added
        # from this tab — the frontend renders an addable row for each absent one.
        "optional": sorted(_OPTIONAL_AGENTS),
        "providers": {
            name: {
                "label": spec.label,
                "needs_endpoint": spec.needs_endpoint,
                "default_base_url": spec.default_base_url,
                "auth_style": spec.auth_style,
                "supports_thinking": spec.supports_thinking,
                # "sdk" | "endpoint" | "backend" — backend providers (codex)
                # hide the endpoint fields and hint at their own auth story.
                "kind": spec.kind,
            }
            for name, spec in PROVIDERS.items()
        },
        "efforts": ["low", "med", "high"],
    }


# Probe results are cached (TTL) and concurrent callers share one in-flight
# task (single-flight): every COLD codex probe spawns a codex CLI app-server
# subprocess for a JSON-RPC handshake, so an Agents-tab render — or several
# browser tabs — must not stack subprocesses. The timeout keeps a wedged
# (alive but unresponsive) binary from pinning the request and its probe
# worker thread forever: the handshake bottoms out in an untimed Queue.get.
_PROBE_TTL = 60.0
_PROBE_TIMEOUT = 10.0
_probe_cache: dict[str, tuple[float, dict]] = {}
_probe_inflight: dict[str, asyncio.Task] = {}


async def _run_probe(name: str) -> dict:
    try:
        from salient_core import ProviderName, get_provider_registry

        probe = await asyncio.wait_for(
            get_provider_registry().get(ProviderName(name)).probe(), timeout=_PROBE_TIMEOUT
        )
        result = {"provider": name, "available": probe.available, "detail": probe.detail}
    except TimeoutError:
        result = {
            "provider": name,
            "available": False,
            "detail": f"probe timed out after {_PROBE_TIMEOUT:.0f}s",
        }
    except Exception as exc:  # noqa: BLE001 — surface, never 500 the tab
        result = {"provider": name, "available": False, "detail": str(exc)}
    _probe_cache[name] = (time.monotonic(), result)
    _probe_inflight.pop(name, None)
    return result


@app.get("/api/providers/probe")
async def provider_probe(name: str = "codex") -> dict:
    """Availability probe for a backend provider (is the SDK installed and
    authenticated?) so the Agents tab can hint inline before the operator
    routes an agent there. FAIL-SAFE: any error → {available:false, detail}."""
    if not daemon:
        return {"error": "daemon not started"}
    from salient_tutor.providers import PROVIDERS

    spec = PROVIDERS.get(name)
    if spec is None or spec.kind != "backend":
        return {"error": f"provider {name!r} is not a probeable backend provider"}
    cached = _probe_cache.get(name)
    if cached and time.monotonic() - cached[0] < _PROBE_TTL:
        return cached[1]
    task = _probe_inflight.get(name)
    if task is None:
        task = asyncio.create_task(_run_probe(name))
        _probe_inflight[name] = task
    # Awaiting a shared task: a cancelled awaiter (client disconnect) detaches
    # without cancelling the probe, so the other callers still get a result.
    return await task


@app.post("/api/agents/config")
async def set_agent_config(req: dict) -> dict:
    """Set one agent's runtime provider/model/effort. Body:
    ``{agent, provider, model?, base_url?, api_key?, auth_style?, effort?}``."""
    if not daemon:
        return {"error": "daemon not started"}
    provider = req.get("provider", "anthropic")
    model = req.get("model", "")
    base_url = req.get("base_url", "")
    # Local (LM Studio): if the operator left the model blank, auto-fill it from
    # the single loaded chat model instead of erroring 'model required' — the
    # common single-model setup then just works, matching the librarian picker.
    if provider == "local" and not model.strip():
        loaded = await _lms_loaded_chat_models(base_url, req.get("api_key", ""))
        if len(loaded) == 1:
            model = loaded[0]
    return daemon.set_agent_config(
        req.get("agent", ""),
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=req.get("api_key", ""),
        auth_style=req.get("auth_style", ""),
        effort=req.get("effort", "med"),
    )


@app.get("/api/lms/models")
async def lms_models(base_url: str = "", api_key: str = "") -> dict:
    """List LM Studio's models WITH loaded-state — the rich view the dropdowns use.

    Probes ``GET {base_url}/api/v0/models`` (LM Studio's native endpoint; the
    OpenAI ``/v1/models`` lists ids only, no state). Returns each model's id,
    type (embeddings/vlm/llm), state (loaded/not-loaded), max context, and
    quantization, so the modal can show a loaded marker and a load/unload toggle.
    FAIL-SAFE: any error → {reachable:false, error}, never a 500."""
    if not daemon:
        return {"error": "daemon not started"}
    import httpx

    resolved = _lms_resolve(base_url, api_key)
    if resolved is None:
        return {"reachable": False, "error": "no base_url configured", "models": []}
    url, key = resolved
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{url}/api/v0/models", headers=headers)
        if resp.status_code != 200:
            return {
                "reachable": False,
                "error": f"server returned HTTP {resp.status_code}",
                "models": [],
            }
        data = resp.json().get("data")
        models = (
            [
                {
                    "id": str(m.get("id")),
                    "type": str(m.get("type", "")),
                    "state": str(m.get("state", "")),
                    "max_context_length": m.get("max_context_length"),
                    "quantization": m.get("quantization"),
                }
                for m in data
                if isinstance(m, dict) and m.get("id")
            ]
            if isinstance(data, list)
            else []
        )
        loaded = [m["id"] for m in models if m.get("state") == "loaded"]
        return {"reachable": True, "models": models, "loaded": loaded, "base_url": url}
    except Exception as e:  # noqa: BLE001 — server-down must NOT 500 the modal
        return {"reachable": False, "error": str(e), "models": [], "loaded": []}


@app.post("/api/lms/load")
async def lms_load(req: dict) -> dict:
    """Load a model into LM Studio memory. Synchronous — blocks until loaded or
    fails (a big model can take minutes). On success returns {ok:true}; on
    failure returns {ok:false, error: <LM Studio's message>} (e.g. OOM / 'no
    room' / corrupt weights) AND the currently-loaded list so the operator can
    see what to unload. Never raises — the load is an attempt, not a guarantee,
    and a 'no room' is operator-actionable info, not a 500."""
    if not daemon:
        return {"error": "daemon not started"}
    import httpx

    resolved = _lms_resolve(req.get("base_url", ""), req.get("api_key", ""))
    if resolved is None:
        return {"ok": False, "error": "no base_url configured", "loaded": []}
    url, key = resolved
    model = (req.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "model is required", "loaded": []}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        # Long timeout: loading a multi-GB model to VRAM is slow.
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
            resp = await client.post(
                f"{url}/api/v1/models/load", json={"model": model}, headers=headers
            )
        if resp.status_code == 200:
            return {"ok": True, "model": model}
        # LM Studio returns its failure reason in the body — surface it verbatim.
        msg = _lms_error(resp)
        return {"ok": False, "model": model, "error": msg, "loaded": await _lms_loaded(url, key)}
    except Exception as e:  # noqa: BLE001 — a failed load is operator info, not a 500
        return {"ok": False, "model": model, "error": str(e), "loaded": []}


@app.post("/api/lms/unload")
async def lms_unload(req: dict) -> dict:
    """Unload a model instance from LM Studio memory. Returns {ok:true} or
    {ok:false, error}. Never raises."""
    if not daemon:
        return {"error": "daemon not started"}
    import httpx

    resolved = _lms_resolve(req.get("base_url", ""), req.get("api_key", ""))
    if resolved is None:
        return {"ok": False, "error": "no base_url configured"}
    url, key = resolved
    model = (req.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "model is required"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            # LM Studio keys the instance by the model id.
            resp = await client.post(
                f"{url}/api/v1/models/unload",
                json={"instance_id": model},
                headers=headers,
            )
        if resp.status_code == 200:
            return {"ok": True, "model": model}
        return {"ok": False, "model": model, "error": _lms_error(resp)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "model": model, "error": str(e)}


def _lms_error(resp: httpx.Response) -> str:
    """Pull a human-readable message out of an LM Studio error response."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict) and body["error"].get("message"):
            return str(body["error"]["message"])
        if isinstance(body.get("error"), str):
            return body["error"]
        if body.get("message"):
            return str(body["message"])
    return f"HTTP {resp.status_code}"


async def _lms_loaded(url: str, key: str) -> list[str]:
    """The currently-loaded model ids (best-effort; [] on any failure) — shown
    alongside a load failure so the operator can see what to unload."""
    import httpx

    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{url}/api/v0/models", headers=headers)
        data = resp.json().get("data") if resp.status_code == 200 else None
        if isinstance(data, list):
            return [
                str(m["id"]) for m in data if isinstance(m, dict) and m.get("state") == "loaded"
            ]
    except Exception:  # noqa: BLE001
        pass
    return []


async def _lms_loaded_chat_models(base_url: str, api_key: str) -> list[str]:
    """Loaded LM Studio models that can serve chat (i.e. not embeddings),
    best-effort. Lets the per-agent config auto-fill the model for a local
    provider when the operator left it blank and exactly one chat model is
    loaded — so 'model required' doesn't block the common single-model setup."""
    import httpx

    resolved = _lms_resolve(base_url, api_key)
    if resolved is None:
        return []
    url, key = resolved
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{url}/api/v0/models", headers=headers)
        data = resp.json().get("data") if resp.status_code == 200 else None
        if isinstance(data, list):
            return [
                str(m["id"])
                for m in data
                if isinstance(m, dict)
                and m.get("state") == "loaded"
                and str(m.get("type", "")) != "embeddings"
                and m.get("id")
            ]
    except Exception:  # noqa: BLE001
        pass
    return []


app.mount("/js", StaticFiles(directory=_STATIC_DIR / "js"), name="js")
app.mount("/css", StaticFiles(directory=_STATIC_DIR / "css"), name="css")
app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")


def main() -> None:
    parser = argparse.ArgumentParser(prog="salient-tutor-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--work-root",
        default=None,
        help="workspace directory (chats/KG/gradebook/images). Default <repo>/work; "
        "point at another dir for an isolated profile. Overrides $TUTOR_WORK_ROOT.",
    )
    args = parser.parse_args()
    if args.work_root:
        os.environ["TUTOR_WORK_ROOT"] = args.work_root

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        # Tolerant WS keepalive: ping every 20s, but allow 120s for a pong before
        # closing (1011). A slow/intermittent network or a busy event loop during
        # a long turn no longer kills the in-flight connection. Paired with the
        # app-level heartbeat in ws_tutor (a 15s no-op ping) so the connection is
        # never genuinely idle even during a minutes-long local-model parse.
        ws_ping_interval=20,
        ws_ping_timeout=120,
    )


if __name__ == "__main__":
    main()
