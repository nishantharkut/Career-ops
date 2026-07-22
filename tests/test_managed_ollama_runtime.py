from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from careerops.ai.providers.ollama import OllamaModelIdentity, OllamaProvider
from careerops.operations.ollama_runtime import (
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


class FakeProvider:
    def __init__(self, **values):
        self.endpoint = values["endpoint"]
        self.model = values["model"]
        self.runtime_instance_id = values["runtime_instance_id"]
        self.runtime_handle = values["runtime_handle"]
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
    assert "Start-Process" in script
    assert "-WindowStyle Hidden" in script
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
    assert '$env:OLLAMA_MODELS = $ManagedModelsPath' in script


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
    calls = []
    record_payload = {
        "endpoint": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "runtime_instance_id": "runtime-1",
        "runtime_handle": "pid:4101",
        "handle_kind": "process",
        "process_id": 4101,
        "container_id": None,
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
