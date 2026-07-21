from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any
from uuid import uuid4

from careerops.ai.context import (
    ContextBuildError,
    ContextPacket,
    build_context_packet,
)
from careerops.ai.verifier import verify_resume_edits
from careerops.core.models import AiFinding, ResumeVersion
from careerops.core.repository import (
    CareerOpsRepository,
    ResumeDraftAuthority,
    new_id,
)
from careerops.core.status import (
    FindingDecision,
    PrivacyLevel,
    VerificationState,
    VerifierState,
)


MAX_DRAFT_NAME_CHARACTERS = 300
MAX_EDIT_TEXT_CHARACTERS = 4_000
MAX_EDIT_REASON_CHARACTERS = 1_000
MAX_EVIDENCE_REFS = 20
MAX_FINDINGS = 100
MAX_RESUME_SOURCE_BYTES = 16_000_000
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
RESUME_ID_PATTERN = re.compile(r"res_[0-9a-f]{32}\Z")
EXPECTED_FINDING_PAYLOAD_FIELDS = frozenset(
    {"evidence_refs", "item_index", "replacement_text", "source_text"}
)


def create_verified_resume_draft(
    repository: CareerOpsRepository,
    *,
    application_id: str,
    task_id: str,
    finding_ids: list[str],
    name: str,
) -> ResumeVersion:
    safe_name = _validate_request(application_id, task_id, finding_ids, name)
    authority = repository.get_resume_draft_authority(
        application_id=application_id,
        task_id=task_id,
        finding_ids=finding_ids,
    )
    context = _validated_context(authority)
    edits, evidence_ids = _validated_edits(authority, context)
    final_bytes = _apply_edits(authority.parent_source, edits)
    if len(final_bytes) > MAX_RESUME_SOURCE_BYTES:
        raise ValueError("Resume draft source exceeded its size limit")

    resume_id = new_id("res")
    draft_dir, source_path = _publish_source(
        repository, resume_id, final_bytes
    )
    committed = False
    try:
        resume = repository.create_and_select_resume_draft(
            application_id=application_id,
            resume_id=resume_id,
            name=safe_name,
            tex_path=source_path,
            tex_sha256=hashlib.sha256(final_bytes).hexdigest(),
            authority=authority,
            referenced_evidence_ids=sorted(evidence_ids),
        )
        committed = True
        return resume
    finally:
        if not committed:
            _cleanup_new_directory(repository, draft_dir, resume_id)


def _validate_request(
    application_id: str,
    task_id: str,
    finding_ids: list[str],
    name: str,
) -> str:
    if (
        type(application_id) is not str
        or type(task_id) is not str
        or not IDENTIFIER_PATTERN.fullmatch(application_id)
        or not IDENTIFIER_PATTERN.fullmatch(task_id)
    ):
        raise ValueError("Resume draft authority IDs are invalid")
    if (
        type(finding_ids) is not list
        or not 1 <= len(finding_ids) <= MAX_FINDINGS
        or any(
            type(value) is not str or not IDENTIFIER_PATTERN.fullmatch(value)
            for value in finding_ids
        )
        or len(set(finding_ids)) != len(finding_ids)
    ):
        raise ValueError("Resume draft finding IDs are invalid")
    if type(name) is not str:
        raise ValueError("Resume draft name is invalid")
    safe_name = name.strip()
    if not safe_name or len(safe_name) > MAX_DRAFT_NAME_CHARACTERS:
        raise ValueError("Resume draft name is invalid")
    return safe_name


def _validated_context(authority: ResumeDraftAuthority) -> ContextPacket:
    if len(authority.context_bytes) > 120_000:
        raise ValueError("AI task context exceeded its size limit")
    try:
        context_text = authority.context_bytes.decode("utf-8")
        context = json.loads(context_text)
        canonical = json.dumps(
            context,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("AI task context is invalid") from exc
    if canonical != authority.context_bytes or not isinstance(context, dict):
        raise ValueError("AI task context is not canonical")
    if context.get("task_type") != "suggest_resume_edits":
        raise ValueError("AI task context type is invalid")
    selected_resume = context.get("selected_resume")
    if (
        not isinstance(selected_resume, dict)
        or set(selected_resume) != {"source_kind", "source_text", "version_id"}
        or selected_resume.get("version_id") != authority.parent_resume.id
        or selected_resume.get("source_kind") != "latex"
        or type(selected_resume.get("source_text")) is not str
    ):
        raise ValueError("AI task parent resume authority is invalid")
    try:
        context_source = selected_resume["source_text"].encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("AI task parent resume source is invalid") from exc
    if (
        context_source != authority.parent_source
        or hashlib.sha256(context_source).hexdigest()
        != authority.parent_resume.tex_sha256
    ):
        raise ValueError("Selected resume changed since the AI task")

    linked_evidence = context.get("linked_evidence")
    if not isinstance(linked_evidence, list):
        raise ValueError("AI task evidence context is invalid")
    context_evidence_ids: list[str] = []
    for item in linked_evidence:
        if (
            not isinstance(item, dict)
            or type(item.get("id")) is not str
            or not IDENTIFIER_PATTERN.fullmatch(item["id"])
        ):
            raise ValueError("AI task evidence context is invalid")
        context_evidence_ids.append(item["id"])
    if len(set(context_evidence_ids)) != len(context_evidence_ids):
        raise ValueError("AI task evidence context is ambiguous")
    try:
        rebuilt = build_context_packet(
            "suggest_resume_edits",
            job_description=context.get("job_description"),
            resume=selected_resume,
            confirmed_facts=[
                {
                    "id": fact.id,
                    "key": fact.fact_key,
                    "value": fact.fact_value,
                    "verification_state": fact.verification_state.value,
                    "privacy_level": fact.privacy_level.value,
                }
                for fact in authority.profile_facts
            ],
            evidence=[
                {
                    "id": item.id,
                    "summary": item.claim_text,
                    "verification_state": item.verification_state.value,
                    "privacy_level": item.privacy_level.value,
                }
                for item in authority.evidence
            ],
            rejected_claims=[
                {
                    "id": fact.id,
                    "text": fact.fact_value,
                    "reason": fact.notes or "Rejected profile fact",
                }
                for fact in authority.profile_facts
                if fact.verification_state == VerificationState.REJECTED
            ],
            writing_preferences=context.get("writing_preferences"),
        )
    except ContextBuildError as exc:
        raise ValueError("AI task context cannot be revalidated") from exc
    if rebuilt.canonical_bytes != authority.context_bytes:
        raise ValueError("AI task context no longer matches repository authority")
    return rebuilt


def _validated_edits(
    authority: ResumeDraftAuthority,
    context: ContextPacket,
) -> tuple[list[tuple[bytes, bytes]], set[str]]:
    evidence_by_id = {item.id: item for item in authority.evidence}
    context_evidence_ids = {
        item["id"] for item in context.to_dict()["linked_evidence"]
    }
    suggestions: list[dict[str, Any]] = []
    edits: list[tuple[bytes, bytes]] = []
    referenced_evidence_ids: set[str] = set()
    item_indexes: set[int] = set()
    for finding in authority.findings:
        suggestion, source_bytes, replacement_bytes = _validated_finding(finding)
        item_index = suggestion["item_index"]
        if item_index in item_indexes:
            raise ValueError("Resume edit item indexes must be unique")
        item_indexes.add(item_index)
        for evidence_id in suggestion["evidence_refs"]:
            evidence = evidence_by_id.get(evidence_id)
            if (
                evidence is None
                or evidence_id not in context_evidence_ids
                or evidence.verification_state != VerificationState.VERIFIED
                or evidence.privacy_level != PrivacyLevel.PUBLIC
            ):
                raise ValueError("Resume edit evidence is not publishable")
            referenced_evidence_ids.add(evidence_id)
        suggestions.append(
            {
                "source_text": suggestion["source_text"],
                "replacement_text": suggestion["replacement_text"],
                "reason": finding.recommendation,
                "evidence_refs": suggestion["evidence_refs"],
            }
        )
        edits.append((source_bytes, replacement_bytes))

    verification = verify_resume_edits(
        {
            "task_type": "suggest_resume_edits",
            "suggestions": suggestions,
        },
        context=context,
    )
    if not verification.safe:
        raise ValueError("Resume edits failed deterministic verification")
    return edits, referenced_evidence_ids


def _validated_finding(
    finding: AiFinding,
) -> tuple[dict[str, Any], bytes, bytes]:
    if (
        finding.finding_type != "resume_edit_suggestion"
        or finding.verifier_state != VerifierState.VERIFIED
        or finding.decision != FindingDecision.ACCEPTED
        or type(finding.payload) is not dict
        or set(finding.payload) != EXPECTED_FINDING_PAYLOAD_FIELDS
    ):
        raise ValueError("AI finding cannot authorize a resume edit")
    payload = finding.payload
    source_text = payload.get("source_text")
    replacement_text = payload.get("replacement_text")
    evidence_refs = payload.get("evidence_refs")
    item_index = payload.get("item_index")
    if (
        type(source_text) is not str
        or type(replacement_text) is not str
        or not 1 <= len(source_text) <= MAX_EDIT_TEXT_CHARACTERS
        or not 1 <= len(replacement_text) <= MAX_EDIT_TEXT_CHARACTERS
        or "\x00" in source_text
        or "\x00" in replacement_text
        or type(evidence_refs) is not list
        or len(evidence_refs) > MAX_EVIDENCE_REFS
        or any(
            type(value) is not str or not IDENTIFIER_PATTERN.fullmatch(value)
            for value in evidence_refs
        )
        or len(set(evidence_refs)) != len(evidence_refs)
        or type(item_index) is not int
        or not 0 <= item_index < 8
        or type(finding.recommendation) is not str
        or not 1 <= len(finding.recommendation) <= MAX_EDIT_REASON_CHARACTERS
        or finding.claim_text != replacement_text
    ):
        raise ValueError("Persisted resume edit payload is invalid")
    try:
        source_bytes = source_text.encode("utf-8")
        replacement_bytes = replacement_text.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Persisted resume edit text is invalid") from exc
    return dict(payload), source_bytes, replacement_bytes


def _apply_edits(
    parent_source: bytes,
    edits: list[tuple[bytes, bytes]],
) -> bytes:
    spans: list[tuple[int, int, bytes]] = []
    for source, replacement in edits:
        first = parent_source.find(source)
        second = parent_source.find(source, first + 1) if first >= 0 else -1
        if first < 0 or second >= 0:
            raise ValueError("Resume edit source span is not unique")
        spans.append((first, first + len(source), replacement))
    spans.sort(key=lambda item: (item[0], item[1]))
    for left, right in zip(spans, spans[1:]):
        if right[0] < left[1]:
            raise ValueError("Resume edit source spans overlap")
    output: list[bytes] = []
    cursor = 0
    for start, end, replacement in spans:
        output.append(parent_source[cursor:start])
        output.append(replacement)
        cursor = end
    output.append(parent_source[cursor:])
    return b"".join(output)


def _publish_source(
    repository: CareerOpsRepository,
    resume_id: str,
    content: bytes,
) -> tuple[Path, Path]:
    if type(resume_id) is not str or not RESUME_ID_PATTERN.fullmatch(resume_id):
        raise ValueError("Resume draft ID is invalid")
    vault_root = Path(os.path.abspath(repository.vault_path))
    resumes_root = vault_root / "resumes"
    _require_directory(vault_root)
    _require_directory(resumes_root)
    if Path(os.path.realpath(resumes_root)).parent != Path(
        os.path.realpath(vault_root)
    ):
        raise ValueError("Resume storage root is invalid")

    draft_dir = resumes_root / resume_id
    draft_dir.mkdir(mode=0o700)
    try:
        _require_directory(draft_dir)
        source_path = draft_dir / "source.tex"
        temporary_path = draft_dir / f".source.tex.tmp-{uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_path, flags, 0o600)
        try:
            view = memoryview(content)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValueError("Resume draft temporary file is invalid")
        finally:
            os.close(descriptor)
        try:
            source_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("Resume draft source already exists")
        os.replace(temporary_path, source_path)
        source_stat = source_path.lstat()
        if (
            stat.S_ISLNK(source_stat.st_mode)
            or not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_nlink != 1
            or source_path.parent != draft_dir
        ):
            raise ValueError("Resume draft source file is invalid")
        _fsync_directory(draft_dir)
        return draft_dir, source_path
    except BaseException:
        _cleanup_new_directory(repository, draft_dir, resume_id)
        raise


def _require_directory(path: Path) -> None:
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("Repository resume storage is invalid")


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_new_directory(
    repository: CareerOpsRepository,
    draft_dir: Path,
    resume_id: str,
) -> None:
    expected = Path(os.path.abspath(repository.vault_path)) / "resumes" / resume_id
    if draft_dir != expected or draft_dir.name != resume_id:
        return
    try:
        directory_stat = draft_dir.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        return
    shutil.rmtree(draft_dir)


__all__ = ["create_verified_resume_draft"]
