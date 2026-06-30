# UI Migration Agent

An autonomous agent that migrates a **.NET / C# / CSHTML (Razor)** codebase into
**React + TypeScript**, grounded in your existing design system. Migration runs
**layer by layer** — models, controllers, layouts, components, then pages — with
a review gate between each layer.

Roslyn-free: analysis is pure Python (regex grapher). Vector store is **ChromaDB**
(local file storage, no server). No Qdrant.

---

## How it works

```
 .NET repo --> ANALYZE --> page_map.json
                  |
 React repo --> SETUP --> design-system components + derived rules -> vector stores
                  |
              MIGRATE (five ordered passes, each indexes before the next)
                  |
   Pass 1  MODELS      .cs classes      -> types/*.ts        (TypeScript interfaces)
   Pass 2  CONTROLLERS .cs controllers  -> hooks/*.ts        (typed fetch stubs)
   Pass 3  LAYOUTS     _Layout.cshtml   -> layouts/*.tsx     (app shell + slots)
   Pass 4  COMPONENTS  partials         -> components/*.tsx  (design-system mapped)
   Pass 5  PAGES       entry views      -> pages/*.tsx       (composes 1-4)
                  |
              React/TypeScript project + manifest + artifact records
```

Each pass is **topologically sorted** (a model that references another converts
second), **indexes its output** into the artifact store before the next pass, and
**stops at a review gate** so you approve before advancing. Later passes retrieve
the already-converted artifacts (real interfaces, hooks, components) instead of
re-deriving from .NET.

---

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill: gateway URL/key, model ids, CHROMA_PATH

# component discovery needs the React docgen CLI:
npm install -g @react-docgen/cli
```

`.env` keys:

```
LLM_GATEWAY_URL=...         # OpenAI-compatible gateway
LLM_GATEWAY_KEY=...
MODEL_SONNET=claude-sonnet-4-6
MODEL_HAIKU=claude-haiku-3-5
EMBED_CODE_MODEL=BAAI/llm-embedder
EMBED_DOCS_MODEL=amazon.titan-embed-text-v2
RERANK_MODEL=BAAI/bge-reranker-large
CHROMA_PATH=./chroma_storage
```

---

## Run -- full pipeline (CLI)

```bash
cd backend

# 0. Verify the gateway + models are reachable
python ../scripts/verify_phase0.py

# 1. ANALYZE -- build the page map from the .NET repo
python ../scripts/analyze_dotnet.py \
    --repo /path/to/dotnet-repo \
    --out ../page_map.json

# 2. SETUP -- scan the React design system + derive rules + seed stores
python ../scripts/agent_setup.py \
    --react-repo /path/to/react-repo \
    --dotnet-repo /path/to/dotnet-repo \
    --page-map ../page_map.json \
    --import-base "@your-org/ui" \
    --rules-out ../migration_rules.json

# 3. MIGRATE -- run all five passes (auto-approve each review gate)
python ../scripts/migrate.py \
    --dotnet-repo /path/to/dotnet-repo \
    --react-repo /path/to/react-repo \
    --page-map ../page_map.json \
    --out ../migrated \
    --auto-approve
```

Output lands in `../migrated/` as `types/`, `hooks/`, `layouts/`, `components/`,
`pages/`, plus `manifest.json` (control flow) and `artifact_records.json`
(source of truth).

### Run one layer at a time (with review between each)

```bash
python ../scripts/migrate.py --dotnet-repo ... --react-repo ... --page-map ... --out ../migrated --layer model
# review ../migrated/types, then:
python ../scripts/migrate.py ... --approve          # advance to controllers
python ../scripts/migrate.py ... --layer controller
python ../scripts/migrate.py ... --approve
# ... and so on through layout, component, page

python ../scripts/migrate.py ... --status           # see where you are
python ../scripts/migrate.py ... --resume            # run the current layer
```

---

## Run -- as an API (for the UI)

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/project` | configure the repo pair |
| GET  | `/api/health` | gateway + store reachability |
| POST | `/api/analyze` | Stage 1 analyze (SSE) |
| GET  | `/api/pagemap`, `/api/pagemap/unresolved`, `/api/pagemap/page/{name}` | page map reads |
| POST | `/api/setup` | Stage 2 setup (SSE) |
| GET  | `/api/components`, `/api/rules`, `/api/rules/{kind}` | setup reads |
| POST | `/api/migrate/layer` | run the current layer (SSE) |
| POST | `/api/migrate/approve` | approve the review gate -> advance |
| GET  | `/api/migrate/status` | per-layer status (for the stepper) |
| GET  | `/api/migrate/artifacts`, `/api/migrate/artifact/{origin}` | generated output |

Open `http://localhost:8000/docs` for the interactive schema.

---

## Tests

```bash
cd backend
pytest -q          # 122 tests, all mocked -- no live gateway/store needed
```

---

## Project layout

```
backend/app/
  config.py  gateway.py  services.py  prompts.py  main.py
  analysis/          # .NET + React analysis
    dotnet_grapher.py  engine.py  razor_constructs.py
    rule_deriver.py  component_scanner.py  component_semantics.py
  rag/               # ChromaDB stores + indexer + retriever
  passes/            # -- the layered migration --
    toposort.py            # dependency ordering
    csharp_parser.py       # regex C# parser (Python-only)
    artifact_store.py      # migrated-artifact store (keyed by origin)
    base.py  manifest.py  orchestrator.py
    models_pass.py         # Pass 1
    controllers_pass.py    # Pass 2
    layouts_pass.py        # Pass 3
    components_pass.py     # Pass 4
    pages_pass.py          # Pass 5
    registry.py            # wires all five into the orchestrator
  api/               # project / analyze / setup / migrate routes (+ SSE)

scripts/   verify_phase0.py  analyze_dotnet.py  agent_setup.py  migrate.py
backend/tests/   122 tests
```

---

## Notes & limits (honest)

- **Roslyn-free by choice.** The C# parser (`csharp_parser.py`) is regex-based:
  it handles common MVC model/controller shapes (auto-properties, generics,
  inheritance) well, but won't trace dynamic `return View(variable)` names --
  those are flagged as unresolved in the page map and surfaced as page TODOs.
- **API logic is intentionally not migrated.** Controllers become hooks with
  **typed fetch stubs** (`// TODO: wire endpoint`), not working endpoints.
- The agent does the heavy lifting; **you review** at each gate. Low-confidence
  outputs and dropped server-side logic are flagged, never silently lost.
- Tests mock the LLM -- "passing" means the code is correct and wired; real code
  quality depends on your gateway + models at run time.
