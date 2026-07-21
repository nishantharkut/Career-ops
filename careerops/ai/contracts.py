from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Annotated, Any, Literal, Mapping, TypeAlias, Union, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from careerops.core.status import PrivacyMode


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

MAX_PROVIDER_PROMPT_BYTES = 64_000
MAX_PROVIDER_CONTEXT_BYTES = 120_000
MAX_PROVIDER_SCHEMA_BYTES = 256_000
MAX_PROVIDER_OUTPUT_BYTES = 512_000

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
ListText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
CoverLetterText = Annotated[str, StringConstraints(min_length=1, max_length=12_000)]
ReasonText = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
MetadataText = Annotated[str, StringConstraints(max_length=2_048)]
BoundMetadataText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2_048),
]
RuntimeHandleText = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
RawOutputText = Annotated[
    str, StringConstraints(max_length=MAX_PROVIDER_OUTPUT_BYTES)
]
Sha256Text = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


class _DuplicateJsonMember(ValueError):
    pass


def _reject_duplicate_json_members(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonMember()
        value[key] = item
    return value


def _strict_json_loads(raw: str | bytes) -> Any:
    def reject_nonstandard_constant(_value: str) -> Any:
        raise ValueError("non-standard JSON constant")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_json_members,
        parse_constant=reject_nonstandard_constant,
        parse_float=parse_finite_float,
    )
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            current.encode("utf-8")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("non-finite JSON number")
        elif isinstance(current, dict):
            for key, nested in current.items():
                key.encode("utf-8")
                stack.append(nested)
        elif isinstance(current, list):
            stack.extend(current)
        elif current is not None and not isinstance(current, (bool, int)):
            raise ValueError("unsupported JSON value")
    return value


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

    @model_validator(mode="before")
    @classmethod
    def convert_json_arrays_to_tuples(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        converted = dict(value)
        for name, field_info in cls.model_fields.items():
            if get_origin(field_info.annotation) is tuple and isinstance(
                converted.get(name), list
            ):
                converted[name] = tuple(converted[name])
        return converted


class OllamaIsolationEvidence(StrictModel):
    attestation_version: Literal["ollama_isolation_v2"]
    canonical_endpoint: BoundMetadataText
    model: RuntimeHandleText
    runtime_instance_id: Identifier
    runtime_handle: RuntimeHandleText
    egress_policy: Literal[
        "container_internal_no_egress",
        "os_firewall_block",
    ]
    cloud_disabled: Literal[True]
    egress_enforced: Literal[True]
    issuer_instance_id: Identifier
    integrity: Sha256Text

    def canonical_payload_bytes(self) -> bytes:
        payload = {
            "attestation_version": self.attestation_version,
            "canonical_endpoint": self.canonical_endpoint,
            "cloud_disabled": self.cloud_disabled,
            "egress_enforced": self.egress_enforced,
            "egress_policy": self.egress_policy,
            "issuer_instance_id": self.issuer_instance_id,
            "model": self.model,
            "runtime_handle": self.runtime_handle,
            "runtime_instance_id": self.runtime_instance_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_payload_bytes()).hexdigest()


class AnalyzeJdOutput(StrictModel):
    task_type: Literal["analyze_jd"]
    normalized_title: ShortText
    normalized_company: ShortText
    normalized_location: ShortText
    responsibilities: tuple[ListText, ...] = Field(max_length=20)
    required_skills: tuple[ListText, ...] = Field(max_length=30)
    preferred_skills: tuple[ListText, ...] = Field(max_length=30)
    keywords: tuple[ShortText, ...] = Field(max_length=50)
    uncertainties: tuple[ListText, ...] = Field(max_length=20)


class AnalyzeFitOutput(StrictModel):
    task_type: Literal["analyze_fit"]
    score: int = Field(ge=0, le=100)
    matches: tuple[ListText, ...] = Field(max_length=30)
    gaps: tuple[ListText, ...] = Field(max_length=30)
    risks: tuple[ListText, ...] = Field(max_length=30)
    recommendations: tuple[ListText, ...] = Field(max_length=30)


class ResumeEditSuggestion(StrictModel):
    source_text: LongText
    replacement_text: LongText
    reason: ReasonText
    evidence_refs: tuple[Identifier, ...] = Field(max_length=20)


class ResumeEditsOutput(StrictModel):
    task_type: Literal["suggest_resume_edits"]
    suggestions: tuple[ResumeEditSuggestion, ...] = Field(max_length=8)


class CoverLetterOutput(StrictModel):
    task_type: Literal["draft_cover_letter"]
    plain_text: CoverLetterText
    evidence_refs: tuple[Identifier, ...] = Field(max_length=40)


class ClaimDecision(StrictModel):
    claim_id: Identifier
    decision: Literal["supported", "unsupported", "needs_review", "private"]
    reason: ReasonText
    evidence_refs: tuple[Identifier, ...] = Field(max_length=20)


class ClaimReviewOutput(StrictModel):
    task_type: Literal["review_claims"]
    decisions: tuple[ClaimDecision, ...] = Field(max_length=40)


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
    model = _OUTPUT_MODELS.get(task_type)
    if model is None:
        raise ProviderOutputError(f"Unsupported task type: {task_type}")
    return model


def validate_task_output(task_type: str, payload: Any) -> TaskOutput:
    model = _output_model(task_type)
    failed = False
    try:
        output = model.model_validate(payload)
        validated = _OUTPUT_ADAPTER.validate_python(output)
    except (Exception,):
        failed = True
    if failed:
        raise ProviderOutputError("Provider output failed the task schema")
    return validated


def output_schema_for_task(task_type: str) -> dict[str, Any]:
    schema = _output_model(task_type).model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
    }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _cross_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
    )


def _seal_regular_file(path: Path, task_dir: Path, size_limit: int) -> tuple[Path, bytes]:
    candidate = path.expanduser().absolute()
    try:
        before_path = candidate.lstat()
    except OSError as error:
        raise ValueError("Provider input must be an existing regular file") from error
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError("Provider input must not be a symlink")
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError("Provider input must be a regular file")
    if before_path.st_nlink != 1:
        raise ValueError("Provider input must be a single-link file")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(task_dir)
    except (OSError, ValueError) as error:
        raise ValueError("Provider input must stay inside task_dir") from error

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(resolved, flags)
        before_fd = os.fstat(descriptor)
        if not stat.S_ISREG(before_fd.st_mode) or before_fd.st_nlink != 1:
            raise ValueError("Provider input must be a single-link regular file")
        if (before_fd.st_dev, before_fd.st_ino) != (
            before_path.st_dev,
            before_path.st_ino,
        ):
            raise ValueError("Provider input identity changed before read")
        if before_fd.st_size > size_limit:
            raise ValueError("Provider input exceeded its size limit")

        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, size_limit + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > size_limit:
                raise ValueError("Provider input exceeded its size limit")
        after_fd = os.fstat(descriptor)
    except OSError as error:
        raise ValueError("Provider input could not be sealed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        after_path = resolved.lstat()
    except OSError as error:
        raise ValueError("Provider input identity changed after read") from error
    if _stat_identity(before_fd) != _stat_identity(after_fd):
        raise ValueError("Provider input identity changed during read")
    if _stat_identity(before_path) != _stat_identity(after_path):
        raise ValueError("Provider input identity changed after read")
    if _cross_stat_identity(after_fd) != _cross_stat_identity(after_path):
        raise ValueError("Provider input identity changed after read")
    return resolved, b"".join(chunks)


class ProviderRequest(StrictModel):
    task_id: Identifier
    task_type: TaskType
    task_dir: Path
    prompt_path: Path
    context_path: Path
    schema_path: Path
    expected_claim_ids: tuple[Identifier, ...] = Field(default=(), max_length=40)

    _prompt_snapshot: bytes = PrivateAttr(default=b"")
    _context_snapshot: bytes = PrivateAttr(default=b"")
    _schema_snapshot: bytes = PrivateAttr(default=b"")

    @model_validator(mode="after")
    def validate_and_seal_inputs(self) -> ProviderRequest:
        candidate_task_dir = self.task_dir.expanduser().absolute()
        try:
            task_lstat = candidate_task_dir.lstat()
            if stat.S_ISLNK(task_lstat.st_mode):
                raise ValueError("task_dir must not be a symlink")
            task_dir = candidate_task_dir.resolve(strict=True)
        except OSError as error:
            raise ValueError("task_dir must be an existing directory") from error
        if not task_dir.is_dir():
            raise ValueError("task_dir must be an existing directory")

        if len(set(self.expected_claim_ids)) != len(self.expected_claim_ids):
            raise ValueError("expected claim IDs must be unique")
        if self.task_type != "review_claims" and self.expected_claim_ids:
            raise ValueError("expected claim IDs are only valid for review_claims")

        prompt_path, prompt = _seal_regular_file(
            self.prompt_path, task_dir, MAX_PROVIDER_PROMPT_BYTES
        )
        context_path, context = _seal_regular_file(
            self.context_path, task_dir, MAX_PROVIDER_CONTEXT_BYTES
        )
        schema_path, schema = _seal_regular_file(
            self.schema_path, task_dir, MAX_PROVIDER_SCHEMA_BYTES
        )
        try:
            prompt.decode("utf-8")
            context.decode("utf-8")
            parsed_schema = _strict_json_loads(schema.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ValueError("Provider input snapshot was not valid UTF-8 JSON/text") from error
        if parsed_schema != output_schema_for_task(self.task_type):
            raise ValueError("Provider schema snapshot did not match the task type")

        object.__setattr__(self, "task_dir", task_dir)
        object.__setattr__(self, "prompt_path", prompt_path)
        object.__setattr__(self, "context_path", context_path)
        object.__setattr__(self, "schema_path", schema_path)
        object.__setattr__(self, "_prompt_snapshot", prompt)
        object.__setattr__(self, "_context_snapshot", context)
        object.__setattr__(self, "_schema_snapshot", schema)
        return self

    @property
    def prompt_snapshot(self) -> bytes:
        return self._prompt_snapshot

    @property
    def context_snapshot(self) -> bytes:
        return self._context_snapshot

    @property
    def schema_snapshot(self) -> bytes:
        return self._schema_snapshot

    def prompt_text(self) -> str:
        return self._prompt_snapshot.decode("utf-8")

    def context_text(self) -> str:
        return self._context_snapshot.decode("utf-8")

    def schema_object(self) -> dict[str, Any]:
        value = _strict_json_loads(self._schema_snapshot)
        if not isinstance(value, dict):
            raise ProviderOutputError("Provider schema snapshot was invalid")
        return value


def validate_provider_output(request: ProviderRequest, payload: Any) -> TaskOutput:
    output = validate_task_output(request.task_type, payload)
    if isinstance(output, ClaimReviewOutput):
        decision_ids = tuple(item.claim_id for item in output.decisions)
        if (
            len(decision_ids) != len(set(decision_ids))
            or set(decision_ids) != set(request.expected_claim_ids)
        ):
            raise ProviderOutputError("Provider claim coverage was not exact")
    return output


def serialize_provider_output_bytes(output: TaskOutput) -> bytes:
    failed = False
    try:
        raw = json.dumps(
            output.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ProviderOutputError("Provider output could not be serialized")
    if len(raw) > MAX_PROVIDER_OUTPUT_BYTES:
        raise ProviderOutputError("Provider output exceeded the size limit")
    return raw


def serialize_provider_output(output: TaskOutput) -> str:
    return serialize_provider_output_bytes(output).decode("utf-8")


class InvocationMetadata(StrictModel):
    mode: MetadataText
    endpoint: MetadataText = ""
    identity_name: MetadataText = ""
    executable: MetadataText = ""
    executable_sha256: MetadataText = ""
    executable_version: MetadataText = ""
    codex_home: MetadataText = ""
    model_digest: MetadataText = ""
    model_metadata_sha256: Sha256Text | Literal[""] = ""
    show_metadata_sha256: Sha256Text | Literal[""] = ""
    attestation_version: MetadataText = ""
    runtime_instance_id: MetadataText = ""
    runtime_handle: MetadataText = ""
    issuer_instance_id: MetadataText = ""
    egress_policy: MetadataText = ""
    cloud_disabled: bool = False
    egress_enforced: bool = False
    isolation_evidence_sha256: Sha256Text | Literal[""] = ""
    isolation_evidence_signature: Sha256Text | Literal[""] = ""
    reasoning_effort: MetadataText = ""
    raw_output_sha256: Sha256Text | Literal[""] = ""
    command: tuple[MetadataText, ...] = Field(default=(), max_length=40)


class ProviderAvailability(StrictModel):
    available: bool
    provider_kind: MetadataText
    model: MetadataText
    privacy_mode: PrivacyMode
    data_leaves_machine: bool
    is_ai: bool
    detail: MetadataText
    endpoint: MetadataText = ""
    identity_name: MetadataText = ""
    executable: MetadataText = ""
    executable_sha256: MetadataText = ""
    executable_version: MetadataText = ""
    codex_home: MetadataText = ""
    model_digest: MetadataText = ""
    model_metadata_sha256: Sha256Text | Literal[""] = ""
    show_metadata_sha256: Sha256Text | Literal[""] = ""
    attestation_version: MetadataText = ""
    runtime_instance_id: MetadataText = ""
    runtime_handle: MetadataText = ""
    issuer_instance_id: MetadataText = ""
    egress_policy: MetadataText = ""
    cloud_disabled: bool = False
    egress_enforced: bool = False
    isolation_evidence_sha256: Sha256Text | Literal[""] = ""
    isolation_evidence_signature: Sha256Text | Literal[""] = ""
    reasoning_effort: MetadataText = ""


class ProviderResult(StrictModel):
    provider_kind: MetadataText
    model: MetadataText
    privacy_mode: PrivacyMode
    data_leaves_machine: bool
    is_ai: bool
    structured_output: TaskOutput
    raw_output: RawOutputText
    raw_output_sha256: Sha256Text
    invocation: InvocationMetadata

    @model_validator(mode="after")
    def validate_raw_output_provenance(self) -> ProviderResult:
        try:
            raw = self.raw_output.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("raw_output must be valid UTF-8") from error
        digest = hashlib.sha256(raw).hexdigest()
        if (
            len(raw) > MAX_PROVIDER_OUTPUT_BYTES
            or self.raw_output_sha256 != digest
            or self.invocation.raw_output_sha256 != digest
        ):
            raise ValueError("raw_output provenance did not match")
        return self


def bounded_utf8_text(
    raw_output: str | bytes,
    *,
    size_limit: int = MAX_PROVIDER_OUTPUT_BYTES,
) -> tuple[str, bytes]:
    failed = False
    try:
        if isinstance(raw_output, bytes):
            raw = raw_output
            text = raw.decode("utf-8")
        else:
            text = raw_output
            raw = text.encode("utf-8")
    except (TypeError, UnicodeError):
        failed = True
    if failed:
        raise ProviderOutputError("Provider output was not valid UTF-8")
    if len(raw) > size_limit:
        raise ProviderOutputError("Provider output exceeded the size limit")
    return text, raw


def parse_json_object(
    raw_output: str | bytes,
    *,
    size_limit: int = MAX_PROVIDER_OUTPUT_BYTES,
) -> Mapping[str, Any]:
    text, _raw = bounded_utf8_text(raw_output, size_limit=size_limit)
    failed = False
    try:
        value = _strict_json_loads(text)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ProviderOutputError("Provider output was not valid JSON")
    if not isinstance(value, dict):
        raise ProviderOutputError("Provider output must be a JSON object")
    return value
