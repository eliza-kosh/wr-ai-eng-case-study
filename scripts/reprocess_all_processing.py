"""Local driver to rebuild processing outputs from existing source_items."""

from __future__ import annotations

import argparse
import json
import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "processing"))

from shared.pipeline import ProcessingRunner  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Delete derived processing outputs first.")
    parser.add_argument("--max-prepare-runs", type=int, default=200)
    parser.add_argument("--enrichment-workers", type=int, default=1)
    parser.add_argument("--skip-synthesis", action="store_true")
    parser.add_argument("--reset-synthesis", action="store_true")
    args = parser.parse_args()

    runner = ProcessingRunner.from_env()
    runner.store.ensure_schema()

    print(
        json.dumps(
            {
                "event": "config",
                "openai_enrichment_model": runner.config.openai_enrichment_model,
                "openai_embedding_model": runner.config.openai_embedding_model,
                "anthropic_summary_model": runner.config.anthropic_summary_model,
                "enrichment_batch_size": runner.config.enrichment_batch_size,
                "embedding_batch_size": runner.config.embedding_batch_size,
                "temporal_window_days": runner.config.temporal_window_days,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    if args.reset:
        reset_counts = reset_processing_outputs(runner)
        print(json.dumps({"event": "reset", "counts": reset_counts}, sort_keys=True), flush=True)

    for index in range(1, args.max_prepare_runs + 1):
        counts = (
            run_prepare_parallel(runner, args.enrichment_workers)
            if args.enrichment_workers > 1
            else runner.run_prepare()
        )
        print(
            json.dumps({"event": "prepare", "iteration": index, "counts": counts}, sort_keys=True),
            flush=True,
        )
        if counts.get("enriched", 0) == 0 and counts.get("embedded", 0) == 0:
            break
    else:
        raise RuntimeError(f"Prepare loop did not drain after {args.max_prepare_runs} iterations")

    if args.skip_synthesis:
        print(json.dumps({"event": "synthesis_skipped"}, sort_keys=True), flush=True)
    else:
        if args.reset_synthesis:
            reset_counts = reset_synthesis_outputs(runner)
            print(
                json.dumps({"event": "reset_synthesis", "counts": reset_counts}, sort_keys=True),
                flush=True,
            )
        counts = runner.run_synthesis()
        print(json.dumps({"event": "synthesis", "counts": counts}, sort_keys=True), flush=True)
    print(json.dumps({"event": "final_counts", "counts": table_counts(runner)}, sort_keys=True), flush=True)


def reset_processing_outputs(runner: ProcessingRunner) -> dict[str, int]:
    tables = [
        "brain_summaries",
        "connection_clusters",
        "item_embeddings",
        "item_enrichments",
        "sentiment_weekly",
    ]
    counts: dict[str, int] = {}
    with runner.store._connect() as conn:
        for table in tables:
            result = conn.execute(f"delete from {table}")
            counts[table] = result.rowcount or 0
        conn.commit()
    return counts


def reset_synthesis_outputs(runner: ProcessingRunner) -> dict[str, int]:
    tables = [
        "brain_summaries",
        "connection_clusters",
        "sentiment_weekly",
    ]
    counts: dict[str, int] = {}
    with runner.store._connect() as conn:
        for table in tables:
            result = conn.execute(f"delete from {table}")
            counts[table] = result.rowcount or 0
        conn.commit()
    return counts


def run_prepare_parallel(runner: ProcessingRunner, workers: int) -> dict[str, int]:
    """Run one prepare batch with concurrent LLM enrichment calls."""
    runner.store.ensure_schema()
    run_id = runner.store.start_run()
    counts = {"enriched": 0, "embedded": 0}
    try:
        items = runner.store.fetch_unenriched_items(runner.config.enrichment_batch_size)
        if items:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(runner.llm.enrich_item, item): item for item in items}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                        runner.store.upsert_enrichment(
                            item,
                            result,
                            runner.config.openai_enrichment_model,
                        )
                        counts["enriched"] += 1
                    except Exception:
                        logging.exception("Enrichment failed source_item_id=%s", item.source_item_id)
        counts["embedded"] = runner.embed_pending()
        runner.store.complete_run(run_id, {"job": "prepare_processing", "counts": counts})
        return counts
    except Exception as exc:
        runner.store.fail_run(run_id, exc)
        raise


def table_counts(runner: ProcessingRunner) -> dict[str, int]:
    tables = [
        "source_items",
        "item_enrichments",
        "item_embeddings",
        "connection_clusters",
        "brain_summaries",
        "sentiment_weekly",
    ]
    counts: dict[str, int] = {}
    with runner.store._connect() as conn:
        for table in tables:
            row = conn.execute(f"select count(*)::int as rows from {table}").fetchone()
            counts[table] = int(row["rows"])
    return counts


if __name__ == "__main__":
    main()
