"""Validated, resumable private application run state."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from jobsearch_skill.errors import (
    AmbiguousRunError,
    InvalidTransition,
    RunConflictError,
    RunNotFoundError,
    StorageError,
)
from jobsearch_skill.jobs import canonicalize_job_url
from jobsearch_skill.storage import SafeStore


_PHASES = {
    "created",
    "analyzed",
    "cv_selected",
    "cv_ready",
    "applying",
    "upload_pending",
    "review_pending",
    "submission_pending",
    "submitted_confirmed",
    "stopped",
}
_TERMINAL_PHASES = {"submitted_confirmed", "stopped"}
_CHECKPOINT_TRANSITIONS = {
    ("cv_selected", "cv_ready"),
    ("cv_ready", "applying"),
    ("applying", "upload_pending"),
    ("upload_pending", "applying"),
    ("applying", "review_pending"),
    ("review_pending", "submission_pending"),
    *((phase, "stopped") for phase in _PHASES - _TERMINAL_PHASES - {"submission_pending"}),
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _invalid_transition() -> InvalidTransition:
    return InvalidTransition(
        "invalid_transition: requested run transition is unavailable",
        reason_code="invalid_transition",
    )


def _conflict(reason_code: str = "run_conflict") -> RunConflictError:
    return RunConflictError(
        "run_conflict: requested run mutation conflicts with persisted state",
        reason_code=reason_code,
    )


def _safe_ref(value: str) -> bool:
    if not value or value in {".", ".."} or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _stable_merge(current: Sequence[object], incoming: Sequence[object]) -> list[object]:
    merged = list(current)
    for candidate in incoming:
        if candidate not in merged:
            merged.append(candidate)
    return merged


@dataclass(frozen=True)
class RunState:
    """Typed view of one validated run document."""

    run_id: str
    phase: str
    data: Mapping[str, object]

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> RunState:
        run_id = document.get("run_id")
        phase = document.get("phase")
        if not isinstance(run_id, str) or not isinstance(phase, str):
            raise StorageError(
                "run_state: private run state is unavailable",
                reason_code="run_state",
            )
        return cls(run_id=run_id, phase=phase, data=dict(document))


class RunStore:
    """Create and mutate run YAML through one transaction per run mutation."""

    def __init__(self, store: SafeStore, runs_dir: Path) -> None:
        self.store = store
        self.runs_dir = runs_dir
        self.store._ensure_private_directory(runs_dir)

    def _path(self, run_id: str) -> Path:
        if (
            not isinstance(run_id, str)
            or not run_id.startswith("run_")
            or not run_id[4:]
            or not run_id.replace("_", "").replace("-", "").isalnum()
        ):
            raise RunNotFoundError(
                "run_not_found: requested run is unavailable",
                reason_code="run_not_found",
            )
        path = self.runs_dir / run_id / "run.yaml"
        if not path.exists():
            raise RunNotFoundError(
                "run_not_found: requested run is unavailable",
                reason_code="run_not_found",
            )
        return path

    def start(self, job_context: Mapping[str, object]) -> RunState:
        self.store.registry.validate("job-context.v1", job_context)
        while True:
            run_id = f"run_{secrets.token_urlsafe(18)}"
            directory = self.runs_dir / run_id
            try:
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
                break
            except FileExistsError:
                continue
            except OSError as error:
                raise StorageError(
                    "run_create: unable to create private run",
                    reason_code="run_create",
                ) from error
        timestamp = _now()
        document: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "job_context": dict(job_context),
            "phase": "created",
            "selected_cv": None,
            "analysis_ref": None,
            "generated_artifacts": [],
            "completed_page_ids": [],
            "pending_manual_actions": [],
            "unresolved_fields": [],
            "learning_changes": [],
            "application_url": None,
            "application_metadata": {},
            "application_id": None,
            "application_identity": None,
            "workday_id": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.store.write_yaml(directory / "run.yaml", document, "run-state.v1")
        return RunState.from_document(document)

    def get(self, run_id: str) -> RunState:
        return RunState.from_document(self.store.read_yaml(self._path(run_id), "run-state.v1"))

    def require_open(self, run_id: str) -> RunState:
        state = self.get(run_id)
        if state.phase in _TERMINAL_PHASES:
            raise _invalid_transition()
        return state

    def save_analysis(self, run_id: str, analysis: Mapping[str, object]) -> RunState:
        self.store.registry.validate("analysis.v1", analysis)
        state = self.get(run_id)
        if analysis.get("run_id") != run_id:
            raise _conflict("run_id_mismatch")
        context = state.data.get("job_context")
        fingerprint = context.get("job_fingerprint") if isinstance(context, Mapping) else None
        if analysis.get("job_fingerprint") != fingerprint:
            raise _conflict("job_fingerprint_mismatch")
        serialized = __import__("json").dumps(
            dict(analysis), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()
        reference = f"runs/{run_id}/analysis-{digest}.yaml"
        artifact = self.runs_dir / run_id / f"analysis-{digest}.yaml"
        if state.phase == "analyzed":
            if state.data.get("analysis_ref") != reference:
                raise _conflict()
            if self.store.read_yaml(artifact, "analysis.v1") != dict(analysis):
                raise _conflict()
            return state
        if state.phase != "created":
            raise _invalid_transition()
        if artifact.exists():
            persisted = self.store.read_yaml(artifact, "analysis.v1")
            if persisted != dict(analysis):
                raise _conflict()
        else:
            self.store.write_yaml(artifact, analysis, "analysis.v1")

        def transform(document: dict[str, object]) -> Mapping[str, object]:
            if document["phase"] == "analyzed" and document["analysis_ref"] == reference:
                return document
            if document["phase"] != "created":
                raise _invalid_transition()
            document["phase"] = "analyzed"
            document["analysis_ref"] = reference
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )

    def select_cv(self, run_id: str, reference: str | Mapping[str, object]) -> RunState:
        if isinstance(reference, str) and reference:
            selection = {"name": reference, "path": reference, "customized": False}
        elif isinstance(reference, Mapping):
            selection = dict(reference)
        else:
            raise _conflict("cv_reference_invalid")

        def transform(document: dict[str, object]) -> Mapping[str, object]:
            if document["phase"] == "cv_selected" and document["selected_cv"] == selection:
                return document
            if document["phase"] != "analyzed":
                raise _invalid_transition()
            document["selected_cv"] = selection
            document["phase"] = "cv_selected"
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )

    def checkpoint(self, run_id: str, checkpoint: Mapping[str, object]) -> RunState:
        self.store.registry.validate("checkpoint.v1", checkpoint)
        for reference in checkpoint.get("generated_artifacts", []):
            if not isinstance(reference, str) or not _safe_ref(reference):
                raise _conflict("artifact_ref_invalid")
        normalized = dict(checkpoint)
        if "application_url" in normalized:
            normalized["application_url"] = canonicalize_job_url(
                str(normalized["application_url"])
            )

        def transform(document: dict[str, object]) -> Mapping[str, object]:
            source = str(document["phase"])
            target = str(normalized["target_phase"])
            if source != target and (source, target) not in _CHECKPOINT_TRANSITIONS:
                raise _invalid_transition()
            if source in _TERMINAL_PHASES:
                for key in (
                    "completed_page_ids",
                    "generated_artifacts",
                    "pending_manual_actions",
                    "unresolved_fields",
                    "learning_changes",
                ):
                    if key in normalized and _stable_merge(
                        document[key], normalized[key]  # type: ignore[arg-type]
                    ) != document[key]:
                        raise _invalid_transition()
                for key in ("application_url", "application_metadata"):
                    if key in normalized and document[key] != normalized[key]:
                        raise _invalid_transition()
                return document
            changed = source != target
            for key in (
                "completed_page_ids",
                "generated_artifacts",
                "pending_manual_actions",
                "unresolved_fields",
                "learning_changes",
            ):
                if key in normalized:
                    merged = _stable_merge(document[key], normalized[key])  # type: ignore[arg-type]
                    if merged != document[key]:
                        document[key] = merged
                        changed = True
            for key in ("application_url", "application_metadata"):
                if key not in normalized:
                    continue
                existing = document[key]
                incoming = normalized[key]
                if existing not in (None, {}) and existing != incoming:
                    raise _conflict("application_identity_conflict")
                if existing != incoming:
                    document[key] = incoming
                    changed = True
            if not changed:
                return document
            document["phase"] = target
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )

    def merge_learning_changes(self, run_id: str, canonical_ids: Sequence[str]) -> RunState:
        identifiers = list(dict.fromkeys(canonical_ids))

        def transform(document: dict[str, object]) -> Mapping[str, object]:
            if document["phase"] in _TERMINAL_PHASES:
                raise _invalid_transition()
            merged = _stable_merge(document["learning_changes"], identifiers)  # type: ignore[arg-type]
            if merged == document["learning_changes"]:
                return document
            document["learning_changes"] = merged
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )

    def latest_open(self) -> RunState:
        active: list[RunState] = []
        try:
            paths = sorted(self.runs_dir.glob("run_*/run.yaml"))
        except OSError as error:
            raise StorageError(
                "run_read: unable to inspect private runs",
                reason_code="run_read",
            ) from error
        for path in paths:
            state = RunState.from_document(self.store.read_yaml(path, "run-state.v1"))
            if state.phase not in _TERMINAL_PHASES:
                active.append(state)
        if not active:
            raise RunNotFoundError(
                "run_not_found: no open run is available",
                reason_code="run_not_found",
            )
        if len(active) != 1:
            raise AmbiguousRunError(
                "run_ambiguous: multiple open runs are available",
                reason_code="run_ambiguous",
            )
        return active[0]

    def bind_application_identity(
        self,
        run_id: str,
        *,
        application_url: str,
        application_identity: str,
        workday_id: str,
    ) -> RunState:
        """Durably bind tracker identity before acquiring applications.csv."""

        def transform(document: dict[str, object]) -> Mapping[str, object]:
            if document["phase"] not in {"submission_pending", "submitted_confirmed"}:
                raise _invalid_transition()
            if document["application_url"] != application_url:
                raise _conflict("application_identity_conflict")
            existing = document["application_identity"]
            if existing is not None:
                if existing != application_identity or document["workday_id"] != workday_id:
                    raise _conflict("application_identity_conflict")
                return document
            if document["phase"] != "submission_pending":
                raise _invalid_transition()
            document["application_identity"] = application_identity
            document["workday_id"] = workday_id
            document["application_metadata"] = {"workday_id": workday_id}
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )

    def confirm_submission(self, run_id: str, application_id: str) -> RunState:
        def transform(document: dict[str, object]) -> Mapping[str, object]:
            if document["application_identity"] != application_id:
                raise _conflict("application_identity_conflict")
            existing = document["application_id"]
            if existing not in (None, application_id):
                raise _conflict("application_identity_conflict")
            if document["phase"] == "submitted_confirmed":
                return document
            if document["phase"] != "submission_pending":
                raise _invalid_transition()
            document["application_id"] = application_id
            document["phase"] = "submitted_confirmed"
            document["updated_at"] = _now()
            return document

        return RunState.from_document(
            self.store.update_yaml(self._path(run_id), "run-state.v1", transform)
        )
