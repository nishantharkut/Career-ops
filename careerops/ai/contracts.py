from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, TypeAlias, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)


ALLOWED_TASK_TYPES = (
    "analyze_jd",
    "analyze_fit",
    "suggest_resume_edits",
    "draft_cover_letter",
    "review_claims",
)

TaskType: TypeAlias = Literal[
    "analyze_jd",
    "analyze_fit",
    "suggest_resume_edits",
    "draft_cover_letter",
    "review_claims",
]
PrivacyMode: TypeAlias = Literal["local_private", "cloud_review"]

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
ListText = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=12_000)]
ReasonText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
MetadataText = Annotated[str, StringConstraints(max_length=2_048)]
RawOutputText = Annotated[str, StringConstraints(max_length=2_000_000)]
MAX_RAW_OUTPUT_CHARACTERS = 2_000_000


class ProviderError(RuntimeError):
    """Base error whose messages are safe to persist or display."""


class ProviderUnavailableError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderInvocationError(ProviderError):
    pass


class ProviderOutputError(ProviderError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AnalyzeJdOutput(StrictModel):
    task_type: Literal["analyze_jd"]
    normalized_title: ShortText
    normalized_company: ShortText
    normalized_location: ShortText
    responsibilities: list[ListText] = Field(max_length=50)
    required_skills: list[ListText] = Field(max_length=50)
    preferred_skills: list[ListText] = Field(max_length=50)
    keywords: list[ShortText] = Field(max_length=50)
    uncertainties: list[ListText] = Field(max_length=30)


class AnalyzeFitOutput(StrictModel):
    task_type: Literal["analyze_fit"]
    score: int = Field(ge=0, le=100)
    matches: list[ListText] = Field(max_length=50)
    gaps: list[ListText] = Field(max_length=50)
    risks: list[ListText] = Field(max_length=50)
    recommendations: list[ListText] = Field(max_length=50)


class ResumeEditSuggestion(StrictModel):
    source_text: LongText
    replacement_text: LongText
    reason: ReasonText
    evidence_refs: list[Identifier] = Field(max_length=100)


class ResumeEditsOutput(StrictModel):
    task_type: Literal["suggest_resume_edits"]
    suggestions: list[ResumeEditSuggestion] = Field(max_length=50)


class CoverLetterOutput(StrictModel):
    task_type: Literal["draft_cover_letter"]
    plain_text: LongText
    evidence_refs: list[Identifier] = Field(max_length=100)


class ClaimDecision(StrictModel):
    claim_id: Identifier
    decision: Literal["supported", "unsupported", "needs_review", "private"]
    reason: ReasonText
    evidence_refs: list[Identifier] = Field(max_length=100)


class ClaimReviewOutput(StrictModel):
    task_type: Literal["review_claims"]
    decisions: list[ClaimDecision] = Field(max_length=100)


TaskOutput = Annotated[
    Union[
        AnalyzeJdOutput,
        AnalyzeFitOutput,
        ResumeEditsOutput,
        CoverLetterOutput,
        ClaimReviewOutput,
    ],
    Field(discriminator="task_type"),
]

_OUTPUT_MODELS: dict[str, type[StrictModel]] = {
    "analyze_jd": AnalyzeJdOutput,
    "analyze_fit": AnalyzeFitOutput,
    "suggest_resume_edits": ResumeEditsOutput,
    "draft_cover_letter": CoverLetterOutput,
    "review_claims": ClaimReviewOutput,
}
_OUTPUT_ADAPTER = TypeAdapter(TaskOutput)


def _output_model(task_type: str) -> type[StrictModel]:
    try:
        return _OUTPUT_MODELS[task_type]
    except KeyError as error:
        raise ProviderOutputError(f"Unsupported task type: {task_type}") from error


def validate_task_output(task_type: str, payload: Any) -> TaskOutput:
    model = _output_model(task_type)
    try:
        output = model.model_validate(payload)
        return _OUTPUT_ADAPTER.validate_python(output)
    except (TypeError, ValidationError) as error:
        raise ProviderOutputError("Provider output failed the task schema") from error


def output_schema_for_task(task_type: str) -> dict[str, Any]:
    schema = _output_model(task_type).model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
    }


class ProviderRequest(StrictModel):
    task_id: Identifier
    task_type: TaskType
    task_dir: Path
    prompt_path: Path
    context_path: Path
    schema_path: Path

    @model_validator(mode="after")
    def validate_paths(self) -> ProviderRequest:
        try:
            task_dir = self.task_dir.expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("task_dir must be an existing directory") from error
        if not task_dir.is_dir():
            raise ValueError("task_dir must be an existing directory")

        object.__setattr__(self, "task_dir", task_dir)
        for field_name in ("prompt_path", "context_path", "schema_path"):
            source = getattr(self, field_name)
            try:
                resolved = source.expanduser().resolve(strict=True)
            except OSError as error:
                raise ValueError(
                    f"{field_name} must be an existing regular file"
                ) from error
            if not resolved.is_file():
                raise ValueError(f"{field_name} must be an existing regular file")
            try:
                resolved.relative_to(task_dir)
            except ValueError as error:
                raise ValueError(f"{field_name} must stay inside task_dir") from error
            object.__setattr__(self, field_name, resolved)
        return self


class InvocationMetadata(StrictModel):
    mode: MetadataText
    endpoint: MetadataText = ""
    executable: MetadataText = ""
    executable_version: MetadataText = ""
    command: tuple[MetadataText, ...] = Field(default=(), max_length=32)


class ProviderAvailability(StrictModel):
    available: bool
    provider_kind: MetadataText
    model: MetadataText
    privacy_mode: PrivacyMode
    data_leaves_machine: bool
    is_ai: bool
    detail: MetadataText
    executable: MetadataText = ""
    executable_version: MetadataText = ""


class ProviderResult(StrictModel):
    provider_kind: MetadataText
    model: MetadataText
    privacy_mode: PrivacyMode
    data_leaves_machine: bool
    is_ai: bool
    structured_output: TaskOutput
    raw_output: RawOutputText
    invocation: InvocationMetadata


def parse_json_object(raw_output: str) -> Mapping[str, Any]:
    if len(raw_output) > MAX_RAW_OUTPUT_CHARACTERS:
        raise ProviderOutputError("Provider output exceeded the size limit")
    try:
        value = json.loads(raw_output)
    except (TypeError, ValueError) as error:
        raise ProviderOutputError("Provider output was not valid JSON") from error
    if not isinstance(value, dict):
        raise ProviderOutputError("Provider output must be a JSON object")
    return value
