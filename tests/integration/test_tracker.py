from __future__ import annotations

from pathlib import Path
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobsearch_skill.errors import (
    ApplicationConflictError,
    RunConflictError,
    StorageError,
    SubmissionNotConfirmed,
)
from jobsearch_skill.home import APPLICATION_FIELDNAMES
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore
from jobsearch_skill.tracker import ApplicationTracker


def _analysis(run_id: str, fingerprint: str) -> dict[str, object]:
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


@pytest.fixture
def services(tmp_path: Path) -> tuple[RunStore, ApplicationTracker]:
    registry = SchemaRegistry()
    store = SafeStore(registry, tmp_path / "backups")
    runs = RunStore(store, tmp_path / "runs")
    applications = tmp_path / "applications.csv"
    store.rewrite_csv(
        applications,
        APPLICATION_FIELDNAMES,
        [],
        "application-record.v1",
        1,
    )
    return runs, ApplicationTracker(store, runs, applications)


def _ready_run(
    runs: RunStore,
    *,
    job_number: int = 7,
    workday_id: str = "",
):
    context = make_job_context(
        job_url=f"https://example.invalid/jobs/{job_number}",
        company="Synthetic Systems",
        role=f"Backend Engineer {job_number}",
        location="Remote",
        description=f"Build reliable APIs for job {job_number}.",
    )
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis(run.run_id, str(context["job_fingerprint"])))
    runs.select_cv(run.run_id, "SWE")
    runs.checkpoint(run.run_id, {"target_phase": "cv_ready"})
    runs.checkpoint(run.run_id, {"target_phase": "applying"})
    runs.checkpoint(run.run_id, {"target_phase": "review_pending"})
    return runs.checkpoint(
        run.run_id,
        {
            "target_phase": "submission_pending",
            "application_url": f"https://example.invalid/apply/{job_number}?utm_source=test",
            "application_metadata": {"workday_id": workday_id},
        },
    )


def test_application_is_recorded_only_after_explicit_confirmation(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    ready_run = _ready_run(runs)
    assert tracker.rows() == []

    with pytest.raises(SubmissionNotConfirmed):
        tracker.record(ready_run.run_id, confirmed_submitted=False)

    tracker.record(ready_run.run_id, confirmed_submitted=True)
    tracker.record(ready_run.run_id, confirmed_submitted=True)
    assert len(tracker.rows()) == 1
    assert tracker.rows()[0]["status"] == "Applied"


def test_no_confirmation_preserves_tracker_and_run_bytes(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    run = _ready_run(runs)
    tracker_bytes = tracker.path.read_bytes()
    run_path = runs.runs_dir / run.run_id / "run.yaml"
    run_bytes = run_path.read_bytes()
    backups = list(tracker.store.backup_dir.iterdir())

    with pytest.raises(SubmissionNotConfirmed):
        tracker.record(run.run_id, confirmed_submitted=False)

    assert tracker.path.read_bytes() == tracker_bytes
    assert run_path.read_bytes() == run_bytes
    assert list(tracker.store.backup_dir.iterdir()) == backups


def test_tracker_uses_exact_header_schema_and_private_modes(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    run = _ready_run(runs)
    tracker.record(run.run_id, confirmed_submitted=True)

    lines = tracker.path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# schema_version=1", ",".join(APPLICATION_FIELDNAMES)]
    row = tracker.rows()[0]
    assert tuple(row) == APPLICATION_FIELDNAMES
    assert row["schema_version"] == "1"
    assert row["status"] == "Applied"
    assert stat.S_IMODE(tracker.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tracker.path.with_name("applications.csv.lock").stat().st_mode) == 0o600
    backups = list(tracker.store.backup_dir.glob("applications.*.csv"))
    assert backups and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)


def test_workday_identity_is_preferred_and_repeat_preserves_applied_at(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    run = _ready_run(runs, workday_id="WD-123")
    first = tracker.record(run.run_id, confirmed_submitted=True)
    second = tracker.record(run.run_id, confirmed_submitted=True)

    assert first["application_id"] == "workday:WD-123"
    assert second["application_id"] == first["application_id"]
    assert second["applied_at"] == first["applied_at"]
    assert len(tracker.rows()) == 1


def test_fallback_identity_uses_canonical_application_url(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    run = _ready_run(runs)
    row = tracker.record(run.run_id, confirmed_submitted=True)

    assert row["application_id"].startswith("sha256:")
    assert row["url"] == "https://example.invalid/apply/7"


def test_different_identity_after_confirmation_preserves_both_files(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    run = _ready_run(runs, workday_id="WD-123")
    tracker.record(run.run_id, confirmed_submitted=True)
    tracker_bytes = tracker.path.read_bytes()
    run_path = runs.runs_dir / run.run_id / "run.yaml"
    run_bytes = run_path.read_bytes()

    with pytest.raises(ApplicationConflictError):
        tracker.record(
            run.run_id,
            confirmed_submitted=True,
            workday_id="WD-DIFFERENT",
        )

    assert tracker.path.read_bytes() == tracker_bytes
    assert run_path.read_bytes() == run_bytes


def test_tracker_recovers_when_csv_succeeded_before_run_advance(
    services: tuple[RunStore, ApplicationTracker], monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, tracker = services
    run = _ready_run(runs)
    original_confirm = runs.confirm_submission
    calls = 0

    def fail_once(run_id: str, application_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StorageError(
                "storage_replace: synthetic interruption",
                reason_code="storage_replace",
            )
        return original_confirm(run_id, application_id)

    monkeypatch.setattr(runs, "confirm_submission", fail_once)
    with pytest.raises(StorageError):
        tracker.record(run.run_id, confirmed_submitted=True)

    assert len(tracker.rows()) == 1
    assert runs.get(run.run_id).phase == "submission_pending"
    applied_at = tracker.rows()[0]["applied_at"]

    recovered = tracker.record(run.run_id, confirmed_submitted=True)

    assert recovered["applied_at"] == applied_at
    assert len(tracker.rows()) == 1
    assert runs.get(run.run_id).phase == "submitted_confirmed"


def test_concurrent_distinct_records_do_not_lose_rows(
    services: tuple[RunStore, ApplicationTracker],
) -> None:
    runs, tracker = services
    first = _ready_run(runs, job_number=7)
    second = _ready_run(runs, job_number=8)

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(
            executor.map(
                lambda run: tracker.record(run.run_id, confirmed_submitted=True),
                (first, second),
            )
        )

    assert len({row["application_id"] for row in rows}) == 2
    assert len(tracker.rows()) == 2
    assert runs.get(first.run_id).phase == "submitted_confirmed"
    assert runs.get(second.run_id).phase == "submitted_confirmed"


def test_concurrent_different_identities_for_same_run_leave_no_orphan_row(
    services: tuple[RunStore, ApplicationTracker], monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, tracker = services
    run = _ready_run(runs)
    original_update = tracker.store.update_csv
    barrier = threading.Barrier(2)

    def coordinated_update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(tracker.store, "update_csv", coordinated_update)

    def record(workday_id: str):
        return tracker.record(
            run.run_id,
            confirmed_submitted=True,
            workday_id=workday_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(record, workday_id) for workday_id in ("WD-ONE", "WD-TWO")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except (ApplicationConflictError, RunConflictError):
                outcomes.append(None)

    assert sum(outcome is not None for outcome in outcomes) == 1
    assert len(tracker.rows()) == 1
    assert runs.get(run.run_id).data["application_id"] == tracker.rows()[0]["application_id"]
