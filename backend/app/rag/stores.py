"""
rag/stores.py
─────────────
ChromaDB vector store client (local file storage — no server required).

Two collections are managed here:
  CODE_COLLECTION    — CSHTML patterns, migration rules, usage examples
                       Embedded with BAAI/llm-embedder
  DESIGN_COLLECTION  — Design system component docs
                       Embedded with Amazon Titan embed-text-v2

Each chunk's payload:
  { "text": ..., "source": ..., "type": ..., "tags": [...] }

ChromaDB metadata must be scalar (str/int/float/bool) and non-empty, so:
  - list fields (tags) are JSON-serialised to a string on write
    and restored to a list on read (see _clean_metadata / _restore_metadata)
  - empty lists / empty strings / None are dropped

Usage:
    from app.rag.stores import code_store, design_store
    design_store().upsert(chunks, embeddings)
    results = design_store().search(query_vector, top_k=10)
"""

from __future__ import annotations

import json
import uuid
import logging
from dataclasses import dataclass

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings

logger = logging.getLogger(__name__)

# ── Collection names ──────────────────────────────────────────────────────────

CODE_COLLECTION   = "code_patterns"
DESIGN_COLLECTION = "design_system"


# ── Chunk dataclass ───────────────────────────────────────────────────────────

@dataclass
class Chunk:
    text: str
    source: str
    type: str                        # component_doc | migration_example | atom_mapping | usage_example | layout_rule
    tags: list[str] | None = None

    def to_payload(self) -> dict:
        return {
            "text":   self.text,
            "source": self.source,
            "type":   self.type,
            "tags":   self.tags or [],
        }


# ── Search result stand-in (mirrors the fields retriever.py uses) ─────────────

@dataclass
class _SearchResult:
    payload: dict
    score: float


# ── Metadata helpers (Chroma needs scalar, non-empty metadata) ────────────────

def _clean_metadata(payload: dict) -> dict:
    """
    Chroma metadata values must be scalar (str/int/float/bool) and non-empty.
    Lists (e.g. tags) become a JSON string; None/empty values are dropped.
    """
    clean: dict = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            clean[k] = json.dumps(list(v))
        elif isinstance(v, (str, int, float, bool)):
            if v == "":
                continue
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def _restore_metadata(meta: dict) -> dict:
    """Reverse _clean_metadata: turn JSON-string `tags` back into a list."""
    out = dict(meta or {})
    val = out.get("tags")
    if isinstance(val, str):
        try:
            out["tags"] = json.loads(val)
        except json.JSONDecodeError:
            out["tags"] = [val]
    return out


# ── VectorStore ───────────────────────────────────────────────────────────────

class VectorStore:
    """Wrapper around a single Chroma collection (code or design)."""

    def __init__(self, collection: str):
        self.collection = collection
        self._client = chromadb.PersistentClient(
            path=settings.chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Chroma collection ready: %s (path=%s)", collection, settings.chroma_path)

    # ── Write ─────────────────────────────────────────────────────────────────

    def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]):
        """Insert/update chunks with embeddings (idempotent via content-hash ids)."""
        assert len(chunks) == len(embeddings), \
            f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must match"

        ids = [
            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self.collection}:{c.source}:{c.text[:100]}"))
            for c in chunks
        ]
        self._col.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[_clean_metadata(c.to_payload()) for c in chunks],
        )
        logger.info("Upserted %d chunks into %s", len(chunks), self.collection)

    # ── Read ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filter_type: str | None = None,
    ) -> list:
        """Vector similarity search. Returns _SearchResult objects (.payload, .score)."""
        total = self._col.count()
        if total == 0:
            return []
        n_results = min(top_k, total)

        where = {"type": filter_type} if filter_type else None
        results = self._col.query(
            query_embeddings=[query_vector],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        hits: list = []
        docs      = results["documents"][0]
        metas     = results["metadatas"][0]
        distances = results["distances"][0]
        for doc, meta, dist in zip(docs, metas, distances):
            hits.append(_SearchResult(
                payload={**_restore_metadata(meta), "text": doc},
                score=1 - dist,    # cosine distance → similarity
            ))
        logger.debug("search %s top_k=%d filter=%s → %d results",
                     self.collection, top_k, filter_type, len(hits))
        return hits

    def count(self) -> int:
        return self._col.count()

    def delete_collection(self):
        self._client.delete_collection(self.collection)
        self._col = self._client.get_or_create_collection(
            name=self.collection,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Dropped and recreated collection: %s", self.collection)


# ── Module-level singletons (lazy) ────────────────────────────────────────────

_code_store = None
_design_store = None


def code_store() -> VectorStore:
    global _code_store
    if _code_store is None:
        _code_store = VectorStore(CODE_COLLECTION)
    return _code_store


def design_store() -> VectorStore:
    global _design_store
    if _design_store is None:
        _design_store = VectorStore(DESIGN_COLLECTION)
    return _design_store
