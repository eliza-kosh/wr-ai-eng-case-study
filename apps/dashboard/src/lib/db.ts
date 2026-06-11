import { Pool, type QueryResultRow } from "pg";

export type SourceItem = {
  id: string;
  source: string;
  sourceUrl: string | null;
  title: string | null;
  summary: string;
  sentiment: "bullish" | "bearish" | "neutral";
  relevance: number;
  firsthand: boolean;
  firsthandType: string | null;
  publishedAt: string | null;
  cited: boolean;
};

export type ConnectionItem = {
  id: string;
  sourceA: string;
  sourceB: string;
  sourceAText: string | null;
  sourceBText: string | null;
  confidence: number;
  narrative: string;
  stockRelevance: string;
  connectionType: string;
};

export type SentimentPoint = { source: string; weekStart: string; itemCount: number; sentimentAvg: number; alert: boolean };
export type PipelineStatus = { dataloadRuns: number; successfulDataloadRuns: number; sourceItems: number; enrichments: number; embeddings: number; connections: number; summaries: number; sentimentRows: number; latestRunAt: string | null };
export type BrainSummary = { headline: string; confidence: string; bearCase: string; keySignals: unknown; crossSourceConnections: unknown; citedItemIds: string[]; generatedAt: string };
export type DashboardData = { ticker: string; tickers: string[]; error: string | null; summary: BrainSummary | null; sources: SourceItem[]; connections: ConnectionItem[]; sentiment: SentimentPoint[]; pipeline: PipelineStatus };

let pool: Pool | null = null;

function getPool() {
  const dsn = process.env.AZURE_POSTGRES_DSN || process.env.DATABASE_URL;
  if (!dsn) throw new Error("Set AZURE_POSTGRES_DSN or DATABASE_URL to connect the dashboard to Postgres.");
  pool ??= new Pool({ connectionString: dsn, max: 5, ssl: { rejectUnauthorized: false } });
  return pool;
}

async function query<T extends QueryResultRow>(sql: string, params: unknown[] = []) {
  return (await getPool().query<T>(sql, params)).rows;
}

export async function getDashboardData(tickerParam?: string): Promise<DashboardData> {
  try {
    const tickers = await getTickers();
    const ticker = tickerParam && tickers.includes(tickerParam) ? tickerParam : tickers[0] || "AMD";
    const [summary, sources, connections, sentiment, pipeline] = await Promise.all([
      getSummary(ticker),
      getSources(ticker),
      getConnections(ticker),
      getSentiment(ticker),
      getPipeline(),
    ]);
    return { ticker, tickers: tickers.length ? tickers : [ticker], summary, sources, connections, sentiment, pipeline, error: null };
  } catch (error) {
    return { ticker: tickerParam || "AMD", tickers: [tickerParam || "AMD"], summary: null, sources: [], connections: [], sentiment: [], pipeline: emptyPipeline(), error: error instanceof Error ? error.message : String(error) };
  }
}

async function getTickers() {
  const rows = await query<{ ticker: string }>(`select ticker from (select distinct ticker from source_items union select distinct ticker from brain_summaries union select distinct ticker from sentiment_weekly) t where ticker is not null order by ticker`);
  return rows.map((r) => r.ticker);
}

async function getSummary(ticker: string) {
  const rows = await query<{ headline: string; confidence: string; bear_case: string; key_signals: unknown; cross_source_connections: unknown; cited_item_ids: string[] | null; generated_at: Date }>(
    `select headline,confidence,bear_case,key_signals,cross_source_connections,cited_item_ids,generated_at from brain_summaries where ticker=$1 order by generated_at desc limit 1`,
    [ticker],
  );
  const r = rows[0];
  return r
    ? { headline: r.headline, confidence: r.confidence, bearCase: r.bear_case, keySignals: r.key_signals, crossSourceConnections: r.cross_source_connections, citedItemIds: r.cited_item_ids || [], generatedAt: r.generated_at.toISOString() }
    : null;
}

async function getSources(ticker: string): Promise<SourceItem[]> {
  const rows = await query<{ source_item_id: string; source: string; source_url: string | null; title: string | null; body: string | null; published_at: Date | null; summary: string | null; sentiment: "bullish" | "bearish" | "neutral" | null; relevance: number | null; firsthand: boolean | null; firsthand_type: string | null; cited: boolean }>(
    `with latest_summary as (select cited_item_ids from brain_summaries where ticker=$1 order by generated_at desc limit 1) select si.source_item_id,si.source,si.source_url,si.title,si.body,si.published_at,ie.summary,ie.sentiment,ie.relevance,ie.firsthand,ie.firsthand_type,si.source_item_id = any(coalesce((select cited_item_ids from latest_summary), array[]::text[])) as cited from source_items si left join item_enrichments ie on ie.source_item_id=si.source_item_id where si.ticker=$1 order by cited desc,ie.relevance desc nulls last,si.published_at desc nulls last limit 150`,
    [ticker],
  );
  return rows.map((r) => ({ id: r.source_item_id, source: r.source, sourceUrl: r.source_url, title: r.title, summary: r.summary || r.title || r.body?.slice(0, 240) || "No summary available yet.", sentiment: r.sentiment || "neutral", relevance: r.relevance ?? 0, firsthand: Boolean(r.firsthand), firsthandType: r.firsthand_type, publishedAt: r.published_at?.toISOString() || null, cited: r.cited }));
}

async function getConnections(ticker: string): Promise<ConnectionItem[]> {
  const rows = await query<{ connection_id: string; source_a: string; source_b: string; source_a_text: string | null; source_b_text: string | null; confidence: number; narrative: string; stock_relevance: string; connection_type: string }>(
    `select ic.connection_id,ic.source_a,ic.source_b,coalesce(nullif(ae.summary,''),nullif(left(a.body,220),''),nullif(a.title,'')) as source_a_text,coalesce(nullif(be.summary,''),nullif(left(b.body,220),''),nullif(b.title,'')) as source_b_text,ic.confidence,ic.narrative,ic.stock_relevance,ic.connection_type from item_connections ic left join source_items a on a.source_item_id=ic.source_a left join source_items b on b.source_item_id=ic.source_b left join item_enrichments ae on ae.source_item_id=ic.source_a left join item_enrichments be on be.source_item_id=ic.source_b where ic.ticker=$1 and ic.valid=true order by ic.confidence desc,ic.verified_at desc limit 24`,
    [ticker],
  );
  return rows.map((r) => ({ id: r.connection_id, sourceA: r.source_a, sourceB: r.source_b, sourceAText: r.source_a_text, sourceBText: r.source_b_text, confidence: Number(r.confidence), narrative: r.narrative, stockRelevance: r.stock_relevance, connectionType: r.connection_type }));
}

async function getSentiment(ticker: string): Promise<SentimentPoint[]> {
  const rows = await query<{ source: string; week_start: Date; item_count: number; sentiment_avg: number; alert: boolean }>(`select source,week_start,item_count,sentiment_avg,alert from sentiment_weekly where ticker=$1 order by week_start asc,source asc`, [ticker]);
  return rows.map((r) => ({ source: r.source, weekStart: r.week_start.toISOString().slice(0, 10), itemCount: Number(r.item_count), sentimentAvg: Number(r.sentiment_avg), alert: r.alert }));
}

async function getPipeline(): Promise<PipelineStatus> {
  const rows = await query<PipelineStatus>(`select (select count(*)::int from dataload_runs) as "dataloadRuns",(select count(*)::int from dataload_runs where status='success') as "successfulDataloadRuns",(select count(*)::int from source_items) as "sourceItems",(select count(*)::int from item_enrichments) as "enrichments",(select count(*)::int from item_embeddings) as "embeddings",(select count(*)::int from item_connections where valid=true) as "connections",(select count(*)::int from brain_summaries) as "summaries",(select count(*)::int from sentiment_weekly) as "sentimentRows",greatest((select max(completed_at) from dataload_runs),(select max(enriched_at) from item_enrichments),(select max(generated_at) from brain_summaries),(select max(refreshed_at) from sentiment_weekly)) as "latestRunAt"`);
  return rows[0] || emptyPipeline();
}

function emptyPipeline(): PipelineStatus {
  return { dataloadRuns: 0, successfulDataloadRuns: 0, sourceItems: 0, enrichments: 0, embeddings: 0, connections: 0, summaries: 0, sentimentRows: 0, latestRunAt: null };
}

