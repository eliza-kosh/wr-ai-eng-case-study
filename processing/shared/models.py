"""Typed contracts used by the processing pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

Sentiment = Literal["bullish", "bearish", "neutral"]
ConnectionType = Literal["causal", "corroborating", "contradicting", "leading_indicator"]


@dataclass(frozen=True)
class SourceItem:
    source_item_id: str
    ticker: str
    source: str
    source_url: str | None
    title: str | None
    body: str | None
    author: str | None
    published_at: dt.datetime | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EnrichmentResult:
    relevance: int
    sentiment: Sentiment
    sentiment_rationale: str
    themes: tuple[str, ...]
    firsthand: bool
    firsthand_type: str | None
    summary: str


@dataclass(frozen=True)
class EmbeddedItem:
    source_item_id: str
    ticker: str
    source: str
    published_at: dt.datetime | None
    summary: str


@dataclass(frozen=True)
class ConnectionCandidate:
    item_a_id: str
    item_b_id: str
    ticker: str
    source_a: str
    source_b: str
    published_a: dt.datetime | None
    published_b: dt.datetime | None
    summary_a: str
    summary_b: str
    similarity: float


@dataclass(frozen=True)
class ConnectionVerification:
    valid: bool
    confidence: float
    narrative: str
    stock_relevance: str
    connection_type: ConnectionType
    supporting_item_ids: tuple[str, ...] = ()
    rejected_item_ids: tuple[str, ...] = ()
    connection_title: str = ""


@dataclass(frozen=True)
class ConnectionClusterItem:
    source_item_id: str
    source: str
    published_at: dt.datetime | None
    summary: str
    relevance: int
    sentiment: Sentiment
    firsthand: bool
    similarity: float


@dataclass(frozen=True)
class ConnectionClusterCandidate:
    cluster_key: str
    ticker: str
    anchor_item_id: str
    average_similarity: float
    sources: tuple[str, ...]
    items: tuple[ConnectionClusterItem, ...]


def sentiment_to_score(sentiment: str) -> int:
    """Convert sentiment labels to numeric chart values."""
    normalized = sentiment.lower().strip()
    if normalized == "bullish":
        return 1
    if normalized == "bearish":
        return -1
    return 0
