from pathlib import Path

import pytest

from careerops.operations import storage_init


def test_storage_initializer_sets_only_explicit_directory_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directories = [tmp_path / name for name in ("data", "vault", "backups")]
    for directory in directories:
        directory.mkdir()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        storage_init.os,
        "chmod",
        lambda path, mode, *, follow_symlinks: calls.append(
            ("chmod", Path(path), mode, follow_symlinks)
        ),
    )
    monkeypatch.setattr(
        storage_init.os,
        "chown",
        lambda path, uid, gid, *, follow_symlinks: calls.append(
            ("chown", Path(path), uid, gid, follow_symlinks)
        ),
        raising=False,
    )

    result = storage_init.initialize_storage(directories, uid=1000, gid=1000)

    assert result == tuple(directory.resolve() for directory in directories)
    assert calls == [
        call
        for directory in directories
        for call in (
            ("chmod", directory.resolve(), 0o700, False),
            ("chown", directory.resolve(), 1000, 1000, False),
        )
    ]


def test_storage_initializer_rejects_links_and_duplicate_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable")
    monkeypatch.setattr(
        storage_init.os,
        "chown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe target must not be changed")
        ),
        raising=False,
    )

    with pytest.raises(storage_init.StorageInitializationError):
        storage_init.initialize_storage([link], uid=1000, gid=1000)
    with pytest.raises(storage_init.StorageInitializationError):
        storage_init.initialize_storage([target, target], uid=1000, gid=1000)


@pytest.mark.parametrize("uid,gid", [(-1, 1000), (1000, -1), (2**31, 1000)])
def test_storage_initializer_rejects_invalid_identity_before_changes(
    tmp_path: Path, uid: int, gid: int, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setattr(
        storage_init.os,
        "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid identity must not change storage")
        ),
    )

    with pytest.raises(storage_init.StorageInitializationError):
        storage_init.initialize_storage([directory], uid=uid, gid=gid)
