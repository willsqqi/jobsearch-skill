from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jobsearch_skill.cli import main
from jobsearch_skill.errors import StorageError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _facts(run_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "cv_name": "Synthetic CV",
        "source_hash": "b" * 64,
        "source_hashes": [{"source_ref": "resume.tex", "sha256": "b" * 64}],
        "identity": {"full_name": {"value": "PRIVATE_CV_CANARY", "evidence_anchor": "Synthetic"}},
        "education": [],
        "employment": [],
        "skills": [],
        "projects": [],
    }


def test_form_plan_persists_private_plan_and_emits_value_free_summary(tmp_path: Path, capsys) -> None:
    home = tmp_path / "PRIVATE_HOME_CANARY" / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    store = SafeStore(SchemaRegistry(), home / "backups")
    profile = store.read_yaml(home / "profile.yaml", "profile.v1")
    profile["identity"] = {"full_name": "Synthetic Person"}
    store.write_yaml(home / "profile.yaml", profile, "profile.v1")
    runs = RunStore(store, home / "runs")
    context = make_job_context(
        job_url="https://example.invalid/jobs/synthetic",
        company="Synthetic",
        role="Synthetic",
        location="Remote",
        employment_type="internship",
        description="Synthetic",
    )
    created = runs.start(context)
    analysis = {
        "schema_version": 1,
        "run_id": created.run_id,
        "job_fingerprint": context["job_fingerprint"],
        "role_summary": "Synthetic",
        "required_qualifications": [],
        "preferred_qualifications": [],
        "strong_matches": [],
        "partial_matches": [],
        "material_gaps": [],
        "cv_comparison": [],
        "recommended_cv": "Synthetic CV",
        "recommendation_rationale": "Synthetic",
        "customization": {"worthwhile": False, "rationale": "Synthetic"},
        "evidence_references": [],
    }
    runs.save_analysis(created.run_id, analysis)
    runs.select_cv(created.run_id, {"name": "Synthetic CV", "path": "cvs/synthetic/resume.tex", "customized": False})
    facts_path = home / "runs" / created.run_id / f"cv-facts-{'b' * 64}.json"
    store.write_json(facts_path, _facts(created.run_id), "cv-facts.v1")
    runs.checkpoint(
        created.run_id,
        {"target_phase": "cv_selected", "generated_artifacts": [facts_path.relative_to(home).as_posix()]},
    )
    snapshot = {
        "schema_version": 1,
        "run_id": created.run_id,
        "platform": "workday",
        "page_id": "personal-info",
        "url": "https://example.invalid/application",
        "fields": [
            {
                "field_id": "full-name",
                "label": "PRIVATE_LABEL_CANARY",
                "control_type": "text",
                "answer_type": "string",
                "required": True,
                "semantic_key": "identity.full_name",
                "options": [],
            }
        ],
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    assert main(["--home", str(home), "form", "plan", "--run-id", created.run_id, "--snapshot", str(snapshot_path)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    plan_path = home / "runs" / created.run_id / "fill-plan-personal-info.json"

    assert captured.err == ""
    assert payload == {
        "schema_version": 1,
        "ok": True,
        "command": "form.plan",
        "result": {"page_id": "personal-info", "action_counts": {"fill": 1}, "reason_counts": {}, "stop_before_submit": True},
        "warnings": [],
    }
    assert all(canary not in captured.out for canary in ("PRIVATE_HOME_CANARY", "PRIVATE_CV_CANARY", "PRIVATE_LABEL_CANARY"))
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert store.read_json(plan_path, "fill-plan.v1")["decisions"][0]["value"] == "Synthetic Person"


def test_form_plan_replaces_idempotently_and_preserves_prior_plan_on_write_failure(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    store = SafeStore(SchemaRegistry(), home / "backups")
    profile = store.read_yaml(home / "profile.yaml", "profile.v1")
    profile["identity"] = {"full_name": "Synthetic Person"}
    store.write_yaml(home / "profile.yaml", profile, "profile.v1")
    runs = RunStore(store, home / "runs")
    context = make_job_context(
        job_url="https://example.invalid/jobs/synthetic",
        company="Synthetic",
        role="Synthetic",
        location="Remote",
        employment_type="internship",
        description="Synthetic",
    )
    created = runs.start(context)
    runs.save_analysis(
        created.run_id,
        {
            "schema_version": 1, "run_id": created.run_id, "job_fingerprint": context["job_fingerprint"],
            "role_summary": "Synthetic", "required_qualifications": [], "preferred_qualifications": [],
            "strong_matches": [], "partial_matches": [], "material_gaps": [], "cv_comparison": [],
            "recommended_cv": "Synthetic CV", "recommendation_rationale": "Synthetic",
            "customization": {"worthwhile": False, "rationale": "Synthetic"}, "evidence_references": [],
        },
    )
    runs.select_cv(created.run_id, {"name": "Synthetic CV", "path": "resume.tex", "customized": False})
    facts_path = home / "runs" / created.run_id / f"cv-facts-{'b' * 64}.json"
    store.write_json(facts_path, _facts(created.run_id), "cv-facts.v1")
    runs.checkpoint(created.run_id, {"target_phase": "cv_selected", "generated_artifacts": [facts_path.relative_to(home).as_posix()]})
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({
        "schema_version": 1, "run_id": created.run_id, "platform": "workday", "page_id": "idempotent",
        "url": "https://example.invalid/application", "fields": [{"field_id": "name", "label": "Name", "control_type": "text", "answer_type": "string", "required": True, "semantic_key": "identity.full_name", "options": []}],
    }), encoding="utf-8")
    command = ["--home", str(home), "form", "plan", "--run-id", created.run_id, "--snapshot", str(snapshot_path)]
    assert main(command) == 0
    capsys.readouterr()
    plan_path = home / "runs" / created.run_id / "fill-plan-idempotent.json"
    original = plan_path.read_bytes()
    assert main(command) == 0
    capsys.readouterr()
    assert plan_path.read_bytes() == original
    original_replace = SafeStore._replace_locked

    def fail_plan_replace(self, path, text, *, create_backup):
        if path == plan_path:
            raise StorageError("PRIVATE_WRITE_CANARY", reason_code="storage_write")
        return original_replace(self, path, text, create_backup=create_backup)

    monkeypatch.setattr(SafeStore, "_replace_locked", fail_plan_replace)
    assert main(command) == 6
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "storage_write"
    assert "PRIVATE_WRITE_CANARY" not in captured.out
    assert plan_path.read_bytes() == original
    assert not list(plan_path.parent.glob(f".{plan_path.name}.*.tmp"))
