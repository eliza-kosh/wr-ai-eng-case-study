# Alternative Data Brain

An Azure-hosted alternative data pipeline that collects signals from Reddit, Hacker News, and GitHub, enriches them with LLM-based analyst scoring, and synthesises per-ticker intelligence briefs for a portfolio manager dashboard.

See `DESIGN.md` for a full discussion of source selection, architecture decisions, evaluation approach, cost, and what to build next.

---

## Repository Structure

```
dataload/               Azure Function App — source ingestion
  sources/              Per-source fetch + normalise logic (Reddit, HN, GitHub)
  shared/               Base runner, incremental windowing, storage client
  reddit_dataload/      Timer function entry point
  hacker_news_dataload/ Timer function entry point
  github_dataload/      Timer function entry point

processing/             Azure Function App — enrichment, embeddings, synthesis
  shared/               All pipeline logic (see below)
  prepare_processing/   Timer function entry point — enrichment + embedding
  synthesis_processing/ Timer function entry point — connections + summaries + sentiment
  processing_status/    HTTP function — run status endpoint

infra/azure/            PowerShell provisioning and deployment scripts
.github/workflows/      CI/CD pipelines for both Function Apps
tests/                  Unit tests for processing helpers and pipeline logic
```

---

## Dataload

The dataload app is responsible for pulling raw data from external sources and landing it in PostgreSQL.

### Base runner (`dataload/shared/base.py`)

`SourceDataloadRunner` is the base class all three sources extend. It implements **incremental windowing**: on each run it queries `dataload_runs` for the last successful `source_window_end` for that source × ticker partition, sets the new window start to that timestamp minus a 2-hour overlap (to catch late-arriving items), and sets the end to now. On the very first run it falls back to `DATALOAD_INITIAL_LOOKBACK_DAYS` (default 180 days). The run is recorded as `success` or `failed`; only successful runs advance the watermark, so a failed partition safely retries from the same position on the next timer fire.

Source item IDs are derived as `sha256(source_prefix:native_id)[:24]`, making all database upserts idempotent regardless of how many times the same item is fetched across overlapping windows.

### Sources (`dataload/sources/`)

Each source file contains two things: a per-ticker config dict mapping tickers to subreddits/queries/repos, and a `SourceDataloadRunner` subclass implementing `fetch()` and `normalize()`.

- **`reddit.py`** — fetches posts via Arctic Shift's `posts/search` endpoint across each configured subreddit × query combination, then fetches up to 10 comments per post. Normalises posts and comments into the canonical `source_items` shape.
- **`hacker_news.py`** — fetches stories via Algolia's `search_by_date` API (up to 500 results per query) and BFS-traverses each story's comment tree up to a bounded depth. Strips HTML from comment bodies.
- **`github.py`** — fetches repo metadata, issues (`state=all`, filtered by `since=window.start`), and releases for each configured repository. Issue labels and state are preserved in the metadata field for downstream enrichment.

### Function entry points

Each of `reddit_dataload/`, `hacker_news_dataload/`, and `github_dataload/` contains a single `__init__.py` with a timer-triggered `main()` function that calls `Runner().run_all()`. These are the Azure Functions timer triggers — thin wrappers that delegate immediately to the source runner.

---

## Processing

The processing app reads from `source_items` and runs two sequential stages, each triggered by a separate timer.

### Shared models (`processing/shared/models.py`)

Defines the typed dataclasses that flow through the pipeline: `SourceItem`, `EnrichmentResult`, `EmbeddedItem`, `ConnectionClusterCandidate`, and `ConnectionVerification`. These contracts keep the pipeline stages decoupled — each stage reads and writes typed objects rather than raw dicts.

### Config (`processing/shared/config.py`)

`ProcessingConfig` is a frozen dataclass loaded from environment variables. Key settings include model names (`openai_enrichment_model`, `anthropic_summary_model`), thresholds (`similarity_threshold`, `connection_confidence_threshold`), window sizes (`temporal_window_days`), and batch sizes for enrichment and embedding. All defaults are conservative and overridable via env vars.

### LLM client (`processing/shared/openai_client.py`)

`OpenAIProcessorClient` is the single wrapper for all LLM calls. It holds both an OpenAI client (for enrichment and embedding) and an Anthropic client (for connection verification and brain summary generation).

- **`enrich_item()`** — calls GPT-4.1-mini via the OpenAI Responses API with structured JSON output. The system prompt instructs the model to act as a buy-side analyst, scoring each item on relevance (0–10), sentiment direction with rationale, up to four theme tags, a firsthand flag, and a 2–3 sentence analyst summary. Source-specific guidance is injected per item (GitHub bug labels read differently from Reddit migration stories).
- **`embed_texts()`** — batches summaries through `text-embedding-3-small` (1536 dimensions). Only called for items that scored ≥ 2 on relevance.
- **`verify_connection_cluster()`** — sends a semantic neighborhood of source chunks to Claude Opus with hard rejection criteria for duplicate announcements, source inventories, and weak clusters. Valid clusters receive a short `connection_title`, a PM-ready narrative, stock relevance, and supporting item IDs.
- **`generate_summary()`** — sends up to 30 enriched items plus verified connection clusters to Claude Opus. The system prompt instructs Claude to synthesise a PM-facing brief with a trade-thesis headline, a 2–3 paragraph overview, cross-source connection narratives, a bear case, and a confidence rating. A `key_signals` audit trail maps each claim in the overview to the source item ID it was drawn from, enabling LLM-as-judge verification downstream.

### Storage (`processing/shared/storage.py`)

`ProcessingStore` handles all database reads and writes. Key methods:

- **`fetch_unenriched_items()`** / **`fetch_unembedded_items()`** — pull batches of items that haven't been processed yet.
- **`fetch_connection_cluster_candidates()`** — selects up to N high-relevance anchor items per ticker, then for each anchor runs a cosine ANN query to find semantically similar items within the temporal window. Returns `ConnectionClusterCandidate` objects grouping each anchor with its nearest neighbours across sources.
- **`fetch_initial_summary_context()`** — assembles the context block sent to Claude for brain summary generation: top 10 enriched items per source (by relevance, within 90 days) plus up to 25 verified connection clusters.
- **`refresh_sentiment_weekly()`** — runs a single CTE that aggregates weekly sentiment scores per ticker × source, computes an 8-week rolling mean and standard deviation, and sets an alert flag where the z-score exceeds 2.

### Pipeline orchestration (`processing/shared/pipeline.py`)

`ProcessingRunner` orchestrates the two processing stages.

**Prepare stage** (`run_prepare()`): fetches unenriched items in batches of 25, calls `llm.enrich_item()` for each, writes results to `item_enrichments`, then batches the relevant summaries through `llm.embed_texts()` and writes vectors to `item_embeddings`.

**Synthesis stage** (`run_synthesis()`): prunes connections outside the temporal window, runs `verify_connections()` for each ticker (fetching cluster candidates → LLM verification → guardrail check → upsert), then runs `generate_brain_summaries()` for each ticker (fetching context → Claude generation → citation validation → upsert), and finally refreshes the sentiment weekly table.

The guardrail in `_guardrail_cluster_verification()` applies a post-LLM quality check: clusters with fewer than 3 supporting items, no cross-source evidence, or implementation vocabulary in the narrative (`"semantic similarity"`, `"embedding"`, etc.) are rejected regardless of what Claude returned.

### Citation validation (`processing/shared/citations.py`)

`find_invalid_citations()` scans generated text for source item ID patterns (e.g. `reddit:abc123`, `hacker_news:xyz789`) and returns any that weren't in the context supplied to the model. These are stored in `brain_summaries.invalid_citation_ids` as an auditable hallucination signal per run.

### Sentiment helpers (`processing/shared/sentiment.py`)

`rolling_z_score()` is a pure function computing a z-score against a list of historical values, used by the weekly sentiment aggregation to flag abnormal sentiment shifts. Isolated into its own module for testability.

---

## Infrastructure and Deployment

All Azure resources are provisioned via PowerShell scripts in `infra/azure/` using the Azure CLI. The setup creates a Blob Storage account, PostgreSQL Flexible Server (with `pgvector` enabled), Key Vault, Application Insights, and a Consumption Plan Function App. All secrets are stored in Key Vault; the Function Apps access them via managed identity and Key Vault references — no credentials in app settings or source control.

Deployment is handled by GitHub Actions in `.github/workflows/`. Both workflows are path-filtered so a change to `dataload/` doesn't trigger a processing redeploy and vice versa. The dataload workflow authenticates via a publish profile secret; the processing workflow uses OIDC workload identity federation for keyless deployment.

---

## Configuration

Copy `.env.example` to `.env` for local development. Key variables:

| Variable | Purpose |
|---|---|
| `AZURE_POSTGRES_DSN` | PostgreSQL connection string |
| `OPENAI_API_KEY` | Used for enrichment and embedding |
| `ANTHROPIC_API_KEY` | Used for connection verification and brain summaries |
| `GITHUB_TOKEN` | GitHub REST API authentication |
| `DATALOAD_INITIAL_LOOKBACK_DAYS` | How far back to fetch on first run (default 180) |
| `PROCESSING_TEMPORAL_WINDOW_DAYS` | Rolling window for connections and summaries (default 90) |
| `PROCESSING_CONNECTION_CONFIDENCE_THRESHOLD` | Minimum confidence for a connection to surface (default 0.25) |
| `ANTHROPIC_SUMMARY_MODEL` | Claude model for synthesis (default `claude-opus-4-8`) |
