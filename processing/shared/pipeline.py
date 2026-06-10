"""End-to-end scheduled processing orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.citations import find_invalid_citations
from shared.config import ProcessingConfig
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
            "connection_candidates": 0,
            "connections_valid": 0,
            "summaries": 0,
            "sentiment_rows": 0,
        }
        try:
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
        """Enrich source items that have not been classified yet."""
        total = 0
        while True:
            items = self.store.fetch_unenriched_items(self.config.enrichment_batch_size)
            if not items:
                return total
            for item in items:
                try:
                    result = self.llm.enrich_item(item)
                    self.store.upsert_enrichment(item, result, self.config.openai_enrichment_model)
                    total += 1
                except Exception:
                    logging.exception("Enrichment failed source_item_id=%s", item.source_item_id)

    def embed_pending(self) -> int:
        """Embed relevant enriched summaries that do not have vectors yet."""
        total = 0
        while True:
            items = self.store.fetch_unembedded_items(self.config.embedding_batch_size)
            if not items:
                return total
            embeddings = self.llm.embed_texts([item.summary for item in items])
            self.store.upsert_embeddings(items, embeddings, self.config.openai_embedding_model)
            total += len(items)

    def verify_connections(self, run_id: str) -> dict[str, int]:
        """Generate cross-source candidates and persist model verification results."""
        candidate_count = 0
        valid_count = 0
        for ticker in self.store.fetch_tickers_with_embeddings():
            candidates = self.store.fetch_connection_candidates(ticker)
            candidate_count += len(candidates)
            for candidate in candidates:
                verification = self.llm.verify_connection(candidate)
                self.store.upsert_connection(
                    candidate,
                    verification,
                    self.config.anthropic_connection_model,
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
                for search_index in range(self.config.max_agent_searches):
                    query = self.llm.propose_search(ticker, context, search_index)
                    if not query:
                        break
                    query_embedding = self.llm.embed_texts([query])[0]
                    results = self.store.semantic_search(ticker, query_embedding)
                    new_results = [row for row in results if row["source_item_id"] not in allowed_ids]
                    search_log.append({"query": query, "results": results})
                    allowed_ids.update(row["source_item_id"] for row in results)
                    if new_results:
                        context.setdefault("semantic_searches", []).append(
                            {"query": query, "results": new_results}
                        )
                payload = self.llm.generate_summary(ticker, context)
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

    def _invalid_summary_citations(self, payload: dict[str, Any], allowed_ids: set[str]) -> set[str]:
        cited_ids = set(str(item_id) for item_id in payload.get("cited_item_ids", []))
        body = json.dumps(payload)
        return (cited_ids | find_invalid_citations(body, allowed_ids)) - allowed_ids
