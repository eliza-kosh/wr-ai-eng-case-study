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

$processingRoot = Resolve-Path "$PSScriptRoot\..\..\processing"
$zipPath = Join-Path ([System.IO.Path]::GetTempPath()) "wr-processing-function.zip"

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Get-ChildItem -Path $processingRoot -Recurse -File -Force | Where-Object {
        $_.FullName -notmatch "[\\/]__pycache__[\\/]" -and
        $_.Name -ne "local.settings.json" -and
        $_.Name -ne ".funcignore"
    } | ForEach-Object {
        $rootUri = [System.Uri]((Join-Path $processingRoot "_") -replace "_$", "")
        $fileUri = [System.Uri]$_.FullName
        $entryName = [System.Uri]::UnescapeDataString($rootUri.MakeRelativeUri($fileUri).ToString())
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $_.FullName,
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $zip.Dispose()
}

az functionapp deployment source config-zip `
    --resource-group $ResourceGroup `
    --name $FunctionApp `
    --src $zipPath `
    --build-remote true `
    --timeout 600
