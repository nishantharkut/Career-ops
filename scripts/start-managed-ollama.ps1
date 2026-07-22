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
    [string]$IsolationRuleId = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$IdentifierPattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
$ModelPattern = '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
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
        $RuntimeHandle -ne "pid:$ProcessId"
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
        [Parameter(Mandatory = $true)][string]$Endpoint
    )
    $Payload = [ordered]@{
        version = 1
        runtime_instance_id = $RuntimeInstanceId
        isolation_rule_id = $IsolationRuleId
        owned_executable = $Paths.OwnedExecutable
        endpoint = $Endpoint
        model = $Model
        process_id = $OwnedProcessId
        runtime_handle = $OwnedRuntimeHandle
        state = $State
    } | ConvertTo-Json -Compress
    $TemporaryRecord = "$($Paths.LaunchRecord).tmp-$([System.Guid]::NewGuid().ToString('N'))"
    $Bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Payload)
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
    if (Test-Path -LiteralPath $Paths.LaunchRecord) {
        [System.IO.File]::Replace($TemporaryRecord, $Paths.LaunchRecord, $null)
    }
    else {
        [System.IO.File]::Move($TemporaryRecord, $Paths.LaunchRecord)
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
        if (
            $Record.version -ne 1 -or
            $Record.runtime_instance_id -ne $Directory.Name -or
            $Record.runtime_instance_id -notmatch $IdentifierPattern -or
            $Record.isolation_rule_id -ne "CareerOps-Ollama-$($Directory.Name)" -or
            [System.IO.Path]::GetFullPath([string]$Record.owned_executable) -ne $ExpectedExecutable
        ) {
            throw "Managed runtime launch record identity was invalid"
        }
        $CandidateProcesses = @()
        if ([int]$Record.process_id -gt 0) {
            $CandidateProcesses = @(Get-Process -Id ([int]$Record.process_id) -ErrorAction SilentlyContinue)
        }
        else {
            $CandidateProcesses = @(Get-CimInstance Win32_Process | Where-Object {
                $null -ne $_.ExecutablePath -and
                [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq $ExpectedExecutable
            })
        }
        foreach ($Candidate in $CandidateProcesses) {
            $CandidatePath = if ($null -ne $Candidate.Path) {
                $Candidate.Path
            }
            else {
                $Candidate.ExecutablePath
            }
            if ([System.IO.Path]::GetFullPath($CandidatePath) -ne $ExpectedExecutable) {
                throw "Refusing to recover a process with a mismatched identity"
            }
            $CandidateId = if ($null -ne $Candidate.PSObject.Properties["ProcessId"]) {
                [int]$Candidate.ProcessId
            }
            else {
                [int]$Candidate.Id
            }
            Stop-Process -Id $CandidateId -Force -ErrorAction Stop
        }
        if (Get-NetFirewallRule -Name $Record.isolation_rule_id -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -Name $Record.isolation_rule_id -ErrorAction Stop
        }
        Remove-Item -LiteralPath $RecordPath -Force -ErrorAction Stop
        $Recovered += $Directory.Name
    }
    return $Recovered
}

function Assert-FirewallBoundary {
    param(
        [Parameter(Mandatory = $true)][string]$RuleName,
        [Parameter(Mandatory = $true)][string]$OwnedExecutable
    )
    $Rule = Get-NetFirewallRule -Name $RuleName -ErrorAction Stop
    if (
        $Rule.Enabled -ne "True" -or
        $Rule.Direction -ne "Outbound" -or
        $Rule.Action -ne "Block"
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
        [Parameter(Mandatory = $true)][string]$OwnedExecutable
    )
    $Process = Get-Process -Id $ProcessId -ErrorAction Stop
    if (
        [System.IO.Path]::GetFullPath($Process.Path) -ne $OwnedExecutable -or
        -not $OwnedExecutable.StartsWith(
            $Root + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Managed Ollama process identity did not match"
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
    $RuleCreated = $false
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

        $Endpoint = "http://127.0.0.1:$Port"
        Assert-PortUnbound
        Write-LaunchRecord `
            -Paths $Paths `
            -State "intent" `
            -OwnedProcessId 0 `
            -OwnedRuntimeHandle "" `
            -Endpoint $Endpoint

        New-NetFirewallRule `
            -Name $IsolationRuleId `
            -DisplayName $IsolationRuleId `
            -Direction Outbound `
            -Action Block `
            -Enabled True `
            -Profile Any `
            -Program $Paths.OwnedExecutable `
            -RemoteAddress $NonLoopbackRanges | Out-Null
        $RuleCreated = $true
        Assert-FirewallBoundary -RuleName $IsolationRuleId -OwnedExecutable $Paths.OwnedExecutable

        Write-LaunchRecord `
            -Paths $Paths `
            -State "firewall_ready" `
            -OwnedProcessId 0 `
            -OwnedRuntimeHandle "" `
            -Endpoint $Endpoint
        $env:OLLAMA_HOST = "127.0.0.1:$Port"
        $env:OLLAMA_NO_CLOUD = "1"
        $env:OLLAMA_MODELS = $ManagedModelsPath
        $env:NO_PROXY = "127.0.0.1,localhost"
        $env:no_proxy = "127.0.0.1,localhost"
        [string[]]$Arguments = @("serve")
        $Process = Start-Process `
            -FilePath $Paths.OwnedExecutable `
            -ArgumentList $Arguments `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Paths.InstanceRoot "stdout.log") `
            -RedirectStandardError (Join-Path $Paths.InstanceRoot "stderr.log")
        $ProcessId = $Process.Id
        $RuntimeHandle = "pid:$ProcessId"
        Write-LaunchRecord `
            -Paths $Paths `
            -State "launched" `
            -OwnedProcessId $ProcessId `
            -OwnedRuntimeHandle $RuntimeHandle `
            -Endpoint $Endpoint
        Assert-OwnedProcess -Root $Paths.InstanceRoot -OwnedExecutable $Paths.OwnedExecutable | Out-Null
        Assert-FirewallBoundary -RuleName $IsolationRuleId -OwnedExecutable $Paths.OwnedExecutable
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
            egress_policy = "os_firewall_block"
            egress_enforced = $true
            cloud_disabled = $true
            isolation_rule_id = $IsolationRuleId
        } | ConvertTo-Json -Compress
    }
    catch {
        if ($null -ne $Process -and -not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
        if ($RuleCreated -or (Get-NetFirewallRule -Name $IsolationRuleId -ErrorAction SilentlyContinue)) {
            Remove-NetFirewallRule -Name $IsolationRuleId -ErrorAction SilentlyContinue
        }
        if ($null -ne $Paths -and (Test-Path -LiteralPath $Paths.LaunchRecord)) {
            Remove-Item -LiteralPath $Paths.LaunchRecord -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    exit 0
}

Assert-IdentityParameters
$Paths = Get-InstancePaths -Root $ResolvedRuntimeRoot

if ($Action -eq "Verify") {
    Assert-OwnedProcess -Root $Paths.InstanceRoot -OwnedExecutable $Paths.OwnedExecutable | Out-Null
    Assert-FirewallBoundary -RuleName $IsolationRuleId -OwnedExecutable $Paths.OwnedExecutable
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

Assert-FirewallBoundary -RuleName $IsolationRuleId -OwnedExecutable $Paths.OwnedExecutable
$OwnedProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($null -ne $OwnedProcess) {
    if ([System.IO.Path]::GetFullPath($OwnedProcess.Path) -ne $Paths.OwnedExecutable) {
        throw "Refusing to stop a process with a mismatched identity"
    }
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
