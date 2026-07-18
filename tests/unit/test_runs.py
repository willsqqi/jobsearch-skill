from __future__ import annotations

from pathlib import Path
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobsearch_skill.errors import (
    AmbiguousRunError,
    InvalidTransition,
    RunConflictError,
    RunNotFoundError,
    SchemaValidationError,
)
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


@pytest.fixture
def run_store(tmp_path: Path) -> RunStore:
    return RunStore(SafeStore(SchemaRegistry(), tmp_path / "backups"), tmp_path / "runs")


@pytest.fixture
def job_context() -> dict[str, object]:
    return make_job_context(
        job_url="https://example.invalid/jobs/7",
        company="Synthetic Systems",
        role="Backend Engineer",
        location="Remote",
        description="Build reliable APIs.",
    )


def analysis(run_id: str, fingerprint: str, *, summary: str = "Synthetic role") -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "job_fingerprint": fingerprint,
        "role_summary": summary,
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


def advance_to(run_store: RunStore, job_context: dict[str, object], phase: str):
    run = run_store.start(job_context)
    if phase == "created":
        return run
    run = run_store.save_analysis(
        run.run_id, analysis(run.run_id, str(job_context["job_fingerprint"]))
    )
    if phase == "analyzed":
        return run
    run = run_store.select_cv(run.run_id, "SWE")
    if phase == "cv_selected":
        return run
    for target in ("cv_ready", "applying"):
        run = run_store.checkpoint(run.run_id, {"target_phase": target})
        if phase == target:
            return run
    if phase == "upload_pending":
        return run_store.checkpoint(run.run_id, {"target_phase": phase})
    run = run_store.checkpoint(run.run_id, {"target_phase": "review_pending"})
    if phase == "review_pending":
        return run
    if phase == "submission_pending":
        return run_store.checkpoint(
            run.run_id,
            {
                "target_phase": phase,
                "application_url": "https://example.invalid/apply/7",
            },
        )
    raise AssertionError(f"unsupported test phase {phase}")


def test_cv_override_does_not_skip_analysis(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)

    with pytest.raises(InvalidTransition):
        run_store.select_cv(run.run_id, "DE")

    with pytest.raises(InvalidTransition):
        run_store.checkpoint(run.run_id, {"target_phase": "analyzed"})


def test_latest_open_is_ambiguous_with_two_active_runs(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run_store.start(job_context)
    run_store.start(job_context)

    with pytest.raises(AmbiguousRunError):
        run_store.latest_open()


def test_start_creates_unique_private_runs_and_latest_open_returns_one(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    first = run_store.start(job_context)
    assert run_store.latest_open().run_id == first.run_id
    second = run_store.start(job_context)

    assert first.run_id != second.run_id
    for run in (first, second):
        directory = run_store.runs_dir / run.run_id
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "run.yaml").stat().st_mode) == 0o600


def test_latest_open_has_fixed_not_found_error(run_store: RunStore) -> None:
    with pytest.raises(RunNotFoundError) as error:
        run_store.latest_open()

    assert error.value.reason_code == "run_not_found"
    assert str(run_store.runs_dir) not in str(error.value)


def test_start_rejects_invalid_job_context_without_creating_run(run_store: RunStore) -> None:
    with pytest.raises(SchemaValidationError):
        run_store.start({"description": "PRIVATE_JOB_CANARY"})

    assert list(run_store.runs_dir.iterdir()) == []


def test_analysis_is_owned_by_run_private_and_identical_repeat_is_noop(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    document = analysis(run.run_id, str(job_context["job_fingerprint"]))
    analyzed = run_store.save_analysis(run.run_id, document)
    reference = analyzed.data["analysis_ref"]
    assert isinstance(reference, str)
    artifact = run_store.runs_dir.parent / reference
    original = artifact.read_bytes()
    run_bytes = (artifact.parent / "run.yaml").read_bytes()
    backup_count = len(list(run_store.store.backup_dir.iterdir()))

    repeated = run_store.save_analysis(run.run_id, document)

    assert repeated.phase == "analyzed"
    assert artifact.read_bytes() == original
    assert (artifact.parent / "run.yaml").read_bytes() == run_bytes
    assert len(list(run_store.store.backup_dir.iterdir())) == backup_count
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_invalid_analysis_preserves_run_and_creates_no_artifact(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    run_path = run_store.runs_dir / run.run_id / "run.yaml"
    original = run_path.read_bytes()

    with pytest.raises(SchemaValidationError) as error:
        run_store.save_analysis(run.run_id, {"role_summary": "PRIVATE_ANALYSIS_CANARY"})

    assert "PRIVATE_ANALYSIS_CANARY" not in str(error.value)
    assert run_path.read_bytes() == original
    assert sorted(path.name for path in run_path.parent.iterdir()) == ["run.yaml", "run.yaml.lock"]


def test_conflicting_analysis_repeat_is_rejected(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    first = analysis(run.run_id, str(job_context["job_fingerprint"]))
    run_store.save_analysis(run.run_id, first)

    with pytest.raises(RunConflictError):
        run_store.save_analysis(
            run.run_id,
            analysis(
                run.run_id,
                str(job_context["job_fingerprint"]),
                summary="Conflicting role",
            ),
        )


def test_analysis_repeat_verifies_owned_artifact_integrity(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    document = analysis(run.run_id, str(job_context["job_fingerprint"]))
    analyzed = run_store.save_analysis(run.run_id, document)
    artifact = run_store.runs_dir.parent / str(analyzed.data["analysis_ref"])
    artifact.write_text("PRIVATE_CORRUPTED_ANALYSIS", encoding="utf-8")

    with pytest.raises(Exception) as error:
        run_store.save_analysis(run.run_id, document)

    assert "PRIVATE_CORRUPTED_ANALYSIS" not in str(error.value)


def test_every_allowed_transition_and_upload_return_path(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = advance_to(run_store, job_context, "applying")
    assert run_store.checkpoint(run.run_id, {"target_phase": "upload_pending"}).phase == "upload_pending"
    assert run_store.checkpoint(run.run_id, {"target_phase": "applying"}).phase == "applying"
    assert run_store.checkpoint(run.run_id, {"target_phase": "review_pending"}).phase == "review_pending"
    assert (
        run_store.checkpoint(
            run.run_id,
            {
                "target_phase": "submission_pending",
                "application_url": "https://example.invalid/apply/7",
            },
        ).phase
        == "submission_pending"
    )

    with pytest.raises(InvalidTransition):
        run_store.checkpoint(run.run_id, {"target_phase": "submitted_confirmed"})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("created", "cv_selected"),
        ("analyzed", "cv_ready"),
        ("cv_selected", "applying"),
        ("cv_ready", "review_pending"),
        ("applying", "submission_pending"),
        ("review_pending", "applying"),
        ("submission_pending", "review_pending"),
    ],
)
def test_skips_and_backwards_transitions_are_rejected(
    run_store: RunStore,
    job_context: dict[str, object],
    source: str,
    target: str,
) -> None:
    run = advance_to(run_store, job_context, source)

    with pytest.raises(InvalidTransition):
        run_store.checkpoint(run.run_id, {"target_phase": target})


@pytest.mark.parametrize(
    "phase",
    ["created", "analyzed", "cv_selected", "cv_ready", "applying", "upload_pending", "review_pending"],
)
def test_each_permitted_nonterminal_phase_can_stop(
    run_store: RunStore, job_context: dict[str, object], phase: str
) -> None:
    run = advance_to(run_store, job_context, phase)
    stopped = run_store.checkpoint(run.run_id, {"target_phase": "stopped"})

    assert stopped.phase == "stopped"
    with pytest.raises(InvalidTransition):
        run_store.checkpoint(run.run_id, {"target_phase": phase})


def test_submission_pending_cannot_stop(run_store: RunStore, job_context: dict[str, object]) -> None:
    run = advance_to(run_store, job_context, "submission_pending")

    with pytest.raises(InvalidTransition):
        run_store.checkpoint(run.run_id, {"target_phase": "stopped"})


def test_stopped_terminal_checkpoint_allows_only_identical_noop(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    stopped = run_store.checkpoint(
        run.run_id,
        {"target_phase": "stopped", "completed_page_ids": ["page-1"]},
    )
    path = run_store.runs_dir / run.run_id / "run.yaml"
    original = path.read_bytes()
    backup_count = len(list(run_store.store.backup_dir.glob("run.*.yaml")))

    repeated = run_store.checkpoint(
        run.run_id,
        {"target_phase": "stopped", "completed_page_ids": ["page-1"]},
    )
    assert repeated.data == stopped.data
    assert path.read_bytes() == original
    assert len(list(run_store.store.backup_dir.glob("run.*.yaml"))) == backup_count

    with pytest.raises(InvalidTransition):
        run_store.checkpoint(
            run.run_id,
            {"target_phase": "stopped", "completed_page_ids": ["page-2"]},
        )
    assert path.read_bytes() == original


def test_checkpoint_is_closed_merges_duplicates_and_identical_repeat_is_noop(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = advance_to(run_store, job_context, "cv_selected")
    checkpoint = {
        "target_phase": "cv_ready",
        "completed_page_ids": ["page-1", "page-1"],
        "generated_artifacts": ["generated/resume.pdf", "generated/resume.pdf"],
        "pending_manual_actions": [
            {"kind": "upload", "field_id": "resume", "reason_code": "manual_upload"},
            {"kind": "upload", "field_id": "resume", "reason_code": "manual_upload"},
        ],
        "unresolved_fields": ["field-1", "field-1"],
        "learning_changes": ["q_one", "q_one"],
    }
    run_store.checkpoint(run.run_id, checkpoint)
    path = run_store.runs_dir / run.run_id / "run.yaml"
    original = path.read_bytes()
    backup_count = len(list(run_store.store.backup_dir.glob("run.*.yaml")))

    repeated = run_store.checkpoint(run.run_id, checkpoint)

    assert repeated.data["completed_page_ids"] == ["page-1"]
    assert repeated.data["generated_artifacts"] == ["generated/resume.pdf"]
    assert repeated.data["unresolved_fields"] == ["field-1"]
    assert repeated.data["learning_changes"] == ["q_one"]
    assert path.read_bytes() == original
    assert len(list(run_store.store.backup_dir.glob("run.*.yaml"))) == backup_count

    with pytest.raises(SchemaValidationError):
        run_store.checkpoint(run.run_id, {"target_phase": "cv_ready", "phase": "submitted_confirmed"})


@pytest.mark.parametrize(
    "reference", [".", "/private/resume.pdf", "../resume.pdf", "a/../../resume.pdf"]
)
def test_checkpoint_rejects_unsafe_artifact_references_without_mutation(
    run_store: RunStore, job_context: dict[str, object], reference: str
) -> None:
    run = advance_to(run_store, job_context, "cv_selected")
    path = run_store.runs_dir / run.run_id / "run.yaml"
    original = path.read_bytes()

    with pytest.raises(RunConflictError):
        run_store.checkpoint(
            run.run_id,
            {"target_phase": "cv_ready", "generated_artifacts": [reference]},
        )

    assert path.read_bytes() == original


def test_concurrent_checkpoints_on_one_run_preserve_both_pages(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = advance_to(run_store, job_context, "cv_ready")
    checkpoints = [
        {"target_phase": "applying", "completed_page_ids": ["page-1"]},
        {"target_phase": "applying", "completed_page_ids": ["page-2"]},
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda value: run_store.checkpoint(run.run_id, value), checkpoints))

    assert all(result.phase == "applying" for result in results)
    assert sorted(run_store.get(run.run_id).data["completed_page_ids"]) == ["page-1", "page-2"]


def test_learning_change_audit_requires_open_run_and_is_idempotent(
    run_store: RunStore, job_context: dict[str, object]
) -> None:
    run = run_store.start(job_context)
    merged = run_store.merge_learning_changes(run.run_id, ["q_one", "q_one", "q_two"])
    assert merged.data["learning_changes"] == ["q_one", "q_two"]
    path = run_store.runs_dir / run.run_id / "run.yaml"
    original = path.read_bytes()
    assert run_store.merge_learning_changes(run.run_id, ["q_one"]).data["learning_changes"] == [
        "q_one",
        "q_two",
    ]
    assert path.read_bytes() == original

    run_store.checkpoint(run.run_id, {"target_phase": "stopped"})
    with pytest.raises(InvalidTransition):
        run_store.merge_learning_changes(run.run_id, ["q_three"])
