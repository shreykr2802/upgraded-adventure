"""
config.py
─────────
All environment variables and model constants in one place.
Import `settings` anywhere — never read os.environ directly.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Gateway ───────────────────────────────────────────────────────────
    llm_gateway_url: str
    llm_gateway_key: str

    # ── Model identifiers ─────────────────────────────────────────────────
    # If a call returns "model not found", the string below is wrong —
    # confirm the exact identifier with your gateway admin.
    model_sonnet: str = "claude-sonnet-4-6"
    model_haiku: str = "claude-haiku-3-5"
    model_code_embed: str = "baai/llm-embedder"
    model_doc_embed: str = "amazon.titan-embed-text-v2"
    model_reranker: str = "baai/bge-reranker-large"

    # ── Vector store (ChromaDB, local file storage) ──────────────────────
    chroma_path: str = "./chroma_storage"

    # ── RAG ───────────────────────────────────────────────────────────────
    rag_top_k_per_store: int = 10     # chunks retrieved per store pre-rerank
    rag_top_n_after_rerank: int = 5   # chunks passed to LLM after reranking

    # ── Defaults ──────────────────────────────────────────────────────────
    default_max_tokens: int = 4000
    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
