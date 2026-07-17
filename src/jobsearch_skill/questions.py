"""Deterministic learned-question matching and reviewed-only persistence."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from jobsearch_skill.errors import JobsearchError, SchemaValidationError, StorageError
from jobsearch_skill.storage import SafeStore

_ANSWER_TYPES = {"boolean", "string", "selection", "multi_selection", "number", "date", "free_text"}
_SCOPE_KEYS = ("company", "role", "job_fingerprint")
_REQUIRED_SCOPE_KEYS = {"company": "company", "role": "role", "job": "job_fingerprint"}
_QUALIFIER_REASONS = (
    ("negated", "negation_mismatch"),
    ("jurisdiction", "jurisdiction_mismatch"),
    ("time_period", "time_period_mismatch"),
    ("unit", "unit_mismatch"),
)
_MISSING = object()


class QuestionMemoryError(JobsearchError):
    exit_code = 4


@dataclass(frozen=True)
class QuestionQuery:
    wording: str
    answer_type: str
    scope: Mapping[str, object]
    qualifiers: Mapping[str, object]


@dataclass(frozen=True)
class MatchResult:
    kind: Literal["exact", "alias", "ambiguous", "unseen"]
    canonical_id: str | None
    candidates: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True)
class ReuseResult:
    allowed: bool
    reason_code: str


@dataclass(frozen=True)
class SyncEntryResult:
    canonical_id: str
    action: Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class SyncResult:
    entries: tuple[SyncEntryResult, ...]

    @property
    def created(self) -> int:
        return sum(entry.action == "created" for entry in self.entries)

    @property
    def updated(self) -> int:
        return sum(entry.action == "updated" for entry in self.entries)

    @property
    def unchanged(self) -> int:
        return sum(entry.action == "unchanged" for entry in self.entries)


def normalize_question(wording: str) -> str:
    """Fold display-only differences while retaining semantic tokens."""
    if not isinstance(wording, str) or not wording.strip():
        raise SchemaValidationError(
            "question_input_invalid: wording must be nonempty",
            reason_code="question_input_invalid",
            field_path="$.wording",
        )
    value = unicodedata.normalize("NFKC", wording).casefold()
    value = re.sub(
        r"(?<!\w)(?:[^\W\d_]\.)+[^\W\d_](?:\.)?",
        lambda match: match.group(0).replace(".", ""),
        value,
    )
    output: list[str] = []
    for index, character in enumerate(value):
        category = unicodedata.category(character)
        before = value[index - 1] if index else ""
        after = value[index + 1] if index + 1 < len(value) else ""
        numeric_separator = character in {".", ","} and before.isdigit() and after.isdigit()
        if character.isalnum() or category.startswith("M") or category == "Sc":
            output.append(character)
        elif (
            character in {"%", "‰", "‱", "°"}
            or category == "Sm"
            or numeric_separator
            or (character == "-" and after.isdigit())
        ):
            output.append(character)
        else:
            output.append(" ")
    return " ".join("".join(output).split())


def canonical_question_id(wording: str, answer_type: str, scope: Mapping[str, object]) -> str:
    identity = json.dumps(
        {
            "answer_type": answer_type,
            "normalized_wording": normalize_question(wording),
            "scope": dict(scope),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "q_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _comparable(value: object) -> object:
    return normalize_question(value) if isinstance(value, str) else value


def validate_reuse(candidate: Mapping[str, object], proposal: Mapping[str, object]) -> ReuseResult:
    """Validate a supplied candidate; never discover semantic equivalence."""
    if candidate.get("answer_type", _MISSING) != proposal.get("answer_type", _MISSING):
        return ReuseResult(False, "answer_type_mismatch")
    candidate_scope = candidate.get("scope")
    proposal_scope = proposal.get("scope")
    if not isinstance(candidate_scope, Mapping) or not isinstance(proposal_scope, Mapping):
        return ReuseResult(False, "scope_kind_mismatch")
    if candidate_scope.get("kind", _MISSING) != proposal_scope.get("kind", _MISSING):
        return ReuseResult(False, "scope_kind_mismatch")
    required_key = _REQUIRED_SCOPE_KEYS.get(str(candidate_scope.get("kind")))
    if required_key and (
        required_key not in candidate_scope or required_key not in proposal_scope
    ):
        return ReuseResult(False, "scope_key_mismatch")
    if any(candidate_scope.get(key, _MISSING) != proposal_scope.get(key, _MISSING) for key in _SCOPE_KEYS):
        return ReuseResult(False, "scope_key_mismatch")
    candidate_qualifiers = candidate.get("qualifiers")
    proposal_qualifiers = proposal.get("qualifiers")
    if not isinstance(candidate_qualifiers, Mapping) or not isinstance(proposal_qualifiers, Mapping):
        return ReuseResult(False, "negation_mismatch")
    for key, reason_code in _QUALIFIER_REASONS:
        left = candidate_qualifiers.get(key, _MISSING)
        right = proposal_qualifiers.get(key, _MISSING)
        if left is _MISSING and right is _MISSING:
            continue
        if left is _MISSING or right is _MISSING or _comparable(left) != _comparable(right):
            return ReuseResult(False, reason_code)
    return ReuseResult(True, "compatible")


def question_query(value: Mapping[str, object]) -> QuestionQuery:
    expected = {"wording", "answer_type", "scope", "qualifiers"}
    wording = value.get("wording")
    answer_type = value.get("answer_type")
    scope = value.get("scope")
    qualifiers = value.get("qualifiers")
    if (
        set(value) != expected
        or not isinstance(wording, str)
        or not wording.strip()
        or answer_type not in _ANSWER_TYPES
        or not isinstance(scope, Mapping)
        or not isinstance(qualifiers, Mapping)
        or set(scope) - {"kind", *_SCOPE_KEYS}
        or scope.get("kind") not in {"global", "company", "role", "job"}
        or set(qualifiers) - {key for key, _ in _QUALIFIER_REASONS}
        or not isinstance(qualifiers.get("negated"), bool)
    ):
        raise SchemaValidationError(
            "question_input_invalid: query shape is invalid", reason_code="question_input_invalid"
        )
    if any(key in scope and not isinstance(scope[key], str) for key in _SCOPE_KEYS):
        raise SchemaValidationError(
            "question_input_invalid: scope is invalid", reason_code="question_input_invalid"
        )
    required_scope_key = _REQUIRED_SCOPE_KEYS.get(str(scope.get("kind")))
    if required_scope_key and not scope.get(required_scope_key):
        raise SchemaValidationError(
            "question_input_invalid: scope key is missing", reason_code="question_input_invalid"
        )
    if any(
        key in qualifiers and not isinstance(qualifiers[key], str)
        for key in ("jurisdiction", "time_period", "unit")
    ):
        raise SchemaValidationError(
            "question_input_invalid: qualifier is invalid", reason_code="question_input_invalid"
        )
    return QuestionQuery(wording, str(answer_type), dict(scope), dict(qualifiers))


def load_mapping(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise StorageError("storage_read: unable to read input", reason_code="storage_read") from error
    if not isinstance(value, dict):
        raise SchemaValidationError(
            "question_input_invalid: input must be an object", reason_code="question_input_invalid"
        )
    return value


class QuestionMemory:
    def __init__(self, store: SafeStore, path: Path | None = None) -> None:
        self.store = store
        self.path = path or store.backup_dir.parent / "questions.yaml"

    def _document(self) -> dict[str, object]:
        return self.store.read_yaml(self.path, "questions.v1")

    def all(self) -> list[dict[str, object]]:
        return copy.deepcopy(self._document()["questions"])  # type: ignore[return-value]

    def get(self, canonical_id: str) -> dict[str, object]:
        matches = [record for record in self.all() if record["canonical_id"] == canonical_id]
        if not matches:
            raise QuestionMemoryError(
                "canonical_source_missing: canonical question unavailable",
                reason_code="canonical_source_missing",
            )
        if len(matches) > 1:
            raise QuestionMemoryError(
                "question_ambiguous: duplicate canonical identity",
                reason_code="question_ambiguous",
            )
        return matches[0]

    @staticmethod
    def _match_records(records: list[dict[str, object]], query: QuestionQuery) -> MatchResult:
        normalized = normalize_question(query.wording)
        matches: list[tuple[str, Literal["exact", "alias"]]] = []
        proposal = {"answer_type": query.answer_type, "scope": query.scope, "qualifiers": query.qualifiers}
        for record in records:
            if not validate_reuse(record, proposal).allowed:
                continue
            canonical_id = str(record["canonical_id"])
            if normalize_question(str(record["canonical_wording"])) == normalized:
                matches.append((canonical_id, "exact"))
            elif any(normalize_question(str(wording)) == normalized for wording in record["observed_wordings"]):
                matches.append((canonical_id, "alias"))
        matches.sort(key=lambda item: (item[0], item[1]))
        candidates = tuple(canonical_id for canonical_id, _ in matches)
        if not candidates:
            return MatchResult("unseen", None, (), "question_unseen")
        if len(candidates) > 1:
            return MatchResult("ambiguous", None, candidates, "question_ambiguous")
        canonical_id = candidates[0]
        kind = matches[0][1]
        return MatchResult(kind, canonical_id, candidates, f"question_{kind}")

    def match(self, query: QuestionQuery) -> MatchResult:
        return self._match_records(self.all(), query)

    @staticmethod
    def _merge_unique(existing: object, additions: object) -> list[str]:
        result = list(existing) if isinstance(existing, list) else []
        for item in additions if isinstance(additions, list) else []:
            if isinstance(item, str) and item not in result:
                result.append(item)
        return result

    @classmethod
    def _apply_entry(cls, record: dict[str, object], entry: Mapping[str, object]) -> str:
        changed = False
        observed = record["observed_wordings"]
        wording = str(entry["wording"])
        if isinstance(observed, list) and wording not in observed:
            observed.append(wording)
            changed = True
        for key in ("topic_tags", "role_tags"):
            merged = cls._merge_unique(record.get(key), entry.get(key))
            if merged != record.get(key):
                record[key] = merged
                changed = True
        answer = record["answer"]
        history = record["history"]
        if not isinstance(answer, dict) or not isinstance(history, list):
            raise QuestionMemoryError("question_record_invalid: invalid record", reason_code="question_record_invalid")
        if answer.get("value") != entry["value"] or answer.get("source") != entry["source"]:
            history.append(copy.deepcopy(answer))
            record["answer"] = {
                "value": copy.deepcopy(entry["value"]),
                "source": entry["source"],
                "updated_at": entry["reviewed_at"],
            }
            changed = True
        if changed:
            record["updated_at"] = entry["reviewed_at"]
        return "updated" if changed else "unchanged"

    @staticmethod
    def _new_record(entry: Mapping[str, object], canonical_id: str) -> dict[str, object]:
        return {
            "canonical_id": canonical_id,
            "canonical_wording": entry["wording"],
            "observed_wordings": [entry["wording"]],
            "answer_type": entry["answer_type"],
            "scope": copy.deepcopy(entry["scope"]),
            "qualifiers": copy.deepcopy(entry["qualifiers"]),
            "topic_tags": list(entry["topic_tags"]),
            "role_tags": list(entry["role_tags"]),
            "answer": {"value": copy.deepcopy(entry["value"]), "source": entry["source"], "updated_at": entry["reviewed_at"]},
            "created_at": entry["reviewed_at"],
            "updated_at": entry["reviewed_at"],
            "history": [],
        }

    def sync(self, reviewed: Mapping[str, object]) -> SyncResult:
        self.store.registry.validate("reviewed-answers.v1", reviewed)
        document = self._document()
        records = copy.deepcopy(document["questions"])
        entries = reviewed["entries"]
        if not isinstance(records, list) or not isinstance(entries, list):
            raise SchemaValidationError("schema_validation: invalid document", reason_code="schema_validation")
        results: list[SyncEntryResult] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise SchemaValidationError("schema_validation: invalid entry", reason_code="schema_validation")
            source_id = entry.get("canonical_source_id")
            if isinstance(source_id, str):
                source_records = [
                    item for item in records if item.get("canonical_id") == source_id
                ]
                if not source_records:
                    raise QuestionMemoryError("canonical_source_missing: unavailable", reason_code="canonical_source_missing")
                if len(source_records) > 1:
                    raise QuestionMemoryError(
                        "question_ambiguous: duplicate canonical identity",
                        reason_code="question_ambiguous",
                    )
                record = source_records[0]
                compatibility = validate_reuse(record, entry)
                if not compatibility.allowed:
                    raise QuestionMemoryError(f"{compatibility.reason_code}: incompatible", reason_code=compatibility.reason_code)
                results.append(SyncEntryResult(source_id, self._apply_entry(record, entry)))
                continue
            query = question_query(
                {
                    "wording": entry["wording"],
                    "answer_type": entry["answer_type"],
                    "scope": entry["scope"],
                    "qualifiers": entry["qualifiers"],
                }
            )
            match = self._match_records(records, query)
            if match.kind == "ambiguous":
                raise QuestionMemoryError("question_ambiguous: multiple records", reason_code="question_ambiguous")
            if match.canonical_id:
                record = next(item for item in records if item.get("canonical_id") == match.canonical_id)
                results.append(SyncEntryResult(match.canonical_id, self._apply_entry(record, entry)))
                continue
            canonical_id = canonical_question_id(str(entry["wording"]), str(entry["answer_type"]), entry["scope"])  # type: ignore[arg-type]
            if any(item.get("canonical_id") == canonical_id for item in records):
                raise QuestionMemoryError("canonical_id_conflict: identity conflict", reason_code="canonical_id_conflict")
            records.append(self._new_record(entry, canonical_id))
            results.append(SyncEntryResult(canonical_id, "created"))
        replacement = {"schema_version": 1, "questions": records}
        if replacement != document:
            self.store.write_yaml(self.path, replacement, "questions.v1")
        return SyncResult(tuple(results))
