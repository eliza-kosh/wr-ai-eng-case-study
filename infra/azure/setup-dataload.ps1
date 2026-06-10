param(
    [string]$Project = "eliza-ai-workbench",
    [string]$Location = "eastus",
    [string]$PostgresLocation = "centralus",
    [string]$ResourceGroup = "$Project-rg",
    [string]$PostgresAdminUser = "wradmin",
    [Parameter(Mandatory = $true)]
    [SecureString]$PostgresAdminPassword,
    [SecureString]$GitHubToken
)

$ErrorActionPreference = "Stop"

function az {
    & az.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed. See the Azure CLI output above for details."
    }
}

function Convert-SecretToPlainText {
    param([SecureString]$Secret)
    if (-not $Secret) {
        return ""
    }

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$suffix = (Get-Random -Minimum 10000 -Maximum 99999).ToString()
$nameStem = ($Project -replace '[^a-zA-Z0-9]', '').ToLowerInvariant()
$shortStem = $nameStem.Substring(0, [Math]::Min(14, $nameStem.Length))

$storageAccount = "$($nameStem.Substring(0, [Math]::Min(16, $nameStem.Length)))dl$suffix"
$functionApp = "$Project-dataload-func-$suffix"
$appInsights = "$Project-dataload-ai-$suffix"
$keyVault = "$shortStem-kv-$suffix"
$postgresServer = "$Project-pg-$suffix"
$postgresDb = "whalerock"
$blobContainer = "source-data"

$postgresPassword = Convert-SecretToPlainText $PostgresAdminPassword
$githubTokenPlainText = Convert-SecretToPlainText $GitHubToken

az config set extension.use_dynamic_install=yes_without_prompt
az group show --name $ResourceGroup 1>$null

az storage account create `
    --name $storageAccount `
    --resource-group $ResourceGroup `
    --location $Location `
    --sku Standard_LRS `
    --kind StorageV2 `
    --min-tls-version TLS1_2 `
    --allow-blob-public-access false `
    --https-only true

$storageConnectionString = az storage account show-connection-string `
    --name $storageAccount `
    --resource-group $ResourceGroup `
    --query connectionString `
    --output tsv

az storage container create `
    --name $blobContainer `
    --connection-string $storageConnectionString `
    --public-access off 1>$null

az postgres flexible-server create `
    --name $postgresServer `
    --resource-group $ResourceGroup `
    --location $PostgresLocation `
    --admin-user $PostgresAdminUser `
    --admin-password $postgresPassword `
    --sku-name Standard_B1ms `
    --tier Burstable `
    --storage-size 32 `
    --version 16 `
    --public-access 0.0.0.0

az postgres flexible-server db create `
    --resource-group $ResourceGroup `
    --server-name $postgresServer `
    --name $postgresDb

az postgres flexible-server firewall-rule create `
    --resource-group $ResourceGroup `
    --server-name $postgresServer `
    --name AllowAzureServices `
    --start-ip-address 0.0.0.0 `
    --end-ip-address 0.0.0.0

$postgresHost = "$postgresServer.postgres.database.azure.com"
$encodedPostgresUser = [System.Uri]::EscapeDataString($PostgresAdminUser)
$encodedPostgresPassword = [System.Uri]::EscapeDataString($postgresPassword)
$postgresDsn = "postgresql://$encodedPostgresUser`:$encodedPostgresPassword@$postgresHost`:5432/$postgresDb?sslmode=require"

az keyvault create `
    --name $keyVault `
    --resource-group $ResourceGroup `
    --location $Location `
    --enable-rbac-authorization true `
    --retention-days 7

$currentUserObjectId = az ad signed-in-user show --query id --output tsv
$keyVaultScope = az keyvault show --name $keyVault --resource-group $ResourceGroup --query id --output tsv

az role assignment create `
    --assignee $currentUserObjectId `
    --role "Key Vault Secrets Officer" `
    --scope $keyVaultScope 1>$null

az keyvault secret set `
    --vault-name $keyVault `
    --name "azure-storage-connection-string" `
    --value $storageConnectionString 1>$null

az keyvault secret set `
    --vault-name $keyVault `
    --name "azure-postgres-dsn" `
    --value $postgresDsn 1>$null

if ($githubTokenPlainText) {
    az keyvault secret set `
        --vault-name $keyVault `
        --name "github-token" `
        --value $githubTokenPlainText 1>$null
}

az monitor app-insights component create `
    --app $appInsights `
    --location $Location `
    --resource-group $ResourceGroup `
    --application-type web 1>$null

$appInsightsConnectionString = az monitor app-insights component show `
    --app $appInsights `
    --resource-group $ResourceGroup `
    --query connectionString `
    --output tsv

az functionapp create `
    --name $functionApp `
    --resource-group $ResourceGroup `
    --storage-account $storageAccount `
    --consumption-plan-location $Location `
    --runtime python `
    --runtime-version 3.11 `
    --functions-version 4 `
    --os-type Linux `
    --assign-identity

$principalId = az functionapp identity show `
    --name $functionApp `
    --resource-group $ResourceGroup `
    --query principalId `
    --output tsv

az role assignment create `
    --assignee $principalId `
    --role "Key Vault Secrets User" `
    --scope $keyVaultScope 1>$null

$storageSecretUri = "https://$keyVault.vault.azure.net/secrets/azure-storage-connection-string"
$postgresSecretUri = "https://$keyVault.vault.azure.net/secrets/azure-postgres-dsn"
$githubSecretExists = az keyvault secret list `
    --vault-name $keyVault `
    --query "[?name=='github-token'] | length(@)" `
    --output tsv
$githubSecretUri = "https://$keyVault.vault.azure.net/secrets/github-token"

$appSettings = @(
    "AzureWebJobsStorage=@Microsoft.KeyVault(SecretUri=$storageSecretUri)",
    "AZURE_STORAGE_CONNECTION_STRING=@Microsoft.KeyVault(SecretUri=$storageSecretUri)",
    "AZURE_STORAGE_CONTAINER=$blobContainer",
    "AZURE_POSTGRES_DSN=@Microsoft.KeyVault(SecretUri=$postgresSecretUri)",
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$appInsightsConnectionString",
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
    "ENABLE_ORYX_BUILD=true",
    "REDDIT_DATALOAD_SCHEDULE=0 0 */6 * * *",
    "HACKER_NEWS_DATALOAD_SCHEDULE=0 30 */6 * * *",
    "GITHUB_DATALOAD_SCHEDULE=0 0 */12 * * *"
)

if ($githubSecretExists -gt 0) {
    $appSettings += "GITHUB_TOKEN=@Microsoft.KeyVault(SecretUri=$githubSecretUri)"
}

az functionapp config appsettings set `
    --name $functionApp `
    --resource-group $ResourceGroup `
    --settings $appSettings 1>$null

[PSCustomObject]@{
    resource_group = $ResourceGroup
    storage_account = $storageAccount
    blob_container = $blobContainer
    postgres_server = $postgresServer
    postgres_database = $postgresDb
    key_vault = $keyVault
    function_app = $functionApp
    app_insights = $appInsights
} | ConvertTo-Json
