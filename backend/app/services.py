"""
services.py
───────────
Role-specific service functions — the layer above the gateway.

Each function maps to one model role in the pipeline:

  generate_component → Claude Sonnet 4.6   complex React generation
  simple_generate    → Claude Haiku 3.5    simple/static screens (cost saving)
  classify_file      → Claude Haiku 3.5    pre-filter & complexity scoring
  review_component   → Claude Haiku 3.5    post-generation validation
  embed_code         → BAAI/llm-embedder   code pattern store indexing
  embed_docs         → Titan embed-text-v2 design system store indexing
  rerank             → bge-reranker-large  cross-store result reranking

Pipeline stages and FastAPI endpoints import from here only —
they never call gateway functions directly. If a model string changes,
update config.py; nothing else needs to change.
"""

from app.config import settings
from app.gateway import (
    chat, embed, rerank as _rerank,
    ChatResponse, EmbedResponse, RerankResponse,
)


# ── LLM — generation ──────────────────────────────────────────────────────────

def generate_component(
    messages: list[dict],
    system: str,
    max_tokens: int = 4000,
    temperature: float = 0.1,
) -> ChatResponse:
    """
    Generate a React component from CSHTML + C# context.
    Uses Claude Sonnet 4.6 — reserved for complex screens.
    Called only when Haiku's complexity router escalates.
    """
    return chat(
        model=settings.model_sonnet,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def simple_generate(
    messages: list[dict],
    system: str,
    max_tokens: int = 4000,
) -> ChatResponse:
    """
    Full generation on Haiku for simple/static screens.
    Same output contract as generate_component, lower cost.
    """
    return chat(
        model=settings.model_haiku,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=0.1,
    )


# ── LLM — pre-filter & post-review ───────────────────────────────────────────

def classify_file(
    cshtml: str,
    controller: str | None = None,
) -> ChatResponse:
    """
    Classify a CSHTML file and return a complexity score.
    Uses Claude Haiku 3.5 — fast, cheap, no RAG needed.

    Returns a ChatResponse whose .text is a JSON string:
      {"complexity": "simple"|"medium"|"complex", "reason": "..."}
    """
    context = f"CONTROLLER:\n{controller}\n\n" if controller else ""
    return chat(
        model=settings.model_haiku,
        messages=[{"role": "user", "content": f"{context}CSHTML:\n{cshtml}"}],
        system=(
            "You are a migration complexity classifier. "
            "Given a CSHTML view and optional controller, "
            "classify migration complexity as simple, medium, or complex. "
            'Reply ONLY with valid JSON: {"complexity": "simple"|"medium"|"complex", "reason": "<one sentence>"}'
        ),
        max_tokens=128,
        temperature=0.0,  # fully deterministic for classification
    )


def review_component(
    generated_tsx: str,
) -> ChatResponse:
    """
    Post-generation validation pass on the generated .tsx.
    Checks design-rule compliance and ensures TODOs are flagged.
    Uses Claude Haiku 3.5.

    Returns a ChatResponse whose .text is a JSON string:
      {"valid": true|false, "issues": ["..."]}
    """
    return chat(
        model=settings.model_haiku,
        messages=[{"role": "user", "content": f"COMPONENT:\n{generated_tsx}"}],
        system=(
            "You are a React migration reviewer. "
            "Check that the component: "
            "(1) uses only named imports from the design library, not raw HTML elements where a component exists; "
            "(2) has no unresolved Razor syntax (@, Html.); "
            "(3) flags any server-side logic with // TODO comments. "
            'Reply ONLY with valid JSON: {"valid": true|false, "issues": ["..."]}'
        ),
        max_tokens=512,
        temperature=0.0,
    )


# ── Embeddings ────────────────────────────────────────────────────────────────

def embed_code(texts: list[str] | str) -> EmbedResponse:
    """
    Embed code chunks for the Code Pattern RAG store.
    Uses BAAI/llm-embedder — specialised for code retrieval.
    """
    return embed(model=settings.model_code_embed, texts=texts)


def embed_docs(texts: list[str] | str) -> EmbedResponse:
    """
    Embed documentation chunks for the Design System RAG store.
    Uses Amazon Titan embed-text-v2 — tuned for prose/docs.
    """
    return embed(model=settings.model_doc_embed, texts=texts)


# ── Reranking ─────────────────────────────────────────────────────────────────

def rerank(
    query: str,
    documents: list[str],
    top_n: int = 5,
) -> RerankResponse:
    """
    Rerank documents retrieved from BOTH vector stores into one ranked list.

    As a cross-encoder, bge-reranker-large scores raw text pairs directly —
    no embedding dimension compatibility needed between the two stores.

    Args:
        query:     The CSHTML content being migrated (used as the query).
        documents: Combined chunks from Code Pattern + Design System stores.
        top_n:     How many top results to pass to the generation LLM.
                   Default 5 — tune this based on context window budget.
    """
    return _rerank(
        model=settings.model_reranker,
        query=query,
        documents=documents,
        top_n=top_n,
    )
