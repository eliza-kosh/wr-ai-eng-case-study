import { Pool, type QueryResultRow } from "pg";

export type SourceItem = {
  id: string;
  source: string;
  sourceUrl: string | null;
  title: string | null;
  summary: string;
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

export type BrainSummary = { headline: string; overview: string; confidence: string; bearCase: string; keySignals: unknown; crossSourceConnections: unknown; citedItemIds: string[]; generatedAt: string };
export type DashboardData = { ticker: string; tickers: string[]; error: string | null; summary: BrainSummary | null; sources: SourceItem[]; connections: ConnectionItem[] };

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
    const [summary, sources, connections] = await Promise.all([
      getSummary(ticker),
      getSources(ticker),
      getConnections(ticker),
    ]);
    return { ticker, tickers: tickers.length ? tickers : [ticker], summary, sources, connections, error: null };
  } catch (error) {
    return { ticker: tickerParam || "AMD", tickers: [tickerParam || "AMD"], summary: null, sources: [], connections: [], error: error instanceof Error ? error.message : String(error) };
  }
}

async function getTickers() {
  const rows = await query<{ ticker: string }>(`select ticker from (select distinct ticker from source_items union select distinct ticker from brain_summaries) t where ticker is not null order by ticker`);
  return rows.map((r) => r.ticker);
}

async function getSummary(ticker: string) {
  const rows = await query<{ headline: string; overview: string | null; confidence: string; bear_case: string; key_signals: unknown; cross_source_connections: unknown; cited_item_ids: string[] | null; generated_at: Date }>(
    `select headline,coalesce(overview,'') as overview,confidence,bear_case,key_signals,cross_source_connections,cited_item_ids,generated_at from brain_summaries where ticker=$1 order by generated_at desc limit 1`,
    [ticker],
  );
  const r = rows[0];
  return r
    ? { headline: r.headline, overview: r.overview || "", confidence: r.confidence, bearCase: r.bear_case, keySignals: r.key_signals, crossSourceConnections: r.cross_source_connections, citedItemIds: r.cited_item_ids || [], generatedAt: r.generated_at.toISOString() }
    : null;
}

async function getSources(ticker: string): Promise<SourceItem[]> {
  const rows = await query<{ source_item_id: string; source: string; source_url: string | null; title: string | null; body: string | null; published_at: Date | null; summary: string | null; relevance: number | null; firsthand: boolean | null; firsthand_type: string | null; cited: boolean }>(
    `with latest_summary as (select cited_item_ids from brain_summaries where ticker=$1 order by generated_at desc limit 1) select si.source_item_id,si.source,si.source_url,si.title,si.body,si.published_at,ie.summary,ie.relevance,ie.firsthand,ie.firsthand_type,si.source_item_id = any(coalesce((select cited_item_ids from latest_summary), array[]::text[])) as cited from source_items si left join item_enrichments ie on ie.source_item_id=si.source_item_id where si.ticker=$1 and coalesce(ie.relevance,0) > 0 order by cited desc,ie.relevance desc nulls last,si.published_at desc nulls last limit 150`,
    [ticker],
  );
  return rows.map((r) => ({ id: r.source_item_id, source: r.source, sourceUrl: r.source_url, title: r.title, summary: r.summary || r.title || r.body?.slice(0, 240) || "No summary available yet.", relevance: r.relevance ?? 0, firsthand: Boolean(r.firsthand), firsthandType: r.firsthand_type, publishedAt: r.published_at?.toISOString() || null, cited: r.cited }));
}

async function getConnections(ticker: string): Promise<ConnectionItem[]> {
  const rows = await query<{ connection_id: string; source_a: string; source_b: string; source_a_text: string | null; source_b_text: string | null; confidence: number; narrative: string; stock_relevance: string; connection_type: string }>(
    `select cc.cluster_id as connection_id,array_to_string(cc.sources,' + ') as source_a,'' as source_b,string_agg(coalesce(nullif(ie.summary,''),nullif(left(si.body,220),''),nullif(si.title,'')), E'\\n\\n' order by ie.relevance desc nulls last) filter (where si.source_item_id = any(cc.item_ids[1:3])) as source_a_text,null::text as source_b_text,cc.confidence,cc.narrative,cc.stock_relevance,cc.connection_type from connection_clusters cc left join source_items si on si.source_item_id = any(cc.item_ids) left join item_enrichments ie on ie.source_item_id=si.source_item_id where cc.ticker=$1 and cc.valid=true group by cc.cluster_id,cc.sources,cc.confidence,cc.narrative,cc.stock_relevance,cc.connection_type,cc.verified_at order by cc.confidence desc,cc.verified_at desc limit 24`,
    [ticker],
  );
  return rows.map((r) => ({ id: r.connection_id, sourceA: r.source_a, sourceB: r.source_b, sourceAText: r.source_a_text, sourceBText: r.source_b_text, confidence: Number(r.confidence), narrative: r.narrative, stockRelevance: r.stock_relevance, connectionType: r.connection_type }));
}


