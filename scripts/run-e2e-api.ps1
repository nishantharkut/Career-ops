$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$RootPath = [System.IO.Path]::GetFullPath($Root)
Set-Location $RootPath

$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$RunName = "careerops-e2e-$PID-$([System.Guid]::NewGuid().ToString('N'))"
$RunRoot = [System.IO.Path]::GetFullPath((Join-Path $TempRoot $RunName))
if (
    -not $RunRoot.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $RunRoot) -ne $RunName -or
    -not $RunName.StartsWith("careerops-e2e-", [System.StringComparison]::Ordinal)
) {
    throw "Refusing to use an unsafe E2E state path: $RunRoot"
}
New-Item -ItemType Directory -Path $RunRoot | Out-Null

$env:CAREEROPS_DB_PATH = Join-Path $RunRoot "careerops.db"
$env:CAREEROPS_VAULT_PATH = Join-Path $RunRoot "vault"

try {
    & ".\.venv\Scripts\python.exe" -m uvicorn tests.e2e_api_app:create_e2e_app --factory --host 127.0.0.1 --port 8765
    exit $LASTEXITCODE
}
finally {
    $ResolvedRunRoot = [System.IO.Path]::GetFullPath($RunRoot)
    if (
        $ResolvedRunRoot.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $ResolvedRunRoot) -eq $RunName -and
        $RunName.StartsWith("careerops-e2e-", [System.StringComparison]::Ordinal) -and
        (Test-Path -LiteralPath $ResolvedRunRoot)
    ) {
        Remove-Item -LiteralPath $ResolvedRunRoot -Recurse -Force
    }
}
