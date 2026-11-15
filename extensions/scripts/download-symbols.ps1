# Download BC reference symbols into .alpackages without a live BC environment.
# Prefer VS Code: Ctrl+Shift+P -> "AL: Download Symbols from Global Sources"
#
# Usage (from FinFlux Backend\extensions):
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\download-symbols.ps1

param(
    [int]$MajorVersion = 27
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$outDir = Join-Path $root ".alpackages"

if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

function Get-LatestSandboxVersion {
    param([int]$Major)
    $latestUrl = "https://bcartifacts.azureedge.net/sandbox/$Major.0/latest/version"
    return (Invoke-RestMethod -Uri $latestUrl -UseBasicParsing).Trim()
}

function Download-FromBcArtifacts {
    param([string]$Version, [string]$TargetDir)
    $baseUrl = "https://bcartifacts.azureedge.net/sandbox/$Version/base"
    $manifest = Invoke-RestMethod -Uri "$baseUrl/platformFiles.manifest" -UseBasicParsing
    $symbolApps = @("Microsoft_Application.app", "Microsoft_System.app")
    foreach ($appName in $symbolApps) {
        $entry = $manifest.platformFiles | Where-Object { $_.name -eq $appName }
        if (-not $entry) {
            Write-Warning "Not in manifest: $appName"
            continue
        }
        $dest = Join-Path $TargetDir $appName
        Write-Host "Downloading $appName ..."
        Invoke-WebRequest -Uri "$baseUrl/$appName" -OutFile $dest -UseBasicParsing
        Write-Host "  -> $dest"
    }
}

Write-Host "Resolving BC $MajorVersion sandbox artifact version..."
try {
    $version = Get-LatestSandboxVersion -Major $MajorVersion
    Write-Host "Artifact version: $version"
    Download-FromBcArtifacts -Version $version -TargetDir $outDir
}
catch {
    Write-Host ""
    Write-Host "Offline script failed: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "Use VS Code instead:"
    Write-Host "  Ctrl+Shift+P -> AL: Download Symbols from Global Sources"
    exit 1
}

Write-Host ""
Write-Host "Done. Symbols saved to $outDir"
Write-Host "Next: Ctrl+Shift+P -> AL: Package"
