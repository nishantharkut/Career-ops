from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError

from pydantic import ValidationError
import pytest

from careerops.ai.contracts import (
    ALLOWED_TASK_TYPES,
    AnalyzeFitOutput,
    AnalyzeJdOutput,
    ClaimReviewOutput,
    CoverLetterOutput,
    ProviderRequest,
    ResumeEditsOutput,
    output_schema_for_task,
    validate_task_output,
)
from careerops.ai.providers.base import (
    ProviderInvocationError,
    ProviderOutputError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from careerops.ai.providers.codex import CodexProvider, discover_codex_providers
from careerops.ai.providers.deterministic import DeterministicProvider
from careerops.ai.providers.mock import MockProvider
from careerops.ai.providers.ollama import OllamaProvider


VALID_OUTPUTS: dict[str, dict[str, Any]] = {
    "analyze_jd": {
        "task_type": "analyze_jd",
        "normalized_title": "Software Engineer",
        "normalized_company": "Example Corp",
        "normalized_location": "Bengaluru, India",
        "responsibilities": ["Build reliable services"],
        "required_skills": ["Python"],
        "preferred_skills": ["FastAPI"],
        "keywords": ["backend"],
        "uncertainties": ["Experience level is not explicit"],
    },
    "analyze_fit": {
        "task_type": "analyze_fit",
        "score": 78,
        "matches": ["Python experience"],
        "gaps": ["No confirmed Kubernetes evidence"],
        "risks": ["Role level is unclear"],
        "recommendations": ["Lead with backend project evidence"],
    },
    "suggest_resume_edits": {
        "task_type": "suggest_resume_edits",
        "suggestions": [
            {
                "source_text": "Built a service",
                "replacement_text": "Built a Python service",
                "reason": "Names the verified implementation language",
                "evidence_refs": ["evidence_python_project"],
            }
        ],
    },
    "draft_cover_letter": {
        "task_type": "draft_cover_letter",
        "plain_text": "I am applying for the Software Engineer role.",
        "evidence_refs": ["evidence_python_project"],
    },
    "review_claims": {
        "task_type": "review_claims",
        "decisions": [
            {
                "claim_id": "claim_1",
                "decision": "supported",
                "reason": "The repository confirms the implementation.",
                "evidence_refs": ["evidence_python_project"],
            }
        ],
    },
}

MODEL_BY_TASK = {
    "analyze_jd": AnalyzeJdOutput,
    "analyze_fit": AnalyzeFitOutput,
    "suggest_resume_edits": ResumeEditsOutput,
    "draft_cover_letter": CoverLetterOutput,
    "review_claims": ClaimReviewOutput,
}


def make_request(tmp_path: Path, task_type: str = "analyze_fit") -> ProviderRequest:
    task_dir = tmp_path / f"task-{task_type}"
    task_dir.mkdir()
    prompt_path = task_dir / "prompt.txt"
    context_path = task_dir / "context.json"
    schema_path = task_dir / "schema.json"
    prompt_path.write_text("Return only evidence-grounded JSON.", encoding="utf-8")
    context_path.write_text('{"resume":"Python"}', encoding="utf-8")
    schema_path.write_text(
        json.dumps(output_schema_for_task(task_type)), encoding="utf-8"
    )
    return ProviderRequest(
        task_id="task_123",
        task_type=task_type,
        task_dir=task_dir,
        prompt_path=prompt_path,
        context_path=context_path,
        schema_path=schema_path,
    )


class FakeHttpResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


def response_with_json(payload: Any, status: int = 200) -> FakeHttpResponse:
    return FakeHttpResponse(json.dumps(payload).encode("utf-8"), status=status)


def assert_all_collections_are_bounded(schema: dict[str, Any]) -> None:
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not ({"const", "enum"} & node.keys()):
                assert "maxLength" in node, node
            if node.get("type") == "array":
                assert "maxItems" in node, node
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


@pytest.mark.parametrize("task_type", sorted(VALID_OUTPUTS))
def test_all_five_task_outputs_validate_and_match_checked_in_schema(task_type: str):
    output = validate_task_output(task_type, VALID_OUTPUTS[task_type])
    assert isinstance(output, MODEL_BY_TASK[task_type])

    schema = output_schema_for_task(task_type)
    schema_path = (
        Path(__file__).parents[1]
        / "careerops"
        / "ai"
        / "schemas"
        / f"{task_type}_v1.json"
    )
    assert json.loads(schema_path.read_text(encoding="utf-8")) == schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert_all_collections_are_bounded(schema)


@pytest.mark.parametrize("task_type", sorted(VALID_OUTPUTS))
def test_task_outputs_forbid_extra_fields(task_type: str):
    payload = {**VALID_OUTPUTS[task_type], "unexpected": "not allowed"}
    with pytest.raises(ProviderOutputError, match="schema"):
        validate_task_output(task_type, payload)


def test_nested_task_outputs_forbid_extra_fields():
    payload = json.loads(json.dumps(VALID_OUTPUTS["suggest_resume_edits"]))
    payload["suggestions"][0]["invented"] = True
    with pytest.raises(ProviderOutputError, match="schema"):
        validate_task_output("suggest_resume_edits", payload)


def test_task_output_string_and_list_limits_are_enforced():
    too_long_string = json.loads(json.dumps(VALID_OUTPUTS["suggest_resume_edits"]))
    too_long_string["suggestions"][0]["replacement_text"] = "x" * 12_001
    with pytest.raises(ProviderOutputError, match="schema"):
        validate_task_output("suggest_resume_edits", too_long_string)

    too_long_list = json.loads(json.dumps(VALID_OUTPUTS["analyze_fit"]))
    too_long_list["matches"] = ["match"] * 51
    with pytest.raises(ProviderOutputError, match="schema"):
        validate_task_output("analyze_fit", too_long_list)


def test_task_output_requires_matching_discriminator():
    payload = {**VALID_OUTPUTS["analyze_fit"], "task_type": "review_claims"}
    with pytest.raises(ProviderOutputError, match="schema"):
        validate_task_output("analyze_fit", payload)


def test_only_the_five_documented_task_types_are_allowed():
    assert ALLOWED_TASK_TYPES == (
        "analyze_jd",
        "analyze_fit",
        "suggest_resume_edits",
        "draft_cover_letter",
        "review_claims",
    )
    with pytest.raises(ProviderOutputError, match="Unsupported task type"):
        output_schema_for_task("invent_resume_fact")


def test_provider_request_requires_supported_task_and_existing_contained_inputs(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    assert request.task_dir.is_absolute()
    assert request.prompt_path.is_file()

    values = request.model_dump()
    values["task_type"] = "invent_resume_fact"
    with pytest.raises(ValidationError):
        ProviderRequest(**values)

    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    values = request.model_dump()
    values["context_path"] = outside
    with pytest.raises(ValidationError, match="inside task_dir"):
        ProviderRequest(**values)

    request.prompt_path.unlink()
    values = request.model_dump()
    with pytest.raises(ValidationError, match="regular file"):
        ProviderRequest(**values)


def test_mock_provider_validates_configured_output(tmp_path: Path):
    request = make_request(tmp_path)
    provider = MockProvider(raw_output={"invented": "shape"})

    with pytest.raises(ProviderOutputError, match="schema"):
        provider.run(request)


def test_mock_provider_returns_validated_result_and_local_privacy(tmp_path: Path):
    request = make_request(tmp_path)
    provider = MockProvider(raw_output=VALID_OUTPUTS["analyze_fit"])

    result = provider.run(request)

    assert isinstance(result.structured_output, AnalyzeFitOutput)
    assert result.provider_kind == "mock"
    assert result.privacy_mode == "local_private"
    assert result.data_leaves_machine is False
    assert result.is_ai is True


def test_mock_provider_rejects_oversized_raw_output_with_typed_error(tmp_path: Path):
    request = make_request(tmp_path)
    raw_output = json.dumps(VALID_OUTPUTS["analyze_fit"]) + (" " * 2_000_001)
    with pytest.raises(ProviderOutputError, match="size limit"):
        MockProvider(raw_output=raw_output).run(request)


def test_deterministic_provider_is_verifier_only_and_never_marked_as_ai(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    observed: list[ProviderRequest] = []

    def verify(value: ProviderRequest) -> dict[str, Any]:
        observed.append(value)
        return VALID_OUTPUTS["analyze_fit"]

    result = DeterministicProvider(verifier=verify).run(request)

    assert observed == [request]
    assert result.provider_kind == "deterministic_verifier"
    assert result.is_ai is False
    assert result.data_leaves_machine is False
    assert result.invocation.mode == "verifier_only"


def test_ollama_probe_reports_unavailable_without_throwing():
    def unavailable(*_args: object, **_kwargs: object):
        raise URLError("secret endpoint detail")

    provider = OllamaProvider(
        endpoint="http://127.0.0.1:11434",
        model="qwen2.5:7b",
        timeout_seconds=1,
        urlopen=unavailable,
    )

    status = provider.availability()

    assert status.available is False
    assert status.privacy_mode == "local_private"
    assert status.data_leaves_machine is False
    assert "secret endpoint detail" not in status.detail


def test_ollama_probe_rejects_malformed_response_without_throwing():
    provider = OllamaProvider(
        endpoint="http://127.0.0.1:11434",
        model="qwen2.5:7b",
        timeout_seconds=1,
        urlopen=lambda *_args, **_kwargs: FakeHttpResponse(b"not-json"),
    )
    assert provider.availability().available is False


def test_ollama_requires_a_finite_positive_timeout():
    with pytest.raises(ValueError, match="positive"):
        OllamaProvider(timeout_seconds=0)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:11434?api_key=private",
        "http://127.0.0.1:11434#private",
        "http://user:private@127.0.0.1:11434",
        "not-an-http-url",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_ollama_invalid_endpoint_is_unavailable_without_throwing(endpoint: str):
    provider = OllamaProvider(endpoint=endpoint)
    status = provider.availability()

    assert status.available is False
    assert status.detail == "invalid_configuration"


def test_ollama_local_private_mode_rejects_remote_hosts():
    provider = OllamaProvider(endpoint="https://ollama.example.com")
    assert provider.availability().available is False


def test_ollama_invalid_configuration_fails_run_with_typed_sanitized_error(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    provider = OllamaProvider(endpoint="http://user:private@127.0.0.1:11434")

    with pytest.raises(ProviderUnavailableError) as captured:
        provider.run(request)

    assert "private" not in str(captured.value)


def test_ollama_posts_chat_schema_and_validates_content(tmp_path: Path):
    request = make_request(tmp_path)
    observed: dict[str, Any] = {}

    def open_request(http_request: Any, timeout: float):
        observed["url"] = http_request.full_url
        observed["method"] = http_request.method
        observed["body"] = json.loads(http_request.data)
        observed["timeout"] = timeout
        return response_with_json(
            {"message": {"content": json.dumps(VALID_OUTPUTS["analyze_fit"])}}
        )

    provider = OllamaProvider(
        endpoint="http://127.0.0.1:11434",
        model="qwen2.5:7b",
        timeout_seconds=3,
        urlopen=open_request,
    )
    result = provider.run(request)

    assert observed["url"] == "http://127.0.0.1:11434/api/chat"
    assert observed["method"] == "POST"
    assert observed["timeout"] == 3
    assert observed["body"]["stream"] is False
    assert observed["body"]["format"] == output_schema_for_task("analyze_fit")
    assert observed["body"]["messages"] == [
        {"role": "system", "content": "Return only evidence-grounded JSON."},
        {"role": "user", "content": '{"resume":"Python"}'},
    ]
    assert isinstance(result.structured_output, AnalyzeFitOutput)
    assert result.model == "qwen2.5:7b"
    assert result.data_leaves_machine is False
    assert result.is_ai is True


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (socket.timeout("private timeout detail"), ProviderTimeoutError),
        (TimeoutError("private timeout detail"), ProviderTimeoutError),
        (URLError("private transport detail"), ProviderUnavailableError),
        (
            HTTPError(
                "http://127.0.0.1/api/chat",
                500,
                "private server detail",
                hdrs=None,
                fp=None,
            ),
            ProviderInvocationError,
        ),
    ],
)
def test_ollama_transport_failures_are_typed_and_sanitized(
    tmp_path: Path, failure: Exception, error_type: type[Exception]
):
    request = make_request(tmp_path)

    def fail(*_args: object, **_kwargs: object):
        raise failure

    provider = OllamaProvider(urlopen=fail, timeout_seconds=1)
    with pytest.raises(error_type) as captured:
        provider.run(request)

    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    "response",
    [
        FakeHttpResponse(b"not-json"),
        response_with_json({"response": "wrong envelope"}),
        response_with_json({"message": {"content": "not-json"}}),
        response_with_json({"message": {"content": '{"invented":true}'}}),
    ],
)
def test_ollama_rejects_malformed_or_schema_invalid_output(
    tmp_path: Path, response: FakeHttpResponse
):
    request = make_request(tmp_path)
    provider = OllamaProvider(urlopen=lambda *_args, **_kwargs: response)
    with pytest.raises(ProviderOutputError):
        provider.run(request)


def test_ollama_wraps_input_file_races_without_exposing_paths(tmp_path: Path):
    request = make_request(tmp_path)
    request.context_path.unlink()
    provider = OllamaProvider(
        urlopen=lambda *_args, **_kwargs: pytest.fail("network must not be called")
    )

    with pytest.raises(
        ProviderInvocationError, match="input could not be read"
    ) as captured:
        provider.run(request)

    assert str(request.context_path) not in str(captured.value)


def make_successful_codex_runner(
    output: dict[str, Any], calls: list[tuple[list[str], dict[str, Any]]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.144.5\n", "")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(output), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def make_fake_executable(tmp_path: Path, name: str = "codex2.cmd") -> Path:
    executable = tmp_path / name
    executable.write_text("test fixture", encoding="utf-8")
    return executable


def test_codex_command_is_read_only_bounded_and_uses_stdin(tmp_path: Path):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    provider = CodexProvider(
        executable="codex2",
        timeout_seconds=30,
        which=lambda value: str(executable) if value == "codex2" else None,
        run_process=make_successful_codex_runner(
            VALID_OUTPUTS["analyze_fit"], calls
        ),
    )

    result = provider.run(request)

    version_call, execution_call = calls
    assert version_call[0] == [str(executable.resolve()), "--version"]
    command, kwargs = execution_call
    assert command[:2] == [str(executable.resolve()), "exec"]
    assert command[command.index("--sandbox") : command.index("--sandbox") + 2] == [
        "--sandbox",
        "read-only",
    ]
    assert command[command.index("--cd") + 1] == str(request.task_dir)
    assert command[command.index("--output-schema") + 1] == str(request.schema_path)
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert "--output-last-message" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert all("Return only evidence" not in argument for argument in command)
    assert kwargs["input"] == (
        "Return only evidence-grounded JSON.\n\n"
        "<careerops_context>\n"
        '{"resume":"Python"}\n'
        "</careerops_context>"
    )
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 30
    assert result.invocation.executable == str(executable.resolve())
    assert result.invocation.executable_version == "codex-cli 0.144.5"
    assert result.invocation.command == tuple(command)
    assert result.data_leaves_machine is True
    assert result.privacy_mode == "cloud_review"
    assert result.is_ai is True


def test_codex_discovery_preserves_order_default_and_resolved_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("CAREEROPS_CODEX_COMMAND", raising=False)
    monkeypatch.setenv(
        "CAREEROPS_CODEX_COMMANDS", " codex2, codex, CODEX2, ,codex "
    )
    resolved = {
        "codex2": "C:/bin/codex2.cmd",
        "codex": "C:/bin/codex.exe",
    }

    providers = discover_codex_providers(which=lambda value: resolved.get(value))

    assert [provider.executable_name for provider in providers] == ["codex2", "codex"]
    assert [provider.executable for provider in providers] == [
        "C:/bin/codex2.cmd",
        "C:/bin/codex.exe",
    ]
    assert [provider.is_default for provider in providers] == [True, False]


def test_codex_single_command_override_wins_and_empty_values_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CAREEROPS_CODEX_COMMAND", " codex ")
    monkeypatch.setenv("CAREEROPS_CODEX_COMMANDS", "codex2,codex")
    providers = discover_codex_providers(which=lambda value: f"C:/bin/{value}.exe")
    assert [item.executable_name for item in providers] == ["codex"]

    monkeypatch.setenv("CAREEROPS_CODEX_COMMAND", " , , ")
    assert discover_codex_providers(which=lambda value: value) == []


def test_codex_explicit_empty_environment_does_not_read_process_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CAREEROPS_CODEX_COMMAND", "process-only-codex")
    providers = discover_codex_providers(
        environment={}, which=lambda value: f"C:/bin/{value}"
    )
    assert [item.executable_name for item in providers] == ["codex2", "codex"]


def test_codex_default_discovery_prefers_codex2(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("CAREEROPS_CODEX_COMMAND", raising=False)
    monkeypatch.delenv("CAREEROPS_CODEX_COMMANDS", raising=False)
    providers = discover_codex_providers(which=lambda value: f"C:/bin/{value}")
    assert [item.executable_name for item in providers] == ["codex2", "codex"]
    assert providers[0].is_default is True


def test_missing_selected_codex_identity_never_falls_back(tmp_path: Path):
    request = make_request(tmp_path)
    observed: list[str] = []

    def which(value: str) -> None:
        observed.append(value)
        return None

    provider = CodexProvider(executable="missing-codex2", which=which)
    with pytest.raises(ProviderUnavailableError, match="missing-codex2"):
        provider.run(request)

    assert observed == ["missing-codex2"]


def test_codex_availability_never_raises_and_does_not_expose_process_output(
    tmp_path: Path,
):
    executable = make_fake_executable(tmp_path)

    def fail(_command: list[str], **_kwargs: Any):
        raise OSError("private process detail")

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=fail,
    )
    status = provider.availability()
    assert status.available is False
    assert status.data_leaves_machine is True
    assert "private process detail" not in status.detail


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("codex2", 1), TimeoutError()]
)
def test_codex_timeout_is_typed_and_does_not_retry(
    tmp_path: Path, failure: Exception
):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any):
        calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1", "")
        raise failure

    provider = CodexProvider(
        executable="codex2",
        timeout_seconds=1,
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(ProviderTimeoutError, match="codex2"):
        provider.run(request)

    assert len(calls) == 2


def test_codex_nonzero_exit_is_sanitized_and_not_retried(tmp_path: Path):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any):
        calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1", "")
        return subprocess.CompletedProcess(
            command, 7, "private prompt echo", "api_key=private-secret"
        )

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(ProviderInvocationError) as captured:
        provider.run(request)

    message = str(captured.value)
    assert "exit code 7" in message
    assert "private" not in message
    assert len(calls) == 2


@pytest.mark.parametrize("output_mode", ["missing", "malformed", "schema_invalid"])
def test_codex_requires_valid_output_file(tmp_path: Path, output_mode: str):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)

    def run(command: list[str], **_kwargs: Any):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1", "")
        output_path = Path(command[command.index("--output-last-message") + 1])
        if output_mode == "malformed":
            output_path.write_text("not-json", encoding="utf-8")
        elif output_mode == "schema_invalid":
            output_path.write_text('{"invented":true}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(ProviderOutputError):
        provider.run(request)


def test_codex_rejects_output_symlink_created_by_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)
    process_finished = False

    def run(command: list[str], **_kwargs: Any):
        nonlocal process_finished
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1", "")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(VALID_OUTPUTS["analyze_fit"]), encoding="utf-8"
        )
        process_finished = True
        return subprocess.CompletedProcess(command, 0, "", "")

    original_is_symlink = Path.is_symlink

    def reports_process_replaced_output(path: Path) -> bool:
        if process_finished and path.name == "codex-output-task_123.json":
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", reports_process_replaced_output)

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(ProviderOutputError, match="regular file"):
        provider.run(request)


def test_codex_version_failure_prevents_task_execution(tmp_path: Path):
    request = make_request(tmp_path)
    executable = make_fake_executable(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "private", "private")

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(ProviderUnavailableError, match="codex2"):
        provider.run(request)

    assert len(calls) == 1


def test_codex_wraps_input_file_races_without_starting_execution(tmp_path: Path):
    request = make_request(tmp_path)
    request.prompt_path.unlink()
    executable = make_fake_executable(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "codex-cli 1", "")

    provider = CodexProvider(
        executable="codex2",
        which=lambda _value: str(executable),
        run_process=run,
    )
    with pytest.raises(
        ProviderInvocationError, match="input could not be read"
    ) as captured:
        provider.run(request)

    assert str(request.prompt_path) not in str(captured.value)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable semantics")
@pytest.mark.parametrize("kind", ["cmd", "exe"])
def test_codex_version_probe_invokes_cmd_and_exe_directly(
    tmp_path: Path, kind: str
):
    if kind == "cmd":
        executable = tmp_path / "fixture-codex.cmd"
        executable.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" (echo fixture-codex 1.0& exit /b 0)\r\n"
            "exit /b 2\r\n",
            encoding="utf-8",
        )
    else:
        executable = Path(sys.executable)

    provider = CodexProvider(
        executable=str(executable), executable_name=f"codex.{kind}"
    )
    status = provider.availability()

    assert status.available is True
    assert status.executable == str(executable.resolve())
