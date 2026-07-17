"""Private CV source staging and strict manifest persistence."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from jobsearch_skill.cv_core import CVServiceBase, cv_build_error, now
from jobsearch_skill.cv_models import CVSelection, PreparedCV
from jobsearch_skill.cv_security import (
    copy_bytes_atomic,
    is_safe_ref,
    safe_read_relative,
)
from jobsearch_skill.errors import (
    CVBuildError,
    SchemaValidationError,
    StorageError,
)
from jobsearch_skill.runs import RunState


class CVPrepareMixin(CVServiceBase):
    """Stage immutable declared inputs and own the prepared manifest contract."""

    def prepare(self, run_id: str, selection: CVSelection) -> PreparedCV:
        state = self.run_store.require_open(run_id)
        self._require_selected(state, selection)
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        context = state.data.get("job_context")
        if not isinstance(context, Mapping):
            raise cv_build_error("cv_selection_mismatch")
        company = self._slug(context.get("company"))
        role = self._slug(context.get("role"))
        destination = self.generated_root / company / role / run_id
        self._require_beneath(destination, self.generated_root)
        self._ensure_directory(destination.parent)
        manifest_path = destination / "manifest.yaml"
        if destination.exists():
            return self._existing_prepared(
                state,
                selection,
                destination,
                manifest_path,
                source_hashes,
                source_hash,
            )

        stage = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=destination.parent))
        try:
            stage.chmod(0o700)
            source_dir = stage / "source"
            self._ensure_directory(source_dir, root=stage)
            copied_files: list[str] = []
            for snapshot in declared:
                target = source_dir / snapshot.relative
                self._require_beneath(target, source_dir)
                self._ensure_directory(target.parent, root=stage)
                self._copy_bytes_atomic(snapshot.data, target)
                actual = hashlib.sha256(
                    safe_read_relative(target.parent, Path(target.name))
                ).hexdigest()
                if actual != snapshot.sha256:
                    raise CVBuildError(
                        "cv_copy_digest: copied CV input digest does not match",
                        reason_code="cv_copy_digest",
                    )
                copied_files.append(snapshot.relative.as_posix())
            timestamp = now()
            manifest: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "source_cv_name": selection.name,
                "source_hash": source_hash,
                "source_hashes": source_hashes,
                "job_fingerprint": context.get("job_fingerprint"),
                "copied_files": copied_files,
                "expected_tex": (
                    selection.tex.relative_to(selection.root).as_posix()
                    if selection.tex is not None
                    else None
                ),
                "expected_pdf": (
                    selection.pdf.relative_to(selection.root).as_posix()
                    if selection.pdf is not None
                    else None
                ),
                "status": "prepared",
                "output_pdf": None,
                "verification": None,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self._atomic_manifest(stage / "manifest.yaml", manifest)
            try:
                os.replace(stage, destination)
            except OSError as error:
                if destination.exists():
                    return self._existing_prepared(
                        state,
                        selection,
                        destination,
                        manifest_path,
                        source_hashes,
                        source_hash,
                    )
                raise cv_build_error("cv_path_unsafe") from error
            stage = Path()
        finally:
            if stage != Path() and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        reference = manifest_path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return self._prepared_from_manifest(selection, destination, manifest)

    def _existing_prepared(
        self,
        state: RunState,
        selection: CVSelection,
        destination: Path,
        manifest_path: Path,
        source_hashes: Mapping[str, str],
        source_hash: str,
    ) -> PreparedCV:
        manifest = self._read_manifest(manifest_path)
        context = state.data.get("job_context")
        if not isinstance(context, Mapping) or any(
            (
                manifest.get("run_id") != state.run_id,
                manifest.get("source_cv_name") != selection.name,
                manifest.get("source_hash") != source_hash,
                manifest.get("source_hashes") != dict(source_hashes),
                manifest.get("job_fingerprint") != context.get("job_fingerprint"),
            )
        ):
            raise cv_build_error("cv_prepare_conflict")
        try:
            self._verify_manifest_sources(destination, manifest)
        except CVBuildError as error:
            raise cv_build_error("cv_prepare_conflict") from error
        reference = manifest_path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            state.run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return self._prepared_from_manifest(selection, destination, manifest)

    @staticmethod
    def _prepared_from_manifest(
        selection: CVSelection, destination: Path, manifest: Mapping[str, object]
    ) -> PreparedCV:
        source_dir = destination / "source"
        tex = (
            source_dir / selection.tex.relative_to(selection.root)
            if selection.tex is not None
            else None
        )
        pdf = (
            source_dir / selection.pdf.relative_to(selection.root)
            if selection.pdf is not None
            else None
        )
        return PreparedCV(
            run_id=str(manifest["run_id"]),
            selection=selection,
            root=destination,
            source_dir=source_dir,
            tex=tex,
            pdf=pdf,
            manifest_path=destination / "manifest.yaml",
            manifest=dict(manifest),
        )

    @staticmethod
    def _slug(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value) if value is not None else "")
        if text.strip() in {".", ".."}:
            raise cv_build_error("cv_path_unsafe")
        ascii_text = text.encode("ascii", "ignore").decode("ascii").casefold()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
        if slug in {".", ".."}:
            raise cv_build_error("cv_path_unsafe")
        return slug or "unknown"

    @staticmethod
    def _copy_bytes_atomic(data: bytes, target: Path) -> None:
        copy_bytes_atomic(data, target)

    def _validate_manifest_semantics(self, manifest: Mapping[str, object]) -> None:
        source_hashes = manifest.get("source_hashes")
        copied_files = manifest.get("copied_files")
        if (
            not isinstance(source_hashes, Mapping)
            or not source_hashes
            or not isinstance(copied_files, Sequence)
            or isinstance(copied_files, (str, bytes))
        ):
            raise cv_build_error("cv_prepare_conflict")
        normalized: dict[str, str] = {}
        for reference, digest in source_hashes.items():
            if (
                not isinstance(reference, str)
                or not is_safe_ref(reference)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise cv_build_error("cv_prepare_conflict")
            normalized[reference] = digest
        if list(copied_files) != list(normalized):
            raise cv_build_error("cv_prepare_conflict")
        if manifest.get("source_hash") != self._aggregate_hash(normalized):
            raise cv_build_error("cv_prepare_conflict")
        expected_tex = manifest.get("expected_tex")
        expected_pdf = manifest.get("expected_pdf")
        if expected_tex is not None and (
            not isinstance(expected_tex, str)
            or expected_tex not in normalized
            or not expected_tex.casefold().endswith(".tex")
        ):
            raise cv_build_error("cv_prepare_conflict")
        if expected_pdf is not None and (
            not isinstance(expected_pdf, str)
            or expected_pdf not in normalized
            or not expected_pdf.casefold().endswith(".pdf")
        ):
            raise cv_build_error("cv_prepare_conflict")
        tex_refs = [
            reference
            for reference in normalized
            if reference.casefold().endswith(".tex")
        ]
        if (expected_tex is None) != (not tex_refs) or len(tex_refs) > 1:
            raise cv_build_error("cv_prepare_conflict")
        pdf_refs = [
            reference
            for reference in normalized
            if reference.casefold().endswith(".pdf")
        ]
        if (expected_pdf is None) != (not pdf_refs) or len(pdf_refs) > 1:
            raise cv_build_error("cv_prepare_conflict")
        output_pdf = manifest.get("output_pdf")
        if output_pdf is not None:
            if not isinstance(expected_tex, str):
                raise cv_build_error("cv_prepare_conflict")
            expected_output = (
                Path("output") / Path(expected_tex).with_suffix(".pdf")
            ).as_posix()
            if output_pdf != expected_output:
                raise cv_build_error("cv_prepare_conflict")
        status = manifest.get("status")
        if status == "verified" and output_pdf is None:
            raise cv_build_error("cv_prepare_conflict")
        if status in {"prepared", "failed"} and output_pdf is not None:
            raise cv_build_error("cv_prepare_conflict")

    def _verify_manifest_sources(
        self, destination: Path, manifest: Mapping[str, object]
    ) -> None:
        self._validate_manifest_semantics(manifest)
        source_hashes = manifest["source_hashes"]
        assert isinstance(source_hashes, Mapping)
        source_dir = destination / "source"
        for reference, expected in source_hashes.items():
            try:
                data = safe_read_relative(source_dir, Path(str(reference)))
            except CVBuildError as error:
                raise cv_build_error("cv_prepare_conflict") from error
            if hashlib.sha256(data).hexdigest() != expected:
                raise cv_build_error("cv_prepare_conflict")

    def _atomic_manifest(self, path: Path, manifest: Mapping[str, object]) -> None:
        self._validate_manifest_semantics(manifest)
        self.store.registry.validate("cv-manifest.v1", manifest)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".manifest.")
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(
                    dict(manifest), stream, sort_keys=False, allow_unicode=True
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            path.chmod(0o600)
        except OSError as error:
            raise cv_build_error("cv_path_unsafe") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_manifest(self, path: Path) -> dict[str, object]:
        self._require_beneath(path, self.generated_root)
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise cv_build_error("cv_prepare_conflict") from error
        if not isinstance(value, dict):
            raise cv_build_error("cv_prepare_conflict")
        try:
            self.store.registry.validate("cv-manifest.v1", value)
        except (StorageError, SchemaValidationError) as error:
            raise cv_build_error("cv_prepare_conflict") from error
        self._validate_manifest_semantics(value)
        return value
