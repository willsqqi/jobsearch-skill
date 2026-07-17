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


class JobContextError(JobsearchError):
    """A supplied job context cannot be normalized safely."""

    exit_code = 3


class ProfileLookupError(JobsearchError):
    """A profile semantic key is unapproved or unavailable."""

    exit_code = 3


class CVSelectionError(JobsearchError):
    """A requested CV cannot be selected without exposing private details."""

    exit_code = 4


class CVFactsError(JobsearchError):
    """CV facts are stale, unsupported, or not bound to the selected source."""

    exit_code = 4


class CVBuildError(JobsearchError):
    """A CV artifact cannot be prepared or verified safely."""

    exit_code = 5

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        log_path=None,
        decision: str = "stop",
    ) -> None:
        super().__init__(message, reason_code=reason_code)
        self.log_path = log_path
        self.decision = decision


class RunNotFoundError(JobsearchError):
    """A requested resumable run is unavailable."""

    exit_code = 4


class AmbiguousRunError(JobsearchError):
    """Latest-open selection cannot safely choose among multiple runs."""

    exit_code = 4


class InvalidTransition(JobsearchError):
    """A requested run transition is not allowed."""

    exit_code = 6


class RunConflictError(JobsearchError):
    """A repeated run mutation conflicts with persisted state."""

    exit_code = 6


class SubmissionNotConfirmed(JobsearchError):
    """Tracker mutation lacks explicit post-submission confirmation."""

    exit_code = 3


class ApplicationConflictError(JobsearchError):
    """An application identity conflicts with persisted state."""

    exit_code = 6
