"use client";

import { useMemo, useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ExternalLink } from "lucide-react";
import type { ConnectionItem, DashboardData, SentimentPoint, SourceItem } from "@/lib/db";

const colors: Record<string, string> = {
  reddit: "#cf735d",
  hacker_news: "#b88930",
  github: "#4d849e",
  glassdoor: "#8772a5",
};

export default function Dashboard({ data }: { data: DashboardData }) {
  const [view, setView] = useState<"connections" | "sentiment">("connections");
  const [sourceFilter, setSourceFilter] = useState("all");
  const visibleSources = useMemo(() => dedupeSources(data.sources), [data.sources]);
  const sourceNames = useMemo(() => Array.from(new Set(visibleSources.map((i) => i.source))).sort(), [visibleSources]);
  const filtered = sourceFilter === "all" ? visibleSources : visibleSources.filter((i) => i.source === sourceFilter);
  const chartRows = useMemo(() => toChartRows(data.sentiment), [data.sentiment]);
  const sentimentSources = useMemo(() => Array.from(new Set(data.sentiment.map((p) => p.source))).sort(), [data.sentiment]);

  return (
    <div className="pageShell">
      <header className="siteHeader">
        <div>
          <h1>Whale Rock Signal Research</h1>
          <p>Overview, source evidence, cross-source connections, and weekly sentiment from Postgres.</p>
        </div>
        <nav className="tickerNav" aria-label="Ticker">
          {data.tickers.map((t) => (
            <a className={t === data.ticker ? "ticker active" : "ticker"} href={`/?ticker=${t}`} key={t}>
              {t}
            </a>
          ))}
        </nav>
      </header>

      {data.error ? (
        <section className="notice">
          <strong>Postgres connection issue</strong>
          <p>{data.error}</p>
        </section>
      ) : null}

      <main className="mainStack">
        <Overview data={data} sources={visibleSources} />

        <div className="workspace">
          <section className="primaryStack">
            <section id="sources" className="card">
              <div className="sectionTitleRow">
                <div className="sectionHeading">
                  <p>Sources</p>
                  <h2>Ranked source feed</h2>
                </div>
                <div className="filterPills">
                  <button className={sourceFilter === "all" ? "pill active" : "pill"} onClick={() => setSourceFilter("all")}>
                    All {visibleSources.length}
                  </button>
                  {sourceNames.map((s) => (
                    <button className={sourceFilter === s ? "pill active" : "pill"} key={s} onClick={() => setSourceFilter(s)}>
                      {label(s)}
                    </button>
                  ))}
                </div>
              </div>
              <SourceFeed items={filtered} />
            </section>

            <section className="card">
              <div className="sectionTitleRow">
                <div className="sectionHeading">
                  <p>{view === "connections" ? "Connections" : "Sentiment"}</p>
                  <h2>{view === "connections" ? "What is actually happening" : "Sentiment over time"}</h2>
                </div>
                <div className="segmented">
                  <button className={view === "connections" ? "selected" : ""} onClick={() => setView("connections")}>
                    Connections
                  </button>
                  <button className={view === "sentiment" ? "selected" : ""} onClick={() => setView("sentiment")}>
                    Sentiment
                  </button>
                </div>
              </div>
              {view === "connections" ? <Connections items={data.connections} sources={visibleSources} /> : <Sentiment rows={chartRows} sources={sentimentSources} />}
            </section>
          </section>
        </div>
      </main>
    </div>
  );
}

function Overview({ data, sources }: { data: DashboardData; sources: SourceItem[] }) {
  const rawSignals = normalize(data.summary?.keySignals).filter(Boolean).slice(0, 2);
  const citationIds = unique(rawSignals.flatMap(extractCitationIds));
  const citationNumbers = new Map(citationIds.map((id, index) => [id, index + 1]));
  const sourceById = new Map(sources.map((source) => [source.id, source]));

  return (
    <section className="card overviewCard">
      <div className="overviewHeader">
        <div className="sectionHeading">
          <p>{data.ticker}</p>
          <h2>{data.summary?.headline || `${data.ticker} source intelligence`}</h2>
        </div>
      </div>
      {data.summary ? (
        <div className="overviewReadout">
          {rawSignals.length ? (
            rawSignals.map((signal, index) => {
              const ids = extractCitationIds(signal);
              return (
                <p key={index}>
                  {cleanSignal(signal)}
                  <CitationGroup ids={ids} citationNumbers={citationNumbers} sourceById={sourceById} />
                </p>
              );
            })
          ) : (
            <p>Summary synthesis is available, but no key signal notes were stored.</p>
          )}
        </div>
      ) : (
        <div className="emptyState">
          <strong>No brain summary yet.</strong>
          <p>Run synthesis_processing after enrichment to populate brain_summaries.</p>
        </div>
      )}
    </section>
  );
}

function CitationGroup({ ids, citationNumbers, sourceById }: { ids: string[]; citationNumbers: Map<string, number>; sourceById: Map<string, SourceItem> }) {
  const refs = unique(ids);
  if (!refs.length) return null;
  return (
    <sup className="citationGroup">
      {refs.map((id) => {
        const number = citationNumbers.get(id);
        const source = sourceById.get(id);
        if (!number) return null;
        return source ? (
          <a key={id} href={`#${sourceAnchorId(id)}`} title={source.title || id}>
            [{number}]
          </a>
        ) : (
          <a key={id} href="#sources" title={id}>
            [{number}]
          </a>
        );
      })}
    </sup>
  );
}

function SourceFeed({ items }: { items: SourceItem[] }) {
  if (!items.length) return <div className="emptyState">No source rows found for this ticker.</div>;
  return (
    <div className="sourceFeed">
      {items.map((item) => (
        <article id={sourceAnchorId(item.id)} className={item.cited ? "sourceRow cited" : "sourceRow"} key={item.id}>
          <div className="sourceBadge" style={{ color: color(item.source) }}>
            <span>{label(item.source)}</span>
            <small>{formatDate(item.publishedAt)}</small>
          </div>
          <div className="sourceCopy">
            <strong>{item.title || `${label(item.source)} source item`}</strong>
            <p>{item.summary}</p>
            <div className="tagLine">
              <span className={`sentiment ${item.sentiment}`}>{item.sentiment}</span>
              <span>relevance {item.relevance}</span>
              {item.firsthand ? <span>firsthand {item.firsthandType || ""}</span> : null}
              {item.cited ? <span>cited</span> : null}
            </div>
          </div>
          {item.sourceUrl ? (
            <a className="external" href={item.sourceUrl} target="_blank" aria-label="Open source">
              <ExternalLink size={16} />
            </a>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function Connections({ items, sources }: { items: ConnectionItem[]; sources: SourceItem[] }) {
  const topItems = dedupeConnections(items).slice(0, 5);
  const sourceById = new Map(sources.map((source) => [source.id, source]));
  if (!topItems.length) return <div className="emptyState">No verified connections found for this ticker yet.</div>;
  return (
    <div className="connectionList">
      {topItems.map((item, index) => {
        const headline = connectionRead(item);
        const support = connectionSupport(item, headline);
        const citationIds = connectionCitationIds(item, sources);
        const citationNumbers = new Map(citationIds.map((id, citationIndex) => [id, citationIndex + 1]));
        return (
          <article className="connectionCard" key={item.id}>
            <div className="connectionRank">#{index + 1}</div>
            <div className="connectionBody">
              <div className="connectionMeta">
                <span>{Math.round(item.confidence * 100)}% confidence</span>
                <span>{label(item.connectionType)}</span>
              </div>
              <h3>{headline}</h3>
              {support ? <p>{support}</p> : null}
              <small>
                Evidence: {label(item.sourceA)} and {label(item.sourceB)}
                <CitationGroup ids={citationIds} citationNumbers={citationNumbers} sourceById={sourceById} />
              </small>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function connectionCitationIds(item: ConnectionItem, sources: SourceItem[]) {
  return unique([resolveSourceRef(item.sourceA, sources), resolveSourceRef(item.sourceB, sources)]);
}

function resolveSourceRef(value: string, sources: SourceItem[]) {
  if (sources.some((source) => source.id === value)) return value;
  const normalized = value.toLowerCase();
  return sources.find((source) => source.source.toLowerCase() === normalized || label(source.source).toLowerCase() === label(value).toLowerCase())?.id || value;
}
function connectionRead(item: ConnectionItem) {
  const sourceALabel = label(item.sourceA);
  const sourceBLabel = label(item.sourceB);
  const a = summarizeConnectionText(item.sourceAText, sourceALabel);
  const b = summarizeConnectionText(item.sourceBText, sourceBLabel);
  if (a && b) return `${sourceALabel}: ${a} / ${sourceBLabel}: ${b}`;
  if (a || b) return `${a || b}`;
  return cleanSignal(item.narrative) || item.stockRelevance;
}

function connectionSupport(item: ConnectionItem, headline: string) {
  const narrative = cleanSignal(item.narrative);
  if (narrative && !sameText(narrative, headline) && !normalizeForDedupe(headline).includes(normalizeForDedupe(narrative))) return narrative;
  const relevance = cleanSignal(item.stockRelevance);
  if (relevance && !sameText(relevance, headline) && !sameText(relevance, narrative)) return relevance;
  return "";
}

function summarizeConnectionText(value: string | null, sourceLabel: string) {
  if (!value) return "";
  const cleaned = cleanSignal(value).replace(/\s+/g, " ").trim();
  if (!cleaned || cleaned.toLowerCase() === sourceLabel.toLowerCase()) return "";
  return cleaned.length > 150 ? `${cleaned.slice(0, 147).trim()}...` : cleaned;
}

function Sentiment({ rows, sources }: { rows: Record<string, string | number>[]; sources: string[] }) {
  if (!rows.length) return <div className="emptyState">No weekly sentiment rows found for this ticker yet.</div>;
  return (
    <div className="chartPanel">
      <ResponsiveContainer width="100%" height={360}>
        <ComposedChart data={rows} margin={{ top: 16, right: 12, left: -16, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis dataKey="week" stroke="var(--muted)" tickLine={false} fontSize={12} />
          <YAxis yAxisId="left" domain={[-1, 1]} stroke="var(--muted)" tickLine={false} fontSize={12} />
          <YAxis yAxisId="right" orientation="right" stroke="var(--muted)" tickLine={false} fontSize={12} />
          <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text)" }} />
          <Legend />
          <Bar yAxisId="right" dataKey="volume" fill="var(--input)" radius={[4, 4, 0, 0]} />
          {sources.map((s) => (
            <Line yAxisId="left" key={s} type="monotone" dataKey={s} stroke={color(s)} strokeWidth={2} dot={false} />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function toChartRows(points: SentimentPoint[]) {
  const byWeek = new Map<string, Record<string, string | number>>();
  points.forEach((p) => {
    const row = byWeek.get(p.weekStart) || { week: p.weekStart.slice(5), volume: 0 };
    row[p.source] = p.sentimentAvg;
    row.volume = Number(row.volume) + p.itemCount;
    byWeek.set(p.weekStart, row);
  });
  return Array.from(byWeek.values());
}

function label(source: string) {
  return source.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
}

function color(source: string) {
  return colors[source] || "#256f79";
}

function formatDate(value: string | null) {
  return value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric" }).format(new Date(value)) : "pending";
}

function normalize(value: unknown) {
  if (Array.isArray(value)) return value.map(stringify);
  if (value && typeof value === "object") return Object.values(value).map(stringify);
  if (typeof value === "string") return [value];
  return [];
}

function stringify(value: unknown) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return String(record.signal || record.summary || record.narrative || record.text || JSON.stringify(record));
  }
  return String(value);
}

function cleanSignal(value: string) {
  return value
    .replace(/^\s*(?:[a-z_]+:[0-9a-f]{8,}\s*(?:\+\s*)?)+:\s*/i, "")
    .replace(/\b[a-z_]+:[0-9a-f]{8,}\s+reports\s+[a-z_]+\s+sentiment\s+is\s+(bullish|bearish|neutral)\.\s*/gi, "")
    .replace(/\([\s,]*[a-z_]+:[0-9a-f]{8,}(?:[\s,]+[a-z_]+:[0-9a-f]{8,})*[\s,]*\)/gi, "")
    .replace(/\b[a-z_]+:[0-9a-f]{8,}\b\s*/gi, "")
    .replace(/^\s*(?:\+|:)\s*/, "")
    .replace(/\s+/g, " ")
    .replace(/\s+([,.;:])/g, "$1")
    .trim();
}

function extractCitationIds(value: string) {
  return unique(Array.from(value.matchAll(/\b[a-z_]+:[0-9a-f]{8,}\b/g)).map((match) => match[0]));
}

function sourceAnchorId(id: string) {
  return `source-${id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}
function dedupeSources(items: SourceItem[]) {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = normalizeForDedupe(`${item.source}:${item.title || item.summary}`);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function dedupeConnections(items: ConnectionItem[]) {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = normalizeForDedupe(connectionRead(item));
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function unique<T>(items: T[]) {
  return Array.from(new Set(items));
}

function normalizeForDedupe(value: string) {
  return cleanSignal(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function sameText(a: string, b: string) {
  return normalizeForDedupe(a) === normalizeForDedupe(b);
}

