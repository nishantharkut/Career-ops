$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$RootPath = [System.IO.Path]::GetFullPath($Root)
Set-Location $RootPath

$TempRoot = (Get-Item -LiteralPath ([System.IO.Path]::GetTempPath())).FullName.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$RunName = "careerops-e2e-$PID-$([System.Guid]::NewGuid().ToString('N'))"
$RunNamePattern = '^careerops-e2e-[0-9]+-[0-9a-f]{32}$'
$RunRoot = [System.IO.Path]::GetFullPath((Join-Path $TempRoot $RunName))
$RunParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $RunRoot))
if (
    $RunParent -ne $TempRoot -or
    (Split-Path -Leaf $RunRoot) -ne $RunName -or
    $RunName -notmatch $RunNamePattern
) {
    throw "Refusing to use an unsafe E2E state path: $RunRoot"
}
New-Item -ItemType Directory -Path $RunRoot | Out-Null
$RunRootItem = Get-Item -LiteralPath $RunRoot
if (($RunRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Refusing to use an E2E reparse point: $RunRoot"
}

$env:CAREEROPS_E2E_RUN_ROOT = $RunRoot
$env:CAREEROPS_DB_PATH = Join-Path $RunRoot "careerops.db"
$env:CAREEROPS_VAULT_PATH = Join-Path $RunRoot "vault"

try {
    & ".\.venv\Scripts\python.exe" -m uvicorn tests.e2e_api_app:create_e2e_app --factory --host 127.0.0.1 --port 8765
    exit $LASTEXITCODE
}
finally {
    $ResolvedRunRoot = [System.IO.Path]::GetFullPath($RunRoot)
    $ResolvedRunParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $ResolvedRunRoot))
    $ExpectedDatabase = [System.IO.Path]::GetFullPath((Join-Path $ResolvedRunRoot "careerops.db"))
    $ExpectedVault = [System.IO.Path]::GetFullPath((Join-Path $ResolvedRunRoot "vault"))
    if (
        $ResolvedRunParent -eq $TempRoot -and
        (Split-Path -Leaf $ResolvedRunRoot) -eq $RunName -and
        $RunName -match $RunNamePattern -and
        [System.IO.Path]::GetFullPath($env:CAREEROPS_E2E_RUN_ROOT) -eq $ResolvedRunRoot -and
        [System.IO.Path]::GetFullPath($env:CAREEROPS_DB_PATH) -eq $ExpectedDatabase -and
        [System.IO.Path]::GetFullPath($env:CAREEROPS_VAULT_PATH) -eq $ExpectedVault -and
        (Test-Path -LiteralPath $ResolvedRunRoot)
    ) {
        $CleanupItem = Get-Item -LiteralPath $ResolvedRunRoot
        if (($CleanupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to remove an E2E reparse point: $ResolvedRunRoot"
        }
        Remove-Item -LiteralPath $ResolvedRunRoot -Recurse -Force
    }
}
