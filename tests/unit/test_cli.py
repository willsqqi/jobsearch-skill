import csv
import json
import os
from pathlib import Path

import pytest

from jobsearch_skill.cli import main
from jobsearch_skill.errors import StorageError
from jobsearch_skill.storage import SafeStore


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
