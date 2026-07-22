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
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any

from careerops.ai.contracts import (
    MAX_PROVIDER_OUTPUT_BYTES,
    InvocationMetadata,
    ProviderAvailability,
    ProviderError,
    ProviderInvocationError,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    bounded_utf8_text,
    parse_json_object,
    validate_provider_output,
)
from careerops.ai.providers.base import build_provider_result, sanitized_provider_error
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
MAX_CODEX_EXECUTABLE_BYTES = 1024**3
_CODEX_HASH_CHUNK_BYTES = 1024 * 1024
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
_CODEX_IDENTITIES = frozenset({"codex2", "codex"})
_PROVIDER_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CODEX_EVENT_CAPTURE_BYTES = 16_384


def codex_identity_endpoint(identity_name: str) -> str:
    if identity_name not in _CODEX_IDENTITIES:
        raise ValueError("Unsupported Codex identity name")
    return f"codex-identity://{identity_name}"


@dataclass(frozen=True)
class _CodexIdentity:
    name: str
    descriptor: Path
    native_executable: Path
    native_sha256: str
    codex_home: Path


@dataclass(frozen=True)
class _AttemptDirectory:
    path: Path
    device: int
    inode: int


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
        raise ProviderUnavailableError(
            "Codex identity file was unavailable"
        ) from error
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
        raise ProviderUnavailableError(
            "Codex identity file was unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ProviderUnavailableError("Codex identity file changed during read")
    return b"".join(chunks), after


def _stream_sha256(
    descriptor: int,
    *,
    size_limit: int,
    deadline: float,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed = 0
    while True:
        _remaining(deadline)
        chunk = os.read(
            descriptor,
            min(_CODEX_HASH_CHUNK_BYTES, size_limit + 1 - observed),
        )
        _remaining(deadline)
        if not chunk:
            return digest.hexdigest(), observed
        observed += len(chunk)
        if observed > size_limit:
            raise ProviderUnavailableError("Codex identity file was too large")
        digest.update(chunk)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
    )


def _windows_drive_type(path: Path) -> int:
    import ctypes

    root = path.expanduser().absolute().anchor
    return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))


def _hash_single_link_file(
    path: Path,
    size_limit: int,
    deadline: float,
) -> tuple[Path, str]:
    candidate = path.expanduser().absolute()
    _remaining(deadline)
    if sys.platform == "win32" and _windows_drive_type(candidate) != 3:
        raise ProviderUnavailableError(
            "Native Codex executable must be on a fixed local drive"
        )
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
            or opened.st_size > size_limit
            or _file_identity(opened) != _file_identity(before)
        ):
            raise ProviderUnavailableError("Codex identity file was unsafe")
        digest, observed = _stream_sha256(
            descriptor,
            size_limit=size_limit,
            deadline=deadline,
        )
        after = os.fstat(descriptor)
    except OSError as error:
        raise ProviderUnavailableError("Codex identity file was unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if observed != opened.st_size or _file_identity(after) != _file_identity(opened):
        raise ProviderUnavailableError("Codex identity file changed during read")
    try:
        after_path = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ProviderUnavailableError(
            "Codex identity file changed during read"
        ) from error
    if (
        stat.S_ISLNK(after_path.st_mode)
        or not stat.S_ISREG(after_path.st_mode)
        or _file_identity(after_path) != _file_identity(after)
    ):
        raise ProviderUnavailableError("Codex identity file changed during read")
    return resolved, digest


def _validate_codex_native(
    path: str | Path,
    *,
    deadline: float,
) -> tuple[Path, str]:
    candidate = Path(path).expanduser().absolute()
    if candidate.name.casefold() != "codex.exe":
        raise ProviderUnavailableError("Native Codex executable must be codex.exe")
    return _hash_single_link_file(
        candidate,
        MAX_CODEX_EXECUTABLE_BYTES,
        deadline,
    )


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


def _create_attempt_directory(task_dir: Path) -> _AttemptDirectory:
    failure = False
    try:
        created = Path(
            tempfile.mkdtemp(prefix=".careerops-attempt-", dir=task_dir)
        ).absolute()
        before = created.lstat()
        resolved = created.resolve(strict=True)
        resolved.relative_to(task_dir)
        after = resolved.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISDIR(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            failure = True
    except (OSError, ValueError):
        failure = True
    if failure:
        raise ProviderInvocationError("Could not isolate Codex attempt")
    return _AttemptDirectory(resolved, after.st_dev, after.st_ino)


def _cleanup_attempt_directory(
    attempt: _AttemptDirectory,
    known_files: tuple[Path, ...],
) -> None:
    failed = False
    try:
        before = attempt.path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or (before.st_dev, before.st_ino) != (attempt.device, attempt.inode)
        ):
            raise OSError("attempt identity changed")
        for candidate in known_files:
            if candidate.parent != attempt.path:
                raise OSError("unexpected cleanup path")
            try:
                value = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(value.st_mode):
                raise OSError("known output became a directory")
            os.unlink(candidate)
        after = attempt.path.lstat()
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISDIR(after.st_mode)
            or (after.st_dev, after.st_ino) != (attempt.device, attempt.inode)
        ):
            raise OSError("attempt identity changed")
        os.rmdir(attempt.path)
    except OSError:
        failed = True
    if failed:
        raise ProviderInvocationError("Could not clean isolated Codex attempt")


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


def _codex_thread_id(raw_events: bytes) -> str:
    try:
        first_line = next(
            line for line in raw_events.decode("utf-8").splitlines() if line.strip()
        )
        event = json.loads(first_line)
    except (StopIteration, UnicodeError, ValueError, TypeError) as error:
        raise ProviderOutputError("Codex did not report a valid thread") from error
    thread_id = event.get("thread_id") if isinstance(event, dict) else None
    if (
        event.get("type") != "thread.started"
        if isinstance(event, dict)
        else True
    ) or not isinstance(thread_id, str) or _PROVIDER_THREAD_ID.fullmatch(thread_id) is None:
        raise ProviderOutputError("Codex did not report a valid thread")
    return thread_id


@dataclass(frozen=True)
class CodexProvider:
    executable: str
    session_root: Path = Path("data/vault/codex-sessions")
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
        object.__setattr__(
            self,
            "session_root",
            self.session_root.expanduser().absolute(),
        )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "reasoning_effort", effort)

    @property
    def identity_name(self) -> str:
        return self._resolved_identity(self._identity_deadline()).name

    @property
    def native_executable(self) -> Path:
        return self._resolved_identity(self._identity_deadline()).native_executable

    @property
    def codex_home(self) -> Path:
        return self._resolved_identity(self._identity_deadline()).codex_home

    def _identity_deadline(self) -> float:
        return time.monotonic() + min(self.timeout_seconds, 20)

    def _resolved_identity(self, deadline: float) -> _CodexIdentity:
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
            native, digest = _validate_codex_native(
                configured_path,
                deadline=deadline,
            )
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
                native, digest = _validate_codex_native(
                    native_value,
                    deadline=deadline,
                )
            else:
                native, digest = _validate_codex_native(
                    descriptor,
                    deadline=deadline,
                )
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
        digest = self._verify_identity_unchanged(identity, deadline)
        native = identity.native_executable
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
            login_stdout = login_result.stdout.decode("utf-8").strip()
            login_stderr = login_result.stderr.decode("utf-8").strip()
        except UnicodeError as error:
            raise ProviderUnavailableError("Codex login status was invalid") from error
        valid_login_outputs = {
            ("Logged in using ChatGPT", ""),
            ("", "Logged in using ChatGPT"),
        }
        if (
            login_result.returncode != 0
            or (login_stdout, login_stderr) not in valid_login_outputs
        ):
            raise ProviderUnavailableError("Codex ChatGPT login was not available")
        return version, digest

    @staticmethod
    def _verify_identity_unchanged(
        identity: _CodexIdentity,
        deadline: float,
    ) -> str:
        native, digest = _validate_codex_native(
            identity.native_executable,
            deadline=deadline,
        )
        if native != identity.native_executable or digest != identity.native_sha256:
            raise ProviderUnavailableError("Native Codex executable identity changed")
        return digest

    def availability(self) -> ProviderAvailability:
        deadline = self._identity_deadline()
        try:
            identity = self._resolved_identity(deadline)
            version, digest = self._probe_identity(identity, deadline)
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
            endpoint=(codex_identity_endpoint(identity.name) if identity else ""),
            identity_name=identity.name if identity else "",
            executable=str(identity.native_executable) if identity else "",
            executable_sha256=digest,
            executable_version=version,
            codex_home=str(identity.codex_home) if identity else "",
            reasoning_effort=self.reasoning_effort,
        )

    def _session_workspace(self, identity: _CodexIdentity, application_id: str) -> Path:
        try:
            self.session_root.mkdir(parents=True, exist_ok=True)
            root_state = self.session_root.lstat()
            root = self.session_root.resolve(strict=True)
            if stat.S_ISLNK(root_state.st_mode) or not stat.S_ISDIR(root_state.st_mode):
                raise OSError("unsafe session root")
            identity_root = root / identity.name
            identity_root.mkdir(exist_ok=True)
            workspace = identity_root / application_id
            workspace.mkdir(exist_ok=True)
            for candidate in (identity_root, workspace):
                state = candidate.lstat()
                if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
                    raise OSError("unsafe session workspace")
                candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise ProviderInvocationError(
                "Could not create the Codex session workspace"
            ) from error
        return workspace.resolve(strict=True)

    def build_command(
        self,
        identity: _CodexIdentity,
        workspace: Path,
        schema_path: Path,
        output_path: Path,
        provider_thread_id: str | None,
    ) -> list[str]:
        shared = [
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "-c",
            'shell_environment_policy.inherit="none"',
        ]
        output = [
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if provider_thread_id is not None:
            return [
                str(identity.native_executable),
                "exec",
                "resume",
                *shared,
                *output,
                provider_thread_id,
                "-",
            ]
        return [
            str(identity.native_executable),
            "exec",
            *shared,
            "--sandbox",
            "read-only",
            "--cd",
            str(workspace),
            *output,
        ]

    def run(self, request: ProviderRequest) -> ProviderResult:
        failure: ProviderError | None = None
        try:
            return self._run(request)
        except ProviderError as error:
            failure = error
        raise sanitized_provider_error(failure)

    def _run(self, request: ProviderRequest) -> ProviderResult:
        deadline = time.monotonic() + self.timeout_seconds
        identity = self._resolved_identity(deadline)
        version, digest = self._probe_identity(identity, deadline)
        workspace = self._session_workspace(identity, request.application_id)
        attempt = _create_attempt_directory(request.task_dir)
        schema_path = attempt.path / "schema.json"
        output_path = attempt.path / "output.json"
        try:
            _write_exclusive(schema_path, request.schema_snapshot)
            stdin = (
                request.prompt_snapshot
                + b"\n\n<careerops_context>\n"
                + request.context_snapshot
                + b"\n</careerops_context>"
            )
            provider_thread_id = request.provider_thread_id
            provider_thread_mode = "continued" if provider_thread_id else "started"
            command = self.build_command(
                identity,
                workspace,
                schema_path,
                output_path,
                provider_thread_id,
            )
            self._verify_identity_unchanged(identity, deadline)
            completed = self._run_boundary(
                identity,
                command,
                input_bytes=stdin,
                deadline=deadline,
                capture_limit=_CODEX_EVENT_CAPTURE_BYTES,
            )
            if completed.returncode != 0 and provider_thread_id is not None:
                output_path.unlink(missing_ok=True)
                provider_thread_mode = "rebuilt"
                command = self.build_command(
                    identity,
                    workspace,
                    schema_path,
                    output_path,
                    None,
                )
                self._verify_identity_unchanged(identity, deadline)
                completed = self._run_boundary(
                    identity,
                    command,
                    input_bytes=stdin,
                    deadline=deadline,
                    capture_limit=_CODEX_EVENT_CAPTURE_BYTES,
                )
            if completed.returncode != 0:
                self._verify_identity_unchanged(identity, deadline)
                raise ProviderInvocationError(
                    f"Codex provider failed with exit code {completed.returncode}"
                )
            observed_thread_id = _codex_thread_id(completed.stdout)
            if (
                provider_thread_mode == "continued"
                and observed_thread_id != provider_thread_id
            ):
                raise ProviderOutputError("Codex resumed an unexpected thread")
            raw = _read_codex_output(output_path, attempt.path)
            # A same-user process that swaps and perfectly restores the executable
            # between checks remains outside the app's local-user threat model.
            post_digest = self._verify_identity_unchanged(identity, deadline)
            payload = parse_json_object(raw)
            output = validate_provider_output(request, payload)
            raw_output, _raw_bytes = bounded_utf8_text(raw)
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
                    endpoint=codex_identity_endpoint(identity.name),
                    identity_name=identity.name,
                    executable=str(identity.native_executable),
                    executable_sha256=post_digest,
                    executable_version=version,
                    codex_home=str(identity.codex_home),
                    reasoning_effort=self.reasoning_effort,
                    provider_thread_id=observed_thread_id,
                    provider_thread_mode=provider_thread_mode,
                    command=tuple(command),
                ),
            )
        finally:
            _cleanup_attempt_directory(attempt, (schema_path, output_path))


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
    session_root: Path | None = None,
) -> list[CodexProvider]:
    environment = dict(os.environ if environment is None else environment)
    providers: list[CodexProvider] = []
    for configured in _configured_commands(environment):
        provider = CodexProvider(
            executable=configured,
            session_root=(
                session_root
                if session_root is not None
                else Path("data/vault/codex-sessions")
            ),
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
            provider._resolved_identity(provider._identity_deadline())
        except ProviderUnavailableError:
            continue
        providers.append(provider)
    return providers
