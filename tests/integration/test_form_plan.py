from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jobsearch_skill.cli import main
from jobsearch_skill.cv import CVRegistry, CVService
from jobsearch_skill.errors import StorageError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _analysis(run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "schema_version": 1, "run_id": run_id, "job_fingerprint": fingerprint,
        "role_summary": "Synthetic", "required_qualifications": [], "preferred_qualifications": [],
        "strong_matches": [], "partial_matches": [], "material_gaps": [], "cv_comparison": [],
        "recommended_cv": "Synthetic CV", "recommendation_rationale": "Synthetic",
        "customization": {"worthwhile": False, "rationale": "Synthetic"}, "evidence_references": [],
    }


def _facts(run_id: str, source_hash: str, source_hashes: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema_version": 1, "run_id": run_id, "cv_name": "Synthetic CV",
        "source_hash": source_hash, "source_hashes": source_hashes,
        "identity": {"full_name": {"value": "PRIVATE_CV_CANARY", "evidence_anchor": "Synthetic Person"}},
        "education": [], "employment": [], "skills": [], "projects": [],
    }


def _setup(tmp_path: Path, capsys) -> tuple[Path, SafeStore, RunStore, object, Path]:
    home = tmp_path / "PRIVATE_HOME_CANARY" / ".jobsearch"
    assert main(["--home", str(home), "bootstrap"]) == 0
    capsys.readouterr()
    store = SafeStore(SchemaRegistry(), home / "backups")
    profile = store.read_yaml(home / "profile.yaml", "profile.v1")
    profile["identity"] = {"full_name": "Synthetic Person"}
    store.write_yaml(home / "profile.yaml", profile, "profile.v1")
    cv_root = home / "cvs" / "synthetic"
    cv_root.mkdir(parents=True)
    tex = cv_root / "resume.tex"
    tex.write_text("\\documentclass{article}\n\\begin{document}\nSynthetic Person\n\\end{document}\n", encoding="utf-8")
    preferences = store.read_yaml(home / "preferences.yaml", "preferences.v1")
    preferences["default_cv"] = "Synthetic CV"
    preferences["cvs"] = [{"name": "Synthetic CV", "root": "cvs/synthetic", "tex": "resume.tex", "assets": []}]
    store.write_yaml(home / "preferences.yaml", preferences, "preferences.v1")
    runs = RunStore(store, home / "runs")
    context = make_job_context(job_url="https://example.invalid/jobs/synthetic", company="Synthetic", role="Synthetic", location="Remote", employment_type="internship", description="Synthetic")
    created = runs.start(context)
    runs.save_analysis(created.run_id, _analysis(created.run_id, str(context["job_fingerprint"])))
    registry = CVRegistry(preferences, home)
    selection = registry.resolve("Synthetic CV")
    assert selection.tex is not None
    runs.select_cv(created.run_id, {"name": selection.name, "path": str(selection.tex), "customized": True})
    service = CVService(home, store, runs, registry)
    evidence = service.evidence(created.run_id, selection)
    service.store_facts(created.run_id, selection, _facts(created.run_id, evidence.source_hash, [{"source_ref": ref, "sha256": digest} for ref, digest in sorted(evidence.source_hashes.items())]))
    return home, store, runs, created, tex


def _snapshot(run_id: str, page_id: str = "personal-info") -> dict[str, object]:
    return {"schema_version": 1, "run_id": run_id, "platform": "workday", "page_id": page_id, "url": "https://example.invalid/application", "fields": [{"field_id": "full-name", "label": "PRIVATE_LABEL_CANARY", "control_type": "text", "answer_type": "string", "required": True, "semantic_key": "identity.full_name", "options": []}]}


def _run(home: Path, run_id: str, snapshot: Path) -> int:
    return main(["--home", str(home), "form", "plan", "--run-id", run_id, "--snapshot", str(snapshot)])


def test_form_plan_persists_private_plan_and_emits_value_free_summary(tmp_path: Path, capsys) -> None:
    home, store, _, created, _ = _setup(tmp_path, capsys)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(created.run_id)), encoding="utf-8")

    assert _run(home, created.run_id, snapshot_path) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    plan_path = home / "runs" / created.run_id / "fill-plan-personal-info.json"

    assert captured.err == ""
    assert payload == {"schema_version": 1, "ok": True, "command": "form.plan", "result": {"page_id": "personal-info", "action_counts": {"fill": 1}, "reason_counts": {}, "stop_before_submit": True}, "warnings": []}
    assert all(canary not in captured.out for canary in ("PRIVATE_HOME_CANARY", "PRIVATE_CV_CANARY", "PRIVATE_LABEL_CANARY"))
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert store.read_json(plan_path, "fill-plan.v1")["decisions"][0]["value"] == "Synthetic Person"


def test_form_plan_rejects_tampered_selected_cv_before_writing(tmp_path: Path, capsys) -> None:
    home, _, _, created, tex = _setup(tmp_path, capsys)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(created.run_id)), encoding="utf-8")
    tex.write_text("\\documentclass{article}\n\\begin{document}\nChanged\n\\end{document}\n", encoding="utf-8")

    assert _run(home, created.run_id, snapshot_path) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "cv_facts_source_mismatch"
    assert "Changed" not in captured.out
    assert not (home / "runs" / created.run_id / "fill-plan-personal-info.json").exists()


def test_form_plan_rejects_multiple_or_symlinked_facts_artifacts(tmp_path: Path, capsys) -> None:
    home, store, runs, created, _ = _setup(tmp_path, capsys)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(created.run_id)), encoding="utf-8")
    run_path = home / "runs" / created.run_id / "run.yaml"
    state = store.read_yaml(run_path, "run-state.v1")
    state["generated_artifacts"].append(f"runs/{created.run_id}/cv-facts-{'d' * 64}.json")
    store.write_yaml(run_path, state, "run-state.v1")

    assert _run(home, created.run_id, snapshot_path) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "cv_facts_source_mismatch"
    assert not (home / "runs" / created.run_id / "fill-plan-personal-info.json").exists()

    state["generated_artifacts"].pop()
    store.write_yaml(run_path, state, "run-state.v1")
    facts_path = next((home / "runs" / created.run_id).glob("cv-facts-*.json"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    facts_path.unlink()
    facts_path.symlink_to(outside)
    assert _run(home, created.run_id, snapshot_path) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "cv_facts_source_mismatch"


def test_invalid_snapshot_and_terminal_run_never_write_a_plan(tmp_path: Path, capsys) -> None:
    home, _, runs, created, _ = _setup(tmp_path, capsys)
    invalid = _snapshot("run_other")
    snapshot_path = tmp_path / "invalid.json"
    snapshot_path.write_text(json.dumps(invalid), encoding="utf-8")
    assert _run(home, created.run_id, snapshot_path) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "run_id_mismatch"
    plan_path = home / "runs" / created.run_id / "fill-plan-personal-info.json"
    assert not plan_path.exists()
    runs.checkpoint(created.run_id, {"target_phase": "stopped"})
    assert _run(home, created.run_id, snapshot_path) == 6
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "invalid_transition"
    assert not plan_path.exists()


def test_form_plan_replaces_idempotently_and_preserves_prior_plan_on_write_failure(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _, _, created, _ = _setup(tmp_path, capsys)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(created.run_id, "idempotent")), encoding="utf-8")
    assert _run(home, created.run_id, snapshot_path) == 0
    capsys.readouterr()
    plan_path = home / "runs" / created.run_id / "fill-plan-idempotent.json"
    original = plan_path.read_bytes()
    assert _run(home, created.run_id, snapshot_path) == 0
    capsys.readouterr()
    assert plan_path.read_bytes() == original
    original_replace = SafeStore._replace_locked

    def fail_plan_replace(self, path, text, *, create_backup):
        if path == plan_path:
            raise StorageError("PRIVATE_WRITE_CANARY", reason_code="storage_write")
        return original_replace(self, path, text, create_backup=create_backup)

    monkeypatch.setattr(SafeStore, "_replace_locked", fail_plan_replace)
    assert _run(home, created.run_id, snapshot_path) == 6
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason_code"] == "storage_write"
    assert "PRIVATE_WRITE_CANARY" not in captured.out
    assert plan_path.read_bytes() == original
    assert not list(plan_path.parent.glob(f".{plan_path.name}.*.tmp"))
