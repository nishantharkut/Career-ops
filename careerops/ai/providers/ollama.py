from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from careerops.ai.contracts import (
    MAX_PROVIDER_OUTPUT_BYTES,
    InvocationMetadata,
    OllamaIsolationEvidence,
    ProviderAvailability,
    ProviderError,
    ProviderInvocationError,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    parse_json_object,
    validate_provider_output,
)
from careerops.ai.providers.base import build_provider_result, sanitized_provider_error
from careerops.core.status import PrivacyMode


_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_OLLAMA_ENVELOPE_BYTES = (6 * MAX_PROVIDER_OUTPUT_BYTES) + 64_000


@dataclass(frozen=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    base_path: str


@dataclass(frozen=True)
class _LocalModel:
    name: str
    digest: str
    size: int
    tags_metadata_sha256: str
    show_metadata_sha256: str


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderTimeoutError("Ollama provider timed out")
    return remaining


def _response_socket(response: http.client.HTTPResponse) -> socket.socket | None:
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    value = getattr(raw, "_sock", None)
    return value if isinstance(value, socket.socket) else None


class _LoopbackTransport:
    def __init__(self, endpoint: _Endpoint):
        self.endpoint = endpoint

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        deadline: float,
        response_limit: int,
    ) -> tuple[int, bytes]:
        connection_type = (
            http.client.HTTPSConnection
            if self.endpoint.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self.endpoint.host,
            self.endpoint.port,
            timeout=_remaining(deadline),
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.connect()
            peer = connection.sock.getpeername()[0] if connection.sock else ""
            try:
                if not ipaddress.ip_address(peer).is_loopback:
                    raise ProviderUnavailableError(
                        "Ollama endpoint did not resolve to loopback"
                    )
            except ValueError as error:
                raise ProviderUnavailableError(
                    "Ollama endpoint did not resolve to loopback"
                ) from error

            if connection.sock:
                connection.sock.settimeout(_remaining(deadline))
            target = f"{self.endpoint.base_path}{path}"
            headers = {"Accept": "application/json", "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, target, body=body, headers=headers)
            if connection.sock:
                connection.sock.settimeout(_remaining(deadline))
            response = connection.getresponse()
            raw_length = response.getheader("Content-Length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except ValueError as error:
                    raise ProviderOutputError(
                        "Ollama response Content-Length was invalid"
                    ) from error
                if content_length < 0 or content_length > response_limit:
                    raise ProviderOutputError("Ollama response exceeded the size limit")
            return response.status, self._read_body(
                response,
                deadline,
                response_limit,
            )
        except ProviderTimeoutError:
            raise
        except (socket.timeout, TimeoutError) as error:
            raise ProviderTimeoutError("Ollama provider timed out") from error
        except (ProviderOutputError, ProviderUnavailableError):
            raise
        except (OSError, http.client.HTTPException) as error:
            raise ProviderUnavailableError("Ollama provider is unavailable") from error
        finally:
            if response is not None:
                response.close()
            connection.close()

    @staticmethod
    def _read_body(
        response: http.client.HTTPResponse,
        deadline: float,
        response_limit: int,
    ) -> bytes:
        chunks: list[bytes] = []
        observed = 0
        while True:
            remaining = _remaining(deadline)
            response_socket = _response_socket(response)
            if response_socket is not None:
                response_socket.settimeout(remaining)
            allowance = response_limit + 1 - observed
            try:
                chunk = response.read1(min(16 * 1024, allowance))
            except (socket.timeout, TimeoutError) as error:
                raise ProviderTimeoutError("Ollama provider timed out") from error
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > response_limit:
                raise ProviderOutputError("Ollama response exceeded the size limit")
        return b"".join(chunks)


@dataclass(frozen=True)
class OllamaProvider:
    endpoint: str = field(default="http://127.0.0.1:11434", repr=False)
    model: str = "qwen2.5:7b"
    timeout_seconds: float = 30
    isolation_evidence: OllamaIsolationEvidence | None = field(
        default=None,
        repr=False,
    )
    _parsed_endpoint: _Endpoint | None = field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        endpoint = self.endpoint.strip().rstrip("/")
        parsed_endpoint = self._parse_endpoint(endpoint)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.isolation_evidence is not None and not isinstance(
            self.isolation_evidence, OllamaIsolationEvidence
        ):
            raise ValueError("isolation_evidence must be a trusted attestation")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "_parsed_endpoint", parsed_endpoint)

    @staticmethod
    def _parse_endpoint(endpoint: str) -> _Endpoint | None:
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if host.casefold() == "localhost":
                local_host = True
            else:
                local_host = ipaddress.ip_address(host).is_loopback
            valid = bool(
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and 0 < port <= 65_535
                and local_host
            )
        except ValueError:
            return None
        if not valid:
            return None
        return _Endpoint(
            scheme=parsed.scheme,
            host=host,
            port=port,
            base_path=parsed.path.rstrip("/"),
        )

    def availability(self) -> ProviderAvailability:
        if self.isolation_evidence is None:
            return self._availability_status(False, "isolation_evidence_required")
        if self._parsed_endpoint is None:
            return self._availability_status(False, "invalid_configuration")
        try:
            deadline = time.monotonic() + self.timeout_seconds
            local_model = self._resolve_local_model(deadline)
        except Exception:
            return self._availability_status(False, "unavailable")
        return self._availability_status(
            True, "available", model_digest=local_model.digest
        )

    def _availability_status(
        self, available: bool, detail: str, model_digest: str = ""
    ) -> ProviderAvailability:
        isolated = self.isolation_evidence is not None
        return ProviderAvailability(
            available=available,
            provider_kind="ollama",
            model=self.model,
            privacy_mode=(
                PrivacyMode.LOCAL_PRIVATE
                if isolated
                else PrivacyMode.CLOUD_EXTERNAL
            ),
            data_leaves_machine=not isolated,
            is_ai=True,
            detail=detail,
            model_digest=model_digest,
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        failure: ProviderError | None = None
        try:
            return self._run(request)
        except ProviderError as error:
            failure = error
        raise sanitized_provider_error(failure)

    def _run(self, request: ProviderRequest) -> ProviderResult:
        if self.isolation_evidence is None:
            raise ProviderUnavailableError(
                "Ollama isolation evidence is required"
            )
        if self._parsed_endpoint is None:
            raise ProviderUnavailableError("Ollama provider configuration is invalid")
        deadline = time.monotonic() + self.timeout_seconds
        local_model = self._resolve_local_model(deadline)
        body = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "format": request.schema_object(),
                "messages": [
                    {"role": "system", "content": request.prompt_text()},
                    {"role": "user", "content": request.context_text()},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        envelope = self._request_json("POST", "/api/chat", body, deadline)
        chat_metadata = dict(envelope)
        message = envelope.get("message")
        if isinstance(message, dict):
            chat_metadata["message"] = {
                key: value for key, value in message.items() if key != "content"
            }
        if self._contains_remote_metadata(chat_metadata):
            raise ProviderOutputError("Ollama chat reported remote model metadata")
        if envelope.get("model") != self.model or envelope.get("done") is not True:
            raise ProviderOutputError("Ollama chat response was incomplete or mismatched")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ProviderOutputError("Ollama response envelope was invalid")
        payload = parse_json_object(message["content"])
        output = validate_provider_output(request, payload)
        # These checks detect honest drift. A malicious server under the same OS
        # user that swaps and perfectly restores state is outside the threat model.
        if self._resolve_local_model(deadline) != local_model:
            raise ProviderUnavailableError("Ollama model provenance changed during run")
        raw_output = message["content"]
        return build_provider_result(
            provider_kind="ollama",
            model=self.model,
            privacy_mode=PrivacyMode.LOCAL_PRIVATE,
            data_leaves_machine=False,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(
                mode="api_chat",
                endpoint=self.endpoint,
                model_digest=local_model.digest,
            ),
        )

    def _resolve_local_model(self, deadline: float) -> _LocalModel:
        payload = self._request_json("GET", "/api/tags", None, deadline)
        if self._contains_remote_metadata(payload):
            raise ProviderUnavailableError(
                "Ollama model local provenance could not be proven"
            )
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProviderUnavailableError("Ollama model metadata was invalid")
        matches = [
            item
            for item in models
            if isinstance(item, dict)
            and item.get("name") == self.model
            and item.get("model") == self.model
        ]
        if len(matches) != 1:
            raise ProviderUnavailableError("Ollama model was not uniquely installed")
        metadata = matches[0]
        digest, size = self._validate_local_metadata(metadata)
        show = self._request_json(
            "POST",
            "/api/show",
            json.dumps(
                {"model": self.model},
                separators=(",", ":"),
            ).encode("utf-8"),
            deadline,
        )
        show_digest, show_size = self._validate_local_metadata(show)
        if show_digest != digest or show_size != size:
            raise ProviderUnavailableError(
                "Ollama model metadata did not agree across local APIs"
            )
        return _LocalModel(
            name=self.model,
            digest=digest,
            size=size,
            tags_metadata_sha256=self._metadata_sha256(metadata),
            show_metadata_sha256=self._metadata_sha256(show),
        )

    def _validate_local_metadata(self, metadata: dict[str, Any]) -> tuple[str, int]:
        digest = metadata.get("digest")
        size = metadata.get("size")
        if (
            metadata.get("name") != self.model
            or metadata.get("model") != self.model
            or not isinstance(digest, str)
            or _DIGEST_PATTERN.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or self._contains_remote_metadata(metadata)
        ):
            raise ProviderUnavailableError(
                "Ollama model local provenance could not be proven"
            )
        return digest.lower(), size

    @staticmethod
    def _metadata_sha256(metadata: dict[str, Any]) -> str:
        raw = json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _contains_remote_metadata(cls, value: Any, key: str = "") -> bool:
        if isinstance(value, dict):
            for raw_key, nested in value.items():
                normalized_key = str(raw_key).casefold()
                if normalized_key in {
                    "remote_host",
                    "remote_model",
                } and cls._metadata_value_is_present(nested):
                    return True
                if "cloud" in normalized_key and cls._metadata_value_is_present(
                    nested
                ):
                    return True
                if cls._contains_remote_metadata(nested, normalized_key):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(cls._contains_remote_metadata(item, key) for item in value)
        if isinstance(value, str):
            return "cloud" in value.casefold()
        return False

    @staticmethod
    def _metadata_value_is_present(value: Any) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value != 0
        return True

    def _request_json(
        self,
        method: str,
        path: str,
        body: bytes | None,
        deadline: float,
    ) -> dict[str, Any]:
        if self._parsed_endpoint is None:
            raise ProviderUnavailableError("Ollama provider configuration is invalid")
        status, raw = _LoopbackTransport(self._parsed_endpoint).request(
            method,
            path,
            body,
            deadline,
            MAX_OLLAMA_ENVELOPE_BYTES,
        )
        if not 200 <= status < 300:
            raise ProviderInvocationError(
                f"Ollama returned HTTP status {status}"
            )
        payload = parse_json_object(
            raw,
            size_limit=MAX_OLLAMA_ENVELOPE_BYTES,
        )
        return dict(payload)
