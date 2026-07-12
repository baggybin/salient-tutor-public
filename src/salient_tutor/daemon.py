"""Minimal tutor daemon — composes salient-core kernel pieces.

Implements the DaemonServices protocol so the bus and runner can interact
with it. Holds the persistent state (ContextStore, KnowledgeGraph,
QuestionInbox, ActionLedger) and manages agent runners.

Usage:
    daemon = TutorDaemon(work_root="work")
    await daemon.start()
    result = await daemon.prompt("tutor", "teach me about photosynthesis")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from salient_core import (
    ActionLedger,
    AgentRunner,
    ContextStore,
    EventHub,
    KnowledgeGraph,
    LocalClaudeBackend,
    QuestionInbox,
    bucketed_profile,
    make_bus,
)

from salient_tutor.lesson import LessonController
from salient_tutor.lesson_store import LessonStore
from salient_tutor.pedagogy import import_bundle
from salient_tutor.providers import PROVIDERS, resolve_thinking

_log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
# Full image-authoring rubric appended to image-authoring agents' system prompts
# only when illustrations are enabled — occasional feature, so it's off the
# always-on path when disabled (the tutor prompt keeps a compressed floor).
_IMAGE_SKILL_PATH = _PROMPTS_DIR / "skills" / "image_authoring.md"
# Method-of-loci memory-palace authoring, appended after the image skill (it
# extends it). Same gate: only when illustrations are available, since a palace
# renders one loci image per locus.
_PALACE_SKILL_PATH = _PROMPTS_DIR / "skills" / "palace_authoring.md"
_IMAGE_SKILL_AGENTS: frozenset[str] = frozenset({"tutor", "tutor_alt", "judge"})
_BUNDLE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "pedagogy_bundle.json"
_CURRICULA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "curricula"
_CURRICULUM_AGENT = "curriculum"


def _expand_envvars(value: Any) -> Any:
    """Resolve ``${VAR}`` references in a string against ``os.environ``.

    Ported from salient-core's helper of the same name (used by its per-agent
    endpoint-override block) so the librarian's local-endpoint credentials can
    reference env vars the same way. Non-strings pass through; unresolved refs
    stay literal."""
    if not isinstance(value, str) or "$" not in value:
        return value
    import re

    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value
    )


# The learner gradebook lives under one KG subject; mastery facts are recorded
# by the tutor via record_review (predicate strong_topic/weak_topic) + the
# misconception predicate. Kept in sync with prompts/tutor.md.
LEARNER_SUBJECT = "learner:op"

# Pedagogy-filter strictness → how aggressively the judge pares back a leaked
# tutor answer. Threaded into _PEDAGOGY_FILTER_PROMPT; surfaced as the operator's
# strictness dial. Keys are the wire values the web layer validates against.
_STRICTNESS_LEVELS: dict[str, str] = {
    "explain": (
        "LENIENT. A full explanation of concepts is fine. Count it leaked ONLY if "
        "the draft hands over a ready-to-run, step-by-step solution the learner "
        "was meant to derive themselves."
    ),
    "socratic": (
        "DEFAULT. Count it leaked if the draft reveals the answer or the concrete "
        "steps to it. Rewrite a leak to an orient/narrow hint or a Socratic "
        "question that points at the next move without performing it."
    ),
    "bare": (
        "STRICT. Count it leaked if the draft reveals the method at all. Rewrite a "
        "leak to the single minimal orient hint; never state the technique, tool, "
        "or steps."
    ),
}

_PEDAGOGY_FILTER_PROMPT = (
    "PEDAGOGY FILTER (Mode B). A tutor drafted the reply below to a learner's "
    "question. Enforce two things, in order:\n\n"
    "1. ATTEMPT-FIRST. attempt_pending={attempt_pending}. If it is False AND the "
    "question is a problem/exercise/'how do I' the learner should TRY before being "
    "taught, set needs_attempt=true and make `revised` a SHORT, warm probing "
    "question that elicits their current read or best guess — no teaching, no "
    "steps, no answer. A conceptual/'what is' question is exempt (needs_attempt "
    "false). If attempt_pending is True the learner has just attempted — NEVER set "
    "needs_attempt; proceed to step 2.\n\n"
    "2. NO LEAK. If not eliciting an attempt, decide whether the draft LEAKS the "
    "solution the learner should derive, at this strictness level:\n"
    "  level: {level} — {rubric}\n"
    "If leaked, rewrite `revised` to the allowed hint level, preserving the "
    "tutor's warm voice and any diagram scaffolding that doesn't give away the "
    "answer. If not leaked, `revised` is the draft unchanged.\n\n"
    "`revised` is always the exact text to show the learner (attempt-elicitation, "
    "hint, or the draft). Respond with STRICT JSON only, no prose, no code "
    "fences:\n"
    '{{"needs_attempt": <true|false>, "leaked": <true|false>, '
    '"revised": "<the reply to show the learner>"}}\n\n'
    "LEARNER QUESTION:\n{question}\n\n"
    "TUTOR DRAFT:\n{draft}"
)

# Retrieval micro-quiz: the tutor generates ONE question then grades the learner's
# recall on the four-button SM-2 scale (see prompts/tutor.md "LEARNER MEMORY").
_QUIZ_GEN_PROMPT = (
    "RETRIEVAL QUIZ. Write ONE focused retrieval question that tests recall (not "
    "recognition) of the topic below, plus a concise reference answer. Pull from "
    "your knowledge base / tools as needed. One question only — short, answerable "
    "from memory in a few sentences.\n\n"
    "Respond with STRICT JSON only, no prose, no code fences:\n"
    '{{"question": "<the question>", "answer": "<the concise reference answer>"}}\n\n'
    "TOPIC:\n{topic}"
)
_QUIZ_GRADE_PROMPT = (
    "RETRIEVAL GRADING. Grade the learner's answer to a retrieval question on "
    "'{topic}'. Grade the RETRIEVAL, not the lesson, on the four-button scale:\n"
    "  again — blanked or wrong (a lapse)\n"
    "  hard  — recalled with effort or partially\n"
    "  good  — clean, unaided recall\n"
    "  easy  — trivial, complete recall\n"
    "Give ONE or two sentences of warm, specific feedback (what was right, what to "
    "firm up).\n\n"
    "Respond with STRICT JSON only, no prose, no code fences:\n"
    '{{"grade": "<again|hard|good|easy>", "feedback": "<one-two sentences>"}}\n\n'
    "QUESTION:\n{question}\n\n"
    "REFERENCE ANSWER:\n{answer}\n\n"
    "LEARNER'S ANSWER:\n{learner_answer}"
)

# Prerequisite-DAG skill map: tutor-inferred prereq edges persisted under the
# curriculum: KG namespace (subject/object both prefixed), predicate prereq_of.
_CURRICULUM_PREFIX = "curriculum:inferred:"
_PREREQ_PREDICATE = "prereq_of"
_SKILL_GRAPH_PROMPT = (
    "PREREQUISITE GRAPH. The learner has studied these topics:\n{topics}\n\n"
    "Build a prerequisite graph over them: an edge [A, B] means A should be "
    "learned BEFORE B (A is a prerequisite of B). Only include edges you are "
    "confident about — a sparse, correct DAG beats a dense guess. You MAY add up "
    "to 5 sensible NEXT topics these lead toward (not yet studied) and edges into "
    "them. No cycles. Use the exact topic wording given.\n\n"
    "Respond with STRICT JSON only, no prose, no code fences:\n"
    '{{"edges": [["prereq", "dependent"], ...], "next": ["topic", ...]}}'
)

_AGENT_CONFIGS: dict[str, dict[str, Any]] = {
    "tutor": {
        "system_prompt_file": "tutor.md",
        "model": os.environ.get("TUTOR_MODEL", "claude-opus-4-8[1m]"),
        "builtin_tools": []
        if os.environ.get("TUTOR_PROVIDER") == "deepseek"
        else ["WebSearch", "WebFetch"],
        "max_turns": 30,
        "family": "tutor",
        "label": "tutor",
    },
    "websearch": {
        "system_prompt_file": "websearch.md",
        "model": os.environ.get("TUTOR_WEBSEARCH_MODEL", "claude-sonnet-5[1m]"),
        "builtin_tools": ["WebSearch", "WebFetch"],
        "max_turns": 8,
        # One-shot lookup leaf — tutors reach it via ask_agent (tutor.md's
        # KB-discipline step 5); it never delegates back, so no bus server
        # (same context-size rationale as the librarian below).
        "bus_tools": False,
    },
    "librarian": {
        "system_prompt_file": "librarian.md",
        "model": os.environ.get("TUTOR_LIBRARIAN_MODEL", "claude-sonnet-5[1m]"),
        "builtin_tools": ["Read"],
        "max_turns": 20,
        "confine_reads_to_study": True,
        # The librarian is a one-shot extractor: its prompt forbids every bus
        # tool (no context/delegation/kg/etc.). Attaching the bus MCP server
        # only adds ~6K tokens of tool schemas to every request — which
        # overflows the small context window (often 4K) that local LM Studio
        # models are loaded with, causing HTTP 500 "tokens to keep ... greater
        # than the context length". So the librarian runs bus-less.
        "bus_tools": False,
    },
}


# Env-var provider override per agent — lets ANY agent be routed to a
# non-Anthropic endpoint (deepseek/minimax/local) from startup env, without the
# 🤖 Agents tab. Mirrors the persisted agent_configs.json block; a persisted
# block always wins. base_url falls back to the provider-registry default and
# the api_key is inherited from the process env unless a per-agent one is given.
_PROVIDER_ENV: dict[str, str] = {
    "tutor": "TUTOR_PROVIDER",
    "tutor_alt": "TUTOR_VARIANT_PROVIDER",
    "judge": "TUTOR_JUDGE_PROVIDER",
    "librarian": "TUTOR_LIBRARIAN_PROVIDER",
    "websearch": "TUTOR_WEBSEARCH_PROVIDER",
}

# Agents that don't exist in the base roster but can be created purely from the
# persisted config (a runtime block carrying a model) — the same two _build_
# agent_configs registers. set_agent_config accepts these even when they aren't
# live yet, so the 🤖 Agents tab can add/remove them without env vars.
_OPTIONAL_AGENTS: frozenset[str] = frozenset({"judge", "tutor_alt"})


def _build_agent_configs(
    runtime: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Agent roster for one daemon, with an optional shadow tutor + judge.

    The two optional agents (``tutor_alt``, ``judge``) are registered when
    EITHER a startup env registers them (``TUTOR_VARIANT_MODEL`` /
    ``TUTOR_JUDGE_MODEL``) OR the persisted ``agent_configs.json`` block
    (``runtime``) carries a ``model`` for them — so the whole roster can be
    driven from the config file alone, no env required. Env wins for the model
    when both are set. With neither the roster is just tutor + librarian +
    websearch.

    ``tutor_alt`` runs the SAME lesson-loop prompt on a different model; the web
    modal's variant picker then lets the operator switch which tutor answers
    (one at a time), and it stays hidden when tutor_alt is absent.
    """
    runtime = runtime or {}
    configs = {name: dict(cfg) for name, cfg in _AGENT_CONFIGS.items()}

    def _runtime_model(agent: str) -> str:
        return ((runtime.get(agent) or {}).get("model") or "").strip()

    def _runtime_provider(agent: str) -> str:
        return ((runtime.get(agent) or {}).get("provider") or "").strip()

    variant_model = (os.environ.get("TUTOR_VARIANT_MODEL", "") or "").strip() or _runtime_model(
        "tutor_alt"
    )
    if variant_model:
        variant_provider = (
            os.environ.get("TUTOR_VARIANT_PROVIDER") or _runtime_provider("tutor_alt") or ""
        )
        configs["tutor_alt"] = {
            "system_prompt_file": "tutor.md",
            "model": variant_model,
            "builtin_tools": [] if variant_provider == "deepseek" else ["WebSearch", "WebFetch"],
            "max_turns": 30,
            "family": "tutor",
            "label": os.environ.get("TUTOR_VARIANT_LABEL", "").strip() or variant_model,
            # Marks tutor_alt as tutor's shadow so the kernel's consensus panel
            # (resolve_panel) can pair them natively.
            "substitute_for": "tutor",
        }
    judge_model = (os.environ.get("TUTOR_JUDGE_MODEL", "") or "").strip() or _runtime_model("judge")
    if judge_model:
        configs["judge"] = {
            "system_prompt_file": "judge.md",
            "model": judge_model,
            "builtin_tools": [],
            "max_turns": 4,
        }
    return configs


class TutorDaemon:
    """Minimal daemon for the tutor showcase.

    Implements DaemonServices (profile, engagement_path, context, kg, inbox,
    add_question) so the bus tools and runner can interact with it.
    """

    def __init__(self, work_root: str | Path | None = None) -> None:
        self.work_root = Path(work_root or Path.cwd() / "work")
        self.work_root.mkdir(parents=True, exist_ok=True)

        self.profile: dict[str, Any] = {}
        self.engagement_path: Path | None = None

        # Operator-settable embeddings config (the gear modal writes here). The
        # file is the runtime override; salient-core's resolve_config reads the
        # `embeddings:` profile block FIRST, then falls back to SALIENT_EMBED_*
        # env vars — so an absent/empty block lets the deploy env drive again.
        self._embed_config_path = self.work_root / "embed_config.json"
        self._load_embed_profile()

        # Operator-settable per-agent provider config (the 🤖 Agents tab). Each
        # agent can run on anthropic (inherited env, default) or be rerouted at a
        # non-Anthropic endpoint (deepseek/minimax/local). Persisted to
        # work/agent_configs.json; the legacy librarian_config.json is migrated
        # on first load.
        self._librarian_config_path = self.work_root / "librarian_config.json"
        self._agent_config_path = self.work_root / "agent_configs.json"
        self._agent_runtime = self._load_agent_runtime()

        self.context = ContextStore(self.work_root / "context.db")
        self.kg = KnowledgeGraph(self.work_root / "kg.db")
        # QuestionInbox takes the ContextStore (its persistence backing), NOT a
        # db path — questions persist into context.db's `questions` table. Passing
        # a path here made every inbox.add() blow up with
        # "'PosixPath' object has no attribute 'record_question'".
        self.inbox = QuestionInbox(self.context)
        self.actions = ActionLedger(self.work_root / "actions.db")
        self.lesson_store = LessonStore(self.work_root / "lessons.db")
        self.lessons = LessonController(self.lesson_store)

        self.event_hub = EventHub()
        self.agent_configs = _build_agent_configs(self._agent_runtime)
        self._seed_providers_from_env()
        # Read by the kernel's consensus/delegation cycle check (`_bus_calls or {}`);
        # the tutor never registers bus calls, so it stays empty.
        self._bus_calls: dict[str, Any] = {}
        self.runners: dict[str, AgentRunner] = {}
        self._tasks: list[asyncio.Task] = []
        self._pedagogy_seeded = False
        self._curricula_seeded = False

    # ── live event observation (DaemonServices seam) ──────────────────
    # The documented attach point for observers — the web overlay, a tailer,
    # or a downstream relay — delegating to our own EventHub. Kept as methods
    # (not a raw `event_hub` reference) so the hub stays a single swappable
    # seam; salient-core's `_EventObservationMixin` is the same two-method
    # shape for daemons assembled from the kernel mixins.
    def subscribe_events(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        return self.event_hub.subscribe()

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        self.event_hub.unsubscribe(q)

    def _seed_pedagogy(self) -> None:
        """Ingest the mnemonic KG bundle into the pedagogy: namespace.

        Idempotent: assert_fact max-merges, so re-seeding corroborates
        rather than duplicates. Skips if already seeded or bundle missing.
        """
        if self._pedagogy_seeded:
            return
        if not _BUNDLE_PATH.exists():
            _log.warning("pedagogy bundle not found at %s — skipping seed", _BUNDLE_PATH)
            return
        try:
            stats = import_bundle(
                self.kg,
                bundle_path=_BUNDLE_PATH,
                source_root=_BUNDLE_PATH.parent,
                with_prose=False,
            )
            self._pedagogy_seeded = True
            _log.info(
                "pedagogy KG seeded: %d facts, %d edges, %d chunks",
                stats.get("facts", 0),
                stats.get("edges", 0),
                stats.get("chunks", 0),
            )
        except Exception:
            _log.exception("pedagogy KG seed failed")

    # ── Curricula seed (private data/curricula/*.json) ────────────────

    def _seed_curricula(self) -> None:
        """Ingest every curriculum track in ``data/curricula/`` into the
        ``curriculum:track:<id>:`` KG namespace.

        Idempotent: ``assert_fact`` max-merges, so re-seeding on each startup
        corroborates rather than duplicates. Skips if already seeded or the
        directory is missing (e.g. public mirror checkout, where
        ``.gitattributes export-ignore`` strips the dir).
        """
        if self._curricula_seeded:
            return
        if not _CURRICULA_DIR.is_dir():
            _log.warning("curricula dir not found at %s — skipping seed", _CURRICULA_DIR)
            return
        total_facts = 0
        total_topics = 0
        for path in sorted(_CURRICULA_DIR.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                facts, topics = self._ingest_curriculum_track(data)
                total_facts += facts
                total_topics += topics
            except Exception:
                _log.exception("curriculum seed failed for %s", path)
        if total_facts:
            self._curricula_seeded = True
            _log.info(
                "curricula KG seeded: %d facts across %d topics",
                total_facts,
                total_topics,
            )

    def _ingest_curriculum_track(self, data: dict[str, Any]) -> tuple[int, int]:
        """Write one curriculum track's modules + topics into the KG.

        Returns (facts_written, topics_count). Namespace layout::

            curriculum:track:<track_id>                          track meta
            curriculum:track:<track_id>:module:<module_id>        module meta
            curriculum:track:<track_id>:module:<mod>:topic:<top>  topic facts
        """
        kg = self.kg
        a = _CURRICULUM_AGENT
        track_id = str(data.get("track") or "unknown")
        ts = f"curriculum:track:{track_id}"
        facts = 0

        for key, pred in (("title", "title"), ("description", "description")):
            if v := data.get(key):
                kg.assert_fact(ts, pred, str(v), agent=a, expires_at=None)
                facts += 1

        topics = 0
        for mod in data.get("modules") or []:
            if not isinstance(mod, dict):
                continue
            mod_id = str(mod.get("id") or "").strip()
            if not mod_id:
                continue
            ms = f"{ts}:module:{mod_id}"
            for key, pred in (
                ("title", "title"),
                ("difficulty", "difficulty"),
                ("objective", "objective"),
            ):
                if v := mod.get(key):
                    kg.assert_fact(ms, pred, str(v), agent=a, expires_at=None)
                    facts += 1
            for prereq in mod.get("prerequisites") or []:
                if prereq:
                    kg.assert_fact(ms, "prerequisite", str(prereq), agent=a, expires_at=None)
                    facts += 1

            for topic in mod.get("topics") or []:
                if not isinstance(topic, dict):
                    continue
                topic_id = str(topic.get("id") or "").strip()
                if not topic_id:
                    continue
                ss = f"{ms}:topic:{topic_id}"
                topics += 1

                if v := topic.get("title"):
                    kg.assert_fact(ss, "title", str(v), agent=a, expires_at=None)
                    facts += 1
                for concept in topic.get("concepts") or []:
                    kg.assert_fact(ss, "concept", str(concept), agent=a, expires_at=None)
                    facts += 1
                for kf in topic.get("key_facts") or []:
                    kg.assert_fact(ss, "key_fact", str(kf), agent=a, expires_at=None)
                    facts += 1
                for at in topic.get("attack_techniques") or []:
                    kg.assert_fact(ss, "attack_technique", str(at), agent=a, expires_at=None)
                    facts += 1
                if kc := topic.get("killchain_stage"):
                    kg.assert_fact(ss, "killchain_stage", str(kc), agent=a, expires_at=None)
                    facts += 1

                facts += self._ingest_topic_pairing(ss, topic)

                for drill in topic.get("drills") or []:
                    kg.assert_fact(ss, "drill", str(drill), agent=a, expires_at=None)
                    facts += 1
                for cm in topic.get("common_mistakes") or []:
                    kg.assert_fact(ss, "common_mistake", str(cm), agent=a, expires_at=None)
                    facts += 1

        return facts, topics

    def _ingest_topic_pairing(self, ss: str, topic: dict[str, Any]) -> int:
        """Write the offense-defense pairing (the 'unicorn rule') for a topic.

        Handles all four curriculum schemas uniformly:
        - red-team ``defense_pairing``
        - blue-team ``offense_pairing``
        - cyber-fundamentals flat ``offense_application`` / ``defense_application``
        - purple-team structured ``attack_side`` / ``defense_side``
        """
        kg = self.kg
        a = _CURRICULUM_AGENT
        facts = 0

        def _fact(pred: str, obj: Any) -> None:
            nonlocal facts
            if obj:
                kg.assert_fact(ss, pred, str(obj), agent=a, expires_at=None)
                facts += 1

        def _facts(pred: str, items: Any) -> None:
            nonlocal facts
            for item in items or []:
                _fact(pred, item)

        # Red-team: defense_pairing
        if dp := topic.get("defense_pairing"):
            _fact("defense_summary", dp.get("what_blue_sees"))
            _facts("defense_log", dp.get("logs"))
            _facts("defense_detection", dp.get("detections"))
            _facts("defense_control", dp.get("controls"))

        # Blue-team: offense_pairing
        if op := topic.get("offense_pairing"):
            _fact("offense_summary", op.get("what_red_does"))
            _fact("offense_perspective", op.get("attacker_perspective"))
            _facts("offense_bypass", op.get("bypass_attempts"))

        # Cyber-fundamentals: flat string fields
        _fact("offense_summary", topic.get("offense_application"))
        _fact("defense_summary", topic.get("defense_application"))

        # Purple-team: attack_side + defense_side
        if atk := topic.get("attack_side"):
            _fact("offense_summary", atk.get("technique"))
            _facts("offense_step", atk.get("attack_steps"))
            _facts("offense_tool", atk.get("tools"))
            _facts("attack_technique", atk.get("attack_techniques"))
            _fact("killchain_stage", atk.get("killchain_stage"))

        if dfn := topic.get("defense_side"):
            _fact("defense_summary", dfn.get("what_to_detect"))
            _facts("defense_log", dfn.get("logs"))
            _facts("defense_detection", dfn.get("detection_rules"))
            _facts("defense_tool", dfn.get("tools"))
            _facts("defense_control", dfn.get("controls"))

        # Purple-team: gap_analysis
        _fact("gap_analysis", topic.get("gap_analysis"))

        return facts

    # ── Embeddings config + backfill ──────────────────────────────────
    # salient-core ships a provider-agnostic OpenAI-compatible embedder
    # (memory/embeddings.py) + the storage helpers (kg.py: facts_needing_embedding
    # / store_embeddings / embedding_counts / semantic_query). The operator daemon
    # drives the backfill off its bus-call reaper; the minimal TutorDaemon has no
    # such reaper, so until now `study.py`'s `passage` facts were never vectorized
    # and semantic_recall stayed inert here. We run a dedicated backfill loop and
    # expose the config so the operator can point it at e.g. http://ai.home:1234.

    _EMBED_CONFIG_PATH = "embed_config.json"

    def _load_embed_profile(self) -> None:
        """Load the operator-saved embeddings block (if any) into the profile.

        Inert on a fresh work root (no file). ``resolve_config`` checks the
        ``embeddings:`` block first, then ``SALIENT_EMBED_*`` env, so an absent
        block means the deploy environment drives the embedder."""
        try:
            if self._embed_config_path.exists():
                import json as _json

                block = _json.loads(self._embed_config_path.read_text())
                if isinstance(block, dict):
                    self.profile["embeddings"] = block
        except Exception:
            _log.exception("embed config load failed — ignoring %s", self._embed_config_path)

    def embed_config(self) -> dict[str, Any]:
        """Resolved embeddings config for the web gear modal.

        Returns the model/base_url (api_key masked to present/absent), an
        ``enabled`` flag (true iff a full config resolved), and KG coverage
        ``{total, embedded, pending}`` under the resolved model (empty when no
        embedder is configured)."""
        from salient_core.memory.embeddings import resolve_config

        cfg = resolve_config(self.profile)
        if cfg is None:
            return {"enabled": False, "base_url": "", "model": "", "api_key": False, "coverage": {}}
        total, embedded, pending = self.kg.embedding_counts(cfg.model)
        return {
            "enabled": True,
            "base_url": cfg.base_url,
            "model": cfg.model,
            # Never echo the secret back — just whether one is set.
            "api_key": bool(cfg.api_key),
            "coverage": {"total": total, "embedded": embedded, "pending": pending},
        }

    def set_embed_config(self, *, base_url: str, model: str, api_key: str) -> dict[str, Any]:
        """Set (or clear, when all empty) the embeddings config from the modal.

        Persisted to ``work/embed_config.json`` so it survives a restart and
        overrides ``SALIENT_EMBED_*`` env. Clearing every field removes the block
        and the file, reverting to the deploy default. The embedder cache is
        flushed so the new endpoint + the backfill loop pick it up next pass."""
        import json as _json

        from salient_core.memory.embeddings import clear_embedder_cache

        base_url = (base_url or "").strip()
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        if not base_url and not model and not api_key:
            # Revert to env-driven config: drop the block + delete the file.
            self.profile.pop("embeddings", None)
            self._embed_config_path.unlink(missing_ok=True)
            clear_embedder_cache()
            return self.embed_config()
        # A full config needs both base_url and model; an api_key alone is useless.
        if not base_url or not model:
            return {"error": "base_url and model are required (or leave all blank to clear)"}
        block = {"base_url": base_url, "model": model}
        if api_key:
            block["api_key"] = api_key
        self.profile["embeddings"] = block
        try:
            self._embed_config_path.write_text(_json.dumps(block))
        except Exception:
            _log.exception("embed config persist failed")
        clear_embedder_cache()
        return self.embed_config()

    # ── Per-agent provider config (Claude ↔ DeepSeek/MiniMax/local) ──────
    # Every agent can be routed at a non-Anthropic endpoint (DeepSeek, MiniMax,
    # or a local LM Studio server) while others stay on Claude. The override is
    # per-agent: only that agent's SDK subprocess gets the rerouted env. Ported
    # from salient-core's endpoint: block (_runner_factory.py), generalized from
    # the original librarian-only form. The provider policy (auth style, thinking
    # support, tool disabling) lives in salient_tutor.providers.

    def _load_agent_runtime(self) -> dict[str, dict[str, Any]]:
        """Load the operator-saved per-agent runtime config, keyed by agent name.
        Each value is {provider, model?, base_url?, api_key?, auth_style?, effort?}.
        Agents absent from the file default to ``{provider: anthropic}`` (inherited
        env). Migrated from the old librarian_config.json on first load."""
        import json as _json

        out: dict[str, dict[str, Any]] = {}
        # Migrate the legacy librarian_config.json (provider was "claude"|"local").
        try:
            if self._librarian_config_path.exists():
                legacy = _json.loads(self._librarian_config_path.read_text())
                if isinstance(legacy, dict):
                    p = "local" if legacy.get("provider") == "local" else "anthropic"
                    block = {"provider": p}
                    if p == "local":
                        block.update(
                            base_url=legacy.get("base_url", ""),
                            model=legacy.get("model", ""),
                            auth_style=legacy.get("auth_style", "api_key"),
                        )
                        if legacy.get("api_key"):
                            block["api_key"] = legacy["api_key"]
                    out["librarian"] = block
                    self._librarian_config_path.unlink(missing_ok=True)
        except Exception:
            _log.exception("legacy librarian config migration failed — ignoring")
        try:
            if self._agent_config_path.exists():
                data = _json.loads(self._agent_config_path.read_text())
                if isinstance(data, dict):
                    for name, block in data.items():
                        if isinstance(block, dict):
                            out[name] = dict(block)
        except Exception:
            _log.exception("agent config load failed — ignoring %s", self._agent_config_path)
        return out

    def _runtime_for(self, agent: str) -> dict[str, Any]:
        """The runtime config block for one agent (defaults to anthropic)."""
        cfg = self._agent_runtime.get(agent) or {}
        return {
            "provider": cfg.get("provider", "anthropic"),
            **{k: v for k, v in cfg.items() if k != "provider"},
        }

    def _seed_providers_from_env(self) -> None:
        """Seed each agent's runtime provider from its ``TUTOR_*_PROVIDER`` env.

        Generalizes the per-agent endpoint override — previously reachable only
        via the persisted 🤖 Agents tab — to startup env, so any agent (tutor,
        tutor_alt, judge, librarian) can be routed to deepseek/minimax/local
        without the UI. ``base_url`` falls back to the provider-registry default
        and the endpoint's api_key is inherited from the process env
        (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN) unless ``<ENV>_KEY`` is set. A
        persisted *endpoint* choice (deepseek/minimax/local) always wins over
        env; a persisted ``anthropic`` block is only the default (inherited env,
        no endpoint), so env may upgrade it — preserving its effort. Unknown
        providers and ``anthropic`` are no-ops.
        """
        for agent, env_name in _PROVIDER_ENV.items():
            if agent not in self.agent_configs:
                continue
            provider = (os.environ.get(env_name) or "").strip().lower()
            if not provider or provider == "anthropic":
                continue
            if provider not in PROVIDERS:
                _log.warning("ignoring %s=%r (unknown provider)", env_name, provider)
                continue
            existing = self._agent_runtime.get(agent)
            if existing and existing.get("provider", "anthropic") != "anthropic":
                continue  # explicit persisted endpoint choice wins over env
            effort = (existing or {}).get("effort", "med")
            block: dict[str, Any] = {"provider": provider, "effort": effort}
            if PROVIDERS[provider].kind != "backend":
                # Endpoint fields only apply to the ANTHROPIC_BASE_URL reroute
                # providers; a backend provider (codex) authenticates itself.
                base_url = (os.environ.get(f"{env_name}_BASE_URL") or "").strip()
                if base_url:
                    block["base_url"] = base_url
                api_key = (os.environ.get(f"{env_name}_KEY") or "").strip()
                if api_key:
                    block["api_key"] = api_key
            self._agent_runtime[agent] = block
            _log.info("agent %s routed to %s via %s", agent, provider, env_name)

    def agent_config(self, agent: str) -> dict[str, Any]:
        """Resolved runtime config for one agent for the gear modal. The api_key
        is masked to a presence flag; endpoint fields are zeroed when the
        provider is anthropic (inherited env)."""
        if agent not in self.agent_configs:
            return {"error": f"unknown agent {agent}"}
        cfg = self._runtime_for(agent)
        provider = cfg.get("provider", "anthropic")
        spec = PROVIDERS.get(provider)
        needs = bool(spec and spec.needs_endpoint)
        return {
            "provider": provider,
            # Model applies to every provider (endpoint model, or the per-agent
            # Anthropic model override); only base_url/api_key are endpoint-only.
            "model": cfg.get("model", ""),
            "base_url": cfg.get("base_url", "") if needs else "",
            "api_key": bool(cfg.get("api_key")) if needs else False,
            "auth_style": cfg.get("auth_style", spec.auth_style if spec else "api_key"),
            "effort": cfg.get("effort", "med"),
        }

    def all_agent_configs(self) -> dict[str, dict[str, Any]]:
        """Every agent's resolved runtime config (for the 🤖 Agents tab)."""
        return {name: self.agent_config(name) for name in self.agent_configs}

    def set_agent_config(
        self,
        agent: str,
        *,
        provider: str,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        auth_style: str = "",
        effort: str = "med",
    ) -> dict[str, Any]:
        """Set (or clear to anthropic) one agent's runtime provider config.

        ``provider="anthropic"`` reverts to inherited env. Any endpoint provider
        (deepseek/minimax/local) requires base_url + model. Persists to
        work/agent_configs.json and rebuilds the agent's runner (the SDK binds
        env at subprocess spawn). Validates provider + effort against the
        registry. When ``api_key`` is blank and the agent already had one, the
        existing key is kept (so a re-save without re-typing the secret works)."""
        from salient_tutor.providers import EFFORTS

        # The two optional agents (judge/tutor_alt) may be configured before they
        # are live — set_agent_config is how they get created (and persisted).
        if agent not in self.agent_configs and agent not in _OPTIONAL_AGENTS:
            return {"error": f"unknown agent {agent}"}
        provider = (provider or "anthropic").strip().lower()
        if provider not in PROVIDERS:
            return {"error": f"unknown provider {provider!r}"}
        effort = (effort or "med").strip().lower()
        if effort not in EFFORTS:
            return {"error": f"unknown effort {effort!r}"}
        spec = PROVIDERS[provider]
        model = (model or "").strip()

        # An optional agent with provider=anthropic and no model has nothing to
        # run (it has no roster default), so this is its "remove" gesture — the
        # inverse of adding it: drop the block, de-register, persist, done.
        if agent in _OPTIONAL_AGENTS and provider == "anthropic" and not model:
            self._agent_runtime.pop(agent, None)
            self.agent_configs.pop(agent, None)
            self._persist_agent_runtime()
            self._rebuild_runner(agent)
            return {"provider": "anthropic", "removed": True, "agent": agent}

        newly_registered = agent not in self.agent_configs

        if provider == "anthropic":
            # Revert to inherited env, but KEEP the effort dial — it still drives
            # the Anthropic thinking budget in _build_options — and an optional
            # per-agent Claude model (opus/sonnet/fable). Store a minimal block
            # (no endpoint fields) so the setting survives a reload.
            block = {"provider": "anthropic", "effort": effort}
            if model:
                block["model"] = model
            self._agent_runtime[agent] = block
        elif spec.kind == "backend":
            # A backend provider (codex) has no endpoint fields — auth is
            # OPENAI_API_KEY or an existing `codex login` session, and the
            # model is optional for roster agents (the tier map fills it). An
            # optional agent being CREATED here still needs a model, since the
            # roster registers judge/tutor_alt only when one is configured.
            if agent not in self.agent_configs and not model:
                return {
                    "error": f"model is required to create optional agent {agent!r}"
                    f" on provider {provider!r}"
                }
            block = {"provider": provider, "effort": effort}
            if model:
                block["model"] = model
            self._agent_runtime[agent] = block
        else:
            base_url = (base_url or spec.default_base_url).strip().rstrip("/")
            api_key = (api_key or "").strip()
            if not base_url or not model:
                return {"error": f"base_url and model are required for provider {provider!r}"}
            block: dict[str, Any] = {
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "auth_style": "bearer"
                if (auth_style or spec.auth_style) == "bearer"
                else "api_key",
                "effort": effort,
            }
            if api_key:
                block["api_key"] = api_key
            elif (
                agent in self._agent_runtime
                and self._agent_runtime[agent].get("provider") == provider
                and self._agent_runtime[agent].get("api_key")
            ):
                # Keep the stored secret on a blank re-save ONLY when the provider
                # is unchanged. On a provider SWITCH a blank field must mean "no
                # key" (fall back to this provider's key env) — otherwise the old
                # provider's key (e.g. an LM Studio 'lmstudio') carries over and
                # overrides MINIMAX_API_KEY, producing a 401 against the new host.
                block["api_key"] = self._agent_runtime[agent][
                    "api_key"
                ]  # keep existing (same provider)
            self._agent_runtime[agent] = block

        # A just-created optional agent (judge/tutor_alt) must join the live
        # roster so it can be built + run this session, not only after a restart.
        if newly_registered:
            self.agent_configs = _build_agent_configs(self._agent_runtime)

        self._persist_agent_runtime()
        self._rebuild_runner(agent)
        return self.agent_config(agent)

    def _persist_agent_runtime(self) -> None:
        """Write the runtime map to work/agent_configs.json. Keep any non-
        anthropic endpoint block, plus anthropic blocks carrying a non-default
        effort or a per-agent model (so those survive a reload); a plain
        anthropic/med/no-model agent stays unpersisted (it's just the default)."""
        import json as _json

        try:
            persist = {
                k: v
                for k, v in self._agent_runtime.items()
                if v.get("provider") != "anthropic"
                or v.get("effort", "med") != "med"
                or v.get("model")
            }
            if persist:
                self._agent_config_path.write_text(_json.dumps(persist))
            else:
                self._agent_config_path.unlink(missing_ok=True)
        except Exception:
            _log.exception("agent config persist failed")

    def _rebuild_runner(self, agent: str) -> None:
        """Drop the cached runner for `agent` so the next ``_make_runner`` rebuilds
        it with the freshly-resolved endpoint env (the SDK binds env at spawn).
        Best-effort teardown: ``stop()`` is async and this is a sync setter, so we
        fire-and-forget it onto the loop — tracking the task in ``self._tasks`` so
        it isn't GC'd before completing and ``stop()`` can await it. No-op when no
        runner exists or there's no running loop (unit-test path)."""
        runner = self.runners.pop(agent, None)
        if runner is None:
            return
        try:
            loop = asyncio.get_running_loop()
            self._tasks.append(loop.create_task(runner.stop()))  # type: ignore[func-returns-value]
        except RuntimeError:
            pass  # no running loop — nothing async to tear down

    # ── Backward-compat shims for the librarian (legacy routes/tests) ─────
    def librarian_config(self) -> dict[str, Any]:
        """Legacy: the librarian's runtime config (now per-agent underneath).
        Maps the old ``claude``/``local`` provider vocabulary onto the new
        ``anthropic``/endpoint model."""
        cfg = self.agent_config("librarian")
        return {
            "provider": "local" if cfg["provider"] != "anthropic" else "claude",
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "api_key": cfg["api_key"],
            "auth_style": cfg["auth_style"],
        }

    def set_librarian_config(
        self,
        *,
        provider: str,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        auth_style: str = "api_key",
    ) -> dict[str, Any]:
        """Legacy: set the librarian's provider (``claude``→anthropic, else local).
        Returns the legacy ``librarian_config()`` shape (``provider: claude|local``)."""
        provider = (provider or "claude").strip().lower()
        if provider not in ("claude", "local"):
            return {"error": f"unknown provider {provider!r}"}
        res = self.set_agent_config(
            "librarian",
            provider="anthropic" if provider == "claude" else "local",
            base_url=base_url,
            model=model,
            api_key=api_key,
            auth_style=auth_style,
        )
        if "error" in res:
            return res
        return self.librarian_config()

    def _rebuild_librarian_runner(self) -> None:
        self._rebuild_runner("librarian")

    async def _embed_backfill_loop(self, *, interval: float = 30.0) -> None:
        """Periodic semantic-memory backfill: embed KG facts missing a current-
        model vector, in bounded batches. Inert (returns after one sleep) when no
        embedder is configured — semantic recall is an enhancement, not load-
        bearing. Mirrors salient-core's operator-daemon backfill without its bus-
        call reaper machinery; never raises (a single bad pass is logged + the
        loop keeps running)."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self._embed_backfill_once()
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("embed backfill pass failed")

    async def _embed_backfill_once(self, *, limit: int = 200) -> int:
        """Embed up to ``limit`` KG facts lacking a current-model vector. Returns
        the count stored. No-op when no embedder is configured (``get_embedder``
        is None) or there is nothing pending."""
        from salient_core import get_embedder
        from salient_core.memory.embeddings import pack_vector

        embedder = get_embedder(self.profile)
        if embedder is None:
            return 0
        pending = self.kg.facts_needing_embedding(embedder.model, limit=limit)
        if not pending:
            return 0
        vecs = await embedder.embed([text for _, text in pending])
        if not vecs:
            return 0
        stored = self.kg.store_embeddings(
            [(fid, pack_vector(v)) for (fid, _), v in zip(pending, vecs, strict=False)],
            embedder.model,
        )
        if stored:
            _log.info("embed backfill: stored %d vectors (%s)", stored, embedder.model)
        return stored

    @property
    def all_cfgs(self) -> dict[str, dict[str, Any]]:
        """Kernel-facing alias — the bus consensus/delegation tools read
        ``daemon.all_cfgs`` (the operator daemon's name for the roster)."""
        return self.agent_configs

    def add_question(self, agent: str, question: str, job_id: int | None = None) -> int:
        """File an operator question. Returns the Q-id."""
        qid = self.inbox.add(agent=agent, text=question, job_id=job_id)
        return qid

    def _load_prompt(self, agent: str) -> str:
        cfg = self.agent_configs[agent]
        prompt = (_PROMPTS_DIR / cfg["system_prompt_file"]).read_text()
        # When illustrations are enabled, give the image-authoring agents the
        # full rubric on top of the compressed always-on block in tutor.md.
        if agent in _IMAGE_SKILL_AGENTS and _IMAGE_SKILL_PATH.exists():
            from salient_tutor import illustrations

            if illustrations.available():
                prompt += "\n\n---\n\n" + _IMAGE_SKILL_PATH.read_text()
                if _PALACE_SKILL_PATH.exists():
                    prompt += "\n\n---\n\n" + _PALACE_SKILL_PATH.read_text()
        return prompt

    def _make_options(self, agent: str) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for one agent.

        For the librarian with a ``local`` provider configured, this injects a
        per-agent endpoint override — routing the librarian's SDK subprocess at a
        local chat endpoint (LM Studio serving /v1/messages) via ANTHROPIC_BASE_URL,
        passing ``--bare`` so the CLI skips Anthropic-side startup prefetches that
        hang against a local model, and disabling extended thinking (a local
        endpoint can't stream thinking blocks). Ported from salient-core's
        ``endpoint:`` block; only the librarian is ever rerouted.

        codex agents never reach here — _make_runner branches to the
        CodexProvider backend first (codex isn't an Anthropic-compatible
        endpoint, so ClaudeAgentOptions don't apply to it)."""
        cfg = self.agent_configs[agent]
        system_prompt = self._load_prompt(agent)

        builtin = cfg.get("builtin_tools", [])
        opt_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            # `tools` *registers* the built-in tools so they actually exist;
            # `allowed_tools` only auto-approves ones already registered.
            # Both are needed: under --bare (local librarian endpoint) the
            # CLI skips default-toolset discovery, so a built-in named only in
            # allowed_tools would be auto-approved-but-missing -> "No such
            # tool available".
            "tools": list(builtin),
            "allowed_tools": list(builtin),
            "model": cfg["model"],
            "max_turns": cfg.get("max_turns", 30),
        }

        # The bus MCP server (context/delegation/kg tools) is optional per
        # agent. Agents that collaborate (the tutor) attach it; a one-shot
        # extractor like the librarian does not — its prompt forbids every bus
        # tool, and the ~6K tokens of schemas they add overflow the small
        # context windows local models are often loaded with.
        if cfg.get("bus_tools", True):
            bus_server, server_name, wire_names = make_bus(self, agent)
            opt_kwargs["mcp_servers"] = {server_name: bus_server}
            opt_kwargs["allowed_tools"] = list(wire_names) + builtin

        # Per-agent endpoint override — any agent whose runtime provider isn't
        # anthropic gets rerouted at its configured endpoint. Ported from
        # salient-core's endpoint: block, generalized from librarian-only.
        ep = self._agent_endpoint_for(agent)
        if ep is not None:
            base_url, model, api_key, auth_style, bare, provider, effort = ep
            sub_env = dict(os.environ)
            # Drop the Anthropic-specific attribution header on non-Anthropic
            # endpoints (LM Studio, LiteLLM, DeepSeek, MiniMax don't need it).
            sub_env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
            sub_env["ANTHROPIC_BASE_URL"] = base_url
            if api_key:
                if auth_style == "bearer":
                    sub_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                    sub_env.pop("ANTHROPIC_API_KEY", None)
                else:
                    sub_env["ANTHROPIC_API_KEY"] = api_key
                    sub_env.pop("ANTHROPIC_AUTH_TOKEN", None)
            opt_kwargs["env"] = sub_env
            if model:
                opt_kwargs["model"] = model  # the configured model, not the roster default
            if bare:
                extra = dict(opt_kwargs.get("extra_args") or {})
                extra.setdefault("bare", None)
                opt_kwargs["extra_args"] = extra
            # Thinking policy comes from the provider registry (local/deepseek
            # → disabled; minimax → coupled; anthropic never reaches here).
            opt_kwargs["thinking"] = resolve_thinking(provider, effort, model)
            opt_kwargs["effort"] = effort
            # Non-Anthropic backends can't run Anthropic-side server tools.
            spec_disable = (
                PROVIDERS.get(provider).disable_builtin_tools if PROVIDERS.get(provider) else ()
            )
            if spec_disable:
                opt_kwargs["tools"] = [
                    t for t in opt_kwargs.get("tools", []) if t not in spec_disable
                ]
                opt_kwargs["allowed_tools"] = [
                    t for t in opt_kwargs.get("allowed_tools", []) if t not in spec_disable
                ]
        else:
            # Anthropic (inherited env): no endpoint reroute, but the operator's
            # effort dial still drives the extended-thinking budget. Without this
            # the dial was a no-op for the default (anthropic) agents.
            rt = self._runtime_for(agent)
            # A per-agent Anthropic model (opus/sonnet/fable/…) overrides the
            # roster default so the operator can pick the Claude model per agent
            # from the config/UI, not just via the TUTOR_*_MODEL env.
            model = _expand_envvars(rt.get("model", "")) or cfg["model"]
            opt_kwargs["model"] = model
            effort = rt.get("effort", "med")
            opt_kwargs["effort"] = effort
            opt_kwargs["thinking"] = resolve_thinking("anthropic", effort, model)

        return ClaudeAgentOptions(**opt_kwargs)

    def _agent_endpoint_for(self, agent: str) -> tuple[str, str, str, str, bool, str, str] | None:
        """The endpoint override for one agent, or None when it runs on anthropic
        (inherited env). Returns ``(base_url, model, api_key, auth_style, bare,
        provider, effort)`` with ${VAR} expanded. None for unknown agents or
        anthropic provider (no rerouting needed)."""
        if agent not in self.agent_configs:
            return None
        cfg = self._runtime_for(agent)
        provider = cfg.get("provider", "anthropic")
        spec = PROVIDERS.get(provider)
        if spec is None or not spec.needs_endpoint:
            return None  # anthropic — inherited env, no override
        base_url = _expand_envvars(cfg.get("base_url", "") or spec.default_base_url)
        if not base_url:
            return None
        model = _expand_envvars(cfg.get("model", ""))
        api_key = _expand_envvars(cfg.get("api_key", ""))
        # No per-agent key configured → fall back to this provider's conventional
        # key env (DEEPSEEK_API_KEY / MINIMAX_API_KEY), so each agent picks up ITS
        # OWN provider's credential instead of silently inheriting the global
        # ANTHROPIC_API_KEY. An explicit per-agent key (above) always wins.
        if not api_key and spec.default_key_env:
            api_key = os.environ.get(spec.default_key_env, "")
        if not api_key and provider != "local":
            _log.warning(
                "agent %s routed to %s but no key resolved (set %s or a per-agent "
                "key); it will fall back to the inherited Anthropic credentials "
                "(API key or OAuth) and likely fail auth against %s",
                agent,
                provider,
                spec.default_key_env or "<none>",
                provider,
            )
        auth_style = cfg.get("auth_style", spec.auth_style)
        effort = cfg.get("effort", "med")
        bare = True  # always --bare for non-Anthropic endpoints (skip startup prefetch)
        return (base_url, model, api_key, auth_style, bare, provider, effort)

    def _make_codex_backend_factory(self, agent: str) -> tuple[Any, Any]:
        """Zero-arg backend factory producing a salient-core CodexBackend for
        `agent`, plus the bus tool bundle it hands codex over the MCP gateway.

        Mirrors salient-core's own codex runner branch: the same system prompt
        the claude path would use rides as `instructions`, the roster's Claude
        tier maps to a codex model unless the operator configured one, and the
        tutor effort dial maps to codex reasoningEffort."""
        from salient_core import ProviderName, ToolBundle, get_provider_registry
        from salient_core.bus import make_bus_tool_bundle
        from salient_core.codex import CodexProvider

        from salient_tutor.providers import codex_effort, codex_model_for

        cfg = self.agent_configs[agent]
        rt = self._runtime_for(agent)
        tool_bundle = ToolBundle()
        if cfg.get("bus_tools", True):
            tool_bundle, _wires = make_bus_tool_bundle(self, agent)
        provider = get_provider_registry().get(ProviderName("codex"))
        if not isinstance(provider, CodexProvider):
            raise TypeError("registered codex provider has an incompatible implementation")
        config: dict[str, Any] = {
            "agent_name": agent,
            "cwd": str(self.work_root),
            "instructions": self._load_prompt(agent),
            "model": codex_model_for(
                _expand_envvars(rt.get("model", "")) or "", cfg.get("model", "")
            ),
        }
        effort = codex_effort(rt.get("effort", "med"))
        if effort:
            config["effort"] = effort

        def factory() -> Any:
            loop = asyncio.get_running_loop()
            return provider.create_backend(
                config,
                tool_bundle=tool_bundle,
                approval_handler=self._make_codex_approval_handler(agent, loop),
            )

        return factory, tool_bundle

    def _make_codex_approval_handler(self, agent: str, loop: asyncio.AbstractEventLoop) -> Any:
        """Fail-closed codex approval policy, scoped by the agent's own tool
        whitelist: read-only commands (core's default-deny classifier)
        auto-accept ONLY for agents whose builtin_tools already grant
        unconfined filesystem reads on the claude path (Bash, or Read without
        confine_reads_to_study) — routing an agent to codex must not widen
        what it could touch. Everything else declines and files an
        informational note in the question inbox. The tutor has no
        approval-answer surface, so blocking on the inbox futures (salient's
        operator pattern) would hang the turn with nobody able to answer.
        Called from the codex worker thread — hence call_soon_threadsafe."""
        from salient_core import codex_command_is_read_only
        from salient_core.codex import ApprovalDecision

        builtin = set(self.agent_configs.get(agent, {}).get("builtin_tools", []))
        reads_unconfined = "Bash" in builtin or (
            "Read" in builtin
            and not self.agent_configs.get(agent, {}).get("confine_reads_to_study")
        )

        def handle(request: Any) -> Any:
            if (
                reads_unconfined
                and request.kind.value == "command"
                and codex_command_is_read_only(request.params)
            ):
                _log.info(
                    "codex read-only auto-accept for %s: %s",
                    agent,
                    str(request.params.get("command", ""))[:300],
                )
                return ApprovalDecision.ACCEPT
            params = request.params
            detail = str(
                params.get("command")
                or params.get("reason")
                or params.get("permissions")
                or request.method
            )[:300]
            _log.warning("codex approval declined for %s (no operator surface): %s", agent, detail)
            loop.call_soon_threadsafe(
                self.add_question, agent, f"[codex declined] {request.kind.value}: {detail}"
            )
            return ApprovalDecision.DECLINE

        return handle

    def _make_runner(self, agent: str) -> AgentRunner:
        """Create or return the AgentRunner for an agent."""
        if agent in self.runners:
            return self.runners[agent]

        cfg = self.agent_configs[agent]
        # Core's AgentRunner takes a provider-neutral backend_factory (the
        # `options=` field died with the codex provider seam). codex routes
        # through salient-core's CodexProvider backend; every other provider
        # (anthropic + the Anthropic-compatible endpoint reroutes) is the 1:1
        # LocalClaudeBackend passthrough over the assembled options.
        if self._runtime_for(agent).get("provider") == "codex":
            backend_factory, tool_bundle = self._make_codex_backend_factory(agent)
            runner = AgentRunner(
                name=agent,
                cfg=cfg,
                backend_factory=backend_factory,
                tool_bundle=tool_bundle,
                context=self.context,
            )
        else:
            options = self._make_options(agent)
            runner = AgentRunner(
                name=agent,
                cfg=cfg,
                backend_factory=partial(LocalClaudeBackend, options),
                context=self.context,
            )
        runner._daemon = self  # type: ignore[attr-defined]  # DaemonServices back-injection
        # Feed the daemon-wide hub the /ws/tutor forwarder subscribes to —
        # without this the runner's live events never reach the socket and the
        # modal only shows the final reply in one block (no streaming).
        runner._event_hub = self.event_hub  # type: ignore[attr-defined]
        self.runners[agent] = runner
        return runner

    async def start(self) -> None:
        """Seed the mnemonic KG, then start all agent runners."""
        self._seed_pedagogy()
        self._seed_curricula()
        self._migrate_curriculum_prefix()
        for agent in self.agent_configs:
            runner = self._make_runner(agent)
            task = asyncio.create_task(runner.start())
            self._tasks.append(task)
        # Semantic-memory backfill — embeds KG facts lacking a current-model
        # vector every ~30s. Inert when no embedder is configured. Tracked in
        # self._tasks so stop()'s cancel sweep tears it down with the runners.
        self._tasks.append(asyncio.create_task(self._embed_backfill_loop()))
        _log.info("tutor daemon started — %d agents", len(self.runners))

    async def stop(self) -> None:
        """Stop all runners."""
        for runner in self.runners.values():
            await runner.stop()
        for task in self._tasks:
            task.cancel()
        _log.info("tutor daemon stopped")

    async def prompt(
        self, agent: str, message: str, *, timeout: float = 120.0, session_id: str | None = None
    ) -> str:
        """Send a prompt to an agent and wait for the response."""
        if session_id:
            session = self.get_session(session_id)
            message = (
                f"SERVER SESSION STATE: phase={session['phase']}; skill_id={session['skill_id']}; "
                "Structured assessment actions, not prose, advance the lesson.\n\n" + message
            )
        runner = self._make_runner(agent)
        if runner.status not in ("running", "idle"):
            await runner.start()
        store = getattr(self, "lesson_store", None)
        # start/finish_agent_run are blocking sqlite writes; keep them off the
        # event loop so telemetry never stalls the daemon.
        run = (
            await asyncio.to_thread(
                store.start_agent_run,
                session_id,
                agent,
                str(runner.cfg.get("model") or runner.cfg.get("provider") or "unknown"),
                "prompt",
            )
            if store is not None
            else None
        )

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # cfg max_turns as the per-job budget: claude backends enforce it via
        # ClaudeAgentOptions, but backend-seam providers (codex) have no
        # native turn cap — the hint arms the runner's wire-level hard cap.
        runner.submit(message, future=future, max_turns_hint=runner.cfg.get("max_turns"))
        try:
            result_job = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            if store is not None and run is not None:
                await asyncio.to_thread(
                    store.finish_agent_run, run["run_id"], "timeout", error="agent timeout"
                )
            raise
        except Exception as error:
            if store is not None and run is not None:
                await asyncio.to_thread(
                    store.finish_agent_run, run["run_id"], "failed", error=str(error)
                )
            raise
        output = result_job.result or ""
        status = "failed" if result_job.error else "completed"
        if store is not None and run is not None:
            await asyncio.to_thread(
                store.finish_agent_run, run["run_id"], status, output=output, error=result_job.error
            )
        return output or result_job.error or "(no response)"

    async def second_opinion(self, question: str, *, timeout: float = 180.0) -> dict[str, Any]:
        """Ask the tutor panel (tutor + tutor_alt) the same question via the
        kernel's ``ask_consensus`` and return its parsed payload.

        The consensus tool normally dispatches legs through the operator
        daemon's ``ask_agent`` bus tool; here a lightweight shim routes each
        leg through :meth:`prompt` instead, so none of the delegation
        machinery (bus-call registry, redispatch gates) is needed. The judge
        leg only runs when a ``judge`` agent is configured
        (``TUTOR_JUDGE_MODEL``).

        Returns the ask_consensus payload — ``{ok, panel, agreement_score,
        semantic_score, corroborated, divergent, per_agent, judge,
        warnings?}`` — or ``{"ok": False, "error": ...}`` on refusal
        (e.g. fewer than 2 live tutors).
        """
        import json

        from salient_core.bus import make_consensus_tools

        daemon = self

        class _AskShim:
            """Duck-types the routed ask_agent bus tool: consensus awaits
            ``ask_agent.trusted(child, flags=...)`` and unwraps an MCP text
            block. ``flags`` (the BusFlags routing channel) is accepted and
            ignored — this shim dispatches directly through :meth:`prompt`
            with no bus routing."""

            @staticmethod
            async def trusted(child: dict[str, Any], *, flags: Any = None) -> dict[str, Any]:
                name = (child.get("name") or "").strip()
                try:
                    reply = await daemon.prompt(name, child.get("prompt") or "", timeout=timeout)
                    return {"content": [{"type": "text", "text": reply}]}
                except Exception as e:  # noqa: BLE001 — one leg's fault ≠ whole panel
                    return {
                        "content": [{"type": "text", "text": f"error: {e}"}],
                        "is_error": True,
                    }

        (ask_consensus,) = make_consensus_tools(self, "operator", _AskShim())
        judge_on = "judge" in self.agent_configs
        out = await ask_consensus.handler(
            {
                "name": "tutor",
                "prompt": question,
                "agents": ["tutor", "tutor_alt"],
                "judge": "on" if judge_on else "off",
                "judge_agent": "judge",
            }
        )
        text = (out.get("content") or [{}])[0].get("text", "")
        if out.get("is_error"):
            return {"ok": False, "error": text}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "error": text}

    # ── Pedagogy filter (judge as answer-leakage gate) ──────────────

    def judge_enabled(self) -> bool:
        """Whether a ``judge`` agent is configured (``TUTOR_JUDGE_MODEL`` set).

        Drives the web ``/api/config`` flag, the strictness dial's visibility,
        and whether tutor turns are reviewed before display.
        """
        return "judge" in self.agent_configs

    async def pedagogy_filter(
        self,
        question: str,
        draft: str,
        *,
        strictness: str = "socratic",
        attempt_pending: bool = False,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Have the ``judge`` agent gate a tutor draft before it reaches the learner.

        Enforces two pedagogy rules in one pass and returns
        ``{"leaked": bool, "needs_attempt": bool, "revised": str}``:

        * **Attempt-first** — when ``attempt_pending`` is False and the question is
          a problem the learner should try first, ``needs_attempt`` is True and
          ``revised`` is a short probing question eliciting their attempt (no
          teaching). ``attempt_pending=True`` means the learner just attempted, so
          this is suppressed defensively even if the judge slips.
        * **No leak** — otherwise, when the draft hands over a solution the learner
          should derive, ``leaked`` is True and ``revised`` is the rewrite to the
          requested hint level.

        ``revised`` is always the exact text to show. Degrades to a passthrough
        (``draft`` unchanged, both flags False) when no judge is configured or the
        judge errors/returns unparseable output — a judge failure never blocks the
        turn. ``strictness`` ∈ {"explain", "socratic", "bare"}.
        """

        draft = draft or ""
        if not self.judge_enabled() or not draft.strip():
            return {"leaked": False, "needs_attempt": False, "revised": draft}

        level = strictness if strictness in _STRICTNESS_LEVELS else "socratic"
        prompt = _PEDAGOGY_FILTER_PROMPT.format(
            level=level,
            rubric=_STRICTNESS_LEVELS[level],
            attempt_pending=attempt_pending,
            question=question,
            draft=draft,
        )
        try:
            reply = await self.prompt("judge", prompt, timeout=timeout)
            parsed = _parse_json_reply(reply)
            # Never gate the attempt turn, even if the judge slips.
            needs_attempt = bool(parsed.get("needs_attempt")) and not attempt_pending
            leaked = bool(parsed.get("leaked"))
            revised = parsed.get("revised") or draft
            if needs_attempt:
                return {"leaked": False, "needs_attempt": True, "revised": revised}
            if leaked:
                return {"leaked": True, "needs_attempt": False, "revised": revised}
            return {"leaked": False, "needs_attempt": False, "revised": draft}
        except Exception:  # noqa: BLE001 — judge is advisory; never block the answer
            return {"leaked": False, "needs_attempt": False, "revised": draft}

    # ── Retrieval micro-quiz (SM-2 review from a due tile) ──────────

    def record_review(self, topic: str, grade: str) -> dict[str, Any]:
        """Record a graded retrieval review under ``learner:op`` and reschedule.

        Deterministic (no LLM) — mirrors the kernel ``record_review`` bus tool:
        reads the topic's prior scheduling state, runs the SM-2 functions, and
        upserts the mastery fact with the new mastery + next-review date. Returns
        ``{topic, grade, mastery, predicate, interval_days, review_due}``. Raises
        ValueError on an unknown grade (caller validates first).
        """
        import time

        from salient_core.tutor.schedule import (
            next_interval_days,
            next_mastery,
            normalize_grade,
            predicate_for,
        )

        grade = normalize_grade(grade)
        now = time.time()
        state = self.kg.learner_review_state(LEARNER_SUBJECT, topic)
        prev_interval = state["prev_interval_days"] if state else None
        prev_mastery = state["mastery"] if state else None
        interval = next_interval_days(prev_interval, grade)
        mastery = next_mastery(prev_mastery, grade)
        predicate = predicate_for(mastery)
        review_due = now + interval * 86400
        self.kg.record_learner_review(
            LEARNER_SUBJECT,
            topic,
            predicate=predicate,
            mastery=mastery,
            review_due=review_due,
            agent="quiz",
            now=now,
        )
        # Append-only telemetry (Phase 0): the KG upsert above OVERWRITES the
        # learner fact, so the scheduler's history lives only here. Best-effort —
        # reviewlog.append swallows its own errors so it can't break a review.
        from salient_tutor import reviewlog

        reviewlog.append(
            self.work_root,
            {
                "ts": now,
                "topic": topic,
                "grade": grade,
                "mastery_before": prev_mastery,
                "mastery_after": mastery,
                "prev_interval_days": prev_interval,
                "interval_days": interval,
                "review_due": review_due,
                "predicate": predicate,
            },
        )
        return {
            "topic": topic,
            "grade": grade,
            "mastery": mastery,
            "predicate": predicate,
            "interval_days": interval,
            "review_due": review_due,
        }

    def create_session(
        self, skill_id: str, *, session_kind: str = "lesson", **bindings: str | None
    ) -> dict[str, Any]:
        return self.lessons.create_session(skill_id, session_kind=session_kind, **bindings)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.lessons.get_session(session_id)

    def current_session(self) -> dict[str, Any] | None:
        return self.lessons.current_session()

    def session_events(self, session_id: str) -> list[dict[str, Any]]:
        return self.lesson_store.events(session_id)

    def pause_session(self, session_id: str, expected_version: int | None = None) -> dict[str, Any]:
        return self.lessons.pause(session_id, expected_version)

    def resume_session(
        self, session_id: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        return self.lessons.resume(session_id, expected_version)

    def abandon_session(
        self, session_id: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        return self.lessons.abandon(session_id, expected_version)

    def advance_phase(self, session_id: str, expected_version: int | None = None) -> dict[str, Any]:
        return self.lessons.advance(session_id, expected_version)

    def issue_assessment_item(
        self,
        session_id: str,
        item: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self.lessons.issue_item(session_id, item=item, expected_version=expected_version)

    def record_attempt(
        self,
        session_id: str,
        item_id: str,
        item_version: int,
        response: str,
        idempotency_key: str,
        hints_used: int = 0,
        judge_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.lessons.record_attempt(
            session_id,
            item_id,
            item_version,
            response,
            idempotency_key,
            hints_used=hints_used,
            judge_result=judge_result,
        )

    def apply_attempt_review(
        self, session_id: str, attempt_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        attempt = self.lesson_store.get_attempt(attempt_id)
        if attempt is None or attempt["session_id"] != session_id:
            raise ValueError("attempt does not belong to session")
        existing = self.lesson_store.get_review_application(idempotency_key)
        if existing:
            return existing
        session = self.get_session(session_id)
        grade = self.lessons.grade_for_attempt(attempt)
        if grade is None:
            return self.lesson_store.save_review_application(
                {
                    "idempotency_key": idempotency_key,
                    "attempt_id": attempt_id,
                    "srs_topic": session["srs_topic"],
                    "requested_grade": "unscored",
                    "application_status": "not_applied",
                    "scheduler_result": {},
                    "error": "assessment was not confidently scored",
                }
            )
        result = self.record_review(session["srs_topic"], grade)
        return self.lesson_store.save_review_application(
            {
                "idempotency_key": idempotency_key,
                "attempt_id": attempt_id,
                "srs_topic": session["srs_topic"],
                "requested_grade": grade,
                "application_status": "applied",
                "scheduler_result": result,
            }
        )

    def create_card(self, session_id: str, card: dict[str, Any]) -> dict[str, Any]:
        session = self.get_session(session_id)
        item_id = str(card.get("source_item_id") or session.get("active_item_id") or "")
        if not item_id or not card.get("question") or not card.get("answer"):
            raise ValueError("card requires source_item_id, question, and answer")
        return self.lesson_store.save_card(
            {
                "card_id": str(card.get("card_id") or uuid.uuid4().hex),
                "version": int(card.get("version", 1)),
                "skill_id": session["skill_id"],
                "source_item_id": item_id,
                "question": str(card["question"]),
                "answer": str(card["answer"]),
                "card_type": str(card.get("card_type") or "basic"),
                "provenance": card.get("provenance", []),
                "srs_topic": session["srs_topic"],
                "status": str(card.get("status") or "active"),
            }
        )

    def list_cards(self, skill_id: str | None = None) -> list[dict[str, Any]]:
        return self.lesson_store.list_cards(skill_id=skill_id)

    def update_card_status(self, card_id: str, version: int, status: str) -> dict[str, Any]:
        return self.lesson_store.update_card_status(card_id, version, status)

    def analytics(self) -> dict[str, Any]:
        return self.lesson_store.analytics()

    def migration_report(self) -> dict[str, Any]:
        return self.lesson_store.migration_report()

    def review_log(
        self, *, topic: str | None = None, limit: int | None = 500
    ) -> list[dict[str, Any]]:
        """The append-only review-event history (Phase-0 scheduling telemetry),
        oldest→newest, optionally filtered to one topic. See
        :mod:`salient_tutor.reviewlog`."""
        from salient_tutor import reviewlog

        return reviewlog.read(self.work_root, topic=topic, limit=limit)

    async def quiz(self, topic: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """Ask the tutor to generate one retrieval question for ``topic``.

        Returns ``{"question": str, "answer": str}`` (the reference answer backs
        the card + grading), or ``{"error": ...}`` on empty topic / unparseable
        model output.
        """

        topic = (topic or "").strip()
        if not topic:
            return {"error": "no topic"}
        try:
            reply = await self.prompt(
                "tutor", _QUIZ_GEN_PROMPT.format(topic=topic), timeout=timeout
            )
            parsed = _parse_json_reply(reply)
            question = (parsed.get("question") or "").strip()
            if not question:
                return {"error": "tutor returned no question"}
            return {"question": question, "answer": (parsed.get("answer") or "").strip()}
        except Exception:  # noqa: BLE001 — quiz-gen is best-effort; degrade to an error card
            return {"error": "could not generate a question"}

    async def grade_quiz(
        self,
        topic: str,
        question: str,
        answer: str,
        learner_answer: str,
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Grade the learner's retrieval answer and record the SM-2 review.

        The tutor judges the answer on the four-button scale; a valid grade is
        applied via :meth:`record_review` (deterministic). Returns
        ``{grade, feedback, mastery, interval_days, review_due}``; on an unusable
        grade, ``grade`` is None and no review is written (feedback still shown).
        """

        from salient_core.tutor.schedule import GRADES

        prompt = _QUIZ_GRADE_PROMPT.format(
            topic=topic,
            question=question,
            answer=answer,
            learner_answer=learner_answer or "",
        )
        try:
            reply = await self.prompt("tutor", prompt, timeout=timeout)
            parsed = _parse_json_reply(reply)
            grade = (parsed.get("grade") or "").strip().lower()
            feedback = (parsed.get("feedback") or "").strip()
        except Exception:  # noqa: BLE001 — grading is best-effort; never crash the request
            return {"grade": None, "feedback": "", "error": "could not grade the answer"}

        if grade not in GRADES:
            return {"grade": None, "feedback": feedback, "note": "no review recorded"}
        review = self.record_review(topic, grade)
        return {
            "grade": grade,
            "feedback": feedback,
            "mastery": review["mastery"],
            "interval_days": review["interval_days"],
            "review_due": review["review_due"],
        }

    # ── Prerequisite-DAG skill map ──────────────────────────────────

    def _persisted_edges(self) -> list[tuple[str, str]]:
        """Read the persisted prerequisite DAG from the ``curriculum:`` namespace.

        Returns ``(prereq, dependent)`` topic pairs (prefix stripped). Pure read.
        """
        out: list[tuple[str, str]] = []
        for f in self.kg.export_by_subject_prefix(_CURRICULUM_PREFIX):
            if f.get("predicate") != _PREREQ_PREDICATE:
                continue
            a = str(f.get("subject", "")).removeprefix(_CURRICULUM_PREFIX).strip()
            b = str(f.get("object", "")).removeprefix(_CURRICULUM_PREFIX).strip()
            if a and b:
                out.append((a, b))
        return out

    def _migrate_curriculum_prefix(self) -> int:
        """One-time, idempotent migration of tutor-inferred prereq edges from the
        legacy bare ``curriculum:<topic>`` namespace to ``curriculum:inferred:``.

        Earlier builds persisted ``prereq_of`` edges under a bare ``curriculum:``
        prefix, which collided with the seeded ``curriculum:track:`` namespace;
        the inferred-edge prefix was later renamed to ``curriculum:inferred:``.
        Without this migration an existing install's edges become invisible to
        :meth:`_persisted_edges` (the tutor needlessly regenerates them),
        unpurgeable by ``skill_graph`` rebuild, and leak into KB search (the
        namespace-skip filter no longer matches them).

        Re-asserts each legacy edge under the new prefix (``assert_fact`` dedupe
        makes re-runs a no-op) then deletes the old rows by id. New-prefix rows
        are written before their old twin is dropped, so a crash mid-migration
        just leaves work for the next startup. Returns the count migrated.
        """

        def _remap(term: str) -> str:
            if term.startswith(("curriculum:track:", _CURRICULUM_PREFIX)):
                return term
            if term.startswith("curriculum:"):
                return _CURRICULUM_PREFIX + term.removeprefix("curriculum:")
            return term

        stale_ids: list[int] = []
        for f in self.kg.export_by_subject_prefix("curriculum:"):
            subject = str(f.get("subject", ""))
            # Leave the seeded track namespace and already-migrated rows alone.
            if subject.startswith(("curriculum:track:", _CURRICULUM_PREFIX)):
                continue
            if f.get("predicate") != _PREREQ_PREDICATE:
                continue
            fid = f.get("id")
            if not isinstance(fid, int):
                continue
            try:
                self.kg.assert_fact(
                    _remap(subject),
                    _PREREQ_PREDICATE,
                    _remap(str(f.get("object", ""))),
                    confidence=float(f.get("confidence") or 1.0),
                    agent=f.get("agent") or "curriculum",
                    engagement_id=f.get("engagement_id"),
                    expires_at=f.get("expires_at"),
                )
            except Exception:  # noqa: BLE001 — skip a bad row, keep the old one for next run
                continue
            stale_ids.append(fid)
        if stale_ids:
            self.kg.delete_many(stale_ids)
            _log.info(
                "migrated %d legacy curriculum: prereq edges to %s",
                len(stale_ids),
                _CURRICULUM_PREFIX,
            )
        return len(stale_ids)

    def _assemble_skill_graph(
        self, profile: dict[str, Any], edges: list[tuple[str, str]]
    ) -> dict[str, Any]:
        """Build the DAG payload from a bucketed profile + prereq edges. Pure.

        Node status priority: misconception → due → mastered (strong) → learning
        (weak). A node that appears only in edges (a next-topic, not yet studied)
        is ``available`` when every prerequisite is mastered, else ``locked``.
        """
        strong = {e["topic"] for e in profile.get("strong", [])}
        weak = {e["topic"] for e in profile.get("weak", [])}
        due = {e["topic"] for e in profile.get("due", [])}
        misc = {m["topic"] for m in profile.get("misconceptions", [])}
        studied = strong | weak | due | misc

        # Prerequisites (incoming edge sources) per node, for available/locked.
        prereqs: dict[str, list[str]] = {}
        node_set: set[str] = set(studied)
        for a, b in edges:
            node_set.add(a)
            node_set.add(b)
            prereqs.setdefault(b, []).append(a)

        def status(topic: str) -> str:
            if topic in misc:
                return "misconception"
            if topic in due:
                return "due"
            if topic in strong:
                return "mastered"
            if topic in weak:
                return "learning"
            # Not yet studied: gated on its prerequisites all being mastered.
            reqs = prereqs.get(topic, [])
            return "available" if all(r in strong for r in reqs) else "locked"

        nodes = [{"id": t, "label": t, "status": status(t)} for t in sorted(node_set)]
        counts: dict[str, int] = {}
        for n in nodes:
            counts[n["status"]] = counts.get(n["status"], 0) + 1
        return {
            "nodes": nodes,
            "edges": [[a, b] for a, b in edges if a in node_set and b in node_set],
            "counts": counts,
        }

    async def _generate_and_persist_edges(
        self, topics: list[str], *, timeout: float = 120.0
    ) -> list[tuple[str, str]]:
        """Ask the tutor for a prerequisite DAG over ``topics`` and persist it.

        Degrades to ``[]`` (nothing persisted) on any failure — the map simply
        renders without edges rather than crashing.
        """

        if not topics:
            return []
        prompt = _SKILL_GRAPH_PROMPT.format(topics="\n".join(f"- {t}" for t in topics))
        try:
            reply = await self.prompt("tutor", prompt, timeout=timeout)
            parsed = _parse_json_reply(reply)
            raw = parsed.get("edges") or []
        except Exception:  # noqa: BLE001 — best-effort; render without edges on failure
            return []
        edges: list[tuple[str, str]] = []
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            a, b = str(pair[0]).strip(), str(pair[1]).strip()
            if not a or not b or a == b:
                continue
            try:
                self.kg.assert_fact(
                    f"{_CURRICULUM_PREFIX}{a}",
                    _PREREQ_PREDICATE,
                    f"{_CURRICULUM_PREFIX}{b}",
                    agent="curriculum",
                )
            except Exception:  # noqa: BLE001 — skip a bad triple, keep the rest
                continue
            edges.append((a, b))
        return edges

    async def skill_graph(self, *, rebuild: bool = False) -> dict[str, Any]:
        """Prerequisite-DAG view of the gradebook, colored by mastery.

        Reads persisted ``curriculum:`` edges; generates + persists them via the
        tutor on first use (or ``rebuild``). Returns ``{nodes, edges, counts}`` or
        ``{nodes: [], note}`` when the gradebook is empty.
        """
        profile = bucketed_profile(self.kg, LEARNER_SUBJECT)
        topics = sorted(
            {e["topic"] for e in profile.get("strong", [])}
            | {e["topic"] for e in profile.get("weak", [])}
            | {m["topic"] for m in profile.get("misconceptions", [])}
        )
        if not topics:
            return {"nodes": [], "edges": [], "note": "no topics yet — drill a lesson first"}
        if rebuild:
            self.kg.purge_by_subject_prefix(_CURRICULUM_PREFIX)
        edges = self._persisted_edges()
        if not edges:
            edges = await self._generate_and_persist_edges(topics)
        return self._assemble_skill_graph(profile, edges)

    # ── Study project management ────────────────────────────────────

    def study_create(self, title: str, subject: str = "cyber") -> dict[str, Any]:
        """Create a new study project envelope + on-disk dir tree."""
        from salient_tutor.study import new_project_id, new_study, save_study

        existing = {s["project_id"] for s in self.study_list()}
        pid = new_project_id(title, existing)
        study = new_study(pid, title, subject=subject)
        save_study(self.context, study)
        return {"status": "created", "project_id": pid, "title": title, "subject": study["subject"]}

    def study_list(self) -> list[dict[str, Any]]:
        """List all study projects (light summary)."""
        from salient_tutor.study import list_studies

        return list_studies(self.context)

    def curricula_list(self) -> list[dict[str, Any]]:
        """List available curriculum tracks with module/topic counts from the KG.

        Reads the ``curriculum:track:`` namespace to summarise what was seeded
        at startup. Returns one dict per track: ``{id, title, description,
        modules, topics}``.
        """
        from salient_core.tutor.schedule import STRONG_THRESHOLD

        facts = self.kg.export_by_subject_prefix("curriculum:track:")
        tracks: dict[str, dict[str, Any]] = {}
        mod_subjects: dict[str, set[str]] = {}  # track_id → set of module subjects
        topic_subjects: dict[str, set[str]] = {}  # track_id → set of topic subjects
        for f in facts:
            s = f["subject"]
            parts = s.split(":")
            # parts: [curriculum, track, <track_id>, module, <mod_id>, topic, <topic_id>]
            if len(parts) < 3:
                continue
            track_id = parts[2]
            if track_id not in tracks:
                tracks[track_id] = {
                    "id": track_id,
                    "title": track_id,
                    "description": "",
                    "modules": 0,
                    "topics": 0,
                }
                mod_subjects[track_id] = set()
                topic_subjects[track_id] = set()
            if len(parts) <= 3:
                # Track-level fact
                if f["predicate"] == "title":
                    tracks[track_id]["title"] = f["object"]
                elif f["predicate"] == "description":
                    tracks[track_id]["description"] = f["object"]
            elif ":module:" in s and ":topic:" not in s:
                mod_subjects[track_id].add(s.split(":topic:")[0])
            elif ":topic:" in s:
                mod_key = s.split(":topic:")[0]
                mod_subjects[track_id].add(mod_key)
                topic_subjects[track_id].add(s)
        for track_id, t in tracks.items():
            t["modules"] = len(mod_subjects.get(track_id, set()))
            t["topics"] = len(topic_subjects.get(track_id, set()))
        records = list(tracks.values())
        authored: dict[str, dict[str, Any]] = {}
        for path in sorted(_CURRICULA_DIR.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            track_id = str(data.get("track") or "")
            if track_id:
                authored[track_id] = data
        for track in records:
            data = authored.get(track["id"], {})
            modules: list[dict[str, Any]] = []
            for module in data.get("modules") or []:
                if not isinstance(module, dict):
                    continue
                module_id = str(module.get("id") or "")
                if not module_id:
                    continue
                topics: list[dict[str, Any]] = []
                for topic in module.get("topics") or []:
                    if not isinstance(topic, dict) or not topic.get("id"):
                        continue
                    topic_id = str(topic["id"])
                    skill_id = f"curriculum:track:{track['id']}:module:{module_id}:topic:{topic_id}"
                    state = self.kg.learner_review_state(LEARNER_SUBJECT, skill_id)
                    topics.append(
                        {
                            "id": topic_id,
                            "title": str(topic.get("title") or topic_id),
                            "skill_id": skill_id,
                            "difficulty": module.get("difficulty"),
                            "mastery_stage": "durable_mastery"
                            if state and state.get("mastery", 0.0) >= STRONG_THRESHOLD
                            else "unstarted",
                            "available": True,
                        }
                    )
                modules.append(
                    {
                        "id": module_id,
                        "title": str(module.get("title") or module_id),
                        "objective": str(module.get("objective") or ""),
                        "difficulty": module.get("difficulty"),
                        "prerequisites": list(module.get("prerequisites") or []),
                        "topics": topics,
                    }
                )
            mastered_modules = {
                module["id"]
                for module in modules
                if module["topics"]
                and all(topic["mastery_stage"] == "durable_mastery" for topic in module["topics"])
            }
            for module in modules:
                module["available"] = all(
                    prerequisite in mastered_modules for prerequisite in module["prerequisites"]
                )
                for topic in module["topics"]:
                    topic["available"] = module["available"]
            track["modules_detail"] = modules
        return records

    def study_show(self, project_id: str) -> dict[str, Any] | None:
        """Show one project's full envelope."""
        from salient_tutor.study import load_study

        return load_study(self.context, project_id)

    def study_upload(self, project_id: str, filename: str, data: bytes) -> dict[str, Any]:
        """Save an uploaded file with sha-based dedup."""
        from salient_tutor.study import load_study, save_study, save_upload

        study = load_study(self.context, project_id)
        if not study:
            return {"error": f"unknown project {project_id}"}
        try:
            doc = save_upload(project_id, filename, data)
        except ValueError as e:  # size cap / empty document — operator error, not a 500
            return {"error": str(e)}
        existing = next((d for d in study["docs"] if d["sha"] == doc["sha"]), None)
        if not existing:
            study["docs"].append(doc)
            save_study(self.context, study)
        return {"status": "uploaded", "doc": doc}

    def study_delete(self, project_id: str, *, confirm: bool = False) -> dict[str, Any]:
        """Delete a study project (dry-run by default)."""
        import shutil

        from salient_tutor.study import load_study, meta_key

        study = load_study(self.context, project_id)
        if not study:
            return {"error": f"unknown project {project_id}"}
        if not confirm:
            return {"status": "dry_run", "project_id": project_id, "docs": len(study["docs"])}
        self.kg.purge_by_subject_prefix(f"study:{project_id}:")
        self.context.meta_delete(meta_key(project_id))
        pdir = _study_path(project_id)
        if pdir.exists():
            shutil.rmtree(pdir)
        return {"status": "deleted", "project_id": project_id}

    def study_delete_doc(
        self, project_id: str, sha: str, *, confirm: bool = False
    ) -> dict[str, Any]:
        """Delete ONE document from a project (dry-run by default).

        Purges only this doc's KG facts — its ``doc:<sha8>`` node and
        ``chunk:<sha8>-*`` passage facts — leaving sibling docs and the
        project-level ``sec:`` scaffold intact. Also removes the on-disk upload,
        its extracted .md, and any OCR overlay. The structured ``sec:`` facts are
        project-scoped (not keyed per-doc), so they are NOT removed here; that is
        the known scoping limit of per-doc delete today."""
        from salient_tutor.study import find_doc, load_study, save_study

        study = load_study(self.context, project_id)
        if not study:
            return {"error": f"unknown project {project_id}"}
        doc = find_doc(study, sha)
        if not doc:
            return {"error": f"unknown document {sha[:8]} in {project_id}"}
        if not confirm:
            return {"status": "dry_run", "project_id": project_id, "sha": sha[:8]}

        sha8 = sha[:8]
        # KG: this doc's node + its passage chunks only (project sec: scaffold stays).
        purged = self.kg.purge_by_subject_prefix(f"study:{project_id}:doc:{sha8}")
        purged += self.kg.purge_by_subject_prefix(f"study:{project_id}:chunk:{sha8}")
        # Envelope: drop the doc descriptor.
        study["docs"] = [d for d in study["docs"] if d.get("sha") != sha]
        save_study(self.context, study)
        # On-disk: the raw upload, the librarian's extracted .md, and any OCR file.
        import shutil as _shutil

        pdir = _study_path(project_id)
        for sub in ("uploads", "extracted"):
            d = pdir / sub
            if not d.exists():
                continue
            for f in d.glob(f"{sha8}*"):
                with suppress(FileNotFoundError):
                    _shutil.rmtree(f) if f.is_dir() else f.unlink()
        # OCR overlays land in uploads/ under the stored_name (best-effort).
        stored = str(doc.get("stored_name") or "")
        if stored:
            for candidate in ((pdir / "uploads" / stored), (pdir / "uploads" / f"{sha8}.ocr.pdf")):
                with suppress(FileNotFoundError):
                    candidate.unlink()
        return {"status": "deleted", "project_id": project_id, "sha": sha8, "purged": purged}

    def learner_profile(self) -> dict[str, Any]:
        """Bucketed ``learner:op`` gradebook for the skill-map rail.

        Delegates to the kernel's :func:`bucketed_profile`, which buckets the raw
        mastery facts into due/strong/weak/misconceptions and annotates each topic
        with its real-forgetting-curve recall odds.
        """
        return bucketed_profile(self.kg, LEARNER_SUBJECT)

    def kg_search(self, query: str, *, limit: int = 60) -> list[dict[str, Any]]:
        """Substring search of the knowledge base for the KB rail.

        Matches the query against fact objects (and subjects), skipping the
        internal ``learner:``/``pedagogy:``/``curriculum:`` meta namespaces so the
        operator sees engagement/study facts. Returns light ``{subject, predicate,
        object}`` rows the rail renders as clickable triples.
        """
        q = (query or "").strip()
        if not q:
            return []
        seen: set[tuple[str, str, str]] = set()
        rows: list[dict[str, Any]] = []
        for facts in (self.kg.query(object_=q, limit=limit), self.kg.query(subject=q, limit=limit)):
            for f in facts:
                subj = getattr(f, "subject", "")
                if subj.startswith(("learner:", "pedagogy:", _CURRICULUM_PREFIX)):
                    continue
                key = (subj, getattr(f, "predicate", ""), getattr(f, "object", ""))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"subject": key[0], "predicate": key[1], "object": key[2]})
        return rows[:limit]

    def context_usage(self, agent: str = "tutor") -> dict[str, Any]:
        """Latest context-window usage for an agent (drives the Context bar).

        Reads the runner's ``last_context_usage`` snapshot (populated by the SDK
        after each turn). Returns an empty dict when the runner hasn't reported
        usage yet — the client degrades to a dash.
        """
        runner = self.runners.get(agent)
        usage = getattr(runner, "last_context_usage", None) if runner else None
        return dict(usage) if usage else {}

    def tutors(self) -> list[dict[str, Any]]:
        """The tutor family (default + any shadow variant) for the picker.

        Empty-ish rosters (just the one default tutor) let the web modal keep
        the picker hidden. ``running`` reflects whether the runner is live.
        """
        out: list[dict[str, Any]] = []
        for name, cfg in self.agent_configs.items():
            if cfg.get("family") != "tutor":
                continue
            runner = self.runners.get(name)
            out.append(
                {
                    "name": name,
                    "label": cfg.get("label", name),
                    "model": cfg.get("model", ""),
                    "running": bool(
                        runner and getattr(runner, "status", "") in ("running", "idle")
                    ),
                    "default": name == "tutor",
                }
            )
        out.sort(key=lambda t: (not t["default"], t["name"]))
        return out

    _HISTORY_SENTINELS = ("__EXPORT_LESSON__", "__FIX_DIAGRAM__", "__DRILL__", "__STUDY__")

    def history(self, agent: str = "tutor", *, limit: int = 30) -> dict[str, Any]:
        """Recent transcript turns for replay when the page (re)loads.

        Reads the persisted events table (survives daemon restarts), keeps
        ``user_message`` + ``text`` events, drops machine sentinel turns, and
        merges consecutive tutor blocks into one turn. Returns the last
        ``limit`` turns oldest→newest.
        """
        try:
            events = self.context.query_events(agent=agent, limit=max(limit * 8, 160))
        except Exception:
            return {"turns": []}
        turns: list[dict[str, str]] = []
        for e in events:
            kind = e.get("kind")
            text = (e.get("content") or {}).get("text") or ""
            if not text.strip():
                continue
            if kind == "user_message":
                if text.startswith(self._HISTORY_SENTINELS):
                    continue
                turns.append({"role": "operator", "text": text})
            elif kind in ("text", "reply"):
                if turns and turns[-1]["role"] == "tutor":
                    turns[-1]["text"] += "\n\n" + text
                else:
                    turns.append({"role": "tutor", "text": text})
        return {"turns": turns[-limit:]}

    async def study_extract(self, project_id: str, *, doc_sha: str | None = None) -> dict[str, Any]:
        """Dispatch the librarian agent to extract a document.

        The document is pre-converted to PLAIN TEXT first (pdftotext, with an
        OCR fallback for scanned PDFs) and the daemon INLINES that text straight
        into the librarian's prompt. This works for ANY model: text-only local
        models (Gemma/Llama/Qwen/Ornith — which reject the page IMAGES the SDK's
        Read tool would otherwise render) as well as vision-capable ones.
        Inlining also sidesteps a whole class of failures on small local models
        driving the ``--bare`` CLI, which mangle the Read tool-call path (or
        fabricate one) and come back "File not found" even though the .txt is on
        disk. Only when text extraction fails does the librarian fall back to
        reading the original file directly via the Read tool (which needs a
        vision model). The librarian emits the JSON contract and the daemon
        writes structured artifacts into the study:<id>: KG namespace.
        """
        from salient_tutor.study import extract_text, load_study

        study = load_study(self.context, project_id)
        if not study:
            return {"error": f"unknown project {project_id}"}
        target = next((d for d in study["docs"] if not doc_sha or d["sha"][:8] == doc_sha), None)
        if not target:
            return {"error": "no matching document found"}
        doc_path = _study_path(project_id) / "uploads" / target["stored_name"]
        if not doc_path.exists():
            return {"error": f"file not found: {target['stored_name']}"}

        try:
            # 1. Deterministically extract plain text so any model can ingest it.
            sha8 = target["sha"][:8]
            text_file, tex_err = extract_text(project_id, doc_path, sha8, first_pages=20)
            if text_file is not None:
                # Inline the pre-extracted text rather than asking the librarian
                # to Read it: a small local model on the --bare CLI is unreliable
                # at tool-calling and returns spurious "File not found" errors on
                # a path that is genuinely on disk. Cap the payload so a dense
                # doc can't blow a small local context window (Read would have
                # truncated too); the tail marker tells the model it's clipped.
                text = text_file.read_text(encoding="utf-8", errors="replace")
                _MAX_INLINE_CHARS = 60_000
                if len(text) > _MAX_INLINE_CHARS:
                    text = text[:_MAX_INLINE_CHARS] + "\n\n[…document truncated…]"
                instruction = (
                    "Extract the structured JSON from the document below. This is "
                    "the pre-extracted plain text of the source — treat it as the "
                    "whole document; do not call Read or any other tool.\n\n"
                    "───── DOCUMENT TEXT ─────\n"
                    f"{text}\n"
                    "───── END DOCUMENT ─────"
                )
            else:
                # Text extraction failed — let the librarian read the original.
                # This needs a vision model (the SDK renders PDF pages as images).
                instruction = (
                    f"Read pages 1–20 of {doc_path} and extract the structured JSON. "
                    f"(text pre-extraction failed: {tex_err})"
                )

            reply = await self.prompt("librarian", instruction)

            from salient_tutor.study import embed_into_kg, ingest_sections

            try:
                parsed = _parse_json_reply(reply)
            except ValueError as parse_err:
                # Persist the librarian's raw reply so a parse failure is
                # debuggable instead of a bare "Expecting ',' delimiter".
                # The next manual retry can inspect what the model returned.
                reply_path = _study_path(project_id) / "extracted" / f"{sha8}.reply.txt"
                try:
                    reply_path.parent.mkdir(parents=True, exist_ok=True)
                    reply_path.write_text(reply or "(empty reply)", encoding="utf-8")
                except OSError:
                    reply_path = None
                _log.warning(
                    "librarian JSON parse failed for %s doc %s: %s (raw reply saved to %s)",
                    project_id,
                    sha8,
                    parse_err,
                    reply_path,
                )
                return {"status": "failed", "error": str(parse_err)}
            if parsed.get("status") == "extracted":
                embed_into_kg(
                    self.kg,
                    project_id=project_id,
                    doc_sha=target["sha"][:8],
                    doc_filename=target.get("filename", "document"),
                    chunks=parsed.get("chunks", []),
                )
                ingest_sections(
                    self.kg,
                    project_id=project_id,
                    sections=parsed.get("sections", []),
                )
                target["status"] = "extracted"
                from salient_tutor.study import save_study

                save_study(self.context, study)
                return {"status": "extracted", "sections": len(parsed.get("sections", []))}
            return {"status": "failed", "error": parsed.get("error", "unknown")}
        except Exception as e:
            return {"status": "failed", "error": str(e)}


def _new_project_id(existing: set[str]) -> str:
    """Generate a short unique project id."""
    import secrets

    for _ in range(8):
        pid = secrets.token_hex(4)
        if pid not in existing:
            return pid
    raise RuntimeError("could not generate unique project id")


def _parse_json_reply(reply: str) -> Any:
    """json.loads a model reply, tolerating a ```json fence, surrounding prose,
    and raw control characters inside string values.

    Agents are prompted for strict JSON, but some (the librarian especially)
    still wrap the payload in a fenced block with a language tag — which the
    old ``strip("`")`` unwrap left in place, failing at char 0. Local models
    (glm-4.6v-flash, Llama, Gemma) also routinely emit RAW control characters
    (literal tabs / newlines / form-feeds) inside string values instead of
    escaping them, which strict ``json.loads`` rejects with
    "Invalid control character at: line N column M". ``strict=False`` accepts
    those, matching what the model actually emitted.

    Beyond that, long extractions (a whole document's chunks in one reply)
    come back with missing commas, unescaped inner quotes, or truncation at
    the model's output cap — e.g. "Expecting ',' delimiter: line 84". Those
    are structurally recoverable, so the last resort hands the payload to
    ``json_repair`` rather than failing the whole upload. Repair output is
    only trusted when it yields a non-empty dict (every payload this daemon
    parses is an object; garbage repairs to a bare string/``[]`` are rejected
    and re-raised with a snippet of the unparsed payload for debuggability).

    The payload span is taken from the FIRST ``{`` … LAST ``}`` (and only
    falls back to ``[`` … ``]`` when there is no brace). This avoids a stray
    ``[`` in leading prose — "Document [1] extracted:" — stealing the slice
    start and corrupting the JSON, which surfaced as a char-0 parse failure.
    """
    text = (reply or "").strip()
    try:
        return json.loads(text, strict=False)
    except ValueError:
        pass
    # Every payload this daemon parses (extractions, quizzes, skill-graphs) is a
    # JSON OBJECT, and the json_repair trust-check below only accepts dicts. So
    # prefer the brace span when present and only fall back to brackets when
    # there is no '{' at all. This also fixes a subtle bug: the old
    # min(find('{'), find('[')) let a stray '[' in LEADING prose — e.g.
    # "Document [1] extracted:\n```json\n{...}`` — capture the slice from the
    # wrong position, so the payload began mid-prose and failed at char 0 with
    # "Expecting ',' delimiter: line 1 column 3 (char 2)".
    start = text.find("{")
    end = text.rfind("}") if start != -1 else -1
    if start == -1:
        start = text.find("[")
        end = text.rfind("]")
    if start == -1:
        raise ValueError(f"no JSON payload in reply: {text[:80]!r}")
    # Narrow to the balanced span when a closer exists (strips trailing prose);
    # otherwise the reply was truncated mid-structure — keep everything from the
    # first opener so json_repair can close the open braces/arrays.
    payload = text[start : end + 1] if end > start else text[start:]
    try:
        return json.loads(payload, strict=False)
    except ValueError as exc:
        import json_repair

        # Repair is aggressive — it will coerce almost any text into *some*
        # value. Only trust it when it yields a non-empty object, the shape
        # every extraction/quiz/skill-graph payload actually has; a bare
        # string or [] means it found no real structure, so surface the error.
        repaired = json_repair.loads(payload)
        if isinstance(repaired, dict) and repaired:
            _log.warning("JSON reply needed repair (%s) — recovered", exc)
            return repaired
        # Include a snippet of what we couldn't parse so the surfaced error is
        # actionable — the bare CPython message ("Expecting ',' delimiter: line
        # 1 column 3 (char 2)") gives no clue what the model actually returned.
        raise ValueError(f"{exc} — unparsed reply was: {payload[:120]!r}") from exc


def _study_path(project_id: str) -> Path:
    from salient_tutor.study import project_dir

    return project_dir(project_id)
