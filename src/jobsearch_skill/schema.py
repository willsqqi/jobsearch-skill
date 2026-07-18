from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from jobsearch_skill.errors import SchemaValidationError


def _field_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


class SchemaRegistry:
    """Load and validate the public, versioned JSON contracts."""

    def __init__(self, schema_dir: Path | None = None) -> None:
        self._schema_dir = schema_dir.resolve() if schema_dir is not None else None
        self._validators: dict[str, Draft202012Validator] = {}

    def _read_schema(self, contract: str) -> dict[str, object]:
        if not contract or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in contract):
            raise SchemaValidationError(
                "schema_not_found: the requested contract is not available",
                reason_code="schema_not_found",
            )
        filename = f"{contract}.schema.json"
        try:
            if self._schema_dir is not None:
                text = (self._schema_dir / filename).read_text(encoding="utf-8")
            else:
                text = (
                    resources.files("jobsearch_skill.data.schemas")
                    .joinpath(filename)
                    .read_text(encoding="utf-8")
                )
        except (FileNotFoundError, ModuleNotFoundError, TypeError) as error:
            raise SchemaValidationError(
                "schema_not_found: the requested contract is not available",
                reason_code="schema_not_found",
            ) from error
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise SchemaValidationError(
                "schema_invalid: the requested contract is not valid JSON",
                reason_code="schema_invalid",
            ) from error
        if not isinstance(value, dict):
            raise SchemaValidationError(
                "schema_invalid: the requested contract is not an object",
                reason_code="schema_invalid",
            )
        return value

    def _validator(self, contract: str) -> Draft202012Validator:
        validator = self._validators.get(contract)
        if validator is not None:
            return validator
        schema = self._read_schema(contract)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise SchemaValidationError(
                "schema_invalid: the requested contract is not a valid draft 2020-12 schema",
                reason_code="schema_invalid",
                field_path=_field_path(error.path),
            ) from error
        validator = Draft202012Validator(schema)
        self._validators[contract] = validator
        return validator

    def validate(self, contract: str, value: object) -> None:
        errors = list(self._validator(contract).iter_errors(value))
        if not errors:
            return
        error = max(
            errors,
            key=lambda candidate: (
                len(candidate.absolute_path),
                tuple(str(part) for part in candidate.absolute_path),
            ),
        )
        field_path = _field_path(error.absolute_path)
        raise SchemaValidationError(
            f"schema_validation: {field_path} violates {error.validator}",
            reason_code="schema_validation",
            field_path=field_path,
        )


def load_yaml_document(path: Path) -> dict[str, object]:
    """Load one YAML mapping without exposing its values in diagnostics."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SchemaValidationError(
            "document_parse: unable to read a valid YAML document",
            reason_code="document_parse",
        ) from error
    if not isinstance(value, dict):
        raise SchemaValidationError(
            "document_type: the YAML document must be an object",
            reason_code="document_type",
        )
    return value
