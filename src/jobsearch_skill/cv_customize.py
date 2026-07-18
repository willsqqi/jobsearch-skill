"""Evidence-grounded customization of a prepared CV copy."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from jobsearch_skill.cv_core import CVServiceBase, now
from jobsearch_skill.cv_models import CVEvidence, CVSelection
from jobsearch_skill.cv_security import copy_bytes_atomic, safe_read_relative, validate_tex_bytes
from jobsearch_skill.errors import CVBuildError, SchemaValidationError, StorageError


_GROUNDING_GLUE = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "href",
    "in",
    "item",
    "of",
    "on",
    "or",
    "textbf",
    "textit",
    "the",
    "to",
    "url",
    "with",
}
_TOKEN = re.compile(
    r"[$€£]?\d+(?:[.,]\d+)*(?:%|x)?|[A-Za-z][A-Za-z0-9+#]*(?:-[A-Za-z0-9+#]+)*"
)


def customization_error(reason_code: str) -> CVBuildError:
    messages = {
        "cv_customization_anchor": "cv_customization_anchor: claim evidence is unavailable",
        "cv_customization_conflict": "cv_customization_conflict: stored customization conflicts",
        "cv_customization_invalid": "cv_customization_invalid: customization input is unavailable",
        "cv_customization_source": "cv_customization_source: customization source does not match",
    }
    return CVBuildError(messages[reason_code], reason_code=reason_code)


class CVCustomizeMixin(CVServiceBase):
    """Validate a claim ledger and persist a separate customized TeX source."""

    def customize(
        self,
        run_id: str,
        selection: CVSelection,
        request: Mapping[str, object],
    ) -> dict[str, object]:
        state = self.run_store.require_open(run_id)
        self._require_selected(state, selection)
        selected = state.data.get("selected_cv")
        if (
            selection.tex is None
            or not isinstance(selected, Mapping)
            or selected.get("customized") is not True
            or selected.get("path") != str(selection.tex.resolve())
        ):
            raise customization_error("cv_customization_source")
        manifest_path, manifest = self._prepared_manifest(state)
        try:
            self.store.registry.validate("cv-customization.v1", request)
        except SchemaValidationError as error:
            raise customization_error("cv_customization_invalid") from error
        candidate = dict(request)
        if (
            candidate.get("run_id") != run_id
            or candidate.get("cv_name") != selection.name
            or candidate.get("source_hash") != manifest.get("source_hash")
        ):
            raise customization_error("cv_customization_source")
        expected_tex = manifest.get("expected_tex")
        if not isinstance(expected_tex, str):
            raise customization_error("cv_customization_source")
        customized_tex = candidate.get("customized_tex")
        if not isinstance(customized_tex, str):
            raise customization_error("cv_customization_invalid")
        source_hashes = manifest.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            raise customization_error("cv_customization_source")
        asset_refs = {
            str(reference)
            for reference in source_hashes
            if Path(str(reference)).suffix == ".png"
        }
        try:
            validate_tex_bytes(customized_tex.encode("utf-8"), asset_refs)
        except CVBuildError as error:
            raise customization_error("cv_customization_invalid") from error

        evidence = self.evidence(run_id, selection)
        try:
            facts = self.store.read_json(
                self.facts_path(run_id, evidence.source_hash), "cv-facts.v1"
            )
        except (OSError, SchemaValidationError, StorageError) as error:
            raise customization_error("cv_customization_anchor") from error
        claims = candidate.get("claims")
        assert isinstance(claims, Sequence) and not isinstance(claims, (str, bytes))
        claim_texts = self._validated_claim_texts(
            claims, customized_tex, evidence, facts
        )

        original = safe_read_relative(manifest_path.parent / "source", Path(expected_tex)).decode(
            "utf-8"
        )
        self._require_new_lines_grounded(original, customized_tex, claim_texts)
        customized_ref = (Path("customized") / Path(expected_tex)).as_posix()
        customized_path = manifest_path.parent / customized_ref
        self._require_beneath(customized_path, manifest_path.parent)
        customization_path = manifest_path.parent / "customization.json"
        digest = hashlib.sha256(customized_tex.encode("utf-8")).hexdigest()
        bound = any(
            manifest.get(key) is not None
            for key in ("customized_tex", "customized_tex_sha256", "claim_evidence_ref")
        )
        replace_allowed = manifest.get("status") == "failed" or not bound
        existing_matches = False
        if customization_path.exists() and customized_path.exists():
            try:
                existing = self.store.read_json(customization_path, "cv-customization.v1")
                existing_tex = safe_read_relative(
                    customized_path.parent, Path(customized_path.name)
                ).decode("utf-8")
            except (CVBuildError, OSError, SchemaValidationError, StorageError, UnicodeError):
                existing = None
                existing_tex = None
            existing_matches = existing == candidate and existing_tex == customized_tex
        if (customization_path.exists() or customized_path.exists()) and not (
            existing_matches or replace_allowed
        ):
            raise customization_error("cv_customization_conflict")

        if bound and not existing_matches and not replace_allowed:
                raise customization_error("cv_customization_conflict")

        self._ensure_directory(customized_path.parent, root=manifest_path.parent)
        if not existing_matches:
            self.store.write_json(customization_path, candidate, "cv-customization.v1")
            copy_bytes_atomic(customized_tex.encode("utf-8"), customized_path)
        updated = dict(manifest)
        updated.update(
            {
                "customized_tex": customized_ref,
                "customized_tex_sha256": digest,
                "claim_evidence_ref": "customization.json",
                "status": "prepared",
                "output_pdf": None,
                "verification": None,
                "updated_at": now(),
            }
        )
        self._atomic_manifest(manifest_path, updated)
        return candidate

    def _customized_source(
        self,
        manifest_path: Path,
        manifest: Mapping[str, object],
        evidence: CVEvidence,
        facts: Mapping[str, object],
    ) -> Path:
        customized_ref = manifest.get("customized_tex")
        expected_digest = manifest.get("customized_tex_sha256")
        claim_ref = manifest.get("claim_evidence_ref")
        if (
            not isinstance(customized_ref, str)
            or not isinstance(expected_digest, str)
            or claim_ref != "customization.json"
        ):
            raise customization_error("cv_customization_source")
        customized_path = manifest_path.parent / customized_ref
        customization_path = manifest_path.parent / str(claim_ref)
        self._require_beneath(customized_path, manifest_path.parent)
        self._require_beneath(customization_path, manifest_path.parent)
        try:
            customized_tex = safe_read_relative(
                customized_path.parent, Path(customized_path.name)
            ).decode("utf-8")
            request = self.store.read_json(customization_path, "cv-customization.v1")
        except (CVBuildError, OSError, SchemaValidationError, StorageError, UnicodeError) as error:
            raise customization_error("cv_customization_source") from error
        if (
            hashlib.sha256(customized_tex.encode("utf-8")).hexdigest() != expected_digest
            or request.get("customized_tex") != customized_tex
            or request.get("run_id") != manifest.get("run_id")
            or request.get("cv_name") != manifest.get("source_cv_name")
            or request.get("source_hash") != manifest.get("source_hash")
        ):
            raise customization_error("cv_customization_source")
        source_hashes = manifest.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            raise customization_error("cv_customization_source")
        asset_refs = {
            str(reference)
            for reference in source_hashes
            if Path(str(reference)).suffix == ".png"
        }
        try:
            validate_tex_bytes(customized_tex.encode("utf-8"), asset_refs)
        except CVBuildError as error:
            raise customization_error("cv_customization_invalid") from error
        claims = request.get("claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise customization_error("cv_customization_invalid")
        claim_texts = self._validated_claim_texts(
            claims, customized_tex, evidence, facts
        )
        expected_tex = manifest.get("expected_tex")
        if not isinstance(expected_tex, str):
            raise customization_error("cv_customization_source")
        try:
            original = safe_read_relative(
                manifest_path.parent / "source", Path(expected_tex)
            ).decode("utf-8")
        except (CVBuildError, UnicodeError) as error:
            raise customization_error("cv_customization_source") from error
        self._require_new_lines_grounded(original, customized_tex, claim_texts)
        return customized_path

    @classmethod
    def _validated_claim_texts(
        cls,
        claims: Sequence[object],
        customized_tex: str,
        evidence: CVEvidence,
        facts: Mapping[str, object],
    ) -> set[str]:
        claim_texts: set[str] = set()
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise customization_error("cv_customization_invalid")
            text = claim.get("claim")
            anchors = claim.get("evidence_anchors")
            fact_refs = claim.get("fact_refs")
            if (
                not isinstance(text, str)
                or text not in customized_tex
                or not isinstance(anchors, Sequence)
                or isinstance(anchors, (str, bytes))
                or not isinstance(fact_refs, Sequence)
                or isinstance(fact_refs, (str, bytes))
            ):
                raise customization_error("cv_customization_anchor")
            records = [cls._fact_record(facts, reference) for reference in fact_refs]
            known_anchors = {
                record.get("evidence_anchor")
                for record in records
                if isinstance(record.get("evidence_anchor"), str)
            }
            if any(
                not isinstance(anchor, str)
                or anchor not in known_anchors
                or anchor not in evidence.extracted_text
                for anchor in anchors
            ):
                raise customization_error("cv_customization_anchor")
            support = " ".join(cls._string_values(records))
            support_tokens = cls._grounding_tokens(support)
            claim_tokens = cls._grounding_tokens(text)
            if claim_tokens - support_tokens - _GROUNDING_GLUE:
                raise customization_error("cv_customization_anchor")
            claim_texts.add(text.strip())
        return claim_texts

    @staticmethod
    def _grounding_tokens(value: str) -> set[str]:
        return {
            token.lower()
            for token in _TOKEN.findall(value.replace(r"\%", "%"))
        }

    @staticmethod
    def _fact_record(facts: Mapping[str, object], reference: object) -> Mapping[str, object]:
        if not isinstance(reference, str) or not reference.startswith("/"):
            raise customization_error("cv_customization_anchor")
        current: object = facts
        for part in reference[1:].split("/"):
            if isinstance(current, Mapping):
                current = current.get(part)
            elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
                try:
                    current = current[int(part)]
                except (IndexError, ValueError):
                    raise customization_error("cv_customization_anchor") from None
            else:
                raise customization_error("cv_customization_anchor")
        if not isinstance(current, Mapping):
            raise customization_error("cv_customization_anchor")
        return current

    @staticmethod
    def _string_values(value: object):
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for child in value.values():
                yield from CVCustomizeMixin._string_values(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                yield from CVCustomizeMixin._string_values(child)

    @staticmethod
    def _require_new_lines_grounded(
        original: str, customized: str, claim_texts: set[str]
    ) -> None:
        original_lines = Counter(
            line.strip()
            for line in original.splitlines()
            if line.strip() and not line.lstrip().startswith("%")
        )
        customized_lines = Counter(
            line.strip()
            for line in customized.splitlines()
            if line.strip() and not line.lstrip().startswith("%")
        )
        additions = customized_lines - original_lines
        for line in additions.elements():
            if line not in claim_texts:
                raise customization_error("cv_customization_anchor")
