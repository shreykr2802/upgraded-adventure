"""
rag/indexer.py
──────────────
Indexing functions — converts your React component library and
migration mapping rules into chunks and stores them in ChromaDB.

Two indexing jobs:

  index_design_system(components)
    — Call this once, then re-run whenever your design system changes.
    — Reads your atom component definitions and indexes them into
      the Design System store using Titan embeddings.

  index_code_pattern(patterns)
    — Seed this with your atom-mapping table and past migrations.
    — Indexes into the Code Pattern store using BAAI embeddings.

Input formats are simple dicts so you can feed them from:
  - JSON files extracted from your component library
  - Storybook component metadata
  - Manual mapping tables
  - Past migration examples you've done by hand

Run the seeding scripts in scripts/ to populate both stores
before running the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services import embed_code, embed_docs
from app.rag.stores import Chunk, code_store, design_store

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32   # embed this many chunks per API call


# ── Input types ───────────────────────────────────────────────────────────────

@dataclass
class ComponentDoc:
    """
    One entry from your React design system component library.

    Example:
        ComponentDoc(
            name="TextInput",
            import_path="@baxter/ui/atoms",
            props="label: string; value: string; onChange: (v: string) => void; placeholder?: string",
            usage="<TextInput label='First name' value={name} onChange={setName} />",
            description="Single-line text input. Use for all free-text form fields.",
            tags=["input", "form", "text", "field"],
        )
    """
    name: str
    import_path: str
    props: str
    usage: str
    description: str
    tags: list[str] | None = None
    tier: str | None = None   # "atom" | "molecule" | "organism" — from folder structure


@dataclass
class CodePattern:
    """
    A known CSHTML → React mapping rule or past migration example.

    Example:
        CodePattern(
            cshtml_pattern="@Html.TextBoxFor(m => m.X)",
            react_equivalent="<TextInput label='...' value={model.x} onChange={...} />",
            notes="Replace all TextBoxFor with TextInput from @baxter/ui/atoms",
            tags=["input", "textbox", "form"],
        )
    """
    cshtml_pattern: str
    react_equivalent: str
    notes: str
    tags: list[str] | None = None


# ── Chunk builders ────────────────────────────────────────────────────────────

def _component_to_chunk(c: ComponentDoc) -> Chunk:
    """
    Convert a ComponentDoc into an indexable text chunk.
    The text is structured so both the embedder and the LLM can use it.
    """
    tier_line = f"Tier: {c.tier}\n" if c.tier else ""
    text = f"""
Component: {c.name}
{tier_line}Import: import {{ {c.name} }} from '{c.import_path}'
Description: {c.description}
Props: {c.props}
Usage example: {c.usage}
""".strip()

    # Fold tier into tags so retrieval can filter by atom/molecule/organism
    tags = list(c.tags or [])
    if c.tier and c.tier not in tags:
        tags.append(c.tier)

    return Chunk(
        text=text,
        source=f"{c.name}.tsx",
        type="component_doc",
        tags=tags,
    )


def _pattern_to_chunk(p: CodePattern) -> Chunk:
    """Convert a CodePattern into an indexable text chunk."""
    text = f"""
CSHTML pattern: {p.cshtml_pattern}
React equivalent: {p.react_equivalent}
Notes: {p.notes}
""".strip()

    return Chunk(
        text=text,
        source="mapping_rules",
        type="migration_example" if "example" in p.notes.lower() else "atom_mapping",
        tags=p.tags or [],
    )


# ── Public indexing functions ─────────────────────────────────────────────────

def index_design_system(components: list[ComponentDoc]):
    """
    Index React design system components into the Design System store.
    Uses Amazon Titan embed-text-v2.

    Args:
        components: List of ComponentDoc objects describing your atoms.

    Example:
        from app.rag.indexer import index_design_system, ComponentDoc
        index_design_system([
            ComponentDoc(
                name="TextInput",
                import_path="@baxter/ui/atoms",
                props="label: string; value: string; onChange: ...",
                usage="<TextInput label='Name' value={v} onChange={setV} />",
                description="Single-line text input.",
                tags=["input", "form", "text"],
            ),
            ...
        ])
    """
    if not components:
        logger.warning("index_design_system called with empty list — nothing to index")
        return

    chunks = [_component_to_chunk(c) for c in components]
    logger.info("Indexing %d design system components", len(chunks))

    # Embed in batches to avoid large single API calls
    all_embeddings = _embed_in_batches(
        texts=[c.text for c in chunks],
        embed_fn=embed_docs,
        label="design",
    )

    design_store().upsert(chunks, all_embeddings)
    logger.info("Design system indexing complete. Total: %d", design_store().count())


def index_code_patterns(patterns: list[CodePattern]):
    """
    Index CSHTML→React mapping rules into the Code Pattern store.
    Uses BAAI/llm-embedder.

    Args:
        patterns: List of CodePattern objects (atom mappings + past migrations).

    Example:
        from app.rag.indexer import index_code_patterns, CodePattern
        index_code_patterns([
            CodePattern(
                cshtml_pattern="@Html.TextBoxFor(m => m.X)",
                react_equivalent="<TextInput value={model.x} onChange={...} />",
                notes="Replace all TextBoxFor with TextInput",
                tags=["textbox", "input"],
            ),
            ...
        ])
    """
    if not patterns:
        logger.warning("index_code_patterns called with empty list — nothing to index")
        return

    chunks = [_pattern_to_chunk(p) for p in patterns]
    logger.info("Indexing %d code patterns", len(chunks))

    all_embeddings = _embed_in_batches(
        texts=[c.text for c in chunks],
        embed_fn=embed_code,
        label="code",
    )

    code_store().upsert(chunks, all_embeddings)
    logger.info("Code pattern indexing complete. Total: %d", code_store().count())


def index_migration_example(
    cshtml: str,
    tsx: str,
    source_file: str,
    tags: list[str] | None = None,
):
    """
    Index a completed migration as a future example.
    Call this after a migration has been manually reviewed and accepted —
    it feeds the pipeline's own successful outputs back into the code store,
    making future migrations progressively better.

    Args:
        cshtml:      The original CSHTML that was migrated.
        tsx:         The accepted React component output.
        source_file: Filename for reference e.g. "HomeIndex.cshtml"
        tags:        Optional tags.
    """
    pattern = CodePattern(
        cshtml_pattern=cshtml[:500],   # store a meaningful excerpt
        react_equivalent=tsx[:500],
        notes=f"Accepted migration example from {source_file}",
        tags=tags or [],
    )
    index_code_patterns([pattern])
    logger.info("Indexed accepted migration example from %s", source_file)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _embed_in_batches(texts: list[str], embed_fn, label: str) -> list[list[float]]:
    """Embed a list of texts in batches, return flat list of vectors."""
    all_embeddings: list[list[float]] = []
    total_batches = (len(texts) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i: i + EMBED_BATCH_SIZE]
        batch_num = i // EMBED_BATCH_SIZE + 1
        logger.debug("Embedding %s batch %d/%d (%d texts)", label, batch_num, total_batches, len(batch))
        resp = embed_fn(batch)
        all_embeddings.extend(resp.embeddings)

    return all_embeddings
