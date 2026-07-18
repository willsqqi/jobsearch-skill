"""Semantic-only, side-effect-free form fill planning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

from jobsearch_skill.errors import ProfileLookupError, SchemaValidationError
from jobsearch_skill.profile import lookup_profile_value
from jobsearch_skill.questions import QuestionMemory, QuestionMemoryError, normalize_question, question_query, validate_reuse
from jobsearch_skill.schema import SchemaRegistry


_CV_KEY = re.compile(r"^(identity\.(?:full_name|email|phone|location)|(education|employment)\.(0|[1-9][0-9]*)\.(institution|company|degree|field_of_study|title|location|start_date|end_date|highlights))$")
_SELECTION_TYPES = {"selection", "multi_selection"}


def _unresolved(field: Mapping[str, object], reason_code: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"field_id": field["field_id"], "action": "ask" if field["required"] else "leave_empty"}
    if result["action"] == "ask" and reason_code:
        result["reason_code"] = reason_code
    return result


def _matches_answer_type(value: object, answer_type: object) -> bool:
    if answer_type == "boolean":
        return isinstance(value, bool)
    if answer_type in {"string", "free_text", "selection", "date"}:
        return isinstance(value, str)
    if answer_type == "multi_selection":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return answer_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)


def _selection_value(
    value: object, options: object, answer_type: object, control_type: object
) -> tuple[bool, object]:
    """Return an observed control value only for an exact normalized membership match."""

    if answer_type not in _SELECTION_TYPES and control_type not in {"select", "radio", "checkbox"}:
        return True, value
    if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
        return False, None
    normalized: dict[str, list[str]] = {}
    for option in options:
        if not isinstance(option, str):
            return False, None
        key = normalize_question(option)
        normalized.setdefault(key, []).append(option)
    if any(len(matches) != 1 for matches in normalized.values()):
        return False, None
    if answer_type == "selection":
        if not isinstance(value, str):
            return False, None
        match = normalized.get(normalize_question(value), [])
        return (len(match) == 1, match[0] if len(match) == 1 else None)
    if answer_type != "multi_selection":
        return False, None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return False, None
    observed: list[str] = []
    seen: set[str] = set()
    for item in value:
        key = normalize_question(item)
        match = normalized.get(key, [])
        if len(match) != 1 or key in seen:
            return False, None
        seen.add(key)
        observed.append(match[0])
    return True, observed


class FormService:
    """Plan safe values from supplied semantic keys; never inspect display labels."""

    def __init__(
        self,
        registry: SchemaRegistry,
        *,
        profile: Mapping[str, object],
        cv_facts: Mapping[str, object],
        questions: QuestionMemory,
        job_context: Mapping[str, object],
        run_id: str,
    ) -> None:
        self.registry = registry
        self.profile = profile
        self.cv_facts = cv_facts
        self.questions = questions
        self.job_context = job_context
        self.run_id = run_id

    def plan(self, snapshot: Mapping[str, object]) -> dict[str, object]:
        self.registry.validate("form-snapshot.v1", snapshot)
        if snapshot.get("run_id") != self.run_id:
            raise SchemaValidationError(
                "run_id_mismatch: snapshot belongs to another run", reason_code="run_id_mismatch"
            )
        fields = snapshot.get("fields")
        if not isinstance(fields, list):  # schema validation above makes this defensive.
            raise SchemaValidationError("schema_validation: fields are unavailable", reason_code="schema_validation")
        field_ids = [field.get("field_id") for field in fields if isinstance(field, Mapping)]
        if len(field_ids) != len(set(field_ids)):
            raise SchemaValidationError(
                "duplicate_field_id: field IDs must be unique", reason_code="duplicate_field_id"
            )
        decisions = [self._decide(field) for field in fields if isinstance(field, Mapping)]
        result: dict[str, object] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "page_id": snapshot["page_id"],
            "decisions": decisions,
            "stop_before_submit": True,
        }
        self.registry.validate("fill-plan.v1", result)
        return result

    def _decide(self, field: Mapping[str, object]) -> dict[str, object]:
        control_type = field["control_type"]
        if control_type == "file":
            return {"field_id": field["field_id"], "action": "manual_upload"}
        if control_type == "submit":
            return {"field_id": field["field_id"], "action": "manual_submit"}
        semantic_key = field.get("semantic_key")
        if isinstance(semantic_key, str):
            profile_decision = self._profile_decision(field, semantic_key)
            if profile_decision is not None:
                return profile_decision
            cv_decision = self._cv_decision(field, semantic_key)
            if cv_decision is not None:
                return cv_decision
        descriptor = field.get("question_reuse")
        if isinstance(descriptor, Mapping):
            return self._question_decision(field, descriptor)
        if isinstance(semantic_key, str):
            return _unresolved(field, "unknown_semantic_key")
        return _unresolved(field)

    def _profile_decision(self, field: Mapping[str, object], key: str) -> dict[str, object] | None:
        try:
            value = lookup_profile_value(self.profile, key)
        except ProfileLookupError as error:
            if error.reason_code == "profile_path_missing":
                return _unresolved(field, "missing_profile_value")
            return None
        return self._fill_or_unresolved(field, value, {"kind": "profile", "ref": key}, "direct")

    def _cv_decision(self, field: Mapping[str, object], key: str) -> dict[str, object] | None:
        match = _CV_KEY.fullmatch(key)
        if match is None:
            return None
        parts = key.split(".")
        value: object
        if parts[0] == "identity":
            identity = self.cv_facts.get("identity")
            item = identity.get(parts[1]) if isinstance(identity, Mapping) else None
            value = item.get("value") if isinstance(item, Mapping) else None
        else:
            rows = self.cv_facts.get(parts[0])
            index = int(parts[1])
            item = rows[index] if isinstance(rows, list) and index < len(rows) else None
            value = item.get(parts[2]) if isinstance(item, Mapping) else None
        if value is None:
            return _unresolved(field, "missing_cv_value")
        return self._fill_or_unresolved(field, value, {"kind": "cv", "ref": key}, "direct")

    def _question_decision(
        self, field: Mapping[str, object], descriptor: Mapping[str, object]
    ) -> dict[str, object]:
        canonical_id = descriptor.get("canonical_source_id")
        if not isinstance(canonical_id, str):
            return _unresolved(field, "canonical_source_missing")
        try:
            query = question_query(
                {
                    "wording": descriptor["wording"],
                    "answer_type": field["answer_type"],
                    "scope": descriptor["scope"],
                    "qualifiers": descriptor["qualifiers"],
                }
            )
            matched = self.questions.match(query)
            if matched.kind == "ambiguous":
                return _unresolved(field, matched.reason_code)
            if matched.canonical_id is not None and matched.canonical_id != canonical_id:
                return _unresolved(field, "canonical_source_mismatch")
            candidate = self.questions.get(canonical_id)
        except (KeyError, QuestionMemoryError, SchemaValidationError) as error:
            reason = error.reason_code if isinstance(error, (QuestionMemoryError, SchemaValidationError)) else "question_unseen"
            return _unresolved(field, reason)
        validation = validate_reuse(
            candidate,
            {"answer_type": query.answer_type, "scope": query.scope, "qualifiers": query.qualifiers},
        )
        if not validation.allowed:
            return _unresolved(field, validation.reason_code)
        answer = candidate.get("answer")
        value = answer.get("value") if isinstance(answer, Mapping) else None
        match_kind = matched.kind if matched.kind in {"exact", "alias"} else "semantic"
        return self._fill_or_unresolved(
            field,
            value,
            {"kind": "question", "ref": canonical_id},
            match_kind,
            canonical_source_id=canonical_id,
        )

    def _fill_or_unresolved(
        self,
        field: Mapping[str, object],
        value: object,
        source: dict[str, str],
        match_kind: str,
        *,
        canonical_source_id: str | None = None,
    ) -> dict[str, object]:
        if not _matches_answer_type(value, field["answer_type"]):
            return _unresolved(field, "answer_type_mismatch")
        selected, observed = _selection_value(
            value, field["options"], field["answer_type"], field["control_type"]
        )
        if not selected:
            return _unresolved(field, "value_not_in_options")
        result: dict[str, object] = {
            "field_id": field["field_id"],
            "action": "fill",
            "value": observed,
            "source": source,
            "match_kind": match_kind,
            "review_required": False,
        }
        if canonical_source_id is not None:
            result["canonical_source_id"] = canonical_source_id
        return result
