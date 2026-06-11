from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "processing"))

from shared.config import ProcessingConfig
from shared.models import ConnectionCandidate, ConnectionVerification, EmbeddedItem, EnrichmentResult, SourceItem
from shared.pipeline import ProcessingRunner


class FakeStore:
    def __init__(self) -> None:
        self.config = ProcessingConfig(enrichment_batch_size=10, embedding_batch_size=10)
        self.enriched: list[tuple[str, EnrichmentResult]] = []
        self.embedded: list[EmbeddedItem] = []
        self.connections: list[ConnectionVerification] = []
        self.summaries: list[dict[str, Any]] = []
        self.run_completed = False

    def ensure_schema(self) -> None: pass
    def start_run(self) -> str: return "run-1"
    def complete_run(self, run_id: str, metadata: dict[str, Any]) -> None: self.run_completed = True
    def fail_run(self, run_id: str, error: Exception) -> None: raise AssertionError(error)

    def fetch_unenriched_items(self, limit: int) -> list[SourceItem]:
        if self.enriched:
            return []
        return [
            SourceItem(
                source_item_id="reddit:item1",
                ticker="AMD",
                source="reddit",
                source_url=None,
                title="Support delays",
                body="Enterprise support got slower.",
                author="u/customer",
                published_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                metadata={},
            )
        ]

    def upsert_enrichment(self, item: SourceItem, result: EnrichmentResult, model: str) -> None:
        self.enriched.append((item.source_item_id, result))

    def fetch_unembedded_items(self, limit: int) -> list[EmbeddedItem]:
        if self.embedded or not self.enriched:
            return []
        return [EmbeddedItem("reddit:item1", "AMD", "reddit", None, self.enriched[0][1].summary)]

    def upsert_embeddings(self, items: list[EmbeddedItem], embeddings: list[list[float]], model: str) -> None:
        self.embedded.extend(items)

    def fetch_tickers_with_embeddings(self) -> list[str]: return ["AMD"] if self.embedded else []

    def prune_connections_outside_window(self) -> int: return 0

    def fetch_connection_candidates(self, ticker: str) -> list[ConnectionCandidate]:
        return [
            ConnectionCandidate(
                item_a_id="reddit:item1",
                item_b_id="github:item2",
                ticker="AMD",
                source_a="reddit",
                source_b="github",
                published_a=None,
                published_b=None,
                summary_a="Support worsened.",
                summary_b="Bug backlog grew.",
                similarity=0.81,
            )
        ] if not self.connections else []

    def upsert_connection(self, candidate: ConnectionCandidate, verification: ConnectionVerification, model: str, run_id: str) -> None:
        self.connections.append(verification)

    def fetch_initial_summary_context(self, ticker: str, per_source: int) -> tuple[dict[str, Any], set[str]]:
        return ({"items_by_source": {"reddit": [{"source_item_id": "reddit:item1"}]}, "connections": []}, {"reddit:item1"})

    def semantic_search(self, ticker: str, query_embedding: list[float], limit: int = 10) -> list[dict[str, Any]]:
        return [{"source_item_id": "github:item2", "summary": "Bug backlog grew."}]

    def insert_brain_summary(self, **kwargs: Any) -> None:
        self.summaries.append(kwargs)

    def refresh_sentiment_weekly(self) -> int: return 1


class FakeLLM:
    def __init__(self) -> None:
        self.searches = 0

    def enrich_item(self, item: SourceItem) -> EnrichmentResult:
        return EnrichmentResult(8, "bearish", "Support delays", ("support",), True, "customer", "Enterprise support got slower.")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    def verify_connection(self, candidate: ConnectionCandidate) -> ConnectionVerification:
        return ConnectionVerification(True, 0.8, "Support and bugs corroborate disruption.", "Retention risk.", "corroborating")

    def propose_search(self, ticker: str, context: dict[str, Any], searches_used: int) -> str | None:
        if self.searches == 0:
            self.searches += 1
            return "support backlog"
        return None

    def generate_summary(self, ticker: str, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "headline": "AMD support signals weakened.",
            "key_signals": ["reddit:item1 cites slower support."],
            "cross_source_connections": ["github:item2 corroborates backlog."],
            "bear_case": "Thin data.",
            "confidence": "medium",
            "cited_item_ids": ["reddit:item1", "github:item2"],
        }


def test_processing_runner_happy_path() -> None:
    store = FakeStore()
    runner = ProcessingRunner(store.config, store, FakeLLM())  # type: ignore[arg-type]

    counts = runner.run_all()

    assert counts == {
        "enriched": 1,
        "embedded": 1,
        "connections_pruned": 0,
        "connection_candidates": 1,
        "connections_valid": 1,
        "summaries": 1,
        "sentiment_rows": 1,
    }
    assert store.run_completed is True
    assert store.summaries[0]["invalid_citations"] == set()
