"""Environment-driven processing configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessingConfig:
    """Config values for one processing run."""

    tickers: tuple[str, ...] = ("AMD", "SNDK", "FROG", "APP", "KVYO")
    sources: tuple[str, ...] = ("reddit", "hacker_news", "github")
    relevance_threshold: int = 0
    similarity_threshold: float = 0.0
    connection_confidence_threshold: float = 0.10
    temporal_window_days: int = 90
    max_connection_candidates_per_ticker: int = 12
    connection_cluster_size: int = 30
    connection_cluster_max_overlap: float = 0.65
    max_agent_searches: int = 5
    enrichment_batch_size: int = 25
    embedding_batch_size: int = 100
    initial_context_per_source: int = 10
    openai_enrichment_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    anthropic_summary_model: str = "claude-opus-4-8"

    @classmethod
    def from_env(cls) -> "ProcessingConfig":
        """Load config from environment variables."""
        tickers = _csv_env("PROCESSING_TICKERS", cls.tickers)
        sources = _csv_env("PROCESSING_SOURCES", cls.sources)
        return cls(
            tickers=tickers,
            sources=sources,
            relevance_threshold=int(os.getenv("PROCESSING_RELEVANCE_THRESHOLD", "0")),
            similarity_threshold=float(os.getenv("PROCESSING_SIMILARITY_THRESHOLD", "0.0")),
            connection_confidence_threshold=float(
                os.getenv("PROCESSING_CONNECTION_CONFIDENCE_THRESHOLD", "0.10")
            ),
            temporal_window_days=int(os.getenv("PROCESSING_TEMPORAL_WINDOW_DAYS", "90")),
            max_connection_candidates_per_ticker=int(
                os.getenv("PROCESSING_MAX_CONNECTION_CANDIDATES_PER_TICKER", "12")
            ),
            connection_cluster_size=int(os.getenv("PROCESSING_CONNECTION_CLUSTER_SIZE", "30")),
            connection_cluster_max_overlap=float(os.getenv("PROCESSING_CONNECTION_CLUSTER_MAX_OVERLAP", "0.65")),
            max_agent_searches=int(os.getenv("PROCESSING_MAX_AGENT_SEARCHES", "5")),
            enrichment_batch_size=int(os.getenv("PROCESSING_ENRICHMENT_BATCH_SIZE", "25")),
            embedding_batch_size=int(os.getenv("PROCESSING_EMBEDDING_BATCH_SIZE", "100")),
            initial_context_per_source=int(os.getenv("PROCESSING_INITIAL_CONTEXT_PER_SOURCE", "10")),
            openai_enrichment_model=os.getenv("OPENAI_ENRICHMENT_MODEL", "gpt-4.1-mini"),
            openai_embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            embedding_dimensions=int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536")),
            anthropic_summary_model=os.getenv("ANTHROPIC_SUMMARY_MODEL", "claude-opus-4-8"),
        )


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default
