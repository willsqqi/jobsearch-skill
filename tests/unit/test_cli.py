import json
from pathlib import Path

from jobsearch_skill.cli import main


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
