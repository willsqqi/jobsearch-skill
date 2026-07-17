from __future__ import annotations

import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from jobsearch_skill.cli import main
from jobsearch_skill.errors import JobsearchError, SchemaValidationError
from jobsearch_skill.home import bootstrap_private_home
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.questions import QuestionMemory
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


TIMESTAMP_1 = "2026-07-18T00:00:00Z"
TIMESTAMP_2 = "2026-07-18T01:00:00Z"


def reviewed_entry(**changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "wording": "Are you authorized to work in the US?",
        "value": True,
        "answer_type": "boolean",
        "scope": {"kind": "global"},
        "qualifiers": {"negated": False, "jurisdiction": "US"},
        "topic_tags": ["work authorization"],
        "role_tags": [],
        "source": "user",
        "canonical_source_id": None,
        "reviewed_at": TIMESTAMP_1,
    }
    entry.update(changes)
    return entry


def reviewed_document(*entries: dict[str, object], run_id: str = "run_synthetic") -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "page_id": "synthetic-page",
        "entries": list(entries) or [reviewed_entry()],
    }


@pytest.fixture
def setup_memory(tmp_path: Path) -> tuple[QuestionMemory, Path, SafeStore]:
    registry = SchemaRegistry()
    store = SafeStore(registry, tmp_path / "backups")
    path = tmp_path / "questions.yaml"
    store.write_yaml(path, {"schema_version": 1, "questions": []}, "questions.v1")
    return QuestionMemory(store, path), path, store


def test_sync_learns_reviewed_document_and_preserves_changed_answer_history(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, _, _ = setup_memory
    first = memory.sync(reviewed_document(reviewed_entry(value=True)))
    second = memory.sync(
        reviewed_document(reviewed_entry(value=False, reviewed_at=TIMESTAMP_2))
    )

    record = memory.get(first.entries[0].canonical_id)
    assert second.entries[0].canonical_id == first.entries[0].canonical_id
    assert record["answer"] == {
        "value": False,
        "source": "user",
        "updated_at": TIMESTAMP_2,
    }
    assert record["history"][-1] == {
        "value": True,
        "source": "user",
        "updated_at": TIMESTAMP_1,
    }


def test_source_only_change_adds_history(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, _, _ = setup_memory
    learned = memory.sync(reviewed_document())
    memory.sync(
        reviewed_document(reviewed_entry(source="codex_reviewed", reviewed_at=TIMESTAMP_2))
    )

    record = memory.get(learned.entries[0].canonical_id)
    assert record["answer"]["source"] == "codex_reviewed"
    assert record["history"] == [
        {"value": True, "source": "user", "updated_at": TIMESTAMP_1}
    ]


def test_identical_sync_is_idempotent_and_aliases_remain_unique(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, _ = setup_memory
    first = memory.sync(reviewed_document())
    after_first = path.read_bytes()
    second = memory.sync(reviewed_document())

    record = memory.get(first.entries[0].canonical_id)
    assert second.entries[0].canonical_id == first.entries[0].canonical_id
    assert record["history"] == []
    assert record["observed_wordings"] == ["Are you authorized to work in the US?"]
    assert path.read_bytes() == after_first


def test_semantic_reuse_updates_canonical_record_and_adds_exact_alias(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, _, _ = setup_memory
    first = memory.sync(reviewed_document())
    canonical_id = first.entries[0].canonical_id
    alias = "Do you currently have U.S. work authorization?"

    result = memory.sync(
        reviewed_document(
            reviewed_entry(
                wording=alias,
                canonical_source_id=canonical_id,
                reviewed_at=TIMESTAMP_2,
            )
        )
    )

    assert result.entries[0].canonical_id == canonical_id
    assert len(memory.all()) == 1
    assert memory.get(canonical_id)["observed_wordings"] == [
        "Are you authorized to work in the US?",
        alias,
    ]


def test_missing_or_incompatible_canonical_source_is_rejected_atomically(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, _ = setup_memory
    first = memory.sync(reviewed_document())
    original = path.read_bytes()

    with pytest.raises(JobsearchError) as missing:
        memory.sync(
            reviewed_document(reviewed_entry(canonical_source_id="q_missing"))
        )
    assert missing.value.reason_code == "canonical_source_missing"
    assert path.read_bytes() == original

    with pytest.raises(JobsearchError) as incompatible:
        memory.sync(
            reviewed_document(
                reviewed_entry(
                    canonical_source_id=first.entries[0].canonical_id,
                    qualifiers={"negated": False, "jurisdiction": "CA"},
                )
            )
        )
    assert incompatible.value.reason_code == "jurisdiction_mismatch"
    assert path.read_bytes() == original


def test_invalid_or_partial_reviewed_input_preserves_original_bytes(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, _ = setup_memory
    original = path.read_bytes()

    with pytest.raises(SchemaValidationError) as error:
        memory.sync({"wording": "PRIVATE_ANSWER_CANARY", "value": "PRIVATE_VALUE"})

    assert "PRIVATE" not in str(error.value)
    assert path.read_bytes() == original


def test_empty_qualifier_is_rejected_before_identical_repeat_and_preserves_bytes(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, _ = setup_memory
    memory.sync(reviewed_document())
    original = path.read_bytes()
    invalid = reviewed_document(
        reviewed_entry(qualifiers={"negated": False, "jurisdiction": ""})
    )

    with pytest.raises(SchemaValidationError) as error:
        memory.sync(invalid)

    assert error.value.reason_code == "schema_validation"
    assert path.read_bytes() == original


def test_multi_entry_sync_is_all_or_none(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, _ = setup_memory
    original = path.read_bytes()
    document = reviewed_document(
        reviewed_entry(wording="First reviewed question?"),
        reviewed_entry(
            wording="Second reviewed question?",
            canonical_source_id="q_missing",
        ),
    )

    with pytest.raises(JobsearchError, match="canonical_source_missing"):
        memory.sync(document)

    assert path.read_bytes() == original
    assert memory.all() == []


def test_sync_persists_private_file_lock_and_backup_modes(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, store = setup_memory
    memory.sync(reviewed_document())

    backups = list(store.backup_dir.glob("questions.*.yaml"))
    assert backups
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name("questions.yaml.lock").stat().st_mode) == 0o600
    assert all(stat.S_IMODE(backup.stat().st_mode) == 0o600 for backup in backups)


def test_concurrent_syncs_preserve_both_distinct_reviewed_questions(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, path, store = setup_memory
    barrier = threading.Barrier(2)
    original_write = store.write_yaml

    def coordinated_write(*args: object, **kwargs: object) -> None:
        barrier.wait(timeout=5)
        original_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "write_yaml", coordinated_write)
    first = reviewed_document(reviewed_entry(wording="First distinct reviewed question?"))
    second = reviewed_document(reviewed_entry(wording="Second distinct reviewed question?"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(memory.sync, document) for document in (first, second)]
        for future in futures:
            future.result(timeout=10)

    persisted = store.read_yaml(path, "questions.v1")
    assert sorted(record["canonical_wording"] for record in persisted["questions"]) == [
        "First distinct reviewed question?",
        "Second distinct reviewed question?",
    ]


def test_identical_transactional_sync_does_not_rewrite_or_add_backup(
    setup_memory: tuple[QuestionMemory, Path, SafeStore],
) -> None:
    memory, path, store = setup_memory
    memory.sync(reviewed_document())
    original = path.read_bytes()
    backup_count = len(list(store.backup_dir.glob("questions.*.yaml")))

    memory.sync(reviewed_document())

    assert path.read_bytes() == original
    assert len(list(store.backup_dir.glob("questions.*.yaml"))) == backup_count


def _write_input(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _open_run(home: Path) -> tuple[RunStore, str]:
    store = SafeStore(SchemaRegistry(), home / "backups")
    runs = RunStore(store, home / "runs")
    run = runs.start(
        make_job_context(
            job_url="https://example.invalid/jobs/question-sync",
            description="Synthetic question sync run",
        )
    )
    return runs, run.run_id


def test_cli_match_and_sync_emit_one_value_free_versioned_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    bootstrap_private_home(home, SchemaRegistry())
    runs, run_id = _open_run(home)
    sync_input = tmp_path / "reviewed.yaml"
    _write_input(sync_input, reviewed_document(run_id=run_id))

    assert main(
        ["--home", str(home), "questions", "sync", "--run-id", run_id, "--input", str(sync_input)]
    ) == 0
    synced = capsys.readouterr()
    sync_payload = json.loads(synced.out)
    assert synced.err == ""
    assert sync_payload == {
        "schema_version": 1,
        "ok": True,
        "command": "questions.sync",
        "result": {
            "run_id": run_id,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "canonical_ids": sync_payload["result"]["canonical_ids"],
        },
        "warnings": [],
    }
    assert '"value"' not in synced.out
    assert runs.get(run_id).data["learning_changes"] == sync_payload["result"]["canonical_ids"]

    match_input = tmp_path / "match.json"
    match_input.write_text(
        json.dumps(
            {
                "wording": "Are you authorized to work in the U.S.?",
                "answer_type": "boolean",
                "scope": {"kind": "global"},
                "qualifiers": {"negated": False, "jurisdiction": "US"},
            }
        ),
        encoding="utf-8",
    )
    assert main(
        ["--home", str(home), "questions", "match", "--input", str(match_input)]
    ) == 0
    matched = capsys.readouterr()
    payload = json.loads(matched.out)
    assert matched.err == ""
    assert payload["schema_version"] == 1
    assert payload["command"] == "questions.match"
    assert payload["result"]["kind"] == "exact"
    assert payload["result"]["canonical_source_id"] in sync_payload["result"]["canonical_ids"]
    assert "value" not in matched.out


def test_cli_validate_reuse_returns_review_result_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    bootstrap_private_home(home, SchemaRegistry())
    memory = QuestionMemory(SafeStore(SchemaRegistry(), home / "backups"), home / "questions.yaml")
    learned = memory.sync(reviewed_document())
    input_path = tmp_path / "reuse.yaml"
    _write_input(
        input_path,
        {
            "canonical_source_id": learned.entries[0].canonical_id,
            "wording": "Do you have Canadian work authorization?",
            "answer_type": "boolean",
            "scope": {"kind": "global"},
            "qualifiers": {"negated": False, "jurisdiction": "CA"},
        },
    )

    assert main(
        ["--home", str(home), "questions", "validate-reuse", "--input", str(input_path)]
    ) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["result"] == {
        "allowed": False,
        "canonical_source_id": learned.entries[0].canonical_id,
        "reason_code": "jurisdiction_mismatch",
    }
    assert '"value"' not in captured.out


@pytest.mark.parametrize("run_id", ["", "   ", "../run", "run/child", "run id", "."])
def test_cli_sync_rejects_unsafe_run_id_without_reading_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], run_id: str
) -> None:
    home = tmp_path / ".jobsearch"
    bootstrap_private_home(home, SchemaRegistry())
    input_path = tmp_path / "reviewed.yaml"
    input_path.write_text("PRIVATE_ANSWER_CANARY", encoding="utf-8")

    assert main(
        ["--home", str(home), "questions", "sync", "--run-id", run_id, "--input", str(input_path)]
    ) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "ok": False,
        "command": "questions.sync",
        "reason_code": "invalid_run_id",
        "warnings": [],
    }
    assert "PRIVATE_ANSWER_CANARY" not in captured.out


def test_cli_invalid_reviewed_input_does_not_leak_answer_or_change_questions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    bootstrap_private_home(home, SchemaRegistry())
    _, run_id = _open_run(home)
    questions = home / "questions.yaml"
    original = questions.read_bytes()
    input_path = tmp_path / "invalid.yaml"
    _write_input(input_path, {"value": "PRIVATE_ANSWER_CANARY"})

    assert main(
        ["--home", str(home), "questions", "sync", "--run-id", run_id, "--input", str(input_path)]
    ) == 3
    captured = capsys.readouterr()
    assert "PRIVATE_ANSWER_CANARY" not in captured.out
    assert questions.read_bytes() == original


def test_cli_sync_requires_existing_nonterminal_run_before_reading_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / ".jobsearch"
    bootstrap_private_home(home, SchemaRegistry())
    input_path = tmp_path / "reviewed.yaml"
    input_path.write_text("PRIVATE_UNREAD_CANARY", encoding="utf-8")
    questions = home / "questions.yaml"
    original = questions.read_bytes()

    assert main(
        [
            "--home",
            str(home),
            "questions",
            "sync",
            "--run-id",
            "run_missing",
            "--input",
            str(input_path),
        ]
    ) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "run_not_found"
    assert "PRIVATE_UNREAD_CANARY" not in captured.out
    assert questions.read_bytes() == original

    runs, run_id = _open_run(home)
    runs.checkpoint(run_id, {"target_phase": "stopped"})
    assert main(
        [
            "--home",
            str(home),
            "questions",
            "sync",
            "--run-id",
            run_id,
            "--input",
            str(input_path),
        ]
    ) == 6
    terminal = capsys.readouterr()
    assert json.loads(terminal.out)["reason_code"] == "invalid_transition"
    assert questions.read_bytes() == original
