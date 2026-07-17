from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch_skill.questions import QuestionMemory, QuestionQuery, validate_reuse
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


TIMESTAMP = "2026-07-18T00:00:00Z"


def question_record(
    canonical_id: str = "q_synthetic_auth",
    *,
    canonical_wording: str = "Are you authorized to work in the US?",
    aliases: list[str] | None = None,
    answer_type: str = "boolean",
    answer_value: object = True,
    scope: dict[str, object] | None = None,
    qualifiers: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "canonical_id": canonical_id,
        "canonical_wording": canonical_wording,
        "observed_wordings": aliases or [canonical_wording],
        "answer_type": answer_type,
        "scope": scope or {"kind": "global"},
        "qualifiers": qualifiers or {"negated": False, "jurisdiction": "US"},
        "topic_tags": [],
        "role_tags": [],
        "answer": {"value": answer_value, "source": "user", "updated_at": TIMESTAMP},
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
        "history": [],
    }


def proposal(
    *,
    answer_type: str = "boolean",
    scope: dict[str, object] | None = None,
    qualifiers: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "answer_type": answer_type,
        "scope": scope or {"kind": "global"},
        "qualifiers": qualifiers or {"negated": False, "jurisdiction": "US"},
    }


@pytest.mark.parametrize(
    ("candidate_changes", "proposal_changes", "reason_code"),
    [
        ({}, {"answer_type": "string"}, "answer_type_mismatch"),
        (
            {"scope": {"kind": "company", "company": "Synthetic Systems"}},
            {"scope": {"kind": "role", "role": "Engineer"}},
            "scope_kind_mismatch",
        ),
        (
            {"scope": {"kind": "company", "company": "Synthetic Systems"}},
            {"scope": {"kind": "company", "company": "Other Systems"}},
            "scope_key_mismatch",
        ),
        (
            {"qualifiers": {"negated": False}},
            {"qualifiers": {"negated": True}},
            "negation_mismatch",
        ),
        (
            {"qualifiers": {"negated": False, "jurisdiction": "US"}},
            {"qualifiers": {"negated": False, "jurisdiction": "CA"}},
            "jurisdiction_mismatch",
        ),
        (
            {"qualifiers": {"negated": False, "time_period": "year"}},
            {"qualifiers": {"negated": False, "time_period": "month"}},
            "time_period_mismatch",
        ),
        (
            {"qualifiers": {"negated": False, "unit": "USD"}},
            {"qualifiers": {"negated": False, "unit": "CAD"}},
            "unit_mismatch",
        ),
    ],
)
def test_reuse_rejects_each_incompatible_dimension(
    candidate_changes: dict[str, object],
    proposal_changes: dict[str, object],
    reason_code: str,
) -> None:
    candidate = question_record(**candidate_changes)
    proposed = proposal(**proposal_changes)

    result = validate_reuse(candidate, proposed)

    assert result.allowed is False
    assert result.reason_code == reason_code


@pytest.mark.parametrize("qualifier", ["jurisdiction", "time_period", "unit"])
@pytest.mark.parametrize("missing_from", ["candidate", "proposal"])
def test_reuse_treats_missing_and_explicit_qualifiers_conservatively(
    qualifier: str, missing_from: str
) -> None:
    candidate_qualifiers: dict[str, object] = {"negated": False, qualifier: "synthetic"}
    proposal_qualifiers: dict[str, object] = {"negated": False, qualifier: "synthetic"}
    (candidate_qualifiers if missing_from == "candidate" else proposal_qualifiers).pop(qualifier)

    result = validate_reuse(
        question_record(qualifiers=candidate_qualifiers),
        proposal(qualifiers=proposal_qualifiers),
    )

    assert result.allowed is False
    assert result.reason_code == f"{qualifier}_mismatch"


def test_reuse_accepts_compatible_acronym_qualifier() -> None:
    result = validate_reuse(
        question_record(qualifiers={"negated": False, "jurisdiction": "U.S."}),
        proposal(qualifiers={"negated": False, "jurisdiction": "US"}),
    )

    assert result.allowed is True
    assert result.reason_code == "compatible"


def test_reuse_rejects_scope_kind_without_its_required_key() -> None:
    result = validate_reuse(
        question_record(scope={"kind": "company"}),
        proposal(scope={"kind": "company"}),
    )

    assert result.allowed is False
    assert result.reason_code == "scope_key_mismatch"


@pytest.fixture
def memory(tmp_path: Path) -> QuestionMemory:
    store = SafeStore(SchemaRegistry(), tmp_path / "backups")
    path = tmp_path / "questions.yaml"
    store.write_yaml(
        path,
        {
            "schema_version": 1,
            "questions": [
                question_record(
                    aliases=[
                        "Are you authorized to work in the US?",
                        "Do you have U.S. work authorization?",
                    ]
                )
            ],
        },
        "questions.v1",
    )
    return QuestionMemory(store, path)


def query(wording: str, **changes: object) -> QuestionQuery:
    values: dict[str, object] = {
        "wording": wording,
        "answer_type": "boolean",
        "scope": {"kind": "global"},
        "qualifiers": {"negated": False, "jurisdiction": "US"},
    }
    values.update(changes)
    return QuestionQuery(**values)  # type: ignore[arg-type]


def test_exact_match_returns_canonical_source_without_answer(memory: QuestionMemory) -> None:
    result = memory.match(query("ARE YOU AUTHORIZED TO WORK IN THE U.S.???"))

    assert result.kind == "exact"
    assert result.canonical_id == "q_synthetic_auth"
    assert result.candidates == ("q_synthetic_auth",)
    assert not hasattr(result, "value")


def test_alias_match_returns_canonical_source(memory: QuestionMemory) -> None:
    result = memory.match(query("Do you have US work authorization?"))

    assert result.kind == "alias"
    assert result.canonical_id == "q_synthetic_auth"


def test_incompatible_wording_match_is_unseen(memory: QuestionMemory) -> None:
    result = memory.match(query("Do you have US work authorization?", answer_type="string"))

    assert result.kind == "unseen"
    assert result.canonical_id is None
    assert result.candidates == ()


def test_unseen_question_has_no_canonical_source(memory: QuestionMemory) -> None:
    result = memory.match(query("Have you worked at Synthetic Systems before?"))

    assert result.kind == "unseen"
    assert result.canonical_id is None


def test_multiple_compatible_matches_are_ambiguous_and_sorted(tmp_path: Path) -> None:
    store = SafeStore(SchemaRegistry(), tmp_path / "backups")
    path = tmp_path / "questions.yaml"
    store.write_yaml(
        path,
        {
            "schema_version": 1,
            "questions": [
                question_record("q_zed"),
                question_record("q_alpha"),
                question_record(
                    "q_incompatible",
                    answer_type="string",
                    answer_value="Synthetic answer",
                    qualifiers={"negated": False, "jurisdiction": "US"},
                ),
            ],
        },
        "questions.v1",
    )

    result = QuestionMemory(store, path).match(
        query("Are you authorized to work in the U.S.?")
    )

    assert result.kind == "ambiguous"
    assert result.canonical_id is None
    assert result.candidates == ("q_alpha", "q_zed")


def test_duplicate_canonical_ids_do_not_collapse_multiple_compatible_records(
    tmp_path: Path,
) -> None:
    store = SafeStore(SchemaRegistry(), tmp_path / "backups")
    path = tmp_path / "questions.yaml"
    store.write_yaml(
        path,
        {
            "schema_version": 1,
            "questions": [question_record("q_duplicate"), question_record("q_duplicate")],
        },
        "questions.v1",
    )

    result = QuestionMemory(store, path).match(
        query("Are you authorized to work in the U.S.?")
    )

    assert result.kind == "ambiguous"
    assert result.candidates == ("q_duplicate", "q_duplicate")
