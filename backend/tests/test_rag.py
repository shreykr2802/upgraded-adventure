"""
tests/test_rag.py
─────────────────
Unit tests for Phase 2 RAG components.
All vector store and embedding calls are mocked — no real services needed.

Run:
    cd backend
    pytest tests/ -v
"""

import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_embed_resp(vectors: list[list[float]]):
    from app.gateway import EmbedResponse
    raw = MagicMock()
    raw.data = [MagicMock() for _ in vectors]
    for i, v in enumerate(vectors):
        raw.data[i].embedding = v
    return EmbedResponse(raw)


def make_scored_point(text: str, source: str, chunk_type: str, score: float = 0.9):
    pt = MagicMock()
    pt.payload = {"text": text, "source": source, "type": chunk_type, "tags": []}
    pt.score = score
    return pt


# ── Chunk dataclass ───────────────────────────────────────────────────────────

def test_chunk_to_payload():
    from app.rag.stores import Chunk
    c = Chunk(text="hello", source="TextInput.tsx", type="component_doc", tags=["input"])
    p = c.to_payload()
    assert p["text"] == "hello"
    assert p["source"] == "TextInput.tsx"
    assert p["type"] == "component_doc"
    assert p["tags"] == ["input"]


def test_chunk_default_tags():
    from app.rag.stores import Chunk
    c = Chunk(text="x", source="y", type="atom_mapping")
    assert c.to_payload()["tags"] == []


# ── Indexer — component_to_chunk ──────────────────────────────────────────────

def test_component_to_chunk_format():
    from app.rag.indexer import ComponentDoc, _component_to_chunk
    doc = ComponentDoc(
        name="TextInput",
        import_path="@org/ui/atoms",
        props="label: string; value: string",
        usage="<TextInput label='Name' value={v} onChange={setV} />",
        description="Single-line text input.",
        tags=["input", "form"],
    )
    chunk = _component_to_chunk(doc)
    assert "TextInput" in chunk.text
    assert "@org/ui/atoms" in chunk.text
    assert "label: string" in chunk.text
    assert chunk.type == "component_doc"
    assert chunk.source == "TextInput.tsx"


def test_pattern_to_chunk_format():
    from app.rag.indexer import CodePattern, _pattern_to_chunk
    p = CodePattern(
        cshtml_pattern="@Html.TextBoxFor(m => m.X)",
        react_equivalent="<TextInput value={x} />",
        notes="Replace TextBoxFor with TextInput",
        tags=["textbox"],
    )
    chunk = _pattern_to_chunk(p)
    assert "@Html.TextBoxFor" in chunk.text
    assert "TextInput" in chunk.text
    assert chunk.type == "atom_mapping"


# ── Indexer — index_design_system ────────────────────────────────────────────

@patch("app.rag.indexer.design_store")
@patch("app.rag.indexer.embed_docs")
def test_index_design_system_calls_embed_and_upsert(mock_embed, mock_store_fn):
    from app.rag.indexer import index_design_system, ComponentDoc
    mock_embed.return_value = make_embed_resp([[0.1, 0.2]])
    mock_store = MagicMock()
    mock_store_fn.return_value = mock_store

    index_design_system([ComponentDoc(
        name="Button", import_path="@org/ui", props="label: string",
        usage="<Button label='ok' />", description="A button",
    )])

    mock_embed.assert_called_once()
    mock_store.upsert.assert_called_once()


@patch("app.rag.indexer.design_store")
@patch("app.rag.indexer.embed_docs")
def test_index_design_system_empty_list_is_no_op(mock_embed, mock_store_fn):
    from app.rag.indexer import index_design_system
    index_design_system([])
    mock_embed.assert_not_called()


# ── Indexer — index_code_patterns ─────────────────────────────────────────────

@patch("app.rag.indexer.code_store")
@patch("app.rag.indexer.embed_code")
def test_index_code_patterns_calls_embed_and_upsert(mock_embed, mock_store_fn):
    from app.rag.indexer import index_code_patterns, CodePattern
    mock_embed.return_value = make_embed_resp([[0.3, 0.4]])
    mock_store = MagicMock()
    mock_store_fn.return_value = mock_store

    index_code_patterns([CodePattern(
        cshtml_pattern="@Html.TextBoxFor(m => m.X)",
        react_equivalent="<TextInput />",
        notes="Replace TextBoxFor",
    )])

    mock_embed.assert_called_once()
    mock_store.upsert.assert_called_once()


@patch("app.rag.indexer.code_store")
@patch("app.rag.indexer.embed_code")
def test_index_migration_example(mock_embed, mock_store_fn):
    from app.rag.indexer import index_migration_example
    mock_embed.return_value = make_embed_resp([[0.5]])
    mock_store = MagicMock()
    mock_store_fn.return_value = mock_store

    index_migration_example(
        cshtml="<div>@Model.Name</div>",
        tsx="export default function Name() { return <div>{name}</div> }",
        source_file="HomeIndex.cshtml",
    )
    mock_store.upsert.assert_called_once()


# ── Retriever ─────────────────────────────────────────────────────────────────

@patch("app.rag.retriever.rerank")
@patch("app.rag.retriever.design_store")
@patch("app.rag.retriever.code_store")
@patch("app.rag.retriever.embed_docs")
@patch("app.rag.retriever.embed_code")
def test_retrieve_returns_reranked_chunks(
    mock_embed_code, mock_embed_docs,
    mock_code_store_fn, mock_design_store_fn,
    mock_rerank,
):
    from app.rag.retriever import retrieve
    from app.gateway import RerankResponse

    mock_embed_code.return_value = make_embed_resp([[0.1, 0.2]])
    mock_embed_docs.return_value = make_embed_resp([[0.3, 0.4]])

    mock_code_store = MagicMock()
    mock_design_store = MagicMock()
    mock_code_store_fn.return_value = mock_code_store
    mock_design_store_fn.return_value = mock_design_store

    mock_code_store.search.return_value = [
        make_scored_point("CSHTML pattern: @Html.TextBoxFor", "mapping_rules", "atom_mapping", 0.85)
    ]
    mock_design_store.search.return_value = [
        make_scored_point("Component: TextInput\nImport: ...", "TextInput.tsx", "component_doc", 0.92)
    ]

    rerank_raw = {"results": [{"index": 1, "relevance_score": 0.96}, {"index": 0, "relevance_score": 0.80}]}
    mock_rerank.return_value = RerankResponse(rerank_raw)

    result = retrieve("@Html.TextBoxFor(m => m.Name)")

    assert result.code_hits == 1
    assert result.design_hits == 1
    assert result.total_before_rerank == 2
    assert len(result.chunks) == 2
    # First chunk should be the design one (index 1 in combined list)
    assert "TextInput" in result.chunks[0]


@patch("app.rag.retriever.rerank")
@patch("app.rag.retriever.design_store")
@patch("app.rag.retriever.code_store")
@patch("app.rag.retriever.embed_docs")
@patch("app.rag.retriever.embed_code")
def test_retrieve_empty_stores_returns_no_chunks(
    mock_embed_code, mock_embed_docs,
    mock_code_store_fn, mock_design_store_fn,
    mock_rerank,
):
    from app.rag.retriever import retrieve
    mock_embed_code.return_value = make_embed_resp([[0.1]])
    mock_embed_docs.return_value = make_embed_resp([[0.2]])
    mock_code_store_fn.return_value.search.return_value = []
    mock_design_store_fn.return_value.search.return_value = []

    result = retrieve("some cshtml")

    assert result.chunks == []
    assert result.total_before_rerank == 0
    mock_rerank.assert_not_called()   # no point reranking empty results


# ── Retriever chunk formatters ────────────────────────────────────────────────

def test_format_code_chunk():
    from app.rag.retriever import _format_code_chunk
    payload = {"text": "CSHTML: @Html.TextBoxFor", "source": "mapping_rules", "type": "atom_mapping"}
    result = _format_code_chunk(payload)
    assert "[MIGRATION PATTERN]" in result
    assert "mapping_rules" in result
    assert "TextBoxFor" in result


def test_format_design_chunk():
    from app.rag.retriever import _format_design_chunk
    payload = {"text": "Component: TextInput", "source": "TextInput.tsx", "type": "component_doc"}
    result = _format_design_chunk(payload)
    assert "[DESIGN SYSTEM COMPONENT]" in result
    assert "TextInput.tsx" in result


