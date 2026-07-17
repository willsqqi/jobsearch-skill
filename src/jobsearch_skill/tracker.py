"""Confirmation-gated, idempotent application tracking."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from jobsearch_skill.errors import (
    ApplicationConflictError,
    InvalidTransition,
    SubmissionNotConfirmed,
)
from jobsearch_skill.home import APPLICATION_FIELDNAMES
from jobsearch_skill.jobs import canonicalize_job_url
from jobsearch_skill.runs import RunStore
from jobsearch_skill.storage import SafeStore


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _identity(fingerprint: str, url: str, workday_id: str) -> str:
    if workday_id:
        return f"workday:{workday_id}"
    digest = hashlib.sha256(f"{fingerprint}\n{url}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class ApplicationTracker:
    """Rewrite applications.csv under one lock before advancing a run."""

    def __init__(self, store: SafeStore, runs: RunStore, path: Path) -> None:
        self.store = store
        self.runs = runs
        self.path = path

    def rows(self) -> list[dict[str, str]]:
        return self.store.read_csv(
            self.path, APPLICATION_FIELDNAMES, "application-record.v1", 1
        )

    def record(
        self,
        run_id: str,
        *,
        confirmed_submitted: bool,
        workday_id: str | None = None,
    ) -> dict[str, str]:
        if confirmed_submitted is not True:
            raise SubmissionNotConfirmed(
                "submission_not_confirmed: explicit confirmation is required",
                reason_code="submission_not_confirmed",
            )
        state = self.runs.get(run_id)
        if state.phase not in {"submission_pending", "submitted_confirmed"}:
            raise InvalidTransition(
                "invalid_transition: application cannot be recorded in this phase",
                reason_code="invalid_transition",
            )
        data = state.data
        context = data["job_context"]
        assert isinstance(context, Mapping)
        raw_url = data.get("application_url")
        if not isinstance(raw_url, str) or not raw_url:
            raise ApplicationConflictError(
                "application_url_invalid: application identity is unavailable",
                reason_code="application_url_invalid",
            )
        url = canonicalize_job_url(raw_url)
        metadata = data.get("application_metadata")
        stored_workday = metadata.get("workday_id", "") if isinstance(metadata, Mapping) else ""
        bound_identity = data.get("application_identity")
        bound_workday = data.get("workday_id", "")
        chosen_workday = workday_id if workday_id is not None else (
            bound_workday if bound_identity is not None else stored_workday
        )
        if not isinstance(chosen_workday, str):
            chosen_workday = ""
        chosen_workday = chosen_workday.strip()
        fingerprint = str(context["job_fingerprint"])
        application_id = _identity(fingerprint, url, chosen_workday)
        if bound_identity is not None and bound_identity != application_id:
            raise ApplicationConflictError(
                "application_identity_conflict: application identity conflicts with the run",
                reason_code="application_identity_conflict",
            )
        state = self.runs.bind_application_identity(
            run_id,
            application_url=url,
            application_identity=application_id,
            workday_id=chosen_workday,
        )
        data = state.data
        existing_identity = data.get("application_id")
        if existing_identity not in (None, application_id):
            raise ApplicationConflictError(
                "application_identity_conflict: application identity conflicts with the run",
                reason_code="application_identity_conflict",
            )
        selected = data.get("selected_cv")
        selected = selected if isinstance(selected, Mapping) else {}
        now = _now()
        generated = data.get("generated_artifacts")
        artifact_ref = generated[-1] if isinstance(generated, list) and generated else selected.get("path", "")
        proposed = {
            "schema_version": "1",
            "application_id": application_id,
            "company": str(context.get("company", "")),
            "role": str(context.get("role", "")),
            "location": str(context.get("location", "")),
            "url": url,
            "job_fingerprint": fingerprint,
            "cv_name": str(selected.get("name", "")),
            "cv_path": str(selected.get("path", "")),
            "analysis_ref": str(data.get("analysis_ref", "")),
            "artifact_ref": str(artifact_ref),
            "applied_at": now,
            "status": "Applied",
            "workday_id": chosen_workday,
            "updated_at": now,
        }
        result: dict[str, str] = proposed

        def transform(rows: list[dict[str, str]]) -> list[dict[str, str]]:
            nonlocal result
            # All cross-file tracker operations lock applications.csv before run.yaml.
            # Re-read the run under that ordering so stale pre-lock identity cannot append.
            locked_state = self.runs.get(run_id)
            locked_identity = locked_state.data.get("application_identity")
            if locked_identity != application_id:
                raise ApplicationConflictError(
                    "application_identity_conflict: application identity conflicts with the run",
                    reason_code="application_identity_conflict",
                )
            matches = [row for row in rows if row["application_id"] == application_id]
            if len(matches) > 1:
                raise ApplicationConflictError(
                    "application_identity_conflict: duplicate tracker identity",
                    reason_code="application_identity_conflict",
                )
            if matches:
                existing = matches[0]
                refreshed = dict(proposed)
                refreshed["applied_at"] = existing["applied_at"]
                rows[rows.index(existing)] = refreshed
                result = refreshed
            else:
                rows.append(proposed)
                result = proposed
            return rows

        self.store.update_csv(
            self.path,
            APPLICATION_FIELDNAMES,
            "application-record.v1",
            1,
            transform,
            lambda _rows: self.runs.confirm_submission(run_id, application_id),
        )
        return result
