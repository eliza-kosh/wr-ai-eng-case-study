# Alternative Data Brain — Design Document

---

## 1. Source Selection

**Reddit** (Arctic Shift) and **Hacker News** (Algolia) provide unsolicited firsthand developer and operator opinion without a paid contract; migration stories, pricing complaints, and the technical founders who make or influence buy decisions. 

**GitHub** (REST API) adds a structural layer neither community source can: issue velocity, bug severity, and release cadence as a live audit trail of product health. 

**Glassdoor** via Coresignal was planned as a fourth source for employee morale and sales org health; the API was down the entire development window and came back up towards the end of the deadline. 

All three live sources share a demographic blind spot: Reddit skews younger and male, HN skews toward SF-area technical founders, and GitHub captures only active contributors. This pipeline reflects what developers think — not enterprise buyers, CFOs, or end users — which limits applicability for tickers where purchase decisions don't live with technical teams.

**Rejected:** X/Twitter (high noise-to-signal ratio that would require significant filtering work to be useful); earnings transcripts and SEC filings (covered by existing quant feeds); job postings (meaningful only in longitudinal aggregate, not at the item level this pipeline operates on).

---

## 2. Architecture

### Azure resource topology

- **Two Azure Function Apps** (Consumption Plan, Python 3.11) — one for dataload, one for processing. A failure in synthesis never blocks data collection; the two apps deploy, scale, and bill independently.
- **Azure PostgreSQL Flexible Server** with the `pgvector` extension for ANN search. All persistent application state lives here — source items, enrichments, embeddings, connections, summaries, and sentiment aggregates.
- **Azure Blob Storage** — raw source API responses archived before any database write, providing a full reprocessing trail independent of the database.
- **Azure Key Vault** — all secrets accessed by the Function Apps via managed identity. No credentials in app settings or source control.
- **Application Insights** — one workspace per Function App for telemetry and alerting.
- **Azure Container Apps** — the dashboard is containerised, pushed to Azure Container Registry on each merge to main, and deployed as a Container App with external ingress. A separate path-filtered GitHub Actions workflow handles this independently of the Function App deployments.

Everything is provisioned via PowerShell scripts against the Azure CLI and deployed through GitHub Actions CI/CD.

All three dataload functions fire at 10:00 UTC daily; Prepare fires at 11:00 and Synthesis at 11:30, giving dataload a 30-minute head start. Once-a-day is sufficient because Reddit and HN discussions are effectively complete within hours of posting, GitHub moves even slower, and the investment signal is durable enough that a 24-hour lag has no material impact on relevance. 

### Data flow

```
External APIs
    │  Reddit (Arctic Shift), HN (Algolia), GitHub REST
    ▼
Dataload Function App  ──raw JSON──▶  Blob Storage (source-data container)
    │  fetch → normalize → upsert
    ▼
PostgreSQL: source_items
    │
    ▼  (11:00 UTC)
Processing Function App — Prepare stage
    │  GPT-4.1-mini enrichment (batches of 25)
    ▼
PostgreSQL: item_enrichments
    │  text-embedding-3-small (batches of 100, relevance ≥ 2 only)
    ▼
PostgreSQL: item_embeddings  [IVFFlat ANN index]
    │
    ▼  (11:30 UTC)
Processing Function App — Synthesis stage
    │  semantic clustering → Claude connection verification
    ▼
PostgreSQL: connection_clusters
    │  Claude brain summary generation (per ticker)
    ▼
PostgreSQL: brain_summaries
    │  weekly sentiment CTE refresh
    ▼
PostgreSQL: sentiment_weekly
    │
    ▼
Dashboard  ──reads──▶  brain_summaries, connection_clusters, sentiment_weekly
```

### Enrichment

Before any vector search or synthesis, every source item passes through GPT-4.1-mini enrichment. The model scores each item on relevance (0–10), extracts sentiment direction (bullish/bearish/neutral) with a rationale, assigns up to four theme tags, flags whether the item contains firsthand experience (a developer describing their own migration, a customer describing their own support ticket), and writes a 2–3 sentence analyst summary. Items scoring below 2 on relevance are dropped and never embedded.

The enrichment step is effectively the quality ceiling for everything downstream: a poorly summarised item produces a weak embedding, a weak cluster, and weak evidence in the overview.

### Connection discovery

For each ticker, high-relevance items are selected as anchors and pgvector's cosine index finds their nearest neighbours within a 90-day rolling window. Each anchor plus its neighbours is sent to Claude as a single cluster — one LLM call per cluster rather than one per pair.

Claude's task is not to confirm similarity (the vector index already did that) but to judge whether the cluster reveals something about the company that none of the individual items reveals alone. Claude rejects:

- **Same-event pairs** — two sources covering the same release or incident with no independent reaction
- **Announcement-only items** — changelogs or press releases with no firsthand response
- **Redundant corroboration** — where the synthesis reduces to "multiple sources reported the same thing"

Valid connections are written as short analyst prose naming what the cross-source pattern implies for the stock. The output records `connection_title`, `narrative`, `stock_relevance`, `connection_type`, `confidence`, and `supporting_item_ids`.

A deterministic guardrail then runs before anything is stored: single-source clusters are rejected unless they have at least 5 firsthand items; clusters with fewer than 3 supporting items total are rejected; and any narrative containing pipeline vocabulary (`"semantic similarity"`, `"embedding"`) is rejected — that's a sign the model described the retrieval process rather than the business signal.

### Brain summary

Up to 30 enriched items (10 per source, by relevance, within a 90-day lookback) plus up to 25 verified connection clusters are sent to Claude in a single context window. Agentic RAG was explored early on but turned out to be overengineering at current data volumes — everything fits comfortably in one prompt. Worth revisiting as data grows.

The output schema: `headline` (trade idea, not a topic label), `overview` (analyst synthesis across all sources, not a source-by-source recap), `cross_source_connections` (the verified connection narratives), `bear_case` (required even in bullish summaries — what the short thesis would argue), `confidence` (high/medium/low), `key_signals` (each claim in the overview mapped to the source item it came from), and `cited_item_ids` (full citation list for validation).

---

## 3. Evaluation

**Citation accuracy** *(live)* — every Brain Summary records `invalid_citation_ids`: source item IDs that appear in the generated text but were not in the context provided to the model. An empty set means every cited source was real. This runs automatically on every synthesis run and is stored per summary.

**Connection quality guardrail** *(live)* — a deterministic post-LLM check runs before any connection is persisted: fewer than 3 supporting items, single-source clusters with under 5 firsthand items, and pipeline vocabulary in the narrative all trigger automatic rejection regardless of model confidence.

**LLM-as-judge** *(not built)* — each Brain Summary includes a `key_signals` field mapping every claim in the overview to the source item it was drawn from. This was designed as the input to a secondary model pass that would score each claim against its source, but that pass was never implemented. The data structure is in place to enable it (see Section 5).

---

## 4. Cost and Latency

| Step | Model | Est. cost / ticker / run |
|---|---|---|
| Enrichment | GPT-4.1-mini | ~$0.03 |
| Embedding | text-embedding-3-small | < $0.01 |
| Connection verification | Claude Opus 4.8 | ~$0.15 |
| Brain summary | Claude Opus 4.8 | ~$0.20 |
| **Total** | | **~$0.40** |

**Infrastructure cost** is minimal at this scale — Consumption Plan Function Apps (pay-per-execution) and a Postgres instance put fixed Azure spend well under $20/month.

**Data cost:** Reddit (Arctic Shift), HN (Algolia), and GitHub are all free. Coresignal, the planned Glassdoor provider, has a base subscription of $50/month — an account was registered and the integration scaffolded, but the API outage described in Section 1 meant it never contributed data. That $50/month would be the largest fixed data cost in a working deployment, exceeding Azure infrastructure spend entirely.

**Development tooling:** The project was developed using OpenAI Codex CLI rather than Claude Code (for cost saving reasons).

---

## 5. Limitations and What to Build Next

**Source coverage** is the biggest gap. Three developer-skewed sources don't cover all five tickers well. Immediate additions: Glassdoor (integration scaffolded, blocked by API outage), industry news and podcasts. Data licensing will need attention at commercial scale — Arctic Shift's Reddit data is currently used under a research-use interpretation.

**Incomplete features:**
- *Sentiment trend analysis* — a weekly rolling sentiment score per ticker × source with z-score alerting was started but deprioritised due to insufficient confidence in signal quality at current data volumes. The infrastructure is in place; worth revisiting once source coverage improves.
- *Conversational interface* — a chatbot layer on top of the Brain Summary and source items would let analysts ask follow-up questions directly against the underlying evidence rather than reading static summaries.

**Hallucination hardening:** Citation validity is checked automatically. Claim-level accuracy is not — the `key_signals` field is designed to feed an LLM-as-judge pass that scores each claim against its source, but that pass hasn't been built yet.

**R&D:** Which sources actually move prices, with what lag, is uninvestigated. GraphRAG is worth evaluating against the current anchor-clustering approach at higher data volumes. Agentic RAG for the brain summary becomes relevant once context no longer fits in a single prompt.

**Scaling:** Adding tickers is a config change — the architecture is embarrassingly parallel at the ticker level. The main bottlenecks at scale:
- Claude Opus cost (~$80/run at 200 tickers) — addressable by routing lower-stakes steps to Haiku
- IVFFlat recall degradation as the embedding table grows — switch to HNSW
- Enrichment throughput on cold-start backfills — OpenAI Batch API cuts cost 50% and removes rate-limit pressure
