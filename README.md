# Whale Rock Brain

Azure-first monorepo for the Whale Rock AI Engineer case study. The goal is an analyst dashboard that turns non-traditional source data into ticker-level summaries, source feeds, and cited cross-source connections.

## Repository Shape

- `apps/dashboard`: analyst-facing dashboard container. It reads prepared data and should not run ingestion in the request path.
- `apps/ingest_worker`: scheduled ingestion worker container. It fetches source data, processes it, and writes durable outputs.
- `src/whalerock_brain`: shared Python package used by both deployables.
- `infra/azure`: Azure deployment placeholders for dashboard, worker, storage, database, secrets, and monitoring.
- `docs`: design-document sections for source selection, architecture, evaluation, cost, latency, and demo flow.
- `data`: local-only snapshots, fixtures, and development data.

## Intended Azure Architecture

Azure Container Apps hosts the dashboard. Azure Container Apps Jobs or Azure Functions run scheduled ingestion. Azure Blob Storage stores raw and processed artifacts, Azure Database for PostgreSQL stores structured metadata and optionally vectors, Azure Key Vault stores secrets, and Azure Monitor/Application Insights captures logs.

## Local Development

Implementation is intentionally not filled in yet. The first build should add shared models, storage adapters, source connectors, and a minimal dashboard data service before adding real source APIs.
