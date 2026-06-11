"""Key-protected processing status endpoint for Azure-side diagnostics."""

from __future__ import annotations

import json
import os
from typing import Any

import azure.functions as func
import psycopg


TABLES = (
    "source_items",
    "processing_runs",
    "item_enrichments",
    "item_embeddings",
    "connection_clusters",
    "brain_summaries",
    "sentiment_weekly",
)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Return processing table counts and recent run status."""
    dsn = os.getenv("AZURE_POSTGRES_DSN")
    if not dsn:
        return _json_response({"error": "AZURE_POSTGRES_DSN is not configured"}, status_code=500)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            counts = _table_counts(cur)
            ticker_coverage = _ticker_coverage(cur)
            recent_runs = _recent_runs(cur)

    return _json_response(
        {
            "counts": counts,
            "ticker_coverage": ticker_coverage,
            "recent_processing_runs": recent_runs,
        }
    )


def _table_counts(cur: psycopg.Cursor[Any]) -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    for table in TABLES:
        try:
            cur.execute(f"select count(*) from {table}")
            counts[table] = int(cur.fetchone()[0])
        except Exception as exc:
            cur.connection.rollback()
            counts[table] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return counts


def _ticker_coverage(cur: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    """Return output coverage by ticker and source."""
    try:
        cur.execute(
            """
            with tickers as (
                select distinct ticker from source_items
                union
                select distinct ticker from item_enrichments
                union
                select distinct ticker from item_embeddings
            ),
            source_counts as (
                select ticker, source, count(*)::int as source_items
                from source_items
                group by ticker, source
            ),
            enrichment_counts as (
                select ticker, source, count(*)::int as enriched_items,
                       count(*) filter (where relevance >= 0)::int as relevant_items,
                       max(relevance)::int as max_relevance
                from item_enrichments
                group by ticker, source
            ),
            embedding_counts as (
                select ticker, source, count(*)::int as embedded_items
                from item_embeddings
                group by ticker, source
            ),
            connection_counts as (
                select ticker,
                       count(*)::int as connections_total,
                       count(*) filter (where valid = true)::int as valid_connections
                from connection_clusters
                group by ticker
            ),
            summary_counts as (
                select ticker, count(*)::int as summaries
                from brain_summaries
                group by ticker
            ),
            sentiment_counts as (
                select ticker, count(*)::int as sentiment_rows
                from sentiment_weekly
                group by ticker
            )
            select t.ticker,
                   coalesce(jsonb_object_agg(
                       coalesce(sc.source, ec.source, emc.source),
                       jsonb_build_object(
                           'source_items', coalesce(sc.source_items, 0),
                           'enriched_items', coalesce(ec.enriched_items, 0),
                           'relevant_items', coalesce(ec.relevant_items, 0),
                           'embedded_items', coalesce(emc.embedded_items, 0),
                           'max_relevance', ec.max_relevance
                       )
                   ) filter (where coalesce(sc.source, ec.source, emc.source) is not null), '{}'::jsonb)
                       as by_source,
                   coalesce(sum(sc.source_items), 0)::int as source_items,
                   coalesce(sum(ec.enriched_items), 0)::int as enriched_items,
                   coalesce(sum(ec.relevant_items), 0)::int as relevant_items,
                   coalesce(sum(emc.embedded_items), 0)::int as embedded_items,
                   coalesce(cc.connections_total, 0)::int as connections_total,
                   coalesce(cc.valid_connections, 0)::int as valid_connections,
                   coalesce(suc.summaries, 0)::int as summaries,
                   coalesce(sec.sentiment_rows, 0)::int as sentiment_rows
            from tickers t
            left join source_counts sc on sc.ticker = t.ticker
            full join enrichment_counts ec
                on ec.ticker = t.ticker and ec.source = sc.source
            full join embedding_counts emc
                on emc.ticker = t.ticker and emc.source = coalesce(sc.source, ec.source)
            left join connection_counts cc on cc.ticker = t.ticker
            left join summary_counts suc on suc.ticker = t.ticker
            left join sentiment_counts sec on sec.ticker = t.ticker
            group by t.ticker, cc.connections_total, cc.valid_connections, suc.summaries, sec.sentiment_rows
            order by t.ticker
            """
        )
    except Exception:
        cur.connection.rollback()
        return []

    rows = cur.fetchall()
    return [
        {
            "ticker": row[0],
            "by_source": row[1] or {},
            "source_items": row[2],
            "enriched_items": row[3],
            "relevant_items": row[4],
            "embedded_items": row[5],
            "connections_total": row[6],
            "valid_connections": row[7],
            "summaries": row[8],
            "sentiment_rows": row[9],
        }
        for row in rows
    ]


def _recent_runs(cur: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    try:
        cur.execute(
            """
            select run_id, status, started_at, completed_at, error_message, metadata
            from processing_runs
            order by started_at desc
            limit 10
            """
        )
    except Exception:
        cur.connection.rollback()
        return []

    return [
        {
            "run_id": row[0],
            "status": row[1],
            "started_at": row[2].isoformat() if row[2] else None,
            "completed_at": row[3].isoformat() if row[3] else None,
            "error_message": row[4],
            "metadata": row[5] or {},
        }
        for row in cur.fetchall()
    ]


def _json_response(payload: dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, default=str),
        status_code=status_code,
        mimetype="application/json",
    )
