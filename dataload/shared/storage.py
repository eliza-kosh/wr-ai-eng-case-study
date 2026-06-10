"""Blob Storage and PostgreSQL persistence helpers for dataload jobs."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from shared.base import DataloadRun


def utc_now() -> dt.datetime:
    """Return current UTC datetime."""
    return dt.datetime.now(dt.UTC)


def to_iso(value: dt.datetime | None) -> str | None:
    """Serialize a datetime for JSON metadata."""
    return value.isoformat() if value else None


def parse_datetime(value: str | None) -> dt.datetime | None:
    """Parse common API datetime strings."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def json_default(value: Any) -> str:
    """JSON serializer for datetime-like values."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


class BlobStore:
    """Azure Blob Storage writer."""

    def __init__(self, container_client: Any) -> None:
        self.container_client = container_client

    @classmethod
    def from_env(cls) -> "BlobStore":
        """Create a BlobStore from environment variables."""
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container = os.getenv("AZURE_STORAGE_CONTAINER")
        if not connection_string or not container:
            raise RuntimeError(
                "AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER are required"
            )

        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient.from_connection_string(connection_string)
        return cls(service.get_container_client(container))

    def write_json(self, path: str, payload: Any) -> str:
        """Write JSON payload to Blob Storage."""
        body = json.dumps(payload, indent=2, default=json_default).encode("utf-8")
        self.container_client.upload_blob(path, body, overwrite=True)
        logging.info("Wrote blob path=%s bytes=%d", path, len(body))
        return path

    def write_jsonl(self, path: str, rows: list[dict[str, Any]]) -> str:
        """Write JSONL payload to Blob Storage."""
        body = "\n".join(json.dumps(row, default=json_default) for row in rows).encode(
            "utf-8"
        )
        self.container_client.upload_blob(path, body, overwrite=True)
        logging.info("Wrote blob path=%s rows=%d bytes=%d", path, len(rows), len(body))
        return path


class PostgresStore:
    """PostgreSQL run-ledger and source-item store."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @classmethod
    def from_env(cls) -> "PostgresStore":
        """Create a PostgresStore from environment variables."""
        dsn = os.getenv("AZURE_POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("AZURE_POSTGRES_DSN is required")
        return cls(dsn)

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=15)

    def ensure_schema(self) -> None:
        """Create minimal tables if they do not exist."""
        ddl = """
        create table if not exists dataload_runs (
            run_id text primary key,
            source text not null,
            ticker text not null,
            started_at timestamptz not null,
            completed_at timestamptz,
            status text not null,
            source_window_start timestamptz,
            source_window_end timestamptz not null,
            raw_blob_path text,
            normalized_blob_path text,
            row_count integer not null default 0,
            error_message text,
            metadata jsonb not null default '{}'
        );

        create index if not exists dataload_runs_source_ticker_status_idx
            on dataload_runs(source, ticker, status, source_window_end desc);

        create table if not exists source_items (
            source_item_id text primary key,
            ticker text not null,
            source text not null,
            source_url text,
            title text,
            body text,
            author text,
            published_at timestamptz,
            fetched_at timestamptz not null,
            run_id text not null references dataload_runs(run_id),
            blob_raw_path text not null,
            blob_normalized_path text not null,
            metadata jsonb not null default '{}'
        );

        create index if not exists source_items_source_ticker_published_idx
            on source_items(source, ticker, published_at desc);
        """
        with self._connect() as conn:
            conn.execute(ddl)
            conn.commit()

    def last_successful_window_end(self, source: str, ticker: str) -> dt.datetime | None:
        """Return the source+ticker high watermark from successful runs."""
        sql = """
        select max(source_window_end)
        from dataload_runs
        where source = %s and ticker = %s and status = 'success'
        """
        with self._connect() as conn:
            return conn.execute(sql, (source, ticker)).fetchone()[0]

    def start_run(self, run: "DataloadRun", metadata: dict[str, Any]) -> None:
        """Insert a running dataload run."""
        from psycopg.types.json import Jsonb

        sql = """
        insert into dataload_runs (
            run_id, source, ticker, started_at, status,
            source_window_start, source_window_end, metadata
        )
        values (%s, %s, %s, %s, 'running', %s, %s, %s)
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    run.run_id,
                    run.partition.source,
                    run.partition.ticker,
                    run.started_at,
                    run.window.start,
                    run.window.end,
                    Jsonb(metadata),
                ),
            )
            conn.commit()

    def complete_run(
        self,
        run: "DataloadRun",
        raw_blob_path: str,
        normalized_blob_path: str,
        row_count: int,
    ) -> None:
        """Mark a dataload run as successful."""
        sql = """
        update dataload_runs
        set status = 'success',
            completed_at = %s,
            raw_blob_path = %s,
            normalized_blob_path = %s,
            row_count = %s,
            error_message = null
        where run_id = %s
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (utc_now(), raw_blob_path, normalized_blob_path, row_count, run.run_id),
            )
            conn.commit()

    def fail_run(self, run: "DataloadRun", error: Exception) -> None:
        """Mark a dataload run as failed."""
        sql = """
        update dataload_runs
        set status = 'failed',
            completed_at = %s,
            error_message = %s
        where run_id = %s
        """
        with self._connect() as conn:
            conn.execute(sql, (utc_now(), str(error), run.run_id))
            conn.commit()

    def upsert_source_items(
        self,
        rows: list[dict[str, Any]],
        run: "DataloadRun",
        raw_blob_path: str,
        normalized_blob_path: str,
    ) -> None:
        """Upsert normalized source_items."""
        from psycopg.types.json import Jsonb

        sql = """
        insert into source_items (
            source_item_id, ticker, source, source_url, title, body, author,
            published_at, fetched_at, run_id, blob_raw_path,
            blob_normalized_path, metadata
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (source_item_id) do update set
            title = excluded.title,
            body = excluded.body,
            author = excluded.author,
            published_at = excluded.published_at,
            fetched_at = excluded.fetched_at,
            run_id = excluded.run_id,
            blob_raw_path = excluded.blob_raw_path,
            blob_normalized_path = excluded.blob_normalized_path,
            metadata = excluded.metadata
        """
        values = [
            (
                row["source_item_id"],
                row["ticker"],
                row["source"],
                row.get("source_url"),
                row.get("title"),
                row.get("body"),
                row.get("author"),
                row.get("published_at"),
                row["fetched_at"],
                run.run_id,
                raw_blob_path,
                normalized_blob_path,
                Jsonb(row.get("metadata", {})),
            )
            for row in rows
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, values)
            conn.commit()


def run_metadata(run: "DataloadRun", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create serializable run metadata."""
    metadata = {
        "partition": asdict(run.partition),
        "window": {
            "start": to_iso(run.window.start),
            "end": to_iso(run.window.end),
        },
    }
    if extra:
        metadata.update(extra)
    return metadata
