from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch_skill.forms import FormService
from jobsearch_skill.questions import QuestionMemory
from jobsearch_skill.errors import SchemaValidationError
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _field(field_id: str, semantic_key: str | None = None, **changes: object) -> dict[str, object]:
    field: dict[str, object] = {
        "field_id": field_id,
        "label": "Synthetic label",
        "control_type": "text",
        "answer_type": "string",
        "required": True,
        "options": [],
    }
    if semantic_key is not None:
        field["semantic_key"] = semantic_key
    field.update(changes)
    return field


@pytest.fixture
def form_service(tmp_path: Path) -> FormService:
    store = SafeStore(SchemaRegistry(), tmp_path / "backups")
    questions_path = tmp_path / "questions.yaml"
    store.write_yaml(questions_path, {"schema_version": 1, "questions": []}, "questions.v1")
    profile: dict[str, object] = {
        "schema_version": 1,
        "identity": {"full_name": "Synthetic Person"},
        "contact": {},
        "address": {},
        "work_authorization": {"USA": {"sponsorship_required": False}},
        "compensation": {"target": 123},
        "demographics": {"gender": "Synthetic"},
        "disability": {"status": "Synthetic"},
        "veteran": {"status": "Synthetic"},
        "legal_attestations": {"accurate_information": True},
        "links": {},
    }
    facts: dict[str, object] = {
        "schema_version": 1,
        "run_id": "run_synthetic",
        "cv_name": "Synthetic CV",
        "source_hash": "a" * 64,
        "source_hashes": [{"source_ref": "resume.tex", "sha256": "a" * 64}],
        "identity": {"full_name": {"value": "Synthetic Person", "evidence_anchor": "Synthetic"}},
        "education": [
            {
                "institution": "Example University",
                "degree": "MS",
                "start_date": "2024-01-01",
                "end_date": "2026-01-01",
                "evidence_anchor": "Example University",
            }
        ],
        "employment": [
            {
                "company": "Example Company",
                "title": "Software Engineering Intern",
                "start_date": "2025-01-01",
                "end_date": "2025-08-01",
                "evidence_anchor": "Example Company",
            }
        ],
        "skills": [],
        "projects": [],
    }
    return FormService(
        store.registry,
        profile=profile,
        cv_facts=facts,
        questions=QuestionMemory(store, questions_path),
        job_context={
            "company": "Synthetic Company",
            "role": "Synthetic Role",
            "job_fingerprint": "sha256:" + "c" * 64,
        },
        run_id="run_synthetic",
    )


def _snapshot(*fields: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run_synthetic",
        "platform": "workday",
        "page_id": "synthetic-page",
        "url": "https://example.invalid/application",
        "fields": list(fields),
    }


def _decision(plan: dict[str, object], field_id: str) -> dict[str, object]:
    decisions = plan["decisions"]
    assert isinstance(decisions, list)
    return next(item for item in decisions if item["field_id"] == field_id)


def test_file_and_submit_controls_are_always_manual(form_service: FormService) -> None:
    plan = form_service.plan(
        _snapshot(
            _field("resume-upload", "identity.full_name", control_type="file"),
            _field("final-submit", "identity.full_name", control_type="submit"),
        )
    )

    assert _decision(plan, "resume-upload") == {"field_id": "resume-upload", "action": "manual_upload"}
    assert _decision(plan, "final-submit") == {"field_id": "final-submit", "action": "manual_submit"}
    assert plan["stop_before_submit"] is True


def test_unknown_and_optional_unsupplied_keys_never_leak_candidates(form_service: FormService) -> None:
    plan = form_service.plan(
        _snapshot(
            _field("required-unknown", "unapproved.private"),
            _field("optional-empty", required=False),
        )
    )

    assert _decision(plan, "required-unknown") == {
        "field_id": "required-unknown",
        "action": "ask",
        "reason_code": "unknown_semantic_key",
    }
    assert _decision(plan, "optional-empty") == {"field_id": "optional-empty", "action": "leave_empty"}


@pytest.mark.parametrize(
    "semantic_key",
    [
        "compensation.target",
        "work_authorization.USA.sponsorship_required",
        "demographics.gender",
        "disability.status",
        "veteran.status",
        "legal_attestations.accurate_information",
    ],
)
def test_profile_sensitive_categories_have_profile_provenance(
    form_service: FormService, semantic_key: str
) -> None:
    value = form_service.plan(_snapshot(_field("sensitive", semantic_key, answer_type="number" if semantic_key == "compensation.target" else "boolean" if semantic_key.endswith(("sponsorship_required", "accurate_information")) else "string")))
    decision = _decision(value, "sensitive")

    assert decision["action"] == "fill"
    assert decision["source"] == {"kind": "profile", "ref": semantic_key}


def test_selection_requires_one_unambiguous_observed_option(form_service: FormService) -> None:
    plan = form_service.plan(
        _snapshot(
            _field(
                "selection-question",
                "demographics.gender",
                control_type="select",
                answer_type="selection",
                options=["Different value"],
            )
        )
    )

    assert _decision(plan, "selection-question") == {
        "field_id": "selection-question",
        "action": "ask",
        "reason_code": "value_not_in_options",
    }


def test_boolean_radio_requires_an_explicit_observed_value_not_a_guess(
    form_service: FormService,
) -> None:
    plan = form_service.plan(
        _snapshot(
            _field(
                "sponsorship-radio",
                "work_authorization.USA.sponsorship_required",
                control_type="radio",
                answer_type="boolean",
                options=["Yes", "No"],
            )
        )
    )

    assert _decision(plan, "sponsorship-radio") == {
        "field_id": "sponsorship-radio",
        "action": "ask",
        "reason_code": "value_not_in_options",
    }


def test_blank_observed_option_is_a_value_free_per_field_ask(form_service: FormService) -> None:
    plan = form_service.plan(
        _snapshot(_field("name", "identity.full_name", options=["   "]))
    )

    assert _decision(plan, "name") == {
        "field_id": "name",
        "action": "ask",
        "reason_code": "value_not_in_options",
    }


def test_repeated_cv_history_keeps_exact_indexed_provenance(form_service: FormService) -> None:
    plan = form_service.plan(
        _snapshot(
            _field("history-education-0-school", "education.0.institution"),
            _field("history-education-0-degree", "education.0.degree"),
            _field("history-employment-0-company", "employment.0.company"),
            _field("history-employment-0-title", "employment.0.title"),
        )
    )

    assert _decision(plan, "history-education-0-school")["source"] == {
        "kind": "cv",
        "ref": "education.0.institution",
    }
    assert _decision(plan, "history-employment-0-title")["value"] == "Software Engineering Intern"


@pytest.mark.parametrize("semantic_key", ["education.0.title", "employment.0.degree"])
def test_cross_section_cv_keys_are_not_resolved(form_service: FormService, semantic_key: str) -> None:
    plan = form_service.plan(_snapshot(_field("cross-section", semantic_key)))

    assert _decision(plan, "cross-section") == {
        "field_id": "cross-section",
        "action": "ask",
        "reason_code": "unknown_semantic_key",
    }


def _learn_question(
    form_service: FormService,
    *,
    wording: str,
    value: str = "Yes",
    scope: dict[str, object] | None = None,
) -> str:
    result = form_service.questions.sync(
        {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "page_id": "synthetic-page",
            "entries": [
                {
                    "wording": wording,
                    "value": value,
                    "answer_type": "selection",
                    "scope": scope or {"kind": "global"},
                    "qualifiers": {"negated": False},
                    "topic_tags": [],
                    "role_tags": [],
                    "source": "user",
                    "canonical_source_id": None,
                    "reviewed_at": "2026-07-17T00:00:00Z",
                }
            ],
        }
    )
    return result.entries[0].canonical_id


def test_unseen_wording_cannot_reuse_an_explicit_canonical_id(form_service: FormService) -> None:
    canonical_id = _learn_question(form_service, wording="Are you authorized?")
    plan = form_service.plan(
        _snapshot(
            _field(
                "unseen-question",
                control_type="select",
                answer_type="selection",
                options=["Yes", "No"],
                question_reuse={
                    "canonical_source_id": canonical_id,
                    "wording": "Do you require sponsorship?",
                    "scope": {"kind": "global"},
                    "qualifiers": {"negated": False},
                },
            )
        )
    )

    assert _decision(plan, "unseen-question") == {
        "field_id": "unseen-question",
        "action": "ask",
        "reason_code": "question_unseen",
    }


def test_question_scope_must_match_the_current_job_context(form_service: FormService) -> None:
    scope = {"kind": "company", "company": "Other Company"}
    canonical_id = _learn_question(form_service, wording="Question for company?", scope=scope)
    plan = form_service.plan(
        _snapshot(
            _field(
                "wrong-company",
                control_type="select",
                answer_type="selection",
                options=["Yes", "No"],
                question_reuse={
                    "canonical_source_id": canonical_id,
                    "wording": "Question for company?",
                    "scope": scope,
                    "qualifiers": {"negated": False},
                },
            )
        )
    )

    assert _decision(plan, "wrong-company") == {
        "field_id": "wrong-company",
        "action": "ask",
        "reason_code": "question_scope_mismatch",
    }


def test_fill_plan_rejects_provenance_on_a_non_fill_action() -> None:
    with pytest.raises(SchemaValidationError):
        SchemaRegistry().validate(
            "fill-plan.v1",
            {
                "schema_version": 1,
                "run_id": "run_synthetic",
                "page_id": "synthetic",
                "decisions": [
                    {
                        "field_id": "private",
                        "action": "ask",
                        "source": {"kind": "profile", "ref": "identity.full_name"},
                    }
                ],
                "stop_before_submit": True,
            },
        )


def test_snapshot_rejects_a_global_question_scope_with_company_data() -> None:
    snapshot = _snapshot(
        _field(
            "invalid-scope",
            question_reuse={
                "canonical_source_id": "q_synthetic",
                "wording": "Question?",
                "scope": {"kind": "global", "company": "Private Company"},
                "qualifiers": {"negated": False},
            },
        )
    )

    with pytest.raises(SchemaValidationError):
        SchemaRegistry().validate("form-snapshot.v1", snapshot)


def test_duplicate_field_ids_are_rejected_before_a_plan(form_service: FormService) -> None:
    with pytest.raises(Exception) as caught:
        form_service.plan(_snapshot(_field("duplicate"), _field("duplicate")))

    assert getattr(caught.value, "reason_code", None) == "duplicate_field_id"


def test_mock_workday_inventories_are_schema_valid_and_cover_each_page() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "mock-workday" / "page-inventories.json"
    snapshots = json.loads(fixture.read_text(encoding="utf-8"))

    assert [snapshot["page_id"] for snapshot in snapshots] == [
        "personal-information",
        "education-employment",
        "application-questions",
        "voluntary-self-identification",
        "upload",
        "review",
    ]
    registry = SchemaRegistry()
    for snapshot in snapshots:
        registry.validate("form-snapshot.v1", snapshot)
    education = next(snapshot for snapshot in snapshots if snapshot["page_id"] == "education-employment")
    assert {field["field_id"] for field in education["fields"]} >= {
        "education-0-school", "education-1-school", "employment-0-company", "employment-1-company"
    }
    questions = next(snapshot for snapshot in snapshots if snapshot["page_id"] == "application-questions")
    known = next(field for field in questions["fields"] if field["field_id"] == "known-question")
    assert known["answer_type"] == "selection"
    assert known["options"] == ["Yes", "No"]
