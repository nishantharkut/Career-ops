from __future__ import annotations

from typing import Any, Protocol

from pydantic import ValidationError

from careerops.ai.contracts import (
    ProviderAvailability,
    ProviderError,
    ProviderInvocationError,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class AiProvider(Protocol):
    def availability(self) -> ProviderAvailability: ...

    def run(self, request: ProviderRequest) -> ProviderResult: ...


def build_provider_result(**values: Any) -> ProviderResult:
    try:
        return ProviderResult(**values)
    except ValidationError as error:
        raise ProviderOutputError("Provider result metadata was invalid") from error


__all__ = [
    "AiProvider",
    "build_provider_result",
    "ProviderError",
    "ProviderInvocationError",
    "ProviderOutputError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
