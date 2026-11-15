# Download BC 27 reference symbols into .alpackages WITHOUT any BC cloud environment.
#
# Run from FinFlux Backend\extensions:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\download-global-symbols.ps1
#
# Prefer VS Code (AL Language v17+):
#   Ctrl+Shift+P -> AL: Download Symbols from Global Sources
#   (NOT "AL: Download Symbols")

param(
    [int]$MajorVersion = 27
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$outDir = Join-Path $root ".alpackages"

if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Download-FromBcArtifacts {
    param([int]$Major)

    Write-Step "Resolving latest BC $Major sandbox artifact..."
    $latestUrl = "https://bcartifacts.azureedge.net/sandbox/$Major.0/latest/version"
    $version = (Invoke-RestMethod -Uri $latestUrl -UseBasicParsing).Trim()
    Write-Host "Artifact version: $version"

    $baseUrl = "https://bcartifacts.azureedge.net/sandbox/$version/base"
    $manifest = Invoke-RestMethod -Uri "$baseUrl/platformFiles.manifest" -UseBasicParsing

    $required = @(
        "Microsoft_Application.app",
        "Microsoft_System.app",
        "Microsoft_Business Foundation.app"
    )

    foreach ($appName in $required) {
        $entry = $manifest.platformFiles | Where-Object { $_.name -eq $appName }
        if (-not $entry) {
            Write-Warning "Not listed in manifest (may be optional): $appName"
            continue
        }
        $dest = Join-Path $outDir $appName
        Write-Host "Downloading $appName ..."
        Invoke-WebRequest -Uri "$baseUrl/$appName" -OutFile $dest -UseBasicParsing
        Write-Host "  saved -> $dest"
    }
}

Write-Host "FinFlux BC extension — offline symbol download"
Write-Host "Target folder: $outDir"

try {
    Download-FromBcArtifacts -Major $MajorVersion
}
catch {
    Write-Host ""
    Write-Host "Script download failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message
    Write-Host ""
    Write-Host "Use VS Code instead (recommended):" -ForegroundColor Yellow
    Write-Host "  1. Update AL Language extension to v17+"
    Write-Host "  2. Ctrl+Shift+P"
    Write-Host "  3. AL: Download Symbols from Global Sources"
    Write-Host "     (do NOT use 'AL: Download Symbols')"
    Write-Host "  4. Ctrl+Shift+P -> AL: Package"
    exit 1
}

$files = Get-ChildItem -Path $outDir -Filter "*.app" -ErrorAction SilentlyContinue
if (-not $files -or $files.Count -lt 2) {
    Write-Host ""
    Write-Host "Download incomplete — fewer than 2 .app files in .alpackages" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "SUCCESS — $($files.Count) symbol file(s) in .alpackages" -ForegroundColor Green
Write-Host "Next in VS Code:"
Write-Host "  Ctrl+Shift+P -> AL: Package"
Write-Host "Upload the .app to BC -> Extension Management"
