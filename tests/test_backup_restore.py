from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import shutil
import threading
import time
import zipfile
import warnings

import pytest

from careerops.core.repository import CareerOpsRepository
from careerops.operations import backups as backups_module
from careerops.operations.backups import (
    BackupIntegrityError,
    BackupLimits,
    BackupPathError,
    RestoreTargetError,
    create_backup,
    restore_backup,
)


def _make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "source-data" / "careerops.db", tmp_path / "source-vault")
    repo.initialize()
    return repo


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    mutate: callable,
) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        entries = [(item, archive.read(item)) for item in archive.infolist()]
    entries = mutate(entries)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, payload in entries:
            clone = zipfile.ZipInfo(info.filename)
            clone.external_attr = info.external_attr
            clone.create_system = info.create_system
            clone.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(clone, payload)


def _append_entry(
    source: Path,
    destination: Path,
    name: str,
    payload: bytes = b"unexpected",
    *,
    mode: int | None = None,
) -> None:
    def mutate(entries):
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_DEFLATED
        if mode is not None:
            info.create_system = 3
            info.external_attr = mode << 16
        return [*entries, (info, payload)]

    _rewrite_archive(source, destination, mutate=mutate)


def _empty_targets(tmp_path: Path) -> tuple[Path, Path]:
    data_path = tmp_path / "restored-data"
    vault_path = tmp_path / "restored-vault"
    data_path.mkdir(parents=True)
    vault_path.mkdir(parents=True)
    return data_path, vault_path


def test_backup_restores_database_and_vault_with_checksums(tmp_path: Path):
    repo = _make_repo(tmp_path)
    vault_file = repo.vault_path / "evidence" / "Priti profile.txt"
    vault_file.write_bytes(b"verified profile evidence")
    database_before = repo.database_path.read_bytes()
    vault_before = vault_file.read_bytes()

    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")

    with zipfile.ZipFile(backup.path) as archive:
        names = archive.namelist()
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert names == sorted(names, key=lambda value: (value != "manifest.json", value))
        assert manifest["version"] == "careerops_backup_v1"
        assert manifest["manifest_sha256"] == backup.manifest_sha256
        paths = [item["path"] for item in manifest["files"]]
        assert paths == sorted(paths)
        assert "data/careerops.db" in paths
        assert "vault/evidence/Priti profile.txt" in paths
        assert all(len(item["sha256"]) == 64 for item in manifest["files"])
        assert all(item["size"] >= 0 for item in manifest["files"])

    data_path, vault_path = _empty_targets(tmp_path)
    restored = restore_backup(backup.path, data_path, vault_path)

    assert restored.manifest_sha256 == backup.manifest_sha256
    assert restored.database_path == data_path / "careerops.db"
    assert restored.vault_path == vault_path
    assert (vault_path / "evidence" / "Priti profile.txt").read_bytes() == vault_before
    with sqlite3.connect(restored.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert repo.database_path.read_bytes() == database_before
    assert vault_file.read_bytes() == vault_before


def test_live_sqlite_backup_is_a_consistent_snapshot_during_writes(tmp_path: Path):
    repo = _make_repo(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE backup_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO backup_probe(value) VALUES (?)",
            [(f"seed-{index}",) for index in range(200)],
        )

    stop = threading.Event()

    def writer() -> None:
        with sqlite3.connect(repo.database_path, timeout=5) as connection:
            index = 0
            while not stop.is_set():
                connection.execute(
                    "INSERT INTO backup_probe(value) VALUES (?)", (f"live-{index}",)
                )
                connection.commit()
                index += 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with sqlite3.connect(repo.database_path) as connection:
                    if connection.execute("SELECT COUNT(*) FROM backup_probe").fetchone()[0] > 200:
                        break
                time.sleep(0.01)
            backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
        finally:
            stop.set()
            future.result(timeout=5)

    data_path, vault_path = _empty_targets(tmp_path)
    restore_backup(backup.path, data_path, vault_path)
    with sqlite3.connect(data_path / "careerops.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        count, maximum = connection.execute(
            "SELECT COUNT(*), MAX(id) FROM backup_probe"
        ).fetchone()
    assert count >= 200
    assert count == maximum


@pytest.mark.parametrize("target", ["database", "manifest", "missing", "extra"])
def test_restore_rejects_changed_missing_or_undeclared_archive_content(
    tmp_path: Path, target: str
):
    repo = _make_repo(tmp_path)
    (repo.vault_path / "evidence" / "proof.txt").write_bytes(b"proof")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    corrupt = tmp_path / f"{target}.zip"

    def mutate(entries):
        if target == "database":
            return [
                (item, payload + b"changed" if item.filename == "data/careerops.db" else payload)
                for item, payload in entries
            ]
        if target == "manifest":
            return [
                (item, payload.replace(b"careerops_backup_v1", b"careerops_backup_v9"))
                if item.filename == "manifest.json"
                else (item, payload)
                for item, payload in entries
            ]
        if target == "missing":
            return [(item, payload) for item, payload in entries if item.filename != "vault/evidence/proof.txt"]
        info = zipfile.ZipInfo("vault/evidence/extra.txt")
        info.compress_type = zipfile.ZIP_DEFLATED
        return [*entries, (info, b"not declared")]

    _rewrite_archive(backup.path, corrupt, mutate=mutate)
    data_path, vault_path = _empty_targets(tmp_path)

    with pytest.raises(BackupIntegrityError):
        restore_backup(corrupt, data_path, vault_path)

    assert list(data_path.iterdir()) == []
    assert list(vault_path.iterdir()) == []


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute.txt",
        "../parent.txt",
        "vault/../escape.txt",
        "C:/drive.txt",
        "vault/file.txt:stream",
        "vault\\backslash.txt",
        "vault//empty.txt",
        "vault/./dot.txt",
        "vault/CON.txt",
    ],
)
def test_restore_rejects_unsafe_or_windows_ambiguous_archive_names(
    tmp_path: Path, unsafe_name: str
):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    corrupt = tmp_path / "unsafe.zip"
    _append_entry(backup.path, corrupt, unsafe_name)
    if "\\" in unsafe_name:
        normalized = unsafe_name.replace("\\", "/").encode()
        raw = corrupt.read_bytes()
        assert normalized in raw
        corrupt.write_bytes(raw.replace(normalized, unsafe_name.encode()))
    data_path, vault_path = _empty_targets(tmp_path)

    with pytest.raises(BackupPathError):
        restore_backup(corrupt, data_path, vault_path)

    assert list(data_path.iterdir()) == []
    assert list(vault_path.iterdir()) == []


def test_restore_rejects_duplicate_and_case_colliding_paths(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proof = repo.vault_path / "evidence" / "Proof.txt"
    proof.write_bytes(b"proof")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")

    duplicate = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _append_entry(backup.path, duplicate, "vault/evidence/Proof.txt", b"other")
    case_collision = tmp_path / "case.zip"
    _append_entry(backup.path, case_collision, "VAULT/EVIDENCE/proof.TXT", b"other")

    for archive in (duplicate, case_collision):
        data_path, vault_path = _empty_targets(tmp_path / archive.stem)
        with pytest.raises(BackupPathError):
            restore_backup(archive, data_path, vault_path)


def test_restore_rejects_zip_symlink_and_non_regular_members(tmp_path: Path):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    corrupt = tmp_path / "symlink.zip"
    _append_entry(
        backup.path,
        corrupt,
        "vault/evidence/link",
        b"../../outside",
        mode=stat.S_IFLNK | 0o777,
    )
    data_path, vault_path = _empty_targets(tmp_path)

    with pytest.raises(BackupPathError):
        restore_backup(corrupt, data_path, vault_path)


def test_restore_rejects_archive_count_size_and_compression_bombs(tmp_path: Path):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    archives: list[tuple[Path, BackupLimits]] = []

    count_bomb = tmp_path / "count.zip"
    _append_entry(backup.path, count_bomb, "vault/extra.txt")
    archives.append((count_bomb, BackupLimits(max_files=2)))

    size_bomb = tmp_path / "size.zip"
    _append_entry(backup.path, size_bomb, "vault/large.txt", b"x" * 1024)
    archives.append((size_bomb, BackupLimits(max_total_bytes=512)))

    ratio_bomb = tmp_path / "ratio.zip"
    _append_entry(backup.path, ratio_bomb, "vault/ratio.txt", b"0" * (1024 * 1024))
    archives.append((ratio_bomb, BackupLimits(max_compression_ratio=5)))

    for archive, limits in archives:
        data_path, vault_path = _empty_targets(tmp_path / archive.stem)
        with pytest.raises(BackupIntegrityError):
            restore_backup(archive, data_path, vault_path, limits=limits)
        assert list(data_path.iterdir()) == []
        assert list(vault_path.iterdir()) == []


def test_backup_rejects_source_links_and_cleans_failed_temp_archive(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path)
    backup_root = tmp_path / "backups"
    linked_vault = tmp_path / "linked-vault"
    try:
        linked_vault.symlink_to(repo.vault_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(BackupPathError):
        create_backup(repo.database_path, linked_vault, backup_root)

    def fail_publish(source: Path, destination: Path) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(backups_module, "_atomic_rename", fail_publish)
    with pytest.raises(BackupPathError):
        create_backup(repo.database_path, repo.vault_path, backup_root)
    assert list(backup_root.iterdir()) == []


@pytest.mark.parametrize("source_kind", ["database", "vault"])
def test_backup_rejects_hard_linked_source_files(tmp_path: Path, source_kind: str):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside-source.bin"
    if source_kind == "database":
        alias = tmp_path / "database-alias.db"
        try:
            os.link(repo.database_path, alias)
        except OSError as exc:
            pytest.skip(f"Hard links are unavailable: {exc}")
        database_path = alias
    else:
        outside.write_bytes(b"outside private bytes")
        alias = repo.vault_path / "evidence" / "hard-linked.txt"
        try:
            os.link(outside, alias)
        except OSError as exc:
            pytest.skip(f"Hard links are unavailable: {exc}")
        database_path = repo.database_path

    with pytest.raises(BackupPathError, match="single-link"):
        create_backup(database_path, repo.vault_path, tmp_path / "backups")


def test_backup_applies_total_size_limit_to_database_only_archive(tmp_path: Path):
    repo = _make_repo(tmp_path)
    for item in repo.vault_path.rglob("*"):
        if item.is_file():
            item.unlink()
    limit = max(1, repo.database_path.stat().st_size - 1)

    with pytest.raises(BackupIntegrityError, match="total size"):
        create_backup(
            repo.database_path,
            repo.vault_path,
            tmp_path / "backups",
            limits=BackupLimits(max_total_bytes=limit),
        )


def test_restore_requires_explicit_existing_empty_non_link_targets(tmp_path: Path):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    missing = tmp_path / "missing"
    non_empty = tmp_path / "non-empty"
    non_empty.mkdir()
    (non_empty / "keep.txt").write_text("keep", encoding="utf-8")
    safe_empty = tmp_path / "safe-empty"
    safe_empty.mkdir()

    with pytest.raises(RestoreTargetError):
        restore_backup(backup.path, missing, safe_empty)
    with pytest.raises(RestoreTargetError):
        restore_backup(backup.path, non_empty, safe_empty)
    assert (non_empty / "keep.txt").read_text(encoding="utf-8") == "keep"

    linked = tmp_path / "linked"
    try:
        linked.symlink_to(safe_empty, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    with pytest.raises(RestoreTargetError):
        restore_backup(backup.path, linked, tmp_path / "another-missing")
    assert safe_empty.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_restore_rejects_windows_junction_target(tmp_path: Path):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("Junction creation is unavailable")
    vault_path = tmp_path / "empty-vault"
    vault_path.mkdir()
    try:
        with pytest.raises(RestoreTargetError):
            restore_backup(backup.path, junction, vault_path)
        assert target.exists()
    finally:
        os.rmdir(junction)


def test_second_target_publish_failure_rolls_back_to_both_empty_targets(
    tmp_path: Path, monkeypatch
):
    repo = _make_repo(tmp_path)
    (repo.vault_path / "evidence" / "proof.txt").write_bytes(b"proof")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    data_path, vault_path = _empty_targets(tmp_path)
    real_rename = backups_module._atomic_rename
    staged_publications = 0

    def fail_second_staged_publish(source: Path, destination: Path) -> None:
        nonlocal staged_publications
        if source.name.startswith(".careerops-restore-") and destination in {data_path, vault_path}:
            staged_publications += 1
            if staged_publications == 2:
                raise OSError("injected second publication failure")
        real_rename(source, destination)

    monkeypatch.setattr(backups_module, "_atomic_rename", fail_second_staged_publish)

    with pytest.raises(RestoreTargetError):
        restore_backup(backup.path, data_path, vault_path)

    assert data_path.is_dir() and list(data_path.iterdir()) == []
    assert vault_path.is_dir() and list(vault_path.iterdir()) == []
    assert not list(tmp_path.glob(".careerops-restore-*"))


class SimulatedRestoreCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "crash_boundary",
    [
        "data_target_held",
        "vault_target_held",
        "data_published",
        "vault_published",
        "committed",
    ],
)
def test_restore_recovers_transaction_after_crash_at_every_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: str,
):
    repo = _make_repo(tmp_path)
    proof = repo.vault_path / "evidence" / "proof.txt"
    proof.write_bytes(b"transaction-proof")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    data_path, vault_path = _empty_targets(tmp_path)

    def crash(boundary: str) -> None:
        if boundary == crash_boundary:
            raise SimulatedRestoreCrash(boundary)

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        crash,
        raising=False,
    )
    with pytest.raises(SimulatedRestoreCrash, match=crash_boundary):
        restore_backup(backup.path, data_path, vault_path)

    journals = list(tmp_path.glob(".careerops-restore-transaction-*.json"))
    assert journals, "an interrupted publication must remain recoverable"

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        lambda _boundary: None,
        raising=False,
    )
    result = restore_backup(backup.path, data_path, vault_path)

    assert result.database_path == data_path / "careerops.db"
    assert (vault_path / "evidence" / "proof.txt").read_bytes() == b"transaction-proof"
    assert not list(tmp_path.glob(".careerops-restore-transaction-*.json"))
    assert not list(tmp_path.glob(".careerops-empty-*"))


def test_restore_preserves_journal_when_rollback_fails_then_recovers_on_next_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    (repo.vault_path / "proof.txt").write_bytes(b"recoverable")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    data_path, vault_path = _empty_targets(tmp_path)

    def crash_after_data_publish(boundary: str) -> None:
        if boundary == "data_published":
            raise SimulatedRestoreCrash(boundary)

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        crash_after_data_publish,
        raising=False,
    )
    with pytest.raises(SimulatedRestoreCrash):
        restore_backup(backup.path, data_path, vault_path)

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        lambda _boundary: None,
        raising=False,
    )
    real_rename = backups_module._atomic_rename
    failed_once = False

    def fail_first_recovery_rename(source: Path, destination: Path) -> None:
        nonlocal failed_once
        if (
            not failed_once
            and source == data_path
            and destination.name.startswith(".careerops-restore-data-")
        ):
            failed_once = True
            raise OSError("injected rollback interruption")
        real_rename(source, destination)

    monkeypatch.setattr(backups_module, "_atomic_rename", fail_first_recovery_rename)

    with pytest.raises(RestoreTargetError, match="recovery"):
        restore_backup(backup.path, data_path, vault_path)

    assert list(tmp_path.glob(".careerops-restore-transaction-*.json"))

    result = restore_backup(backup.path, data_path, vault_path)

    assert result.database_path.is_file()
    assert (vault_path / "proof.txt").read_bytes() == b"recoverable"
    assert not list(tmp_path.glob(".careerops-restore-transaction-*.json"))


def test_restore_recovers_cross_parent_publication_from_single_authoritative_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    (repo.vault_path / "cross-parent.txt").write_bytes(b"cross-parent")
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    data_parent = tmp_path / "data-parent"
    vault_parent = tmp_path / "vault-parent"
    data_parent.mkdir()
    vault_parent.mkdir()
    data_path = data_parent / "data"
    vault_path = vault_parent / "vault"
    data_path.mkdir()
    vault_path.mkdir()

    def crash_after_vault_publish(boundary: str) -> None:
        if boundary == "vault_published":
            raise SimulatedRestoreCrash(boundary)

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        crash_after_vault_publish,
    )
    with pytest.raises(SimulatedRestoreCrash):
        restore_backup(backup.path, data_path, vault_path)

    journals = list(data_parent.glob(".careerops-restore-transaction-*.json"))
    assert len(journals) == 1
    assert not list(vault_parent.glob(".careerops-restore-transaction-*.json"))
    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        lambda _boundary: None,
    )

    restore_backup(backup.path, data_path, vault_path)

    assert (vault_path / "cross-parent.txt").read_bytes() == b"cross-parent"
    assert not list(data_parent.glob(".careerops-restore-transaction-*.json"))


def test_restore_recovery_never_deletes_a_changed_partially_published_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    backup = create_backup(repo.database_path, repo.vault_path, tmp_path / "backups")
    data_path, vault_path = _empty_targets(tmp_path)

    def crash_after_data_publish(boundary: str) -> None:
        if boundary == "data_published":
            raise SimulatedRestoreCrash(boundary)

    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        crash_after_data_publish,
    )
    with pytest.raises(SimulatedRestoreCrash):
        restore_backup(backup.path, data_path, vault_path)
    changed_file = data_path / "must-not-be-deleted.txt"
    changed_file.write_bytes(b"caller-owned-after-crash")
    monkeypatch.setattr(
        backups_module,
        "_restore_publication_checkpoint",
        lambda _boundary: None,
    )

    with pytest.raises(RestoreTargetError, match="changed"):
        restore_backup(backup.path, data_path, vault_path)

    assert changed_file.read_bytes() == b"caller-owned-after-crash"
    assert list(tmp_path.glob(".careerops-restore-transaction-*.json"))


def test_backup_and_restore_scripts_require_all_explicit_paths():
    root = Path(__file__).resolve().parents[1]
    backup_script = (root / "scripts" / "backup.ps1").read_text(encoding="utf-8")
    restore_script = (root / "scripts" / "restore.ps1").read_text(encoding="utf-8")

    for script in (backup_script, restore_script):
        assert "Mandatory = $true" in script
        assert "[System.IO.Path]::GetFullPath" in script
        assert "ReparsePoint" in script
        assert "Remove-Item" not in script
        assert "Invoke-Expression" not in script
    assert "BackupRoot" in backup_script
    assert "DataPath" in backup_script
    assert "VaultPath" in backup_script
    assert "ArchivePath" in restore_script
    assert "DataPath" in restore_script
    assert "VaultPath" in restore_script


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell is unavailable")
def test_powershell_wrappers_complete_a_verified_backup_restore_smoke(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proof = repo.vault_path / "evidence" / "smoke.txt"
    proof.write_bytes(b"verified-smoke")
    data_path = repo.database_path.parent
    backup_root = tmp_path / "backups"
    restored_data, restored_vault = _empty_targets(tmp_path / "restored")
    root = Path(__file__).resolve().parents[1]
    database_before = repo.database_path.read_bytes()

    backup_process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "backup.ps1"),
            "-BackupRoot",
            str(backup_root),
            "-DataPath",
            str(data_path),
            "-VaultPath",
            str(repo.vault_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup_process.returncode == 0, backup_process.stderr
    assert backup_process.stderr == ""
    backup_result = json.loads(backup_process.stdout)

    restore_process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "restore.ps1"),
            "-ArchivePath",
            backup_result["path"],
            "-DataPath",
            str(restored_data),
            "-VaultPath",
            str(restored_vault),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restore_process.returncode == 0, restore_process.stderr
    assert restore_process.stderr == ""
    restore_result = json.loads(restore_process.stdout)

    assert restore_result["manifest_sha256"] == backup_result["manifest_sha256"]
    assert repo.database_path.read_bytes() == database_before
    assert (restored_vault / "evidence" / "smoke.txt").read_bytes() == b"verified-smoke"
    with sqlite3.connect(restored_data / "careerops.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
