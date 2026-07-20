from __future__ import annotations

from typing import Protocol

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


__all__ = [
    "AiProvider",
    "ProviderError",
    "ProviderInvocationError",
    "ProviderOutputError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
