from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from filelock import FileLock

from jobsearch_skill.errors import SchemaValidationError, StorageError
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


@pytest.fixture
def store(tmp_path: Path) -> SafeStore:
    return SafeStore(SchemaRegistry(), tmp_path / "backups")


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "questions.yaml"


def valid_questions_document() -> dict[str, object]:
    return {"schema_version": 1, "questions": []}


def populated_questions_document() -> dict[str, object]:
    timestamp = "2026-01-01T00:00:00Z"
    return {
        "schema_version": 1,
        "questions": [
            {
                "canonical_id": "q_synthetic",
                "canonical_wording": "Synthetic reviewed question?",
                "observed_wordings": ["Synthetic reviewed question?"],
                "answer_type": "boolean",
                "scope": {"kind": "global"},
                "qualifiers": {"negated": False},
                "topic_tags": [],
                "role_tags": [],
                "answer": {"value": True, "source": "user", "updated_at": timestamp},
                "created_at": timestamp,
                "updated_at": timestamp,
                "history": [],
            }
        ],
    }


def test_invalid_replacement_preserves_original_and_creates_no_partial_file(
    store: SafeStore, path: Path
) -> None:
    original = valid_questions_document()
    store.write_yaml(path, original, "questions.v1")

    with pytest.raises(SchemaValidationError) as error:
        store.write_yaml(
            path,
            {"schema_version": 1, "questions": "PRIVATE_VALUE"},
            "questions.v1",
        )

    assert error.value.exit_code == 6
    assert "PRIVATE_VALUE" not in str(error.value)
    assert store.read_yaml(path, "questions.v1") == original
    assert not list(path.parent.glob("*.tmp"))


def test_replacement_creates_timestamped_backup_and_private_artifact_modes(
    store: SafeStore, path: Path
) -> None:
    store.write_yaml(path, valid_questions_document(), "questions.v1")
    replacement = {"schema_version": 1, "questions": []}
    store.write_yaml(path, replacement, "questions.v1")

    backups = list(store.backup_dir.glob("questions.*.yaml"))
    assert backups
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name(f"{path.name}.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.backup_dir.stat().st_mode) == 0o700


def test_store_sets_every_created_backup_parent_to_mode_0700(tmp_path: Path) -> None:
    nested = tmp_path / "private-root" / "state" / "backups"

    SafeStore(SchemaRegistry(), nested)

    for directory in (nested.parent.parent, nested.parent, nested):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_migration_backs_up_before_transform(store: SafeStore, path: Path) -> None:
    original = valid_questions_document()
    store.write_yaml(path, original, "questions.v1")
    backup_counts: list[int] = []

    def transform(value: dict[str, object]) -> dict[str, object]:
        assert value == original
        backup_counts.append(len(list(store.backup_dir.glob("questions.*.yaml"))))
        return populated_questions_document()

    store.migrate_yaml(path, "questions.v1", "questions.v1", transform)

    assert backup_counts == [1]
    assert store.read_yaml(path, "questions.v1") == populated_questions_document()


def test_failed_migration_preserves_original_and_pretransform_backup(
    store: SafeStore, path: Path
) -> None:
    original = valid_questions_document()
    store.write_yaml(path, original, "questions.v1")

    def fail(_: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("PRIVATE_TRANSFORM_VALUE")

    with pytest.raises(Exception) as error:
        store.migrate_yaml(path, "questions.v1", "questions.v1", fail)

    assert getattr(error.value, "exit_code", None) == 6
    assert "PRIVATE_TRANSFORM_VALUE" not in str(error.value)
    assert store.read_yaml(path, "questions.v1") == original
    assert list(store.backup_dir.glob("questions.*.yaml"))
    assert not list(path.parent.glob("*.tmp"))


def test_json_round_trip_uses_contract(store: SafeStore, tmp_path: Path) -> None:
    path = tmp_path / "questions.json"
    value = valid_questions_document()
    store.write_json(path, value, "questions.v1")
    assert store.read_json(path, "questions.v1") == value
    assert json.loads(path.read_text(encoding="utf-8")) == value


def test_rewrite_csv_validates_every_row_before_replacing(
    store: SafeStore, tmp_path: Path
) -> None:
    path = tmp_path / "applications.csv"
    fields = (
        "application_id",
        "company",
        "role",
        "location",
        "url",
        "job_fingerprint",
        "cv_name",
        "cv_path",
        "analysis_ref",
        "artifact_ref",
        "applied_at",
        "status",
        "workday_id",
        "updated_at",
    )
    store.rewrite_csv(path, fields, [], "application-record.v1", 1)
    original = path.read_bytes()
    invalid = {field: "" for field in fields}
    invalid["application_id"] = "PRIVATE_APPLICATION"
    invalid["status"] = "not-a-status"

    with pytest.raises(SchemaValidationError) as error:
        store.rewrite_csv(path, fields, [invalid], "application-record.v1", 1)

    assert error.value.exit_code == 6
    assert "PRIVATE_APPLICATION" not in str(error.value)
    assert path.read_bytes() == original
    assert path.read_text(encoding="utf-8").splitlines()[:2] == [
        "# schema_version=1",
        ",".join(fields),
    ]
    assert not list(path.parent.glob("*.tmp"))


def test_lock_acquisition_failure_is_normalized_without_path_or_value(
    store: SafeStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_acquire(*args: object, **kwargs: object) -> None:
        raise OSError("PRIVATE_LOCK_CANARY /private/candidate/questions.yaml")

    monkeypatch.setattr(FileLock, "acquire", fail_acquire)

    with pytest.raises(StorageError) as error:
        store.write_yaml(path, valid_questions_document(), "questions.v1")

    assert error.value.exit_code == 6
    assert error.value.reason_code == "storage_lock"
    assert "PRIVATE_LOCK_CANARY" not in str(error.value)
    assert "/private/candidate" not in str(error.value)


def test_lock_release_failure_is_normalized_without_path_or_value(
    store: SafeStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_release(*args: object, **kwargs: object) -> None:
        raise OSError("PRIVATE_RELEASE_CANARY /private/candidate/questions.yaml")

    monkeypatch.setattr(FileLock, "release", fail_release)

    with pytest.raises(StorageError) as error:
        store.write_yaml(path, valid_questions_document(), "questions.v1")

    assert error.value.exit_code == 6
    assert error.value.reason_code == "storage_lock"
    assert "PRIVATE_RELEASE_CANARY" not in str(error.value)
    assert "/private/candidate" not in str(error.value)


def test_temp_permission_failure_preserves_original_without_residue(
    store: SafeStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_yaml(path, valid_questions_document(), "questions.v1")
    original = path.read_bytes()
    original_chmod = Path.chmod

    def fail_temp_chmod(candidate: Path, mode: int) -> None:
        if candidate.name.endswith(".tmp"):
            raise OSError("PRIVATE_CHMOD_CANARY")
        original_chmod(candidate, mode)

    monkeypatch.setattr(Path, "chmod", fail_temp_chmod)

    with pytest.raises(StorageError) as error:
        store.write_yaml(path, populated_questions_document(), "questions.v1")

    assert error.value.exit_code == 6
    assert "PRIVATE_CHMOD_CANARY" not in str(error.value)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_cleanup_failure_does_not_mask_primary_safe_storage_error(
    store: SafeStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_yaml(path, valid_questions_document(), "questions.v1")
    original = path.read_bytes()
    original_unlink = Path.unlink

    def fail_backup(*args: object, **kwargs: object) -> None:
        raise StorageError(
            "storage_backup: unable to create private backup",
            reason_code="storage_backup",
        )

    def fail_temp_unlink(candidate: Path, *args: object, **kwargs: object) -> None:
        if candidate.name.endswith(".tmp"):
            raise OSError("PRIVATE_UNLINK_CANARY /private/temp-file")
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(store, "_create_backup", fail_backup)
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)

    with pytest.raises(StorageError) as error:
        store.write_yaml(path, populated_questions_document(), "questions.v1")

    assert error.value.exit_code == 6
    assert error.value.reason_code == "storage_backup"
    assert "PRIVATE_UNLINK_CANARY" not in str(error.value)
    assert "/private/temp-file" not in str(error.value)
    assert path.read_bytes() == original
