from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    ProviderRequest,
    ProviderResult,
    validate_task_output,
)


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
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=False,
            detail="available",
        )

    def run(self, request: ProviderRequest) -> ProviderResult:
        payload = dict(self.verifier(request))
        output = validate_task_output(request.task_type, payload)
        return ProviderResult(
            provider_kind="deterministic_verifier",
            model=self.model,
            privacy_mode="local_private",
            data_leaves_machine=False,
            is_ai=False,
            structured_output=output,
            raw_output=json.dumps(payload, sort_keys=True),
            invocation=InvocationMetadata(mode="verifier_only"),
        )
