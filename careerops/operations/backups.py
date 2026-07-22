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


def _owned_empty_placeholder(target: Path) -> Path:
    return target.parent / f".careerops-empty-{target.name}-{uuid4().hex}"


def _safe_remove_tree(path: Path | None, parent: Path) -> None:
    if path is None or not path.exists():
        return
    if path.parent != parent or not path.name.startswith(".careerops-restore-") or _is_link_or_reparse(path):
        raise RestoreTargetError("Refusing to clean an unowned restore staging path")
    shutil.rmtree(path)


def _publish_restore(
    targets: _RestoreTargets,
    data_stage: Path,
    vault_stage: Path,
) -> None:
    _revalidate_empty_target(targets.data, targets.data_identity)
    _revalidate_empty_target(targets.vault, targets.vault_identity)
    data_hold = _owned_empty_placeholder(targets.data)
    vault_hold = _owned_empty_placeholder(targets.vault)
    data_published = False
    vault_published = False
    try:
        _atomic_rename(targets.data, data_hold)
        _atomic_rename(targets.vault, vault_hold)
        _atomic_rename(data_stage, targets.data)
        data_published = True
        _atomic_rename(vault_stage, targets.vault)
        vault_published = True
        os.rmdir(data_hold)
        os.rmdir(vault_hold)
        _fsync_directory(targets.data.parent)
        if targets.vault.parent != targets.data.parent:
            _fsync_directory(targets.vault.parent)
    except (OSError, BackupError) as error:
        rollback_error: OSError | BackupError | None = None
        try:
            if vault_published and targets.vault.exists():
                _atomic_rename(targets.vault, vault_stage)
            if data_published and targets.data.exists():
                _atomic_rename(targets.data, data_stage)
            if vault_hold.exists() and not targets.vault.exists():
                _atomic_rename(vault_hold, targets.vault)
            if data_hold.exists() and not targets.data.exists():
                _atomic_rename(data_hold, targets.data)
        except (OSError, BackupError) as rollback:
            rollback_error = rollback
        if rollback_error is not None:
            raise RestoreTargetError("Restore publication failed and rollback could not complete") from rollback_error
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
    targets = _restore_targets(data_path, vault_path)
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

            data_stage = Path(tempfile.mkdtemp(prefix=".careerops-restore-data-", dir=targets.data.parent))
            vault_stage = Path(tempfile.mkdtemp(prefix=".careerops-restore-vault-", dir=targets.vault.parent))
            for record in records:
                destination = (
                    data_stage / "careerops.db"
                    if record.path == DATABASE_ARCHIVE_PATH
                    else vault_stage / PurePosixPath(record.path).relative_to("vault")
                )
                _extract_member(archive, by_path[record.path], destination, record, limits)

        database_stage = data_stage / "careerops.db"
        try:
            with closing(
                sqlite3.connect(f"{database_stage.as_uri()}?mode=ro", uri=True)
            ) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise BackupIntegrityError("Restored database failed its integrity check")
        except BackupError:
            raise
        except sqlite3.Error as error:
            raise BackupIntegrityError("Restored database was not valid SQLite") from error
        _publish_restore(targets, data_stage, vault_stage)
        published = True
        return RestoreResult(
            archive_path=archive_path,
            manifest_sha256=manifest_sha256,
            database_path=targets.data / "careerops.db",
            vault_path=targets.vault,
            file_count=len(records),
            total_bytes=sum(item.size for item in records),
        )
    except (zipfile.BadZipFile, EOFError, OSError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupIntegrityError("Backup archive was corrupt or unreadable") from error
    finally:
        if not published:
            _safe_remove_tree(data_stage, targets.data.parent)
            _safe_remove_tree(vault_stage, targets.vault.parent)


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
