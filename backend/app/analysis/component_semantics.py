"""
analysis/component_semantics.py
───────────────────────────────
Two A2 helpers that build on top of structural component extraction:

  1. enrich_component_semantics()
     react-docgen-typescript gives accurate STRUCTURE (props/types) but not
     SEMANTICS. This adds a one-line "use this when..." description per
     component via a cheap Haiku call, so the migration agent can pick the
     right component (e.g. PhoneInput vs TextInput) by meaning, not just name.

  2. scan_react_pages()
     Reads existing React pages and indexes them as USAGE KNOWLEDGE (no
     pairing) — how components compose and how API calls are wired. This
     teaches the agent your real patterns (typed services, hooks) without
     pretending a React page maps to a specific .NET page.

Per chosen config: Haiku for the semantic pass (cheap, one call per component).
"""

from __future__ import annotations

import os
import glob
import json
import logging

from app.services import classify_file  # Haiku-backed (reused as a generic Haiku call)
from app.rag.indexer import ComponentDoc
from app.rag.stores import Chunk, code_store

logger = logging.getLogger(__name__)


# ── 1. Semantic enrichment (Haiku) ────────────────────────────────────────────

_SEMANTIC_SYSTEM = """
You write a single, concrete usage sentence for a React component, to help
another engineer pick the right component during a .NET-to-React migration.

Given a component's name, props, and any existing description, output ONE
sentence describing WHEN to use it and what kind of data/field it is for.
Be specific about the field type if the name implies one (e.g. phone, date,
email, currency).

Reply ONLY with valid JSON, no markdown:
{"semantic": "<one concrete usage sentence>"}
""".strip()


def _haiku_semantic(component: ComponentDoc) -> str:
    """Call Haiku for a one-line semantic description. Falls back gracefully."""
    user_msg = (
        f"Component: {component.name}\n"
        f"Tier: {component.tier or 'unknown'}\n"
        f"Props: {component.props}\n"
        f"Existing description: {component.description}"
    )
    # classify_file is a thin Haiku chat wrapper; we reuse it with a custom system
    # prompt by calling the underlying gateway through services. To keep the
    # services layer as the only entry point, we use a dedicated helper:
    from app.gateway import chat
    from app.config import settings
    resp = chat(
        model=settings.model_haiku,
        messages=[{"role": "user", "content": user_msg}],
        system=_SEMANTIC_SYSTEM,
        max_tokens=128,
        temperature=0.0,
    )
    raw = resp.text.strip()
    try:
        return json.loads(raw).get("semantic", "").strip()
    except json.JSONDecodeError:
        # Fall back to the raw text if it's a plain sentence, else empty
        if raw and len(raw) < 300 and "{" not in raw:
            return raw
        logger.warning("Semantic parse failed for %s", component.name)
        return ""


def enrich_component_semantics(
    components: list[ComponentDoc],
    progress=None,
) -> list[ComponentDoc]:
    """
    Add a semantic usage sentence to each component's description.
    Returns the same list with descriptions augmented.

    Args:
        components: Structurally-extracted ComponentDocs (from docgen).
        progress:   Optional callback(i, total, name).
    """
    total = len(components)
    for i, c in enumerate(components):
        if progress:
            progress(i + 1, total, c.name)
        semantic = _haiku_semantic(c)
        if semantic:
            # Prepend the semantic sentence; keep the structural description too
            c.description = f"{semantic} {c.description}".strip()
        logger.debug("Enriched %s: %s", c.name, semantic[:60])
    return components


# ── 2. React page usage scanning ──────────────────────────────────────────────

def scan_react_pages(
    react_repo: str,
    pages_dir: str = "src/pages",
    max_chars: int = 4000,
    progress=None,
) -> int:
    """
    Index existing React pages as usage knowledge in the Code Pattern store.

    These are NOT paired to .NET pages — they're stored as examples of how
    YOUR codebase composes components and wires API calls, so the migration
    agent can imitate those patterns.

    Args:
        react_repo: Path to the React repo root.
        pages_dir:  Pages directory relative to the repo.
        max_chars:  Truncate large page files to this many chars.
        progress:   Optional callback(i, total, filename).

    Returns:
        Number of pages indexed.
    """
    pages_path = os.path.join(react_repo, pages_dir)
    page_glob = os.path.join(pages_path, "**", "*.tsx")
    files = [
        f for f in glob.glob(page_glob, recursive=True)
        if not any(skip in f for skip in [".test.", ".spec.", ".stories."])
    ]

    if not files:
        logger.warning("No React pages found under %s", pages_path)
        return 0

    chunks: list[Chunk] = []
    total = len(files)
    for i, abs_path in enumerate(files):
        if progress:
            progress(i + 1, total, os.path.basename(abs_path))
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n/* ...truncated... */"

        rel = os.path.relpath(abs_path, react_repo)
        chunk_text = (
            f"[REACT PAGE USAGE EXAMPLE]\n"
            f"File: {rel}\n"
            f"This shows how components are composed and how API calls are wired "
            f"in this codebase.\n\n{content}"
        )
        chunks.append(Chunk(
            text=chunk_text,
            source=rel,
            type="usage_example",
            tags=["react_page", "usage"],
        ))

    # Embed + index into the code store (usage lives alongside migration patterns)
    from app.services import embed_code
    logger.info("Indexing %d React pages as usage knowledge", len(chunks))
    embeddings = []
    BATCH = 16
    for j in range(0, len(chunks), BATCH):
        batch = chunks[j: j + BATCH]
        resp = embed_code([c.text for c in batch])
        embeddings.extend(resp.embeddings)
    code_store().upsert(chunks, embeddings)
    return len(chunks)
