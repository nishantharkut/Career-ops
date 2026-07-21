from __future__ import annotations

import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    StrictBool,
    field_validator,
    model_validator,
)

from careerops.core.repository import MAX_PERSISTED_JSON_BYTES
from careerops.core.status import AiTaskType, PrivacyLevel, VerificationState


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
OptionalText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2_000)
]
LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120_000)
]
UrlText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_048)]
Sha256Text = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-f]{64}$")
]


class ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateApplicationRequest(ApiRequest):
    company: ShortText
    role_title: ShortText
    location: ShortText = "Unknown"
    source_url: UrlText = ""
    jd_text: LongText
    deadline: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None
    notes: OptionalText = ""


class AnalyzeClaim(ApiRequest):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
    ]
    verification_state: VerificationState
    public_safe: StrictBool


class AnalyzeRequest(ApiRequest):
    claims: list[AnalyzeClaim] = Field(default_factory=list, max_length=100)


class ApproveRequest(ApiRequest):
    report_id: Identifier
    note: OptionalText = ""


class MarkSubmittedRequest(ApiRequest):
    note: OptionalText = ""


class CreateAiTaskRequest(ApiRequest):
    task_type: AiTaskType
    provider_config_id: Identifier
    input_artifact_ids: list[Identifier] = Field(default_factory=list, max_length=64)

    @field_validator("input_artifact_ids")
    @classmethod
    def require_unique_artifact_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("input artifact IDs must be unique")
        return value


class CreateResumeDraftRequest(ApiRequest):
    task_id: Identifier
    finding_ids: list[Identifier] = Field(min_length=1, max_length=100)
    name: ShortText

    @field_validator("finding_ids")
    @classmethod
    def require_unique_finding_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("finding IDs must be unique")
        return value


class FindingDecisionRequest(ApiRequest):
    decision: Literal["accepted", "rejected"]


class RequirementsUpdateRequest(ApiRequest):
    user_corrected: dict[str, Any]

    @field_validator("user_corrected", mode="before")
    @classmethod
    def normalize_user_corrected(cls, value: Any) -> dict[str, Any]:
        normalized = _normalize_json(value, depth=0, budget=[0])
        if not isinstance(normalized, dict):
            raise ValueError("user_corrected must be an object")
        persisted_shape = {
            "raw_ai": {},
            "deterministic": {},
            "user_corrected": normalized,
            "uncertainty": {},
        }
        serialized = json.dumps(
            persisted_shape,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(serialized.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES:
            raise ValueError("requirements exceed the persisted JSON size limit")
        return normalized


class ProfileFactRequest(ApiRequest):
    fact_key: ShortText
    fact_value: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
    ]
    verification_state: VerificationState
    privacy_level: PrivacyLevel
    notes: OptionalText = ""


class EvidenceRequest(ApiRequest):
    evidence_id: Identifier | None = None
    claim_text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
    ] | None = None
    evidence_type: ShortText | None = None
    source_url: UrlText | None = None
    source_sha256: Sha256Text | None = None
    verification_state: VerificationState | None = None
    privacy_level: PrivacyLevel | None = None
    notes: OptionalText | None = None

    @model_validator(mode="after")
    def require_link_or_creation_fields(self) -> "EvidenceRequest":
        creation_values = (
            self.claim_text,
            self.evidence_type,
            self.source_url,
            self.source_sha256,
            self.verification_state,
            self.privacy_level,
            self.notes,
        )
        if self.evidence_id is not None:
            if any(value is not None for value in creation_values):
                raise ValueError(
                    "evidence_id cannot be combined with evidence creation fields"
                )
            return self
        if (
            self.claim_text is None
            or self.evidence_type is None
            or self.verification_state is None
            or self.privacy_level is None
        ):
            raise ValueError("evidence creation fields are incomplete")
        return self


_KEY_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


def _normalize_json(value: Any, *, depth: int, budget: list[int]) -> Any:
    budget[0] += 1
    if budget[0] > 1_000 or depth > 8:
        raise ValueError("user_corrected exceeds the complexity limit")
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("user_corrected has too many fields")
        result: dict[str, Any] = {}
        for raw_key, inner in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("user_corrected keys must be strings")
            key = raw_key.strip()
            if not key or _KEY_PATTERN.fullmatch(key) is None or key in result:
                raise ValueError("user_corrected contains an invalid key")
            result[key] = _normalize_json(inner, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("user_corrected has too many list items")
        return [
            _normalize_json(inner, depth=depth + 1, budget=budget)
            for inner in value
        ]
    if isinstance(value, str):
        normalized = value.strip()
        if len(normalized) > 4_000:
            raise ValueError("user_corrected text is too long")
        return normalized
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("user_corrected numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("user_corrected contains an unsupported value")
