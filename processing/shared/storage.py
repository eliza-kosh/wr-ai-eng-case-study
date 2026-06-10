"""PostgreSQL persistence and retrieval for processing."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Any

from shared.config import ProcessingConfig
from shared.models import ConnectionCandidate, EmbeddedItem, EnrichmentResult, SourceItem


def utc_now() -> dt.datetime:
    """Return current UTC datetime."""
    return dt.datetime.now(dt.UTC)


class ProcessingStore:
    """PostgreSQL store for processing state and outputs."""

    def __init__(self, dsn: str, config: ProcessingConfig) -> None:
        self.dsn = dsn
        self.config = config

    @classmethod
    def from_env(cls, config: ProcessingConfig) -> "ProcessingStore":
        dsn = os.getenv("AZURE_POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("AZURE_POSTGRES_DSN is required")
        return cls(dsn, config)

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def ensure_schema(self) -> None:
        """Create pgvector-backed processing tables if needed."""
        dims = self.config.embedding_dimensions
        ddl = f"""
        create extension if not exists vector;

        create table if not exists processing_runs (
            run_id text primary key,
            started_at timestamptz not null,
            completed_at timestamptz,
            status text not null,
            error_message text,
            metadata jsonb not null default '{{}}'
        );

        create table if not exists item_enrichments (
            source_item_id text primary key references source_items(source_item_id) on delete cascade,
            ticker text not null,
            source text not null,
            relevance integer not null check (relevance between 0 and 10),
            sentiment text not null check (sentiment in ('bullish', 'bearish', 'neutral')),
            sentiment_rationale text not null,
            themes text[] not null default '{{}}',
            firsthand boolean not null default false,
            firsthand_type text,
            summary text not null,
            model text not null,
            enriched_at timestamptz not null default now()
        );

        create index if not exists item_enrichments_ticker_source_relevance_idx
            on item_enrichments(ticker, source, relevance desc);

        create table if not exists item_embeddings (
            source_item_id text primary key references item_enrichments(source_item_id) on delete cascade,
            ticker text not null,
            source text not null,
            published_at timestamptz,
            summary text not null,
            embedding vector({dims}) not null,
            model text not null,
            embedded_at timestamptz not null default now()
        );

        create index if not exists item_embeddings_ticker_source_published_idx
            on item_embeddings(ticker, source, published_at desc);

        create index if not exists item_embeddings_embedding_ivfflat_idx
            on item_embeddings using ivfflat (embedding vector_cosine_ops) with (lists = 100);

        create table if not exists item_connections (
            connection_id text primary key,
            ticker text not null,
            item_a_id text not null references source_items(source_item_id) on delete cascade,
            item_b_id text not null references source_items(source_item_id) on delete cascade,
            source_a text not null,
            source_b text not null,
            similarity double precision not null,
            valid boolean not null,
            confidence double precision not null,
            narrative text not null,
            stock_relevance text not null,
            connection_type text not null,
            model text not null,
            run_id text references processing_runs(run_id),
            verified_at timestamptz not null default now(),
            metadata jsonb not null default '{{}}',
            constraint item_connections_pair_unique unique (ticker, item_a_id, item_b_id)
        );

        create index if not exists item_connections_ticker_valid_confidence_idx
            on item_connections(ticker, valid, confidence desc, verified_at desc);

        create table if not exists brain_summaries (
            summary_id text primary key,
            ticker text not null,
            headline text not null,
            key_signals jsonb not null,
            cross_source_connections jsonb not null,
            bear_case text not null,
            confidence text not null,
            cited_item_ids text[] not null default '{{}}',
            invalid_citation_ids text[] not null default '{{}}',
            search_log jsonb not null default '[]',
            model text not null,
            run_id text references processing_runs(run_id),
            generated_at timestamptz not null default now()
        );

        create index if not exists brain_summaries_ticker_generated_idx
            on brain_summaries(ticker, generated_at desc);

        create table if not exists sentiment_weekly (
            ticker text not null,
            source text not null,
            week_start date not null,
            item_count integer not null,
            sentiment_avg double precision not null,
            rolling_mean_8w double precision,
            rolling_stddev_8w double precision,
            z_score double precision,
            alert boolean not null default false,
            refreshed_at timestamptz not null default now(),
            primary key (ticker, source, week_start)
        );
        """
        with self._connect() as conn:
            conn.execute(ddl)
            conn.commit()

    def start_run(self) -> str:
        from psycopg.types.json import Jsonb

        run_id = f"processing-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        metadata = {
            "tickers": self.config.tickers,
            "sources": self.config.sources,
            "thresholds": {
                "relevance": self.config.relevance_threshold,
                "similarity": self.config.similarity_threshold,
                "connection_confidence": self.config.connection_confidence_threshold,
            },
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into processing_runs (run_id, started_at, status, metadata)
                values (%s, %s, 'running', %s)
                """,
                (run_id, utc_now(), Jsonb(metadata)),
            )
            conn.commit()
        return run_id

    def complete_run(self, run_id: str, metadata: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                """
                update processing_runs
                set status = 'success', completed_at = %s, metadata = metadata || %s
                where run_id = %s
                """,
                (utc_now(), Jsonb(metadata), run_id),
            )
            conn.commit()

    def fail_run(self, run_id: str, error: Exception) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update processing_runs
                set status = 'failed', completed_at = %s, error_message = %s
                where run_id = %s
                """,
                (utc_now(), str(error), run_id),
            )
            conn.commit()

    def fetch_unenriched_items(self, limit: int) -> list[SourceItem]:
        sql = """
        select si.source_item_id, si.ticker, si.source, si.source_url, si.title, si.body,
               si.author, si.published_at, si.metadata
        from source_items si
        left join item_enrichments ie on ie.source_item_id = si.source_item_id
        where ie.source_item_id is null
          and si.source = any(%s)
          and si.ticker = any(%s)
        order by si.fetched_at asc
        limit %s
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (list(self.config.sources), list(self.config.tickers), limit)).fetchall()
        return [_source_item(row) for row in rows]

    def upsert_enrichment(self, item: SourceItem, result: EnrichmentResult, model: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into item_enrichments (
                    source_item_id, ticker, source, relevance, sentiment, sentiment_rationale,
                    themes, firsthand, firsthand_type, summary, model, enriched_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (source_item_id) do update set
                    relevance = excluded.relevance,
                    sentiment = excluded.sentiment,
                    sentiment_rationale = excluded.sentiment_rationale,
                    themes = excluded.themes,
                    firsthand = excluded.firsthand,
                    firsthand_type = excluded.firsthand_type,
                    summary = excluded.summary,
                    model = excluded.model,
                    enriched_at = excluded.enriched_at
                """,
                (
                    item.source_item_id,
                    item.ticker,
                    item.source,
                    result.relevance,
                    result.sentiment,
                    result.sentiment_rationale,
                    list(result.themes),
                    result.firsthand,
                    result.firsthand_type,
                    result.summary,
                    model,
                    utc_now(),
                ),
            )
            conn.commit()

    def fetch_unembedded_items(self, limit: int) -> list[EmbeddedItem]:
        sql = """
        select ie.source_item_id, ie.ticker, ie.source, si.published_at, ie.summary
        from item_enrichments ie
        join source_items si on si.source_item_id = ie.source_item_id
        left join item_embeddings emb on emb.source_item_id = ie.source_item_id
        where emb.source_item_id is null
          and ie.relevance >= %s
          and ie.summary <> ''
          and ie.ticker = any(%s)
          and ie.source = any(%s)
        order by ie.enriched_at asc
        limit %s
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                (
                    self.config.relevance_threshold,
                    list(self.config.tickers),
                    list(self.config.sources),
                    limit,
                ),
            ).fetchall()
        return [
            EmbeddedItem(
                source_item_id=row["source_item_id"],
                ticker=row["ticker"],
                source=row["source"],
                published_at=row["published_at"],
                summary=row["summary"],
            )
            for row in rows
        ]

    def upsert_embeddings(self, items: list[EmbeddedItem], embeddings: list[list[float]], model: str) -> None:
        values = []
        for item, embedding in zip(items, embeddings, strict=True):
            values.append(
                (
                    item.source_item_id,
                    item.ticker,
                    item.source,
                    item.published_at,
                    item.summary,
                    _vector_literal(embedding),
                    model,
                    utc_now(),
                )
            )
        if not values:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    insert into item_embeddings (
                        source_item_id, ticker, source, published_at, summary,
                        embedding, model, embedded_at
                    )
                    values (%s, %s, %s, %s, %s, %s::vector, %s, %s)
                    on conflict (source_item_id) do update set
                        ticker = excluded.ticker,
                        source = excluded.source,
                        published_at = excluded.published_at,
                        summary = excluded.summary,
                        embedding = excluded.embedding,
                        model = excluded.model,
                        embedded_at = excluded.embedded_at
                    """,
                    values,
                )
            conn.commit()

    def fetch_tickers_with_embeddings(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "select distinct ticker from item_embeddings where ticker = any(%s) order by ticker",
                (list(self.config.tickers),),
            ).fetchall()
        return [row["ticker"] for row in rows]

    def fetch_connection_candidates(self, ticker: str) -> list[ConnectionCandidate]:
        sql = """
        select least(a.source_item_id, b.source_item_id) as item_a_id,
               greatest(a.source_item_id, b.source_item_id) as item_b_id,
               a.ticker,
               case when a.source_item_id <= b.source_item_id then a.source else b.source end as source_a,
               case when a.source_item_id <= b.source_item_id then b.source else a.source end as source_b,
               case when a.source_item_id <= b.source_item_id then a.published_at else b.published_at end as published_a,
               case when a.source_item_id <= b.source_item_id then b.published_at else a.published_at end as published_b,
               case when a.source_item_id <= b.source_item_id then a.summary else b.summary end as summary_a,
               case when a.source_item_id <= b.source_item_id then b.summary else a.summary end as summary_b,
               1 - (a.embedding <=> b.embedding) as similarity
        from item_embeddings a
        join item_embeddings b
          on a.ticker = b.ticker
         and a.source_item_id < b.source_item_id
         and a.source <> b.source
        left join item_connections existing
          on existing.ticker = a.ticker
         and existing.item_a_id = least(a.source_item_id, b.source_item_id)
         and existing.item_b_id = greatest(a.source_item_id, b.source_item_id)
        where a.ticker = %s
          and existing.connection_id is null
          and (1 - (a.embedding <=> b.embedding)) >= %s
          and (a.published_at is null or a.published_at >= now() - %s * interval '1 second')
          and (b.published_at is null or b.published_at >= now() - %s * interval '1 second')
          and (
              a.published_at is null or b.published_at is null or
              abs(extract(epoch from (a.published_at - b.published_at))) <= %s
          )
        order by similarity desc
        limit %s
        """
        seconds = self.config.temporal_window_days * 24 * 60 * 60
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                (
                    ticker,
                    self.config.similarity_threshold,
                    seconds,
                    seconds,
                    seconds,
                    self.config.max_connection_candidates_per_ticker,
                ),
            ).fetchall()
        return [
            ConnectionCandidate(
                item_a_id=row["item_a_id"],
                item_b_id=row["item_b_id"],
                ticker=row["ticker"],
                source_a=row["source_a"],
                source_b=row["source_b"],
                published_a=row["published_a"],
                published_b=row["published_b"],
                summary_a=row["summary_a"],
                summary_b=row["summary_b"],
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]

    def upsert_connection(self, candidate: ConnectionCandidate, verification: Any, model: str, run_id: str) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                """
                insert into item_connections (
                    connection_id, ticker, item_a_id, item_b_id, source_a, source_b,
                    similarity, valid, confidence, narrative, stock_relevance,
                    connection_type, model, run_id, verified_at, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (ticker, item_a_id, item_b_id) do update set
                    similarity = excluded.similarity,
                    valid = excluded.valid,
                    confidence = excluded.confidence,
                    narrative = excluded.narrative,
                    stock_relevance = excluded.stock_relevance,
                    connection_type = excluded.connection_type,
                    model = excluded.model,
                    run_id = excluded.run_id,
                    verified_at = excluded.verified_at,
                    metadata = excluded.metadata
                """,
                (
                    f"conn-{uuid.uuid4().hex}",
                    candidate.ticker,
                    candidate.item_a_id,
                    candidate.item_b_id,
                    candidate.source_a,
                    candidate.source_b,
                    candidate.similarity,
                    verification.valid,
                    verification.confidence,
                    verification.narrative,
                    verification.stock_relevance,
                    verification.connection_type,
                    model,
                    run_id,
                    utc_now(),
                    Jsonb({"candidate": _jsonable_candidate(candidate)}),
                ),
            )
            conn.commit()

    def fetch_initial_summary_context(self, ticker: str, per_source: int) -> tuple[dict[str, Any], set[str]]:
        context: dict[str, Any] = {"items_by_source": {}, "connections": []}
        allowed_ids: set[str] = set()
        window_seconds = self.config.temporal_window_days * 24 * 60 * 60
        with self._connect() as conn:
            for source in self.config.sources:
                rows = conn.execute(
                    """
                    select ie.source_item_id, ie.source, ie.relevance, ie.sentiment, ie.themes,
                           ie.firsthand, ie.summary, si.published_at, si.source_url
                    from item_enrichments ie
                    join source_items si on si.source_item_id = ie.source_item_id
                    where ie.ticker = %s and ie.source = %s and ie.relevance >= %s
                      and (si.published_at is null or si.published_at >= now() - %s * interval '1 second')
                    order by ie.relevance desc, si.published_at desc nulls last
                    limit %s
                    """,
                    (ticker, source, self.config.relevance_threshold, window_seconds, per_source),
                ).fetchall()
                context["items_by_source"][source] = [dict(row) for row in rows]
                allowed_ids.update(row["source_item_id"] for row in rows)
            connections = conn.execute(
                """
                select ic.item_a_id, ic.item_b_id, ic.source_a, ic.source_b, ic.confidence,
                       ic.narrative, ic.stock_relevance, ic.connection_type
                from item_connections ic
                join item_embeddings ea on ea.source_item_id = ic.item_a_id
                join item_embeddings eb on eb.source_item_id = ic.item_b_id
                where ic.ticker = %s and ic.valid = true and ic.confidence >= %s
                  and (ea.published_at is null or ea.published_at >= now() - %s * interval '1 second')
                  and (eb.published_at is null or eb.published_at >= now() - %s * interval '1 second')
                order by ic.confidence desc, ic.verified_at desc
                limit 25
                """,
                (ticker, self.config.connection_confidence_threshold, window_seconds, window_seconds),
            ).fetchall()
        context["connections"] = [dict(row) for row in connections]
        for row in connections:
            allowed_ids.add(row["item_a_id"])
            allowed_ids.add(row["item_b_id"])
        return context, allowed_ids

    def semantic_search(self, ticker: str, query_embedding: list[float], limit: int = 10) -> list[dict[str, Any]]:
        sql = """
        select emb.source_item_id, emb.source, emb.published_at, emb.summary,
               ie.relevance, ie.sentiment, 1 - (emb.embedding <=> %s::vector) as similarity
        from item_embeddings emb
        join item_enrichments ie on ie.source_item_id = emb.source_item_id
        where emb.ticker = %s
          and (emb.published_at is null or emb.published_at >= now() - %s * interval '1 second')
        order by emb.embedding <=> %s::vector
        limit %s
        """
        vector = _vector_literal(query_embedding)
        window_seconds = self.config.temporal_window_days * 24 * 60 * 60
        with self._connect() as conn:
            rows = conn.execute(sql, (vector, ticker, window_seconds, vector, limit)).fetchall()
        return [dict(row) for row in rows]

    def insert_brain_summary(
        self,
        ticker: str,
        payload: dict[str, Any],
        invalid_citations: set[str],
        search_log: list[dict[str, Any]],
        run_id: str,
        model: str,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                """
                insert into brain_summaries (
                    summary_id, ticker, headline, key_signals, cross_source_connections,
                    bear_case, confidence, cited_item_ids, invalid_citation_ids,
                    search_log, model, run_id, generated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"summary-{ticker}-{uuid.uuid4().hex[:12]}",
                    ticker,
                    payload["headline"],
                    Jsonb(payload["key_signals"]),
                    Jsonb(payload["cross_source_connections"]),
                    payload["bear_case"],
                    payload["confidence"],
                    list(payload.get("cited_item_ids", [])),
                    list(invalid_citations),
                    Jsonb(search_log),
                    model,
                    run_id,
                    utc_now(),
                ),
            )
            conn.commit()

    def refresh_sentiment_weekly(self) -> int:
        sql = """
        with weekly as (
            select ie.ticker,
                   ie.source,
                   date_trunc('week', coalesce(si.published_at, ie.enriched_at))::date as week_start,
                   count(*)::int as item_count,
                   avg(case ie.sentiment
                       when 'bullish' then 1
                       when 'bearish' then -1
                       else 0
                   end)::double precision as sentiment_avg
            from item_enrichments ie
            join source_items si on si.source_item_id = ie.source_item_id
            group by ie.ticker, ie.source, date_trunc('week', coalesce(si.published_at, ie.enriched_at))::date
        ), scored as (
            select *,
                   avg(sentiment_avg) over (
                       partition by ticker, source order by week_start
                       rows between 8 preceding and 1 preceding
                   ) as rolling_mean_8w,
                   stddev_samp(sentiment_avg) over (
                       partition by ticker, source order by week_start
                       rows between 8 preceding and 1 preceding
                   ) as rolling_stddev_8w
            from weekly
        ), final as (
            select *,
                   case
                       when rolling_stddev_8w is null or rolling_stddev_8w = 0 then null
                       else (sentiment_avg - rolling_mean_8w) / rolling_stddev_8w
                   end as z_score
            from scored
        )
        insert into sentiment_weekly (
            ticker, source, week_start, item_count, sentiment_avg,
            rolling_mean_8w, rolling_stddev_8w, z_score, alert, refreshed_at
        )
        select ticker, source, week_start, item_count, sentiment_avg,
               rolling_mean_8w, rolling_stddev_8w, z_score,
               coalesce(abs(z_score) > 2, false) as alert,
               now()
        from final
        on conflict (ticker, source, week_start) do update set
            item_count = excluded.item_count,
            sentiment_avg = excluded.sentiment_avg,
            rolling_mean_8w = excluded.rolling_mean_8w,
            rolling_stddev_8w = excluded.rolling_stddev_8w,
            z_score = excluded.z_score,
            alert = excluded.alert,
            refreshed_at = excluded.refreshed_at
        """
        with self._connect() as conn:
            result = conn.execute(sql)
            conn.commit()
            return result.rowcount or 0


def _source_item(row: dict[str, Any]) -> SourceItem:
    return SourceItem(
        source_item_id=row["source_item_id"],
        ticker=row["ticker"],
        source=row["source"],
        source_url=row.get("source_url"),
        title=row.get("title"),
        body=row.get("body"),
        author=row.get("author"),
        published_at=row.get("published_at"),
        metadata=row.get("metadata") or {},
    )


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def _jsonable_candidate(candidate: ConnectionCandidate) -> dict[str, Any]:
    return json.loads(json.dumps(candidate.__dict__, default=str))
