from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from threading import RLock
from typing import Any, Callable, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from careerops.ai.contracts import OllamaIsolationEvidence, ProviderError
from careerops.ai.providers.ollama import (
    OllamaModelIdentity,
    OllamaProvider,
    OllamaRuntimeAuthority,
    canonicalize_ollama_endpoint,
)


IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}\Z")


class ManagedRuntimeError(RuntimeError):
    """Base error with a bounded operational message."""


class ManagedRuntimeConfigurationError(ManagedRuntimeError, ValueError):
    pass


class ManagedRuntimeIsolationError(ManagedRuntimeError):
    pass


class ManagedRuntimeLaunchError(ManagedRuntimeError):
    def __init__(self, message: str, *, record: RuntimeLaunchRecord | None = None):
        super().__init__(message)
        self.record = record


@dataclass(frozen=True)
class RuntimeLaunchRecord:
    endpoint: str
    model: str
    runtime_instance_id: str
    runtime_handle: str
    handle_kind: Literal["process", "container"]
    process_id: int | None
    container_id: str | None
    egress_policy: Literal["os_firewall_block", "container_internal_no_egress"]
    egress_enforced: bool
    cloud_disabled: bool
    isolation_rule_id: str

    def __post_init__(self) -> None:
        string_values = (
            self.endpoint,
            self.model,
            self.runtime_instance_id,
            self.runtime_handle,
            self.isolation_rule_id,
        )
        if any(type(value) is not str or not value or len(value) > 2048 for value in string_values):
            raise ManagedRuntimeConfigurationError("Managed runtime identity was invalid")
        if IDENTIFIER_PATTERN.fullmatch(self.runtime_instance_id) is None:
            raise ManagedRuntimeConfigurationError("Managed runtime instance ID was invalid")
        if IDENTIFIER_PATTERN.fullmatch(self.isolation_rule_id) is None:
            raise ManagedRuntimeConfigurationError("Managed runtime isolation rule ID was invalid")
        if type(self.egress_enforced) is not bool or type(self.cloud_disabled) is not bool:
            raise ManagedRuntimeConfigurationError("Managed runtime isolation flags were invalid")
        if self.handle_kind == "process":
            if (
                type(self.process_id) is not int
                or self.process_id <= 0
                or self.container_id is not None
                or self.runtime_handle != f"pid:{self.process_id}"
                or self.egress_policy != "os_firewall_block"
            ):
                raise ManagedRuntimeConfigurationError("Managed process identity was not concrete")
        elif self.handle_kind == "container":
            if (
                self.process_id is not None
                or type(self.container_id) is not str
                or CONTAINER_ID_PATTERN.fullmatch(self.container_id) is None
                or self.runtime_handle != f"container:{self.container_id}"
                or self.egress_policy != "container_internal_no_egress"
            ):
                raise ManagedRuntimeConfigurationError("Managed container identity was not concrete")
        else:
            raise ManagedRuntimeConfigurationError("Managed runtime handle kind was unsupported")


class ManagedRuntimeLauncher(Protocol):
    def launch(self, *, model: str, runtime_root: Path) -> RuntimeLaunchRecord: ...
    def verify(self, record: RuntimeLaunchRecord) -> bool: ...
    def cleanup(self, record: RuntimeLaunchRecord) -> None: ...


ProviderBuilder = Callable[..., Any]
InspectorTransport = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


@dataclass(frozen=True)
class ManagedOllamaRuntime:
    endpoint: str
    model: str
    runtime_instance_id: str
    runtime_handle: str
    egress_policy: str
    provider: Any = field(repr=False, compare=False)
    authority: OllamaRuntimeAuthority = field(repr=False, compare=False)
    evidence: OllamaIsolationEvidence = field(repr=False)
    launch_record: RuntimeLaunchRecord = field(repr=False)
    model_identity: OllamaModelIdentity = field(repr=False)

    @property
    def capability(self) -> OllamaIsolationEvidence:
        return self.evidence

    @property
    def process_id(self) -> int | None:
        return self.launch_record.process_id

    @property
    def container_id(self) -> str | None:
        return self.launch_record.container_id


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        junction_check = getattr(path, "is_junction", None)
        if callable(junction_check) and junction_check():
            return True
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ManagedRuntimeConfigurationError(
            "Managed runtime path could not be inspected"
        ) from error
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _safe_runtime_root(path: Path | str) -> Path:
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(candidate.parts[0])
    for part in candidate.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current):
                raise ManagedRuntimeConfigurationError(
                    "Managed runtime root must not traverse links or reparse points"
                )
    if not candidate.exists():
        try:
            candidate.mkdir()
        except OSError as error:
            raise ManagedRuntimeConfigurationError(
                "Managed runtime root could not be created"
            ) from error
    try:
        value = candidate.lstat()
    except OSError as error:
        raise ManagedRuntimeConfigurationError(
            "Managed runtime root could not be inspected"
        ) from error
    if _is_link_or_reparse(candidate) or not stat.S_ISDIR(value.st_mode):
        raise ManagedRuntimeConfigurationError(
            "Managed runtime root must be an app-owned non-link directory"
        )
    return candidate


def _validate_model(model: str) -> str:
    if (
        type(model) is not str
        or MODEL_PATTERN.fullmatch(model) is None
        or model.startswith(("/", "."))
        or ".." in model.split("/")
        or "//" in model
    ):
        raise ManagedRuntimeConfigurationError("Managed Ollama model name was invalid")
    return model


def _validate_record(record: RuntimeLaunchRecord, model: str) -> str:
    if type(record) is not RuntimeLaunchRecord:
        raise ManagedRuntimeIsolationError("Managed runtime launcher returned an invalid identity")
    try:
        endpoint = canonicalize_ollama_endpoint(
            record.endpoint,
            allow_managed_container=(record.handle_kind == "container"),
        )
    except ValueError as error:
        raise ManagedRuntimeIsolationError(
            "Managed Ollama endpoint was not private loopback"
        ) from error
    endpoint_matches_handle = bool(
        (
            record.handle_kind == "process"
            and endpoint.startswith(("http://127.0.0.1:", "http://[::1]:"))
        )
        or (
            record.handle_kind == "container"
            and endpoint == "http://ollama:11434"
        )
    )
    if (
        endpoint != record.endpoint
        or not endpoint_matches_handle
        or record.model != model
        or record.egress_enforced is not True
        or record.cloud_disabled is not True
    ):
        raise ManagedRuntimeIsolationError(
            "Managed runtime identity or isolation policy did not match"
        )
    return endpoint


def _validate_model_identity(identity: Any, model: str) -> OllamaModelIdentity:
    if (
        type(identity) is not OllamaModelIdentity
        or identity.model != model
        or SHA256_PATTERN.fullmatch(identity.digest) is None
        or type(identity.size) is not int
        or identity.size <= 0
        or SHA256_PATTERN.fullmatch(identity.tags_metadata_sha256) is None
        or SHA256_PATTERN.fullmatch(identity.show_metadata_sha256) is None
    ):
        raise ManagedRuntimeIsolationError(
            "Managed Ollama model metadata was invalid or mismatched"
        )
    return identity


class ManagedOllamaRuntimeFactory:
    def __init__(
        self,
        *,
        launcher: ManagedRuntimeLauncher | None = None,
        runtime_root: Path | str,
        provider_builder: ProviderBuilder = OllamaProvider,
        ollama_executable: Path | str | None = None,
        powershell_executable: Path | str | None = None,
    ):
        self.runtime_root = _safe_runtime_root(runtime_root)
        if launcher is None:
            executable = Path(ollama_executable) if ollama_executable else _find_ollama()
            launcher = WindowsPowerShellOllamaLauncher(
                executable_path=executable,
                powershell_executable=powershell_executable,
            )
        if not callable(getattr(launcher, "launch", None)) or not callable(
            getattr(launcher, "verify", None)
        ) or not callable(getattr(launcher, "cleanup", None)):
            raise ManagedRuntimeConfigurationError("Managed runtime launcher was invalid")
        if not callable(provider_builder):
            raise ManagedRuntimeConfigurationError("Managed Ollama provider builder was invalid")
        self._launcher = launcher
        self._provider_builder = provider_builder
        self.provider_type = provider_builder
        self._authority = OllamaRuntimeAuthority.generate(
            f"careerops-api-{uuid4().hex}"
        )
        self._active: dict[str, ManagedOllamaRuntime] = {}
        self._lock = RLock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(runtime_root={self.runtime_root!r}, "
            f"provider_type={getattr(self.provider_type, '__name__', type(self.provider_type).__name__)!r})"
        )

    def launch(self, *, model: str) -> ManagedOllamaRuntime:
        model = _validate_model(model)
        with self._lock:
            record: RuntimeLaunchRecord | None = None
            try:
                record = self._launcher.launch(model=model, runtime_root=self.runtime_root)
                endpoint = _validate_record(record, model)
                if not self._launcher.verify(record):
                    raise ManagedRuntimeIsolationError(
                        "Managed runtime no-egress enforcement could not be verified"
                    )
                evidence = self._authority.issue(
                    endpoint=endpoint,
                    model=model,
                    runtime_instance_id=record.runtime_instance_id,
                    runtime_handle=record.runtime_handle,
                    egress_policy=record.egress_policy,
                )
                try:
                    provider = self._provider_builder(
                        endpoint=endpoint,
                        model=model,
                        runtime_instance_id=record.runtime_instance_id,
                        runtime_handle=record.runtime_handle,
                        isolation_authority=self._authority,
                        isolation_evidence=evidence,
                    )
                except Exception as error:
                    raise ManagedRuntimeLaunchError(
                        "Managed Ollama provider could not be constructed",
                        record=record,
                    ) from error
                try:
                    identity = _validate_model_identity(
                        provider.compatibility_probe(), model
                    )
                except ManagedRuntimeIsolationError:
                    raise
                except (ProviderError, AttributeError, RuntimeError, TypeError, ValueError) as error:
                    raise ManagedRuntimeIsolationError(
                        "Managed Ollama model metadata could not be verified"
                    ) from error
                managed = ManagedOllamaRuntime(
                    endpoint=endpoint,
                    model=model,
                    runtime_instance_id=record.runtime_instance_id,
                    runtime_handle=record.runtime_handle,
                    egress_policy=record.egress_policy,
                    provider=provider,
                    authority=self._authority,
                    evidence=evidence,
                    launch_record=record,
                    model_identity=identity,
                )
                self._active[record.runtime_instance_id] = managed
                return managed
            except ManagedRuntimeLaunchError as error:
                cleanup_record = record or error.record
                self._cleanup_failed_launch(cleanup_record)
                raise
            except ManagedRuntimeError:
                self._cleanup_failed_launch(record)
                raise
            except Exception as error:
                self._cleanup_failed_launch(record)
                raise ManagedRuntimeLaunchError(
                    "Managed Ollama runtime could not be launched"
                ) from error

    def _cleanup_failed_launch(self, record: RuntimeLaunchRecord | None) -> None:
        if record is None:
            return
        try:
            self._launcher.cleanup(record)
        except Exception as error:
            raise ManagedRuntimeLaunchError(
                "Managed runtime launch failed and isolation cleanup did not complete",
                record=record,
            ) from error

    def verify(self, managed: ManagedOllamaRuntime) -> bool:
        with self._lock:
            try:
                active = self._active.get(managed.runtime_instance_id)
                if active is not managed or managed.authority is not self._authority:
                    return False
                record = managed.launch_record
                return bool(
                    record.runtime_instance_id == managed.runtime_instance_id
                    and record.runtime_handle == managed.runtime_handle
                    and record.endpoint == managed.endpoint
                    and record.model == managed.model
                    and managed.provider.endpoint == managed.endpoint
                    and managed.provider.model == managed.model
                    and self._authority.verifies(
                        managed.evidence,
                        endpoint=managed.endpoint,
                        model=managed.model,
                        runtime_instance_id=managed.runtime_instance_id,
                        runtime_handle=managed.runtime_handle,
                    )
                    and self._launcher.verify(record)
                )
            except Exception:
                return False

    def stop(self, managed: ManagedOllamaRuntime) -> None:
        with self._lock:
            if self._active.get(managed.runtime_instance_id) is not managed:
                return
            try:
                self._launcher.cleanup(managed.launch_record)
            except Exception as error:
                raise ManagedRuntimeLaunchError(
                    "Managed Ollama runtime cleanup did not complete",
                    record=managed.launch_record,
                ) from error
            self._active.pop(managed.runtime_instance_id, None)

    def restart(self, managed: ManagedOllamaRuntime) -> ManagedOllamaRuntime:
        model = managed.model
        self.stop(managed)
        return self.launch(model=model)


def _find_ollama() -> Path:
    executable = shutil.which("ollama")
    if not executable:
        raise ManagedRuntimeConfigurationError(
            "An explicit app-owned Ollama executable is required"
        )
    return Path(executable)


class WindowsPowerShellOllamaLauncher:
    def __init__(
        self,
        *,
        executable_path: Path | str,
        powershell_executable: Path | str | None = None,
        script_path: Path | str | None = None,
        timeout_seconds: float = 90,
    ):
        self.executable_path = Path(os.path.abspath(Path(executable_path).expanduser()))
        if not self.executable_path.is_file() or _is_link_or_reparse(self.executable_path):
            raise ManagedRuntimeConfigurationError(
                "Managed Ollama executable must be an existing non-link file"
            )
        self.script_path = Path(
            script_path
            or Path(__file__).resolve().parents[2] / "scripts" / "start-managed-ollama.ps1"
        ).resolve()
        if not self.script_path.is_file():
            raise ManagedRuntimeConfigurationError("Managed Ollama launcher script was missing")
        powershell = (
            str(powershell_executable)
            if powershell_executable
            else shutil.which("pwsh") or shutil.which("powershell")
        )
        if not powershell:
            raise ManagedRuntimeConfigurationError("PowerShell was unavailable")
        self.powershell_executable = str(powershell)
        if timeout_seconds <= 0:
            raise ManagedRuntimeConfigurationError("Launcher timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._runtime_roots: dict[str, Path] = {}

    def _base_command(self, action: str, model: str, runtime_root: Path) -> list[str]:
        return [
            self.powershell_executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
            "-Action",
            action,
            "-ExecutablePath",
            str(self.executable_path),
            "-Model",
            model,
            "-RuntimeRoot",
            str(runtime_root),
        ]

    @staticmethod
    def _identity_arguments(record: RuntimeLaunchRecord) -> list[str]:
        if record.handle_kind != "process" or record.process_id is None:
            raise ManagedRuntimeConfigurationError(
                "Windows launcher requires a concrete process identity"
            )
        return [
            "-RuntimeInstanceId",
            record.runtime_instance_id,
            "-RuntimeHandle",
            record.runtime_handle,
            "-ProcessId",
            str(record.process_id),
            "-IsolationRuleId",
            record.isolation_rule_id,
        ]

    def _run(self, command: list[str]) -> dict[str, Any]:
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("OLLAMA_")
            and not key.upper().startswith("CAREEROPS_OLLAMA_")
            and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        }
        child_environment.update(
            {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        )
        try:
            completed = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                env=child_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher did not complete") from error
        if completed.returncode != 0:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher reported failure")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as error:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher returned invalid JSON") from error
        if type(payload) is not dict:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher returned an invalid result")
        return payload

    def launch(self, *, model: str, runtime_root: Path) -> RuntimeLaunchRecord:
        try:
            payload = self._run(self._base_command("Launch", model, runtime_root))
        except ManagedRuntimeLaunchError:
            self._recover_failed_launch(model=model, runtime_root=runtime_root)
            raise
        expected = {
            "endpoint",
            "model",
            "runtime_instance_id",
            "runtime_handle",
            "handle_kind",
            "process_id",
            "container_id",
            "egress_policy",
            "egress_enforced",
            "cloud_disabled",
            "isolation_rule_id",
        }
        if set(payload) != expected:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher identity was incomplete")
        try:
            record = RuntimeLaunchRecord(**payload)
        except (TypeError, ValueError) as error:
            raise ManagedRuntimeLaunchError("Managed Ollama launcher identity was invalid") from error
        self._runtime_roots[record.runtime_instance_id] = runtime_root
        return record

    def _recover_failed_launch(self, *, model: str, runtime_root: Path) -> None:
        try:
            payload = self._run(self._base_command("Recover", model, runtime_root))
        except ManagedRuntimeError as error:
            raise ManagedRuntimeLaunchError(
                "Managed Ollama launcher failed and journal recovery did not complete"
            ) from error
        instances = payload.get("runtime_instances")
        if (
            payload.get("recovered") is not True
            or set(payload) != {"recovered", "runtime_instances"}
            or not isinstance(instances, list)
            or not all(
                type(item) is str and IDENTIFIER_PATTERN.fullmatch(item)
                for item in instances
            )
        ):
            raise ManagedRuntimeLaunchError(
                "Managed Ollama launcher journal recovery was invalid"
            )

    def verify(self, record: RuntimeLaunchRecord) -> bool:
        runtime_root = self._runtime_roots.get(record.runtime_instance_id)
        if runtime_root is None:
            return False
        command = self._base_command("Verify", record.model, runtime_root)
        command.extend(self._identity_arguments(record))
        try:
            payload = self._run(command)
        except ManagedRuntimeError:
            return False
        return payload == {
            "verified": True,
            "runtime_instance_id": record.runtime_instance_id,
            "runtime_handle": record.runtime_handle,
        }

    def cleanup(self, record: RuntimeLaunchRecord) -> None:
        runtime_root = self._runtime_roots.get(record.runtime_instance_id)
        if runtime_root is None:
            raise ManagedRuntimeLaunchError("Managed runtime root binding was unavailable")
        command = self._base_command("Stop", record.model, runtime_root)
        command.extend(self._identity_arguments(record))
        payload = self._run(command)
        if payload != {
            "stopped": True,
            "runtime_instance_id": record.runtime_instance_id,
        }:
            raise ManagedRuntimeLaunchError("Managed Ollama cleanup result was invalid")
        self._runtime_roots.pop(record.runtime_instance_id, None)


class DockerComposeOllamaLauncher:
    """Adopts only the Compose Ollama instance verified by the local inspector."""

    _INSPECTOR_ORIGIN = "http://runtime-inspector:8766"
    _MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, *, transport: InspectorTransport | None = None):
        self._transport = transport or self._request_inspector
        if not callable(self._transport):
            raise ManagedRuntimeConfigurationError(
                "Managed container inspector transport was invalid"
            )
        self._active: dict[str, RuntimeLaunchRecord] = {}
        self._lock = RLock()

    def launch(self, *, model: str, runtime_root: Path) -> RuntimeLaunchRecord:
        del runtime_root
        model = _validate_model(model)
        path = f"/v1/ollama-runtime?model={quote(model, safe='')}"
        payload = self._call("GET", path, None)
        record = self._record_from_payload(payload)
        if record.model != model:
            raise ManagedRuntimeIsolationError(
                "Managed container model identity did not match"
            )
        with self._lock:
            self._active[record.runtime_instance_id] = record
        return record

    def verify(self, record: RuntimeLaunchRecord) -> bool:
        with self._lock:
            if self._active.get(record.runtime_instance_id) != record:
                return False
        path = f"/v1/ollama-runtime?model={quote(record.model, safe='')}"
        try:
            observed = self._record_from_payload(self._call("GET", path, None))
        except ManagedRuntimeError:
            return False
        return observed == record

    def cleanup(self, record: RuntimeLaunchRecord) -> None:
        with self._lock:
            if self._active.get(record.runtime_instance_id) != record:
                raise ManagedRuntimeLaunchError(
                    "Managed container runtime binding was unavailable"
                )
        payload = self._call(
            "POST",
            "/v1/ollama-runtime/cleanup",
            {
                "runtime_instance_id": record.runtime_instance_id,
                "container_id": record.container_id,
            },
        )
        if payload != {
            "stopped": True,
            "runtime_instance_id": record.runtime_instance_id,
        }:
            raise ManagedRuntimeLaunchError(
                "Managed container cleanup result was invalid"
            )
        with self._lock:
            self._active.pop(record.runtime_instance_id, None)

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            payload = self._transport(method, path, body)
        except ManagedRuntimeError:
            raise
        except Exception as error:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector was unavailable"
            ) from error
        if type(payload) is not dict:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector returned invalid JSON"
            )
        return payload

    @staticmethod
    def _record_from_payload(payload: dict[str, Any]) -> RuntimeLaunchRecord:
        expected = {
            "endpoint",
            "model",
            "runtime_instance_id",
            "runtime_handle",
            "handle_kind",
            "process_id",
            "container_id",
            "egress_policy",
            "egress_enforced",
            "cloud_disabled",
            "isolation_rule_id",
        }
        if set(payload) != expected:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector identity was incomplete"
            )
        try:
            record = RuntimeLaunchRecord(**payload)
        except (TypeError, ValueError) as error:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector identity was invalid"
            ) from error
        if (
            record.handle_kind != "container"
            or record.endpoint != "http://ollama:11434"
            or record.egress_policy != "container_internal_no_egress"
            or record.egress_enforced is not True
            or record.cloud_disabled is not True
        ):
            raise ManagedRuntimeIsolationError(
                "Managed container isolation evidence did not match"
            )
        return record

    def _request_inspector(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if method not in {"GET", "POST"} or not path.startswith("/v1/ollama-runtime"):
            raise ManagedRuntimeConfigurationError(
                "Managed container inspector request was invalid"
            )
        data = None
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            data = json.dumps(
                body,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._INSPECTOR_ORIGIN}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None and int(raw_length) > self._MAX_RESPONSE_BYTES:
                    raise ManagedRuntimeLaunchError(
                        "Managed container inspector response was too large"
                    )
                raw = response.read(self._MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector was unavailable"
            ) from error
        if len(raw) > self._MAX_RESPONSE_BYTES:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector response was too large"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector returned invalid JSON"
            ) from error
        if type(payload) is not dict:
            raise ManagedRuntimeLaunchError(
                "Managed container inspector returned invalid JSON"
            )
        return payload
