from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from jobsearch_skill.errors import SchemaValidationError
from jobsearch_skill.schema import SchemaRegistry, load_yaml_document


CONTRACTS = (
    "profile.v1",
    "preferences.v1",
    "questions.v1",
    "run-state.v1",
    "job-context.v1",
    "analysis.v1",
    "cv-facts.v1",
    "cv-manifest.v1",
    "form-snapshot.v1",
    "fill-plan.v1",
    "reviewed-answers.v1",
    "application-record.v1",
)


@pytest.fixture
def schema_registry() -> SchemaRegistry:
    return SchemaRegistry()


def test_all_versioned_contracts_are_packaged_and_closed() -> None:
    schema_files = resources.files("jobsearch_skill.data.schemas")
    for contract in CONTRACTS:
        schema = json.loads(schema_files.joinpath(f"{contract}.schema.json").read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["properties"]["schema_version"] == {"const": 1}
        assert "schema_version" in schema["required"]
        assert schema["additionalProperties"] is False


def test_every_declared_object_shape_is_closed() -> None:
    def inspect(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, path
            for key, value in node.items():
                inspect(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                inspect(value, f"{path}[{index}]")

    schema_files = resources.files("jobsearch_skill.data.schemas")
    for contract in CONTRACTS:
        schema = json.loads(schema_files.joinpath(f"{contract}.schema.json").read_text())
        inspect(schema, contract)


def test_key_contracts_expose_every_approved_category() -> None:
    schema_files = resources.files("jobsearch_skill.data.schemas")
    expected = {
        "profile.v1": {
            "identity",
            "contact",
            "address",
            "work_authorization",
            "compensation",
            "demographics",
            "disability",
            "veteran",
            "legal_attestations",
            "links",
        },
        "preferences.v1": {
            "default_cv",
            "cvs",
            "browser",
            "platform_priority",
            "submission_mode",
            "generated_artifacts",
        },
        "analysis.v1": {
            "role_summary",
            "required_qualifications",
            "preferred_qualifications",
            "strong_matches",
            "partial_matches",
            "material_gaps",
            "cv_comparison",
            "recommended_cv",
            "recommendation_rationale",
            "customization",
            "evidence_references",
        },
        "cv-facts.v1": {
            "source_hash",
            "source_hashes",
            "identity",
            "education",
            "employment",
            "skills",
            "projects",
        },
    }
    for contract, categories in expected.items():
        schema = json.loads(schema_files.joinpath(f"{contract}.schema.json").read_text())
        assert categories <= schema["properties"].keys()


def test_profile_rejects_forbidden_secret_fields(schema_registry: SchemaRegistry) -> None:
    profile = {"schema_version": 1, "identity": {}, "password": "secret"}
    with pytest.raises(SchemaValidationError) as error:
        schema_registry.validate("profile.v1", profile)
    assert error.value.field_path == "$"
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("contract", "value", "field_path"),
    [
        ("run-state.v1", {"schema_version": 1, "phase": "invented"}, "$.phase"),
        (
            "form-snapshot.v1",
            {
                "schema_version": 1,
                "run_id": "run_synthetic",
                "platform": "workday",
                "page_id": "page",
                "url": "https://example.invalid/application",
                "fields": [
                    {
                        "field_id": "field",
                        "label": "Field",
                        "control_type": "slider",
                        "answer_type": "string",
                        "required": False,
                        "options": [],
                    }
                ],
            },
            "$.fields[0].control_type",
        ),
        (
            "fill-plan.v1",
            {
                "schema_version": 1,
                "run_id": "run_synthetic",
                "page_id": "page",
                "decisions": [{"field_id": "field", "action": "submit"}],
                "stop_before_submit": True,
            },
            "$.decisions[0].action",
        ),
    ],
)
def test_closed_enums_reject_unknown_values(
    schema_registry: SchemaRegistry,
    contract: str,
    value: dict[str, object],
    field_path: str,
) -> None:
    with pytest.raises(SchemaValidationError) as error:
        schema_registry.validate(contract, value)
    assert error.value.field_path == field_path


def test_question_answer_type_controls_value_type(schema_registry: SchemaRegistry) -> None:
    document = {
        "schema_version": 1,
        "questions": [
            {
                "canonical_id": "q_synthetic",
                "canonical_wording": "Synthetic question?",
                "observed_wordings": ["Synthetic question?"],
                "answer_type": "boolean",
                "scope": {"kind": "global"},
                "qualifiers": {"negated": False},
                "topic_tags": ["synthetic"],
                "role_tags": [],
                "answer": {
                    "value": "yes",
                    "source": "user",
                    "updated_at": "2026-07-17T00:00:00Z",
                },
                "created_at": "2026-07-17T00:00:00Z",
                "updated_at": "2026-07-17T00:00:00Z",
                "history": [],
            }
        ],
    }
    with pytest.raises(SchemaValidationError) as error:
        schema_registry.validate("questions.v1", document)
    assert error.value.field_path == "$.questions[0].answer.value"


def test_packaged_synthetic_documents_validate(schema_registry: SchemaRegistry) -> None:
    synthetic = resources.files("jobsearch_skill.data.synthetic")
    for contract in ("profile.v1", "preferences.v1", "questions.v1"):
        document = load_yaml_document(Path(str(synthetic.joinpath(f"{contract.removesuffix('.v1')}.yaml"))))
        assert schema_registry.validate(contract, document) is None
    resume = synthetic.joinpath("resume.tex").read_text()
    assert "Avery Example" in resume
    assert "Synthetic Systems" in resume
    assert "example.invalid" in resume


def test_remaining_contracts_accept_representative_v1_documents(
    schema_registry: SchemaRegistry,
) -> None:
    timestamp = "2026-07-17T00:00:00Z"
    digest = "0" * 64
    job = {
        "schema_version": 1,
        "job_url": "https://example.invalid/job",
        "canonical_url": "https://example.invalid/job",
        "company": "Synthetic Systems",
        "role": "Synthetic Systems",
        "location": "Synthetic Systems",
        "employment_type": "Synthetic Systems",
        "description": "Synthetic Systems",
        "responsibilities": ["Synthetic Systems"],
        "required_qualifications": ["Synthetic Systems"],
        "preferred_qualifications": ["Synthetic Systems"],
        "technologies": ["Synthetic Systems"],
        "captured_at": timestamp,
        "job_fingerprint": f"sha256:{digest}",
    }
    documents: dict[str, dict[str, object]] = {
        "job-context.v1": job,
        "run-state.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "job_context": job,
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
            "created_at": timestamp,
            "updated_at": timestamp,
        },
        "analysis.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "job_fingerprint": f"sha256:{digest}",
            "role_summary": "Synthetic Systems",
            "required_qualifications": ["Synthetic Systems"],
            "preferred_qualifications": ["Synthetic Systems"],
            "strong_matches": [
                {
                    "requirement": "Synthetic Systems",
                    "finding": "Synthetic Systems",
                    "evidence_refs": ["evidence_synthetic"],
                }
            ],
            "partial_matches": [
                {
                    "requirement": "Synthetic Systems",
                    "finding": "Synthetic Systems",
                    "limits": "Synthetic Systems",
                    "evidence_refs": ["evidence_synthetic"],
                }
            ],
            "material_gaps": ["Synthetic Systems"],
            "cv_comparison": [
                {
                    "cv_name": "Synthetic Systems",
                    "strong_matches": ["Synthetic Systems"],
                    "partial_matches": ["Synthetic Systems"],
                    "material_gaps": ["Synthetic Systems"],
                    "rationale": "Synthetic Systems",
                }
            ],
            "recommended_cv": "Synthetic Systems",
            "recommendation_rationale": "Synthetic Systems",
            "customization": {"worthwhile": False, "rationale": "Synthetic Systems"},
            "evidence_references": [
                {
                    "evidence_id": "evidence_synthetic",
                    "source_kind": "cv",
                    "source_ref": "synthetic-cv/resume.tex",
                    "anchor": "Avery Example",
                    "sha256": digest,
                }
            ],
        },
        "cv-facts.v1": {
            "schema_version": 1,
            "cv_name": "Synthetic Systems",
            "source_hash": digest,
            "source_hashes": [{"source_ref": "resume.tex", "sha256": digest}],
            "identity": {
                "full_name": {"value": "Avery Example", "evidence_anchor": "Avery Example"}
            },
            "education": [
                {
                    "institution": "Synthetic Systems",
                    "degree": "Synthetic Systems",
                    "start_date": "2026",
                    "end_date": "2026",
                    "evidence_anchor": "Synthetic Systems",
                }
            ],
            "employment": [
                {
                    "company": "Synthetic Systems",
                    "title": "Synthetic Systems",
                    "start_date": "2026",
                    "end_date": "2026",
                    "highlights": ["Synthetic Systems"],
                    "evidence_anchor": "Synthetic Systems",
                }
            ],
            "skills": [
                {"name": "Synthetic Systems", "evidence_anchor": "Synthetic Systems"}
            ],
            "projects": [
                {
                    "name": "Synthetic Systems",
                    "description": "Synthetic Systems",
                    "technologies": ["Synthetic Systems"],
                    "evidence_anchor": "Synthetic Systems",
                }
            ],
        },
        "cv-manifest.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "source_cv_name": "Synthetic Systems",
            "source_root": "synthetic-cv",
            "source_hashes": {"resume.tex": digest},
            "job_fingerprint": f"sha256:{digest}",
            "copied_files": ["source/resume.tex"],
            "status": "prepared",
            "output_pdf": None,
            "verification": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
        "form-snapshot.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "platform": "workday",
            "page_id": "synthetic-page",
            "url": "https://example.invalid/application",
            "fields": [
                {
                    "field_id": "synthetic-field",
                    "label": "Synthetic Systems",
                    "control_type": "text",
                    "answer_type": "string",
                    "required": True,
                    "semantic_key": "identity.full_name",
                    "options": [],
                }
            ],
        },
        "fill-plan.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "page_id": "synthetic-page",
            "decisions": [
                {
                    "field_id": "synthetic-field",
                    "action": "fill",
                    "value": "Avery Example",
                    "source": {"kind": "profile", "ref": "identity.full_name"},
                    "match_kind": "direct",
                    "review_required": False,
                }
            ],
            "stop_before_submit": True,
        },
        "reviewed-answers.v1": {
            "schema_version": 1,
            "run_id": "run_synthetic",
            "page_id": "synthetic-page",
            "entries": [
                {
                    "wording": "Synthetic Systems?",
                    "value": True,
                    "answer_type": "boolean",
                    "scope": {"kind": "global"},
                    "qualifiers": {"negated": False},
                    "topic_tags": ["Synthetic Systems"],
                    "role_tags": ["Synthetic Systems"],
                    "source": "user",
                    "canonical_source_id": None,
                    "reviewed_at": timestamp,
                }
            ],
        },
        "application-record.v1": {
            "schema_version": 1,
            "application_id": "application_synthetic",
            "company": "Synthetic Systems",
            "role": "Synthetic Systems",
            "location": "Synthetic Systems",
            "url": "https://example.invalid/application",
            "job_fingerprint": f"sha256:{digest}",
            "cv_name": "Synthetic Systems",
            "cv_path": "synthetic-cv/resume.pdf",
            "analysis_ref": "runs/run_synthetic/analysis.yaml",
            "artifact_ref": "synthetic-cv/resume.pdf",
            "applied_at": timestamp,
            "status": "Applied",
            "workday_id": "synthetic",
            "updated_at": timestamp,
        },
    }
    for contract, document in documents.items():
        assert schema_registry.validate(contract, document) is None


def test_schema_directory_override_isolated_from_package(tmp_path: Path) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"schema_version": {"const": 1}},
        "required": ["schema_version"],
        "additionalProperties": False,
    }
    (tmp_path / "isolated.v1.schema.json").write_text(json.dumps(schema))
    registry = SchemaRegistry(tmp_path)
    registry.validate("isolated.v1", {"schema_version": 1})
    with pytest.raises(SchemaValidationError, match="schema_not_found"):
        registry.validate("profile.v1", {"schema_version": 1})


def test_application_record_status_is_exactly_applied(schema_registry: SchemaRegistry) -> None:
    timestamp = "2026-07-17T00:00:00Z"
    record = {
        "schema_version": 1,
        "application_id": "application_synthetic",
        "company": "Synthetic Systems",
        "role": "Backend Engineer",
        "location": "Remote",
        "url": "https://example.invalid/application",
        "job_fingerprint": f"sha256:{'0' * 64}",
        "cv_name": "SWE",
        "cv_path": "synthetic/resume.pdf",
        "analysis_ref": "runs/run_synthetic/analysis.yaml",
        "artifact_ref": "synthetic/resume.pdf",
        "applied_at": timestamp,
        "status": "Interview",
        "workday_id": "",
        "updated_at": timestamp,
    }

    with pytest.raises(SchemaValidationError):
        schema_registry.validate("application-record.v1", record)


def test_load_yaml_document_requires_mapping(tmp_path: Path) -> None:
    path = tmp_path / "document.yaml"
    path.write_text("- synthetic\n")
    with pytest.raises(SchemaValidationError, match="document_type"):
        load_yaml_document(path)
