from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    ProviderRequest,
    ProviderResult,
    parse_json_object,
    validate_task_output,
)


@dataclass(frozen=True)
class MockProvider:
    raw_output: Mapping[str, Any] | str
    model: str = "fixture-v1"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            available=True,
            provider_kind="mock",
            model=self.model,
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=True,
            detail="available",
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        if isinstance(self.raw_output, str):
            raw_output = self.raw_output
            payload = parse_json_object(raw_output)
        else:
            payload = dict(self.raw_output)
            raw_output = json.dumps(payload, sort_keys=True)
        output = validate_task_output(request.task_type, payload)
        return ProviderResult(
            provider_kind="mock",
            model=self.model,
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=True,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(mode="configured_fixture"),
        )
