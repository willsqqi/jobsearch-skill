"""Minimal command-line interface for Jobsearch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .cv import CVRegistry, CVSelection
from .errors import JobsearchError
from .home import (
    bootstrap_private_home,
    configure_private_home,
    default_config_path,
    resolve_private_home,
    validate_private_documents,
)
from .schema import SchemaRegistry
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
    try:
        args = parser.parse_args(argv)
    except _InvalidArguments:
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
        _emit({"reason_code": "command_missing", "status": "error"})
        return 2
    except JobsearchError as error:
        payload: dict[str, object] = {"reason_code": error.reason_code, "status": "error"}
        field_path = getattr(error, "field_path", None)
        if isinstance(field_path, str):
            payload["field_path"] = field_path
        _emit(payload)
        return error.exit_code
