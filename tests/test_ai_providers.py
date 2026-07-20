from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import careerops.ai.contracts as ai_contracts
import careerops.ai.providers.codex as codex_provider_module
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
from careerops.core.repository import CareerOpsRepository
from careerops.core.status import PrivacyMode


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


def make_request(
    tmp_path: Path,
    task_type: str = "analyze_fit",
    expected_claim_ids: list[str] | None = None,
) -> ProviderRequest:
    task_dir = tmp_path / f"task-{task_type}"
    task_dir.mkdir(parents=True)
    prompt_path = task_dir / "prompt.txt"
    context_path = task_dir / "context.json"
    schema_path = task_dir / "schema.json"
    prompt_path.write_text("Return only evidence-grounded JSON.", encoding="utf-8")
    context_path.write_text('{"resume":"Python"}', encoding="utf-8")
    schema_path.write_text(
        json.dumps(output_schema_for_task(task_type)), encoding="utf-8"
    )
    values: dict[str, Any] = {
        "task_id": "task_123",
        "task_type": task_type,
        "task_dir": task_dir,
        "prompt_path": prompt_path,
        "context_path": context_path,
        "schema_path": schema_path,
    }
    if expected_claim_ids is not None:
        values["expected_claim_ids"] = expected_claim_ids
    return ProviderRequest(
        **values,
    )


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


OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_DIGEST = "a" * 64


class _OllamaTestServer(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def ollama_test_server(responder: Callable[[BaseHTTPRequestHandler], None]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            responder(self)

        def do_POST(self) -> None:
            responder(self)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = _OllamaTestServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def send_http_bytes(
    handler: BaseHTTPRequestHandler, status: int, body: bytes, **headers: str
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    for name, value in headers.items():
        handler.send_header(name.replace("_", "-"), value)
    handler.end_headers()
    try:
        handler.wfile.write(body)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def send_http_json(
    handler: BaseHTTPRequestHandler, status: int, payload: Any
) -> None:
    send_http_bytes(
        handler,
        status,
        json.dumps(payload).encode("utf-8"),
        Content_Type="application/json",
    )


def local_model_metadata(**overrides: Any) -> dict[str, Any]:
    return {
        "name": OLLAMA_MODEL,
        "model": OLLAMA_MODEL,
        "digest": OLLAMA_DIGEST,
        "size": 1_024,
        **overrides,
    }


def successful_ollama_responder(
    observed: dict[str, Any],
    *,
    model_metadata: dict[str, Any] | None = None,
    chat_payload: Any | None = None,
) -> Callable[[BaseHTTPRequestHandler], None]:
    metadata = model_metadata or local_model_metadata()
    envelope = chat_payload or {
        "model": OLLAMA_MODEL,
        "done": True,
        "message": {"content": json.dumps(VALID_OUTPUTS["analyze_fit"])},
    }

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.setdefault("paths", []).append(handler.path)
        if handler.path == "/api/tags":
            send_http_json(handler, 200, {"models": [metadata]})
            return
        if handler.path == "/api/chat":
            length = int(handler.headers.get("Content-Length", "0"))
            body = handler.rfile.read(length)
            observed["chat"] = json.loads(body)
            send_http_json(handler, 200, envelope)
            return
        send_http_json(handler, 404, {"error": "missing"})

    return respond


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
        "http://0.0.0.0:11434",
        "http://192.168.1.10:11434",
        "https://ollama.example.com",
    ],
)
def test_ollama_invalid_or_nonlocal_endpoint_is_unavailable(endpoint: str):
    provider = OllamaProvider(endpoint=endpoint)
    status = provider.availability()

    assert status.available is False
    assert status.detail == "invalid_configuration"


def test_ollama_invalid_configuration_fails_run_with_typed_sanitized_error(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    provider = OllamaProvider(endpoint="http://user:private@127.0.0.1:11434")

    with pytest.raises(ProviderUnavailableError) as captured:
        provider.run(request)

    assert "private" not in str(captured.value)


def test_ollama_real_transport_ignores_hostile_proxy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = make_request(tmp_path)
    observed: dict[str, Any] = {}
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    with ollama_test_server(successful_ollama_responder(observed)) as endpoint:
        provider = OllamaProvider(endpoint=endpoint, model=OLLAMA_MODEL)
        status = provider.availability()
        result = provider.run(request)

    assert status.available is True
    assert status.model_digest == OLLAMA_DIGEST
    assert result.invocation.model_digest == OLLAMA_DIGEST
    assert result.privacy_mode is PrivacyMode.LOCAL_PRIVATE
    assert observed["chat"]["stream"] is False
    assert observed["chat"]["format"] == output_schema_for_task("analyze_fit")
    assert observed["chat"]["messages"] == [
        {"role": "system", "content": "Return only evidence-grounded JSON."},
        {"role": "user", "content": '{"resume":"Python"}'},
    ]


def test_ollama_redirect_is_rejected_without_following(tmp_path: Path):
    request = make_request(tmp_path)
    observed: list[str] = []

    def redirect(handler: BaseHTTPRequestHandler) -> None:
        observed.append(handler.path)
        send_http_bytes(
            handler,
            302,
            b"redirect",
            Location="http://example.com/private",
        )

    with ollama_test_server(redirect) as endpoint:
        provider = OllamaProvider(endpoint=endpoint)
        assert provider.availability().available is False
        with pytest.raises(ProviderInvocationError):
            provider.run(request)

    assert observed == ["/api/tags", "/api/tags"]


def test_ollama_slow_drip_obeys_one_overall_deadline(tmp_path: Path):
    request = make_request(tmp_path)

    def slow(handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/api/tags":
            send_http_json(handler, 200, {"models": [local_model_metadata()]})
            return
        body = json.dumps(
            {
                "model": OLLAMA_MODEL,
                "done": True,
                "message": {"content": json.dumps(VALID_OUTPUTS["analyze_fit"])},
            }
        ).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        for value in body:
            try:
                handler.wfile.write(bytes([value]))
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.02)

    with ollama_test_server(slow) as endpoint:
        provider = OllamaProvider(endpoint=endpoint, timeout_seconds=0.12)
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError):
            provider.run(request)
        elapsed = time.monotonic() - started

    assert elapsed < 0.8


def test_ollama_rejects_oversized_response_incrementally(tmp_path: Path):
    request = make_request(tmp_path)

    def oversized(handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/api/tags":
            send_http_json(handler, 200, {"models": [local_model_metadata()]})
            return
        send_http_bytes(
            handler,
            200,
            b"x" * (ai_contracts.MAX_PROVIDER_OUTPUT_BYTES + 1),
        )

    with ollama_test_server(oversized) as endpoint:
        with pytest.raises(ProviderOutputError, match="size limit"):
            OllamaProvider(endpoint=endpoint).run(request)


def test_ollama_http_error_is_typed_sanitized_and_not_retried(tmp_path: Path):
    request = make_request(tmp_path)
    chat_calls = 0

    def fail(handler: BaseHTTPRequestHandler) -> None:
        nonlocal chat_calls
        if handler.path == "/api/tags":
            send_http_json(handler, 200, {"models": [local_model_metadata()]})
            return
        chat_calls += 1
        send_http_json(handler, 500, {"api_key": "private-server-secret"})

    with ollama_test_server(fail) as endpoint:
        with pytest.raises(ProviderInvocationError) as captured:
            OllamaProvider(endpoint=endpoint).run(request)

    assert chat_calls == 1
    assert "private-server-secret" not in str(captured.value)


@pytest.mark.parametrize(
    "chat_body",
    [
        b"not-json",
        json.dumps({"response": "wrong envelope"}).encode(),
        json.dumps(
            {
                "model": "other-model",
                "done": True,
                "message": {"content": json.dumps(VALID_OUTPUTS["analyze_fit"])},
            }
        ).encode(),
        json.dumps(
            {
                "model": OLLAMA_MODEL,
                "done": False,
                "message": {"content": json.dumps(VALID_OUTPUTS["analyze_fit"])},
            }
        ).encode(),
        json.dumps(
            {"model": OLLAMA_MODEL, "done": True, "message": {"content": "bad"}}
        ).encode(),
    ],
)
def test_ollama_rejects_malformed_incomplete_or_wrong_model_chat(
    tmp_path: Path, chat_body: bytes
):
    request = make_request(tmp_path)

    def malformed(handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/api/tags":
            send_http_json(handler, 200, {"models": [local_model_metadata()]})
        else:
            send_http_bytes(handler, 200, chat_body)

    with ollama_test_server(malformed) as endpoint:
        with pytest.raises(ProviderOutputError):
            OllamaProvider(endpoint=endpoint).run(request)


@pytest.mark.parametrize(
    "metadata",
    [
        local_model_metadata(digest="not-a-digest"),
        local_model_metadata(size=0),
        local_model_metadata(remote_host="ollama.com"),
        local_model_metadata(remote_model="gpt-cloud"),
        local_model_metadata(details={"cloud": True}),
        local_model_metadata(model="qwen2.5:7b-cloud"),
        local_model_metadata(model="other-local-model:7b"),
    ],
)
def test_ollama_rejects_unproven_or_remote_model_metadata(
    tmp_path: Path, metadata: dict[str, Any]
):
    request = make_request(tmp_path)
    observed: dict[str, Any] = {}
    with ollama_test_server(
        successful_ollama_responder(observed, model_metadata=metadata)
    ) as endpoint:
        provider = OllamaProvider(endpoint=endpoint)
        assert provider.availability().available is False
        with pytest.raises(ProviderUnavailableError):
            provider.run(request)
    assert "/api/chat" not in observed.get("paths", [])


def test_ollama_rejects_nested_container_remote_metadata_with_typed_error(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    metadata = local_model_metadata(remote_host={"value": "private.example"})
    observed: dict[str, Any] = {}

    with ollama_test_server(
        successful_ollama_responder(observed, model_metadata=metadata)
    ) as endpoint:
        with pytest.raises(ProviderUnavailableError):
            OllamaProvider(endpoint=endpoint).run(request)

    assert "/api/chat" not in observed.get("paths", [])


def test_ollama_uses_sealed_snapshots_and_request_aware_validation(tmp_path: Path):
    request = make_request(
        tmp_path, "review_claims", expected_claim_ids=["claim_1", "claim_2"]
    )
    request.prompt_path.write_text("replacement prompt", encoding="utf-8")
    request.context_path.write_text("replacement context", encoding="utf-8")
    request.schema_path.write_text('{"replacement":true}', encoding="utf-8")
    observed: dict[str, Any] = {}
    chat = {
        "model": OLLAMA_MODEL,
        "done": True,
        "message": {"content": json.dumps(VALID_OUTPUTS["review_claims"])},
    }

    with ollama_test_server(
        successful_ollama_responder(observed, chat_payload=chat)
    ) as endpoint:
        with pytest.raises(ProviderOutputError, match="claim coverage"):
            OllamaProvider(endpoint=endpoint).run(request)

    assert observed["chat"]["messages"][0]["content"] == (
        "Return only evidence-grounded JSON."
    )
    assert observed["chat"]["messages"][1]["content"] == '{"resume":"Python"}'
    assert observed["chat"]["format"] == output_schema_for_task("review_claims")


def test_review_claims_requires_unique_bounded_expected_ids(tmp_path: Path):
    request = make_request(
        tmp_path, "review_claims", expected_claim_ids=["claim_1", "claim_2"]
    )
    assert request.expected_claim_ids == ("claim_1", "claim_2")

    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    with pytest.raises(ValidationError, match="unique"):
        make_request(
            duplicate_root,
            "review_claims",
            expected_claim_ids=["claim_1", "claim_1"],
        )

    non_review_root = tmp_path / "non-review"
    non_review_root.mkdir()
    with pytest.raises(ValidationError, match="review_claims"):
        make_request(
            non_review_root,
            "analyze_fit",
            expected_claim_ids=["claim_1"],
        )

    with pytest.raises(ValidationError):
        make_request(
            tmp_path / "too-many",
            "review_claims",
            expected_claim_ids=[f"claim_{index}" for index in range(41)],
        )
    with pytest.raises(ValidationError):
        make_request(
            tmp_path / "too-long",
            "review_claims",
            expected_claim_ids=["a" * 129],
        )


@pytest.mark.parametrize(
    "decisions",
    [
        [],
        [
            VALID_OUTPUTS["review_claims"]["decisions"][0],
            VALID_OUTPUTS["review_claims"]["decisions"][0],
        ],
        [
            {
                **VALID_OUTPUTS["review_claims"]["decisions"][0],
                "claim_id": "foreign_claim",
            }
        ],
    ],
)
def test_review_claims_rejects_missing_duplicate_or_foreign_decisions(
    tmp_path: Path, decisions: list[dict[str, Any]]
):
    request = make_request(
        tmp_path, "review_claims", expected_claim_ids=["claim_1", "claim_2"]
    )
    payload = {"task_type": "review_claims", "decisions": decisions}
    with pytest.raises(ProviderOutputError, match="claim coverage"):
        ai_contracts.validate_provider_output(request, payload)


def test_review_claims_accepts_exact_reordered_coverage(tmp_path: Path):
    request = make_request(
        tmp_path, "review_claims", expected_claim_ids=["claim_1", "claim_2"]
    )
    base = VALID_OUTPUTS["review_claims"]["decisions"][0]
    payload = {
        "task_type": "review_claims",
        "decisions": [
            {**base, "claim_id": "claim_2"},
            {**base, "claim_id": "claim_1"},
        ],
    }
    output = ai_contracts.validate_provider_output(request, payload)
    assert tuple(item.claim_id for item in output.decisions) == (
        "claim_2",
        "claim_1",
    )


@pytest.mark.parametrize("provider_kind", ["mock", "deterministic"])
def test_all_in_process_providers_apply_request_aware_claim_coverage(
    tmp_path: Path, provider_kind: str
):
    request = make_request(
        tmp_path, "review_claims", expected_claim_ids=["claim_1", "claim_2"]
    )
    invalid = VALID_OUTPUTS["review_claims"]
    provider = (
        MockProvider(raw_output=invalid)
        if provider_kind == "mock"
        else DeterministicProvider(verifier=lambda _request: invalid)
    )
    with pytest.raises(ProviderOutputError, match="claim coverage"):
        provider.run(request)


def test_provider_request_seals_inputs_and_ignores_later_replacement(tmp_path: Path):
    request = make_request(tmp_path)
    original_prompt = request.prompt_path.read_bytes()
    original_context = request.context_path.read_bytes()
    original_schema = request.schema_path.read_bytes()

    request.prompt_path.write_text("replacement prompt", encoding="utf-8")
    request.context_path.write_text("replacement context", encoding="utf-8")
    request.schema_path.write_text('{"replacement":true}', encoding="utf-8")

    assert request.prompt_snapshot == original_prompt
    assert request.context_snapshot == original_context
    assert request.schema_snapshot == original_schema


def test_provider_request_rejects_hard_linked_input(tmp_path: Path):
    request = make_request(tmp_path)
    linked = request.task_dir / "prompt-copy.txt"
    os.link(request.prompt_path, linked)

    with pytest.raises(ValidationError, match="single-link"):
        ProviderRequest(**request.model_dump())


def test_provider_request_rejects_symlink_input_when_supported(tmp_path: Path):
    request = make_request(tmp_path)
    target = request.task_dir / "prompt-target.txt"
    target.write_text("target", encoding="utf-8")
    link = request.task_dir / "prompt-link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this test user")
    values = request.model_dump()
    values["prompt_path"] = link

    with pytest.raises(ValidationError, match="symlink"):
        ProviderRequest(**values)


def test_provider_request_rejects_oversized_input_snapshot(tmp_path: Path):
    request = make_request(tmp_path)
    request.context_path.write_bytes(
        b"x" * (ai_contracts.MAX_PROVIDER_CONTEXT_BYTES + 1)
    )

    with pytest.raises(ValidationError, match="size limit"):
        ProviderRequest(**request.model_dump())


def test_validated_output_collections_are_deeply_immutable():
    fit = validate_task_output("analyze_fit", VALID_OUTPUTS["analyze_fit"])
    edits = validate_task_output(
        "suggest_resume_edits", VALID_OUTPUTS["suggest_resume_edits"]
    )

    assert isinstance(fit.matches, tuple)
    assert isinstance(edits.suggestions, tuple)
    assert isinstance(edits.suggestions[0].evidence_refs, tuple)
    with pytest.raises(AttributeError):
        fit.matches.append("mutation")
    with pytest.raises(AttributeError):
        edits.suggestions[0].evidence_refs.append("mutation")


def test_canonical_output_serialization_is_bounded_utf8():
    output = validate_task_output("analyze_fit", VALID_OUTPUTS["analyze_fit"])
    raw = ai_contracts.serialize_provider_output(output)

    assert raw.encode("utf-8") == ai_contracts.serialize_provider_output_bytes(output)
    assert len(raw.encode("utf-8")) <= ai_contracts.MAX_PROVIDER_OUTPUT_BYTES
    assert ": " not in raw
    assert ", " not in raw


def test_largest_schema_valid_outputs_fit_the_canonical_byte_limit():
    wide = "\U0010ffff"
    identifier = "a" * 128
    payloads = {
        "analyze_jd": {
            "task_type": "analyze_jd",
            "normalized_title": wide * 300,
            "normalized_company": wide * 300,
            "normalized_location": wide * 300,
            "responsibilities": [wide * 500] * 20,
            "required_skills": [wide * 500] * 30,
            "preferred_skills": [wide * 500] * 30,
            "keywords": [wide * 300] * 50,
            "uncertainties": [wide * 500] * 20,
        },
        "analyze_fit": {
            "task_type": "analyze_fit",
            "score": 100,
            "matches": [wide * 500] * 30,
            "gaps": [wide * 500] * 30,
            "risks": [wide * 500] * 30,
            "recommendations": [wide * 500] * 30,
        },
        "suggest_resume_edits": {
            "task_type": "suggest_resume_edits",
            "suggestions": [
                {
                    "source_text": wide * 4_000,
                    "replacement_text": wide * 4_000,
                    "reason": wide * 1_000,
                    "evidence_refs": [identifier] * 20,
                }
            ]
            * 8,
        },
        "draft_cover_letter": {
            "task_type": "draft_cover_letter",
            "plain_text": wide * 12_000,
            "evidence_refs": [identifier] * 40,
        },
        "review_claims": {
            "task_type": "review_claims",
            "decisions": [
                {
                    "claim_id": f"claim_{index}",
                    "decision": "needs_review",
                    "reason": wide * 1_000,
                    "evidence_refs": [identifier] * 20,
                }
                for index in range(40)
            ],
        },
    }

    for task_type, payload in payloads.items():
        output = validate_task_output(task_type, payload)
        assert (
            len(ai_contracts.serialize_provider_output_bytes(output))
            <= ai_contracts.MAX_PROVIDER_OUTPUT_BYTES
        )


def test_deterministic_callback_failure_is_typed_and_sanitized(tmp_path: Path):
    request = make_request(tmp_path)

    def fail(_request: ProviderRequest):
        raise RuntimeError("api_key=private-callback-secret")

    with pytest.raises(ProviderInvocationError) as captured:
        DeterministicProvider(verifier=fail).run(request)

    assert "private-callback-secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_deterministic_mapping_failure_is_typed_and_sanitized(tmp_path: Path):
    class HostileMapping:
        def keys(self):
            raise RuntimeError("api_key=private-verifier-secret")

        def __getitem__(self, _key: str):
            raise AssertionError("unreachable")

    provider = DeterministicProvider(verifier=lambda _request: HostileMapping())
    with pytest.raises(ProviderOutputError) as captured:
        provider.run(make_request(tmp_path))

    assert "private-verifier-secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_mock_mapping_failure_is_typed_and_sanitized(tmp_path: Path):
    class HostileMapping:
        def keys(self):
            raise RuntimeError("api_key=private-mapping-secret")

        def __getitem__(self, _key: str):
            raise AssertionError("unreachable")

    with pytest.raises(ProviderOutputError) as captured:
        MockProvider(raw_output=HostileMapping()).run(make_request(tmp_path))

    assert "private-mapping-secret" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("provider", "expected_privacy"),
    [
        (MockProvider(raw_output=VALID_OUTPUTS["analyze_fit"]), PrivacyMode.LOCAL_PRIVATE),
        (
            DeterministicProvider(
                verifier=lambda _request: VALID_OUTPUTS["analyze_fit"]
            ),
            PrivacyMode.VERIFIER_ONLY,
        ),
    ],
)
def test_provider_result_privacy_enum_persists_to_provider_and_task(
    tmp_path: Path, provider: Any, expected_privacy: PrivacyMode
):
    request = make_request(tmp_path)
    result = provider.run(request)
    assert result.privacy_mode is expected_privacy

    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="India",
        source_url="https://example.com/job",
        jd_text="Build reliable services",
    )
    config = repo.upsert_ai_provider(
        provider_kind=result.provider_kind,
        display_name=result.provider_kind,
        privacy_mode=result.privacy_mode,
        endpoint=result.invocation.endpoint,
        model=result.model,
        enabled=True,
    )
    task = repo.create_ai_task(
        application_id=application.id,
        task_type="analyze_fit",
        provider_config_id=config.id,
        prompt_template_id="analyze_fit_v1",
        input_artifact_ids=[],
        privacy_mode=result.privacy_mode,
    )

    assert repo.get_ai_provider(config.id).privacy_mode is expected_privacy
    assert repo.get_ai_task(task.id).privacy_mode is expected_privacy


def process_helpers():
    return importlib.import_module("careerops.ai.providers.process")


def make_codex_fixture(tmp_path: Path) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    user_profile = tmp_path / "user"
    default_home = user_profile / ".codex"
    account2_home = user_profile / "codex-account2" / ".codex"
    default_home.mkdir(parents=True)
    account2_home.mkdir(parents=True)
    native = bin_dir / "codex.exe"
    native.write_bytes(b"MZ\x00fixture-native-codex")
    wrapper = bin_dir / "codex2.cmd"
    wrapper.write_text(
        "@echo off\r\n"
        'set "CODEX_HOME=%USERPROFILE%\\codex-account2\\.codex"\r\n'
        "codex %*\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
        newline="",
    )
    environment = {
        "USERPROFILE": str(user_profile),
        "HOME": str(user_profile),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "WINDIR": os.environ.get("WINDIR", "C:\\Windows"),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "CAREEROPS_TEST_SECRET": "must-not-be-inherited",
    }
    resolved = {"codex2": str(wrapper), "codex": str(native)}
    return {
        "wrapper": wrapper,
        "native": native,
        "user_profile": user_profile,
        "default_home": default_home,
        "account2_home": account2_home,
        "environment": environment,
        "which": lambda name: resolved.get(name),
    }


class FakeCodexRunner:
    def __init__(
        self,
        output: dict[str, Any] | None = None,
        *,
        version: str = "codex-cli 0.144.5\n",
        login: str = "Logged in using ChatGPT\n",
        login_stderr: bool = False,
        output_mode: str = "valid",
    ):
        self.output = output or VALID_OUTPUTS["analyze_fit"]
        self.version = version
        self.login = login
        self.login_stderr = login_stderr
        self.output_mode = output_mode
        self.calls: list[dict[str, Any]] = []

    def run(self, command: list[str], **values: Any):
        self.calls.append({"command": command, **values})
        outcome = process_helpers().ProcessOutcome
        if command[-1] == "--version":
            return outcome(0, self.version.encode(), b"")
        if command[-2:] == ["login", "status"]:
            encoded = self.login.encode()
            return (
                outcome(0, b"", encoded)
                if self.login_stderr
                else outcome(0, encoded, b"")
            )
        if self.output_mode == "timeout":
            raise process_helpers().ProcessDeadlineExceeded()
        if self.output_mode == "nonzero":
            return outcome(7, b"private prompt echo", b"api_key=private-secret")
        output_path = Path(command[command.index("--output-last-message") + 1])
        if self.output_mode == "malformed":
            output_path.write_text("not-json", encoding="utf-8")
        elif self.output_mode == "schema_invalid":
            output_path.write_text('{"invented":true}', encoding="utf-8")
        elif self.output_mode == "hardlink":
            outside = output_path.parent.parent / "outside.json"
            outside.write_text(json.dumps(self.output), encoding="utf-8")
            os.link(outside, output_path)
        elif self.output_mode == "valid":
            output_path.write_text(json.dumps(self.output), encoding="utf-8")
        return outcome(0, b"discarded", b"discarded")


def make_hardened_codex_provider(
    tmp_path: Path,
    runner: FakeCodexRunner,
    identity: str = "codex2",
    **environment_overrides: str,
) -> tuple[CodexProvider, dict[str, Any]]:
    fixture = make_codex_fixture(tmp_path)
    environment = {**fixture["environment"], **environment_overrides}
    provider = CodexProvider(
        executable=identity,
        which=fixture["which"],
        environment=environment,
        process_runner=runner,
        timeout_seconds=2,
    )
    return provider, fixture


def test_hardened_codex_discovers_safe_interchangeable_identities(tmp_path: Path):
    fixture = make_codex_fixture(tmp_path)
    runner = FakeCodexRunner()
    providers = discover_codex_providers(
        which=fixture["which"],
        environment=fixture["environment"],
        process_runner=runner,
    )

    assert [item.identity_name for item in providers] == ["codex2", "codex"]
    assert [item.is_default for item in providers] == [True, False]
    assert all(item.native_executable == fixture["native"].resolve() for item in providers)
    assert providers[0].codex_home == fixture["account2_home"].resolve()
    assert providers[1].codex_home == fixture["default_home"].resolve()
    assert all(Path(item.native_executable).suffix.casefold() == ".exe" for item in providers)


def test_native_codex_hash_streams_a_legitimate_current_size(
    monkeypatch: pytest.MonkeyPatch,
):
    current_native_size = 341_195_568
    remaining = current_native_size
    requested_sizes: list[int] = []
    expected = hashlib.sha256()

    def bounded_read(_descriptor: int, requested: int) -> bytes:
        nonlocal remaining
        requested_sizes.append(requested)
        chunk = b"x" * min(requested, remaining)
        remaining -= len(chunk)
        expected.update(chunk)
        return chunk

    monkeypatch.setattr(codex_provider_module.os, "read", bounded_read)

    digest, observed = codex_provider_module._stream_sha256(
        123,
        size_limit=codex_provider_module.MAX_CODEX_EXECUTABLE_BYTES,
    )

    assert observed == current_native_size
    assert digest == expected.hexdigest()
    assert len(requested_sizes) > 1
    assert max(requested_sizes) <= 1024 * 1024
    assert codex_provider_module.MAX_CODEX_EXECUTABLE_BYTES == 1024**3


def test_native_codex_identity_tolerates_only_windows_ctime_representation():
    path_state = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_size=341_195_568,
        st_mtime_ns=3,
        st_ctime_ns=4,
        st_nlink=1,
    )
    opened_state = SimpleNamespace(**vars(path_state))
    opened_state.st_ctime_ns = 5
    changed_state = SimpleNamespace(**vars(opened_state))
    changed_state.st_mtime_ns = 6

    assert codex_provider_module._file_identity(path_state) == (
        codex_provider_module._file_identity(opened_state)
    )
    assert codex_provider_module._file_identity(opened_state) != (
        codex_provider_module._file_identity(changed_state)
    )


def test_native_codex_streaming_hash_detects_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    native = tmp_path / "codex.exe"
    native.write_bytes(b"MZ-fixture")
    stream_sha256 = codex_provider_module._stream_sha256

    def mutate_then_hash(descriptor: int, *, size_limit: int):
        with native.open("ab") as output:
            output.write(b"changed")
            output.flush()
            os.fsync(output.fileno())
        return stream_sha256(descriptor, size_limit=size_limit)

    monkeypatch.setattr(
        codex_provider_module,
        "_stream_sha256",
        mutate_then_hash,
    )

    with pytest.raises(ProviderUnavailableError, match="changed during read"):
        codex_provider_module._validate_codex_native(native)


def test_native_codex_streaming_hash_rejects_hard_link(tmp_path: Path):
    original_dir = tmp_path / "original"
    linked_dir = tmp_path / "linked"
    original_dir.mkdir()
    linked_dir.mkdir()
    original = original_dir / "codex.exe"
    linked = linked_dir / "codex.exe"
    original.write_bytes(b"MZ-fixture")
    os.link(original, linked)

    with pytest.raises(ProviderUnavailableError, match="unsafe"):
        codex_provider_module._validate_codex_native(linked)


def test_codex2_wrapper_reader_remains_capped_at_4_kib(tmp_path: Path):
    fixture = make_codex_fixture(tmp_path)
    fixture["wrapper"].write_bytes(b"x" * 4_097)

    providers = discover_codex_providers(
        which=fixture["which"],
        environment=fixture["environment"],
        process_runner=FakeCodexRunner(),
    )

    assert [item.identity_name for item in providers] == ["codex"]


@pytest.mark.parametrize(
    "wrapper_body",
    [
        "@echo off\r\ncalc.exe\r\ncodex %*\r\nexit /b %ERRORLEVEL%\r\n",
        "@echo off\r\nset CODEX_HOME=C:\\private\r\ncodex %*\r\n",
        '@echo off\r\nset "CODEX_HOME=%USERPROFILE%\\.codex"\r\ncodex %*\r\nexit /b %ERRORLEVEL%\r\n',
        '@echo off\r\nset "CODEX_HOME=%USERPROFILE%\\codex-account2\\.codex"\r\ncall codex %*\r\nexit /b %ERRORLEVEL%\r\n',
    ],
)
def test_hardened_codex_rejects_every_nonexact_batch_wrapper(
    tmp_path: Path, wrapper_body: str
):
    fixture = make_codex_fixture(tmp_path)
    fixture["wrapper"].write_text(wrapper_body, encoding="utf-8")
    providers = discover_codex_providers(
        which=fixture["which"],
        environment=fixture["environment"],
        process_runner=FakeCodexRunner(),
    )

    assert [item.identity_name for item in providers] == ["codex"]


def test_hardened_codex_invokes_native_exe_with_pinned_isolated_command(
    tmp_path: Path,
):
    request = make_request(tmp_path)
    runner = FakeCodexRunner()
    provider, fixture = make_hardened_codex_provider(tmp_path / "provider", runner)

    result = provider.run(request)

    assert len(runner.calls) == 3
    version_call, login_call, execution_call = runner.calls
    assert version_call["command"] == [str(fixture["native"].resolve()), "--version"]
    assert login_call["command"] == [
        str(fixture["native"].resolve()),
        "login",
        "status",
    ]
    command = execution_call["command"]
    assert command[0] == str(fixture["native"].resolve())
    assert not command[0].casefold().endswith((".cmd", ".bat"))
    for required in (
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--model",
        "gpt-5.6-sol",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--output-schema",
        "--output-last-message",
    ):
        assert required in command
    assert 'shell_environment_policy.inherit="none"' in command
    assert 'model_reasoning_effort="xhigh"' in command
    attempt_dir = Path(command[command.index("--cd") + 1])
    assert attempt_dir.parent == request.task_dir
    assert attempt_dir.name.startswith(".careerops-attempt-")
    schema_path = Path(command[command.index("--output-schema") + 1])
    assert schema_path.parent == attempt_dir
    assert schema_path.read_bytes() == request.schema_snapshot
    assert execution_call["input_bytes"] == (
        b"Return only evidence-grounded JSON.\n\n"
        b"<careerops_context>\n"
        b'{"resume":"Python"}\n'
        b"</careerops_context>"
    )
    assert execution_call["capture_limit"] == 0
    process_env = execution_call["environment"]
    assert process_env["CODEX_HOME"] == str(fixture["account2_home"].resolve())
    assert "CAREEROPS_TEST_SECRET" not in process_env
    assert result.privacy_mode is PrivacyMode.CLOUD_EXTERNAL
    assert result.model == "gpt-5.6-sol"
    assert result.invocation.identity_name == "codex2"
    assert result.invocation.executable == str(fixture["native"].resolve())
    assert result.invocation.executable_sha256 == hashlib.sha256(
        fixture["native"].read_bytes()
    ).hexdigest()
    assert result.invocation.executable_version == "codex-cli 0.144.5"
    assert result.invocation.codex_home == str(fixture["account2_home"].resolve())
    assert result.invocation.reasoning_effort == "xhigh"
    assert result.invocation.command == tuple(command)


def test_hardened_codex_explicit_model_effort_and_home_overrides(tmp_path: Path):
    runner = FakeCodexRunner()
    provider, fixture = make_hardened_codex_provider(
        tmp_path,
        runner,
        identity="codex",
        CAREEROPS_CODEX_MODEL="gpt-5.6-sol-test",
        CAREEROPS_CODEX_REASONING_EFFORT="high",
        CAREEROPS_CODEX_HOME=str(tmp_path / "custom-home"),
    )
    (tmp_path / "custom-home").mkdir()
    request = make_request(tmp_path / "request")

    result = provider.run(request)

    command = runner.calls[-1]["command"]
    assert command[command.index("--model") + 1] == "gpt-5.6-sol-test"
    assert 'model_reasoning_effort="high"' in command
    assert runner.calls[-1]["environment"]["CODEX_HOME"] == str(
        (tmp_path / "custom-home").resolve()
    )
    assert result.model == "gpt-5.6-sol-test"
    assert result.invocation.reasoning_effort == "high"
    assert provider.native_executable == fixture["native"].resolve()


def test_hardened_codex_supports_explicit_native_exe_override(tmp_path: Path):
    fixture = make_codex_fixture(tmp_path / "provider")
    runner = FakeCodexRunner()
    provider = CodexProvider(
        executable=str(fixture["native"]),
        executable_name="codex",
        environment=fixture["environment"],
        process_runner=runner,
    )

    result = provider.run(make_request(tmp_path / "request"))

    assert result.invocation.identity_name == "codex"
    assert all(call["command"][0] == str(fixture["native"].resolve()) for call in runner.calls)


def test_hardened_codex_rejects_explicit_batch_override(tmp_path: Path):
    fixture = make_codex_fixture(tmp_path)
    batch = fixture["wrapper"].with_suffix(".bat")
    batch.write_bytes(fixture["wrapper"].read_bytes())
    runner = FakeCodexRunner()
    provider = CodexProvider(
        executable=str(batch),
        environment=fixture["environment"],
        process_runner=runner,
    )

    assert provider.availability().available is False
    with pytest.raises(ProviderUnavailableError):
        provider.run(make_request(tmp_path / "request"))
    assert runner.calls == []


def test_hardened_codex_rejects_python_exe_as_fake_native_identity(tmp_path: Path):
    provider = CodexProvider(
        executable=sys.executable,
        environment={"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)},
        process_runner=FakeCodexRunner(),
    )

    assert provider.availability().available is False
    with pytest.raises(ProviderUnavailableError):
        provider.run(make_request(tmp_path / "request"))


@pytest.mark.parametrize("version", ["Python 3.14\n", "codex-cli 0.144\n"])
def test_hardened_codex_requires_strict_semver_version(
    tmp_path: Path, version: str
):
    runner = FakeCodexRunner(version=version)
    provider, _fixture = make_hardened_codex_provider(tmp_path, runner)

    assert provider.availability().available is False
    with pytest.raises(ProviderUnavailableError):
        provider.run(make_request(tmp_path / "request"))


def test_hardened_codex_requires_chatgpt_login_without_task_data(tmp_path: Path):
    runner = FakeCodexRunner(login="Not logged in\n")
    provider, _fixture = make_hardened_codex_provider(tmp_path, runner)

    assert provider.availability().available is False
    assert len(runner.calls) == 2
    assert all("exec" not in call["command"] for call in runner.calls)


def test_hardened_codex_accepts_exact_chatgpt_login_status_on_stderr(
    tmp_path: Path,
):
    runner = FakeCodexRunner(login_stderr=True)
    provider, _fixture = make_hardened_codex_provider(tmp_path, runner)

    availability = provider.availability()

    assert availability.available is True
    assert availability.detail == "available"
    assert len(runner.calls) == 2
    assert all("exec" not in call["command"] for call in runner.calls)


def test_hardened_codex_missing_selected_identity_never_checks_fallback(
    tmp_path: Path,
):
    fixture = make_codex_fixture(tmp_path)
    observed: list[str] = []

    def which(name: str) -> str | None:
        observed.append(name)
        return str(fixture["native"]) if name == "codex" else None

    provider = CodexProvider(
        executable="codex2",
        which=which,
        environment=fixture["environment"],
        process_runner=FakeCodexRunner(),
    )
    with pytest.raises(ProviderUnavailableError, match="codex2"):
        provider.run(make_request(tmp_path / "request"))

    assert observed == ["codex2"]


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("timeout", ProviderTimeoutError),
        ("nonzero", ProviderInvocationError),
        ("missing", ProviderOutputError),
        ("malformed", ProviderOutputError),
        ("schema_invalid", ProviderOutputError),
        ("hardlink", ProviderOutputError),
    ],
)
def test_hardened_codex_failures_are_typed_sanitized_and_single_attempt(
    tmp_path: Path, mode: str, error_type: type[Exception]
):
    runner = FakeCodexRunner(output_mode=mode)
    provider, _fixture = make_hardened_codex_provider(tmp_path, runner)
    request = make_request(tmp_path / "request")

    with pytest.raises(error_type) as captured:
        provider.run(request)

    assert "private" not in str(captured.value)
    assert len([call for call in runner.calls if "exec" in call["command"]]) == 1


def test_hardened_codex_uses_sealed_inputs_and_request_aware_claims(tmp_path: Path):
    request = make_request(
        tmp_path / "request",
        "review_claims",
        expected_claim_ids=["claim_1", "claim_2"],
    )
    request.prompt_path.write_text("replacement", encoding="utf-8")
    request.context_path.write_text("replacement", encoding="utf-8")
    request.schema_path.write_text("{}", encoding="utf-8")
    runner = FakeCodexRunner(output=VALID_OUTPUTS["review_claims"])
    provider, _fixture = make_hardened_codex_provider(tmp_path / "provider", runner)

    with pytest.raises(ProviderOutputError, match="claim coverage"):
        provider.run(request)

    assert b"replacement" not in runner.calls[-1]["input_bytes"]


def test_hardened_codex_result_privacy_persists_to_repository(tmp_path: Path):
    runner = FakeCodexRunner()
    provider, _fixture = make_hardened_codex_provider(tmp_path / "provider", runner)
    request = make_request(tmp_path / "request")
    result = provider.run(request)
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    application = repo.create_application(
        company="Example",
        role_title="Engineer",
        location="India",
        source_url="https://example.com/job",
        jd_text="Build systems",
    )
    config = repo.upsert_ai_provider(
        provider_kind=result.provider_kind,
        display_name=result.invocation.identity_name,
        privacy_mode=result.privacy_mode,
        endpoint=result.invocation.executable,
        model=result.model,
        enabled=True,
    )
    task = repo.create_ai_task(
        application_id=application.id,
        task_type="analyze_fit",
        provider_config_id=config.id,
        prompt_template_id="analyze_fit_v1",
        input_artifact_ids=[],
        privacy_mode=result.privacy_mode,
    )

    assert repo.get_ai_provider(config.id).privacy_mode is PrivacyMode.CLOUD_EXTERNAL
    assert repo.get_ai_task(task.id).privacy_mode is PrivacyMode.CLOUD_EXTERNAL


def test_hardened_process_runner_bounds_captured_output():
    helper = process_helpers()
    outcome = helper.run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000000)"],
        input_bytes=b"",
        timeout_seconds=5,
        environment=os.environ.copy(),
        capture_limit=1_024,
    )

    assert outcome.returncode == 0
    assert len(outcome.stdout) == 1_024
    assert outcome.stderr == b""


def test_hardened_process_runner_kills_timeout_process_tree(tmp_path: Path):
    helper = process_helpers()
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(10)"
    )

    with pytest.raises(helper.ProcessDeadlineExceeded):
        helper.run_process(
            [sys.executable, "-c", parent_code],
            input_bytes=b"",
            timeout_seconds=0.2,
            environment=os.environ.copy(),
            capture_limit=0,
        )
    time.sleep(1.0)

    assert marker.exists() is False


@pytest.mark.skipif(
    os.environ.get("CAREEROPS_RUN_CODEX_IDENTITY_SMOKE") != "1",
    reason="Opt-in real-machine identity probe",
)
def test_hardened_codex_real_machine_identity_smoke_probe():
    providers = discover_codex_providers()
    assert [item.identity_name for item in providers] == ["codex2", "codex"]
    assert [item.is_default for item in providers] == [True, False]
    assert all(item.model == "gpt-5.6-sol" for item in providers)
    assert all(
        item.native_executable.suffix.casefold() == ".exe" for item in providers
    )
    assert all(
        item.native_executable.name.casefold() == "codex.exe" for item in providers
    )
    assert providers[0].codex_home != providers[1].codex_home

    availability = [item.availability() for item in providers]
    assert all(item.available and item.detail == "available" for item in availability)
    assert [item.identity_name for item in availability] == ["codex2", "codex"]
    assert all(item.model == "gpt-5.6-sol" for item in availability)
    assert all(
        Path(item.executable).name.casefold() == "codex.exe"
        for item in availability
    )
    assert all(
        re.fullmatch(r"codex-cli \d+\.\d+\.\d+", item.executable_version)
        for item in availability
    )
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", item.executable_sha256)
        for item in availability
    )
    assert availability[0].codex_home != availability[1].codex_home
