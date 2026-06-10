param(
    [string]$GitHubOrg = "eliza-kosh",
    [string]$GitHubRepo = "wr-ai-eng-case-study",
    [string]$Branch = "main",
    [string]$ResourceGroup = "eliza-ai-workbench-rg",
    [string]$FunctionApp = "eliza-ai-workbench-dataload-func-58415",
    [string]$AppName = "wr-ai-eng-case-study-github-actions"
)

$ErrorActionPreference = "Stop"

function az {
    & az.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed. See the Azure CLI output above for details."
    }
}

$subscriptionId = az account show --query id --output tsv
$tenantId = az account show --query tenantId --output tsv

$appId = az ad app create --display-name $AppName --query appId --output tsv
$objectId = az ad sp create --id $appId --query id --output tsv

$functionScope = az functionapp show `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --query id `
    --output tsv

az role assignment create `
    --assignee-object-id $objectId `
    --assignee-principal-type ServicePrincipal `
    --role "Website Contributor" `
    --scope $functionScope 1>$null

$credential = @{
    name = "github-main"
    issuer = "https://token.actions.githubusercontent.com"
    subject = "repo:$GitHubOrg/$GitHubRepo`:ref:refs/heads/$Branch"
    description = "GitHub Actions deployment from $GitHubOrg/$GitHubRepo $Branch"
    audiences = @("api://AzureADTokenExchange")
}

$credentialPath = Join-Path ([System.IO.Path]::GetTempPath()) "github-oidc-credential.json"
$credential | ConvertTo-Json | Set-Content -Path $credentialPath -Encoding utf8

az ad app federated-credential create `
    --id $appId `
    --parameters "@$credentialPath" 1>$null

[PSCustomObject]@{
    AZURE_CLIENT_ID = $appId
    AZURE_TENANT_ID = $tenantId
    AZURE_SUBSCRIPTION_ID = $subscriptionId
} | ConvertTo-Json
