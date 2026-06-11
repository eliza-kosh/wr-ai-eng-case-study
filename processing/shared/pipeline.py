"""End-to-end scheduled processing orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.citations import find_invalid_citations
from shared.config import ProcessingConfig
from shared.models import ConnectionCandidate, ConnectionVerification
from shared.openai_client import OpenAIProcessorClient
from shared.storage import ProcessingStore


class ProcessingRunner:
    """Runs the full processing pipeline in idempotent stages."""

    def __init__(
        self,
        config: ProcessingConfig,
        store: ProcessingStore,
        llm: OpenAIProcessorClient,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm

    @classmethod
    def from_env(cls) -> "ProcessingRunner":
        config = ProcessingConfig.from_env()
        store = ProcessingStore.from_env(config)
        llm = OpenAIProcessorClient(config)
        return cls(config=config, store=store, llm=llm)

    def run_all(self) -> dict[str, int]:
        """Run prepare and synthesis stages in one process for local smoke tests."""
        counts = self.run_prepare()
        counts.update(self.run_synthesis())
        return counts

    def run_prepare(self) -> dict[str, int]:
        """Run the incremental enrichment and embedding preparation stage."""
        self.store.ensure_schema()
        run_id = self.store.start_run()
        counts = {"enriched": 0, "embedded": 0}
        try:
            counts["enriched"] = self.enrich_pending()
            counts["embedded"] = self.embed_pending()
            self.store.complete_run(run_id, {"job": "prepare_processing", "counts": counts})
            logging.info("Prepare processing complete run_id=%s counts=%s", run_id, counts)
            return counts
        except Exception as exc:
            logging.exception("Prepare processing failed run_id=%s", run_id)
            self.store.fail_run(run_id, exc)
            raise

    def run_synthesis(self) -> dict[str, int]:
        """Run connections, overview summaries, and sentiment aggregation."""
        self.store.ensure_schema()
        run_id = self.store.start_run()
        counts = {
            "connections_pruned": 0,
            "connection_candidates": 0,
            "connections_valid": 0,
            "summaries": 0,
            "sentiment_rows": 0,
        }
        try:
            counts["connections_pruned"] = self.store.prune_connections_outside_window()
            connection_counts = self.verify_connections(run_id)
            counts.update(connection_counts)
            counts["summaries"] = self.generate_brain_summaries(run_id)
            counts["sentiment_rows"] = self.store.refresh_sentiment_weekly()
            self.store.complete_run(run_id, {"job": "synthesis_processing", "counts": counts})
            logging.info("Synthesis processing complete run_id=%s counts=%s", run_id, counts)
            return counts
        except Exception as exc:
            logging.exception("Synthesis processing failed run_id=%s", run_id)
            self.store.fail_run(run_id, exc)
            raise

    def enrich_pending(self) -> int:
        """Enrich one batch of source items that have not been classified yet."""
        total = 0
        items = self.store.fetch_unenriched_items(self.config.enrichment_batch_size)
        for item in items:
            try:
                result = self.llm.enrich_item(item)
                self.store.upsert_enrichment(item, result, self.config.openai_enrichment_model)
                total += 1
            except Exception:
                logging.exception("Enrichment failed source_item_id=%s", item.source_item_id)
        return total

    def embed_pending(self) -> int:
        """Embed one batch of relevant enriched summaries that do not have vectors yet."""
        items = self.store.fetch_unembedded_items(self.config.embedding_batch_size)
        if not items:
            return 0
        embeddings = self.llm.embed_texts([item.summary for item in items])
        self.store.upsert_embeddings(items, embeddings, self.config.openai_embedding_model)
        return len(items)

    def verify_connections(self, run_id: str) -> dict[str, int]:
        """Generate cross-source candidates and persist model verification results."""
        candidate_count = 0
        valid_count = 0
        for ticker in self.store.fetch_tickers_with_embeddings():
            candidates = self.store.fetch_connection_candidates(ticker)
            candidate_count += len(candidates)
            for candidate in candidates:
                verification = self._heuristic_connection(candidate)
                self.store.upsert_connection(
                    candidate,
                    verification,
                    "heuristic-semantic-connection",
                    run_id,
                )
                if (
                    verification.valid
                    and verification.confidence >= self.config.connection_confidence_threshold
                ):
                    valid_count += 1
        return {"connection_candidates": candidate_count, "connections_valid": valid_count}

    def generate_brain_summaries(self, run_id: str) -> int:
        """Generate current ticker-level overview summaries."""
        count = 0
        for ticker in self.store.fetch_tickers_with_embeddings():
            try:
                context, allowed_ids = self.store.fetch_initial_summary_context(
                    ticker, self.config.initial_context_per_source
                )
                search_log: list[dict[str, Any]] = []
                payload = self._heuristic_summary(ticker, context)
                invalid = self._invalid_summary_citations(payload, allowed_ids)
                self.store.insert_brain_summary(
                    ticker=ticker,
                    payload=payload,
                    invalid_citations=invalid,
                    search_log=search_log,
                    run_id=run_id,
                    model="heuristic-business-overview",
                )
                count += 1
            except Exception:
                logging.exception("Summary generation failed ticker=%s", ticker)
        return count

    def _heuristic_connection(self, candidate: ConnectionCandidate) -> ConnectionVerification:
        """Persist broad semantic links without blocking on model verification."""
        confidence = max(self.config.connection_confidence_threshold, min(0.95, candidate.similarity))
        narrative = (
            f"{candidate.source_a} and {candidate.source_b} items discuss semantically similar "
            f"business context for {candidate.ticker}."
        )
        stock_relevance = (
            "Useful as a cross-source lead for analyst review; validate before treating it as "
            "a high-conviction signal."
        )
        return ConnectionVerification(
            valid=True,
            confidence=confidence,
            narrative=narrative,
            stock_relevance=stock_relevance,
            connection_type="corroborating",
        )

    def _heuristic_summary(self, ticker: str, context: dict[str, Any]) -> dict[str, Any]:
        """Build a deterministic ticker overview from enriched items and connections."""
        items: list[dict[str, Any]] = []
        for source_items in context.get("items_by_source", {}).values():
            items.extend(source_items)

        items.sort(key=lambda row: (row.get("relevance") or 0), reverse=True)
        top_items = items[:5]
        cited_ids = [str(row["source_item_id"]) for row in top_items if row.get("source_item_id")]
        key_signals = []
        for row in top_items[:3]:
            source_item_id = row.get("source_item_id")
            source = row.get("source", "source")
            sentiment = row.get("sentiment", "neutral")
            summary = str(row.get("summary") or "").strip()
            key_signals.append(f"{source_item_id} reports {source} sentiment is {sentiment}. {summary}")

        if not key_signals:
            key_signals = [f"No enriched business-signal items are available yet for {ticker}."]

        connections = context.get("connections", [])
        connection_lines = [
            f"{row.get('item_a_id')} + {row.get('item_b_id')}: {row.get('narrative')}"
            for row in connections[:3]
        ]
        if not connection_lines:
            connection_lines = ["No verified cross-source connections are available yet."]

        confidence = "medium" if len(top_items) >= 5 else "low"
        return {
            "headline": f"{ticker} business-signal overview from current alternative-data items",
            "key_signals": key_signals[:3],
            "cross_source_connections": connection_lines,
            "bear_case": "Coverage is still partial; treat low-relevance and single-source items as leads.",
            "confidence": confidence,
            "cited_item_ids": cited_ids,
        }

    def _invalid_summary_citations(self, payload: dict[str, Any], allowed_ids: set[str]) -> set[str]:
        cited_ids = set(str(item_id) for item_id in payload.get("cited_item_ids", []))
        body = json.dumps(payload)
        return (cited_ids | find_invalid_citations(body, allowed_ids)) - allowed_ids
