"""
tests/test_gateway.py
─────────────────────
Unit tests for gateway functions and response wrappers.
Mocks the OpenAI SDK and httpx — no real gateway needed.

Run:
    cd backend
    pytest tests/test_gateway.py -v
"""

import pytest
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_sdk_chat_response(content: str, prompt=10, completion=20):
    """Build a fake openai ChatCompletion object."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage.prompt_tokens = prompt
    resp.usage.completion_tokens = completion
    resp.usage.total_tokens = prompt + completion
    return resp


def make_sdk_embed_response(vectors: list[list[float]]):
    """Build a fake openai CreateEmbeddingResponse object."""
    resp = MagicMock()
    resp.data = [MagicMock() for _ in vectors]
    for i, vec in enumerate(vectors):
        resp.data[i].embedding = vec
    return resp


RERANK_RAW = {
    "results": [
        {"index": 2, "relevance_score": 0.97, "document": "TextInput"},
        {"index": 0, "relevance_score": 0.74, "document": "Button"},
    ]
}


# ── ChatResponse ──────────────────────────────────────────────────────────────

def test_chat_response_text():
    from app.gateway import ChatResponse
    r = ChatResponse(make_sdk_chat_response("const X = () => <div />;"))
    assert r.text == "const X = () => <div />;"


def test_chat_response_usage():
    from app.gateway import ChatResponse
    r = ChatResponse(make_sdk_chat_response("hi", prompt=5, completion=15))
    assert r.usage == {"prompt_tokens": 5, "completion_tokens": 15, "total_tokens": 20}


# ── EmbedResponse ─────────────────────────────────────────────────────────────

def test_embed_response_embeddings():
    from app.gateway import EmbedResponse
    r = EmbedResponse(make_sdk_embed_response([[0.1, 0.2], [0.3, 0.4]]))
    assert r.embeddings == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_response_first():
    from app.gateway import EmbedResponse
    r = EmbedResponse(make_sdk_embed_response([[0.9, 0.8]]))
    assert r.first == [0.9, 0.8]


# ── RerankResponse ────────────────────────────────────────────────────────────

def test_rerank_top_indices():
    from app.gateway import RerankResponse
    r = RerankResponse(RERANK_RAW)
    assert r.top_indices == [2, 0]


def test_rerank_reranked_documents():
    from app.gateway import RerankResponse
    docs = ["Button", "DataTable", "TextInput"]
    r = RerankResponse(RERANK_RAW)
    assert r.reranked_documents(docs) == ["TextInput", "Button"]


# ── chat() ────────────────────────────────────────────────────────────────────

@patch("app.gateway._client")
def test_chat_sends_system_as_first_message(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response("ok")
    from app.gateway import chat
    chat(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}], system="You are X.")
    call = mock_client.chat.completions.create.call_args.kwargs
    assert call["messages"][0] == {"role": "system", "content": "You are X."}
    assert call["messages"][1] == {"role": "user", "content": "hi"}


@patch("app.gateway._client")
def test_chat_no_system_omits_system_message(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response("ok")
    from app.gateway import chat
    chat(model="claude-haiku-3-5", messages=[{"role": "user", "content": "hi"}])
    messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert all(m["role"] != "system" for m in messages)


@patch("app.gateway._client")
def test_chat_uses_correct_model(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response("ok")
    from app.gateway import chat
    chat(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "x"}])
    assert mock_client.chat.completions.create.call_args.kwargs["model"] == "claude-sonnet-4-6"


@patch("app.gateway._client")
def test_chat_returns_chat_response(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response("result")
    from app.gateway import chat, ChatResponse
    result = chat(model="claude-haiku-3-5", messages=[{"role": "user", "content": "x"}])
    assert isinstance(result, ChatResponse)
    assert result.text == "result"


# ── embed() ───────────────────────────────────────────────────────────────────

@patch("app.gateway._client")
def test_embed_wraps_string_in_list(mock_client):
    mock_client.embeddings.create.return_value = make_sdk_embed_response([[0.1]])
    from app.gateway import embed
    embed(model="baai/llm-embedder", texts="single string")
    call = mock_client.embeddings.create.call_args.kwargs
    assert call["input"] == ["single string"]


@patch("app.gateway._client")
def test_embed_passes_list_unchanged(mock_client):
    mock_client.embeddings.create.return_value = make_sdk_embed_response([[0.1], [0.2]])
    from app.gateway import embed
    embed(model="baai/llm-embedder", texts=["a", "b"])
    call = mock_client.embeddings.create.call_args.kwargs
    assert call["input"] == ["a", "b"]


@patch("app.gateway._client")
def test_embed_returns_embed_response(mock_client):
    mock_client.embeddings.create.return_value = make_sdk_embed_response([[0.5, 0.6]])
    from app.gateway import embed, EmbedResponse
    result = embed(model="amazon.titan-embed-text-v2", texts=["doc"])
    assert isinstance(result, EmbedResponse)
    assert result.first == [0.5, 0.6]


# ── rerank() ──────────────────────────────────────────────────────────────────

@patch("app.gateway.httpx.post")
def test_rerank_payload_shape(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = RERANK_RAW
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    from app.gateway import rerank
    rerank(model="baai/bge-reranker-large", query="convert input", documents=["d1", "d2"], top_n=1)

    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "baai/bge-reranker-large"
    assert payload["query"] == "convert input"
    assert payload["documents"] == ["d1", "d2"]
    assert payload["top_n"] == 1


@patch("app.gateway.httpx.post")
def test_rerank_omits_top_n_when_none(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = RERANK_RAW
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    from app.gateway import rerank
    rerank(model="baai/bge-reranker-large", query="q", documents=["d"], top_n=None)
    assert "top_n" not in mock_post.call_args.kwargs["json"]


@patch("app.gateway.httpx.post")
def test_rerank_returns_rerank_response(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = RERANK_RAW
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    from app.gateway import rerank, RerankResponse
    result = rerank(model="baai/bge-reranker-large", query="q", documents=["a", "b", "c"])
    assert isinstance(result, RerankResponse)
    assert result.top_indices == [2, 0]


@patch("app.gateway.httpx.post")
def test_rerank_raises_gateway_error_on_4xx(mock_post):
    import httpx
    from app.gateway import GatewayError

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "route not found"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=mock_resp
    )
    mock_post.return_value = mock_resp

    from app.gateway import rerank
    with pytest.raises(GatewayError) as exc:
        rerank(model="baai/bge-reranker-large", query="q", documents=["d"])
    assert exc.value.status == 404
    assert exc.value.route == "/rerank"


# ── services layer ────────────────────────────────────────────────────────────

@patch("app.gateway._client")
def test_services_generate_uses_sonnet(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response("tsx code")
    from app.services import generate_component
    from app.config import settings
    generate_component(messages=[{"role": "user", "content": "x"}], system="sys")
    model = mock_client.chat.completions.create.call_args.kwargs["model"]
    assert model == settings.model_sonnet


@patch("app.gateway._client")
def test_services_classify_uses_haiku(mock_client):
    mock_client.chat.completions.create.return_value = make_sdk_chat_response('{"complexity":"simple","reason":"static"}')
    from app.services import classify_file
    from app.config import settings
    classify_file(cshtml="<div>Hello</div>")
    model = mock_client.chat.completions.create.call_args.kwargs["model"]
    assert model == settings.model_haiku


@patch("app.gateway._client")
def test_services_embed_code_uses_llm_embedder(mock_client):
    mock_client.embeddings.create.return_value = make_sdk_embed_response([[0.1]])
    from app.services import embed_code
    from app.config import settings
    embed_code("@Html.TextBoxFor(m => m.Name)")
    model = mock_client.embeddings.create.call_args.kwargs["model"]
    assert model == settings.model_code_embed


@patch("app.gateway._client")
def test_services_embed_docs_uses_titan(mock_client):
    mock_client.embeddings.create.return_value = make_sdk_embed_response([[0.2]])
    from app.services import embed_docs
    from app.config import settings
    embed_docs("Use TextInput for single-line entry.")
    model = mock_client.embeddings.create.call_args.kwargs["model"]
    assert model == settings.model_doc_embed
