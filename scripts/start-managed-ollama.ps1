param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Launch", "Verify", "Stop", "Recover")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$ExecutablePath,
    [Parameter(Mandatory = $true)]
    [string]$Model,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [ValidateRange(1024, 65535)]
    [int]$Port = 11434,
    [string]$RuntimeInstanceId = "",
    [string]$RuntimeHandle = "",
    [int]$ProcessId = 0,
    [string]$IsolationRuleId = "",
    [string]$ProcessStartedAt = "",
    [string]$ExecutableSha256 = "",
    [int64]$ExecutableSize = 0,
    [string]$FirewallRuleToken = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$IdentifierPattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
$ModelPattern = '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
$Sha256Pattern = '^[0-9a-f]{64}$'
$FirewallTokenPattern = '^[0-9a-f]{32}$'
$NonLoopbackRanges = @(
    "0.0.0.0-126.255.255.255",
    "128.0.0.0-255.255.255.255",
    "::",
    "::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
)

function Get-RegularFullPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][bool]$MustExist
    )
    $FullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    if ($MustExist -and -not (Test-Path -LiteralPath $FullPath)) {
        throw "Required managed-runtime path was missing"
    }
    if (Test-Path -LiteralPath $FullPath) {
        $Item = Get-Item -LiteralPath $FullPath -Force
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Managed-runtime paths must not be reparse points"
        }
    }
    return $FullPath
}

function Assert-IdentityParameters {
    if (
        $RuntimeInstanceId -notmatch $IdentifierPattern -or
        $IsolationRuleId -notmatch $IdentifierPattern -or
        $ProcessId -le 0 -or
        $RuntimeHandle -ne "pid:$ProcessId" -or
        $ProcessStartedAt -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$' -or
        $ExecutableSha256 -notmatch $Sha256Pattern -or
        $ExecutableSize -le 0 -or
        $FirewallRuleToken -notmatch $FirewallTokenPattern
    ) {
        throw "Managed runtime identity parameters were invalid"
    }
}

function Get-InstancePaths {
    param([Parameter(Mandatory = $true)][string]$Root)
    $ExpectedInstanceRoot = [System.IO.Path]::GetFullPath((Join-Path $Root $RuntimeInstanceId))
    if ((Split-Path -Parent $ExpectedInstanceRoot) -ne $Root) {
        throw "Managed runtime instance escaped its app-owned root"
    }
    $OwnedExecutable = [System.IO.Path]::GetFullPath(
        (Join-Path $ExpectedInstanceRoot "bin\ollama.exe")
    )
    return @{
        InstanceRoot = $ExpectedInstanceRoot
        OwnedExecutable = $OwnedExecutable
        LaunchRecord = (Join-Path $ExpectedInstanceRoot "launch-record.json")
    }
}

function Write-LaunchRecord {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Paths,
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][int]$OwnedProcessId,
        [Parameter(Mandatory = $true)][string]$OwnedRuntimeHandle,
        [Parameter(Mandatory = $true)][string]$OwnedProcessStartedAt,
        [Parameter(Mandatory = $true)][string]$OwnedExecutableSha256,
        [Parameter(Mandatory = $true)][int64]$OwnedExecutableSize,
        [Parameter(Mandatory = $true)][string]$OwnedFirewallRuleToken,
        [Parameter(Mandatory = $true)][string]$Endpoint
    )
    $Payload = [ordered]@{
        version = 2
        runtime_instance_id = $RuntimeInstanceId
        isolation_rule_id = $IsolationRuleId
        owned_executable = $Paths.OwnedExecutable
        endpoint = $Endpoint
        model = $Model
        process_id = $OwnedProcessId
        runtime_handle = $OwnedRuntimeHandle
        process_started_at = $OwnedProcessStartedAt
        executable_sha256 = $OwnedExecutableSha256
        executable_size = $OwnedExecutableSize
        firewall_rule_token = $OwnedFirewallRuleToken
        state = $State
    } | ConvertTo-Json -Compress
    $Bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Payload)
    if (-not (Test-Path -LiteralPath $Paths.LaunchRecord)) {
        $Stream = [System.IO.FileStream]::new(
            $Paths.LaunchRecord,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $Stream.Write($Bytes, 0, $Bytes.Length)
            $Stream.Flush($true)
        }
        finally {
            $Stream.Dispose()
        }
        return
    }
    $TemporaryRecord = "$($Paths.LaunchRecord).tmp-$([System.Guid]::NewGuid().ToString('N'))"
    $Stream = [System.IO.FileStream]::new(
        $TemporaryRecord,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
    $BackupRecord = "$($Paths.LaunchRecord).bak-$([System.Guid]::NewGuid().ToString('N'))"
    [System.IO.File]::Replace(
        $TemporaryRecord,
        $Paths.LaunchRecord,
        $BackupRecord,
        $true
    )
    Remove-Item -LiteralPath $BackupRecord -Force -ErrorAction Stop
}

function Get-ExecutableIdentity {
    param([Parameter(Mandatory = $true)][string]$OwnedExecutable)
    $Item = Get-Item -LiteralPath $OwnedExecutable -Force -ErrorAction Stop
    if (
        ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $Item.PSIsContainer -or
        [int64]$Item.Length -le 0
    ) {
        throw "Managed Ollama executable identity was invalid"
    }
    $Digest = (Get-FileHash -LiteralPath $OwnedExecutable -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($Digest -notmatch $Sha256Pattern) {
        throw "Managed Ollama executable digest was invalid"
    }
    return [pscustomobject]@{ Sha256 = $Digest; Size = [int64]$Item.Length }
}

function Get-ProcessStartIdentity {
    param([Parameter(Mandatory = $true)]$Process)
    return $Process.StartTime.ToUniversalTime().ToString(
        "o", [System.Globalization.CultureInfo]::InvariantCulture
    )
}

function New-OllamaProcessStartInfo {
    param(
        [Parameter(Mandatory = $true)][string]$OwnedExecutable,
        [Parameter(Mandatory = $true)][string]$ManagedModelsPath,
        [Parameter(Mandatory = $true)][int]$OwnedPort
    )
    $Info = [System.Diagnostics.ProcessStartInfo]::new()
    $Info.FileName = $OwnedExecutable
    $Info.Arguments = "serve"
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $Info.EnvironmentVariables.Clear()
    foreach ($Name in @("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")) {
        $Value = [System.Environment]::GetEnvironmentVariable($Name, "Process")
        if (-not [string]::IsNullOrEmpty($Value)) {
            $Info.EnvironmentVariables[$Name] = $Value
        }
    }
    $Info.EnvironmentVariables["OLLAMA_HOST"] = "127.0.0.1:$OwnedPort"
    $Info.EnvironmentVariables["OLLAMA_NO_CLOUD"] = "1"
    $Info.EnvironmentVariables["OLLAMA_MODELS"] = $ManagedModelsPath
    $Info.EnvironmentVariables["NO_PROXY"] = "127.0.0.1,localhost"
    $Info.EnvironmentVariables["no_proxy"] = "127.0.0.1,localhost"
    return $Info
}

function Set-OllamaProcessEnvironment {
    param([Parameter(Mandatory = $true)]$Variables)
    $Allowed = @(
        "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP",
        "OLLAMA_HOST", "OLLAMA_NO_CLOUD", "OLLAMA_MODELS", "NO_PROXY", "no_proxy"
    )
    $Names = @($Variables.Keys)
    if (
        @($Names | Where-Object { $_ -notin $Allowed }).Count -ne 0 -or
        @("OLLAMA_HOST", "OLLAMA_NO_CLOUD", "OLLAMA_MODELS" | Where-Object { $_ -notin $Names }).Count -ne 0
    ) {
        throw "Managed Ollama child environment was invalid"
    }
    foreach ($Variable in @(Get-ChildItem Env:)) {
        Remove-Item -LiteralPath "Env:\$($Variable.Name)" -ErrorAction Stop
    }
    foreach ($Name in $Names) {
        [System.Environment]::SetEnvironmentVariable(
            $Name,
            $Variables[$Name],
            [System.EnvironmentVariableTarget]::Process
        )
    }
}

function Test-LoopbackListenerAddress {
    param([Parameter(Mandatory = $true)][string]$Address)
    $ParsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$ParsedAddress)) {
        return $false
    }
    if ($ParsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        return $ParsedAddress.GetAddressBytes()[0] -eq 127
    }
    return [System.Net.IPAddress]::IsLoopback($ParsedAddress)
}

function Assert-PortUnbound {
    $ExistingListeners = @(Get-NetTCPConnection `
        -State Listen `
        -LocalPort $Port `
        -ErrorAction SilentlyContinue)
    if ($ExistingListeners.Count -ne 0) {
        throw "Managed Ollama port was already owned by another listener"
    }
}

function Assert-EndpointOwnedByProcess {
    param([Parameter(Mandatory = $true)][int]$OwnedProcessId)
    $Deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        $Listeners = @(Get-NetTCPConnection `
            -State Listen `
            -LocalPort $Port `
            -ErrorAction SilentlyContinue)
        if ($Listeners.Count -gt 0) {
            foreach ($Listener in $Listeners) {
                if (
                    [int]$Listener.OwningProcess -ne $OwnedProcessId -or
                    -not (Test-LoopbackListenerAddress -Address $Listener.LocalAddress)
                ) {
                    throw "Managed Ollama endpoint ownership did not match its process"
                }
            }
            return
        }
        if ([DateTimeOffset]::UtcNow -ge $Deadline) {
            throw "Managed Ollama process did not own its listening endpoint"
        }
        Start-Sleep -Milliseconds 100
    } while ($true)
}

function Recover-PendingLaunches {
    param([Parameter(Mandatory = $true)][string]$Root)
    $Recovered = @()
    if (-not (Test-Path -LiteralPath $Root)) {
        return $Recovered
    }
    foreach ($Directory in @(Get-ChildItem -LiteralPath $Root -Directory -Force)) {
        if (($Directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Managed runtime recovery encountered a reparse point"
        }
        $RecordPath = Join-Path $Directory.FullName "launch-record.json"
        if (-not (Test-Path -LiteralPath $RecordPath)) {
            continue
        }
        $RecordItem = Get-Item -LiteralPath $RecordPath -Force
        if (
            ($RecordItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $RecordItem.Length -gt 16384
        ) {
            throw "Managed runtime launch record was invalid"
        }
        $Record = Get-Content -LiteralPath $RecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ExpectedExecutable = [System.IO.Path]::GetFullPath(
            (Join-Path $Directory.FullName "bin\ollama.exe")
        )
        $RecordProcessId = [int]$Record.process_id
        $RecordRuntimeHandle = [string]$Record.runtime_handle
        $RecordProcessStartedAt = [string]$Record.process_started_at
        $RecordExecutableSha256 = [string]$Record.executable_sha256
        $RecordExecutableSize = [int64]$Record.executable_size
        $RecordFirewallToken = [string]$Record.firewall_rule_token
        if (
            $Record.version -ne 2 -or
            $Record.runtime_instance_id -ne $Directory.Name -or
            $Record.runtime_instance_id -notmatch $IdentifierPattern -or
            $Record.isolation_rule_id -ne "CareerOps-Ollama-$($Directory.Name)" -or
            [System.IO.Path]::GetFullPath([string]$Record.owned_executable) -ne $ExpectedExecutable -or
            $Record.endpoint -ne "http://127.0.0.1:$Port" -or
            $Record.model -ne $Model -or
            $RecordExecutableSha256 -notmatch $Sha256Pattern -or
            $RecordExecutableSize -le 0 -or
            $RecordFirewallToken -notmatch $FirewallTokenPattern -or
            $Record.state -notin @("intent", "firewall_ready", "launched") -or
            (
                $RecordProcessId -eq 0 -and
                ($RecordRuntimeHandle -ne "" -or $RecordProcessStartedAt -ne "")
            ) -or
            (
                $RecordProcessId -gt 0 -and
                (
                    $RecordRuntimeHandle -ne "pid:$RecordProcessId" -or
                    $RecordProcessStartedAt -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$'
                )
            ) -or
            $RecordProcessId -lt 0
        ) {
            throw "Managed runtime launch record identity was invalid"
        }
        $ExecutableIdentity = Get-ExecutableIdentity -OwnedExecutable $ExpectedExecutable
        if (
            $ExecutableIdentity.Sha256 -ne $RecordExecutableSha256 -or
            $ExecutableIdentity.Size -ne $RecordExecutableSize
        ) {
            throw "Managed runtime executable identity changed"
        }
        $Candidate = $null
        if (
            $RecordProcessId -eq 0 -and
            $Record.state -in @("intent", "firewall_ready")
        ) {
            $Discovered = @()
            foreach ($ObservedProcess in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
                if ($null -eq $ObservedProcess.ExecutablePath) {
                    continue
                }
                try {
                    $ObservedExecutable = [System.IO.Path]::GetFullPath(
                        [string]$ObservedProcess.ExecutablePath
                    )
                }
                catch {
                    continue
                }
                if ($ObservedExecutable -eq $ExpectedExecutable) {
                    $Discovered += $ObservedProcess
                }
            }
            if ($Discovered.Count -gt 1) {
                throw "Interrupted managed runtime process identity was ambiguous"
            }
            if ($Discovered.Count -eq 1) {
                $RecordProcessId = [int]$Discovered[0].ProcessId
                if ($RecordProcessId -le 0) {
                    throw "Interrupted managed runtime process identity was invalid"
                }
                $Candidate = Get-Process -Id $RecordProcessId -ErrorAction Stop
                $RecordProcessStartedAt = Get-ProcessStartIdentity -Process $Candidate
                $RecordRuntimeHandle = "pid:$RecordProcessId"
                Assert-OwnedProcess `
                    -Root $Directory.FullName `
                    -OwnedExecutable $ExpectedExecutable `
                    -OwnedProcessId $RecordProcessId `
                    -ExpectedStartedAt $RecordProcessStartedAt `
                    -ExpectedExecutableSha256 $RecordExecutableSha256 `
                    -ExpectedExecutableSize $RecordExecutableSize | Out-Null
                Write-LaunchRecord `
                    -Paths @{ LaunchRecord = $RecordPath; OwnedExecutable = $ExpectedExecutable } `
                    -State "launched" `
                    -OwnedProcessId $RecordProcessId `
                    -OwnedRuntimeHandle $RecordRuntimeHandle `
                    -OwnedProcessStartedAt $RecordProcessStartedAt `
                    -OwnedExecutableSha256 $RecordExecutableSha256 `
                    -OwnedExecutableSize $RecordExecutableSize `
                    -OwnedFirewallRuleToken $RecordFirewallToken `
                    -Endpoint ([string]$Record.endpoint)
            }
        }
        if ($RecordProcessId -gt 0) {
            if ($null -eq $Candidate) {
                $Candidate = Get-Process -Id $RecordProcessId -ErrorAction SilentlyContinue
            }
            if ($null -ne $Candidate) {
                Assert-OwnedProcess `
                    -Root $Directory.FullName `
                    -OwnedExecutable $ExpectedExecutable `
                    -OwnedProcessId $RecordProcessId `
                    -ExpectedStartedAt $RecordProcessStartedAt `
                    -ExpectedExecutableSha256 $RecordExecutableSha256 `
                    -ExpectedExecutableSize $RecordExecutableSize | Out-Null
            }
        }
        $Rule = Get-NetFirewallRule -Name $Record.isolation_rule_id -ErrorAction SilentlyContinue
        if ($null -ne $Rule) {
            Assert-FirewallBoundary `
                -RuleName $Record.isolation_rule_id `
                -OwnedExecutable $ExpectedExecutable `
                -RuleToken $RecordFirewallToken
        }
        if ($null -ne $Candidate) {
            Stop-Process -Id $RecordProcessId -Force -ErrorAction Stop
        }
        if ($null -ne $Rule) {
            Remove-NetFirewallRule -Name $Record.isolation_rule_id -ErrorAction Stop
            if (Get-NetFirewallRule -Name $Record.isolation_rule_id -ErrorAction SilentlyContinue) {
                throw "Managed Ollama firewall rule was not removed"
            }
        }
        Remove-Item -LiteralPath $RecordPath -Force -ErrorAction Stop
        $Recovered += $Directory.Name
    }
    return $Recovered
}

function Assert-FirewallRuleNameAvailable {
    param([Parameter(Mandatory = $true)][string]$RuleName)
    if (Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) {
        throw "Managed Ollama firewall rule name was already in use"
    }
}

function Assert-FirewallBoundary {
    param(
        [Parameter(Mandatory = $true)][string]$RuleName,
        [Parameter(Mandatory = $true)][string]$OwnedExecutable,
        [Parameter(Mandatory = $true)][string]$RuleToken
    )
    $Rule = Get-NetFirewallRule -Name $RuleName -ErrorAction Stop
    if (
        [string]$Rule.Name -ne $RuleName -or
        [string]$Rule.DisplayName -ne $RuleName -or
        [string]$Rule.Description -ne "CareerOps managed Ollama runtime $RuleToken" -or
        [string]$Rule.Enabled -ne "True" -or
        [string]$Rule.Direction -ne "Outbound" -or
        [string]$Rule.Action -ne "Block" -or
        [string]$Rule.Profile -ne "Any"
    ) {
        throw "Managed Ollama firewall rule was not enforced"
    }
    $Application = $Rule | Get-NetFirewallApplicationFilter
    if ([System.IO.Path]::GetFullPath($Application.Program) -ne $OwnedExecutable) {
        throw "Managed Ollama firewall rule targeted the wrong executable"
    }
    $Address = $Rule | Get-NetFirewallAddressFilter
    $ObservedAddresses = @($Address.RemoteAddress | Sort-Object)
    $ExpectedAddresses = @($NonLoopbackRanges | Sort-Object)
    if (Compare-Object $ObservedAddresses $ExpectedAddresses) {
        throw "Managed Ollama firewall rule did not block every non-loopback address"
    }
}

function Assert-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$OwnedExecutable,
        [Parameter(Mandatory = $true)][int]$OwnedProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedStartedAt,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutableSha256,
        [Parameter(Mandatory = $true)][int64]$ExpectedExecutableSize
    )
    $Process = Get-Process -Id $OwnedProcessId -ErrorAction Stop
    $ExecutableIdentity = Get-ExecutableIdentity -OwnedExecutable $OwnedExecutable
    if (
        [System.IO.Path]::GetFullPath($Process.Path) -ne $OwnedExecutable -or
        -not $OwnedExecutable.StartsWith(
            $Root + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Managed Ollama process path identity did not match"
    }
    if ((Get-ProcessStartIdentity -Process $Process) -ne $ExpectedStartedAt) {
        throw "Managed Ollama process creation identity did not match"
    }
    if (
        $ExecutableIdentity.Sha256 -ne $ExpectedExecutableSha256 -or
        $ExecutableIdentity.Size -ne $ExpectedExecutableSize
    ) {
        throw "Managed Ollama process executable identity did not match"
    }
    return $Process
}

function Assert-LocalModel {
    param([Parameter(Mandatory = $true)][string]$Endpoint)
    $Deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        try {
            $Request = [System.Net.HttpWebRequest]::Create("$Endpoint/api/tags")
            $Request.Proxy = $null
            $Request.Timeout = 2000
            $Request.ReadWriteTimeout = 2000
            $HttpResponse = $Request.GetResponse()
            try {
                $Reader = [System.IO.StreamReader]::new($HttpResponse.GetResponseStream())
                try {
                    $Response = $Reader.ReadToEnd() | ConvertFrom-Json
                }
                finally {
                    $Reader.Dispose()
                }
            }
            finally {
                $HttpResponse.Dispose()
            }
            $Match = @($Response.models | Where-Object {
                $_.name -eq $Model -and
                $_.digest -match '^[0-9a-fA-F]{64}$' -and
                [int64]$_.size -gt 0
            })
            if ($Match.Count -eq 1) {
                return
            }
            throw "The exact local Ollama model is not installed"
        }
        catch {
            if ([DateTimeOffset]::UtcNow -ge $Deadline) {
                throw "Managed Ollama did not expose the exact local model"
            }
            Start-Sleep -Milliseconds 250
        }
    } while ($true)
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}

$ResolvedExecutable = Get-RegularFullPath `
    -LiteralPath $ExecutablePath `
    -MustExist ($Action -eq "Launch")
$ResolvedRuntimeRoot = Get-RegularFullPath -LiteralPath $RuntimeRoot -MustExist $false
if ($Model -notmatch $ModelPattern -or $Model.StartsWith(".") -or $Model.Contains("..") -or $Model.Contains("//")) {
    throw "Managed Ollama model name was invalid"
}

if ($Action -eq "Recover") {
    $RecoveredInstances = @(Recover-PendingLaunches -Root $ResolvedRuntimeRoot)
    [ordered]@{
        recovered = $true
        runtime_instances = $RecoveredInstances
    } | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq "Launch") {
    $RuntimeInstanceId = "runtime-$([System.Guid]::NewGuid().ToString('N'))"
    $IsolationRuleId = "CareerOps-Ollama-$RuntimeInstanceId"
    if (-not (Test-Path -LiteralPath $ResolvedRuntimeRoot)) {
        [System.IO.Directory]::CreateDirectory($ResolvedRuntimeRoot) | Out-Null
    }
    $RootItem = Get-Item -LiteralPath $ResolvedRuntimeRoot -Force
    if (($RootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Managed Ollama runtime root must not be a reparse point"
    }
    $Paths = Get-InstancePaths -Root $ResolvedRuntimeRoot
    $ManagedModelsPath = [System.IO.Path]::GetFullPath(
        (Join-Path $ResolvedRuntimeRoot "models")
    )
    if ((Split-Path -Parent $ManagedModelsPath) -ne $ResolvedRuntimeRoot) {
        throw "Managed Ollama model path escaped its app-owned root"
    }
    $Process = $null
    try {
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $Paths.OwnedExecutable)) | Out-Null
        [System.IO.Directory]::CreateDirectory($ManagedModelsPath) | Out-Null
        $ModelsItem = Get-Item -LiteralPath $ManagedModelsPath -Force
        if (($ModelsItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Managed Ollama model path must not be a reparse point"
        }
        [System.IO.File]::Copy($ResolvedExecutable, $Paths.OwnedExecutable, $false)
        $OwnedItem = Get-Item -LiteralPath $Paths.OwnedExecutable -Force
        if (($OwnedItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Managed Ollama executable copy was not regular"
        }
        $ExecutableIdentity = Get-ExecutableIdentity -OwnedExecutable $Paths.OwnedExecutable
        $ExecutableSha256 = $ExecutableIdentity.Sha256
        $ExecutableSize = $ExecutableIdentity.Size
        $FirewallRuleToken = [System.Guid]::NewGuid().ToString('N').ToLowerInvariant()

        $Endpoint = "http://127.0.0.1:$Port"
        Assert-PortUnbound
        Assert-FirewallRuleNameAvailable -RuleName $IsolationRuleId
        Write-LaunchRecord `
            -Paths $Paths `
            -State "intent" `
            -OwnedProcessId 0 `
            -OwnedRuntimeHandle "" `
            -OwnedProcessStartedAt "" `
            -OwnedExecutableSha256 $ExecutableSha256 `
            -OwnedExecutableSize $ExecutableSize `
            -OwnedFirewallRuleToken $FirewallRuleToken `
            -Endpoint $Endpoint

        New-NetFirewallRule `
            -Name $IsolationRuleId `
            -DisplayName $IsolationRuleId `
            -Direction Outbound `
            -Action Block `
            -Enabled True `
            -Profile Any `
            -Description "CareerOps managed Ollama runtime $FirewallRuleToken" `
            -Program $Paths.OwnedExecutable `
            -RemoteAddress $NonLoopbackRanges | Out-Null
        Assert-FirewallBoundary `
            -RuleName $IsolationRuleId `
            -OwnedExecutable $Paths.OwnedExecutable `
            -RuleToken $FirewallRuleToken

        Write-LaunchRecord `
            -Paths $Paths `
            -State "firewall_ready" `
            -OwnedProcessId 0 `
            -OwnedRuntimeHandle "" `
            -OwnedProcessStartedAt "" `
            -OwnedExecutableSha256 $ExecutableSha256 `
            -OwnedExecutableSize $ExecutableSize `
            -OwnedFirewallRuleToken $FirewallRuleToken `
            -Endpoint $Endpoint
        $StartInfo = New-OllamaProcessStartInfo `
            -OwnedExecutable $Paths.OwnedExecutable `
            -ManagedModelsPath $ManagedModelsPath `
            -OwnedPort $Port
        Get-Command Get-CimInstance -ErrorAction Stop | Out-Null
        Set-OllamaProcessEnvironment -Variables $StartInfo.EnvironmentVariables
        [string[]]$Arguments = @("serve")
        $Process = Start-Process `
            -FilePath $Paths.OwnedExecutable `
            -ArgumentList $Arguments `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Paths.InstanceRoot "stdout.log") `
            -RedirectStandardError (Join-Path $Paths.InstanceRoot "stderr.log")
        $ProcessId = $Process.Id
        $ProcessStartedAt = Get-ProcessStartIdentity -Process $Process
        $RuntimeHandle = "pid:$ProcessId"
        Write-LaunchRecord `
            -Paths $Paths `
            -State "launched" `
            -OwnedProcessId $ProcessId `
            -OwnedRuntimeHandle $RuntimeHandle `
            -OwnedProcessStartedAt $ProcessStartedAt `
            -OwnedExecutableSha256 $ExecutableSha256 `
            -OwnedExecutableSize $ExecutableSize `
            -OwnedFirewallRuleToken $FirewallRuleToken `
            -Endpoint $Endpoint
        Assert-OwnedProcess `
            -Root $Paths.InstanceRoot `
            -OwnedExecutable $Paths.OwnedExecutable `
            -OwnedProcessId $ProcessId `
            -ExpectedStartedAt $ProcessStartedAt `
            -ExpectedExecutableSha256 $ExecutableSha256 `
            -ExpectedExecutableSize $ExecutableSize | Out-Null
        Assert-FirewallBoundary `
            -RuleName $IsolationRuleId `
            -OwnedExecutable $Paths.OwnedExecutable `
            -RuleToken $FirewallRuleToken
        Assert-EndpointOwnedByProcess -OwnedProcessId $ProcessId
        Assert-LocalModel -Endpoint $Endpoint
        Assert-EndpointOwnedByProcess -OwnedProcessId $ProcessId

        [ordered]@{
            endpoint = $Endpoint
            model = $Model
            runtime_instance_id = $RuntimeInstanceId
            runtime_handle = $RuntimeHandle
            handle_kind = "process"
            process_id = $ProcessId
            container_id = $null
            container_image_reference = $null
            container_image_id = $null
            process_started_at = $ProcessStartedAt
            executable_sha256 = $ExecutableSha256
            executable_size = $ExecutableSize
            firewall_rule_token = $FirewallRuleToken
            egress_policy = "os_firewall_block"
            egress_enforced = $true
            cloud_disabled = $true
            isolation_rule_id = $IsolationRuleId
        } | ConvertTo-Json -Compress
    }
    catch {
        $LaunchError = $_
        try {
            Recover-PendingLaunches -Root $ResolvedRuntimeRoot | Out-Null
        }
        catch {
            throw "Managed Ollama launch failed and durable recovery did not complete"
        }
        throw $LaunchError
    }
    exit 0
}

Assert-IdentityParameters
$Paths = Get-InstancePaths -Root $ResolvedRuntimeRoot

if ($Action -eq "Verify") {
    Assert-OwnedProcess `
        -Root $Paths.InstanceRoot `
        -OwnedExecutable $Paths.OwnedExecutable `
        -OwnedProcessId $ProcessId `
        -ExpectedStartedAt $ProcessStartedAt `
        -ExpectedExecutableSha256 $ExecutableSha256 `
        -ExpectedExecutableSize $ExecutableSize | Out-Null
    Assert-FirewallBoundary `
        -RuleName $IsolationRuleId `
        -OwnedExecutable $Paths.OwnedExecutable `
        -RuleToken $FirewallRuleToken
    Assert-EndpointOwnedByProcess -OwnedProcessId $ProcessId
    Assert-LocalModel -Endpoint "http://127.0.0.1:$Port"
    Assert-EndpointOwnedByProcess -OwnedProcessId $ProcessId
    [ordered]@{
        verified = $true
        runtime_instance_id = $RuntimeInstanceId
        runtime_handle = $RuntimeHandle
    } | ConvertTo-Json -Compress
    exit 0
}

Assert-FirewallBoundary `
    -RuleName $IsolationRuleId `
    -OwnedExecutable $Paths.OwnedExecutable `
    -RuleToken $FirewallRuleToken
$OwnedProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($null -ne $OwnedProcess) {
    Assert-OwnedProcess `
        -Root $Paths.InstanceRoot `
        -OwnedExecutable $Paths.OwnedExecutable `
        -OwnedProcessId $ProcessId `
        -ExpectedStartedAt $ProcessStartedAt `
        -ExpectedExecutableSha256 $ExecutableSha256 `
        -ExpectedExecutableSize $ExecutableSize | Out-Null
    $OwnedProcess | Stop-Process -Force -ErrorAction Stop
    $OwnedProcess.WaitForExit(10000)
}
Remove-NetFirewallRule -Name $IsolationRuleId -ErrorAction Stop
if (Get-NetFirewallRule -Name $IsolationRuleId -ErrorAction SilentlyContinue) {
    throw "Managed Ollama firewall rule was not removed"
}
if (Test-Path -LiteralPath $Paths.LaunchRecord) {
    Remove-Item -LiteralPath $Paths.LaunchRecord -Force -ErrorAction Stop
}
[ordered]@{
    stopped = $true
    runtime_instance_id = $RuntimeInstanceId
} | ConvertTo-Json -Compress
