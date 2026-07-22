param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [Parameter(Mandatory = $true)]
    [string]$DataPath,
    [Parameter(Mandatory = $true)]
    [string]$VaultPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Get-SafeFullPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $FullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    if (Test-Path -LiteralPath $FullPath) {
        $Item = Get-Item -LiteralPath $FullPath -Force
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing a reparse-point path: $FullPath"
        }
    }
    return $FullPath
}

$ResolvedBackupRoot = Get-SafeFullPath -LiteralPath $BackupRoot
$ResolvedDataPath = Get-SafeFullPath -LiteralPath $DataPath
$ResolvedVaultPath = Get-SafeFullPath -LiteralPath $VaultPath
$Python = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot ".venv\Scripts\python.exe"))
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "CareerOps Python environment was not found"
}

& $Python -m careerops.operations.backups backup `
    --backup-root $ResolvedBackupRoot `
    --data-path $ResolvedDataPath `
    --vault-path $ResolvedVaultPath
exit $LASTEXITCODE
