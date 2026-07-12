# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-12

Second public snapshot, consolidating the `0.0.x` line into a first
feature-bearing release: a durable lesson loop, an additional runtime provider,
and a websearch roster agent.

### Added
- **Durable tutor lesson sessions**: lesson state persists across runs —
  tutor cards are stored, assessments are scored with review applications fed
  back into the schedule, curriculum records are bindable, and provenance
  analytics / migration reports track where facts came from.
- **Codex runtime provider**: OpenAI Codex is available as a backend provider
  alongside the Claude SDK, surfaced in the Agents tab with a probe API and
  packaging support; per-agent tool policy and turn caps carry onto the codex
  path.
- **Websearch roster agent**: the roster agent that `tutor.md` already
  delegates to for web search is now included.

### Changed
- Provider probe wired into the Agents tab; adapts to core's
  `AgentRunner` `backend_factory` seam.

### Fixed
- Correctness fixes in the durable tutor loop.
- Legacy `curriculum:prereq` edges migrate to `curriculum:inferred:`.
- Probe endpoint hardened.

## [0.0.1] - 2026-07-08

First public snapshot. A spaced-repetition Socratic teaching agent built on the
`salient-core` kernel: a 9-phase lesson loop, SM-2 skill map, Mermaid diagrams,
and method-of-loci memory palaces with opt-in diffusion illustrations.
