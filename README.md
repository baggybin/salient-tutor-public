# salient-tutor

![Hero Banner](docs/assets/hero_banner.jpg)

> A Socratic teaching agent with spaced repetition, built on the
> [`salient-core`](https://github.com/baggybin/salient-core) coordination kernel.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

`salient-tutor` is the showcase application for `salient-core`. It
demonstrates that the kernel's coordination infrastructure — bus-as-MCP,
noisy-OR knowledge graph, SM-2 scheduler, operator-mediated delegation —
works in a completely different domain than the security work it was
born from.

## What it does

A Socratic teaching coach that:

- Runs a **9-phase LESSON LOOP** (Diagnose → Objective → Model → Check →
  Anchor → Drill → Reflect → Cards → Elaborate)
- Teaches via **Mermaid diagrams** and **method-of-loci memory palaces**
- Tracks mastery with an **SM-2 spaced-repetition scheduler**
- Consults a **pedagogy knowledge graph** (classical mnemonics + an
  evidence-graded 27-technique learning-methods catalog) to choose how to
  encode stubborn facts and which retrieval/scheduling strategy to drill
- Exports lessons as **Obsidian notes**
- Learns from **uploaded documents** (study projects: upload → extract →
  teach from the document's own structure)
- Runs the parser (librarian) **locally on LM Studio** — or on Claude —
  and **semantic-recalls** study material via a selectable embedding model

Ships with a **security curriculum** (ATT&CK, kill chain, the unicorn
rule) as the default. The LESSON LOOP, scheduler, and gradebook are
domain-agnostic — any subject can be a curriculum.

## The LESSON LOOP

Every lesson runs the same nine phases. Two are decision points — **CHECK**
decides advance vs re-teach, **DRILL** diagnoses the error type on a wrong
answer — and the loop doesn't exit until the **mastery gate** is met (the
current technique demonstrated at the *Apply* Bloom level in a fresh case).

![9-Phase Lesson Loop](docs/diagrams/lesson_loop.jpg)

**DRILL's error taxonomy** decides the re-teach strategy: *Structural*
(knowledge never there → re-teach the model), *Deviation* (knows it,
misunderstood → contrast wrong vs right), *Application* (right idea, wrong
execution → re-run with feedback), *Metacognitive* (blank → drop a Bloom
level and rebuild).

## How it uses the kernel

| Kernel feature | How the tutor uses it |
|---|---|
| **Bus-as-MCP** | Tutor agent gets ~35 typed tools (ask_agent, kg_query, record_review, context_read, etc.) |
| **Noisy-OR KG** | Learner gradebook (`learner:op`), study-project namespaces (`study:<id>:`), mnemonic meta-layer (`pedagogy:`) |
| **SM-2 scheduler** | `record_review(topic, grade)` drives the spaced-repetition schedule |
| **Operator inbox** | Study-project extraction is operator-triggered |
| **Scope gate** | Read-containment hook confines the librarian to the uploads tree |
| **Per-agent endpoint override** | The librarian can be rerouted at a local chat endpoint (LM Studio) while the tutor stays on Claude |
| **Semantic recall** | Embeddings-backed `semantic_recall` over the KG, driven by a periodic backfill |
| **Runner** | Claude-SDK-specific, wired with the bus MCP server |

![Architecture Kernel](docs/diagrams/architecture_kernel.jpg)

The bus MCP server is the connective tissue: it binds the daemon as the tool
backend, so every tool the tutor calls routes back into the in-process
knowledge graph, scheduler, and inbox. SM-2 is not a separate store —
`record_review` runs the scheduling functions and writes the result back into
the KG under `learner:op`, so the gradebook and scheduler share one source of
truth.

## Quick start

> **New here?** The **[How-to / Getting Started guide](docs/HOWTO.md)** walks through
> install (incl. the public kernel), every config option, running, and a first
> session end-to-end. The essentials:

```bash
# Install the kernel + tutor
pip install "git+https://github.com/baggybin/salient-core-public.git"
pip install -e .

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...
export TUTOR_MODEL=claude-opus-4-8[1m]        # pick a model your key can access

# CLI mode
salient-tutor "teach me about photosynthesis"

# Web mode
python -m salient_tutor.web --port 8000
# → open http://localhost:8000
```

## The web modal

`python -m salient_tutor.web` serves a single-page coaching workspace — a
two-column **Coach** surface (streaming conversation + a context rail) plus a
**Library** tab for study projects and a **⚙ Settings** modal for embeddings +
the parser.

- **Streaming replies** over `WS /ws/tutor` — thinking, tool-calls, and text
  stream in live, the way the operator console tails an agent.
- **Skill-map rail** — due/strong/weak/misconception buckets with mastery bars,
  recall-odds and due badges, and a review-load forecast. Click a topic to drill it.
- **Knowledge-base search**, a **lesson-plan picker**, and a **context-usage bar**.
- **Mermaid diagrams** with learner-controlled **step-through** + **WebM export**
  and one-click auto-repair, plus **method-of-loci** memory-palace cards.
- **Read-aloud** (per-message TTS with a voice/pitch/volume/format popover) when
  `MINIMAX_API_KEY` is set — otherwise the audio UI is hidden.
- **History replay** on reload and a **tutor-variant picker** (see env below).

### Workspaces ("schoolbags")

Everything for one profile — chats/history, knowledge graph, gradebook, review
logs, agent configs, and generated `images/` — lives under a single **workspace
directory** (default `<repo>/work`). Run separate, fully-isolated profiles by
pointing at a different directory; each gets its own data, logs, chats, and
images:

```bash
python -m salient_tutor.web --port 8000 --work-root work/alice
python -m salient_tutor.web --port 8001 --work-root work/bob   # separate profile
```

`--work-root` (or the `TUTOR_WORK_ROOT` env var) accepts any path; a relative one
resolves against the repo root, so it's independent of the launch directory. The
active workspace name is exposed at `GET /api/config` (`"workspace"`).

**Autoload.** A plain `python -m salient_tutor.web` (no `--work-root`/env)
**resumes the last-used workspace** — whichever one you last launched is
remembered in a `.salient-tutor-workspace` pointer at the repo root. An explicit
`--work-root` always wins and becomes the new autoload target. First-ever launch
falls back to the default `<repo>/work`.

**Start fresh / reset a workspace:** stop the server, then delete or rename its
directory and restart — a new empty one is created:

```bash
mv work work.archived-$(date +%F)   # keep the old "schoolbag" around
# or: rm -rf work                    # discard it
python -m salient_tutor.web --port 8000
```

Generated images are content-addressed and live under `<work-root>/images`; they
persist across restarts and are never auto-deleted. Even if the cache is cleared,
reopening an old thread deterministically regenerates the same images from the
saved `image` fences.

### Library — study projects

Upload a document (PDF/text/markdown), have the **librarian** agent read and
structure it, then teach from it. Each project card shows a **per-project fact
count**, the document list with extraction-status chips, and **delete** actions:

- **Project delete** (confirm-gated) purges the `study:<id>:` KG namespace, the
  envelope, and the on-disk tree.
- **Per-document delete** purges only that doc's `doc:`/`chunk:` facts, leaving
  siblings and the project-level `sec:` scaffold intact.

PDFs are **pre-extracted to plain text** (pdftotext, with an OCR fallback for
scanned/image-only docs) so *any* model — not just vision-capable ones — can
parse them. The librarian runs either on Claude or on a **local LM Studio**
endpoint while the tutor always stays on Claude:

![Library Extraction Flow](docs/diagrams/library_extraction_flow.jpg)

### ⚙ Settings — embeddings + parser

A topbar gear opens a tabbed modal:

- **🜂 Embeddings** — point salient-tutor at an OpenAI-compatible embeddings
  server (e.g. `http://ai.home:1234`). The model is **selected from what the
  server has loaded**. Switching the model **re-embeds every fact** automatically
  (the modal animates coverage back to full). Falls back to `SALIENT_EMBED_*`
  env or runs inert when unconfigured.
- **📚 Librarian / parser** — route the librarian (the PDF parser) at a **local
  chat endpoint** (LM Studio serving the Anthropic `/v1/messages` shape) so
  parsing is fully local, while the tutor always stays on Claude. The provider
  toggle is gated on a live `/v1/messages` probe.

Both tabs list models with a **loaded-state marker** (`● loaded` / `○
not-loaded`) and a **⚡ Load / ⏏ Unload** toggle that loads or unloads the
selected model in LM Studio directly — no need to leave the app. On a load
failure (typically "no room"), the modal surfaces LM Studio's error and lists
what's currently in memory so the operator can decide what to unload.

### Environment

| Variable | Effect |
|---|---|
| `TUTOR_MODEL` | Model for the tutor orchestrator (default `claude-opus-4-8[1m]`). Set this to a model your key can access. |
| `TUTOR_LIBRARIAN_MODEL` | Model for the librarian read/summarize agent (default `claude-sonnet-5[1m]`). Overridden by a local provider set in the ⚙ modal. |
| `TUTOR_VARIANT_MODEL` | Registers a shadow tutor on a second model; the web modal shows a variant picker. Try `claude-fable-5[1m]` for Fable. `TUTOR_VARIANT_LABEL` names it. |
| `SALIENT_EMBED_BASE_URL` | Default embeddings endpoint (the ⚙ modal overrides at runtime). |
| `SALIENT_EMBED_MODEL` | Default embedding model (the ⚙ modal overrides at runtime). |
| `SALIENT_EMBED_API_KEY` | Embeddings endpoint key, if required. |
| `MINIMAX_API_KEY` | Enables read-aloud (MiniMax T2A). `MINIMAX_API_HOST` / `MINIMAX_GROUP_ID` for the China region. |
| `TUTOR_TTS_CACHE=1` | Cache synthesized audio on disk (`~/.salient-tutor/tts`). |

## Screenshots

<!-- Drop captures into docs/screenshots/ and these render automatically.
     Suggested shots: the Coach workspace, the Library tab, the ⚙ Settings modal. -->

| Coach workspace | Library (study projects) | ⚙ Settings — embeddings + parser |
|---|---|---|
| ![Coach workspace](docs/screenshots/coach.jpg) | ![Library](docs/screenshots/library.jpg) | ![Settings](docs/screenshots/settings.jpg) |

## The pedagogy KG

The tutor ships with a knowledge graph of *how to teach*, spanning both
the classical mnemonic-encoding techniques and the modern learning
science of retrieval, spacing, and transfer. Each technique carries a
rationale node explaining *why* it works, an evidence grade (established
/ promising / contested) with primary-source citations, and edges to the
techniques it composes with.

Two layers:

- **Classical mnemonics** (extracted from memory-research sources): the
  Memory Palace (Method of Loci), Number/Date encoding, the Link Method,
  Name Recall, Mind Mapping, and more.
- **Evidence-graded learning-methods catalog** (`data/learning_methods_catalog.json`,
  27 techniques): the encoding stack above **plus** the Major System /
  PAO / peg systems, and the modern strategies — retrieval practice,
  spaced & interleaved practice, successive relearning, elaboration,
  self-explanation, worked examples, concrete examples, dual coding,
  generation, pretesting, metacognitive calibration, and desirable
  difficulties. Ships with a `myths` list (learning styles, the learning
  pyramid, etc.) so discredited ideas are flagged, not taught.

The tutor consults this meta-layer when it needs to choose HOW to
encode a stubborn fact — or which retrieval/scheduling strategy to drill
with:

```
kg_semantic_query(text="how to memorize port numbers", subject_prefix="pedagogy:")
→ Major System / PAO / Number Shapes / Memory Palace

kg_semantic_query(text="make these sibling concepts stick", subject_prefix="pedagogy:")
→ interleaving / retrieval practice / desirable difficulties
```

The catalog is the source of truth; `tools/transform_learning_catalog.py`
compiles it into the `pedagogy:` bundle (nodes + rationale + line-addressed
prose the ingest slices), re-runnably.

## Project structure

```
salient-tutor/
├── prompts/
│   ├── tutor.md          The LESSON LOOP system prompt (387 lines)
│   └── librarian.md      The document-extractor contract (69 lines)
├── src/salient_tutor/
│   ├── daemon.py         TutorDaemon — composes kernel pieces + embed backfill
│   ├── export.py         Obsidian lesson export
│   ├── study.py          Study-project store + deterministic text extraction
│   ├── pedagogy.py       Mnemonic KG ingestion
│   ├── read_containment.py  Librarian's PreToolUse hook
│   ├── cli.py            CLI entry point
│   └── web.py            FastAPI server (chat modal + study/embed/LM-Studio RPCs)
├── data/
│   ├── pedagogy_bundle.json         classical + learning-methods pedagogy KG
│   ├── learning_methods_catalog.json  27-technique evidence-graded source catalog
│   └── extracted/learning-methods.txt line-addressed prose the ingest slices
├── tools/
│   └── transform_learning_catalog.py  compiles the catalog into the KG bundle
├── web/static/           Vanilla-JS modal (Mermaid, loci, export, settings)
└── tests/                266 tests (prompts, export, scheduler, study, pedagogy,
                          embed/librarian config, LM Studio mgmt, JSON tolerance)
```

Architecture and flow diagrams (Mermaid source + rendered SVGs) live in
[`docs/diagrams.md`](docs/diagrams.md).

## Status

Pre-alpha. See [`salient-core/PLAN.md`](https://github.com/baggybin/salient-core/blob/main/PLAN.md)
for the full roadmap.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
