"use client";

import { useMemo, useState, type ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import type { ConnectionItem, DashboardData, SourceItem } from "@/lib/db";

const colors: Record<string, string> = {
  reddit: "#cf735d",
  hacker_news: "#b88930",
  github: "#4d849e",
  glassdoor: "#8772a5",
};

export default function Dashboard({ data }: { data: DashboardData }) {
  const [sourceFilter, setSourceFilter] = useState("all");
  const visibleSources = useMemo(() => dedupeSources(data.sources), [data.sources]);
  const sourceNames = useMemo(() => Array.from(new Set(visibleSources.map((i) => i.source))).sort(), [visibleSources]);
  const filtered = sourceFilter === "all" ? visibleSources : visibleSources.filter((i) => i.source === sourceFilter);
  const topConnections = useMemo(() => dedupeConnections(data.connections).slice(0, 5), [data.connections]);
  const sourceById = useMemo(() => new Map(visibleSources.map((source) => [source.id, source])), [visibleSources]);
  const citationNumbers = useMemo(() => {
    const overviewIds = splitParagraphs(data.summary?.overview || "").flatMap(extractCitationIds);
    const connectionIds = topConnections.flatMap((item) => connectionCitationIds(item, visibleSources));
    return new Map(unique([...overviewIds, ...connectionIds]).map((id, index) => [id, index + 1]));
  }, [data.summary?.overview, topConnections, visibleSources]);

  return (
    <div className="pageShell">
      <header className="siteHeader">
        <div>
          <h1>Whale Rock Signal Research</h1>
          <p>Overview, source evidence, and cross-source connections from Postgres.</p>
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
        <Overview data={data} citationNumbers={citationNumbers} sourceById={sourceById} />

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
              <SourceFeed items={filtered} citationNumbers={citationNumbers} />
            </section>

            <section className="card">
              <div className="sectionTitleRow">
                <div className="sectionHeading">
                  <p>Connections</p>
                  <h2>What is actually happening</h2>
                </div>
              </div>
              <Connections items={topConnections} sources={visibleSources} citationNumbers={citationNumbers} sourceById={sourceById} />
            </section>
          </section>
        </div>
      </main>
    </div>
  );
}

function Overview({ data, citationNumbers, sourceById }: { data: DashboardData; citationNumbers: Map<string, number>; sourceById: Map<string, SourceItem> }) {
  const overviewParagraphs = splitParagraphs(data.summary?.overview || "");

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
          {overviewParagraphs.length ? (
            overviewParagraphs.map((paragraph, index) => {
              return (
                <p key={index}>
                  {renderInlineCitations(paragraph, citationNumbers, sourceById)}
                </p>
              );
            })
          ) : (
            <p>Summary synthesis is available, but no overview was stored.</p>
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

function renderInlineCitations(value: string, citationNumbers: Map<string, number>, sourceById: Map<string, SourceItem>) {
  const parts: ReactNode[] = [];
  const idPattern = /\b[a-z_]+:[0-9a-f]{8,}\b/g;
  const pattern = /\((?:\s*\b[a-z_]+:[0-9a-f]{8,}\b\s*,?)+\)|\b[a-z_]+:[0-9a-f]{8,}\b/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value)) !== null) {
    if (match.index > lastIndex) parts.push(value.slice(lastIndex, match.index));
    const ids = unique(Array.from(match[0].matchAll(idPattern)).map((idMatch) => idMatch[0]));
    ids.forEach((id, index) => {
      if (index > 0) parts.push(" ");
      parts.push(renderCitation(id, match!.index + index, citationNumbers, sourceById));
    });
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < value.length) parts.push(value.slice(lastIndex));
  return parts.length ? parts : cleanSignal(value);
}

function renderCitation(id: string, key: number, citationNumbers: Map<string, number>, sourceById: Map<string, SourceItem>) {
  const number = citationNumbers.get(id);
  const source = sourceById.get(id);
  if (!number) return null;
  return (
    <sup className="citationGroup" key={`${id}-${key}`}>
      {source ? (
        <a href={`#${sourceAnchorId(id)}`} title={source.title || id}>
          [{number}]
        </a>
      ) : (
        <a href="#sources" title={id}>
          [{number}]
        </a>
      )}
    </sup>
  );
}

function SourceFeed({ items, citationNumbers }: { items: SourceItem[]; citationNumbers: Map<string, number> }) {
  if (!items.length) return <div className="emptyState">No source rows found for this ticker.</div>;
  return (
    <div className="sourceFeed">
      {items.map((item) => {
        const citationNumber = citationNumbers.get(item.id);
        return (
          <article id={sourceAnchorId(item.id)} className={item.cited || citationNumber ? "sourceRow cited" : "sourceRow"} key={item.id}>
            <div className="sourceBadge" style={{ color: color(item.source) }}>
              {citationNumber ? <span className="sourceRefBadge" style={{ display: "inline-flex", alignItems: "center", minHeight: 24, marginBottom: 7, padding: "0 9px", borderRadius: 999, background: "var(--primary)", color: "#fff", fontSize: 11, fontWeight: 900 }}>Excerpt [{citationNumber}]</span> : null}
              <span>{label(item.source)}</span>
              <small>{formatDate(item.publishedAt)}</small>
            </div>
            <div className="sourceCopy">
              <strong>{item.title || `${label(item.source)} source item`}</strong>
              <p>{item.summary}</p>
              <div className="tagLine">
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
        );
      })}
    </div>
  );
}

function Connections({ items, sources, citationNumbers, sourceById }: { items: ConnectionItem[]; sources: SourceItem[]; citationNumbers: Map<string, number>; sourceById: Map<string, SourceItem> }) {
  if (!items.length) return <div className="emptyState">No verified connections found for this ticker yet.</div>;
  return (
    <div className="connectionList">
      {items.map((item, index) => {
        const headline = connectionRead(item);
        const support = connectionSupport(item, headline);
        const citationIds = connectionCitationIds(item, sources);
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
  const narrative = cleanSignal(item.narrative);
  const title = firstSentence(narrative);
  return title || cleanSignal(item.stockRelevance);
}

function connectionSupport(item: ConnectionItem, headline: string) {
  const narrative = cleanSignal(item.narrative);
  const body = narrativeRemainder(narrative);
  if (body && !sameText(body, headline)) return body;
  if (narrative && !sameText(narrative, headline) && !normalizeForDedupe(headline).includes(normalizeForDedupe(narrative))) return narrative;
  const relevance = cleanSignal(item.stockRelevance);
  if (relevance && !sameText(relevance, headline) && !sameText(relevance, narrative)) return relevance;
  return "";
}

function firstSentence(value: string) {
  const match = value.match(/^(.+?[.!?])(\s|$)/);
  return match ? match[1].trim() : value.trim();
}

function narrativeRemainder(value: string) {
  const title = firstSentence(value);
  return value.slice(title.length).trim();
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

function splitParagraphs(value: string) {
  return value
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean);
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

