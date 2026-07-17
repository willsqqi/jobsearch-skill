"""Shared state and invariant helpers for the CV service mixins."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jobsearch_skill.cv_models import CVSelection
from jobsearch_skill.cv_security import (
    InputSnapshot,
    aggregate_hash,
    ensure_private_directory,
    snapshot_declared_inputs,
)
from jobsearch_skill.errors import CVBuildError
from jobsearch_skill.runs import RunState, RunStore
from jobsearch_skill.storage import SafeStore

if TYPE_CHECKING:
    from jobsearch_skill.cv_registry import CVRegistry


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def cv_build_error(reason_code: str, message: str | None = None) -> CVBuildError:
    messages = {
        "cv_asset_outside_root": "cv_asset_outside_root: input is outside the declared CV root",
        "cv_prepare_conflict": "cv_prepare_conflict: prepared CV sources conflict",
        "cv_source_invalid": "cv_source_invalid: declared CV input is unavailable",
        "cv_selection_mismatch": "cv_selection_mismatch: run CV selection does not match",
        "cv_path_unsafe": "cv_path_unsafe: generated CV path is unavailable",
    }
    return CVBuildError(message or messages[reason_code], reason_code=reason_code)


class CVServiceBase:
    """Own common service dependencies and cross-cutting invariants."""

    def __init__(
        self,
        home: Path,
        store: SafeStore,
        run_store: RunStore,
        cv_registry: CVRegistry,
    ) -> None:
        self.home = home.expanduser().resolve()
        self.store = store
        self.run_store = run_store
        self.cv_registry = cv_registry
        self.generated_root = (self.home / "generated").resolve()
        if not self.generated_root.is_relative_to(self.home):
            raise cv_build_error("cv_path_unsafe")
        self._ensure_directory(self.generated_root)

    def _require_selected(self, state: RunState, selection: CVSelection) -> None:
        selected = state.data.get("selected_cv")
        expected = selection.pdf if selection.pdf is not None else selection.tex
        if (
            state.phase != "cv_selected"
            or not isinstance(selected, Mapping)
            or selected.get("name") != selection.name
            or expected is None
            or not isinstance(selected.get("path"), str)
        ):
            raise cv_build_error("cv_selection_mismatch")
        try:
            selected_path = Path(str(selected["path"])).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise cv_build_error("cv_selection_mismatch") from error
        if selected_path != expected.resolve():
            raise cv_build_error("cv_selection_mismatch")

    def _declared_inputs(
        self, selection: CVSelection
    ) -> tuple[dict[str, str], tuple[InputSnapshot, ...]]:
        snapshots = snapshot_declared_inputs(selection)
        hashes = {snapshot.relative.as_posix(): snapshot.sha256 for snapshot in snapshots}
        return hashes, snapshots

    @staticmethod
    def _aggregate_hash(source_hashes: Mapping[str, str]) -> str:
        return aggregate_hash(dict(source_hashes))

    @staticmethod
    def _hash_entries(source_hashes: Mapping[str, str]) -> list[dict[str, str]]:
        return [
            {"source_ref": source_ref, "sha256": digest}
            for source_ref, digest in sorted(source_hashes.items())
        ]

    def _ensure_directory(self, path: Path, *, root: Path | None = None) -> None:
        boundary = root or self.home
        self._require_beneath(path, boundary)
        ensure_private_directory(path, boundary)

    @staticmethod
    def _require_beneath(path: Path, root: Path) -> None:
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise cv_build_error("cv_path_unsafe")
        except (OSError, RuntimeError) as error:
            raise cv_build_error("cv_path_unsafe") from error
