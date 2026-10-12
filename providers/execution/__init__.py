"""Audited execution boundaries for external and local side effects."""

from importlib import import_module

_EXPORT_MODULES = {
    "AmbiguousHTTPReplayError": "http",
    "AuditedAsyncHTTPClient": "http",
    "AuditedFilesystem": "filesystem",
    "AuditedHTTPClient": "http",
    "AuditedSubprocessRunner": "subprocess",
    "FilesystemAuditError": "filesystem",
    "ProcessResult": "subprocess",
    "RootedPath": "filesystem",
    "durable_filesystem_emitter": "filesystem",
}

__all__ = [
    "AmbiguousHTTPReplayError",
    "AuditedAsyncHTTPClient",
    "AuditedFilesystem",
    "AuditedHTTPClient",
    "AuditedSubprocessRunner",
    "FilesystemAuditError",
    "ProcessResult",
    "RootedPath",
    "durable_filesystem_emitter",
]


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORT_MODULES))
