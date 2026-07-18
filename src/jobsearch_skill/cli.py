"""Minimal command-line interface for Jobsearch."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .cv import CVRegistry, CVSelection, CVService
from .errors import CVFactsError, JobsearchError, SchemaValidationError
from .forms import FormService
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
from .runs import RunState, RunStore
from .tracker import ApplicationTracker


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


def _state_payload(state: RunState) -> dict[str, object]:
    return dict(state.data)


def _success_envelope(command: str, result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "command": command,
        "result": result,
        "warnings": [],
    }


def _versioned_command(arguments: list[str]) -> str | None:
    allowed = {
        "cv": {"evidence", "facts", "prepare", "build"},
        "run": {"start", "analyze", "select-cv", "checkpoint", "show"},
        "application": {"record"},
        "form": {"plan"},
        "questions": {"match", "validate-reuse", "sync"},
    }
    for parent, children in allowed.items():
        if parent not in arguments:
            continue
        index = arguments.index(parent)
        child = arguments[index + 1] if index + 1 < len(arguments) else ""
        return f"{parent}.{child}" if child in children else parent
    return None


def _form_facts(store: SafeStore, runs: RunStore, state: RunState, home: Path) -> dict[str, object]:
    """Load exactly one regular run-owned CV-facts artifact bound to the selected CV."""

    selected = state.data.get("selected_cv")
    selected_name = selected.get("name") if isinstance(selected, dict) else None
    artifacts = state.data.get("generated_artifacts")
    pattern = re.compile(rf"^runs/{re.escape(state.run_id)}/cv-facts-([0-9a-f]{{64}})\.json$")
    candidates = [item for item in artifacts if isinstance(item, str) and pattern.fullmatch(item)] if isinstance(artifacts, list) else []
    if len(candidates) != 1 or not isinstance(selected_name, str):
        raise CVFactsError("cv_facts_binding: selected CV facts are unavailable", reason_code="cv_facts_binding")
    reference = candidates[0]
    filename = Path(reference).name
    path = runs.runs_dir / state.run_id / filename
    run_directory = runs.runs_dir / state.run_id
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != run_directory.resolve()
            or not path.resolve().is_relative_to(home.resolve())
        ):
            raise OSError("unsafe facts artifact")
    except OSError as error:
        raise CVFactsError("cv_facts_binding: selected CV facts are unavailable", reason_code="cv_facts_binding") from error
    facts = store.read_json(path, "cv-facts.v1")
    digest = pattern.fullmatch(reference)
    if (
        digest is None
        or facts.get("run_id") != state.run_id
        or facts.get("cv_name") != selected_name
        or facts.get("source_hash") != digest.group(1)
    ):
        raise CVFactsError("cv_facts_binding: selected CV facts are unavailable", reason_code="cv_facts_binding")
    return facts


def _form_summary(plan: dict[str, object]) -> dict[str, object]:
    action_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    decisions = plan.get("decisions")
    if isinstance(decisions, list):
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            action = decision.get("action")
            reason = decision.get("reason_code")
            if isinstance(action, str):
                action_counts[action] = action_counts.get(action, 0) + 1
            if isinstance(reason, str):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "page_id": plan["page_id"],
        "action_counts": action_counts,
        "reason_counts": reason_counts,
        "stop_before_submit": True,
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
    cv_evidence = cv_commands.add_parser("evidence")
    cv_evidence.add_argument("--run-id", required=True)
    cv_facts = cv_commands.add_parser("facts")
    cv_facts.add_argument("--run-id", required=True)
    cv_facts.add_argument("--input", required=True, type=Path)
    cv_prepare = cv_commands.add_parser("prepare")
    cv_prepare.add_argument("--run-id", required=True)
    cv_build = cv_commands.add_parser("build")
    cv_build.add_argument("--run-id", required=True)
    questions = commands.add_parser("questions")
    question_commands = questions.add_subparsers(dest="question_command")
    match = question_commands.add_parser("match")
    match.add_argument("--input", required=True, type=Path)
    reuse = question_commands.add_parser("validate-reuse")
    reuse.add_argument("--input", required=True, type=Path)
    sync = question_commands.add_parser("sync")
    sync.add_argument("--run-id", required=True)
    sync.add_argument("--input", required=True, type=Path)
    run = commands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command")
    run_start = run_commands.add_parser("start")
    run_start.add_argument("--job-context", required=True, type=Path)
    run_analyze = run_commands.add_parser("analyze")
    run_analyze.add_argument("--run-id", required=True)
    run_analyze.add_argument("--analysis", required=True, type=Path)
    run_select = run_commands.add_parser("select-cv")
    run_select.add_argument("--run-id", required=True)
    run_select.add_argument("--cv", required=True)
    run_checkpoint = run_commands.add_parser("checkpoint")
    run_checkpoint.add_argument("--run-id", required=True)
    run_checkpoint.add_argument("--input", required=True, type=Path)
    run_show = run_commands.add_parser("show")
    show_target = run_show.add_mutually_exclusive_group(required=True)
    show_target.add_argument("--run-id")
    show_target.add_argument("--latest-open", action="store_true")
    application = commands.add_parser("application")
    application_commands = application.add_subparsers(dest="application_command")
    application_record = application_commands.add_parser("record")
    application_record.add_argument("--run-id", required=True)
    application_record.add_argument("--confirmed-submitted", action="store_true")
    application_record.add_argument("--workday-id")
    form = commands.add_parser("form")
    form_commands = form.add_subparsers(dest="form_command")
    form_plan = form_commands.add_parser("plan")
    form_plan.add_argument("--run-id", required=True)
    form_plan.add_argument("--snapshot", required=True, type=Path)
    arguments = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(arguments)
    except _InvalidArguments:
        command = _versioned_command(arguments)
        if command is not None:
            _emit(
                {
                    "schema_version": 1,
                    "ok": False,
                    "command": command,
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
        run_id = getattr(args, "run_id", None)
        requires_run_id = (
            args.command == "application"
            or args.command == "form"
            or args.command == "cv" and args.cv_command in {"evidence", "facts", "prepare", "build"}
            or args.command == "run"
            and args.run_command != "start"
            and not getattr(args, "latest_open", False)
        )
        if requires_run_id and (not isinstance(run_id, str) or not _valid_run_id(run_id)):
            child = getattr(args, f"{args.command}_command", None)
            command = f"{args.command}.{child}" if child else str(args.command)
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
            if args.cv_command not in {"list", "resolve", "evidence", "facts", "prepare", "build"}:
                _emit({"reason_code": "invalid_arguments", "status": "error"})
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            store = SafeStore(registry, home / "backups")
            preferences = store.read_yaml(home / "preferences.yaml", "preferences.v1")
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
            if args.cv_command == "resolve":
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
            runs = RunStore(store, home / "runs")
            state = runs.require_open(args.run_id)
            selected = state.data.get("selected_cv")
            selected_name = selected.get("name") if isinstance(selected, dict) else None
            if not isinstance(selected_name, str):
                raise SchemaValidationError(
                    "cv_selection_mismatch: run CV selection is unavailable",
                    reason_code="cv_selection_mismatch",
                )
            selection = cvs.resolve(selected_name)
            service = CVService(home, store, runs, cvs)
            command = f"cv.{args.cv_command}"
            if args.cv_command == "evidence":
                evidence = service.evidence(args.run_id, selection)
                result = {
                    "evidence_ref": evidence.reference,
                    "source_count": len(evidence.source_hashes),
                    "status": "stored",
                }
            elif args.cv_command == "facts":
                facts = service.store_facts(args.run_id, selection, load_mapping(args.input))
                source_hash = facts.get("source_hash")
                facts_path = service.facts_path(args.run_id, str(source_hash))
                result = {
                    "facts_ref": facts_path.relative_to(home).as_posix(),
                    "education_count": len(facts.get("education", [])),
                    "employment_count": len(facts.get("employment", [])),
                    "project_count": len(facts.get("projects", [])),
                    "status": "stored",
                }
            elif args.cv_command == "prepare":
                prepared = service.prepare(args.run_id, selection)
                copied = prepared.manifest.get("copied_files")
                result = {
                    "manifest_ref": prepared.manifest_path.relative_to(home).as_posix(),
                    "copied_count": len(copied) if isinstance(copied, list) else 0,
                    "status": str(prepared.manifest.get("status")),
                }
            else:
                built = service.build(args.run_id)
                result = {
                    "pdf_ref": built.pdf_reference,
                    "page_count": built.page_count,
                    "verified": built.verified,
                    "status": "verified",
                }
            _emit(_success_envelope(command, result))
            return 0
        if args.command == "run":
            command = f"run.{args.run_command}"
            if args.run_command not in {"start", "analyze", "select-cv", "checkpoint", "show"}:
                _emit(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "command": "run",
                        "reason_code": "invalid_arguments",
                        "warnings": [],
                    }
                )
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            store = SafeStore(registry, home / "backups")
            runs = RunStore(store, home / "runs")
            if args.run_command == "start":
                state = runs.start(load_mapping(args.job_context))
            elif args.run_command == "analyze":
                state = runs.save_analysis(args.run_id, load_mapping(args.analysis))
            elif args.run_command == "select-cv":
                preferences = store.read_yaml(home / "preferences.yaml", "preferences.v1")
                selection = CVRegistry(preferences, home).resolve(args.cv)
                selected_path = selection.pdf if selection.pdf is not None else selection.tex
                if selected_path is None:
                    raise SchemaValidationError(
                        "cv_selection_invalid: selected CV has no usable file",
                        reason_code="cv_selection_invalid",
                    )
                state = runs.select_cv(
                    args.run_id,
                    {
                        "name": selection.name,
                        "path": str(selected_path),
                        "customized": selection.pdf is None,
                    },
                )
            elif args.run_command == "checkpoint":
                state = runs.checkpoint(args.run_id, load_mapping(args.input))
            elif args.latest_open:
                state = runs.latest_open()
            else:
                state = runs.get(args.run_id)
            _emit(_success_envelope(command, _state_payload(state)))
            return 0
        if args.command == "application":
            command = f"application.{args.application_command}"
            if args.application_command != "record":
                _emit(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "command": "application",
                        "reason_code": "invalid_arguments",
                        "warnings": [],
                    }
                )
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            store = SafeStore(registry, home / "backups")
            runs = RunStore(store, home / "runs")
            result = ApplicationTracker(store, runs, home / "applications.csv").record(
                args.run_id,
                confirmed_submitted=args.confirmed_submitted,
                workday_id=args.workday_id,
            )
            _emit(_success_envelope(command, result))
            return 0
        if args.command == "form":
            command = f"form.{args.form_command}"
            if args.form_command != "plan":
                _emit(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "command": "form",
                        "reason_code": "invalid_arguments",
                        "warnings": [],
                    }
                )
                return 2
            home = resolve_private_home(args.home, Path.cwd(), default_config_path())
            store = SafeStore(registry, home / "backups")
            runs = RunStore(store, home / "runs")
            state = runs.require_open(args.run_id)
            profile = store.read_yaml(home / "profile.yaml", "profile.v1")
            facts = _form_facts(store, runs, state, home)
            snapshot = load_mapping(args.snapshot)
            page_id = snapshot.get("page_id")
            if not isinstance(page_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", page_id):
                raise SchemaValidationError(
                    "page_id_invalid: page identifier is invalid", reason_code="page_id_invalid"
                )
            plan = FormService(
                registry,
                profile=profile,
                cv_facts=facts,
                questions=QuestionMemory(store, home / "questions.yaml"),
                job_context=state.data.get("job_context") if isinstance(state.data.get("job_context"), dict) else {},
                run_id=args.run_id,
            ).plan(snapshot)
            store.write_json(
                home / "runs" / args.run_id / f"fill-plan-{page_id}.json", plan, "fill-plan.v1"
            )
            _emit(_success_envelope(command, _form_summary(plan)))
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
            runs = RunStore(store, home / "runs")
            if args.question_command == "sync":
                runs.require_open(args.run_id)
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
            canonical_ids = [entry.canonical_id for entry in result.entries]
            runs.merge_learning_changes(args.run_id, canonical_ids)
            _emit(
                _question_envelope(
                    command,
                    {
                        "run_id": args.run_id,
                        "created": result.created,
                        "updated": result.updated,
                        "unchanged": result.unchanged,
                        "canonical_ids": canonical_ids,
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
        parent_command = getattr(args, "command", None)
        if parent_command in {"run", "application", "form"} or (
            parent_command == "cv"
            and getattr(args, "cv_command", None) in {"evidence", "facts", "prepare", "build"}
        ):
            child = getattr(args, f"{parent_command}_command", None)
            command = f"{parent_command}.{child}" if child else parent_command
            _emit(
                {
                    "schema_version": 1,
                    "ok": False,
                    "command": command,
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
