from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import ipaddress
import json
import math
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen as default_urlopen

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


MAX_HTTP_RESPONSE_BYTES = 2_000_000
UrlOpen = Callable[..., Any]


@dataclass(frozen=True)
class OllamaProvider:
    endpoint: str = field(default="http://127.0.0.1:11434", repr=False)
    model: str = "qwen2.5:7b"
    timeout_seconds: float = 30
    urlopen: UrlOpen = field(default=default_urlopen, repr=False, compare=False)
    _configuration_valid: bool = field(
        init=False, default=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        endpoint = self.endpoint.strip().rstrip("/")
        try:
            parsed = urlsplit(endpoint)
            hostname = parsed.hostname or ""
            port = parsed.port
            try:
                is_loopback = (
                    hostname.casefold() == "localhost"
                    or ipaddress.ip_address(hostname).is_loopback
                )
            except ValueError:
                is_loopback = False
            configuration_valid = bool(
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and is_loopback
                and (port is None or port > 0)
            )
        except ValueError:
            configuration_valid = False
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "_configuration_valid", configuration_valid)

    def availability(self) -> ProviderAvailability:
        if not self._configuration_valid:
            return self._availability_status(False, "invalid_configuration")
        available = False
        detail = "unavailable"
        try:
            request = Request(f"{self.endpoint}/api/tags", method="GET")
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                if not 200 <= getattr(response, "status", 200) < 300:
                    raise ValueError("status")
                payload = self._read_json(response)
            models = payload.get("models")
            if not isinstance(models, list):
                raise ValueError("models")
            names = {
                value
                for item in models
                if isinstance(item, dict)
                for value in (item.get("name"), item.get("model"))
                if isinstance(value, str)
            }
            available = self.model in names
            detail = "available" if available else "model_unavailable"
        except Exception:
            available = False
            detail = "unavailable"
        return self._availability_status(available, detail)

    def _availability_status(
        self, available: bool, detail: str
    ) -> ProviderAvailability:
        return ProviderAvailability(
            available=available,
            provider_kind="ollama",
            model=self.model,
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=True,
            detail=detail,
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        if not self._configuration_valid:
            raise ProviderUnavailableError("Ollama provider configuration is invalid")
        schema = self._load_matching_schema(request)
        try:
            prompt = request.prompt_path.read_text(encoding="utf-8")
            context = request.context_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProviderInvocationError("Provider input could not be read") from error
        body = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "format": schema,
                "messages": [
                    {
                        "role": "system",
                        "content": prompt,
                    },
                    {
                        "role": "user",
                        "content": context,
                    },
                ],
            }
        ).encode("utf-8")
        http_request = Request(
            f"{self.endpoint}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.urlopen(http_request, timeout=self.timeout_seconds) as response:
                if not 200 <= getattr(response, "status", 200) < 300:
                    raise ProviderInvocationError("Ollama returned an HTTP error")
                envelope = self._read_json(response)
        except (socket.timeout, TimeoutError) as error:
            raise ProviderTimeoutError("Ollama provider timed out") from error
        except HTTPError as error:
            raise ProviderInvocationError("Ollama returned an HTTP error") from error
        except URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise ProviderTimeoutError("Ollama provider timed out") from error
            raise ProviderUnavailableError("Ollama provider is unavailable") from error
        except OSError as error:
            raise ProviderUnavailableError("Ollama provider is unavailable") from error

        message = envelope.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ProviderOutputError("Ollama response envelope was invalid")
        raw_output = message["content"]
        payload = parse_json_object(raw_output)
        output = validate_task_output(request.task_type, payload)
        return ProviderResult(
            provider_kind="ollama",
            model=self.model,
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(mode="api_chat", endpoint=self.endpoint),
        )

    def _read_json(self, response: Any) -> dict[str, Any]:
        raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(raw) > MAX_HTTP_RESPONSE_BYTES:
            raise ProviderOutputError("Ollama response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ProviderOutputError("Ollama response was not valid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderOutputError("Ollama response must be a JSON object")
        return payload

    @staticmethod
    def _load_matching_schema(request: ProviderRequest) -> dict[str, Any]:
        try:
            schema = json.loads(request.schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ProviderOutputError("Task schema input was invalid") from error
        if schema != output_schema_for_task(request.task_type):
            raise ProviderOutputError("Task schema did not match the task type")
        return schema
