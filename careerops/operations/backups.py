from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Any, BinaryIO, Iterable
import unicodedata
from uuid import uuid4
import zipfile


BACKUP_VERSION = "careerops_backup_v1"
DATABASE_ARCHIVE_PATH = "data/careerops.db"
MANIFEST_ARCHIVE_PATH = "manifest.json"
CHUNK_SIZE = 128 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class BackupError(RuntimeError):
    """Base error with a bounded operational message."""


class BackupPathError(BackupError):
    """A source, destination, or archive member path was unsafe."""


class BackupIntegrityError(BackupError):
    """Backup bytes did not match the declared archive contract."""


class RestoreTargetError(BackupError):
    """Restore targets were unsafe or could not be published atomically."""


@dataclass(frozen=True)
class BackupLimits:
    max_files: int = 100_000
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024 * 1024
    max_archive_bytes: int = 8 * 1024 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_compression_ratio: float = 1_000.0

    def __post_init__(self) -> None:
        integer_values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_archive_bytes,
            self.max_manifest_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in integer_values):
            raise ValueError("Backup limits must be positive integers")
        if self.max_compression_ratio <= 0:
            raise ValueError("Backup compression ratio limit must be positive")


DEFAULT_LIMITS = BackupLimits()


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest_sha256: str
    archive_sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    archive_path: Path
    manifest_sha256: str
    database_path: Path
    vault_path: Path
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class _RestoreTargets:
    data: Path
    vault: Path
    data_identity: tuple[int, int, int, int]
    vault_identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _RestoreLayout:
    transaction_id: str
    data: Path
    vault: Path
    data_stage: Path
    vault_stage: Path
    data_hold: Path
    vault_hold: Path
    journals: tuple[Path, ...]


@dataclass(frozen=True)
class _RestoreTransaction:
    layout: _RestoreLayout
    archive_sha256: str
    manifest_sha256: str
    file_count: int
    total_bytes: int
    data_identity: tuple[int, int]
    vault_identity: tuple[int, int]


class _DuplicateJsonMember(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonMember(key)
        result[key] = value
    return result


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        junction_check = getattr(path, "is_junction", None)
        if callable(junction_check) and junction_check():
            return True
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise BackupPathError("A filesystem path could not be inspected") from error
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _absolute(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    return Path(os.path.abspath(candidate))


def _assert_no_link_components(path: Path, *, include_leaf: bool = True) -> None:
    candidate = _absolute(path)
    parts = candidate.parts
    if not parts:
        raise BackupPathError("A filesystem path was empty")
    current = Path(parts[0])
    stop = len(parts) if include_leaf else len(parts) - 1
    for part in parts[1:stop]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        if _is_link_or_reparse(current):
            raise BackupPathError("Filesystem paths must not traverse links or reparse points")


def _regular_file(path: Path | str, description: str) -> Path:
    candidate = _absolute(path)
    _assert_no_link_components(candidate)
    try:
        value = candidate.lstat()
    except OSError as error:
        raise BackupPathError(f"{description} must be an existing regular file") from error
    if _is_link_or_reparse(candidate) or not stat.S_ISREG(value.st_mode):
        raise BackupPathError(f"{description} must be an existing regular file")
    if value.st_nlink != 1:
        raise BackupPathError(f"{description} must be a single-link regular file")
    return candidate


def _regular_directory(path: Path | str, description: str) -> Path:
    candidate = _absolute(path)
    _assert_no_link_components(candidate)
    try:
        value = candidate.lstat()
    except OSError as error:
        raise BackupPathError(f"{description} must be an existing directory") from error
    if _is_link_or_reparse(candidate) or not stat.S_ISDIR(value.st_mode):
        raise BackupPathError(f"{description} must be an existing directory")
    return candidate


def _ensure_backup_root(path: Path | str) -> Path:
    root = _absolute(path)
    _assert_no_link_components(root, include_leaf=False)
    if not root.exists():
        try:
            root.mkdir()
        except OSError as error:
            raise BackupPathError("Backup root could not be created") from error
    return _regular_directory(root, "Backup root")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _directory_identity(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stream_file(source: Path, destination: BinaryIO, limit: int) -> tuple[int, str]:
    before_path = source.lstat()
    if _is_link_or_reparse(source) or not stat.S_ISREG(before_path.st_mode):
        raise BackupPathError("Vault entries must be regular files")
    if before_path.st_nlink != 1:
        raise BackupPathError("Backup source files must be single-link regular files")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    digest = hashlib.sha256()
    observed = 0
    try:
        descriptor = os.open(source, flags)
        before_fd = os.fstat(descriptor)
        if not stat.S_ISREG(before_fd.st_mode) or before_fd.st_nlink != 1:
            raise BackupPathError("Backup source files must be single-link regular files")
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            observed += len(chunk)
            if observed > limit:
                raise BackupIntegrityError("A backup file exceeded its size limit")
            destination.write(chunk)
            digest.update(chunk)
        after_fd = os.fstat(descriptor)
    except OSError as error:
        raise BackupPathError("A backup source file could not be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after_path = source.lstat()
    except OSError as error:
        raise BackupPathError("A backup source changed during the snapshot") from error
    if (
        _stat_identity(before_path) != _stat_identity(after_path)
        or _stat_identity(before_fd) != _stat_identity(after_fd)
        or (after_fd.st_dev, after_fd.st_ino) != (after_path.st_dev, after_path.st_ino)
        or after_fd.st_nlink != 1
        or after_path.st_nlink != 1
    ):
        raise BackupPathError("A backup source changed during the snapshot")
    return observed, digest.hexdigest()


def _safe_archive_path(raw: str, *, allow_manifest: bool = True) -> str:
    if not isinstance(raw, str) or not raw or raw != unicodedata.normalize("NFC", raw):
        raise BackupPathError("Archive member paths must be normalized UTF-8 names")
    if (
        "\\" in raw
        or ":" in raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
        or raw.startswith("/")
        or PureWindowsPath(raw).drive
    ):
        raise BackupPathError("Archive member path was absolute or ambiguous")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupPathError("Archive member path contained traversal")
    for part in parts:
        if part != part.rstrip(" ."):
            raise BackupPathError("Archive member path was Windows-ambiguous")
        base = part.split(".", 1)[0].upper()
        if base in WINDOWS_RESERVED_NAMES:
            raise BackupPathError("Archive member path used a reserved device name")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != raw:
        raise BackupPathError("Archive member path was not normalized")
    if allow_manifest and raw == MANIFEST_ARCHIVE_PATH:
        return raw
    if raw == DATABASE_ARCHIVE_PATH:
        return raw
    if len(parts) >= 2 and parts[0] == "vault":
        return raw
    raise BackupPathError("Archive member path was outside data and vault roots")


def _vault_files(vault: Path) -> Iterable[tuple[str, Path]]:
    stack = [vault]
    while stack:
        directory = stack.pop()
        if _is_link_or_reparse(directory):
            raise BackupPathError("Vault directories must not be links or reparse points")
        children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        for entry in reversed(children):
            path = Path(entry.path)
            value = path.lstat()
            if _is_link_or_reparse(path):
                raise BackupPathError("Vault entries must not be links or reparse points")
            if stat.S_ISDIR(value.st_mode):
                stack.append(path)
            elif stat.S_ISREG(value.st_mode):
                relative = path.relative_to(vault).as_posix()
                yield _safe_archive_path(f"vault/{relative}", allow_manifest=False), path
            else:
                raise BackupPathError("Vault entries must be regular files or directories")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _manifest(records: list[_FileRecord]) -> tuple[bytes, str]:
    payload = {
        "files": [asdict(record) for record in records],
        "version": BACKUP_VERSION,
    }
    manifest_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return _canonical_json({**payload, "manifest_sha256": manifest_sha256}), manifest_sha256


def _snapshot_database(source: Path, destination: Path) -> None:
    source_uri = source.as_uri() + "?mode=ro"
    deadline = time.monotonic() + 60

    def enforce_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupIntegrityError("SQLite backup exceeded its snapshot deadline")

    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as live:
            with closing(sqlite3.connect(destination)) as snapshot:
                live.backup(snapshot, pages=-1, progress=enforce_deadline, sleep=0.01)
                snapshot.commit()
        with closing(sqlite3.connect(f"{destination.as_uri()}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupIntegrityError("SQLite snapshot failed its integrity check")
        _fsync_file(destination)
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupIntegrityError("SQLite backup could not produce a valid snapshot") from error


def _hash_path(path: Path, limit: int) -> tuple[int, str]:
    with open(os.devnull, "wb") as sink:
        return _stream_file(path, sink, limit)


def _atomic_rename(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def create_backup(
    database_path: Path | str,
    vault_path: Path | str,
    backup_root: Path | str,
    *,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> BackupResult:
    database = _regular_file(database_path, "Database")
    vault = _regular_directory(vault_path, "Vault")
    root = _ensure_backup_root(backup_root)
    if root == vault or root.is_relative_to(vault):
        raise BackupPathError("Backup root must be outside the vault")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"careerops-backup-{timestamp}-{uuid4().hex}.zip"
    final_path = root / archive_name
    temp_path = root / f".{archive_name}.tmp"
    stage_path: Path | None = None
    try:
        stage_path = Path(tempfile.mkdtemp(prefix=".careerops-backup-stage-", dir=root))
        snapshot_db = stage_path / "careerops.db"
        snapshot_vault = stage_path / "vault"
        snapshot_vault.mkdir()
        _snapshot_database(database, snapshot_db)

        records: list[_FileRecord] = []
        db_size, db_sha256 = _hash_path(snapshot_db, limits.max_file_bytes)
        records.append(_FileRecord(DATABASE_ARCHIVE_PATH, db_size, db_sha256))
        total_bytes = db_size
        if total_bytes > limits.max_total_bytes:
            raise BackupIntegrityError("Backup exceeded its total size limit")
        seen = {DATABASE_ARCHIVE_PATH.casefold()}
        for archive_path, source in _vault_files(vault):
            if len(records) >= limits.max_files:
                raise BackupIntegrityError("Backup exceeded its file-count limit")
            folded = archive_path.casefold()
            if folded in seen:
                raise BackupPathError("Vault paths collided after case normalization")
            seen.add(folded)
            destination = snapshot_vault / PurePosixPath(archive_path).relative_to("vault")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                size, sha256 = _stream_file(source, output, limits.max_file_bytes)
                output.flush()
                os.fsync(output.fileno())
            total_bytes += size
            if total_bytes > limits.max_total_bytes:
                raise BackupIntegrityError("Backup exceeded its total size limit")
            records.append(_FileRecord(archive_path, size, sha256))
        records.sort(key=lambda item: item.path)
        manifest_bytes, manifest_sha256 = _manifest(records)
        if len(manifest_bytes) > limits.max_manifest_bytes:
            raise BackupIntegrityError("Backup manifest exceeded its size limit")

        with zipfile.ZipFile(
            temp_path,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr(MANIFEST_ARCHIVE_PATH, manifest_bytes)
            for record in records:
                source = (
                    snapshot_db
                    if record.path == DATABASE_ARCHIVE_PATH
                    else snapshot_vault / PurePosixPath(record.path).relative_to("vault")
                )
                archive.write(source, record.path)
        _fsync_file(temp_path)
        if temp_path.stat().st_size > limits.max_archive_bytes:
            raise BackupIntegrityError("Backup archive exceeded its size limit")
        archive_size, archive_sha256 = _hash_path(temp_path, limits.max_archive_bytes)
        if archive_size <= 0:
            raise BackupIntegrityError("Backup archive was empty")
        _atomic_rename(temp_path, final_path)
        _fsync_directory(root)
        return BackupResult(
            path=final_path,
            manifest_sha256=manifest_sha256,
            archive_sha256=archive_sha256,
            file_count=len(records),
            total_bytes=total_bytes,
        )
    except BackupError:
        raise
    except OSError as error:
        raise BackupPathError("Backup could not be published atomically") from error
    finally:
        if temp_path.exists() and not _is_link_or_reparse(temp_path):
            temp_path.unlink()
        if stage_path is not None and stage_path.exists() and not _is_link_or_reparse(stage_path):
            shutil.rmtree(stage_path)


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = stream.read(min(CHUNK_SIZE, limit + 1 - observed))
        if not chunk:
            break
        observed += len(chunk)
        if observed > limit:
            raise BackupIntegrityError("Archive member exceeded its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_zip_info(info: zipfile.ZipInfo, limits: BackupLimits) -> str:
    raw_name = getattr(info, "orig_filename", info.filename)
    if raw_name != info.filename:
        raise BackupPathError("Archive member path used platform-dependent separators")
    path = _safe_archive_path(raw_name)
    if info.is_dir() or info.flag_bits & 0x1:
        raise BackupPathError("Archive members must be unencrypted regular files")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG}:
        raise BackupPathError("Archive members must not be links or special files")
    if info.file_size < 0 or info.file_size > limits.max_file_bytes:
        raise BackupIntegrityError("Archive member exceeded its size limit")
    if info.compress_size < 0:
        raise BackupIntegrityError("Archive member compression metadata was invalid")
    if info.file_size and info.compress_size == 0:
        raise BackupIntegrityError("Archive member compression ratio was invalid")
    ratio = info.file_size / max(1, info.compress_size)
    if ratio > limits.max_compression_ratio:
        raise BackupIntegrityError("Archive member exceeded its compression-ratio limit")
    return path


def _parse_manifest(raw: bytes) -> tuple[list[_FileRecord], str]:
    try:
        value = json.loads(raw, object_pairs_hook=_strict_json_object)
    except (UnicodeError, ValueError, TypeError) as error:
        raise BackupIntegrityError("Backup manifest was not strict JSON") from error
    if type(value) is not dict or set(value) != {"version", "files", "manifest_sha256"}:
        raise BackupIntegrityError("Backup manifest shape was invalid")
    if value["version"] != BACKUP_VERSION or type(value["files"]) is not list:
        raise BackupIntegrityError("Backup manifest version was unsupported")
    manifest_sha256 = value["manifest_sha256"]
    if type(manifest_sha256) is not str or SHA256_PATTERN.fullmatch(manifest_sha256) is None:
        raise BackupIntegrityError("Backup manifest checksum was invalid")
    payload = {"files": value["files"], "version": value["version"]}
    expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not secrets_compare_digest(expected, manifest_sha256):
        raise BackupIntegrityError("Backup manifest checksum did not match")

    records: list[_FileRecord] = []
    seen: set[str] = set()
    for item in value["files"]:
        if type(item) is not dict or set(item) != {"path", "size", "sha256"}:
            raise BackupIntegrityError("Backup file manifest entry was invalid")
        path = _safe_archive_path(item["path"], allow_manifest=False)
        folded = path.casefold()
        if folded in seen:
            raise BackupPathError("Backup manifest contained colliding paths")
        seen.add(folded)
        size = item["size"]
        sha256 = item["sha256"]
        if type(size) is not int or size < 0:
            raise BackupIntegrityError("Backup file size was invalid")
        if type(sha256) is not str or SHA256_PATTERN.fullmatch(sha256) is None:
            raise BackupIntegrityError("Backup file checksum was invalid")
        records.append(_FileRecord(path, size, sha256))
    if [item.path for item in records] != sorted(item.path for item in records):
        raise BackupIntegrityError("Backup manifest files were not sorted")
    if sum(item.path == DATABASE_ARCHIVE_PATH for item in records) != 1:
        raise BackupIntegrityError("Backup manifest did not declare one database")
    return records, manifest_sha256


def secrets_compare_digest(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _validate_restore_target(path: Path | str, description: str) -> tuple[Path, tuple[int, int, int, int]]:
    candidate = _absolute(path)
    try:
        _assert_no_link_components(candidate)
    except BackupPathError as error:
        raise RestoreTargetError(
            f"{description} must be an existing empty non-link directory"
        ) from error
    try:
        value = candidate.lstat()
    except OSError as error:
        raise RestoreTargetError(f"{description} must be an existing empty directory") from error
    if _is_link_or_reparse(candidate) or not stat.S_ISDIR(value.st_mode):
        raise RestoreTargetError(f"{description} must be an existing empty non-link directory")
    try:
        if next(candidate.iterdir(), None) is not None:
            raise RestoreTargetError(f"{description} must be empty")
    except OSError as error:
        raise RestoreTargetError(f"{description} could not be inspected") from error
    return candidate, _directory_identity(candidate)


def _restore_targets(data_path: Path | str, vault_path: Path | str) -> _RestoreTargets:
    data, data_identity = _validate_restore_target(data_path, "Data target")
    vault, vault_identity = _validate_restore_target(vault_path, "Vault target")
    if data == vault or data.is_relative_to(vault) or vault.is_relative_to(data):
        raise RestoreTargetError("Data and vault targets must be separate directories")
    return _RestoreTargets(data, vault, data_identity, vault_identity)


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    record: _FileRecord,
    limits: BackupLimits,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    observed = 0
    try:
        with archive.open(info, "r") as source, destination.open("xb") as output:
            while True:
                chunk = source.read(min(CHUNK_SIZE, limits.max_file_bytes + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > limits.max_file_bytes or observed > record.size:
                    raise BackupIntegrityError("Archive member exceeded its declared size")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BackupError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise BackupIntegrityError("Archive member could not be extracted safely") from error
    if observed != record.size or not secrets_compare_digest(digest.hexdigest(), record.sha256):
        raise BackupIntegrityError("Archive member checksum or size did not match")


def _revalidate_empty_target(path: Path, identity: tuple[int, int, int, int]) -> None:
    try:
        if _is_link_or_reparse(path) or _directory_identity(path) != identity:
            raise RestoreTargetError("Restore target identity changed before publication")
        if next(path.iterdir(), None) is not None:
            raise RestoreTargetError("Restore target changed before publication")
    except BackupError:
        raise
    except OSError as error:
        raise RestoreTargetError("Restore target could not be revalidated") from error


def _safe_remove_tree(path: Path | None, parent: Path) -> None:
    if path is None or not path.exists():
        return
    if path.parent != parent or not path.name.startswith(".careerops-restore-") or _is_link_or_reparse(path):
        raise RestoreTargetError("Refusing to clean an unowned restore staging path")
    shutil.rmtree(path)


def _restore_layout(data_path: Path | str, vault_path: Path | str) -> _RestoreLayout:
    data = _absolute(data_path)
    vault = _absolute(vault_path)
    try:
        _assert_no_link_components(data)
        _assert_no_link_components(vault)
        _regular_directory(data.parent, "Data target parent")
        _regular_directory(vault.parent, "Vault target parent")
    except BackupPathError as error:
        raise RestoreTargetError("Restore target parents must be existing non-link directories") from error
    if data == vault or data.is_relative_to(vault) or vault.is_relative_to(data):
        raise RestoreTargetError("Data and vault targets must be separate directories")
    identity_bytes = (
        f"{os.path.normcase(str(data))}\0{os.path.normcase(str(vault))}"
    ).encode("utf-8")
    transaction_id = hashlib.sha256(identity_bytes).hexdigest()[:32]
    journal_name = f".careerops-restore-transaction-{transaction_id}.json"
    journals = (data.parent / journal_name,)
    return _RestoreLayout(
        transaction_id=transaction_id,
        data=data,
        vault=vault,
        data_stage=data.parent / f".careerops-restore-data-{transaction_id}",
        vault_stage=vault.parent / f".careerops-restore-vault-{transaction_id}",
        data_hold=data.parent / f".careerops-empty-data-{transaction_id}",
        vault_hold=vault.parent / f".careerops-empty-vault-{transaction_id}",
        journals=journals,
    )


def _journal_payload(transaction: _RestoreTransaction, phase: str) -> dict[str, Any]:
    if phase not in {
        "prepared",
        "data_target_held",
        "vault_target_held",
        "data_published",
        "vault_published",
        "committed",
    }:
        raise RestoreTargetError("Restore transaction phase was invalid")
    layout = transaction.layout
    payload: dict[str, Any] = {
        "version": 1,
        "transaction_id": layout.transaction_id,
        "phase": phase,
        "data_target": str(layout.data),
        "vault_target": str(layout.vault),
        "data_stage": str(layout.data_stage),
        "vault_stage": str(layout.vault_stage),
        "data_hold": str(layout.data_hold),
        "vault_hold": str(layout.vault_hold),
        "archive_sha256": transaction.archive_sha256,
        "manifest_sha256": transaction.manifest_sha256,
        "file_count": transaction.file_count,
        "total_bytes": transaction.total_bytes,
        "data_identity": list(transaction.data_identity),
        "vault_identity": list(transaction.vault_identity),
    }
    checksum_bytes = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload["journal_sha256"] = hashlib.sha256(checksum_bytes).hexdigest()
    return payload


def _journal_bytes(transaction: _RestoreTransaction, phase: str) -> bytes:
    return json.dumps(
        _journal_payload(transaction, phase),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_restore_journals(transaction: _RestoreTransaction, phase: str) -> None:
    raw = _journal_bytes(transaction, phase)
    for journal in transaction.layout.journals:
        temporary = journal.parent / f".{journal.name}.tmp-{uuid4().hex}"
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, journal)
            _fsync_directory(journal.parent)
        except OSError as error:
            if temporary.exists() and not _is_link_or_reparse(temporary):
                temporary.unlink()
            raise RestoreTargetError("Restore transaction journal could not be persisted") from error


def _read_restore_journal(path: Path, layout: _RestoreLayout) -> tuple[_RestoreTransaction, str]:
    try:
        value = path.lstat()
        if _is_link_or_reparse(path) or not stat.S_ISREG(value.st_mode) or value.st_size > 32 * 1024:
            raise RestoreTargetError("Restore transaction journal was invalid")
        raw = path.read_bytes()
        payload = json.loads(raw, object_pairs_hook=_strict_json_object)
    except RestoreTargetError:
        raise
    except (OSError, UnicodeError, ValueError, _DuplicateJsonMember) as error:
        raise RestoreTargetError("Restore transaction journal was invalid") from error
    expected_fields = {
        "version",
        "transaction_id",
        "phase",
        "data_target",
        "vault_target",
        "data_stage",
        "vault_stage",
        "data_hold",
        "vault_hold",
        "archive_sha256",
        "manifest_sha256",
        "file_count",
        "total_bytes",
        "data_identity",
        "vault_identity",
        "journal_sha256",
    }
    if type(payload) is not dict or set(payload) != expected_fields:
        raise RestoreTargetError("Restore transaction journal was invalid")
    checksum = payload.pop("journal_sha256")
    checksum_bytes = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    fixed_paths = {
        "data_target": layout.data,
        "vault_target": layout.vault,
        "data_stage": layout.data_stage,
        "vault_stage": layout.vault_stage,
        "data_hold": layout.data_hold,
        "vault_hold": layout.vault_hold,
    }
    identities = (payload.get("data_identity"), payload.get("vault_identity"))
    invalid = bool(
        payload.get("version") != 1
        or payload.get("transaction_id") != layout.transaction_id
        or type(checksum) is not str
        or SHA256_PATTERN.fullmatch(checksum) is None
        or not secrets_compare_digest(hashlib.sha256(checksum_bytes).hexdigest(), checksum)
        or any(payload.get(name) != str(expected) for name, expected in fixed_paths.items())
        or type(payload.get("archive_sha256")) is not str
        or SHA256_PATTERN.fullmatch(payload["archive_sha256"]) is None
        or type(payload.get("manifest_sha256")) is not str
        or SHA256_PATTERN.fullmatch(payload["manifest_sha256"]) is None
        or type(payload.get("file_count")) is not int
        or payload["file_count"] <= 0
        or type(payload.get("total_bytes")) is not int
        or payload["total_bytes"] <= 0
        or any(
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(item) is not int or item < 0 for item in identity)
            for identity in identities
        )
    )
    if invalid:
        raise RestoreTargetError("Restore transaction journal was invalid")
    try:
        transaction = _RestoreTransaction(
            layout=layout,
            archive_sha256=payload["archive_sha256"],
            manifest_sha256=payload["manifest_sha256"],
            file_count=payload["file_count"],
            total_bytes=payload["total_bytes"],
            data_identity=tuple(payload["data_identity"]),
            vault_identity=tuple(payload["vault_identity"]),
        )
        phase = payload["phase"]
        _journal_payload(transaction, phase)
    except (TypeError, RestoreTargetError) as error:
        raise RestoreTargetError("Restore transaction journal was invalid") from error
    return transaction, phase


def _restore_journals_exist(layout: _RestoreLayout) -> bool:
    return any(path.exists() or path.is_symlink() for path in layout.journals)


def _validate_original_hold(path: Path, identity: tuple[int, int]) -> None:
    try:
        value = path.lstat()
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISDIR(value.st_mode)
            or (value.st_dev, value.st_ino) != identity
            or next(path.iterdir(), None) is not None
        ):
            raise RestoreTargetError("Restore recovery found a changed target placeholder")
    except RestoreTargetError:
        raise
    except OSError as error:
        raise RestoreTargetError("Restore recovery could not inspect a target placeholder") from error


def _rollback_target(
    *,
    target: Path,
    hold: Path,
    stage: Path,
    identity: tuple[int, int],
    records: list[_FileRecord],
    target_kind: str,
) -> None:
    hold_exists = hold.exists() or hold.is_symlink()
    target_exists = target.exists() or target.is_symlink()
    stage_exists = stage.exists() or stage.is_symlink()
    if hold_exists:
        _validate_original_hold(hold, identity)
        if target_exists:
            if stage_exists or _is_link_or_reparse(target) or not target.is_dir():
                raise RestoreTargetError("Restore recovery found ambiguous published data")
            _verify_restore_target_files(target, records, target_kind)
            _atomic_rename(target, stage)
            target_exists = False
        if not target_exists:
            _atomic_rename(hold, target)
        return
    if not target_exists:
        raise RestoreTargetError("Restore recovery could not locate the original target")
    _validate_original_hold(target, identity)


def _remove_restore_journals(layout: _RestoreLayout) -> None:
    for journal in layout.journals:
        if journal.exists() or journal.is_symlink():
            if _is_link_or_reparse(journal) or not journal.is_file():
                raise RestoreTargetError("Restore transaction journal changed during recovery")
            journal.unlink()
            _fsync_directory(journal.parent)


def _recover_restore_transaction(
    layout: _RestoreLayout,
    *,
    archive_sha256: str,
    manifest_sha256: str,
    records: list[_FileRecord],
) -> _RestoreTransaction | None:
    existing = [path for path in layout.journals if path.exists() or path.is_symlink()]
    if not existing:
        return None
    observed = [_read_restore_journal(path, layout) for path in existing]
    transaction = observed[0][0]
    if any(item[0] != transaction for item in observed[1:]):
        raise RestoreTargetError("Restore transaction journals did not agree")
    if (
        transaction.archive_sha256 != archive_sha256
        or transaction.manifest_sha256 != manifest_sha256
        or transaction.file_count != len(records)
        or transaction.total_bytes != sum(record.size for record in records)
    ):
        raise RestoreTargetError("Restore transaction did not match this backup")
    fully_committed = bool(
        len(existing) == len(layout.journals)
        and all(phase == "committed" for _item, phase in observed)
    )
    if fully_committed:
        for target, hold, stage, identity in (
            (layout.data, layout.data_hold, layout.data_stage, transaction.data_identity),
            (layout.vault, layout.vault_hold, layout.vault_stage, transaction.vault_identity),
        ):
            if (
                not target.is_dir()
                or _is_link_or_reparse(target)
                or stage.exists()
                or stage.is_symlink()
            ):
                raise RestoreTargetError("Committed restore transaction was incomplete")
            if hold.exists() or hold.is_symlink():
                _validate_original_hold(hold, identity)
        return transaction
    try:
        _rollback_target(
            target=layout.vault,
            hold=layout.vault_hold,
            stage=layout.vault_stage,
            identity=transaction.vault_identity,
            records=records,
            target_kind="vault",
        )
        _rollback_target(
            target=layout.data,
            hold=layout.data_hold,
            stage=layout.data_stage,
            identity=transaction.data_identity,
            records=records,
            target_kind="data",
        )
        _safe_remove_tree(layout.data_stage, layout.data.parent)
        _safe_remove_tree(layout.vault_stage, layout.vault.parent)
        _remove_restore_journals(layout)
        _fsync_directory(layout.data.parent)
        if layout.vault.parent != layout.data.parent:
            _fsync_directory(layout.vault.parent)
    except RestoreTargetError:
        raise
    except (OSError, BackupError) as error:
        raise RestoreTargetError("Restore transaction recovery could not complete") from error
    return None


def _finalize_committed_restore(transaction: _RestoreTransaction) -> None:
    layout = transaction.layout
    try:
        if layout.data_hold.exists():
            os.rmdir(layout.data_hold)
        if layout.vault_hold.exists():
            os.rmdir(layout.vault_hold)
        _remove_restore_journals(layout)
        _fsync_directory(layout.data.parent)
        if layout.vault.parent != layout.data.parent:
            _fsync_directory(layout.vault.parent)
    except (OSError, BackupError) as error:
        raise RestoreTargetError("Committed restore transaction could not be finalized") from error


def _verify_restore_target_files(
    target: Path,
    records: list[_FileRecord],
    target_kind: str,
) -> None:
    if target_kind == "data":
        expected = {
            record.path: record
            for record in records
            if record.path == DATABASE_ARCHIVE_PATH
        }
        try:
            entries = list(target.iterdir())
        except OSError as error:
            raise RestoreTargetError("Restore recovery found changed published files") from error
        if len(entries) != 1 or entries[0].name != "careerops.db":
            raise RestoreTargetError("Restore recovery found changed published files")
        observed: dict[str, Path] = {DATABASE_ARCHIVE_PATH: entries[0]}
    elif target_kind == "vault":
        expected = {
            record.path: record
            for record in records
            if record.path != DATABASE_ARCHIVE_PATH
        }
        try:
            observed = {
                archive_path: source for archive_path, source in _vault_files(target)
            }
        except BackupError as error:
            raise RestoreTargetError("Restore recovery found changed published files") from error
    else:
        raise RestoreTargetError("Restore recovery target kind was invalid")
    if set(observed) != set(expected):
        raise RestoreTargetError("Restore recovery found changed published files")
    try:
        for archive_path, source in observed.items():
            record = expected[archive_path]
            size, digest = _hash_path(source, max(record.size, 1))
            if size != record.size or not secrets_compare_digest(digest, record.sha256):
                raise RestoreTargetError("Restore recovery found changed published files")
    except RestoreTargetError:
        raise
    except BackupError as error:
        raise RestoreTargetError("Restore recovery found changed published files") from error


def _verify_committed_files(transaction: _RestoreTransaction, records: list[_FileRecord]) -> None:
    _verify_restore_target_files(transaction.layout.data, records, "data")
    _verify_restore_target_files(transaction.layout.vault, records, "vault")


def _restore_publication_checkpoint(_boundary: str) -> None:
    return None


def _publish_restore(
    targets: _RestoreTargets,
    transaction: _RestoreTransaction,
    records: list[_FileRecord],
) -> None:
    layout = transaction.layout
    _revalidate_empty_target(targets.data, targets.data_identity)
    _revalidate_empty_target(targets.vault, targets.vault_identity)
    _write_restore_journals(transaction, "prepared")
    try:
        _atomic_rename(targets.data, layout.data_hold)
        _restore_publication_checkpoint("data_target_held")
        _write_restore_journals(transaction, "data_target_held")
        _atomic_rename(targets.vault, layout.vault_hold)
        _restore_publication_checkpoint("vault_target_held")
        _write_restore_journals(transaction, "vault_target_held")
        _atomic_rename(layout.data_stage, targets.data)
        _restore_publication_checkpoint("data_published")
        _write_restore_journals(transaction, "data_published")
        _atomic_rename(layout.vault_stage, targets.vault)
        _restore_publication_checkpoint("vault_published")
        _write_restore_journals(transaction, "vault_published")
        _write_restore_journals(transaction, "committed")
        _restore_publication_checkpoint("committed")
        _finalize_committed_restore(transaction)
    except (OSError, BackupError) as error:
        try:
            recovered_commit = _recover_restore_transaction(
                layout,
                archive_sha256=transaction.archive_sha256,
                manifest_sha256=transaction.manifest_sha256,
                records=records,
            )
            if recovered_commit is not None:
                _finalize_committed_restore(recovered_commit)
        except (OSError, BackupError) as rollback_error:
            raise RestoreTargetError(
                "Restore publication failed and recovery could not complete"
            ) from rollback_error
        raise RestoreTargetError("Restore could not be published atomically") from error


def restore_backup(
    archive_path: Path | str,
    data_path: Path | str,
    vault_path: Path | str,
    *,
    limits: BackupLimits = DEFAULT_LIMITS,
) -> RestoreResult:
    archive_path = _regular_file(archive_path, "Backup archive")
    if archive_path.stat().st_size > limits.max_archive_bytes:
        raise BackupIntegrityError("Backup archive exceeded its size limit")
    archive_size, archive_sha256 = _hash_path(archive_path, limits.max_archive_bytes)
    if archive_size <= 0:
        raise BackupIntegrityError("Backup archive was empty")
    layout = _restore_layout(data_path, vault_path)
    recovered_commit: _RestoreTransaction | None = None
    targets: _RestoreTargets | None = None
    data_stage: Path | None = None
    vault_stage: Path | None = None
    published = False
    records: list[_FileRecord] = []
    manifest_sha256 = ""
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_files + 1:
                raise BackupIntegrityError("Backup exceeded its file-count limit")
            by_path: dict[str, zipfile.ZipInfo] = {}
            folded_paths: set[str] = set()
            for info in infos:
                path = _validate_zip_info(info, limits)
                folded = path.casefold()
                if folded in folded_paths:
                    raise BackupPathError("Backup archive contained duplicate or colliding paths")
                folded_paths.add(folded)
                by_path[path] = info
                total_bytes += info.file_size
                if total_bytes > limits.max_total_bytes + limits.max_manifest_bytes:
                    raise BackupIntegrityError("Backup exceeded its total size limit")
            manifest_info = by_path.get(MANIFEST_ARCHIVE_PATH)
            if manifest_info is None:
                raise BackupIntegrityError("Backup manifest was missing")
            if manifest_info.file_size > limits.max_manifest_bytes:
                raise BackupIntegrityError("Backup manifest exceeded its size limit")
            with archive.open(manifest_info, "r") as manifest_stream:
                manifest_bytes = _read_bounded(manifest_stream, limits.max_manifest_bytes)
            records, manifest_sha256 = _parse_manifest(manifest_bytes)
            if len(records) > limits.max_files:
                raise BackupIntegrityError("Backup exceeded its file-count limit")
            declared = {item.path for item in records}
            archived = set(by_path) - {MANIFEST_ARCHIVE_PATH}
            if declared != archived:
                raise BackupIntegrityError("Backup contained missing or undeclared files")
            if sum(item.size for item in records) > limits.max_total_bytes:
                raise BackupIntegrityError("Backup exceeded its declared total size limit")
            for record in records:
                info = by_path[record.path]
                if info.file_size != record.size:
                    raise BackupIntegrityError("Archive metadata did not match the manifest")

            declared_total_bytes = sum(item.size for item in records)
            recovered_commit = _recover_restore_transaction(
                layout,
                archive_sha256=archive_sha256,
                manifest_sha256=manifest_sha256,
                records=records,
            )
            if recovered_commit is None:
                targets = _restore_targets(data_path, vault_path)
                reserved = (
                    layout.data_stage,
                    layout.vault_stage,
                    layout.data_hold,
                    layout.vault_hold,
                )
                if any(path.exists() or path.is_symlink() for path in reserved):
                    raise RestoreTargetError("Restore transaction paths were already occupied")
                layout.data_stage.mkdir()
                layout.vault_stage.mkdir()
                data_stage = layout.data_stage
                vault_stage = layout.vault_stage
                for record in records:
                    destination = (
                        data_stage / "careerops.db"
                        if record.path == DATABASE_ARCHIVE_PATH
                        else vault_stage / PurePosixPath(record.path).relative_to("vault")
                    )
                    _extract_member(archive, by_path[record.path], destination, record, limits)

        if recovered_commit is not None:
            if (
                recovered_commit.archive_sha256 != archive_sha256
                or recovered_commit.manifest_sha256 != manifest_sha256
                or recovered_commit.file_count != len(records)
                or recovered_commit.total_bytes != declared_total_bytes
            ):
                raise RestoreTargetError(
                    "Committed restore transaction did not match this backup"
                )
            _verify_committed_files(recovered_commit, records)
            database_stage = layout.data / "careerops.db"
            try:
                with closing(
                    sqlite3.connect(
                        f"{database_stage.as_uri()}?mode=ro&immutable=1",
                        uri=True,
                    )
                ) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise BackupIntegrityError(
                            "Restored database failed its integrity check"
                        )
            except BackupError:
                raise
            except sqlite3.Error as error:
                raise BackupIntegrityError("Restored database was not valid SQLite") from error
            _finalize_committed_restore(recovered_commit)
            published = True
            return RestoreResult(
                archive_path=archive_path,
                manifest_sha256=manifest_sha256,
                database_path=layout.data / "careerops.db",
                vault_path=layout.vault,
                file_count=len(records),
                total_bytes=declared_total_bytes,
            )

        if data_stage is None or vault_stage is None or targets is None:
            raise RestoreTargetError("Restore staging transaction was unavailable")
        database_stage = data_stage / "careerops.db"
        try:
            with closing(
                sqlite3.connect(
                    f"{database_stage.as_uri()}?mode=ro&immutable=1",
                    uri=True,
                )
            ) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise BackupIntegrityError("Restored database failed its integrity check")
        except BackupError:
            raise
        except sqlite3.Error as error:
            raise BackupIntegrityError("Restored database was not valid SQLite") from error
        transaction = _RestoreTransaction(
            layout=layout,
            archive_sha256=archive_sha256,
            manifest_sha256=manifest_sha256,
            file_count=len(records),
            total_bytes=declared_total_bytes,
            data_identity=targets.data_identity[:2],
            vault_identity=targets.vault_identity[:2],
        )
        _publish_restore(targets, transaction, records)
        published = True
        return RestoreResult(
            archive_path=archive_path,
            manifest_sha256=manifest_sha256,
            database_path=targets.data / "careerops.db",
            vault_path=targets.vault,
            file_count=len(records),
            total_bytes=declared_total_bytes,
        )
    except (zipfile.BadZipFile, EOFError, OSError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupIntegrityError("Backup archive was corrupt or unreadable") from error
    finally:
        if not published and not _restore_journals_exist(layout):
            _safe_remove_tree(data_stage, layout.data.parent)
            _safe_remove_tree(vault_stage, layout.vault.parent)


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="careerops-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--backup-root", required=True)
    backup_parser.add_argument("--data-path", required=True)
    backup_parser.add_argument("--vault-path", required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive-path", required=True)
    restore_parser.add_argument("--data-path", required=True)
    restore_parser.add_argument("--vault-path", required=True)
    arguments = parser.parse_args()
    if arguments.command == "backup":
        result: BackupResult | RestoreResult = create_backup(
            Path(arguments.data_path) / "careerops.db",
            arguments.vault_path,
            arguments.backup_root,
        )
    else:
        result = restore_backup(
            arguments.archive_path,
            arguments.data_path,
            arguments.vault_path,
        )
    print(json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in asdict(result).items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
