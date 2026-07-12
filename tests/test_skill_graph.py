"""Prerequisite-DAG skill map — assembly (pure), edge persistence, orchestration.

_assemble_skill_graph is pure (no LLM/KG); _persisted_edges + skill_graph run
against a REAL KnowledgeGraph on a tmp work_root; _generate_and_persist_edges
drives the tutor via a stubbed prompt.
"""

from __future__ import annotations

import asyncio

from salient_tutor.daemon import _CURRICULUM_PREFIX, _PREREQ_PREDICATE, TutorDaemon


def _daemon(tmp_path, monkeypatch, *, reply=None, raises=False):
    monkeypatch.delenv("TUTOR_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("TUTOR_VARIANT_MODEL", raising=False)
    d = TutorDaemon(work_root=tmp_path / "work")
    d.agent_configs = {"tutor": {}}

    async def _fake_prompt(agent, message, *, timeout=120.0):
        if raises:
            raise RuntimeError("tutor down")
        return reply

    monkeypatch.setattr(d, "prompt", _fake_prompt)
    return d


def _profile(strong=(), weak=(), due=(), misc=()):
    return {
        "strong": [{"topic": t} for t in strong],
        "weak": [{"topic": t} for t in weak],
        "due": [{"topic": t} for t in due],
        "misconceptions": [{"topic": t} for t in misc],
    }


# ── _assemble_skill_graph (pure) ──────────────────────────────────────────────


def test_assemble_status_buckets(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    profile = _profile(strong=["a"], weak=["b"], due=["b"], misc=["c"])
    # a mastered; b is weak AND due → due wins; c misconception;
    # d not studied, its only prereq (a) is mastered → available;
    # e not studied, prereq (b) not mastered → locked.
    edges = [("a", "d"), ("b", "e")]
    g = d._assemble_skill_graph(profile, edges)
    st = {n["id"]: n["status"] for n in g["nodes"]}
    assert st["a"] == "mastered"
    assert st["b"] == "due"  # due takes priority over learning
    assert st["c"] == "misconception"
    assert st["d"] == "available"
    assert st["e"] == "locked"
    assert sorted(g["edges"]) == [["a", "d"], ["b", "e"]]
    assert g["counts"]["mastered"] == 1


def test_assemble_drops_dangling_edges(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    # every referenced topic becomes a node, so nothing is dangling here;
    # a truly empty profile + no edges → empty graph.
    g = d._assemble_skill_graph(_profile(), [])
    assert g["nodes"] == []
    assert g["edges"] == []


# ── _persisted_edges (real KG) ────────────────────────────────────────────────


def test_persisted_edges_roundtrip(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    d.kg.assert_fact(
        f"{_CURRICULUM_PREFIX}a", _PREREQ_PREDICATE, f"{_CURRICULUM_PREFIX}b", agent="curriculum"
    )
    d.kg.assert_fact(
        f"{_CURRICULUM_PREFIX}b", _PREREQ_PREDICATE, f"{_CURRICULUM_PREFIX}c", agent="curriculum"
    )
    # a non-prereq curriculum fact must be ignored
    d.kg.assert_fact(f"{_CURRICULUM_PREFIX}a", "note", f"{_CURRICULUM_PREFIX}x", agent="curriculum")
    edges = sorted(d._persisted_edges())
    assert edges == [("a", "b"), ("b", "c")]


# ── _migrate_curriculum_prefix (real KG) ──────────────────────────────────────


def _bare_curriculum_subjects(kg):
    return [
        f["subject"]
        for f in kg.export_by_subject_prefix("curriculum:")
        if not f["subject"].startswith(("curriculum:track:", _CURRICULUM_PREFIX))
    ]


def test_migrate_curriculum_prefix_relabels_legacy_edges(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    # Legacy bare-prefix prereq edges from an older build.
    d.kg.assert_fact("curriculum:a", _PREREQ_PREDICATE, "curriculum:b", agent="curriculum")
    d.kg.assert_fact("curriculum:b", _PREREQ_PREDICATE, "curriculum:c", agent="curriculum")
    # A seeded track fact and an already-migrated edge must be left untouched.
    d.kg.assert_fact(
        "curriculum:track:blue:module:m:topic:t", "in_track", "blue", agent="curriculum"
    )
    d.kg.assert_fact(
        f"{_CURRICULUM_PREFIX}x", _PREREQ_PREDICATE, f"{_CURRICULUM_PREFIX}y", agent="curriculum"
    )

    migrated = d._migrate_curriculum_prefix()
    assert migrated == 2
    # All edges now readable under the new prefix (legacy + pre-existing).
    assert sorted(d._persisted_edges()) == [("a", "b"), ("b", "c"), ("x", "y")]
    # No bare-prefix rows leak into KB search anymore; the seeded track fact survives.
    assert _bare_curriculum_subjects(d.kg) == []
    assert [f["subject"] for f in d.kg.export_by_subject_prefix("curriculum:track:")] == [
        "curriculum:track:blue:module:m:topic:t"
    ]

    # Idempotent: a second run migrates nothing and leaves the graph intact.
    assert d._migrate_curriculum_prefix() == 0
    assert sorted(d._persisted_edges()) == [("a", "b"), ("b", "c"), ("x", "y")]


# ── _generate_and_persist_edges (stubbed tutor) ───────────────────────────────


def test_generate_persists_edges(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, reply='{"edges": [["a","b"],["b","c"]], "next": ["c"]}')
    edges = asyncio.run(d._generate_and_persist_edges(["a", "b"]))
    assert sorted(edges) == [("a", "b"), ("b", "c")]
    assert sorted(d._persisted_edges()) == [("a", "b"), ("b", "c")]  # actually persisted


def test_generate_bad_json_persists_nothing(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, reply="no json here")
    assert asyncio.run(d._generate_and_persist_edges(["a"])) == []
    assert d._persisted_edges() == []


def test_generate_tutor_failure_degrades(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, raises=True)
    assert asyncio.run(d._generate_and_persist_edges(["a"])) == []


# ── skill_graph (orchestration) ───────────────────────────────────────────────


def test_skill_graph_empty_gradebook(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    g = asyncio.run(d.skill_graph())
    assert g["nodes"] == []
    assert "note" in g


def test_skill_graph_uses_persisted_edges_without_tutor(tmp_path, monkeypatch):
    # A tutor call would raise — proves persisted edges are used, no generation.
    d = _daemon(tmp_path, monkeypatch, raises=True)
    d.record_review("a", "good")  # populate the gradebook (a is now studied)
    d.kg.assert_fact(
        f"{_CURRICULUM_PREFIX}a", _PREREQ_PREDICATE, f"{_CURRICULUM_PREFIX}b", agent="curriculum"
    )
    g = asyncio.run(d.skill_graph())
    st = {n["id"]: n["status"] for n in g["nodes"]}
    assert "a" in st and "b" in st
    assert g["edges"] == [["a", "b"]]
