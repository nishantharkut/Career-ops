from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
from typing import Sequence


class StorageInitializationError(RuntimeError):
    """Raised when hosted storage cannot be initialized without ambiguity."""


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction_check = getattr(path, "is_junction", None)
    if callable(junction_check) and junction_check():
        return True
    value = path.lstat()
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _validated_directory(value: Path | str) -> Path:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise StorageInitializationError("Storage target must be an absolute path")
    candidate = Path(os.path.abspath(supplied))
    current = Path(candidate.anchor)
    try:
        for part in candidate.parts[1:]:
            current /= part
            if _is_link_or_reparse(current):
                raise StorageInitializationError(
                    "Storage target must not traverse links or reparse points"
                )
        metadata = candidate.lstat()
    except FileNotFoundError as error:
        raise StorageInitializationError("Storage target directory was missing") from error
    except OSError as error:
        raise StorageInitializationError("Storage target could not be inspected") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageInitializationError("Storage target was not a directory")
    return candidate


def initialize_storage(
    directories: Sequence[Path | str], *, uid: int, gid: int
) -> tuple[Path, ...]:
    if (
        type(uid) is not int
        or type(gid) is not int
        or not 0 <= uid < 2**31
        or not 0 <= gid < 2**31
    ):
        raise StorageInitializationError("Storage owner identity was invalid")
    if not 1 <= len(directories) <= 16:
        raise StorageInitializationError("Storage target set was invalid")
    validated = tuple(_validated_directory(path) for path in directories)
    identities = [os.path.normcase(str(path)) for path in validated]
    if len(set(identities)) != len(identities):
        raise StorageInitializationError("Storage targets must be distinct")
    chown = getattr(os, "chown", None)
    if not callable(chown):
        raise StorageInitializationError("Storage ownership is unsupported")
    try:
        for directory in validated:
            os.chmod(directory, 0o700, follow_symlinks=False)
            chown(directory, uid, gid, follow_symlinks=False)
    except OSError as error:
        raise StorageInitializationError(
            "Storage ownership could not be initialized"
        ) from error
    return validated


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="careerops-storage-init")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("directories", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    initialize_storage(arguments.directories, uid=arguments.uid, gid=arguments.gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
