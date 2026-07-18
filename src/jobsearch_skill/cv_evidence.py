"""Evidence extraction and fact binding for declared CV inputs."""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jobsearch_skill.cv_core import CVServiceBase, now
from jobsearch_skill.cv_models import CVEvidence, CVSelection
from jobsearch_skill.cv_security import InputSnapshot
from jobsearch_skill.errors import CVBuildError, CVFactsError, SchemaValidationError, StorageError


class CVEvidenceMixin(CVServiceBase):
    """Extract immutable source evidence and bind validated facts to it."""

    def evidence(self, run_id: str, selection: CVSelection) -> CVEvidence:
        state = self.run_store.require_open(run_id)
        self._require_selected(state, selection)
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        path = self.run_store.runs_dir / run_id / f"cv-evidence-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        sources: list[dict[str, str]] = []
        for snapshot in declared:
            reference = snapshot.relative.as_posix()
            if snapshot.kind == "tex":
                sources.append(
                    {
                        "source_ref": reference,
                        "kind": "tex",
                        "text": snapshot.data.decode("utf-8"),
                    }
                )
            elif snapshot.kind == "pdf":
                sources.append(
                    {
                        "source_ref": reference,
                        "kind": "pdf",
                        "text": self._pdf_text(snapshot.data),
                    }
                )
        if not sources:
            raise self._evidence_error("cv_evidence_empty")
        if path.exists():
            document = self.store.read_json(path, "cv-evidence.v1")
            return self._evidence_from_document(
                path,
                document,
                source_hashes,
                run_id=run_id,
                cv_name=selection.name,
                source_hash=source_hash,
                expected_sources=sources,
            )
        document: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "cv_name": selection.name,
            "source_hash": source_hash,
            "source_hashes": self._hash_entries(source_hashes),
            "sources": sources,
            "created_at": now(),
        }
        self.store.write_json(path, document, "cv-evidence.v1")
        reference = path.relative_to(self.home).as_posix()
        self.run_store.checkpoint(
            run_id,
            {"target_phase": state.phase, "generated_artifacts": [reference]},
        )
        return self._evidence_from_document(
            path,
            document,
            source_hashes,
            run_id=run_id,
            cv_name=selection.name,
            source_hash=source_hash,
            expected_sources=sources,
        )

    def candidate_evidence(self, run_id: str, selection: CVSelection) -> CVEvidence:
        """Extract evidence for one registered CV without selecting it in run state."""

        state = self.run_store.require_open(run_id)
        if state.phase not in {"created", "analyzed"} or state.data.get("selected_cv") is not None:
            raise self._evidence_error("cv_evidence_selection")
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        directory = self._candidate_directory(run_id, selection.name)
        self._ensure_directory(directory, root=self.run_store.runs_dir)
        path = directory / f"cv-evidence-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        sources = self._sources(declared)
        if not sources:
            raise self._evidence_error("cv_evidence_empty")
        if path.exists():
            document = self.store.read_json(path, "cv-evidence.v1")
        else:
            document = {
                "schema_version": 1,
                "run_id": run_id,
                "cv_name": selection.name,
                "source_hash": source_hash,
                "source_hashes": self._hash_entries(source_hashes),
                "sources": sources,
                "created_at": now(),
            }
            self.store.write_json(path, document, "cv-evidence.v1")
        return self._evidence_from_document(
            path,
            document,
            source_hashes,
            run_id=run_id,
            cv_name=selection.name,
            source_hash=source_hash,
            expected_sources=sources,
        )

    def store_facts(
        self,
        run_id: str,
        selection: CVSelection,
        facts: Mapping[str, object],
    ) -> dict[str, object]:
        evidence = self.evidence(run_id, selection)
        candidate = self._validated_facts(run_id, selection, evidence, facts)
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

    def store_candidate_facts(
        self,
        run_id: str,
        selection: CVSelection,
        facts: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate and stage facts for a CV without binding it as the run selection."""

        evidence = self.candidate_evidence(run_id, selection)
        candidate = self._validated_facts(run_id, selection, evidence, facts)
        path = self.candidate_facts_path(run_id, selection.name, evidence.source_hash)
        if path.exists():
            existing = self.store.read_json(path, "cv-facts.v1")
            if existing != candidate:
                raise self._facts_error("cv_facts_conflict")
            return existing
        self.store.write_json(path, candidate, "cv-facts.v1")
        return candidate

    def facts_for_selected_run(
        self, run_id: str, selection: CVSelection
    ) -> dict[str, object]:
        """Read only facts and evidence still bound to the current selected CV bytes."""

        state = self.run_store.require_open(run_id)
        self._require_selected_binding(state, selection)
        source_hashes, declared = self._declared_inputs(selection)
        source_hash = self._aggregate_hash(source_hashes)
        facts_path = self.run_store.runs_dir / run_id / f"cv-facts-{source_hash}.json"
        evidence_path = self.run_store.runs_dir / run_id / f"cv-evidence-{source_hash}.json"
        facts_ref = facts_path.relative_to(self.home).as_posix()
        evidence_ref = evidence_path.relative_to(self.home).as_posix()
        references = state.data.get("generated_artifacts")
        fact_refs = (
            [reference for reference in references if isinstance(reference, str) and reference.startswith(f"runs/{run_id}/cv-facts-")]
            if isinstance(references, Sequence)
            else []
        )
        evidence_refs = (
            [reference for reference in references if isinstance(reference, str) and reference.startswith(f"runs/{run_id}/cv-evidence-")]
            if isinstance(references, Sequence)
            else []
        )
        if fact_refs != [facts_ref] or evidence_refs != [evidence_ref]:
            raise self._facts_error("cv_facts_source_mismatch")
        for path in (facts_path, evidence_path):
            try:
                info = path.stat(follow_symlinks=False)
                if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise OSError
            except OSError as error:
                raise self._facts_error("cv_facts_source_mismatch") from error
        try:
            facts = self.store.read_json(facts_path, "cv-facts.v1")
            evidence_document = self.store.read_json(evidence_path, "cv-evidence.v1")
        except (StorageError, SchemaValidationError) as error:
            raise self._facts_error("cv_facts_source_mismatch") from error
        expected_entries = self._hash_entries(source_hashes)
        if (
            facts.get("run_id") != run_id
            or facts.get("cv_name") != selection.name
            or facts.get("source_hash") != source_hash
            or facts.get("source_hashes") != expected_entries
        ):
            raise self._facts_error("cv_facts_source_mismatch")
        expected_sources: list[dict[str, str]] = []
        for snapshot in declared:
            reference = snapshot.relative.as_posix()
            if snapshot.kind == "tex":
                expected_sources.append(
                    {"source_ref": reference, "kind": "tex", "text": snapshot.data.decode("utf-8")}
                )
            elif snapshot.kind == "pdf":
                expected_sources.append(
                    {"source_ref": reference, "kind": "pdf", "text": self._pdf_text(snapshot.data)}
                )
        try:
            evidence = self._evidence_from_document(
                evidence_path,
                evidence_document,
                source_hashes,
                run_id=run_id,
                cv_name=selection.name,
                source_hash=source_hash,
                expected_sources=expected_sources,
            )
        except CVBuildError as error:
            raise self._facts_error("cv_facts_source_mismatch") from error
        if any(not anchor or anchor not in evidence.extracted_text for anchor in self._evidence_anchors(facts)):
            raise self._facts_error("cv_facts_source_mismatch")
        return facts

    def facts_path(self, run_id: str, source_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise self._facts_error("cv_facts_source_mismatch")
        path = self.run_store.runs_dir / run_id / f"cv-facts-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        return path

    def candidate_facts_path(self, run_id: str, cv_name: str, source_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise self._facts_error("cv_facts_source_mismatch")
        path = self._candidate_directory(run_id, cv_name) / f"cv-facts-{source_hash}.json"
        self._require_beneath(path, self.run_store.runs_dir)
        return path

    def _candidate_directory(self, run_id: str, cv_name: str) -> Path:
        name_hash = hashlib.sha256(cv_name.encode("utf-8")).hexdigest()[:24]
        path = self.run_store.runs_dir / run_id / "cv-candidates" / name_hash
        self._require_beneath(path, self.run_store.runs_dir)
        return path

    def _validated_facts(
        self,
        run_id: str,
        selection: CVSelection,
        evidence: CVEvidence,
        facts: Mapping[str, object],
    ) -> dict[str, object]:
        candidate = dict(facts)
        try:
            self.store.registry.validate("cv-facts.v1", candidate)
        except SchemaValidationError as error:
            raise self._facts_error("cv_facts_invalid") from error
        if (
            candidate.get("run_id") != run_id
            or candidate.get("cv_name") != selection.name
            or candidate.get("source_hash") != evidence.source_hash
            or candidate.get("source_hashes") != self._hash_entries(evidence.source_hashes)
        ):
            raise self._facts_error("cv_facts_source_mismatch")
        for anchor in self._evidence_anchors(candidate):
            if not anchor or anchor not in evidence.extracted_text:
                raise self._facts_error("cv_facts_anchor")
        return candidate

    def _sources(self, declared: Sequence[InputSnapshot]) -> list[dict[str, str]]:
        sources: list[dict[str, str]] = []
        for snapshot in declared:
            reference = snapshot.relative.as_posix()
            if snapshot.kind == "tex":
                sources.append(
                    {
                        "source_ref": reference,
                        "kind": "tex",
                        "text": snapshot.data.decode("utf-8"),
                    }
                )
            elif snapshot.kind == "pdf":
                sources.append(
                    {
                        "source_ref": reference,
                        "kind": "pdf",
                        "text": self._pdf_text(snapshot.data),
                    }
                )
        return sources

    def _evidence_from_document(
        self,
        path: Path,
        document: Mapping[str, object],
        expected_hashes: Mapping[str, str],
        *,
        run_id: str,
        cv_name: str,
        source_hash: str,
        expected_sources: Sequence[Mapping[str, str]],
    ) -> CVEvidence:
        entries = document.get("source_hashes")
        hashes = (
            {
                str(entry["source_ref"]): str(entry["sha256"])
                for entry in entries
                if isinstance(entry, Mapping)
            }
            if isinstance(entries, Sequence)
            else {}
        )
        sources = document.get("sources")
        text = (
            "\n".join(
                str(source["text"])
                for source in sources
                if isinstance(source, Mapping)
                and isinstance(source.get("text"), str)
            )
            if isinstance(sources, Sequence)
            else ""
        )
        if (
            document.get("run_id") != run_id
            or document.get("cv_name") != cv_name
            or document.get("source_hash") != source_hash
            or source_hash != self._aggregate_hash(expected_hashes)
            or entries != self._hash_entries(expected_hashes)
            or hashes != dict(expected_hashes)
            or sources != list(expected_sources)
            or not text
        ):
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
    def _evidence_anchors(value: object):
        if isinstance(value, Mapping):
            anchor = value.get("evidence_anchor")
            if anchor is not None:
                yield anchor if isinstance(anchor, str) else ""
            for child in value.values():
                yield from CVEvidenceMixin._evidence_anchors(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                yield from CVEvidenceMixin._evidence_anchors(child)

    @staticmethod
    def _pdf_text(data: bytes) -> str:
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise CVEvidenceMixin._evidence_error("cv_evidence_encrypted")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except CVBuildError:
            raise
        except (OSError, PyPdfError, ValueError) as error:
            raise CVEvidenceMixin._evidence_error("cv_evidence_pdf") from error
        if not text.strip():
            raise CVEvidenceMixin._evidence_error("cv_evidence_empty")
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
            "cv_facts_source_mismatch": (
                "cv_facts_source_mismatch: facts source does not match"
            ),
            "cv_facts_conflict": "cv_facts_conflict: stored facts conflict",
            "cv_facts_invalid": "cv_facts_invalid: facts document is unavailable",
        }
        return CVFactsError(messages[reason_code], reason_code=reason_code)
