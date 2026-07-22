from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import re
import secrets
import socket
import time
from typing import Any, Literal
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
MAX_OLLAMA_METADATA_DEPTH = 32
MAX_OLLAMA_METADATA_NODES = 4_096


@dataclass(frozen=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    base_path: str
    scope: Literal["loopback", "managed_container"]

    @property
    def canonical(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.base_path}"


def _parse_endpoint(
    endpoint: str,
    *,
    allow_managed_container: bool = False,
) -> _Endpoint | None:
    try:
        parsed = urlsplit(endpoint.strip().rstrip("/"))
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        port = parsed.port or (443 if scheme == "https" else 80)
        managed_container = bool(
            allow_managed_container
            and scheme == "http"
            and host == "ollama"
            and port == 11434
            and parsed.path in {"", "/"}
        )
        if host == "localhost":
            local_host = True
        elif managed_container:
            local_host = False
        else:
            address = ipaddress.ip_address(host)
            local_host = address.is_loopback
            host = address.compressed
        valid = bool(
            scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and 0 < port <= 65_535
            and (local_host or managed_container)
        )
    except ValueError:
        return None
    if not valid:
        return None
    return _Endpoint(
        scheme=scheme,
        host=host,
        port=port,
        base_path=parsed.path.rstrip("/"),
        scope="managed_container" if managed_container else "loopback",
    )


def canonicalize_ollama_endpoint(
    endpoint: str,
    *,
    allow_managed_container: bool = False,
) -> str:
    parsed = _parse_endpoint(
        endpoint,
        allow_managed_container=allow_managed_container,
    )
    if parsed is None:
        raise ValueError("Ollama endpoint was not canonicalizable")
    return parsed.canonical


@dataclass(frozen=True, init=False)
class OllamaRuntimeAuthority:
    issuer_instance_id: str
    _key: bytes = field(repr=False, compare=False)

    def __init__(self, issuer_instance_id: str, key: bytes):
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", issuer_instance_id)
            is None
        ):
            raise ValueError("Ollama authority issuer instance ID was invalid")
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Ollama authority key must contain at least 32 bytes")
        object.__setattr__(self, "issuer_instance_id", issuer_instance_id)
        object.__setattr__(self, "_key", bytes(key))

    @classmethod
    def generate(cls, issuer_instance_id: str) -> OllamaRuntimeAuthority:
        return cls(issuer_instance_id, secrets.token_bytes(32))

    def issue(
        self,
        *,
        endpoint: str,
        model: str,
        runtime_instance_id: str,
        runtime_handle: str,
        egress_policy: str,
    ) -> OllamaIsolationEvidence:
        unsigned = OllamaIsolationEvidence(
            attestation_version="ollama_isolation_v2",
            canonical_endpoint=canonicalize_ollama_endpoint(
                endpoint,
                allow_managed_container=(
                    egress_policy == "container_internal_no_egress"
                ),
            ),
            model=model,
            runtime_instance_id=runtime_instance_id,
            runtime_handle=runtime_handle,
            egress_policy=egress_policy,
            cloud_disabled=True,
            egress_enforced=True,
            issuer_instance_id=self.issuer_instance_id,
            integrity="0" * 64,
        )
        signature = hmac.new(
            self._key,
            unsigned.canonical_payload_bytes(),
            hashlib.sha256,
        ).hexdigest()
        return unsigned.model_copy(update={"integrity": signature})

    def verifies(
        self,
        evidence: OllamaIsolationEvidence,
        *,
        endpoint: str,
        model: str,
        runtime_instance_id: str,
        runtime_handle: str,
    ) -> bool:
        try:
            exact_fields = set(type(evidence).model_fields)
            if (
                type(evidence) is not OllamaIsolationEvidence
                or set(evidence.__dict__) != exact_fields
                or evidence.canonical_endpoint
                != canonicalize_ollama_endpoint(
                    endpoint,
                    allow_managed_container=(
                        evidence.egress_policy == "container_internal_no_egress"
                    ),
                )
                or evidence.model != model
                or evidence.runtime_instance_id != runtime_instance_id
                or evidence.runtime_handle != runtime_handle
                or evidence.issuer_instance_id != self.issuer_instance_id
                or evidence.cloud_disabled is not True
                or evidence.egress_enforced is not True
            ):
                return False
            expected = hmac.new(
                self._key,
                evidence.canonical_payload_bytes(),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, evidence.integrity)
        except Exception:
            return False


@dataclass(frozen=True)
class OllamaModelIdentity:
    model: str
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


class _PrivateTransport:
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
                peer_address = ipaddress.ip_address(peer)
                if self.endpoint.scope == "loopback":
                    peer_allowed = peer_address.is_loopback
                else:
                    peer_allowed = bool(
                        peer_address.is_private
                        and not peer_address.is_loopback
                        and not peer_address.is_link_local
                        and not peer_address.is_multicast
                        and not peer_address.is_unspecified
                    )
                if not peer_allowed:
                    raise ProviderUnavailableError(
                        "Ollama endpoint resolved outside its verified private boundary"
                    )
            except ValueError as error:
                raise ProviderUnavailableError(
                    "Ollama endpoint resolved outside its verified private boundary"
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
    runtime_instance_id: str = ""
    runtime_handle: str = ""
    isolation_authority: OllamaRuntimeAuthority | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    isolation_evidence: OllamaIsolationEvidence | None = field(
        default=None,
        repr=False,
    )
    _parsed_endpoint: _Endpoint | None = field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        endpoint = self.endpoint.strip().rstrip("/")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.isolation_evidence is not None and not isinstance(
            self.isolation_evidence, OllamaIsolationEvidence
        ):
            raise ValueError("isolation_evidence must be a trusted attestation")
        if self.isolation_authority is not None and not isinstance(
            self.isolation_authority, OllamaRuntimeAuthority
        ):
            raise ValueError("isolation_authority must be a trusted authority")
        parsed_endpoint = _parse_endpoint(
            endpoint,
            allow_managed_container=True,
        )
        object.__setattr__(
            self,
            "endpoint",
            parsed_endpoint.canonical if parsed_endpoint is not None else endpoint,
        )
        object.__setattr__(self, "_parsed_endpoint", parsed_endpoint)

    @staticmethod
    def _parse_endpoint(endpoint: str) -> _Endpoint | None:
        return _parse_endpoint(endpoint, allow_managed_container=True)

    def _verified_isolation_evidence(self) -> OllamaIsolationEvidence | None:
        if (
            self._parsed_endpoint is None
            or self.isolation_authority is None
            or self.isolation_evidence is None
            or not self.runtime_instance_id
            or not self.runtime_handle
            or not self.isolation_authority.verifies(
                self.isolation_evidence,
                endpoint=self._parsed_endpoint.canonical,
                model=self.model,
                runtime_instance_id=self.runtime_instance_id,
                runtime_handle=self.runtime_handle,
            )
        ):
            return None
        return self.isolation_evidence

    def availability(self) -> ProviderAvailability:
        if self._parsed_endpoint is None:
            return self._availability_status(False, "invalid_configuration")
        evidence = self._verified_isolation_evidence()
        if evidence is None:
            configured = any(
                (
                    self.isolation_authority is not None,
                    self.isolation_evidence is not None,
                    bool(self.runtime_instance_id),
                    bool(self.runtime_handle),
                )
            )
            return self._availability_status(
                False,
                "isolation_evidence_invalid"
                if configured
                else "isolation_evidence_required",
            )
        try:
            deadline = time.monotonic() + self.timeout_seconds
            local_model = self._resolve_local_model(deadline)
        except Exception:
            return self._availability_status(
                False,
                "unavailable",
                evidence=evidence,
            )
        return self._availability_status(
            True,
            "available",
            model_digest=local_model.digest,
            evidence=evidence,
            local_model=local_model,
        )

    def _availability_status(
        self,
        available: bool,
        detail: str,
        model_digest: str = "",
        evidence: OllamaIsolationEvidence | None = None,
        local_model: OllamaModelIdentity | None = None,
    ) -> ProviderAvailability:
        isolated = evidence is not None
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
            endpoint=(
                self._parsed_endpoint.canonical
                if self._parsed_endpoint is not None
                else ""
            ),
            model_digest=model_digest,
            model_metadata_sha256=(
                local_model.tags_metadata_sha256 if local_model else ""
            ),
            show_metadata_sha256=(
                local_model.show_metadata_sha256 if local_model else ""
            ),
            **self._isolation_provenance(evidence),
        )

    @staticmethod
    def _isolation_provenance(
        evidence: OllamaIsolationEvidence | None,
    ) -> dict[str, Any]:
        if evidence is None:
            return {}
        return {
            "attestation_version": evidence.attestation_version,
            "runtime_instance_id": evidence.runtime_instance_id,
            "runtime_handle": evidence.runtime_handle,
            "issuer_instance_id": evidence.issuer_instance_id,
            "egress_policy": evidence.egress_policy,
            "cloud_disabled": evidence.cloud_disabled,
            "egress_enforced": evidence.egress_enforced,
            "isolation_evidence_sha256": evidence.canonical_sha256,
            "isolation_evidence_signature": evidence.integrity,
        }

    def run(self, request: ProviderRequest) -> ProviderResult:
        failure: ProviderError | None = None
        try:
            return self._run(request)
        except ProviderError as error:
            failure = error
        raise sanitized_provider_error(failure)

    def compatibility_probe(self) -> OllamaModelIdentity:
        failure: ProviderError | None = None
        try:
            if self._parsed_endpoint is None:
                raise ProviderUnavailableError(
                    "Ollama provider configuration is invalid"
                )
            if (
                self._parsed_endpoint.scope == "managed_container"
                and self._verified_isolation_evidence() is None
            ):
                raise ProviderUnavailableError(
                    "Ollama isolation evidence is invalid or unavailable"
                )
            return self._resolve_local_model(
                time.monotonic() + self.timeout_seconds
            )
        except ProviderError as error:
            failure = error
        raise sanitized_provider_error(failure)

    def _run(self, request: ProviderRequest) -> ProviderResult:
        if self._parsed_endpoint is None:
            raise ProviderUnavailableError("Ollama provider configuration is invalid")
        evidence = self._verified_isolation_evidence()
        if evidence is None:
            raise ProviderUnavailableError(
                "Ollama isolation evidence is invalid or unavailable"
            )
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
                model_metadata_sha256=local_model.tags_metadata_sha256,
                show_metadata_sha256=local_model.show_metadata_sha256,
                **self._isolation_provenance(evidence),
            ),
        )

    def _resolve_local_model(self, deadline: float) -> OllamaModelIdentity:
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
        show_metadata_sha256 = self._validate_show_metadata(show)
        return OllamaModelIdentity(
            model=self.model,
            digest=digest,
            size=size,
            tags_metadata_sha256=self._metadata_sha256(payload),
            show_metadata_sha256=show_metadata_sha256,
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

    def _validate_show_metadata(self, metadata: dict[str, Any]) -> str:
        optional_string_fields = {
            "license",
            "modelfile",
            "modified_at",
            "parameters",
            "system",
            "template",
        }
        optional_object_fields = {"details", "model_info"}
        optional_list_fields = {"messages", "tensors"}
        recognized_fields = (
            optional_string_fields
            | optional_object_fields
            | optional_list_fields
            | {"capabilities"}
        )
        invalid = not metadata or not recognized_fields.intersection(metadata)
        invalid = invalid or any(
            field in metadata and not isinstance(metadata[field], str)
            for field in optional_string_fields
        )
        invalid = invalid or any(
            field in metadata and not isinstance(metadata[field], dict)
            for field in optional_object_fields
        )
        invalid = invalid or any(
            field in metadata and not isinstance(metadata[field], list)
            for field in optional_list_fields
        )
        capabilities = metadata.get("capabilities")
        invalid = invalid or (
            capabilities is not None
            and (
                not isinstance(capabilities, list)
                or not all(isinstance(item, str) for item in capabilities)
            )
        )
        if invalid or self._contains_remote_metadata(metadata):
            raise ProviderUnavailableError(
                "Ollama show metadata was invalid or remote"
            )
        return self._metadata_sha256(metadata)

    @staticmethod
    def _metadata_sha256(metadata: dict[str, Any]) -> str:
        try:
            raw = json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderOutputError(
                "Ollama metadata was not canonical JSON"
            ) from None
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _contains_remote_metadata(cls, value: Any) -> bool:
        stack: list[tuple[Any, int]] = [(value, 0)]
        observed = 0
        while stack:
            current, depth = stack.pop()
            observed += 1
            if (
                depth > MAX_OLLAMA_METADATA_DEPTH
                or observed > MAX_OLLAMA_METADATA_NODES
            ):
                raise ProviderOutputError(
                    "Ollama metadata exceeded the complexity limit"
                )
            if isinstance(current, dict):
                for raw_key, nested in current.items():
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
                    cls._push_metadata_node(stack, observed, nested, depth + 1)
            elif isinstance(current, (list, tuple)):
                for item in current:
                    cls._push_metadata_node(stack, observed, item, depth + 1)
        return False

    @staticmethod
    def _push_metadata_node(
        stack: list[tuple[Any, int]],
        observed: int,
        value: Any,
        depth: int,
    ) -> None:
        if (
            depth > MAX_OLLAMA_METADATA_DEPTH
            or observed + len(stack) >= MAX_OLLAMA_METADATA_NODES
        ):
            raise ProviderOutputError(
                "Ollama metadata exceeded the complexity limit"
            )
        stack.append((value, depth))

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
        if (
            self._parsed_endpoint.scope == "managed_container"
            and self._verified_isolation_evidence() is None
        ):
            raise ProviderUnavailableError(
                "Ollama isolation evidence is invalid or unavailable"
            )
        status, raw = _PrivateTransport(self._parsed_endpoint).request(
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
