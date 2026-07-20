from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    ProviderInvocationError,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    output_schema_for_task,
    parse_json_object,
    validate_task_output,
)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
MAX_CODEX_OUTPUT_BYTES = 2_000_000


@dataclass(frozen=True)
class CodexProvider:
    executable: str
    executable_name: str | None = None
    model: str = "account-default"
    timeout_seconds: float = 180
    is_default: bool = False
    which: Which = field(default=shutil.which, repr=False, compare=False)
    run_process: ProcessRunner = field(
        default=subprocess.run, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        executable = self.executable.strip()
        if not executable:
            raise ValueError("Codex executable must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "executable", executable)
        if self.executable_name is None:
            object.__setattr__(self, "executable_name", Path(executable).stem)

    def availability(self) -> ProviderAvailability:
        resolved = ""
        version = ""
        try:
            resolved = self._resolve_selected()
            version = self._verify_version(resolved)
            available = True
            detail = "available"
        except Exception:
            available = False
            detail = "unavailable"
        return ProviderAvailability(
            available=available,
            provider_kind="codex",
            model=self.model,
            privacy_mode="cloud_review",
            data_leaves_machine=True,
            is_ai=True,
            detail=detail,
            executable=resolved,
            executable_version=version,
        )

    def build_command(
        self,
        task_dir: Path,
        schema_path: Path,
        output_path: Path,
        resolved_executable: str | None = None,
    ) -> list[str]:
        task_dir = task_dir.resolve(strict=True)
        schema_path = schema_path.resolve(strict=True)
        output_path = output_path.resolve(strict=False)
        for path, label in ((schema_path, "schema"), (output_path, "output")):
            try:
                path.relative_to(task_dir)
            except ValueError as error:
                raise ProviderOutputError(
                    f"Codex {label} path escaped task_dir"
                ) from error
        executable = resolved_executable or self._resolve_selected()
        return [
            executable,
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(task_dir),
            "--skip-git-repo-check",
            "--ephemeral",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]

    def run(self, request: ProviderRequest) -> ProviderResult:
        self._load_matching_schema(request)
        resolved = self._resolve_selected()
        version = self._verify_version(resolved)
        output_path = request.task_dir / f"codex-output-{request.task_id}.json"
        if output_path.is_symlink():
            raise ProviderOutputError("Codex output path was not a regular file")
        if output_path.exists():
            if not output_path.is_file():
                raise ProviderOutputError("Codex output path was not a regular file")
            output_path.unlink()
        command = self.build_command(
            request.task_dir,
            request.schema_path,
            output_path,
            resolved_executable=resolved,
        )
        try:
            prompt = request.prompt_path.read_text(encoding="utf-8")
            context = request.context_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProviderInvocationError("Provider input could not be read") from error
        stdin = f"{prompt}\n\n<careerops_context>\n{context}\n</careerops_context>"
        try:
            completed = self.run_process(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
                check=False,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise ProviderTimeoutError(
                f"Codex provider timed out ({self.executable_name})"
            ) from error
        except (FileNotFoundError, OSError) as error:
            raise ProviderUnavailableError(
                f"Selected Codex executable is unavailable: {self.executable_name}"
            ) from error
        if completed.returncode != 0:
            raise ProviderInvocationError(
                f"Codex provider failed with exit code {completed.returncode}"
            )
        if output_path.is_symlink():
            raise ProviderOutputError("Codex output path was not a regular file")
        if not output_path.is_file():
            raise ProviderOutputError("Codex did not create its output file")
        try:
            if output_path.stat().st_size > MAX_CODEX_OUTPUT_BYTES:
                raise ProviderOutputError("Codex output exceeded the size limit")
            raw_output = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProviderOutputError("Codex output could not be read") from error
        payload = parse_json_object(raw_output)
        output = validate_task_output(request.task_type, payload)
        return ProviderResult(
            provider_kind="codex",
            model=self.model,
            privacy_mode="cloud_review",
            data_leaves_machine=True,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(
                mode="codex_exec",
                executable=resolved,
                executable_version=version,
                command=tuple(command),
            ),
        )

    def _resolve_selected(self) -> str:
        executable_path = Path(self.executable).expanduser()
        path_like = executable_path.is_absolute() or any(
            separator in self.executable for separator in ("/", "\\")
        )
        if path_like:
            if executable_path.is_file():
                return str(executable_path.resolve())
            resolved = None
        else:
            try:
                resolved = self.which(self.executable)
            except Exception as error:
                raise ProviderUnavailableError(
                    f"Selected Codex executable is unavailable: {self.executable_name}"
                ) from error
        if not resolved:
            raise ProviderUnavailableError(
                f"Selected Codex executable is unavailable: {self.executable_name}"
            )
        return str(Path(resolved).expanduser().resolve())

    def _verify_version(self, resolved_executable: str) -> str:
        try:
            completed = self.run_process(
                [resolved_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=min(self.timeout_seconds, 10),
                shell=False,
                check=False,
            )
        except Exception as error:
            raise ProviderUnavailableError(
                f"Selected Codex executable is unavailable: {self.executable_name}"
            ) from error
        if completed.returncode != 0:
            raise ProviderUnavailableError(
                f"Selected Codex executable is unavailable: {self.executable_name}"
            )
        version = next(
            (line.strip() for line in completed.stdout.splitlines() if line.strip()), ""
        )
        if not version:
            raise ProviderUnavailableError(
                f"Selected Codex executable is unavailable: {self.executable_name}"
            )
        return version[:2_048]

    @staticmethod
    def _load_matching_schema(request: ProviderRequest) -> dict[str, Any]:
        try:
            schema = json.loads(request.schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ProviderOutputError("Task schema input was invalid") from error
        if schema != output_schema_for_task(request.task_type):
            raise ProviderOutputError("Task schema did not match the task type")
        return schema


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
    run_process: ProcessRunner = subprocess.run,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 180,
) -> list[CodexProvider]:
    configured = _configured_commands(
        os.environ if environment is None else environment
    )
    providers: list[CodexProvider] = []
    for name in configured:
        try:
            resolved = which(name)
        except Exception:
            resolved = None
        if not resolved:
            continue
        providers.append(
            CodexProvider(
                executable=resolved,
                executable_name=name,
                timeout_seconds=timeout_seconds,
                is_default=not providers,
                which=which,
                run_process=run_process,
            )
        )
    return providers
