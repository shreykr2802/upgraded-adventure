"""
gateway.py
──────────
Base gateway client using the OpenAI Python SDK.

Since your org's gateway is OpenAI-compatible, we point the SDK at your
internal base_url and api_key — no custom HTTP code needed.

Three clients are created at module level:
  - `chat_client`   for /chat/completions  (Claude Sonnet + Haiku)
  - `embed_client`  for /embeddings        (Titan + BAAI llm-embedder)
  - `rerank_client` for /rerank            (bge-reranker-large)

The reranker uses a thin httpx call because /rerank is not part of the
OpenAI spec and the SDK doesn't have a typed method for it — but the
auth/base_url wiring is identical.

All three share the same gateway URL and API key from config.
"""

import logging
import httpx
from openai import OpenAI
from app.config import settings

logger = logging.getLogger(__name__)


# ── OpenAI SDK clients ────────────────────────────────────────────────────────

# One client is enough for both chat and embeddings —
# the SDK routes to the right endpoint based on the method called.
_client = OpenAI(
    api_key=settings.llm_gateway_key,
    base_url=settings.llm_gateway_url,
)

# ── Reranker — thin httpx wrapper ─────────────────────────────────────────────
# The OpenAI SDK has no typed rerank method. We use httpx directly here,
# matching the same Cohere-style /rerank shape most gateways expose.
# If your gateway uses a different shape, only this function needs updating.

_rerank_headers = {
    "Authorization": f"Bearer {settings.llm_gateway_key}",
    "Content-Type": "application/json",
}


# ── Public response types ────────────────────────────────────────────────────
# Lightweight wrappers so callers get a consistent interface regardless
# of what the SDK or raw HTTP returns underneath.

class ChatResponse:
    """Wraps openai.types.chat.ChatCompletion for convenient access."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def text(self) -> str:
        return self._raw.choices[0].message.content

    @property
    def usage(self) -> dict:
        u = self._raw.usage
        return {
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "total_tokens": u.total_tokens,
        }

    def __repr__(self):
        preview = self.text[:80].replace("\n", " ")
        return f"<ChatResponse text='{preview}...' usage={self.usage}>"


class EmbedResponse:
    """Wraps openai.types.CreateEmbeddingResponse."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def embeddings(self) -> list[list[float]]:
        return [item.embedding for item in self._raw.data]

    @property
    def first(self) -> list[float]:
        return self.embeddings[0]

    def __repr__(self):
        count = len(self.embeddings)
        dim = len(self.first) if count else 0
        return f"<EmbedResponse count={count} dim={dim}>"


class RerankResponse:
    """Wraps the raw /rerank JSON response."""

    def __init__(self, raw: dict):
        self._raw = raw

    @property
    def results(self) -> list[dict]:
        return self._raw.get("results", [])

    @property
    def top_indices(self) -> list[int]:
        return [r["index"] for r in self.results]

    def reranked_documents(self, documents: list[str]) -> list[str]:
        return [documents[r["index"]] for r in self.results]

    def __repr__(self):
        return f"<RerankResponse results={len(self.results)}>"


class GatewayError(Exception):
    def __init__(self, status: int, route: str, model: str, detail: str):
        self.status = status
        self.route = route
        self.model = model
        self.detail = detail
        super().__init__(f"[{status}] {route} model={model}: {detail[:200]}")


# ── Gateway functions ─────────────────────────────────────────────────────────

def chat(
    model: str,
    messages: list[dict],
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.1,
) -> ChatResponse:
    """
    Chat completion via the OpenAI SDK.

    Args:
        model:       Gateway model identifier.
        messages:    List of {"role": "user"|"assistant", "content": str}.
        system:      Optional system prompt — prepended as {"role": "system"}.
        max_tokens:  Defaults to settings.default_max_tokens.
        temperature: Low default for deterministic code output.
    """
    all_messages = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    logger.debug("chat model=%s messages=%d", model, len(all_messages))

    raw = _client.chat.completions.create(
        model=model,
        messages=all_messages,
        max_tokens=max_tokens or settings.default_max_tokens,
        temperature=temperature,
    )
    return ChatResponse(raw)


def embed(
    model: str,
    texts: list[str] | str,
) -> EmbedResponse:
    """
    Text embedding via the OpenAI SDK.

    Args:
        model: Gateway model identifier for the embedding model.
        texts: A single string or list of strings.
    """
    if isinstance(texts, str):
        texts = [texts]

    logger.debug("embed model=%s texts=%d", model, len(texts))

    raw = _client.embeddings.create(
        model=model,
        input=texts,
    )
    return EmbedResponse(raw)


def rerank(
    model: str,
    query: str,
    documents: list[str],
    top_n: int | None = None,
) -> RerankResponse:
    """
    Rerank documents via /rerank (not in OpenAI spec — direct httpx call).

    Args:
        model:     Gateway reranker model identifier.
        query:     Query string to score documents against.
        documents: List of retrieved document strings.
        top_n:     Number of top results to return. None = all.
    """
    url = f"{settings.llm_gateway_url.rstrip('/')}/rerank"
    payload: dict = {"model": model, "query": query, "documents": documents}
    if top_n is not None:
        payload["top_n"] = top_n

    logger.debug("rerank model=%s docs=%d", model, len(documents))

    try:
        resp = httpx.post(url, json=payload, headers=_rerank_headers, timeout=30)
        resp.raise_for_status()
        return RerankResponse(resp.json())
    except httpx.HTTPStatusError as e:
        raise GatewayError(
            status=e.response.status_code,
            route="/rerank",
            model=model,
            detail=e.response.text,
        ) from e
    except httpx.RequestError as e:
        raise GatewayError(status=0, route="/rerank", model=model, detail=str(e)) from e


def health() -> bool:
    """Quick connectivity check — returns True if the gateway responds."""
    try:
        _client.models.list()
        return True
    except Exception:
        return False
