"""
rag/retriever.py
────────────────
Retrieval + reranking step.

Given a CSHTML query, this module:
  1. Embeds the query with BOTH embedding models (one per store)
  2. Searches both vector stores in parallel
  3. Combines retrieved chunks into one list
  4. Reranks with bge-reranker-large (cross-encoder — handles mixed sources)
  5. Returns the top-N reranked chunks as formatted context strings

The output of retrieve() is a list of plain strings ready to be
injected into the LLM generation prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services import embed_code, embed_docs, rerank
from app.rag.stores import code_store, design_store

logger = logging.getLogger(__name__)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RetrievedContext:
    chunks: list[str]          # formatted text chunks ready for the prompt
    code_hits: int             # how many came from the code store
    design_hits: int           # how many came from the design store
    total_before_rerank: int   # total retrieved before reranking


# ── Core retrieval ────────────────────────────────────────────────────────────

def retrieve(
    cshtml: str,
    top_k_per_store: int = 10,
    top_n_after_rerank: int = 5,
) -> RetrievedContext:
    """
    Retrieve and rerank context from both stores for a given CSHTML input.

    Args:
        cshtml:              The CSHTML content being migrated (used as query).
        top_k_per_store:     How many chunks to retrieve from each store (pre-rerank).
        top_n_after_rerank:  How many chunks to return after reranking.

    Returns:
        RetrievedContext with formatted chunks ready for the generation prompt.
    """
    # ── Step 1: embed the query with both models ──────────────────────────────
    # Each store was indexed with a different embedder, so we query each
    # with its own matching embedding — otherwise similarity scores are meaningless.
    logger.debug("Embedding query for code store (BAAI)")
    code_query_vec = embed_code(cshtml).first

    logger.debug("Embedding query for design store (Titan)")
    design_query_vec = embed_docs(cshtml).first

    # ── Step 2: search both stores ────────────────────────────────────────────
    code_results    = _search_code_store(code_query_vec, top_k_per_store)
    design_results  = _search_design_store(design_query_vec, top_k_per_store)

    code_hits   = len(code_results)
    design_hits = len(design_results)
    total       = code_hits + design_hits

    logger.debug("Retrieved %d code + %d design chunks", code_hits, design_hits)

    if total == 0:
        logger.warning("Both stores returned 0 results — stores may not be indexed yet")
        return RetrievedContext(
            chunks=[], code_hits=0, design_hits=0, total_before_rerank=0
        )

    # ── Step 3: combine into one flat list of text strings ───────────────────
    # Tag each chunk with its store source so the LLM knows what it's reading.
    all_chunks = (
        [_format_code_chunk(r.payload) for r in code_results]
        + [_format_design_chunk(r.payload) for r in design_results]
    )

    # If only one store returned results, skip reranking (nothing to compare)
    if total == 1:
        return RetrievedContext(
            chunks=all_chunks,
            code_hits=code_hits,
            design_hits=design_hits,
            total_before_rerank=total,
        )

    # ── Step 4: rerank combined list against the original CSHTML ─────────────
    # bge-reranker-large is a cross-encoder — it scores each (query, chunk) pair
    # directly, so it handles chunks from two different embedding spaces equally.
    logger.debug("Reranking %d combined chunks → top %d", total, top_n_after_rerank)
    rerank_resp = rerank(
        query=cshtml,
        documents=all_chunks,
        top_n=top_n_after_rerank,
    )
    reranked = rerank_resp.reranked_documents(all_chunks)

    logger.debug(
        "Rerank complete: %d → %d chunks | code_hits=%d design_hits=%d",
        total, len(reranked), code_hits, design_hits,
    )

    return RetrievedContext(
        chunks=reranked,
        code_hits=code_hits,
        design_hits=design_hits,
        total_before_rerank=total,
    )


# ── Store searches ────────────────────────────────────────────────────────────

def _search_code_store(query_vector: list[float], top_k: int):
    try:
        return code_store().search(query_vector, top_k=top_k)
    except Exception as e:
        logger.error("Code store search failed: %s", e)
        return []


def _search_design_store(query_vector: list[float], top_k: int):
    try:
        return design_store().search(query_vector, top_k=top_k)
    except Exception as e:
        logger.error("Design store search failed: %s", e)
        return []


# ── Chunk formatters ──────────────────────────────────────────────────────────
# These format the stored payload into strings the LLM can clearly parse.

def _format_code_chunk(payload: dict | None) -> str:
    if not payload:
        return ""
    return (
        f"[MIGRATION PATTERN]\n"
        f"Source: {payload.get('source', 'unknown')}\n"
        f"{payload.get('text', '')}"
    )


def _format_design_chunk(payload: dict | None) -> str:
    if not payload:
        return ""
    return (
        f"[DESIGN SYSTEM COMPONENT]\n"
        f"Source: {payload.get('source', 'unknown')}\n"
        f"{payload.get('text', '')}"
    )
