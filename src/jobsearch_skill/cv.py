"""Conservative selection of declared CV sources and PDFs."""

from __future__ import annotations

import os
import re
import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import yaml
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jobsearch_skill.errors import (
    CVBuildError,
    CVFactsError,
    CVSelectionError,
    SchemaValidationError,
    StorageError,
)
from jobsearch_skill.runs import RunState, RunStore
from jobsearch_skill.storage import SafeStore

_FILE_LOADING_COMMAND = re.compile(
    r"\\(?:addbibresource|attachfile|bibliography|include|includeanimation|includegraphics|"
    r"includepdf|input|inputminted|loadglsentries|lstinputlisting|subfile|subimport|"
    r"verbatiminput|import)(?![A-Za-z@])",
    re.IGNORECASE,
)
_USEPACKAGE_COMMAND = re.compile(
    r"\\usepackage(?![A-Za-z@])(?:\s*\[[^\]]*\])?\s*\{(?P<packages>[^}]*)\}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CVSelection:
    name: str
    root: Path
    pdf: Path | None
    tex: Path | None
    assets: tuple[Path, ...]


@dataclass(frozen=True)
class PreparedCV:
    run_id: str
    selection: CVSelection
    root: Path
    source_dir: Path
    tex: Path | None
    pdf: Path | None
    manifest_path: Path
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class CVEvidence:
    run_id: str
    cv_name: str
    source_hash: str
    source_hashes: Mapping[str, str]
    extracted_text: str
    path: Path
    reference: str


@dataclass(frozen=True)
class CVBuildResult:
    run_id: str
    pdf: Path
    verified: bool
    page_count: int
    extracted_text: str
    manifest_path: Path
    pdf_reference: str


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _cv_build_error(reason_code: str, message: str | None = None) -> CVBuildError:
    messages = {
        "cv_asset_outside_root": "cv_asset_outside_root: input is outside the declared CV root",
        "cv_prepare_conflict": "cv_prepare_conflict: prepared CV sources conflict",
        "cv_source_invalid": "cv_source_invalid: declared CV input is unavailable",
        "cv_selection_mismatch": "cv_selection_mismatch: run CV selection does not match",
        "cv_path_unsafe": "cv_path_unsafe: generated CV path is unavailable",
    }
    return CVBuildError(message or messages[reason_code], reason_code=reason_code)


class CVService:
    """Prepare private, immutable copies of CV inputs selected by one run."""

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
            raise _cv_build_error("cv_path_unsafe")
        self._ensure_directory(self.generated_root)

    def evidence(self, run_id: str, selection: CVSelection) -> CVEvidence:
        state = self.run_store.require_open(run_id)
        self._require_selected(state, selection)
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        path = self.run_store.runs_dir / run_id / f"cv-evidence-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        if path.exists():
            document = self.store.read_json(path, "cv-evidence.v1")
            return self._evidence_from_document(path, document, source_hashes)

        sources: list[dict[str, str]] = []
        for source, relative in declared:
            suffix = source.suffix.casefold()
            if suffix == ".tex":
                try:
                    text = source.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise self._evidence_error("cv_evidence_tex") from error
                if not text.strip():
                    raise self._evidence_error("cv_evidence_empty")
                sources.append(
                    {"source_ref": relative.as_posix(), "kind": "tex", "text": text}
                )
            elif suffix == ".pdf":
                text = self._pdf_text(source)
                sources.append(
                    {"source_ref": relative.as_posix(), "kind": "pdf", "text": text}
                )
        if not sources:
            raise self._evidence_error("cv_evidence_empty")
        document: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "cv_name": selection.name,
            "source_hash": source_hash,
            "source_hashes": self._hash_entries(source_hashes),
            "sources": sources,
            "created_at": _now(),
        }
        self.store.write_json(path, document, "cv-evidence.v1")
        reference = path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return self._evidence_from_document(path, document, source_hashes)

    def store_facts(
        self,
        run_id: str,
        selection: CVSelection,
        facts: Mapping[str, object],
    ) -> dict[str, object]:
        evidence = self.evidence(run_id, selection)
        candidate = dict(facts)
        try:
            self.store.registry.validate("cv-facts.v1", candidate)
        except SchemaValidationError as error:
            raise self._facts_error("cv_facts_invalid") from error
        if (
            candidate.get("cv_name") != selection.name
            or candidate.get("source_hash") != evidence.source_hash
            or candidate.get("source_hashes") != self._hash_entries(evidence.source_hashes)
        ):
            raise self._facts_error("cv_facts_source_mismatch")
        for anchor in self._evidence_anchors(candidate):
            if not anchor or anchor not in evidence.extracted_text:
                raise self._facts_error("cv_facts_anchor")
        path = self.facts_path(run_id, evidence.source_hash)
        if path.exists():
            existing = self.store.read_json(path, "cv-facts.v1")
            if existing != candidate:
                raise self._facts_error("cv_facts_conflict")
            return existing
        self.store.write_json(path, candidate, "cv-facts.v1")
        state = self.run_store.require_open(run_id)
        reference = path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return candidate

    def facts_path(self, run_id: str, source_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise self._facts_error("cv_facts_source_mismatch")
        path = self.run_store.runs_dir / run_id / f"cv-facts-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        return path

    def prepare(self, run_id: str, selection: CVSelection) -> PreparedCV:
        state = self.run_store.require_open(run_id)
        self._require_selected(state, selection)
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        context = state.data.get("job_context")
        if not isinstance(context, Mapping):
            raise _cv_build_error("cv_selection_mismatch")
        company = self._slug(context.get("company"))
        role = self._slug(context.get("role"))
        destination = self.generated_root / company / role / run_id
        self._require_beneath(destination, self.generated_root)
        self._ensure_directory(destination.parent)
        manifest_path = destination / "manifest.yaml"
        if destination.exists():
            return self._existing_prepared(
                state, selection, destination, manifest_path, source_hashes, source_hash
            )

        stage = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=destination.parent))
        try:
            stage.chmod(0o700)
            source_dir = stage / "source"
            self._ensure_directory(source_dir, root=stage)
            copied_files: list[str] = []
            for source, relative in declared:
                target = source_dir / relative
                self._require_beneath(target, source_dir)
                self._ensure_directory(target.parent, root=stage)
                self._copy_atomic(source, target)
                copied_files.append(relative.as_posix())
            timestamp = _now()
            manifest: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "source_cv_name": selection.name,
                "source_root": str(selection.root.resolve()),
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
                raise _cv_build_error("cv_path_unsafe") from error
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

    def build(self, run_id: str) -> CVBuildResult:
        state = self.run_store.require_open(run_id)
        manifest_path, manifest = self._prepared_manifest(state)
        root = manifest_path.parent
        redactions = self._base_redactions(manifest)
        expected_tex = manifest.get("expected_tex")
        if not isinstance(expected_tex, str) or not expected_tex:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_build_requires_tex",
                "PDF-only selection requires manual review.",
                redactions,
                decision="review_required",
            )
        tex = root / "source" / expected_tex
        self._require_beneath(tex, root / "source")
        self._require_regular_generated(tex)
        source_hash = manifest.get("source_hash")
        if not isinstance(source_hash, str):
            self._raise_build_failure(
                manifest_path, manifest, "cv_manifest_invalid", "Manifest invalid.", redactions
            )
        facts_path = self.facts_path(run_id, source_hash)
        try:
            facts = self.store.read_json(facts_path, "cv-facts.v1")
        except (StorageError, SchemaValidationError, OSError):
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_facts_missing",
                "Validated CV facts unavailable.",
                redactions,
            )
        redactions.extend(self._private_strings(facts))
        identity = facts.get("identity")
        full_name = identity.get("full_name") if isinstance(identity, Mapping) else None
        expected_identity = full_name.get("value") if isinstance(full_name, Mapping) else None
        if not isinstance(expected_identity, str) or not expected_identity:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_identity_missing",
                "Expected CV identity unavailable.",
                redactions,
            )
        output = tex.with_suffix(".pdf")
        self._require_beneath(output, self.generated_root)
        if manifest.get("status") == "verified" and manifest.get("output_pdf"):
            return self._verified_result(
                state, manifest_path, manifest, output, expected_identity, redactions
            )
        self._verify_prepared_sources(root, manifest, manifest_path, redactions)

        command = [
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex.name,
        ]
        previous_umask = os.umask(0o077)
        try:
            try:
                completed = subprocess.run(
                    command,
                    cwd=tex.parent,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except FileNotFoundError:
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_build_tool_missing",
                    "latexmk unavailable.",
                    redactions,
                )
            except subprocess.TimeoutExpired as error:
                try:
                    self._secure_generated_tree(root)
                except CVBuildError:
                    self._raise_build_failure(
                        manifest_path,
                        manifest,
                        "cv_generated_output_unsafe",
                        "Generated output contains an unsafe file.",
                        redactions,
                    )
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_build_timeout",
                    self._process_text(error.stdout, error.stderr),
                    redactions,
                )
            except OSError:
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_build_tool_error",
                    "latexmk could not be started.",
                    redactions,
                )
        finally:
            os.umask(previous_umask)
        try:
            self._secure_generated_tree(root)
        except CVBuildError:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_generated_output_unsafe",
                "Generated output contains an unsafe file.",
                redactions,
            )
        compiler_text = self._process_text(completed.stdout, completed.stderr)
        if completed.returncode != 0:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_build_failed",
                compiler_text,
                redactions,
            )
        return self._verified_result(
            state,
            manifest_path,
            manifest,
            output,
            expected_identity,
            redactions,
            compiler_text=compiler_text,
        )

    def _prepared_manifest(self, state: RunState) -> tuple[Path, dict[str, object]]:
        references = state.data.get("generated_artifacts")
        candidates: list[Path] = []
        if isinstance(references, Sequence) and not isinstance(references, (str, bytes)):
            for reference in references:
                if not isinstance(reference, str) or not reference.endswith("/manifest.yaml"):
                    continue
                path = self.home / reference
                try:
                    self._require_beneath(path, self.generated_root)
                except CVBuildError:
                    continue
                candidates.append(path)
        if len(candidates) != 1:
            raise CVBuildError(
                "cv_prepared_missing: prepared CV is unavailable",
                reason_code="cv_prepared_missing",
            )
        manifest = self._read_manifest(candidates[0])
        context = state.data.get("job_context")
        if (
            manifest.get("run_id") != state.run_id
            or not isinstance(context, Mapping)
            or manifest.get("job_fingerprint") != context.get("job_fingerprint")
        ):
            raise CVBuildError(
                "cv_manifest_mismatch: prepared CV is unavailable",
                reason_code="cv_manifest_mismatch",
            )
        return candidates[0], manifest

    def _verify_prepared_sources(
        self,
        root: Path,
        manifest: Mapping[str, object],
        manifest_path: Path,
        redactions: Sequence[str],
    ) -> None:
        source_hashes = manifest.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            self._raise_build_failure(
                manifest_path, manifest, "cv_manifest_invalid", "Manifest invalid.", redactions
            )
        for reference, expected in source_hashes.items():
            if not isinstance(reference, str) or not isinstance(expected, str):
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_manifest_invalid",
                    "Manifest invalid.",
                    redactions,
                )
            path = root / "source" / reference
            try:
                self._require_regular_generated(path)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, CVBuildError):
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_prepared_tampered",
                    "Prepared source integrity failed.",
                    redactions,
                )
            if actual != expected:
                self._raise_build_failure(
                    manifest_path,
                    manifest,
                    "cv_prepared_tampered",
                    "Prepared source integrity failed.",
                    redactions,
                )

    def _verified_result(
        self,
        state: RunState,
        manifest_path: Path,
        manifest: Mapping[str, object],
        output: Path,
        expected_identity: str,
        redactions: Sequence[str],
        *,
        compiler_text: str = "",
    ) -> CVBuildResult:
        try:
            self._require_regular_generated(output)
            if output.stat().st_size <= 0:
                raise OSError
            reader = PdfReader(output)
            if reader.is_encrypted:
                raise PyPdfError("encrypted")
            page_count = len(reader.pages)
            extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if page_count < 1 or not extracted_text.strip():
                raise PyPdfError("missing text")
        except (OSError, PyPdfError, ValueError, CVBuildError):
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_pdf_verification",
                compiler_text or "PDF verification failed.",
                redactions,
            )
        if expected_identity not in extracted_text:
            self._raise_build_failure(
                manifest_path,
                manifest,
                "cv_identity_mismatch",
                compiler_text or "PDF identity verification failed.",
                redactions,
            )
        output.chmod(0o600)
        pdf_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        updated = dict(manifest)
        updated.update(
            {
                "status": "verified",
                "output_pdf": output.relative_to(manifest_path.parent).as_posix(),
                "verification": {
                    "verified": True,
                    "page_count": page_count,
                    "identity_present": True,
                    "pdf_sha256": pdf_digest,
                    "log_ref": None,
                },
                "updated_at": _now(),
            }
        )
        self._atomic_manifest(manifest_path, updated)
        pdf_reference = output.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            state.run_id,
            {"target_phase": state.phase, "generated_artifacts": [pdf_reference]},
        )
        return CVBuildResult(
            run_id=state.run_id,
            pdf=output,
            verified=True,
            page_count=page_count,
            extracted_text=extracted_text,
            manifest_path=manifest_path,
            pdf_reference=pdf_reference,
        )

    def _raise_build_failure(
        self,
        manifest_path: Path,
        manifest: Mapping[str, object],
        reason_code: str,
        raw_log: str,
        redactions: Sequence[str],
        *,
        decision: str = "stop",
    ) -> NoReturn:
        root = manifest_path.parent
        log_path = root / "build-redacted.log"
        self._require_beneath(log_path, self.generated_root)
        sanitized = self._redact(raw_log, redactions)
        self._atomic_text(log_path, f"reason_code={reason_code}\n{sanitized}\n")
        updated = dict(manifest)
        updated.update(
            {
                "status": "failed",
                "output_pdf": None,
                "verification": {
                    "verified": False,
                    "page_count": 0,
                    "identity_present": False,
                    "pdf_sha256": None,
                    "log_ref": log_path.relative_to(root).as_posix(),
                },
                "updated_at": _now(),
            }
        )
        self._atomic_manifest(manifest_path, updated)
        raise CVBuildError(
            "cv_build_failed: CV artifact was not verified",
            reason_code=reason_code,
            log_path=log_path,
            decision=decision,
        )

    def _require_regular_generated(self, path: Path) -> None:
        self._require_beneath(path, self.generated_root)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as error:
            raise _cv_build_error("cv_path_unsafe") from error
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise _cv_build_error("cv_path_unsafe")

    def _secure_generated_tree(self, root: Path) -> None:
        self._require_beneath(root, self.generated_root)
        for path in (root, *root.rglob("*")):
            try:
                info = path.stat(follow_symlinks=False)
                if path.is_symlink():
                    raise OSError
                if stat.S_ISDIR(info.st_mode):
                    path.chmod(0o700)
                elif stat.S_ISREG(info.st_mode):
                    path.chmod(0o600)
                else:
                    raise OSError
            except OSError as error:
                raise _cv_build_error("cv_path_unsafe") from error

    def _base_redactions(self, manifest: Mapping[str, object]) -> list[str]:
        values = [str(self.home), str(self.generated_root)]
        source_root = manifest.get("source_root")
        if isinstance(source_root, str):
            values.append(source_root)
        return values

    @staticmethod
    def _private_strings(value: object) -> list[str]:
        strings: list[str] = []
        if isinstance(value, Mapping):
            for child in value.values():
                strings.extend(CVService._private_strings(child))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                strings.extend(CVService._private_strings(child))
        elif isinstance(value, str) and value:
            strings.append(value)
        return strings

    @staticmethod
    def _redact(text: str, values: Sequence[str]) -> str:
        sanitized = text
        for value in sorted({value for value in values if value}, key=len, reverse=True):
            sanitized = sanitized.replace(value, "[REDACTED]")
        return sanitized

    @staticmethod
    def _process_text(*values: object) -> str:
        parts: list[str] = []
        for value in values:
            if isinstance(value, bytes):
                parts.append(value.decode("utf-8", errors="replace"))
            elif isinstance(value, str):
                parts.append(value)
        return "\n".join(parts)

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            path.chmod(0o600)
        except OSError as error:
            raise _cv_build_error("cv_path_unsafe") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
            raise _cv_build_error("cv_prepare_conflict")
        reference = manifest_path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            state.run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return self._prepared_from_manifest(selection, destination, manifest)

    def _evidence_from_document(
        self,
        path: Path,
        document: Mapping[str, object],
        expected_hashes: Mapping[str, str],
    ) -> CVEvidence:
        entries = document.get("source_hashes")
        hashes = {
            str(entry["source_ref"]): str(entry["sha256"])
            for entry in entries
            if isinstance(entry, Mapping)
        } if isinstance(entries, Sequence) else {}
        sources = document.get("sources")
        text = "\n".join(
            str(source["text"])
            for source in sources
            if isinstance(source, Mapping) and isinstance(source.get("text"), str)
        ) if isinstance(sources, Sequence) else ""
        if hashes != dict(expected_hashes) or not text:
            raise self._evidence_error("cv_evidence_conflict")
        reference = path.relative_to(self.home).as_posix()
        return CVEvidence(
            run_id=str(document["run_id"]),
            cv_name=str(document["cv_name"]),
            source_hash=str(document["source_hash"]),
            source_hashes=hashes,
            extracted_text=text,
            path=path,
            reference=reference,
        )

    @staticmethod
    def _hash_entries(source_hashes: Mapping[str, str]) -> list[dict[str, str]]:
        return [
            {"source_ref": source_ref, "sha256": digest}
            for source_ref, digest in sorted(source_hashes.items())
        ]

    @staticmethod
    def _evidence_anchors(value: object):
        if isinstance(value, Mapping):
            anchor = value.get("evidence_anchor")
            if anchor is not None:
                yield anchor if isinstance(anchor, str) else ""
            for child in value.values():
                yield from CVService._evidence_anchors(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                yield from CVService._evidence_anchors(child)

    @staticmethod
    def _pdf_text(path: Path) -> str:
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise CVService._evidence_error("cv_evidence_encrypted")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except CVBuildError:
            raise
        except (OSError, PyPdfError, ValueError) as error:
            raise CVService._evidence_error("cv_evidence_pdf") from error
        if not text.strip():
            raise CVService._evidence_error("cv_evidence_empty")
        return text

    @staticmethod
    def _evidence_error(reason_code: str) -> CVBuildError:
        return CVBuildError(
            "cv_evidence_invalid: selected CV evidence is unavailable",
            reason_code=reason_code,
        )

    @staticmethod
    def _facts_error(reason_code: str) -> CVFactsError:
        messages = {
            "cv_facts_anchor": "cv_facts_anchor: evidence anchor is unavailable",
            "cv_facts_source_mismatch": "cv_facts_source_mismatch: facts source does not match",
            "cv_facts_conflict": "cv_facts_conflict: stored facts conflict",
            "cv_facts_invalid": "cv_facts_invalid: facts document is unavailable",
        }
        return CVFactsError(messages[reason_code], reason_code=reason_code)

    def _prepared_from_manifest(
        self, selection: CVSelection, destination: Path, manifest: Mapping[str, object]
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
            raise _cv_build_error("cv_selection_mismatch")
        try:
            selected_path = Path(str(selected["path"])).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise _cv_build_error("cv_selection_mismatch") from error
        if selected_path != expected.resolve():
            raise _cv_build_error("cv_selection_mismatch")

    def _declared_inputs(
        self, selection: CVSelection
    ) -> tuple[dict[str, str], tuple[tuple[Path, Path], ...]]:
        root = selection.root
        if root.is_symlink():
            raise _cv_build_error("cv_source_invalid")
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise _cv_build_error("cv_source_invalid") from error
        candidates = tuple(path for path in (selection.tex, selection.pdf, *selection.assets) if path)
        if not candidates:
            raise _cv_build_error("cv_source_invalid")
        seen: set[str] = set()
        hashes: dict[str, str] = {}
        declared: list[tuple[Path, Path]] = []
        for path in candidates:
            if path.is_symlink():
                raise _cv_build_error("cv_source_invalid")
            try:
                resolved = path.resolve(strict=True)
                info = path.stat(follow_symlinks=False)
            except (OSError, RuntimeError) as error:
                raise _cv_build_error("cv_source_invalid") from error
            if not resolved.is_relative_to(resolved_root):
                reason = "cv_asset_outside_root" if path in selection.assets else "cv_source_invalid"
                raise _cv_build_error(reason)
            if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
                raise _cv_build_error("cv_source_invalid")
            relative = resolved.relative_to(resolved_root)
            key = relative.as_posix().casefold()
            if key in seen or relative in {Path("."), Path("..")}:
                raise _cv_build_error("cv_source_invalid")
            seen.add(key)
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise _cv_build_error("cv_source_invalid") from error
            hashes[relative.as_posix()] = digest
            declared.append((path, relative))
        return hashes, tuple(declared)

    @staticmethod
    def _aggregate_hash(source_hashes: Mapping[str, str]) -> str:
        serialized = json.dumps(
            dict(source_hashes), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _slug(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value) if value is not None else "")
        if text.strip() in {".", ".."}:
            raise _cv_build_error("cv_path_unsafe")
        ascii_text = text.encode("ascii", "ignore").decode("ascii").casefold()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
        if slug in {".", ".."}:
            raise _cv_build_error("cv_path_unsafe")
        return slug or "unknown"

    def _ensure_directory(self, path: Path, *, root: Path | None = None) -> None:
        boundary = root or self.home
        self._require_beneath(path, boundary)
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise OSError
            path.chmod(0o700)
        except OSError as error:
            raise _cv_build_error("cv_path_unsafe") from error

    @staticmethod
    def _require_beneath(path: Path, root: Path) -> None:
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise _cv_build_error("cv_path_unsafe")
        except (OSError, RuntimeError) as error:
            raise _cv_build_error("cv_path_unsafe") from error

    @staticmethod
    def _copy_atomic(source: Path, target: Path) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            os.replace(temporary, target)
            temporary = None
            target.chmod(0o600)
        except OSError as error:
            raise _cv_build_error("cv_source_invalid") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _atomic_manifest(self, path: Path, manifest: Mapping[str, object]) -> None:
        self.store.registry.validate("cv-manifest.v1", manifest)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".manifest.")
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(dict(manifest), stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            path.chmod(0o600)
        except OSError as error:
            raise _cv_build_error("cv_path_unsafe") from error
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
            raise _cv_build_error("cv_prepare_conflict") from error
        if not isinstance(value, dict):
            raise _cv_build_error("cv_prepare_conflict")
        try:
            self.store.registry.validate("cv-manifest.v1", value)
        except (StorageError, SchemaValidationError) as error:
            raise _cv_build_error("cv_prepare_conflict") from error
        return value


class CVRegistry:
    """Resolve only CV files deliberately declared in validated preferences."""

    def __init__(self, preferences: Mapping[str, object], home: Path) -> None:
        self._preferences = preferences
        self._home = home.expanduser().resolve()

    def list(self) -> tuple[CVSelection, ...]:
        return tuple(self._registered_selection(entry) for entry in self._entries())

    def resolve(self, reference: str | None, *, for_customization: bool = False) -> CVSelection:
        if reference is None:
            default = self._preferences.get("default_cv")
            if not isinstance(default, str):
                raise self._error("cv_registry_invalid")
            return self._registered_by_name(default, for_customization=for_customization)
        if not isinstance(reference, str) or not reference.strip():
            raise self._error("cv_reference_invalid")
        entries = self._entries()
        matches = [entry for entry in entries if entry["name"].casefold() == reference.casefold()]
        if matches:
            return self._registered_by_name(reference, for_customization=for_customization)
        if self._is_explicit_path(reference):
            return self._explicit_selection(reference, for_customization=for_customization)
        raise self._error("cv_name_missing")

    @staticmethod
    def _error(reason_code: str, message: str | None = None) -> CVSelectionError:
        messages = {
            "cv_assets_unregistered": "cv_assets_unregistered: customization requires registry-declared assets",
            "cv_customization_requires_tex": "cv_customization_requires_tex: customization requires a LaTeX source",
        }
        return CVSelectionError(message or messages.get(reason_code, "cv_selection_invalid: CV selection is unavailable"), reason_code=reason_code)

    @staticmethod
    def _is_explicit_path(reference: str) -> bool:
        candidate = Path(reference).expanduser()
        return candidate.is_absolute() or "/" in reference or "\\" in reference or candidate.suffix != ""

    def _entries(self) -> tuple[Mapping[str, object], ...]:
        entries = self._preferences.get("cvs")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise self._error("cv_registry_invalid")
        result: list[Mapping[str, object]] = []
        names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                raise self._error("cv_registry_invalid")
            key = entry["name"].casefold()
            if not key or key in names:
                raise self._error("cv_name_ambiguous")
            names.add(key)
            result.append(entry)
        return tuple(result)

    def _registered_by_name(self, reference: str, *, for_customization: bool) -> CVSelection:
        matches = [entry for entry in self._entries() if entry["name"].casefold() == reference.casefold()]
        if len(matches) != 1:
            raise self._error("cv_name_missing" if not matches else "cv_name_ambiguous")
        selection = self._registered_selection(matches[0])
        if for_customization and selection.tex is None:
            raise self._error("cv_customization_requires_tex")
        return selection

    def _registry_root(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise self._error("cv_registry_invalid")
        raw = Path(value).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (self._home / raw).resolve()
        if not raw.is_absolute() and not candidate.is_relative_to(self._home):
            raise self._error("cv_registry_invalid")
        return candidate

    def _declared_file(self, root: Path, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise self._error("cv_registry_invalid")
        raw = Path(value).expanduser()
        path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        if not raw.is_absolute() and not path.is_relative_to(root):
            raise self._error("cv_registry_invalid")
        self._readable_file(path, registered=True)
        return path

    def _registered_selection(self, entry: Mapping[str, object]) -> CVSelection:
        root = self._registry_root(entry.get("root"))
        if not root.is_dir():
            raise self._error("cv_registry_invalid")
        tex = self._declared_file(root, entry["tex"]) if "tex" in entry else None
        pdf = self._declared_file(root, entry["pdf"]) if "pdf" in entry else None
        assets_value = entry.get("assets")
        if not isinstance(assets_value, Sequence) or isinstance(assets_value, (str, bytes)):
            raise self._error("cv_registry_invalid")
        assets = tuple(self._declared_file(root, asset) for asset in assets_value)
        if len(set(assets)) != len(assets):
            raise self._error("cv_registry_invalid")
        name = entry.get("name")
        if not isinstance(name, str):
            raise self._error("cv_registry_invalid")
        return CVSelection(name=name, root=root, pdf=pdf, tex=tex, assets=assets)

    def _explicit_selection(self, reference: str, *, for_customization: bool) -> CVSelection:
        path = Path(reference).expanduser().resolve()
        self._readable_file(path, registered=False)
        suffix = path.suffix.casefold()
        if suffix == ".pdf":
            if for_customization:
                raise self._error("cv_customization_requires_tex")
            return CVSelection(name=path.stem, root=path.parent, pdf=path, tex=None, assets=())
        if suffix == ".tex":
            if for_customization and self._has_external_dependencies(path):
                raise self._error("cv_assets_unregistered")
            return CVSelection(name=path.stem, root=path.parent, pdf=None, tex=path, assets=())
        raise self._error("cv_type_invalid")

    def _readable_file(self, path: Path, *, registered: bool) -> None:
        try:
            if not path.is_file() or not os.access(path, os.R_OK):
                raise OSError
            with path.open("rb"):
                pass
        except OSError as error:
            raise self._error("cv_registry_invalid" if registered else "cv_path_unavailable") from error

    @staticmethod
    def _has_external_dependencies(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CVRegistry._error("cv_path_unavailable") from error
        uncommented = CVRegistry._strip_tex_comments(text)
        if _FILE_LOADING_COMMAND.search(uncommented) is not None:
            return True
        for match in _USEPACKAGE_COMMAND.finditer(uncommented):
            packages = (package.strip() for package in match.group("packages").split(","))
            if any(
                package
                and (
                    package.startswith(".")
                    or "/" in package
                    or "\\" in package
                    or package.casefold().endswith(".sty")
                )
                for package in packages
            ):
                return True
        return False

    @staticmethod
    def _strip_tex_comments(text: str) -> str:
        lines: list[str] = []
        for line in text.splitlines():
            for index, character in enumerate(line):
                if character != "%":
                    continue
                preceding_backslashes = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == "\\":
                    preceding_backslashes += 1
                    cursor -= 1
                if preceding_backslashes % 2 == 0:
                    line = line[:index]
                    break
            lines.append(line)
        return "\n".join(lines)
