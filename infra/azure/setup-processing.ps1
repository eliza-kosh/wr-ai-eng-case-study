param(
    [string]$Project = "eliza-ai-workbench",
    [string]$Location = "eastus",
    [string]$ResourceGroup = "$Project-rg",
    [Parameter(Mandatory = $true)]
    [string]$StorageAccount,
    [Parameter(Mandatory = $true)]
    [string]$KeyVault,
    [Parameter(Mandatory = $true)]
    [SecureString]$OpenAIApiKey,
    [string]$ProcessingPrepareSchedule = "0 0 11 * * *",
    [string]$ProcessingSynthesisSchedule = "0 30 11 * * *"
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
$functionApp = "$Project-processing-func-$suffix"
$appInsights = "$Project-processing-ai-$suffix"
$openAiPlainText = Convert-SecretToPlainText $OpenAIApiKey

az config set extension.use_dynamic_install=yes_without_prompt
az group show --name $ResourceGroup 1>$null
az storage account show --name $StorageAccount --resource-group $ResourceGroup 1>$null
az keyvault show --name $KeyVault --resource-group $ResourceGroup 1>$null

az keyvault secret set `
    --vault-name $KeyVault `
    --name "openai-api-key" `
    --value $openAiPlainText 1>$null

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
    --storage-account $StorageAccount `
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

$keyVaultScope = az keyvault show --name $KeyVault --resource-group $ResourceGroup --query id --output tsv

az role assignment create `
    --assignee $principalId `
    --role "Key Vault Secrets User" `
    --scope $keyVaultScope 1>$null

$storageSecretUri = "https://$KeyVault.vault.azure.net/secrets/azure-storage-connection-string"
$postgresSecretUri = "https://$KeyVault.vault.azure.net/secrets/azure-postgres-dsn"
$openAiSecretUri = "https://$KeyVault.vault.azure.net/secrets/openai-api-key"

$appSettings = @(
    "AzureWebJobsStorage=@Microsoft.KeyVault(SecretUri=$storageSecretUri)",
    "AZURE_POSTGRES_DSN=@Microsoft.KeyVault(SecretUri=$postgresSecretUri)",
    "OPENAI_API_KEY=@Microsoft.KeyVault(SecretUri=$openAiSecretUri)",
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$appInsightsConnectionString",
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
    "ENABLE_ORYX_BUILD=true",
    "OPENAI_ENRICHMENT_MODEL=gpt-5.4-mini",
    "OPENAI_EMBEDDING_MODEL=text-embedding-3-small",
    "PROCESSING_PREPARE_SCHEDULE=$ProcessingPrepareSchedule",
    "PROCESSING_SYNTHESIS_SCHEDULE=$ProcessingSynthesisSchedule",
    "PROCESSING_RELEVANCE_THRESHOLD=0",
    "PROCESSING_SIMILARITY_THRESHOLD=0.0",
    "PROCESSING_CONNECTION_CONFIDENCE_THRESHOLD=0.10",
    "PROCESSING_MAX_AGENT_SEARCHES=5",
    "PROCESSING_TEMPORAL_WINDOW_DAYS=90",
    "PROCESSING_MAX_CONNECTION_CANDIDATES_PER_TICKER=12",
    "PROCESSING_MAX_VALID_CONNECTIONS_PER_TICKER=4",
    "PROCESSING_CONNECTION_CLUSTER_SIZE=30",
    "PROCESSING_CONNECTION_CLUSTER_MAX_OVERLAP=0.65"
)

$subscriptionId = az account show --query id --output tsv
$appSettingsProperties = @{}
foreach ($setting in $appSettings) {
    $name, $value = $setting.Split("=", 2)
    $appSettingsProperties[$name] = $value
}
$appSettingsBody = @{ properties = $appSettingsProperties } | ConvertTo-Json -Depth 5
$appSettingsBodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "wr-processing-appsettings.json"
Set-Content -Path $appSettingsBodyPath -Value $appSettingsBody -Encoding UTF8
try {
    az rest `
        --method put `
        --uri "https://management.azure.com/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$functionApp/config/appsettings?api-version=2022-03-01" `
        --body "@$appSettingsBodyPath" `
        --headers "Content-Type=application/json" 1>$null
}
finally {
    Remove-Item $appSettingsBodyPath -Force -ErrorAction SilentlyContinue
}

[PSCustomObject]@{
    resource_group = $ResourceGroup
    storage_account = $StorageAccount
    key_vault = $KeyVault
    function_app = $functionApp
    app_insights = $appInsights
} | ConvertTo-Json
