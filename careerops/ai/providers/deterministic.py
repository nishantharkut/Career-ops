from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    ProviderRequest,
    ProviderResult,
    ProviderInvocationError,
    ProviderOutputError,
    serialize_provider_output,
    validate_provider_output,
)
from careerops.ai.providers.base import build_provider_result
from careerops.core.status import PrivacyMode


Verifier = Callable[[ProviderRequest], Mapping[str, Any]]


@dataclass(frozen=True)
class DeterministicProvider:
    verifier: Verifier
    model: str = "careerops-verifier-v1"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            available=True,
            provider_kind="deterministic_verifier",
            model=self.model,
            privacy_mode=PrivacyMode.VERIFIER_ONLY,
            data_leaves_machine=False,
            is_ai=False,
            detail="available",
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        try:
            verifier_output = self.verifier(request)
        except Exception:
            raise ProviderInvocationError("Deterministic verifier failed") from None
        try:
            payload = dict(verifier_output)
        except Exception:
            raise ProviderOutputError(
                "Deterministic verifier output was invalid"
            ) from None
        output = validate_provider_output(request, payload)
        raw_output = serialize_provider_output(output)
        return build_provider_result(
            provider_kind="deterministic_verifier",
            model=self.model,
            privacy_mode=PrivacyMode.VERIFIER_ONLY,
            data_leaves_machine=False,
            is_ai=False,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(mode="verifier_only"),
        )
