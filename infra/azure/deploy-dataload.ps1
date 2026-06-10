param(
    [Parameter(Mandatory = $true)]
    [string]$FunctionApp,
    [string]$ResourceGroup = "eliza-ai-workbench-rg"
)

$ErrorActionPreference = "Stop"

function az {
    & az.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed. See the Azure CLI output above for details."
    }
}

$dataloadRoot = Resolve-Path "$PSScriptRoot\..\..\dataload"
$zipPath = Join-Path ([System.IO.Path]::GetTempPath()) "wr-dataload-function.zip"

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Push-Location $dataloadRoot
try {
    $items = Get-ChildItem -Force | Where-Object {
        $_.Name -notin @(".python_packages", "__pycache__", "local.settings.json")
    }
    Compress-Archive -Path $items.FullName -DestinationPath $zipPath -Force
}
finally {
    Pop-Location
}

az functionapp deployment source config-zip `
    --resource-group $ResourceGroup `
    --name $FunctionApp `
    --src $zipPath `
    --build-remote true `
    --timeout 600
