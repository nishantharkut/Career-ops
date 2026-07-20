from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    parse_json_object,
    serialize_provider_output,
    validate_provider_output,
)
from careerops.ai.providers.base import build_provider_result
from careerops.core.status import PrivacyMode


@dataclass(frozen=True)
class MockProvider:
    raw_output: Mapping[str, Any] | str
    model: str = "fixture-v1"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            available=True,
            provider_kind="mock",
            model=self.model,
            privacy_mode=PrivacyMode.LOCAL_PRIVATE,
            data_leaves_machine=False,
            is_ai=True,
            detail="available",
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        if isinstance(self.raw_output, str):
            payload = parse_json_object(self.raw_output)
        else:
            try:
                payload = dict(self.raw_output)
            except Exception:
                raise ProviderOutputError("Mock provider output was invalid") from None
        output = validate_provider_output(request, payload)
        raw_output = serialize_provider_output(output)
        return build_provider_result(
            provider_kind="mock",
            model=self.model,
            privacy_mode=PrivacyMode.LOCAL_PRIVATE,
            data_leaves_machine=False,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(mode="configured_fixture"),
        )
