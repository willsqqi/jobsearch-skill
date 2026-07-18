from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


REDACTED = "[REDACTED]"
_VALUE_KEYS = {
    "access_token",
    "address",
    "answer",
    "authorization",
    "compensation",
    "company",
    "cv_path",
    "email",
    "first_name",
    "full_name",
    "home",
    "last_name",
    "location",
    "name",
    "path",
    "phone",
    "private_home",
    "role",
    "salary",
    "secret",
    "token",
    "url",
    "value",
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    field_id: str
    decision: str
    reason_code: str


class Redactor:
    """Recursively remove configured canaries and value-bearing fields."""

    def __init__(self, canaries: list[str] | tuple[str, ...]) -> None:
        self._canaries = tuple(canary for canary in canaries if canary)

    @staticmethod
    def _value_bearing(key: object) -> bool:
        normalized = str(key).casefold()
        return normalized in _VALUE_KEYS or normalized.endswith(("_path", "_token", "_secret"))

    def _redact_text(self, text: str) -> str:
        result = text
        for canary in self._canaries:
            result = result.replace(canary, REDACTED)
        return result

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                self._redact_text(key) if isinstance(key, str) else key: (
                    REDACTED if self._value_bearing(key) else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value


def format_diagnostic(
    diagnostic: Diagnostic,
    *,
    redactor: Redactor,
    context: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {"diagnostic": asdict(diagnostic)}
    if context is not None:
        payload["context"] = context
    return json.dumps(redactor.redact(payload), sort_keys=True)
