"""Minimal command-line interface for Jobsearch."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .cv import CVRegistry, CVSelection
from .errors import JobsearchError, SchemaValidationError
from .home import (
    bootstrap_private_home,
    configure_private_home,
    default_config_path,
    resolve_private_home,
    validate_private_documents,
)
from .schema import SchemaRegistry
from .questions import (
    QuestionMemory,
    QuestionMemoryError,
    load_mapping,
    question_query,
    validate_reuse,
)
from .storage import SafeStore


class _InvalidArguments(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _InvalidArguments from None


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _selection_payload(selection: CVSelection) -> dict[str, object]:
    return {
        "name": selection.name,
        "root": str(selection.root),
        "pdf": str(selection.pdf) if selection.pdf is not None else None,
        "tex": str(selection.tex) if selection.tex is not None else None,
        "assets": [str(asset) for asset in selection.assets],
    }


def _question_envelope(command: str, result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "command": command,
        "result": result,
        "warnings": [],
    }


def _valid_run_id(run_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id))


def main(argv: list[str] | None = None) -> int:
    """Run the Jobsearch command-line interface."""
    parser = _SafeArgumentParser(prog="jobsearch")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--version", action="store_true", help="Print the Jobsearch version.")
    commands = parser.add_subparsers(dest="command")
    configure = commands.add_parser("configure")
    configure.add_argument("private_home", type=Path)
    configure.add_argument("--config", type=Path)
    commands.add_parser("bootstrap")
    validate = commands.add_parser("validate")
    validate.add_argument("--ready", action="store_true")
    cv = commands.add_parser("cv")
    cv_commands = cv.add_subparsers(dest="cv_command")
    cv_commands.add_parser("list")
    resolve = cv_commands.add_parser("resolve")
    resolve.add_argument("--cv")
    resolve.add_argument("--for-customization", action="store_true")
    questions = commands.add_parser("questions")
    question_commands = questions.add_subparsers(dest="question_command")
    match = question_commands.add_parser("match")
    match.add_argument("--input", required=True, type=Path)
    reuse = question_commands.add_parser("validate-reuse")
    reuse.add_argument("--input", required=True, type=Path)
    sync = question_commands.add_parser("sync")
    sync.add_argument("--run-id", required=True)
    sync.add_argument("--input", required=True, type=Path)
    arguments = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(arguments)
    except _InvalidArguments:
        if "questions" in arguments:
            _emit(
                {
                    "schema_version": 1,
                    "ok": False,
                    "command": "questions",
                    "reason_code": "invalid_arguments",
                    "warnings": [],
                }
            )
            return 2
        _emit({"reason_code": "invalid_arguments", "status": "error"})
        return 2
    if args.version:
        _emit({"command": "version", "version": __version__})
        return 0
    registry = SchemaRegistry()
    try:
        if args.command == "configure":
            config_path = args.config or default_config_path()
            configure_private_home(args.private_home, config_path, Path.cwd())
            _emit({"command": "configure", "status": "ok"})
            return 0
        if args.command in {"bootstrap", "validate"}:
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            if args.command == "bootstrap":
                bootstrap_private_home(home, registry)
                _emit({"command": "bootstrap", "status": "ok"})
                return 0
            missing = validate_private_documents(home, registry, ready=args.ready)
            status = "incomplete" if missing else "ok"
            _emit({"command": "validate", "missing_fields": missing, "status": status})
            return 3 if missing else 0
        if args.command == "cv":
            if args.cv_command not in {"list", "resolve"}:
                _emit({"reason_code": "invalid_arguments", "status": "error"})
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            preferences = SafeStore(registry, home / "backups").read_yaml(
                home / "preferences.yaml", "preferences.v1"
            )
            cvs = CVRegistry(preferences, home)
            if args.cv_command == "list":
                _emit(
                    {
                        "command": "cv list",
                        "cvs": [_selection_payload(selection) for selection in cvs.list()],
                        "schema_version": 1,
                        "status": "ok",
                    }
                )
                return 0
            selection = cvs.resolve(args.cv, for_customization=args.for_customization)
            _emit(
                {
                    "command": "cv resolve",
                    "cv": _selection_payload(selection),
                    "schema_version": 1,
                    "status": "ok",
                }
            )
            return 0
        if args.command == "questions":
            command = f"questions.{args.question_command}"
            if args.question_command not in {"match", "validate-reuse", "sync"}:
                _emit(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "command": "questions",
                        "reason_code": "invalid_arguments",
                        "warnings": [],
                    }
                )
                return 2
            if args.question_command == "sync" and not _valid_run_id(args.run_id):
                _emit(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "command": command,
                        "reason_code": "invalid_run_id",
                        "warnings": [],
                    }
                )
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            store = SafeStore(registry, home / "backups")
            memory = QuestionMemory(store, home / "questions.yaml")
            input_value = load_mapping(args.input)
            if args.question_command == "match":
                result = memory.match(question_query(input_value))
                _emit(
                    _question_envelope(
                        command,
                        {
                            "kind": result.kind,
                            "reason_code": result.reason_code,
                            "canonical_source_id": result.canonical_id,
                            "candidates": list(result.candidates),
                        },
                    )
                )
                return 0
            if args.question_command == "validate-reuse":
                source_id = input_value.get("canonical_source_id")
                query_value = {
                    key: value
                    for key, value in input_value.items()
                    if key != "canonical_source_id"
                }
                query = question_query(query_value)
                try:
                    candidate = memory.get(str(source_id)) if isinstance(source_id, str) else None
                except QuestionMemoryError as error:
                    if error.reason_code != "canonical_source_missing":
                        raise
                    candidate = None
                if candidate is None:
                    allowed = False
                    reason_code = "canonical_source_missing"
                else:
                    validation = validate_reuse(
                        candidate,
                        {
                            "answer_type": query.answer_type,
                            "scope": query.scope,
                            "qualifiers": query.qualifiers,
                        },
                    )
                    allowed = validation.allowed
                    reason_code = validation.reason_code
                _emit(
                    _question_envelope(
                        command,
                        {
                            "allowed": allowed,
                            "canonical_source_id": source_id,
                            "reason_code": reason_code,
                        },
                    )
                )
                return 0
            registry.validate("reviewed-answers.v1", input_value)
            if input_value.get("run_id") != args.run_id:
                raise SchemaValidationError(
                    "run_id_mismatch: reviewed input belongs to another run",
                    reason_code="run_id_mismatch",
                )
            result = memory.sync(input_value)
            _emit(
                _question_envelope(
                    command,
                    {
                        "run_id": args.run_id,
                        "created": result.created,
                        "updated": result.updated,
                        "unchanged": result.unchanged,
                        "canonical_ids": [entry.canonical_id for entry in result.entries],
                    },
                )
            )
            return 0
        _emit({"reason_code": "command_missing", "status": "error"})
        return 2
    except JobsearchError as error:
        question_command = getattr(args, "question_command", None)
        if getattr(args, "command", None) == "questions" and question_command:
            _emit(
                {
                    "schema_version": 1,
                    "ok": False,
                    "command": f"questions.{question_command}",
                    "reason_code": error.reason_code,
                    "warnings": [],
                }
            )
            return error.exit_code
        payload: dict[str, object] = {"reason_code": error.reason_code, "status": "error"}
        field_path = getattr(error, "field_path", None)
        if isinstance(field_path, str):
            payload["field_path"] = field_path
        _emit(payload)
        return error.exit_code
