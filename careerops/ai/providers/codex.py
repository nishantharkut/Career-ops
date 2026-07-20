from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from types import MappingProxyType
from typing import Any

from careerops.ai.contracts import (
    MAX_PROVIDER_OUTPUT_BYTES,
    InvocationMetadata,
    ProviderAvailability,
    ProviderInvocationError,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    parse_json_object,
    serialize_provider_output,
    validate_provider_output,
)
from careerops.ai.providers.base import build_provider_result
from careerops.ai.providers.process import (
    ProcessDeadlineExceeded,
    ProcessRunner,
    ProcessStartError,
)
from careerops.core.status import PrivacyMode


Which = Any
_SEMVER = re.compile(
    r"^codex-cli (0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_ENVIRONMENT_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
)
_WRAPPER_HOME = "%USERPROFILE%\\codex-account2\\.codex"


@dataclass(frozen=True)
class _CodexIdentity:
    name: str
    descriptor: Path
    native_executable: Path
    native_sha256: str
    codex_home: Path


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ProviderTimeoutError("Codex provider timed out")
    return value


def _read_single_link_file(path: Path, size_limit: int) -> tuple[bytes, os.stat_result]:
    candidate = path.expanduser().absolute()
    try:
        before = candidate.lstat()
    except OSError as error:
        raise ProviderUnavailableError("Codex identity file was unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > size_limit
    ):
        raise ProviderUnavailableError("Codex identity file was unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ProviderUnavailableError("Codex identity file was unsafe")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, size_limit + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > size_limit:
                raise ProviderUnavailableError("Codex identity file was too large")
        after = os.fstat(descriptor)
    except OSError as error:
        raise ProviderUnavailableError("Codex identity file was unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ProviderUnavailableError("Codex identity file changed during read")
    return b"".join(chunks), after


def _validate_codex_native(path: str | Path) -> tuple[Path, str]:
    candidate = Path(path).expanduser().absolute()
    if candidate.name.casefold() != "codex.exe":
        raise ProviderUnavailableError("Native Codex executable must be codex.exe")
    raw, _identity = _read_single_link_file(candidate, 256 * 1024 * 1024)
    return candidate.resolve(), hashlib.sha256(raw).hexdigest()


def _validate_codex_home(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    try:
        if candidate.is_symlink():
            raise ProviderUnavailableError("CODEX_HOME must not be a symlink")
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ProviderUnavailableError("CODEX_HOME was unavailable") from error
    if not resolved.is_dir():
        raise ProviderUnavailableError("CODEX_HOME was unavailable")
    return resolved


def _parse_codex2_wrapper(path: str | Path, environment: Mapping[str, str]) -> Path:
    wrapper = Path(path).expanduser().absolute()
    if wrapper.suffix.casefold() != ".cmd":
        raise ProviderUnavailableError("codex2 must resolve to the approved .cmd descriptor")
    raw, _identity = _read_single_link_file(wrapper, 4_096)
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError as error:
        raise ProviderUnavailableError("codex2 descriptor was invalid") from error
    if lines and lines[0].casefold() == "@echo off":
        lines = lines[1:]
    expected = [
        f'set "CODEX_HOME={_WRAPPER_HOME}"',
        "codex %*",
        "exit /b %ERRORLEVEL%",
    ]
    if [line.casefold() for line in lines] != [line.casefold() for line in expected]:
        raise ProviderUnavailableError("codex2 descriptor was not the approved wrapper")
    user_profile = environment.get("USERPROFILE", "").strip()
    if not user_profile:
        raise ProviderUnavailableError("USERPROFILE was unavailable for codex2")
    return _validate_codex_home(
        Path(user_profile) / "codex-account2" / ".codex"
    )


def _default_codex_home(environment: Mapping[str, str]) -> Path:
    override = environment.get("CAREEROPS_CODEX_HOME", "").strip()
    if override:
        return _validate_codex_home(Path(override))
    root = environment.get("USERPROFILE") or environment.get("HOME")
    if not root:
        raise ProviderUnavailableError("User home was unavailable for Codex")
    return _validate_codex_home(Path(root) / ".codex")


def _write_exclusive(path: Path, value: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    except OSError as error:
        raise ProviderInvocationError("Could not create isolated Codex input") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_codex_output(path: Path, attempt_dir: Path) -> bytes:
    # A random provider-owned directory narrows same-user races; path APIs cannot
    # make another process under the same OS account fully non-adversarial.
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(attempt_dir)
    except (OSError, ValueError) as error:
        raise ProviderOutputError("Codex did not create a safe output file") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_PROVIDER_OUTPUT_BYTES
    ):
        raise ProviderOutputError("Codex output was not a bounded single-link file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ProviderOutputError("Codex output identity was unsafe")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_PROVIDER_OUTPUT_BYTES + 1 - observed),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > MAX_PROVIDER_OUTPUT_BYTES:
                raise ProviderOutputError("Codex output exceeded the size limit")
        after = os.fstat(descriptor)
    except OSError as error:
        raise ProviderOutputError("Codex output could not be read") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ProviderOutputError("Codex output changed during read")
    return b"".join(chunks)


@dataclass(frozen=True)
class CodexProvider:
    executable: str
    executable_name: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 180
    is_default: bool = False
    which: Which = field(default=shutil.which, repr=False, compare=False)
    environment: Mapping[str, str] | None = field(
        default=None, repr=False, compare=False
    )
    process_runner: Any = field(
        default_factory=ProcessRunner, repr=False, compare=False
    )
    _identity: _CodexIdentity | None = field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        configured = self.executable.strip()
        if not configured:
            raise ValueError("Codex executable must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        environment = MappingProxyType(
            dict(os.environ if self.environment is None else self.environment)
        )
        model = self.model or environment.get(
            "CAREEROPS_CODEX_MODEL", "gpt-5.6-sol"
        )
        effort = self.reasoning_effort or environment.get(
            "CAREEROPS_CODEX_REASONING_EFFORT", "xhigh"
        )
        if _SAFE_MODEL.fullmatch(model) is None:
            raise ValueError("CAREEROPS_CODEX_MODEL was invalid")
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("CAREEROPS_CODEX_REASONING_EFFORT was invalid")
        object.__setattr__(self, "executable", configured)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "reasoning_effort", effort)

    @property
    def identity_name(self) -> str:
        return self._resolved_identity().name

    @property
    def native_executable(self) -> Path:
        return self._resolved_identity().native_executable

    @property
    def codex_home(self) -> Path:
        return self._resolved_identity().codex_home

    def _resolved_identity(self) -> _CodexIdentity:
        if self._identity is not None:
            return self._identity
        environment = self.environment or {}
        configured_path = Path(self.executable).expanduser()
        explicit_native = configured_path.is_absolute() or any(
            separator in self.executable for separator in ("/", "\\")
        )
        if explicit_native:
            if configured_path.suffix.casefold() != ".exe":
                raise ProviderUnavailableError("Only native Codex .exe overrides are allowed")
            native, digest = _validate_codex_native(configured_path)
            name = self.executable_name or "codex"
            descriptor = native
            codex_home = _default_codex_home(environment)
        else:
            name = (self.executable_name or self.executable).strip()
            if name not in {"codex2", "codex"}:
                raise ProviderUnavailableError(f"Unsupported Codex identity: {name}")
            try:
                selected = self.which(name)
            except Exception as error:
                raise ProviderUnavailableError(
                    f"Selected Codex identity is unavailable: {name}"
                ) from error
            if not selected:
                raise ProviderUnavailableError(
                    f"Selected Codex identity is unavailable: {name}"
                )
            descriptor = Path(selected).expanduser().absolute()
            if name == "codex2":
                codex_home = _parse_codex2_wrapper(descriptor, environment)
                try:
                    native_value = self.which("codex")
                except Exception as error:
                    raise ProviderUnavailableError(
                        "Native Codex executable was unavailable for codex2"
                    ) from error
                if not native_value:
                    raise ProviderUnavailableError(
                        "Native Codex executable was unavailable for codex2"
                    )
                native, digest = _validate_codex_native(native_value)
            else:
                native, digest = _validate_codex_native(descriptor)
                codex_home = _default_codex_home(environment)
        identity = _CodexIdentity(name, descriptor, native, digest, codex_home)
        object.__setattr__(self, "_identity", identity)
        return identity

    def _safe_environment(self, identity: _CodexIdentity) -> dict[str, str]:
        source = self.environment or {}
        safe = {
            key: source[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in source and isinstance(source[key], str)
        }
        safe["CODEX_HOME"] = str(identity.codex_home)
        return safe

    def _run_boundary(
        self,
        identity: _CodexIdentity,
        command: list[str],
        *,
        input_bytes: bytes,
        deadline: float,
        capture_limit: int,
    ):
        try:
            return self.process_runner.run(
                command,
                input_bytes=input_bytes,
                timeout_seconds=_remaining(deadline),
                environment=self._safe_environment(identity),
                capture_limit=capture_limit,
            )
        except ProcessDeadlineExceeded as error:
            raise ProviderTimeoutError(
                f"Codex provider timed out ({identity.name})"
            ) from error
        except ProcessStartError as error:
            raise ProviderUnavailableError(
                f"Selected Codex identity is unavailable: {identity.name}"
            ) from error

    def _probe_identity(
        self, identity: _CodexIdentity, deadline: float
    ) -> tuple[str, str]:
        native, digest = _validate_codex_native(identity.native_executable)
        if native != identity.native_executable or digest != identity.native_sha256:
            raise ProviderUnavailableError("Native Codex executable identity changed")
        version_result = self._run_boundary(
            identity,
            [str(native), "--version"],
            input_bytes=b"",
            deadline=deadline,
            capture_limit=4_096,
        )
        try:
            version = version_result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise ProviderUnavailableError("Codex version output was invalid") from error
        if version_result.returncode != 0 or _SEMVER.fullmatch(version) is None:
            raise ProviderUnavailableError("Codex version could not be verified")
        login_result = self._run_boundary(
            identity,
            [str(native), "login", "status"],
            input_bytes=b"",
            deadline=deadline,
            capture_limit=4_096,
        )
        try:
            login = login_result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise ProviderUnavailableError("Codex login status was invalid") from error
        if login_result.returncode != 0 or login != "Logged in using ChatGPT":
            raise ProviderUnavailableError("Codex ChatGPT login was not available")
        return version, digest

    def availability(self) -> ProviderAvailability:
        try:
            identity = self._resolved_identity()
            version, digest = self._probe_identity(
                identity, time.monotonic() + min(self.timeout_seconds, 20)
            )
            available = True
            detail = "available"
        except Exception:
            identity = self._identity
            version = ""
            digest = ""
            available = False
            detail = "unavailable"
        return ProviderAvailability(
            available=available,
            provider_kind="codex",
            model=self.model,
            privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
            data_leaves_machine=True,
            is_ai=True,
            detail=detail,
            identity_name=identity.name if identity else "",
            executable=str(identity.native_executable) if identity else "",
            executable_sha256=digest,
            executable_version=version,
            codex_home=str(identity.codex_home) if identity else "",
            reasoning_effort=self.reasoning_effort,
        )

    def build_command(
        self,
        identity: _CodexIdentity,
        attempt_dir: Path,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        return [
            str(identity.native_executable),
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "--sandbox",
            "read-only",
            "--cd",
            str(attempt_dir),
            "--skip-git-repo-check",
            "--ephemeral",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]

    def run(self, request: ProviderRequest) -> ProviderResult:
        deadline = time.monotonic() + self.timeout_seconds
        identity = self._resolved_identity()
        version, digest = self._probe_identity(identity, deadline)
        try:
            attempt_dir = Path(
                tempfile.mkdtemp(prefix=".careerops-attempt-", dir=request.task_dir)
            ).resolve(strict=True)
            attempt_dir.relative_to(request.task_dir)
        except (OSError, ValueError) as error:
            raise ProviderInvocationError("Could not isolate Codex attempt") from error
        schema_path = attempt_dir / "schema.json"
        output_path = attempt_dir / "output.json"
        _write_exclusive(schema_path, request.schema_snapshot)
        command = self.build_command(
            identity, attempt_dir, schema_path, output_path
        )
        stdin = (
            request.prompt_snapshot
            + b"\n\n<careerops_context>\n"
            + request.context_snapshot
            + b"\n</careerops_context>"
        )
        completed = self._run_boundary(
            identity,
            command,
            input_bytes=stdin,
            deadline=deadline,
            capture_limit=0,
        )
        if completed.returncode != 0:
            raise ProviderInvocationError(
                f"Codex provider failed with exit code {completed.returncode}"
            )
        raw = _read_codex_output(output_path, attempt_dir)
        payload = parse_json_object(raw)
        output = validate_provider_output(request, payload)
        raw_output = serialize_provider_output(output)
        return build_provider_result(
            provider_kind="codex",
            model=self.model,
            privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
            data_leaves_machine=True,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(
                mode="codex_exec",
                identity_name=identity.name,
                executable=str(identity.native_executable),
                executable_sha256=digest,
                executable_version=version,
                codex_home=str(identity.codex_home),
                reasoning_effort=self.reasoning_effort,
                command=tuple(command),
            ),
        )


def _configured_commands(environment: Mapping[str, str]) -> list[str]:
    if "CAREEROPS_CODEX_COMMAND" in environment:
        raw_commands = environment.get("CAREEROPS_CODEX_COMMAND", "")
        single_override = True
    else:
        raw_commands = environment.get(
            "CAREEROPS_CODEX_COMMANDS", "codex2,codex"
        )
        single_override = False
    commands: list[str] = []
    observed: set[str] = set()
    for value in raw_commands.split(","):
        command = value.strip()
        key = command.casefold()
        if not command or key in observed:
            continue
        observed.add(key)
        commands.append(command)
        if single_override:
            break
    return commands


def discover_codex_providers(
    *,
    which: Which = shutil.which,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 180,
    process_runner: Any | None = None,
) -> list[CodexProvider]:
    environment = dict(os.environ if environment is None else environment)
    providers: list[CodexProvider] = []
    for configured in _configured_commands(environment):
        provider = CodexProvider(
            executable=configured,
            executable_name=(
                Path(configured).stem
                if Path(configured).is_absolute()
                else configured
            ),
            timeout_seconds=timeout_seconds,
            is_default=not providers,
            which=which,
            environment=environment,
            process_runner=process_runner or ProcessRunner(),
        )
        try:
            provider._resolved_identity()
        except ProviderUnavailableError:
            continue
        providers.append(provider)
    return providers
