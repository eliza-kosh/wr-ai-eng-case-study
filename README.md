# Whale Rock AI Workbench

Azure-first monorepo for collecting alternative data, preparing it for retrieval, and serving analyst-facing workflows.

## Structure

- `dataload`: one Azure Functions app with timer-triggered loaders for Reddit, Hacker News, and GitHub.
- `notebooks/data_exploration`: source exploration notebooks and local-only notebook outputs.
- `processing`: future batch code for cleaning, embedding, and Pinecone indexing.
- `services`: future API containers for connections and agentic RAG workflows.
- `apps/dashboard`: future analyst dashboard.
- `infra/azure`: PowerShell scripts for provisioning and deploying Azure resources.

## Dataload Flow

Each timer function loads one source across the configured ticker list: `AMD`, `SNDK`, `FROG`, `APP`, and `KVYO`.

The loaders write raw JSON and normalized JSONL artifacts to Azure Blob Storage, then upsert normalized source records into Azure Database for PostgreSQL. Incremental loading is tracked at the source+ticker level with an append-only `dataload_runs` table. The next load window is derived from the latest successful run for that source+ticker, with a small lookback window for late-arriving records.

## Azure Setup

The setup script expects the resource group to already exist:

```powershell
$password = Read-Host "Postgres admin password" -AsSecureString
$github = Read-Host "GitHub token" -AsSecureString
.\infra\azure\setup-dataload.ps1 `
  -Project "eliza-ai-workbench" `
  -Location "eastus" `
  -PostgresLocation "centralus" `
  -ResourceGroup "eliza-ai-workbench-rg" `
  -PostgresAdminPassword $password `
  -GitHubToken $github
```

The script creates Blob Storage, PostgreSQL Flexible Server, Key Vault, Application Insights, and a Linux Consumption Azure Function App. Secrets are stored in Key Vault, and the Function App uses Key Vault references in app settings.

The first successful load for each source+ticker uses `DATALOAD_INITIAL_LOOKBACK_DAYS=180`, then later runs increment from the latest successful source+ticker run with a small overlap window.

Deploy the dataload Function App after setup:

```powershell
$env:Path = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:Path"
.\infra\azure\deploy-dataload.ps1 `
  -ResourceGroup "eliza-ai-workbench-rg" `
  -FunctionApp "<function-app-name-from-setup-output>"
```

Local-only values can be copied from `.env.example` into `.env`. Do not commit real credentials.

## GitHub Deployment

`.github/workflows/deploy-dataload.yml` deploys the `dataload` Azure Function App from `main` using GitHub Actions OIDC. Add these GitHub repository secrets after creating an Azure app registration or managed identity with deploy permissions:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

The helper script creates a service principal scoped to the Function App and adds a federated credential for this repository:

```powershell
$env:Path = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:Path"
.\infra\azure\setup-github-oidc.ps1
```
