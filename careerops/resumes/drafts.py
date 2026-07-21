from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
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
    published = _publish_source(
        repository, resume_id, final_bytes
    )
    committed = False
    try:
        resume = repository.create_and_select_resume_draft(
            application_id=application_id,
            resume_id=resume_id,
            name=safe_name,
            tex_path=published.source_path,
            tex_sha256=hashlib.sha256(final_bytes).hexdigest(),
            authority=authority,
            referenced_evidence_ids=sorted(evidence_ids),
        )
        committed = True
        return resume
    finally:
        if committed:
            try:
                _close_published_source(published)
            except OSError:
                pass
        else:
            _cleanup_new_directory(repository, published)


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


@dataclass
class _PublishedSource:
    resume_id: str
    draft_dir: Path
    source_path: Path
    vault_handle: int
    resumes_handle: int
    child_handle: int
    source_descriptor: int
    child_identity: tuple[int, int]
    source_identity: tuple[int, int]


def _publish_source(
    repository: CareerOpsRepository,
    resume_id: str,
    content: bytes,
) -> _PublishedSource:
    if type(resume_id) is not str or not RESUME_ID_PATTERN.fullmatch(resume_id):
        raise ValueError("Resume draft ID is invalid")
    vault_root = Path(os.path.abspath(repository.vault_path))
    resumes_root = vault_root / "resumes"
    _require_directory(vault_root)
    _require_directory(resumes_root)
    if os.name == "nt":
        return _publish_source_windows(
            vault_root, resumes_root, resume_id, content
        )
    return _publish_source_posix(vault_root, resumes_root, resume_id, content)


def _publish_source_posix(
    vault_root: Path,
    resumes_root: Path,
    resume_id: str,
    content: bytes,
) -> _PublishedSource:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    vault_handle = -1
    resumes_handle = -1
    child_handle = -1
    source_descriptor = -1
    child_created = False
    child_identity: tuple[int, int] | None = None
    temporary_name = ""
    temporary_identity: tuple[int, int] | None = None
    destination_published = False
    try:
        vault_handle = os.open(vault_root, directory_flags)
        _require_open_directory(vault_handle)
        resumes_handle = os.open(
            "resumes", directory_flags, dir_fd=vault_handle
        )
        _require_open_directory(resumes_handle)
        os.mkdir(resume_id, mode=0o700, dir_fd=resumes_handle)
        child_created = True
        child_handle = os.open(resume_id, directory_flags, dir_fd=resumes_handle)
        child_stat = _require_open_directory(child_handle)
        child_identity = (child_stat.st_dev, child_stat.st_ino)

        temporary_name = f".source.tex.tmp-{uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(
            temporary_name, flags, 0o600, dir_fd=child_handle
        )
        _write_and_sync(source_descriptor, content)
        temporary_stat = os.fstat(source_descriptor)
        temporary_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        _posix_atomic_publish_no_replace(
            child_handle,
            temporary_name,
            "source.tex",
        )
        destination_published = True
        source_stat = os.stat(
            "source.tex", dir_fd=child_handle, follow_symlinks=False
        )
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise ValueError("Resume draft source file is invalid")
        os.fsync(child_handle)
        os.fsync(resumes_handle)
        draft_dir = resumes_root / resume_id
        _require_directory_identity(draft_dir, child_identity)
        return _PublishedSource(
            resume_id=resume_id,
            draft_dir=draft_dir,
            source_path=draft_dir / "source.tex",
            vault_handle=vault_handle,
            resumes_handle=resumes_handle,
            child_handle=child_handle,
            source_descriptor=source_descriptor,
            child_identity=child_identity,
            source_identity=(source_stat.st_dev, source_stat.st_ino),
        )
    except BaseException as exc:
        if source_descriptor >= 0:
            if temporary_identity is not None:
                owned_name = (
                    "source.tex" if destination_published else temporary_name
                )
                try:
                    owned_stat = os.stat(
                        owned_name,
                        dir_fd=child_handle,
                        follow_symlinks=False,
                    )
                    if (
                        stat.S_ISREG(owned_stat.st_mode)
                        and (owned_stat.st_dev, owned_stat.st_ino)
                        == temporary_identity
                    ):
                        os.unlink(owned_name, dir_fd=child_handle)
                except OSError:
                    pass
            os.close(source_descriptor)
        if child_handle >= 0:
            os.close(child_handle)
        if (
            child_created
            and child_identity is not None
            and resumes_handle >= 0
        ):
            try:
                _posix_remove_owned_directory(
                    resumes_handle, resume_id, child_identity
                )
            except OSError:
                pass
        if resumes_handle >= 0:
            os.close(resumes_handle)
        if vault_handle >= 0:
            os.close(vault_handle)
        if isinstance(exc, OSError):
            raise ValueError("Resume storage changed during publication") from exc
        raise


def _publish_source_windows(
    vault_root: Path,
    resumes_root: Path,
    resume_id: str,
    content: bytes,
) -> _PublishedSource:
    import msvcrt

    vault_handle = 0
    resumes_handle = 0
    child_handle = 0
    temporary_descriptor = -1
    source_guard = 0
    rename_handle = 0
    temporary_handle = 0
    temporary_name = ""
    temporary_identity: tuple[int, int] | None = None
    renamed = False
    try:
        vault_handle = _windows_open_absolute_directory(vault_root)
        resumes_handle = _windows_open_relative(
            vault_handle, "resumes", directory=True, create=False
        )
        child_handle = _windows_open_relative(
            resumes_handle, resume_id, directory=True, create=True
        )
        child_identity = _windows_directory_identity(child_handle)
        temporary_name = f".source.tex.tmp-{uuid4().hex}"
        temporary_handle = _windows_open_relative(
            child_handle,
            temporary_name,
            directory=False,
            create=True,
            file_access="write",
            share_delete=True,
        )
        temporary_identity = _windows_file_identity(temporary_handle)
        temporary_descriptor = msvcrt.open_osfhandle(
            temporary_handle, os.O_WRONLY | os.O_BINARY
        )
        temporary_handle = 0
        _write_and_sync(temporary_descriptor, content)
        rename_handle = _windows_open_relative(
            child_handle,
            temporary_name,
            directory=False,
            create=False,
            file_access="delete",
            share_delete=True,
        )
        _windows_rename_relative(
            rename_handle,
            child_handle,
            "source.tex",
        )
        renamed = True
        _windows_close_handle(rename_handle)
        rename_handle = 0
        os.fsync(temporary_descriptor)
        source_identity = _windows_file_identity(
            msvcrt.get_osfhandle(temporary_descriptor)
        )
        source_guard = _windows_open_relative(
            child_handle,
            "source.tex",
            directory=False,
            create=False,
            file_access="guard",
            share_delete=False,
        )
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        _require_directory(resumes_root / resume_id)
        return _PublishedSource(
            resume_id=resume_id,
            draft_dir=resumes_root / resume_id,
            source_path=resumes_root / resume_id / "source.tex",
            vault_handle=vault_handle,
            resumes_handle=resumes_handle,
            child_handle=child_handle,
            source_descriptor=source_guard,
            child_identity=child_identity,
            source_identity=source_identity,
        )
    except BaseException as exc:
        if source_guard:
            try:
                _windows_close_handle(source_guard)
            except OSError:
                pass
        if rename_handle:
            try:
                _windows_close_handle(rename_handle)
            except OSError:
                pass
        if temporary_handle:
            try:
                _windows_close_handle(temporary_handle)
            except OSError:
                pass
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        child_is_empty = temporary_identity is None
        if child_handle and temporary_identity is not None:
            try:
                child_is_empty = _windows_delete_owned_relative(
                    child_handle,
                    "source.tex" if renamed else temporary_name,
                    temporary_identity,
                )
            except (OSError, ValueError):
                child_is_empty = False
        if child_handle:
            if child_is_empty:
                try:
                    _windows_mark_delete(child_handle)
                except OSError:
                    pass
            try:
                _windows_close_handle(child_handle)
            except OSError:
                pass
        if resumes_handle:
            try:
                _windows_close_handle(resumes_handle)
            except OSError:
                pass
        if vault_handle:
            try:
                _windows_close_handle(vault_handle)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise ValueError("Resume storage changed during publication") from exc
        raise


def _write_and_sync(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("Resume draft source write did not progress")
        written += count
    os.fsync(descriptor)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ValueError("Resume draft temporary file is invalid")


def _require_open_directory(descriptor: int) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        raise ValueError("Repository resume storage is invalid")
    return opened


def _posix_atomic_publish_no_replace(
    directory_handle: int,
    temporary_name: str,
    destination_name: str,
    *,
    use_renameat2: bool | None = None,
) -> None:
    if use_renameat2 is None:
        use_renameat2 = sys.platform.startswith("linux")
    if use_renameat2:
        try:
            _linux_renameat2_no_replace(
                directory_handle, temporary_name, destination_name
            )
            return
        except OSError as exc:
            unsupported = {
                errno.ENOSYS,
                errno.EINVAL,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                errno.ENOTSUP,
            }
            if exc.errno not in unsupported:
                raise

    os.link(
        temporary_name,
        destination_name,
        src_dir_fd=directory_handle,
        dst_dir_fd=directory_handle,
        follow_symlinks=False,
    )
    os.unlink(temporary_name, dir_fd=directory_handle)


def _linux_renameat2_no_replace(
    directory_handle: int,
    temporary_name: str,
    destination_name: str,
) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_handle,
        os.fsencode(temporary_name),
        directory_handle,
        os.fsencode(destination_name),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )


def _posix_remove_owned_directory(
    resumes_handle: int,
    resume_id: str,
    expected_identity: tuple[int, int],
) -> bool:
    for _attempt in range(2):
        current = os.stat(
            resume_id,
            dir_fd=resumes_handle,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            return False
    os.rmdir(resume_id, dir_fd=resumes_handle)
    return True


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def _require_directory(path: Path) -> None:
    path_stat = path.lstat()
    if _is_link_or_reparse(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("Repository resume storage is invalid")


def _require_directory_identity(
    path: Path, expected_identity: tuple[int, int]
) -> None:
    _require_directory(path)
    if os.name == "nt":
        handle = _windows_open_absolute_directory(path)
        try:
            actual_identity = _windows_directory_identity(handle)
        finally:
            _windows_close_handle(handle)
    else:
        path_stat = path.lstat()
        actual_identity = (path_stat.st_dev, path_stat.st_ino)
    if actual_identity != expected_identity:
        raise ValueError("Resume storage changed during publication")


def _windows_open_absolute_directory(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x00000001 | 0x00000020 | 0x00000080 | 0x00100000,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if not handle or handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _windows_directory_identity(handle)
    except BaseException:
        _windows_close_handle(handle)
        raise
    return int(handle)


def _windows_open_relative(
    root_handle: int,
    name: str,
    *,
    directory: bool,
    create: bool,
    file_access: str = "write",
    share_delete: bool = False,
) -> int:
    import ctypes
    from ctypes import wintypes

    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Repository resume storage name is invalid")

    class UnicodeString(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        )

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", wintypes.LPVOID),
            ("Information", ctypes.c_size_t),
        )

    name_buffer = ctypes.create_unicode_buffer(name)
    name_bytes = name.encode("utf-16-le")
    unicode_name = UnicodeString(
        len(name_bytes),
        len(name_bytes) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        wintypes.HANDLE(root_handle),
        ctypes.pointer(unicode_name),
        0x00000040 | 0x00001000,
        None,
        None,
    )
    io_status = IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG
    desired_access = 0x00000080 | 0x00100000
    if directory:
        desired_access |= 0x00000001 | 0x00000020
    elif file_access == "write":
        desired_access |= 0x00000002
    elif file_access == "delete":
        desired_access |= 0x00010000
    elif file_access != "guard":
        raise ValueError("Repository resume storage access is invalid")
    if create and directory:
        desired_access |= 0x00010000
    create_options = 0x00200000 | 0x00000020
    create_options |= 0x00000001 if directory else 0x00000040
    status_code = nt_create_file(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0x00000010 if directory else 0x00000080,
        0x00000001 | 0x00000002 | (0x00000004 if share_delete else 0),
        2 if create else 1,
        create_options,
        None,
        0,
    )
    if status_code < 0:
        _raise_windows_ntstatus(status_code)
    opened_handle = int(handle.value)
    try:
        file_attributes, _identity, link_count = _windows_handle_information(
            opened_handle
        )
        if (
            bool(file_attributes & 0x00000400)
            or bool(file_attributes & 0x00000010) != directory
            or (not directory and link_count != 1)
        ):
            raise ValueError("Repository resume storage handle is invalid")
    except BaseException:
        _windows_close_handle(opened_handle)
        raise
    return opened_handle


def _windows_handle_information(
    handle: int,
) -> tuple[int, tuple[int, int], int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        )

    information = ByHandleFileInformation()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    if not get_information(wintypes.HANDLE(handle), ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    file_id = (information.FileIndexHigh << 32) | information.FileIndexLow
    return (
        information.FileAttributes,
        (information.VolumeSerialNumber, file_id),
        information.NumberOfLinks,
    )


def _windows_directory_identity(handle: int) -> tuple[int, int]:
    file_attributes, identity, _link_count = _windows_handle_information(handle)
    if not file_attributes & 0x00000010 or file_attributes & 0x00000400:
        raise ValueError("Repository resume storage is invalid")
    return identity


def _windows_file_identity(handle: int) -> tuple[int, int]:
    file_attributes, identity, link_count = _windows_handle_information(handle)
    if (
        file_attributes & 0x00000010
        or file_attributes & 0x00000400
        or link_count != 1
    ):
        raise ValueError("Resume draft source file is invalid")
    return identity


def _windows_delete_owned_relative(
    directory_handle: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    delete_handle = _windows_open_relative(
        directory_handle,
        name,
        directory=False,
        create=False,
        file_access="delete",
        share_delete=False,
    )
    try:
        if _windows_file_identity(delete_handle) != expected_identity:
            return False
        _windows_mark_delete(delete_handle)
        return True
    finally:
        _windows_close_handle(delete_handle)


def _windows_rename_relative(
    source_handle: int, directory_handle: int, destination_name: str
) -> None:
    import ctypes
    from ctypes import wintypes

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", wintypes.LPVOID),
            ("Information", ctypes.c_size_t),
        )

    class FileRenameInformation(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.ULONG),
            ("FileName", wintypes.WCHAR * 1),
        )

    encoded_name = destination_name.encode("utf-16-le")
    buffer_size = FileRenameInformation.FileName.offset + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    information = FileRenameInformation.from_buffer(buffer)
    information.ReplaceIfExists = False
    information.RootDirectory = wintypes.HANDLE(directory_handle)
    information.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + FileRenameInformation.FileName.offset,
        encoded_name,
        len(encoded_name),
    )
    io_status = IoStatusBlock()
    ntdll = ctypes.WinDLL("ntdll")
    nt_set_information = ntdll.NtSetInformationFile
    nt_set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    )
    nt_set_information.restype = wintypes.LONG
    status_code = nt_set_information(
        wintypes.HANDLE(source_handle),
        ctypes.byref(io_status),
        buffer,
        buffer_size,
        10,
    )
    if status_code < 0:
        _raise_windows_ntstatus(status_code)


def _windows_mark_delete(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInformation(ctypes.Structure):
        _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

    disposition = FileDispositionInformation(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    if not set_information(
        wintypes.HANDLE(handle),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _raise_windows_ntstatus(status_code: int) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    convert_status = ntdll.RtlNtStatusToDosError
    convert_status.argtypes = (wintypes.LONG,)
    convert_status.restype = wintypes.ULONG
    raise ctypes.WinError(convert_status(status_code))


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_published_source(published: _PublishedSource) -> None:
    first_error: OSError | None = None
    if os.name == "nt":
        for handle in (
            published.source_descriptor,
            published.child_handle,
            published.resumes_handle,
            published.vault_handle,
        ):
            try:
                _windows_close_handle(handle)
            except OSError as exc:
                first_error = first_error or exc
    else:
        for descriptor in (
            published.source_descriptor,
            published.child_handle,
            published.resumes_handle,
            published.vault_handle,
        ):
            try:
                os.close(descriptor)
            except OSError as exc:
                first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _cleanup_new_directory(
    _repository: CareerOpsRepository,
    published: _PublishedSource,
) -> None:
    if os.name == "nt":
        try:
            _windows_close_handle(published.source_descriptor)
        except OSError:
            pass
        source_removed = False
        try:
            source_removed = _windows_delete_owned_relative(
                published.child_handle,
                "source.tex",
                published.source_identity,
            )
        except (OSError, ValueError):
            pass
        if source_removed:
            try:
                _windows_mark_delete(published.child_handle)
            except OSError:
                pass
        for handle in (
            published.child_handle,
            published.resumes_handle,
            published.vault_handle,
        ):
            try:
                _windows_close_handle(handle)
            except OSError:
                pass
        return

    source_removed = False
    try:
        source_stat = os.stat(
            "source.tex",
            dir_fd=published.child_handle,
            follow_symlinks=False,
        )
        if (
            stat.S_ISREG(source_stat.st_mode)
            and source_stat.st_nlink == 1
            and (source_stat.st_dev, source_stat.st_ino)
            == published.source_identity
        ):
            os.unlink("source.tex", dir_fd=published.child_handle)
            source_removed = True
    except OSError:
        pass
    try:
        os.close(published.source_descriptor)
    except OSError:
        pass
    try:
        os.close(published.child_handle)
    except OSError:
        pass
    if source_removed:
        try:
            _posix_remove_owned_directory(
                published.resumes_handle,
                published.resume_id,
                published.child_identity,
            )
        except OSError:
            pass
    for handle in (published.resumes_handle, published.vault_handle):
        try:
            os.close(handle)
        except OSError:
            pass


__all__ = ["create_verified_resume_draft"]
