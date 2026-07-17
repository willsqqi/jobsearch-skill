from __future__ import annotations


class JobsearchError(Exception):
    """Base class for safe, user-facing jobsearch errors."""

    exit_code = 1

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ConfigurationError(JobsearchError):
    """Private configuration is missing or unsafe."""

    exit_code = 3


class SchemaValidationError(JobsearchError):
    """A versioned document or schema failed safe validation."""

    exit_code = 3

    def __init__(self, message: str, *, reason_code: str, field_path: str = "$") -> None:
        super().__init__(message, reason_code=reason_code)
        self.field_path = field_path


class StorageError(JobsearchError):
    """A private storage operation failed without exposing stored values."""

    exit_code = 6


class StorageValidationError(SchemaValidationError):
    """A storage write failed validation and is safe to catch as schema failure."""

    exit_code = 6
