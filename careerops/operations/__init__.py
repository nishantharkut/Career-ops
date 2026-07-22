"""Operational backup, restore, and managed-runtime services."""

from importlib import import_module
from typing import Any


_BACKUP_EXPORTS = frozenset(
    {
        "BackupError",
        "BackupIntegrityError",
        "BackupLimits",
        "BackupPathError",
        "BackupResult",
        "RestoreResult",
        "RestoreTargetError",
        "create_backup",
        "restore_backup",
    }
)
_RUNTIME_EXPORTS = frozenset(
    {
        "ManagedOllamaRuntime",
        "ManagedOllamaRuntimeFactory",
        "ManagedRuntimeConfigurationError",
        "ManagedRuntimeError",
        "ManagedRuntimeIsolationError",
        "ManagedRuntimeLaunchError",
        "RuntimeLaunchRecord",
        "WindowsPowerShellOllamaLauncher",
    }
)
__all__ = sorted(_BACKUP_EXPORTS | _RUNTIME_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _BACKUP_EXPORTS:
        module = import_module("careerops.operations.backups")
    elif name in _RUNTIME_EXPORTS:
        module = import_module("careerops.operations.ollama_runtime")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value
