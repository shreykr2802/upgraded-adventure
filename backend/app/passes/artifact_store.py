"""
passes/artifact_store.py
────────────────────────
The "migrated artifact" store — the spine of the layered migration.

Every pass writes its converted output here; later passes read it so they
build on already-migrated TypeScript instead of re-deriving from .NET.

Key model (confirmed design):
  - id (Chroma key)  = origin    e.g. "Models/UserModel.cs"   (stable, unique)
  - searchable field = symbol    e.g. "UserModel"             (embedded + queryable)
  - embedded on      = output_code + symbol
  - metadata         = layer, output_path, symbol, depends_on, status

This is a THIRD logical store, separate from:
  - design_store  (your hand-built React design-system components)
  - code_store    (derived migration rules + usage examples)

It uses the same ChromaDB-backed VectorStore the rest of the system uses, in a
new collection. Embedding uses the code embedder (BAAI/llm-embedder) since the
content is generated code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

ARTIFACT_COLLECTION = "migrated_artifacts"

# Valid layers, in pipeline order.
LAYERS = ["model", "controller", "layout", "component", "page"]


@dataclass
class MigratedArtifact:
    origin: str                      # .NET source path — the KEY
    layer: str                       # one of LAYERS
    symbol: str                      # exported TS name — searchable
    output_path: str                 # where the TS landed (e.g. types/UserModel.ts)
    output_code: str                 # the generated TypeScript
    depends_on: list[str] = field(default_factory=list)   # origins this needs
    status: str = "generated"        # generated | reviewed | accepted
    notes: str = ""

    def embed_text(self) -> str:
        """What we embed: the symbol + the code, so retrieval matches both."""
        return f"{self.symbol} ({self.layer})\n{self.output_code}"


class ArtifactStore:
    """
    Wraps a VectorStore collection for migrated artifacts.

    Reads/writes go through both:
      - the vector store (for semantic retrieval by later passes), and
      - an in-memory index by origin (for exact lookups + manifest rebuild).
    """

    def __init__(self):
        from app.rag.stores import Chunk
        self._Chunk = Chunk
        self._store = None              # lazily created on first vector op
        self._by_origin: dict[str, MigratedArtifact] = {}
        self._loaded = False

    def _vs(self):
        """Lazily create the underlying VectorStore (avoids hitting the DB at construction)."""
        if self._store is None:
            from app.rag.stores import VectorStore
            try:
                self._store = VectorStore(ARTIFACT_COLLECTION)
            except TypeError:
                self._store = VectorStore(ARTIFACT_COLLECTION, 768)
        return self._store

    # ── Write ─────────────────────────────────────────────────────────────────

    def put(self, artifact: MigratedArtifact):
        """Index one artifact (embeds output_code + symbol)."""
        from app.services import embed_code
        chunk = self._Chunk(
            text=artifact.embed_text(),
            source=artifact.origin,             # origin is the stable source/key
            type=f"artifact_{artifact.layer}",
            tags=[artifact.layer, artifact.symbol, artifact.status],
        )
        emb = embed_code([chunk.text]).embeddings[0]
        self._vs().upsert([chunk], [emb])

        # Persist the full record alongside (the vector store only holds the
        # embed text + metadata; we keep the structured record in-memory and
        # in the manifest layer).
        self._by_origin[artifact.origin] = artifact
        logger.info("Indexed artifact: %s → %s [%s]",
                    artifact.origin, artifact.output_path, artifact.layer)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, origin: str) -> MigratedArtifact | None:
        return self._by_origin.get(origin)

    def all(self) -> list[MigratedArtifact]:
        return list(self._by_origin.values())

    def by_layer(self, layer: str) -> list[MigratedArtifact]:
        return [a for a in self._by_origin.values() if a.layer == layer]

    def retrieve(self, query: str, top_k: int = 6, layer: str | None = None):
        """
        Semantic search for relevant prior artifacts (used by later passes).
        Optionally filter by layer (e.g. only models).
        Returns the vector store's search hits.
        """
        from app.services import embed_code
        qvec = embed_code([query]).embeddings[0]
        filter_type = f"artifact_{layer}" if layer else None
        return self._vs().search(qvec, top_k=top_k, filter_type=filter_type)

    # ── Persistence of the structured records (store = source of truth) ───────

    def dump_records(self, path: str):
        """Write all artifact records to JSON (the durable source of truth)."""
        data = {"artifacts": [asdict(a) for a in self._by_origin.values()]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Dumped %d artifact records → %s", len(self._by_origin), path)

    def load_records(self, path: str):
        """Rebuild the in-memory index from the JSON records."""
        import os
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for rec in data.get("artifacts", []):
            art = MigratedArtifact(**rec)
            self._by_origin[art.origin] = art
        self._loaded = True
        logger.info("Loaded %d artifact records from %s", len(self._by_origin), path)


# ── Module singleton ──────────────────────────────────────────────────────────

_artifact_store: ArtifactStore | None = None


def artifact_store() -> ArtifactStore:
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store
