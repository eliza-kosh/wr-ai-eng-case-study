"""Shared class-based dataload runner contracts."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from shared.storage import BlobStore, PostgresStore, run_metadata


@dataclass(frozen=True)
class DataloadPartition:
    """A source+ticker unit of scheduled loading."""

    source: str
    ticker: str


@dataclass(frozen=True)
class DataloadWindow:
    """Source timestamp window for an incremental load run."""

    start: dt.datetime | None
    end: dt.datetime


@dataclass(frozen=True)
class DataloadRun:
    """Immutable run identity and source+ticker partition."""

    run_id: str
    partition: DataloadPartition
    window: DataloadWindow
    started_at: dt.datetime


class SourceDataloadRunner:
    """Base class for source loaders.

    The standard pattern is source+ticker level incremental loading:
    previous successful dataload_runs determine the next window, and source_items
    are upserted idempotently by stable source_item_id.
    """

    source: str
    tickers: tuple[str, ...] = ("AMD", "SNDK", "FROG", "APP", "KVYO")
    lookback: dt.timedelta = dt.timedelta(hours=2)
    initial_lookback: dt.timedelta = dt.timedelta(
        days=int(os.getenv("DATALOAD_INITIAL_LOOKBACK_DAYS", "180"))
    )

    def run_all(self) -> None:
        """Run dataload for each configured source+ticker partition."""
        for ticker in self.tickers:
            self.run_partition(DataloadPartition(source=self.source, ticker=ticker))

    def run_partition(self, partition: DataloadPartition) -> None:
        """Run one source+ticker load."""
        window = self.next_window(partition)
        run = DataloadRun(
            run_id=self.new_run_id(partition),
            partition=partition,
            window=window,
            started_at=dt.datetime.now(dt.UTC),
        )

        logging.info(
            "Starting dataload run_id=%s source=%s ticker=%s window_start=%s window_end=%s",
            run.run_id,
            partition.source,
            partition.ticker,
            window.start,
            window.end,
        )

        # Implementation sequence:
        # 1. Insert dataload_runs row with status='running'.
        # 2. Fetch source records for this source+ticker window.
        # 3. Write raw and normalized artifacts to Blob Storage.
        # 4. Upsert source_items into PostgreSQL by stable source_item_id.
        # 5. Mark dataload_runs row status='success' with row_count/blob paths.
        #
        # If any step fails, mark the run failed and do not let it advance the
        # next computed watermark.
        store = PostgresStore.from_env()
        store.ensure_schema()
        store.start_run(run, self.metadata(run))
        try:
            records = self.fetch(run)
            normalized = self.normalize(run, records)
            self.persist(run, records, normalized, store)
        except Exception as exc:
            logging.exception("Dataload run failed run_id=%s", run.run_id)
            store.fail_run(run, exc)
            raise

    def new_run_id(self, partition: DataloadPartition) -> str:
        """Create a traceable run id."""
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex[:8]
        return f"{partition.source}-{partition.ticker}-{timestamp}-{suffix}"

    def next_window(self, partition: DataloadPartition) -> DataloadWindow:
        """Compute the next source timestamp window.

        Implementation should query max(source_window_end) from successful
        dataload_runs for this source+ticker, then subtract self.lookback.
        """
        now = dt.datetime.now(dt.UTC)
        last_successful_end = self.load_last_successful_window_end(partition)
        start = (
            last_successful_end - self.lookback
            if last_successful_end
            else now - self.initial_lookback
        )
        return DataloadWindow(start=start, end=now)

    def load_last_successful_window_end(
        self, partition: DataloadPartition
    ) -> dt.datetime | None:
        """Load max successful source_window_end for source+ticker from PostgreSQL."""
        store = PostgresStore.from_env()
        store.ensure_schema()
        return store.last_successful_window_end(partition.source, partition.ticker)

    def fetch(self, run: DataloadRun) -> list[dict[str, Any]]:
        """Fetch source records for a run window."""
        raise NotImplementedError

    def normalize(
        self, run: DataloadRun, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize raw records into source_items shape."""
        raise NotImplementedError

    def persist(
        self,
        run: DataloadRun,
        raw_records: list[dict[str, Any]],
        normalized_records: list[dict[str, Any]],
        store: PostgresStore,
    ) -> None:
        """Write Blob artifacts and upsert PostgreSQL records."""
        blob_store = BlobStore.from_env()
        raw_blob_path = self.blob_path(run, "raw", "json")
        normalized_blob_path = self.blob_path(run, "normalized", "jsonl")

        blob_store.write_json(raw_blob_path, raw_records)
        blob_store.write_jsonl(normalized_blob_path, normalized_records)
        store.upsert_source_items(
            normalized_records,
            run,
            raw_blob_path=raw_blob_path,
            normalized_blob_path=normalized_blob_path,
        )
        store.complete_run(
            run,
            raw_blob_path=raw_blob_path,
            normalized_blob_path=normalized_blob_path,
            row_count=len(normalized_records),
        )
        logging.info("Dataload run complete run_id=%s rows=%d", run.run_id, len(normalized_records))

    def blob_path(self, run: DataloadRun, kind: str, extension: str) -> str:
        """Build a partitioned blob path for run artifacts."""
        run_date = run.started_at.date().isoformat()
        return (
            f"{kind}/source={run.partition.source}/ticker={run.partition.ticker}/"
            f"run_date={run_date}/{run.run_id}.{extension}"
        )

    def metadata(self, run: DataloadRun) -> dict[str, Any]:
        """Metadata stored on dataload_runs."""
        return run_metadata(run)


def stable_source_item_id(source: str, native_id: str) -> str:
    """Create a stable primary key for a source item."""
    digest = hashlib.sha256(f"{source}:{native_id}".encode("utf-8")).hexdigest()[:24]
    return f"{source}:{digest}"
