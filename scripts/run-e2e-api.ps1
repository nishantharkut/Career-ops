$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$RootPath = [System.IO.Path]::GetFullPath($Root)
Set-Location $RootPath

$Targets = @(
    "data/e2e-careerops.db",
    "data/e2e-careerops.db-wal",
    "data/e2e-careerops.db-shm",
    "vault-e2e"
)

foreach ($Target in $Targets) {
    $FullPath = [System.IO.Path]::GetFullPath((Join-Path $RootPath $Target))
    if (-not $FullPath.StartsWith($RootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside project root: $FullPath"
    }
    if (Test-Path -LiteralPath $FullPath) {
        Remove-Item -LiteralPath $FullPath -Recurse -Force
    }
}

$env:CAREEROPS_DB_PATH = "data/e2e-careerops.db"
$env:CAREEROPS_VAULT_PATH = "vault-e2e"

& ".\.venv\Scripts\python.exe" -m uvicorn apps.api.careerops_api.main:app --host 127.0.0.1 --port 8765
