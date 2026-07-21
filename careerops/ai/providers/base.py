from __future__ import annotations

import hashlib
from typing import Any, Protocol

from careerops.ai.contracts import (
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
)


class AiProvider(Protocol):
    def availability(self) -> ProviderAvailability: ...

    def run(self, request: ProviderRequest) -> ProviderResult: ...


def sanitized_provider_error(error: ProviderError) -> ProviderError:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error


def build_provider_result(**values: Any) -> ProviderResult:
    raw_output = values.get("raw_output")
    text, raw = bounded_utf8_text(raw_output)
    digest = hashlib.sha256(raw).hexdigest()
    invocation = values.get("invocation")
    if isinstance(invocation, InvocationMetadata):
        values["invocation"] = invocation.model_copy(
            update={"raw_output_sha256": digest}
        )
    values["raw_output"] = text
    values["raw_output_sha256"] = digest
    failed = False
    try:
        result = ProviderResult(**values)
    except (Exception,):
        failed = True
    if failed:
        raise ProviderOutputError("Provider result metadata was invalid")
    return result


__all__ = [
    "AiProvider",
    "build_provider_result",
    "sanitized_provider_error",
    "ProviderError",
    "ProviderInvocationError",
    "ProviderOutputError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
