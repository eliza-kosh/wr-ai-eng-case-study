"""End-to-end scheduled processing orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.citations import extract_cited_item_ids, find_invalid_citations
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
        """Generate semantic cluster candidates and persist model verification results."""
        candidate_count = 0
        valid_count = 0
        for ticker in self.store.fetch_tickers_with_embeddings():
            existing_valid = self.store.count_valid_connection_clusters(ticker)
            remaining_valid_slots = max(0, self.config.max_valid_connections_per_ticker - existing_valid)
            if remaining_valid_slots <= 0:
                continue
            candidates = self.store.fetch_connection_cluster_candidates(ticker)
            candidate_count += len(candidates)
            accepted_for_ticker = 0
            for candidate in candidates:
                try:
                    verification = self.llm.verify_connection_cluster(candidate)
                    verification = self._guardrail_cluster_verification(candidate, verification)
                except Exception:
                    logging.exception(
                        "Connection cluster verification failed ticker=%s anchor=%s",
                        candidate.ticker,
                        candidate.anchor_item_id,
                    )
                    continue
                self.store.upsert_connection_cluster(
                    candidate,
                    verification,
                    self.config.anthropic_summary_model,
                    run_id,
                )
                if (
                    verification.valid
                    and verification.confidence >= self.config.connection_confidence_threshold
                ):
                    valid_count += 1
                    accepted_for_ticker += 1
                    if accepted_for_ticker >= remaining_valid_slots:
                        break
        return {"connection_candidates": candidate_count, "connections_valid": valid_count}

    def _guardrail_cluster_verification(
        self,
        candidate: Any,
        verification: ConnectionVerification,
    ) -> ConnectionVerification:
        """Reject cluster outputs that pass the model but fail basic analyst-quality gates."""
        if not verification.valid:
            return verification

        support_ids = set(verification.supporting_item_ids) or {
            item.source_item_id for item in candidate.items
        }
        support_items = [item for item in candidate.items if item.source_item_id in support_ids]
        sources = {item.source for item in support_items}
        firsthand_count = sum(1 for item in support_items if item.firsthand)
        forbidden = ("semantic similarity", "embedding", "retrieved chunk", "alternative-data")

        if (
            len(support_items) < 3
            or (len(sources) < 2 and firsthand_count < 5)
            or any(term in verification.narrative.lower() for term in forbidden)
            or any(term in verification.connection_title.lower() for term in forbidden)
        ):
            return ConnectionVerification(
                valid=False,
                confidence=0.0,
                connection_title="",
                narrative="",
                stock_relevance="",
                connection_type=verification.connection_type,
                supporting_item_ids=verification.supporting_item_ids,
                rejected_item_ids=verification.rejected_item_ids,
            )

        return verification

    def generate_brain_summaries(self, run_id: str) -> int:
        """Generate current ticker-level overview summaries."""
        count = 0
        for ticker in self.store.fetch_tickers_with_embeddings():
            try:
                context, allowed_ids = self.store.fetch_initial_summary_context(
                    ticker, self.config.initial_context_per_source
                )
                search_log: list[dict[str, Any]] = []
                payload = self.llm.generate_summary(ticker, context)
                cited_ids = self._summary_cited_ids(payload, allowed_ids)
                if not cited_ids:
                    cited_ids = self._fallback_cited_ids(context, allowed_ids)
                payload["cited_item_ids"] = cited_ids
                invalid = self._invalid_summary_citations(payload, allowed_ids)
                self.store.insert_brain_summary(
                    ticker=ticker,
                    payload=payload,
                    invalid_citations=invalid,
                    search_log=search_log,
                    run_id=run_id,
                    model=self.config.anthropic_summary_model,
                )
                count += 1
            except Exception:
                logging.exception("Summary generation failed ticker=%s", ticker)
        return count

    def _heuristic_connection(self, candidate: ConnectionCandidate) -> ConnectionVerification:
        """Reject broad semantic links when model verification is unavailable."""
        return ConnectionVerification(
            valid=False,
            confidence=0.0,
            narrative="",
            stock_relevance="",
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

        _sentiment_label = {"bullish": "Bullish", "bearish": "Bearish", "neutral": "Neutral"}
        key_signals = []
        for row in top_items[:3]:
            source = row.get("source", "source").replace("_", " ").title()
            sentiment = str(row.get("sentiment", "neutral")).lower()
            label = _sentiment_label.get(sentiment, "Neutral")
            firsthand = " (firsthand)" if row.get("firsthand") else ""
            summary = str(row.get("summary") or "").strip()
            key_signals.append(f"{label} — {source}{firsthand}: {summary}")

        if not key_signals:
            key_signals = [f"No enriched business-signal items are available yet for {ticker}."]

        connections = context.get("connections", [])
        connection_lines = [
            (
                f"[{row.get('source_a', '').replace('_', ' ').upper()} × "
                f"{row.get('source_b', '').replace('_', ' ').upper()}] "
                f"{row.get('narrative', '')}"
            )
            for row in connections[:3]
        ]
        if not connection_lines:
            connection_lines = ["No verified cross-source connections are available yet."]

        confidence = "medium" if len(top_items) >= 5 else "low"
        overview_lines = [f"- {s}" for s in key_signals] if key_signals else ["No data available."]
        return {
            "headline": f"{ticker}: insufficient data for a synthesized brief.",
            "overview": "\n".join(overview_lines),
            "cross_source_connections": connection_lines,
            "bear_case": "Coverage is still partial; treat low-relevance and single-source items as leads.",
            "confidence": confidence,
            "key_signals": [f"{sid}: (heuristic fallback)" for sid in cited_ids[:10]],
            "cited_item_ids": cited_ids,
        }

    def _invalid_summary_citations(self, payload: dict[str, Any], allowed_ids: set[str]) -> set[str]:
        cited_ids = set(str(item_id) for item_id in payload.get("cited_item_ids", []))
        body = json.dumps(payload)
        return (cited_ids | find_invalid_citations(body, allowed_ids)) - allowed_ids

    def _summary_cited_ids(self, payload: dict[str, Any], allowed_ids: set[str]) -> list[str]:
        supplied = {str(item_id) for item_id in payload.get("cited_item_ids", [])}
        extracted = extract_cited_item_ids(json.dumps(payload))
        return sorted((supplied | extracted) & allowed_ids)

    def _fallback_cited_ids(self, context: dict[str, Any], allowed_ids: set[str]) -> list[str]:
        fallback: list[str] = []
        for connection in context.get("connections", [])[:2]:
            fallback.extend(str(item_id) for item_id in connection.get("item_ids", []) if item_id)
        if not fallback:
            for items in context.get("items_by_source", {}).values():
                fallback.extend(str(item.get("source_item_id")) for item in items[:3] if item.get("source_item_id"))
        return [item_id for item_id in dict.fromkeys(fallback) if item_id in allowed_ids][:15]
