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
    "item_connections",
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
            recent_runs = _recent_runs(cur)

    return _json_response({"counts": counts, "recent_processing_runs": recent_runs})


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


def _recent_runs(cur: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    try:
        cur.execute(
            """
            select run_id, run_type, status, started_at, completed_at, error_message
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
            "run_type": row[1],
            "status": row[2],
            "started_at": row[3].isoformat() if row[3] else None,
            "completed_at": row[4].isoformat() if row[4] else None,
            "error_message": row[5],
        }
        for row in cur.fetchall()
    ]


def _json_response(payload: dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, default=str),
        status_code=status_code,
        mimetype="application/json",
    )
