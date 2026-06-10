# Whale Rock AI Workbench

Azure-first monorepo for collecting alternative data, preparing it for retrieval, and serving analyst-facing workflows.

## Structure

- `dataload`: one Azure Functions app with timer-triggered loaders for Reddit, Hacker News, and GitHub.
- `notebooks/data_exploration`: source exploration notebooks and local-only notebook outputs.
- `processing`: Azure Functions app for enrichment, pgvector embeddings, cross-source connections, brain summaries, and weekly sentiment aggregation.
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


## Processing Flow

The processing Function App runs after dataload and reads normalized rows from `source_items`. It has two timer-triggered jobs: `prepare_processing` enriches Reddit, Hacker News, and GitHub items with OpenAI structured outputs and stores relevant summary embeddings in Postgres/pgvector; `synthesis_processing` verifies cross-source connections, generates ticker-level brain summaries with citation checks, and refreshes weekly sentiment aggregates for the dashboard.

Default cadence is daily at `PROCESSING_PREPARE_SCHEDULE=0 30 1 * * *` and `PROCESSING_SYNTHESIS_SCHEDULE=0 30 2 * * *`. Required runtime secrets are `AZURE_POSTGRES_DSN`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY`; model names and thresholds are config-driven in `.env.example`. Connection verification and brain summaries default to Claude Opus once daily in `synthesis_processing`.

Deploy the processing Function App after setup:

```powershell
$openai = Read-Host "OpenAI API key" -AsSecureString
$anthropic = Read-Host "Anthropic API key" -AsSecureString
.\infra\azure\setup-processing.ps1 `
  -Project "eliza-ai-workbench" `
  -ResourceGroup "eliza-ai-workbench-rg" `
  -StorageAccount "<storage-account-from-dataload-setup>" `
  -KeyVault "<key-vault-from-dataload-setup>" `
  -OpenAIApiKey $openai `
  -AnthropicApiKey $anthropic

.\infra\azure\deploy-processing.ps1 `
  -ResourceGroup "eliza-ai-workbench-rg" `
  -FunctionApp "<function-app-name-from-setup-output>"
```

## GitHub Deployment

`.github/workflows/deploy-dataload.yml` deploys the `dataload` Azure Function App from `main`, and `.github/workflows/deploy-processing.yml` deploys `processing`. Add these GitHub repository secrets:

- `AZURE_FUNCTIONAPP_PUBLISH_PROFILE`
- `AZURE_PROCESSING_FUNCTIONAPP_PUBLISH_PROFILE`

You can download the publish profile from the Azure Portal Function App overview page, or with Azure CLI:

```powershell
$env:Path = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:Path"
az functionapp deployment list-publishing-profiles `
  --resource-group "eliza-ai-workbench-rg" `
  --name "eliza-ai-workbench-dataload-func-58415" `
  --xml
```

Copy the full XML output into the GitHub secret value.

The OIDC helper script is kept for future RBAC-based deployment work:

```powershell
$env:Path = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:Path"
.\infra\azure\setup-github-oidc.ps1
```
