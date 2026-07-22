from __future__ import annotations

from dataclasses import MISSING, replace
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

import apps.api.careerops_api.main as api_main
from apps.api.careerops_api.main import create_app
from careerops.ai.providers.base import ProviderUnavailableError
from careerops.ai.providers.ollama import OllamaModelIdentity, OllamaProvider
from careerops.core.repository import CareerOpsRepository
from careerops.core.status import PrivacyMode
from careerops.operations import container_runtime_inspector as inspector_module
from careerops.operations.ollama_runtime import (
    DockerComposeOllamaLauncher,
    ManagedOllamaRuntimeFactory,
    ManagedRuntimeConfigurationError,
    ManagedRuntimeIsolationError,
    ManagedRuntimeLaunchError,
    RuntimeLaunchRecord,
    WindowsPowerShellOllamaLauncher,
)


GOOD_DIGEST = "a" * 64
GOOD_TAGS_SHA = "b" * 64
GOOD_SHOW_SHA = "c" * 64
CONTAINER_IMAGE_C = "ollama/ollama@sha256:" + ("c" * 64)
CONTAINER_IMAGE_E = "ollama/ollama@sha256:" + ("e" * 64)
CONTAINER_IMAGE_ID = "sha256:" + ("d" * 64)
PROCESS_STARTED_AT = "2026-07-22T10:00:00.0000000Z"
PROCESS_EXECUTABLE_SHA256 = "e" * 64
PROCESS_EXECUTABLE_SIZE = 12_345_678
FIREWALL_RULE_TOKEN = "f" * 32


class FakeProvider:
    def __init__(self, **values):
        self.endpoint = values["endpoint"]
        self.model = values["model"]
        self.runtime_instance_id = values["runtime_instance_id"]
        self.runtime_handle = values["runtime_handle"]
        self.container_image_reference = values["container_image_reference"]
        self.container_image_id = values["container_image_id"]
        self.isolation_authority = values["isolation_authority"]
        self.isolation_evidence = values["isolation_evidence"]
        self.identity = OllamaModelIdentity(
            model=self.model,
            digest=GOOD_DIGEST,
            size=4_000_000_000,
            tags_metadata_sha256=GOOD_TAGS_SHA,
            show_metadata_sha256=GOOD_SHOW_SHA,
        )

    def compatibility_probe(self) -> OllamaModelIdentity:
        return self.identity


class FakeLauncher:
    def __init__(self):
        self.events: list[str] = []
        self.records: list[RuntimeLaunchRecord] = []
        self.cleaned: list[RuntimeLaunchRecord] = []
        self.verify_result = True
        self.next_record: RuntimeLaunchRecord | None = None
        self.launch_error: ManagedRuntimeLaunchError | None = None
        self.current: RuntimeLaunchRecord | None = None

    def launch(self, *, model: str, runtime_root: Path) -> RuntimeLaunchRecord:
        self.events.append("launch")
        if self.launch_error is not None:
            raise self.launch_error
        record = self.next_record or _record(
            model=model,
            sequence=len(self.records) + 1,
        )
        self.next_record = None
        self.records.append(record)
        self.current = record
        return record

    def verify(self, record: RuntimeLaunchRecord) -> bool:
        self.events.append("verify")
        return self.verify_result and self.current == record

    def cleanup(self, record: RuntimeLaunchRecord) -> None:
        self.events.append("cleanup")
        self.cleaned.append(record)
        if self.current == record:
            self.current = None


def _record(
    *,
    model: str = "qwen2.5:7b",
    sequence: int = 1,
    **updates,
) -> RuntimeLaunchRecord:
    values = {
        "endpoint": "http://127.0.0.1:11434",
        "model": model,
        "runtime_instance_id": f"runtime-{sequence}",
        "runtime_handle": f"pid:{4100 + sequence}",
        "handle_kind": "process",
        "process_id": 4100 + sequence,
        "container_id": None,
        "container_image_reference": None,
        "container_image_id": None,
        "process_started_at": PROCESS_STARTED_AT,
        "executable_sha256": PROCESS_EXECUTABLE_SHA256,
        "executable_size": PROCESS_EXECUTABLE_SIZE,
        "firewall_rule_token": FIREWALL_RULE_TOKEN,
        "egress_policy": "os_firewall_block",
        "egress_enforced": True,
        "cloud_disabled": True,
        "isolation_rule_id": f"CareerOps-Ollama-runtime-{sequence}",
    }
    values.update(updates)
    return RuntimeLaunchRecord(**values)


def _factory(tmp_path: Path, launcher: FakeLauncher | None = None):
    launcher = launcher or FakeLauncher()

    def provider_builder(**values):
        launcher.events.append("provider")
        return FakeProvider(**values)

    factory = ManagedOllamaRuntimeFactory(
        launcher=launcher,
        runtime_root=tmp_path / "managed-ollama",
        provider_builder=provider_builder,
    )
    return factory, launcher


def test_factory_launches_verifies_then_issues_authority_bound_capability(tmp_path: Path):
    factory, launcher = _factory(tmp_path)

    managed = factory.launch(model="qwen2.5:7b")

    assert launcher.events == ["launch", "verify", "provider"]
    assert managed.provider.endpoint == managed.endpoint
    assert managed.provider.model == managed.model
    assert managed.capability == managed.evidence
    assert managed.evidence.runtime_instance_id == managed.runtime_instance_id
    assert managed.evidence.runtime_handle == managed.runtime_handle
    assert managed.authority.verifies(
        managed.evidence,
        endpoint=managed.endpoint,
        model=managed.model,
        runtime_instance_id=managed.runtime_instance_id,
        runtime_handle=managed.runtime_handle,
        container_image_reference=None,
        container_image_id=None,
    )
    assert managed.model_identity.digest == GOOD_DIGEST
    assert managed.runtime_handle == f"pid:{managed.process_id}"
    assert factory.verify(managed) is True
    assert "_key" not in repr(managed.authority)
    assert GOOD_DIGEST not in repr(factory)


def test_factory_does_not_accept_environment_isolation_assertions(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CAREEROPS_OLLAMA_EGRESS_ENFORCED", "true")
    monkeypatch.setenv("CAREEROPS_OLLAMA_CLOUD_DISABLED", "true")
    monkeypatch.setenv("CAREEROPS_OLLAMA_RUNTIME_HANDLE", "pid:999")
    launcher = FakeLauncher()
    launcher.next_record = _record(egress_enforced=False)
    factory, _ = _factory(tmp_path, launcher)

    with pytest.raises(ManagedRuntimeIsolationError):
        factory.launch(model="qwen2.5:7b")

    assert launcher.cleaned == [launcher.records[0]]


@pytest.mark.parametrize(
    "record",
    [
        _record(endpoint="https://example.com:11434"),
        _record(endpoint="http://192.168.1.20:11434"),
        _record(model="other:7b"),
        _record(cloud_disabled=False),
        _record(egress_enforced=False),
    ],
)
def test_factory_rejects_external_mismatched_or_unenforced_runtime(
    tmp_path: Path, record: RuntimeLaunchRecord
):
    launcher = FakeLauncher()
    launcher.next_record = record
    factory, _ = _factory(tmp_path, launcher)

    with pytest.raises((ManagedRuntimeIsolationError, ManagedRuntimeConfigurationError)):
        factory.launch(model="qwen2.5:7b")

    assert launcher.cleaned == [launcher.records[0]]


@pytest.mark.parametrize(
    "model",
    [
        "",
        "../qwen",
        "qwen; Remove-Item C:/",
        "qwen $(whoami)",
        "qwen`calc`",
        "qwen@cloud",
        "https://ollama.com/model",
    ],
)
def test_factory_rejects_unsafe_or_cloud_model_names_before_launch(
    tmp_path: Path, model: str
):
    factory, launcher = _factory(tmp_path)

    with pytest.raises(ManagedRuntimeConfigurationError):
        factory.launch(model=model)

    assert launcher.events == []


def test_factory_rejects_unavailable_enforcement_and_cleans_runtime(tmp_path: Path):
    factory, launcher = _factory(tmp_path)
    launcher.verify_result = False

    with pytest.raises(ManagedRuntimeIsolationError):
        factory.launch(model="qwen2.5:7b")

    assert launcher.events == ["launch", "verify", "cleanup"]


@pytest.mark.parametrize(
    "identity",
    [
        OllamaModelIdentity("other:7b", GOOD_DIGEST, 1, GOOD_TAGS_SHA, GOOD_SHOW_SHA),
        OllamaModelIdentity("qwen2.5:7b", "bad", 1, GOOD_TAGS_SHA, GOOD_SHOW_SHA),
        OllamaModelIdentity("qwen2.5:7b", GOOD_DIGEST, 0, GOOD_TAGS_SHA, GOOD_SHOW_SHA),
        OllamaModelIdentity("qwen2.5:7b", GOOD_DIGEST, 1, "bad", GOOD_SHOW_SHA),
        OllamaModelIdentity("qwen2.5:7b", GOOD_DIGEST, 1, GOOD_TAGS_SHA, "bad"),
    ],
)
def test_factory_rejects_invalid_local_model_metadata_and_cleans(
    tmp_path: Path, identity: OllamaModelIdentity
):
    launcher = FakeLauncher()

    class BadMetadataProvider(FakeProvider):
        def compatibility_probe(self):
            return identity

    factory = ManagedOllamaRuntimeFactory(
        launcher=launcher,
        runtime_root=tmp_path / "runtime",
        provider_builder=BadMetadataProvider,
    )

    with pytest.raises(ManagedRuntimeIsolationError):
        factory.launch(model="qwen2.5:7b")

    assert launcher.cleaned == [launcher.records[0]]


def test_provider_construction_failure_cleans_process_and_isolation_rule(tmp_path: Path):
    launcher = FakeLauncher()

    def fail_provider(**_values):
        raise RuntimeError("injected provider construction failure")

    factory = ManagedOllamaRuntimeFactory(
        launcher=launcher,
        runtime_root=tmp_path / "runtime",
        provider_builder=fail_provider,
    )

    with pytest.raises(ManagedRuntimeLaunchError, match="provider"):
        factory.launch(model="qwen2.5:7b")

    assert launcher.cleaned == [launcher.records[0]]


def test_launch_error_with_concrete_record_is_cleaned(tmp_path: Path):
    launcher = FakeLauncher()
    record = _record()
    launcher.launch_error = ManagedRuntimeLaunchError("failed after process start", record=record)
    factory, _ = _factory(tmp_path, launcher)

    with pytest.raises(ManagedRuntimeLaunchError):
        factory.launch(model="qwen2.5:7b")

    assert launcher.cleaned == [record]


def test_restart_invalidates_old_capability_and_creates_new_identity(tmp_path: Path):
    factory, launcher = _factory(tmp_path)
    first = factory.launch(model="qwen2.5:7b")

    second = factory.restart(first)

    assert first.runtime_instance_id != second.runtime_instance_id
    assert first.runtime_handle != second.runtime_handle
    assert first.evidence.integrity != second.evidence.integrity
    assert launcher.cleaned == [first.launch_record]
    assert factory.verify(first) is False
    assert factory.verify(second) is True
    assert not second.authority.verifies(
        first.evidence,
        endpoint=second.endpoint,
        model=second.model,
        runtime_instance_id=second.runtime_instance_id,
        runtime_handle=second.runtime_handle,
        container_image_reference=None,
        container_image_id=None,
    )


def test_stale_mismatched_and_replayed_runtime_records_fail_closed(tmp_path: Path):
    factory, launcher = _factory(tmp_path)
    managed = factory.launch(model="qwen2.5:7b")
    launcher.current = replace(managed.launch_record, runtime_instance_id="runtime-replaced")

    assert factory.verify(managed) is False

    launcher.current = managed.launch_record
    factory.stop(managed)
    assert factory.verify(managed) is False
    assert launcher.cleaned == [managed.launch_record]


def test_runtime_root_must_be_app_owned_non_link_directory(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked-runtime"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    launcher = FakeLauncher()

    with pytest.raises(ManagedRuntimeConfigurationError):
        ManagedOllamaRuntimeFactory(
            launcher=launcher,
            runtime_root=linked,
            provider_builder=FakeProvider,
        )


def test_default_provider_type_is_the_hardened_ollama_adapter(tmp_path: Path):
    launcher = FakeLauncher()
    factory = ManagedOllamaRuntimeFactory(
        launcher=launcher,
        runtime_root=tmp_path / "runtime",
    )
    assert factory.provider_type is OllamaProvider


def test_windows_launcher_script_owns_hidden_process_and_firewall_boundary():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "start-managed-ollama.ps1"
    ).read_text(encoding="utf-8")

    assert "Mandatory = $true" in script
    assert "ProcessStartInfo" in script
    assert "EnvironmentVariables.Clear()" in script
    assert '$Info.Arguments = "serve"' in script
    assert "$Info.CreateNoWindow = $true" in script
    assert "ProcessWindowStyle]::Hidden" in script
    assert "Start-Process" in script
    assert "-WindowStyle Hidden" in script
    assert "Get-ChildItem Env:" in script
    assert "Remove-Item -LiteralPath \"Env:" in script
    assert "New-NetFirewallRule" in script
    assert "Get-NetFirewallRule" in script
    assert "Get-NetFirewallAddressFilter" in script
    assert 'Direction Outbound' in script
    assert 'Action Block' in script
    assert "RemoteAddress" in script
    assert "OLLAMA_NO_CLOUD" in script
    assert "OLLAMA_HOST" in script
    assert "ConvertTo-Json" in script
    assert "ProcessId" in script
    assert "RuntimeInstanceId" in script
    assert "RuntimeHandle" in script
    assert "Stop-Process" in script
    assert "Remove-NetFirewallRule" in script
    assert "Invoke-Expression" not in script
    assert "cmd.exe" not in script
    assert "capability" not in script.casefold()
    assert "hmac" not in script.casefold()
    assert "CAREEROPS_OLLAMA_EGRESS_ENFORCED" not in script
    assert "Invoke-RestMethod" not in script
    assert 'Join-Path $ResolvedRuntimeRoot "models"' in script
    assert '$Info.EnvironmentVariables["OLLAMA_MODELS"] = $ManagedModelsPath' in script
    assert "Get-NetTCPConnection" in script
    assert "OwningProcess" in script
    assert "Assert-EndpointOwnedByProcess" in script
    assert "Assert-PortUnbound" in script
    assert "launch-record.json" in script
    assert '"Recover"' in script
    assert '-State "intent"' in script
    assert script.index('-State "intent"') < script.index("New-NetFirewallRule")


def test_windows_launcher_uses_argument_lists_and_scrubs_ambient_ollama_state(
    tmp_path: Path, monkeypatch
):
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    script = tmp_path / "start.ps1"
    script.write_text("param()", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    monkeypatch.setenv("CAREEROPS_OLLAMA_EGRESS_ENFORCED", "true")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "0")
    monkeypatch.setenv("OLLAMA_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    monkeypatch.setenv("CAREEROPS_AUTH_PASSWORD", "must-not-reach-launcher")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-launcher")
    monkeypatch.setenv("CODEX_HOME", "C:\\private-codex-home")
    calls = []
    record_payload = {
        "endpoint": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "runtime_instance_id": "runtime-1",
        "runtime_handle": "pid:4101",
        "handle_kind": "process",
        "process_id": 4101,
        "container_id": None,
        "container_image_reference": None,
        "container_image_id": None,
        "process_started_at": PROCESS_STARTED_AT,
        "executable_sha256": PROCESS_EXECUTABLE_SHA256,
        "executable_size": PROCESS_EXECUTABLE_SIZE,
        "firewall_rule_token": FIREWALL_RULE_TOKEN,
        "egress_policy": "os_firewall_block",
        "egress_enforced": True,
        "cloud_disabled": True,
        "isolation_rule_id": "CareerOps-Ollama-runtime-1",
    }
    responses = iter(
        [
            record_payload,
            {"verified": True, "runtime_instance_id": "runtime-1", "runtime_handle": "pid:4101"},
            {"stopped": True, "runtime_instance_id": "runtime-1"},
        ]
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(next(responses)), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    launcher = WindowsPowerShellOllamaLauncher(
        executable_path=executable,
        powershell_executable="pwsh.exe",
        script_path=script,
    )

    record = launcher.launch(model="qwen2.5:7b", runtime_root=runtime_root)
    assert launcher.verify(record) is True
    launcher.cleanup(record)

    for command, kwargs in calls:
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        environment = kwargs["env"]
        assert not any(key.startswith("OLLAMA_") for key in environment)
        assert "CAREEROPS_OLLAMA_EGRESS_ENFORCED" not in environment
        assert "HTTPS_PROXY" not in environment
        assert "CAREEROPS_AUTH_PASSWORD" not in environment
        assert "OPENAI_API_KEY" not in environment
        assert "CODEX_HOME" not in environment
        assert set(environment).issubset(
            {
                "SystemRoot",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "PATH",
                "PSModulePath",
                "TEMP",
                "TMP",
                "NO_PROXY",
                "no_proxy",
            }
        )
        assert environment["NO_PROXY"] == "127.0.0.1,localhost"


def test_record_requires_concrete_process_or_container_identity():
    with pytest.raises(ManagedRuntimeConfigurationError):
        _record(handle_kind="process", process_id=None)
    with pytest.raises(ManagedRuntimeConfigurationError):
        _record(
            handle_kind="container",
            process_id=None,
            container_id="short",
            runtime_handle="container:short",
            egress_policy="container_internal_no_egress",
        )


def test_runtime_record_requires_explicit_container_image_identity_fields():
    fields = RuntimeLaunchRecord.__dataclass_fields__

    assert fields["container_image_reference"].default is MISSING
    assert fields["container_image_id"].default is MISSING
    assert fields["process_started_at"].default is MISSING
    assert fields["executable_sha256"].default is MISSING
    assert fields["executable_size"].default is MISSING
    assert fields["firewall_rule_token"].default is MISSING


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is unavailable",
)
def test_windows_child_process_environment_is_an_explicit_secret_free_allowlist(
    tmp_path: Path,
):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "start-managed-ollama.ps1"
    )
    harness = tmp_path / "environment-harness.ps1"
    harness.write_text(
        r'''
param([string]$ScriptPath, [string]$Executable, [string]$Models)
. $ScriptPath -Action Recover -ExecutablePath $Executable -Model "qwen2.5:7b" -RuntimeRoot $Models
$env:CAREEROPS_AUTH_PASSWORD = "must-not-reach-ollama"
$env:OPENAI_API_KEY = "must-not-reach-ollama"
$env:CODEX_HOME = "must-not-reach-ollama"
$Info = New-OllamaProcessStartInfo -OwnedExecutable $Executable -ManagedModelsPath $Models -OwnedPort 11434
$Observed = @{}
foreach ($Name in $Info.EnvironmentVariables.Keys) { $Observed[$Name] = $Info.EnvironmentVariables[$Name] }
$Expected = @("OLLAMA_HOST", "OLLAMA_NO_CLOUD", "OLLAMA_MODELS", "NO_PROXY", "no_proxy", "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
if (@($Observed.Keys | Where-Object { $_ -notin $Expected }).Count -ne 0) { throw "unexpected inherited environment" }
if ($Observed.ContainsKey("CAREEROPS_AUTH_PASSWORD") -or $Observed.ContainsKey("OPENAI_API_KEY") -or $Observed.ContainsKey("CODEX_HOME")) { throw "secret inherited" }
if ($Observed["OLLAMA_NO_CLOUD"] -ne "1" -or $Observed["OLLAMA_HOST"] -ne "127.0.0.1:11434") { throw "managed environment missing" }
Set-OllamaProcessEnvironment -Variables $Info.EnvironmentVariables
$Applied = @{}
foreach ($Variable in Get-ChildItem Env:) { $Applied[$Variable.Name] = $Variable.Value }
if (@($Applied.Keys | Where-Object { $_ -notin $Expected }).Count -ne 0) { throw "applied environment inherited unexpected state" }
if ($Applied.ContainsKey("CAREEROPS_AUTH_PASSWORD") -or $Applied.ContainsKey("OPENAI_API_KEY") -or $Applied.ContainsKey("CODEX_HOME")) { throw "applied environment inherited secret" }
$Observed | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    models = tmp_path / "models"
    models.mkdir()

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-ScriptPath",
            str(script),
            "-Executable",
            str(executable),
            "-Models",
            str(models),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_windows_launcher_recovers_durable_launch_record_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    script = tmp_path / "start.ps1"
    script.write_text("param()", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 0.01)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"recovered": True, "runtime_instances": ["runtime-timeout"]}
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    launcher = WindowsPowerShellOllamaLauncher(
        executable_path=executable,
        powershell_executable="pwsh.exe",
        script_path=script,
        timeout_seconds=0.01,
    )

    with pytest.raises(ManagedRuntimeLaunchError, match="did not complete"):
        launcher.launch(model="qwen2.5:7b", runtime_root=runtime_root)

    assert len(calls) == 2
    assert calls[1][calls[1].index("-Action") + 1] == "Recover"
    assert calls[1][calls[1].index("-RuntimeRoot") + 1] == str(runtime_root)


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is unavailable",
)
def test_windows_recovery_state_machine_removes_intent_and_launched_resources(
    tmp_path: Path,
):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "start-managed-ollama.ps1"
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    fixture_executable = tmp_path / "fixture-ollama.exe"
    fixture_executable.write_bytes(b"fixture")

    executable_digest = hashlib.sha256(b"owned").hexdigest()
    for instance, process_id, state, rule_token in (
        ("runtime-intent", 0, "intent", "1" * 32),
        ("runtime-launched", 4201, "launched", "2" * 32),
        ("runtime-starting", 0, "firewall_ready", "4" * 32),
        ("runtime-intent-started", 0, "intent", "5" * 32),
    ):
        instance_root = runtime_root / instance
        owned_executable = instance_root / "bin" / "ollama.exe"
        owned_executable.parent.mkdir(parents=True)
        owned_executable.write_bytes(b"owned")
        (instance_root / "launch-record.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "runtime_instance_id": instance,
                    "isolation_rule_id": f"CareerOps-Ollama-{instance}",
                    "owned_executable": str(owned_executable),
                    "endpoint": "http://127.0.0.1:11434",
                    "model": "qwen2.5:7b",
                    "process_id": process_id,
                    "runtime_handle": f"pid:{process_id}" if process_id else "",
                    "process_started_at": PROCESS_STARTED_AT if process_id else "",
                    "executable_sha256": executable_digest,
                    "executable_size": len(b"owned"),
                    "firewall_rule_token": rule_token,
                    "state": state,
                }
            ),
            encoding="utf-8",
        )

    harness = tmp_path / "recovery-harness.ps1"
    harness.write_text(
        r'''
param([string]$ScriptPath, [string]$Root, [string]$FixtureExecutable)
. $ScriptPath -Action Recover -ExecutablePath $FixtureExecutable -Model "qwen2.5:7b" -RuntimeRoot $Root
$script:HarnessRoot = $Root
$script:RemovedRules = @()
$script:StoppedProcesses = @()
$script:AliveProcesses = @{ 4201 = $true; 4202 = $true; 4203 = $true }
$script:ListenerOwner = 0
$script:Rules = @{
    "CareerOps-Ollama-runtime-intent" = [pscustomobject]@{ Name = "CareerOps-Ollama-runtime-intent"; DisplayName = "CareerOps-Ollama-runtime-intent"; Description = "CareerOps managed Ollama runtime 11111111111111111111111111111111"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" }
    "CareerOps-Ollama-runtime-launched" = [pscustomobject]@{ Name = "CareerOps-Ollama-runtime-launched"; DisplayName = "CareerOps-Ollama-runtime-launched"; Description = "CareerOps managed Ollama runtime 22222222222222222222222222222222"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" }
    "CareerOps-Ollama-runtime-starting" = [pscustomobject]@{ Name = "CareerOps-Ollama-runtime-starting"; DisplayName = "CareerOps-Ollama-runtime-starting"; Description = "CareerOps managed Ollama runtime 44444444444444444444444444444444"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" }
    "CareerOps-Ollama-runtime-intent-started" = [pscustomobject]@{ Name = "CareerOps-Ollama-runtime-intent-started"; DisplayName = "CareerOps-Ollama-runtime-intent-started"; Description = "CareerOps managed Ollama runtime 55555555555555555555555555555555"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" }
    "CareerOps-Ollama-foreign" = [pscustomobject]@{ Name = "CareerOps-Ollama-foreign"; DisplayName = "Foreign"; Description = "not owned"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" }
}
function Get-NetFirewallRule {
    [CmdletBinding()]
    param([string]$Name)
    return $script:Rules[$Name]
}
function Remove-NetFirewallRule {
    [CmdletBinding()]
    param([string]$Name)
    $script:RemovedRules += $Name
    $script:Rules.Remove($Name)
}
function Get-NetFirewallApplicationFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        $Instance = $InputObject.Name.Replace("CareerOps-Ollama-", "")
        [pscustomobject]@{ Program = (Join-Path $script:HarnessRoot "$Instance/bin/ollama.exe") }
    }
}
function Get-NetFirewallAddressFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process { [pscustomobject]@{ RemoteAddress = $NonLoopbackRanges } }
}
function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    if ($Id -in @(4201, 4202, 4203) -and $script:AliveProcesses[$Id]) {
        $Instance = switch ($Id) { 4201 { "runtime-launched" } 4202 { "runtime-starting" } 4203 { "runtime-intent-started" } }
        $Process = [pscustomobject]@{
            Id = $Id
            Path = (Join-Path $script:HarnessRoot "$Instance/bin/ollama.exe")
            StartTime = [DateTime]::ParseExact("2026-07-22T10:00:00.0000000Z", "o", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
        }
        $Process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
            param([int]$Milliseconds)
            $script:AliveProcesses[[int]$this.Id] = $false
            return $true
        }
        return $Process
    }
    return $null
}
function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName)
    $Observed = @()
    if ($script:AliveProcesses[4202]) {
        $Observed += [pscustomobject]@{ ProcessId = 4202; ExecutablePath = (Join-Path $script:HarnessRoot "runtime-starting/bin/ollama.exe") }
    }
    if ($script:AliveProcesses[4203]) {
        $Observed += [pscustomobject]@{ ProcessId = 4203; ExecutablePath = (Join-Path $script:HarnessRoot "runtime-intent-started/bin/ollama.exe") }
    }
    return $Observed
}
function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$Force)
    $script:StoppedProcesses += $Id
}
function Get-NetTCPConnection {
    [CmdletBinding()]
    param([string]$State, [int]$LocalPort)
    if ($script:ListenerOwner -eq 0) { return @() }
    return [pscustomobject]@{
        OwningProcess = $script:ListenerOwner
        LocalAddress = "127.0.0.1"
    }
}
$Recovered = @(Recover-PendingLaunches -Root $Root | Sort-Object)
if (($Recovered -join ",") -ne "runtime-intent,runtime-intent-started,runtime-launched,runtime-starting") { throw "recovery set mismatch" }
if (($script:RemovedRules | Sort-Object) -join "," -ne "CareerOps-Ollama-runtime-intent,CareerOps-Ollama-runtime-intent-started,CareerOps-Ollama-runtime-launched,CareerOps-Ollama-runtime-starting") { throw "firewall cleanup mismatch" }
if (($script:StoppedProcesses | Sort-Object) -join "," -ne "4201,4202,4203") { throw "process cleanup mismatch" }
if (@(Get-ChildItem -LiteralPath $Root -Filter "launch-record.json" -Recurse).Count -ne 0) { throw "launch records remained" }
$script:ListenerOwner = 4201
Assert-EndpointOwnedByProcess -OwnedProcessId 4201
$MismatchRejected = $false
$script:ListenerOwner = 4202
try { Assert-EndpointOwnedByProcess -OwnedProcessId 4201 } catch { $MismatchRejected = $true }
if (-not $MismatchRejected) { throw "listener owner mismatch was accepted" }
$PreexistingRejected = $false
try { Assert-PortUnbound } catch { $PreexistingRejected = $true }
if (-not $PreexistingRejected) { throw "preexisting listener was accepted" }
$CollisionRejected = $false
try { Assert-FirewallRuleNameAvailable -RuleName "CareerOps-Ollama-foreign" } catch { $CollisionRejected = $true }
if (-not $CollisionRejected -or $script:RemovedRules -contains "CareerOps-Ollama-foreign") { throw "foreign firewall rule collision was touched" }
@{ recovered = $Recovered; removed_rules = $script:RemovedRules; stopped_processes = $script:StoppedProcesses; ownership_rejected = $MismatchRejected; preexisting_rejected = $PreexistingRejected; collision_rejected = $CollisionRejected } | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-ScriptPath",
            str(script),
            "-Root",
            str(runtime_root),
            "-FixtureExecutable",
            str(fixture_executable),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert sorted(result["stopped_processes"]) == [4201, 4202, 4203]
    assert result["ownership_rejected"] is True
    assert result["preexisting_rejected"] is True
    assert result["collision_rejected"] is True
    assert sorted(result["removed_rules"]) == [
        "CareerOps-Ollama-runtime-intent",
        "CareerOps-Ollama-runtime-intent-started",
        "CareerOps-Ollama-runtime-launched",
        "CareerOps-Ollama-runtime-starting",
    ]


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is unavailable",
)
def test_windows_recovery_preserves_journal_when_firewall_ownership_mismatches(
    tmp_path: Path,
):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    script = Path(__file__).resolve().parents[1] / "scripts" / "start-managed-ollama.ps1"
    root = tmp_path / "runtime"
    instance = root / "runtime-mismatch"
    owned = instance / "bin" / "ollama.exe"
    owned.parent.mkdir(parents=True)
    owned.write_bytes(b"owned")
    record = instance / "launch-record.json"
    record.write_text(
        json.dumps(
            {
                "version": 2,
                "runtime_instance_id": "runtime-mismatch",
                "isolation_rule_id": "CareerOps-Ollama-runtime-mismatch",
                "owned_executable": str(owned),
                "endpoint": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
                "process_id": 0,
                "runtime_handle": "",
                "process_started_at": "",
                "executable_sha256": hashlib.sha256(b"owned").hexdigest(),
                "executable_size": 5,
                "firewall_rule_token": "3" * 32,
                "state": "intent",
            }
        ),
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"fixture")
    harness = tmp_path / "mismatch-harness.ps1"
    harness.write_text(
        r'''
param([string]$ScriptPath, [string]$Root, [string]$Fixture)
. $ScriptPath -Action Recover -ExecutablePath $Fixture -Model "qwen2.5:7b" -RuntimeRoot $Root
$script:Removed = $false
function Get-NetFirewallRule { [CmdletBinding()] param([string]$Name) [pscustomobject]@{ Name = $Name; DisplayName = $Name; Description = "foreign"; Enabled = "True"; Direction = "Outbound"; Action = "Block"; Profile = "Any" } }
function Remove-NetFirewallRule { [CmdletBinding()] param([string]$Name) $script:Removed = $true }
$Rejected = $false
try { Recover-PendingLaunches -Root $Root | Out-Null } catch { $Rejected = $true }
$RecordExists = Test-Path -LiteralPath (Join-Path $Root "runtime-mismatch/launch-record.json")
if (-not $Rejected -or -not $RecordExists -or $script:Removed) { throw "unsafe recovery state" }
@{ rejected = $Rejected; record_exists = $RecordExists; removed = $script:Removed } | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-ScriptPath",
            str(script),
            "-Root",
            str(root),
            "-Fixture",
            str(fixture),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "record_exists": True,
        "rejected": True,
        "removed": False,
    }


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is unavailable",
)
@pytest.mark.parametrize(
    ("scenario", "wait_result", "expect_failure", "expect_stop"),
    [
        ("direct", False, True, True),
        ("recovery", False, True, True),
        ("direct", True, False, True),
        ("pid_reuse", True, True, False),
    ],
)
def test_windows_cleanup_removes_isolation_only_after_verified_process_exit(
    tmp_path: Path,
    scenario: str,
    wait_result: bool,
    expect_failure: bool,
    expect_stop: bool,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    script = Path(__file__).resolve().parents[1] / "scripts" / "start-managed-ollama.ps1"
    root = tmp_path / "runtime"
    instance = root / "runtime-cleanup"
    owned = instance / "bin" / "ollama.exe"
    owned.parent.mkdir(parents=True)
    owned.write_bytes(b"owned-cleanup")
    started_at = "2026-07-22T11:00:00.0000000Z"
    digest = hashlib.sha256(b"owned-cleanup").hexdigest()
    record = instance / "launch-record.json"
    record.write_text(
        json.dumps(
            {
                "version": 2,
                "runtime_instance_id": "runtime-cleanup",
                "isolation_rule_id": "CareerOps-Ollama-runtime-cleanup",
                "owned_executable": str(owned),
                "endpoint": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
                "process_id": 4301,
                "runtime_handle": "pid:4301",
                "process_started_at": started_at,
                "executable_sha256": digest,
                "executable_size": len(b"owned-cleanup"),
                "firewall_rule_token": "6" * 32,
                "state": "launched",
            }
        ),
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"fixture")
    harness = tmp_path / "cleanup-harness.ps1"
    harness.write_text(
        r'''
param(
    [string]$ScriptPath,
    [string]$Root,
    [string]$Fixture,
    [string]$Scenario,
    [string]$WaitResult
)
. $ScriptPath -Action Recover -ExecutablePath $Fixture -Model "qwen2.5:7b" -RuntimeRoot $Root
$script:HarnessRoot = $Root
$script:Alive = $true
$script:WaitSucceeds = $WaitResult -eq "true"
$script:PidReused = $Scenario -eq "pid_reuse"
$script:RuleExists = $true
$script:StopCalled = $false
$RuleName = "CareerOps-Ollama-runtime-cleanup"
$OwnedExecutable = Join-Path $Root "runtime-cleanup/bin/ollama.exe"
$RecordPath = Join-Path $Root "runtime-cleanup/launch-record.json"
$StartedAt = "2026-07-22T11:00:00.0000000Z"
$Digest = (Get-FileHash -LiteralPath $OwnedExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
$Size = (Get-Item -LiteralPath $OwnedExecutable).Length
function New-FakeOwnedProcess {
    $Process = [pscustomobject]@{
        Id = 4301
        Path = $script:OwnedExecutable
        StartTime = [DateTime]::ParseExact(
            "2026-07-22T11:00:00.0000000Z",
            "o",
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    $Process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
        param([int]$Milliseconds)
        if ($script:WaitSucceeds) { $script:Alive = $false }
        return $script:WaitSucceeds
    }
    return $Process
}
$script:OwnedExecutable = $OwnedExecutable
function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    if ($Id -eq 4301 -and $script:Alive -and $script:PidReused) {
        return [pscustomobject]@{
            Id = 4301
            Path = $Fixture
            StartTime = [DateTime]::ParseExact(
                "2026-07-22T11:01:00.0000000Z",
                "o",
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind
            )
        }
    }
    if ($Id -eq 4301 -and $script:Alive) { return New-FakeOwnedProcess }
    return $null
}
function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName)
    if ($script:Alive) {
        return [pscustomobject]@{ ProcessId = 4301; ExecutablePath = $script:OwnedExecutable }
    }
    return @()
}
function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$Force)
    $script:StopCalled = $true
}
function Get-NetTCPConnection {
    [CmdletBinding()]
    param([string]$State, [int]$LocalPort)
    if ($script:Alive) {
        return [pscustomobject]@{ OwningProcess = 4301; LocalAddress = "127.0.0.1" }
    }
    return @()
}
function Get-NetFirewallRule {
    [CmdletBinding()]
    param([string]$Name)
    if ($script:RuleExists) {
        return [pscustomobject]@{
            Name = $Name
            DisplayName = $Name
            Description = "CareerOps managed Ollama runtime 66666666666666666666666666666666"
            Enabled = "True"
            Direction = "Outbound"
            Action = "Block"
            Profile = "Any"
        }
    }
    return $null
}
function Get-NetFirewallApplicationFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process { [pscustomobject]@{ Program = $script:OwnedExecutable } }
}
function Get-NetFirewallAddressFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process { [pscustomobject]@{ RemoteAddress = $NonLoopbackRanges } }
}
function Remove-NetFirewallRule {
    [CmdletBinding()]
    param([string]$Name)
    $script:RuleExists = $false
}
$Failed = $false
try {
    if ($Scenario -eq "recovery") {
        Recover-PendingLaunches -Root $Root | Out-Null
    }
    else {
        Complete-ManagedRuntimeCleanup `
            -Root (Join-Path $Root "runtime-cleanup") `
            -OwnedExecutable $OwnedExecutable `
            -OwnedProcessId 4301 `
            -ExpectedStartedAt $StartedAt `
            -ExpectedExecutableSha256 $Digest `
            -ExpectedExecutableSize $Size `
            -RuleName $RuleName `
            -RuleToken "66666666666666666666666666666666" `
            -RecordPath $RecordPath
    }
}
catch { $Failed = $true }
[ordered]@{
    failed = $Failed
    alive = $script:Alive
    stop_called = $script:StopCalled
    rule_exists = $script:RuleExists
    record_exists = (Test-Path -LiteralPath $RecordPath)
} | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-ScriptPath",
            str(script),
            "-Root",
            str(root),
            "-Fixture",
            str(fixture),
            "-Scenario",
            scenario,
            "-WaitResult",
            str(wait_result).lower(),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["failed"] is expect_failure
    assert result["stop_called"] is expect_stop
    assert result["alive"] is expect_failure
    assert result["rule_exists"] is expect_failure
    assert result["record_exists"] is expect_failure


def _container_record(*, model: str = "qwen2.5:7b") -> RuntimeLaunchRecord:
    container_id = "a" * 64
    return RuntimeLaunchRecord(
        endpoint="http://ollama:11434",
        model=model,
        runtime_instance_id="runtime-compose-1",
        runtime_handle=f"container:{container_id}",
        handle_kind="container",
        process_id=None,
        container_id=container_id,
        container_image_reference=CONTAINER_IMAGE_C,
        container_image_id=CONTAINER_IMAGE_ID,
        process_started_at=None,
        executable_sha256=None,
        executable_size=None,
        firewall_rule_token=None,
        egress_policy="container_internal_no_egress",
        egress_enforced=True,
        cloud_disabled=True,
        isolation_rule_id="network-" + ("b" * 32),
    )


def _compose_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "CAREEROPS_DB_PATH": str(tmp_path / "data" / "careerops.db"),
        "CAREEROPS_VAULT_PATH": str(tmp_path / "vault"),
        "CAREEROPS_OLLAMA_URL": "http://ollama:11434",
        "CAREEROPS_OLLAMA_MODEL": "qwen2.5:7b",
        "CAREEROPS_OLLAMA_MANAGED_MODE": "container",
        "CAREEROPS_CODEX_ENABLED": "false",
    }


def _local_model_response(_self, method, path, _body, _deadline):
    if method == "GET" and path == "/api/tags":
        return {
            "models": [
                {
                    "name": "qwen2.5:7b",
                    "model": "qwen2.5:7b",
                    "digest": GOOD_DIGEST,
                    "size": 4_000_000_000,
                }
            ]
        }
    if method == "POST" and path == "/api/show":
        return {"details": {"family": "qwen2"}, "capabilities": ["completion"]}
    raise AssertionError(f"unexpected Ollama request: {method} {path}")


def test_production_lifespan_enables_only_verified_compose_managed_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    monkeypatch.setattr(OllamaProvider, "_request_json", _local_model_response)

    api = create_app(
        repo,
        environment=_compose_environment(tmp_path),
        managed_runtime_launcher=launcher,
    )
    with TestClient(api) as client:
        providers = client.get("/ai/providers").json()
        ollama = next(item for item in providers if item["provider_kind"] == "ollama")
        managed = api.state.managed_ollama_runtime

        assert ollama["enabled"] is True
        assert ollama["available"] is True
        assert ollama["privacy_mode"] == PrivacyMode.LOCAL_PRIVATE.value
        assert ollama["endpoint"] == "http://ollama:11434"
        assert ollama["current_metadata"]["container_image_reference"] == (
            CONTAINER_IMAGE_C
        )
        assert ollama["current_metadata"]["container_image_id"] == CONTAINER_IMAGE_ID
        assert managed.provider is api.state.provider_registry[ollama["id"]]
        assert api.state.ai_orchestrator._providers[ollama["id"]] is managed.provider
        assert managed.authority.verifies(
            managed.evidence,
            endpoint=managed.endpoint,
            model=managed.model,
            runtime_instance_id=managed.runtime_instance_id,
            runtime_handle=managed.runtime_handle,
            container_image_reference=CONTAINER_IMAGE_C,
            container_image_id=CONTAINER_IMAGE_ID,
        )
        assert managed.evidence.container_image_reference == CONTAINER_IMAGE_C
        assert managed.evidence.container_image_id == CONTAINER_IMAGE_ID
        assert launcher.events[:2] == ["launch", "verify"]

    assert launcher.cleaned == []


def test_production_managed_provider_reverifies_live_runtime_before_probe_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    ollama_calls: list[tuple[str, str, bytes | None]] = []

    def local_model_response(self, method: str, path: str, body, deadline):
        ollama_calls.append((method, path, body))
        return _local_model_response(self, method, path, body, deadline)

    monkeypatch.setattr(OllamaProvider, "_request_json", local_model_response)
    api = create_app(
        repo,
        environment=_compose_environment(tmp_path),
        managed_runtime_launcher=launcher,
    )

    with TestClient(api) as client:
        managed = api.state.managed_ollama_runtime
        launcher.events.clear()
        ollama_calls.clear()
        launcher.current = replace(
            managed.launch_record,
            isolation_rule_id="network-replaced-after-startup",
        )

        ollama = next(
            item
            for item in client.get("/ai/providers").json()
            if item["provider_kind"] == "ollama"
        )

        assert ollama["available"] is False
        assert launcher.events == ["verify"]
        assert ollama_calls == []
        with pytest.raises(ProviderUnavailableError, match="isolation"):
            managed.provider.run(object())
        assert launcher.events == ["verify", "verify"]
        assert ollama_calls == []


def test_production_managed_provider_rejects_exact_container_image_digest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    ollama_calls: list[tuple[str, str, bytes | None]] = []

    def local_model_response(self, method: str, path: str, body, deadline):
        ollama_calls.append((method, path, body))
        return _local_model_response(self, method, path, body, deadline)

    monkeypatch.setattr(OllamaProvider, "_request_json", local_model_response)
    api = create_app(
        repo,
        environment=_compose_environment(tmp_path),
        managed_runtime_launcher=launcher,
    )

    with TestClient(api) as client:
        managed = api.state.managed_ollama_runtime
        launcher.events.clear()
        ollama_calls.clear()
        launcher.current = replace(
            managed.launch_record,
            container_image_reference=CONTAINER_IMAGE_E,
        )

        ollama = next(
            item
            for item in client.get("/ai/providers").json()
            if item["provider_kind"] == "ollama"
        )

        assert ollama["available"] is False
        assert launcher.events == ["verify"]
        assert ollama_calls == []
        with pytest.raises(ProviderUnavailableError, match="isolation"):
            managed.provider.run(object())
        assert launcher.events == ["verify", "verify"]
        assert ollama_calls == []


def test_production_lifespan_keeps_invalid_managed_runtime_disabled_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    launcher.verify_result = False

    def reject_network(*_args, **_kwargs):
        raise AssertionError("invalid managed evidence must not reach Ollama")

    monkeypatch.setattr(OllamaProvider, "_request_json", reject_network)
    api = create_app(
        repo,
        environment=_compose_environment(tmp_path),
        managed_runtime_launcher=launcher,
    )

    with TestClient(api) as client:
        providers = client.get("/ai/providers").json()
        ollama = next(item for item in providers if item["provider_kind"] == "ollama")

        assert ollama["enabled"] is False
        assert ollama["available"] is False
        assert ollama["privacy_mode"] == PrivacyMode.CLOUD_EXTERNAL.value
        assert api.state.managed_ollama_runtime is None
        assert launcher.cleaned == [launcher.records[0]]


def test_production_lifespan_rejects_arbitrary_managed_endpoint_before_launch(
    tmp_path: Path,
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    environment = _compose_environment(tmp_path)
    environment["CAREEROPS_OLLAMA_URL"] = "https://external.example:11434"

    api = create_app(
        repo,
        environment=environment,
        managed_runtime_launcher=launcher,
    )
    with TestClient(api) as client:
        ollama = next(
            item
            for item in client.get("/ai/providers").json()
            if item["provider_kind"] == "ollama"
        )

        assert ollama["enabled"] is False
        assert ollama["available"] is False
        assert launcher.events == []


def test_production_startup_failure_cleans_verified_managed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    monkeypatch.setattr(OllamaProvider, "_request_json", _local_model_response)
    monkeypatch.setattr(api_main.AiTaskRunner, "start", lambda _self: False)
    api = create_app(
        repo,
        environment=_compose_environment(tmp_path),
        managed_runtime_launcher=launcher,
    )

    with pytest.raises(RuntimeError, match="task runner"):
        with TestClient(api):
            pass

    assert launcher.cleaned == [launcher.records[0]]


def test_unmanaged_compose_endpoint_cannot_claim_local_private_or_probe_network(
    monkeypatch: pytest.MonkeyPatch,
):
    def reject_network(*_args, **_kwargs):
        raise AssertionError("unmanaged container endpoint must not be contacted")

    monkeypatch.setattr(OllamaProvider, "_request_json", reject_network)
    provider = OllamaProvider(endpoint="http://ollama:11434", model="qwen2.5:7b")

    availability = provider.availability()

    assert availability.available is False
    assert availability.privacy_mode is PrivacyMode.CLOUD_EXTERNAL
    assert availability.data_leaves_machine is True
    with pytest.raises(ProviderUnavailableError, match="isolation"):
        provider.compatibility_probe()


def test_production_lifespan_selects_compose_inspector_without_test_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    launcher.next_record = _container_record()
    monkeypatch.setattr(OllamaProvider, "_request_json", _local_model_response)
    monkeypatch.setattr(api_main, "DockerComposeOllamaLauncher", lambda: launcher)

    api = create_app(repo, environment=_compose_environment(tmp_path))
    with TestClient(api) as client:
        ollama = next(
            item
            for item in client.get("/ai/providers").json()
            if item["provider_kind"] == "ollama"
        )

        assert ollama["enabled"] is True
        assert launcher.events[:2] == ["launch", "verify"]


def test_production_windows_process_mode_stops_owned_runtime_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "data" / "careerops.db", tmp_path / "vault")
    repo.initialize()
    launcher = FakeLauncher()
    monkeypatch.setattr(OllamaProvider, "_request_json", _local_model_response)
    environment = _compose_environment(tmp_path)
    environment.update(
        {
            "CAREEROPS_OLLAMA_MANAGED_MODE": "windows_process",
            "CAREEROPS_OLLAMA_URL": "http://127.0.0.1:11434",
            "CAREEROPS_OLLAMA_RUNTIME_ROOT": str(tmp_path / "managed-runtime"),
        }
    )
    api = create_app(
        repo,
        environment=environment,
        managed_runtime_launcher=launcher,
    )

    with TestClient(api) as client:
        ollama = next(
            item
            for item in client.get("/ai/providers").json()
            if item["provider_kind"] == "ollama"
        )
        assert ollama["enabled"] is True
        assert launcher.cleaned == []

    assert launcher.cleaned == [launcher.records[0]]


def test_compose_launcher_reverifies_identity_and_uses_bounded_inspector_actions(
    tmp_path: Path,
):
    payload = {
        "endpoint": "http://ollama:11434",
        "model": "qwen2.5:7b",
        "runtime_instance_id": "runtime-compose-1",
        "runtime_handle": "container:" + ("a" * 64),
        "handle_kind": "container",
        "process_id": None,
        "container_id": "a" * 64,
        "container_image_reference": CONTAINER_IMAGE_C,
        "container_image_id": CONTAINER_IMAGE_ID,
        "process_started_at": None,
        "executable_sha256": None,
        "executable_size": None,
        "firewall_rule_token": None,
        "egress_policy": "container_internal_no_egress",
        "egress_enforced": True,
        "cloud_disabled": True,
        "isolation_rule_id": "network-" + ("b" * 32),
    }
    calls: list[tuple[str, str, object]] = []

    def inspector(method: str, path: str, body):
        calls.append((method, path, body))
        return payload

    launcher = DockerComposeOllamaLauncher(transport=inspector)

    record = launcher.launch(model="qwen2.5:7b", runtime_root=tmp_path)
    assert launcher.verify(record) is True
    launcher.cleanup(record)

    assert calls == [
        ("GET", "/v1/ollama-runtime?model=qwen2.5%3A7b", None),
        ("GET", "/v1/ollama-runtime?model=qwen2.5%3A7b", None),
    ]


def test_container_inspector_rejects_all_mutating_docker_requests(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        inspector_module,
        "_validated_socket",
        lambda: (_ for _ in ()).throw(AssertionError("socket must not be opened")),
        raising=False,
    )

    with pytest.raises(inspector_module.InspectorError, match="invalid"):
        inspector_module._docker_request("POST", "/containers/abc/stop")


def test_compose_launcher_rejects_exact_image_digest_drift_after_launch(
    tmp_path: Path,
):
    payload = {
        "endpoint": "http://ollama:11434",
        "model": "qwen2.5:7b",
        "runtime_instance_id": "runtime-compose-image-drift",
        "runtime_handle": "container:" + ("a" * 64),
        "handle_kind": "container",
        "process_id": None,
        "container_id": "a" * 64,
        "container_image_reference": CONTAINER_IMAGE_C,
        "container_image_id": CONTAINER_IMAGE_ID,
        "process_started_at": None,
        "executable_sha256": None,
        "executable_size": None,
        "firewall_rule_token": None,
        "egress_policy": "container_internal_no_egress",
        "egress_enforced": True,
        "cloud_disabled": True,
        "isolation_rule_id": "network-" + ("b" * 32),
    }

    launcher = DockerComposeOllamaLauncher(
        transport=lambda _method, _path, _body: dict(payload)
    )
    record = launcher.launch(model="qwen2.5:7b", runtime_root=tmp_path)
    payload["container_image_reference"] = CONTAINER_IMAGE_E

    assert launcher.verify(record) is False


def _inspector_fixtures() -> dict[str, object]:
    container_id = "a" * 64
    network_id = "b" * 64
    return {
        "self": {
            "Config": {
                "Labels": {"com.docker.compose.project": "careerops"}
            }
        },
        "containers": [{"Id": container_id}],
        "ollama": {
            "Id": container_id,
            "State": {
                "Running": True,
                "Status": "running",
                "Pid": 2314,
                "StartedAt": "2026-07-22T10:00:00Z",
            },
            "Config": {
                "Image": CONTAINER_IMAGE_C,
                "Env": [
                    "OLLAMA_NO_CLOUD=1",
                    "OLLAMA_HOST=0.0.0.0:11434",
                    "CAREEROPS_OLLAMA_MODEL=qwen2.5:7b",
                ],
            },
            "Image": CONTAINER_IMAGE_ID,
            "HostConfig": {"ReadonlyRootfs": True},
            "NetworkSettings": {
                "Ports": {"11434/tcp": None},
                "Networks": {
                    "careerops_ollama_private": {
                        "NetworkID": network_id,
                        "Aliases": ["ollama", "careerops-ollama-1"],
                        "IPAddress": "172.31.0.2",
                    }
                },
            },
        },
        "network": {
            "Id": network_id,
            "Name": "careerops_ollama_private",
            "Driver": "bridge",
            "Internal": True,
        },
    }


def _install_inspector_fixtures(monkeypatch: pytest.MonkeyPatch, values) -> None:
    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs):
        if str(path).replace("\\", "/") == "/etc/hostname":
            return "d" * 12
        return original_read_text(path, *args, **kwargs)

    def fake_docker_json(path: str):
        if path == "/containers/" + ("d" * 12) + "/json":
            return values["self"]
        if path.startswith("/containers/json?"):
            return values["containers"]
        if path == "/containers/" + ("a" * 64) + "/json":
            return values["ollama"]
        if path == "/networks/" + ("b" * 64):
            return values["network"]
        raise AssertionError(f"unexpected Docker inspection path: {path}")

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(inspector_module, "_docker_json", fake_docker_json)


def test_container_inspector_derives_concrete_identity_from_docker_state(
    monkeypatch: pytest.MonkeyPatch,
):
    values = _inspector_fixtures()
    _install_inspector_fixtures(monkeypatch, values)

    runtime = inspector_module.inspect_ollama_runtime("qwen2.5:7b")

    assert runtime.container_id == "a" * 64
    assert runtime.container_image_reference == CONTAINER_IMAGE_C
    assert runtime.container_image_id == CONTAINER_IMAGE_ID
    assert runtime.runtime_handle == "container:" + ("a" * 64)
    assert re.fullmatch(r"runtime-[0-9a-f]{32}", runtime.runtime_instance_id)
    assert runtime.isolation_rule_id == "network-" + ("b" * 32)
    assert runtime.egress_enforced is True
    assert runtime.cloud_disabled is True

    original_runtime_id = runtime.runtime_instance_id
    values["ollama"]["Config"]["Image"] = CONTAINER_IMAGE_E
    changed = inspector_module.inspect_ollama_runtime("qwen2.5:7b")
    assert changed.runtime_instance_id != original_runtime_id


@pytest.mark.parametrize("invalid_boundary", ["network", "cloud", "published_port"])
def test_container_inspector_rejects_unenforced_or_cloud_enabled_runtime(
    monkeypatch: pytest.MonkeyPatch,
    invalid_boundary: str,
):
    values = deepcopy(_inspector_fixtures())
    if invalid_boundary == "network":
        values["network"]["Internal"] = False
    elif invalid_boundary == "cloud":
        values["ollama"]["Config"]["Env"][0] = "OLLAMA_NO_CLOUD=0"
    else:
        values["ollama"]["NetworkSettings"]["Ports"]["11434/tcp"] = [
            {"HostIp": "0.0.0.0", "HostPort": "11434"}
        ]
    _install_inspector_fixtures(monkeypatch, values)

    with pytest.raises(inspector_module.InspectorError):
        inspector_module.inspect_ollama_runtime("qwen2.5:7b")
