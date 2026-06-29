# UI Migration Agent — Clean Foundation (v2)

A clean, layered foundation for migrating a **.NET / C# / CSHTML (Razor)**
codebase into **React + TypeScript**, grounded in your existing design system.

This export contains **only the reusable, working pieces** — the analysis
layer, the infrastructure, the API for the stable stages, and the new layered
**passes skeleton**. The page-by-page migration code from the earlier iteration
was intentionally left out; migration is being rebuilt on the passes
architecture.

Vector store is **ChromaDB only** (local file storage, no server).

---

## Architecture: layered migration

The agent migrates the codebase **layer by layer**, not page by page. Five
ordered passes, each one topologically sorted by dependency, each indexing its
output before the next pass begins, with a review gate between passes:

```
Pass 1  MODELS      .cs classes      → TypeScript interfaces
Pass 2  CONTROLLERS .cs controllers  → hooks + typed fetch stubs (no real endpoints)
Pass 3  LAYOUTS     _Layout.cshtml   → layout components
Pass 4  COMPONENTS  partials/shared  → components mapped to your design system
Pass 5  PAGES       entry views      → final page components (composes 1–4)
```

Each pass retrieves the **already-converted** output of earlier passes (from
the artifact store) so later work builds on real migrated TypeScript instead of
re-deriving from .NET.

---

## What's in this foundation

```
backend/app/
├── config.py            # env + model ids (ChromaDB path, gateway, models)
├── gateway.py           # OpenAI-compatible gateway client (chat/embed/rerank)
├── services.py          # role-specific model calls (Sonnet/Haiku/embed/rerank)
├── prompts.py           # shared prompts + RAG context builder
├── main.py              # FastAPI app (project + analyze + setup mounted)
│
├── analysis/            # ── .NET + React analysis (reusable) ──
│   ├── dotnet_grapher.py     # regex page-cluster resolver (hardened)
│   ├── engine.py             # engine swap: regex ⇄ roslyn sidecar
│   ├── razor_constructs.py   # unique Razor construct extractor
│   ├── rule_deriver.py       # Sonnet-derived migration rules
│   ├── component_scanner.py  # @react-docgen/cli component discovery
│   └── component_semantics.py# Haiku semantic pass + React page usage scan
│
├── rag/                 # ── retrieval backbone (ChromaDB) ──
│   ├── stores.py             # ChromaDB VectorStore + code/design stores
│   ├── indexer.py            # index components + code patterns
│   └── retriever.py          # dual-store retrieval + reranking
│
├── passes/              # ── NEW: layered migration skeleton ──
│   ├── toposort.py           # dependency ordering + cycle detection
│   ├── artifact_store.py     # migrated-artifact store (keyed by origin)
│   ├── base.py               # MigrationPass protocol + WorkItem/PassResult
│   ├── manifest.py           # resumable control-flow manifest
│   └── orchestrator.py       # runs passes in order, review gates
│
└── api/                 # ── UI-facing API (stable stages) ──
    ├── events.py             # SSE helpers
    ├── deps.py               # single-project config + paths
    ├── routes_project.py     # POST/GET /api/project, /api/health
    ├── routes_analyze.py     # POST /api/analyze (SSE) + page map reads
    └── routes_setup.py       # POST /api/setup (SSE) + components/rules reads

sidecar/                 # ── optional .NET analysis engine ──
    ├── Program.cs            # Roslyn + Razor → page_map.json
    ├── ControllerIndex.cs    # semantic action→view→model resolution
    ├── RazorAnalyzer.cs      # partials/layout/EditorFor from Razor tree
    └── ...                   # (see sidecar/README.md)

scripts/
    ├── verify_phase0.py      # check models reachable via the gateway
    ├── analyze_dotnet.py     # Stage 1: build page_map.json (--engine regex|roslyn)
    └── agent_setup.py        # Stage 2: scan React + derive rules + seed stores

backend/tests/           # 100 tests, all passing (mocked LLM/stores)
```

---

## What's intentionally NOT here (being rebuilt as passes)

- `migrate/` (assembler, migrator, page_prompts) — the old page-by-page flow
- `pipeline.py` — the original single-file pipeline
- `routes_migrate.py` — the page-by-page migrate API
- The five passes themselves — `models_pass.py` … `pages_pass.py` drop into
  `app/passes/` implementing the `MigrationPass` protocol in `base.py`.

The skeleton is ready for them: register a pass with `register_pass()` and the
orchestrator drives it.

---

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill gateway URL/key, model ids, CHROMA_PATH

# component discovery needs:
npm install -g @react-docgen/cli
```

## Run order (stable stages)

```bash
cd backend
python ../scripts/verify_phase0.py                          # models reachable?
python ../scripts/analyze_dotnet.py --repo /path/to/dotnet --out ../page_map.json
python ../scripts/agent_setup.py --react-repo /path/to/react \
    --dotnet-repo /path/to/dotnet --page-map ../page_map.json \
    --import-base "@your-org/ui" --rules-out ../migration_rules.json
```

## Tests

```bash
cd backend
pytest -v       # 100 tests, all mocked — no live gateway/store needed
```

---

## Models (via your gateway)

| Role | Model |
|---|---|
| Generation / rule derivation | Claude Sonnet 4.6 |
| Classification / semantics / review | Claude Haiku 3.5 |
| Code embedding | BAAI/llm-embedder |
| Doc embedding | Amazon Titan embed-text-v2 |
| Reranking | BAAI/bge-reranker-large |
```
