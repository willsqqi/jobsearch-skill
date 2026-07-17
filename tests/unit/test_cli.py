import csv
import json
import os
from pathlib import Path

import pytest
import yaml

from jobsearch_skill.cli import main
from jobsearch_skill.errors import StorageError
from jobsearch_skill.storage import SafeStore
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.schema import SchemaRegistry


def test_version_envelope(capsys) -> None:
    assert main(["--version"]) == 0
    output = capsys.readouterr().out
    assert '"command": "version"' in output
    assert '"version": "0.1.0"' in output


def test_configure_bootstrap_and_validate_emit_one_json_envelope(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    home = tmp_path / "private" / ".jobsearch"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    pointer = config_dir / "config.toml"

    assert main(["configure", str(home), "--config", str(pointer)]) == 0
    configured = capsys.readouterr()
    assert configured.err == ""
    assert json.loads(configured.out) == {"command": "configure", "status": "ok"}

    assert main(["--home", str(home), "bootstrap"]) == 0
    bootstrapped = capsys.readouterr()
    assert bootstrapped.err == ""
    assert json.loads(bootstrapped.out) == {"command": "bootstrap", "status": "ok"}

    assert main(["--home", str(home), "validate"]) == 0
    validated = capsys.readouterr()
    assert validated.err == ""
    assert json.loads(validated.out) == {
        "command": "validate",
        "missing_fields": [],
        "status": "ok",
    }


def test_validate_ready_reports_paths_without_private_values(tmp_path: Path, capsys) -> None:
    home = tmp_path / "PRIVATE_HOME_CANARY" / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()

    result = main(["--home", str(home), "validate", "--ready"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert result != 0
    assert captured.err == ""
    assert payload["status"] == "incomplete"
    assert payload["missing_fields"]
    assert "PRIVATE_HOME_CANARY" not in captured.out


def test_cli_storage_error_is_single_safe_json_envelope(tmp_path: Path, capsys) -> None:
    invalid_home = tmp_path / "not-a-directory"
    invalid_home.write_text("PRIVATE_STORAGE_CANARY")

    assert main(["--home", str(invalid_home), "bootstrap"]) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload == {
        "reason_code": "home_type",
        "status": "error",
    }
    assert "PRIVATE_STORAGE_CANARY" not in captured.out


def test_cli_malformed_private_csv_has_safe_storage_exit_code(tmp_path: Path, capsys) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    applications = home / "applications.csv"
    applications.write_text(
        applications.read_text(encoding="utf-8") + "PRIVATE_CSV_CANARY\n",
        encoding="utf-8",
    )

    assert main(["--home", str(home), "validate"]) == 6
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "error"
    assert "PRIVATE_CSV_CANARY" not in captured.out


def test_cli_usage_error_is_one_json_envelope(capsys) -> None:
    assert main(["configure"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "reason_code": "invalid_arguments",
        "status": "error",
    }


@pytest.mark.parametrize(
    ("family", "canary"),
    [
        ("run", "/PRIVATE_RUN_PATH_CANARY"),
        ("application", "PRIVATE_APPLICATION_NAME_CANARY"),
        ("questions", "--PRIVATE_QUESTION_TOKEN_CANARY"),
    ],
)
def test_unknown_nested_commands_emit_fixed_value_free_envelope(
    capsys: pytest.CaptureFixture[str], family: str, canary: str
) -> None:
    assert main([family, canary]) == 2
    captured = capsys.readouterr()

    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "ok": False,
        "command": family,
        "reason_code": "invalid_arguments",
        "warnings": [],
    }
    assert canary not in captured.out


@pytest.mark.parametrize("family", ["run", "application", "questions"])
def test_missing_nested_command_uses_fixed_parent_id(
    capsys: pytest.CaptureFixture[str], family: str
) -> None:
    assert main([family]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["command"] == family


def test_cli_cv_resolve_returns_only_requested_selection(tmp_path: Path, capsys) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    (home / "cvs" / "swe").mkdir(parents=True)
    (home / "cvs" / "swe" / "resume.tex").write_text("\\documentclass{article}")
    (home / "cvs" / "swe" / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (home / "preferences.yaml").write_text(
        """schema_version: 1
default_cv: SWE
cvs:
  - name: SWE
    root: cvs/swe
    tex: resume.tex
    pdf: resume.pdf
    assets: []
  - name: DE
    root: cvs/de
    pdf: resume.pdf
    assets: []
browser: builtin
platform_priority: [workday]
submission_mode: manual
generated_artifacts:
  naming_template: "{company}-{role}-{run_id}"
  retain_days: 30
  retain_failed_builds: true
  retain_build_logs: true
""",
        encoding="utf-8",
    )

    assert main(["--home", str(home), "cv", "resolve", "--cv", "swe", "--for-customization"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert captured.err == ""
    assert payload["schema_version"] == 1
    assert payload["cv"]["name"] == "SWE"
    assert payload["cv"]["tex"].endswith("cvs/swe/resume.tex")
    assert "DE" not in captured.out


def test_cli_cv_selection_error_is_a_safe_single_envelope(tmp_path: Path, capsys) -> None:
    home = tmp_path / "PRIVATE_CV_HOME_CANARY" / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()

    assert main(["--home", str(home), "cv", "resolve", "--cv", "missing.pdf"]) == 4
    captured = capsys.readouterr()

    assert captured.err == ""
    assert json.loads(captured.out) == {
        "reason_code": "cv_path_unavailable",
        "status": "error",
    }
    assert "PRIVATE_CV_HOME_CANARY" not in captured.out
    assert "missing.pdf" not in captured.out


def test_cli_csv_open_failure_is_safe_storage_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    applications = home / "applications.csv"
    original_open = Path.open

    def fail_applications_open(path: Path, *args, **kwargs):
        if path == applications:
            raise OSError("PRIVATE_OPEN_CANARY /private/applications.csv")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_applications_open)

    assert main(["--home", str(home), "validate"]) == 6
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "field_path": "$",
        "reason_code": "storage_read",
        "status": "error",
    }
    assert "PRIVATE_OPEN_CANARY" not in captured.out
    assert "/private/applications.csv" not in captured.out


def test_cli_csv_parser_failure_is_safe_storage_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()

    def fail_reader(*args, **kwargs):
        raise csv.Error("PRIVATE_PARSER_CANARY")

    monkeypatch.setattr("jobsearch_skill.home.csv.DictReader", fail_reader)

    assert main(["--home", str(home), "validate"]) == 6
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "field_path": "$",
        "reason_code": "storage_read",
        "status": "error",
    }
    assert "PRIVATE_PARSER_CANARY" not in captured.out


def test_configure_rejects_symlink_pointer_without_touching_target(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    private_home = tmp_path / "PRIVATE_HOME_PATH_CANARY" / ".jobsearch"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    unrelated = tmp_path / "unrelated-target.toml"
    original = b"PRIVATE_SYMLINK_TARGET_CANARY\n"
    unrelated.write_bytes(original)
    pointer = config_dir / "config.toml"
    pointer.symlink_to(unrelated)

    result = main(["configure", str(private_home), "--config", str(pointer)])
    captured = capsys.readouterr()

    assert result == 3
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "reason_code": "pointer_symlink",
        "status": "error",
    }
    assert unrelated.read_bytes() == original
    assert pointer.is_symlink()
    assert "PRIVATE_HOME_PATH_CANARY" not in captured.out
    assert "PRIVATE_SYMLINK_TARGET_CANARY" not in captured.out
    assert str(unrelated) not in captured.out


def test_cli_preserves_primary_storage_error_when_temp_cleanup_fails(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    private_home = tmp_path / "PRIVATE_CLEANUP_HOME_CANARY" / ".jobsearch"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    pointer = config_dir / "config.toml"
    assert main(["configure", str(private_home), "--config", str(pointer)]) == 0
    capsys.readouterr()
    original = pointer.read_bytes()
    original_unlink = Path.unlink

    def fail_backup(*args, **kwargs):
        raise StorageError(
            "storage_backup: unable to create private backup",
            reason_code="storage_backup",
        )

    def fail_temp_unlink(path: Path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError("PRIVATE_UNLINK_CANARY /private/temp-file")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(SafeStore, "_create_backup", fail_backup)
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)

    assert main(["configure", str(private_home), "--config", str(pointer)]) == 6
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "reason_code": "storage_backup",
        "status": "error",
    }
    assert pointer.read_bytes() == original
    assert "PRIVATE_UNLINK_CANARY" not in captured.out
    assert "/private/temp-file" not in captured.out


def _analysis_document(run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "job_fingerprint": fingerprint,
        "role_summary": "Synthetic role",
        "required_qualifications": [],
        "preferred_qualifications": [],
        "strong_matches": [],
        "partial_matches": [],
        "material_gaps": [],
        "cv_comparison": [],
        "recommended_cv": "SWE",
        "recommendation_rationale": "Synthetic rationale",
        "customization": {"worthwhile": False, "rationale": "Not needed"},
        "evidence_references": [],
    }


def _write_yaml(path: Path, value: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_cli_run_lifecycle_emits_versioned_envelopes_and_owned_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    context = make_job_context(
        job_url="https://example.invalid/jobs/7",
        company="Synthetic Systems",
        role="Backend Engineer",
        description="Build APIs",
    )
    job_path = tmp_path / "job.yaml"
    _write_yaml(job_path, context)

    assert main(["--home", str(home), "run", "start", "--job-context", str(job_path)]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["schema_version"] == 1
    assert started["ok"] is True
    assert started["command"] == "run.start"
    run_id = started["result"]["run_id"]

    analysis_path = tmp_path / "transport-analysis.yaml"
    _write_yaml(analysis_path, _analysis_document(run_id, str(context["job_fingerprint"])))
    assert main(
        [
            "--home",
            str(home),
            "run",
            "analyze",
            "--run-id",
            run_id,
            "--analysis",
            str(analysis_path),
        ]
    ) == 0
    analyzed = json.loads(capsys.readouterr().out)
    assert analyzed["result"]["phase"] == "analyzed"
    assert "transport-analysis" not in analyzed["result"]["analysis_ref"]

    cv = tmp_path / "synthetic.pdf"
    cv.write_bytes(b"%PDF-1.4 synthetic")
    assert main(
        ["--home", str(home), "run", "select-cv", "--run-id", run_id, "--cv", str(cv)]
    ) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["result"]["phase"] == "cv_selected"

    checkpoint = tmp_path / "checkpoint.yaml"
    _write_yaml(checkpoint, {"target_phase": "cv_ready"})
    assert main(
        ["--home", str(home), "run", "checkpoint", "--run-id", run_id, "--input", str(checkpoint)]
    ) == 0
    checkpointed = json.loads(capsys.readouterr().out)
    assert checkpointed["result"]["phase"] == "cv_ready"

    assert main(["--home", str(home), "run", "show", "--latest-open"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["result"]["run_id"] == run_id
    assert shown["result"]["phase"] == "cv_ready"


def test_cli_application_requires_confirmation_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    context = make_job_context(
        job_url="https://example.invalid/jobs/7",
        description="Build APIs",
    )
    registry = SchemaRegistry()
    store = SafeStore(registry, home / "backups")
    from jobsearch_skill.runs import RunStore

    runs = RunStore(store, home / "runs")
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis_document(run.run_id, str(context["job_fingerprint"])))
    runs.select_cv(run.run_id, "SWE")
    for phase in ("cv_ready", "applying", "review_pending"):
        runs.checkpoint(run.run_id, {"target_phase": phase})
    runs.checkpoint(
        run.run_id,
        {
            "target_phase": "submission_pending",
            "application_url": "https://example.invalid/apply/7",
        },
    )
    applications = home / "applications.csv"
    original_csv = applications.read_bytes()
    run_path = home / "runs" / run.run_id / "run.yaml"
    original_run = run_path.read_bytes()

    assert main(
        ["--home", str(home), "application", "record", "--run-id", run.run_id]
    ) != 0
    rejected = capsys.readouterr()
    assert rejected.err == ""
    assert json.loads(rejected.out) == {
        "schema_version": 1,
        "ok": False,
        "command": "application.record",
        "reason_code": "submission_not_confirmed",
        "warnings": [],
    }
    assert applications.read_bytes() == original_csv
    assert run_path.read_bytes() == original_run

    assert main(
        [
            "--home",
            str(home),
            "application",
            "record",
            "--run-id",
            run.run_id,
            "--confirmed-submitted",
        ]
    ) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["result"]["status"] == "Applied"
    assert runs.get(run.run_id).phase == "submitted_confirmed"


def test_cli_run_errors_do_not_leak_private_values_or_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "PRIVATE_RUN_HOME_CANARY" / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    assert main(
        ["--home", str(home), "run", "show", "--run-id", "run_PRIVATE_VALUE_CANARY"]
    ) == 4
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["reason_code"] == "run_not_found"
    assert "PRIVATE_RUN_HOME_CANARY" not in captured.out
    assert "PRIVATE_VALUE_CANARY" not in captured.out

    assert main(["--home", str(home), "run", "show", "--run-id", "../PRIVATE_PATH_CANARY"]) == 2
    unsafe = capsys.readouterr()
    assert json.loads(unsafe.out)["reason_code"] == "invalid_run_id"
    assert "PRIVATE_PATH_CANARY" not in unsafe.out
