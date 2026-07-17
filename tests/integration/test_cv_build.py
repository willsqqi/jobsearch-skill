from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfWriter

from jobsearch_skill.cli import main
from jobsearch_skill.cv import CVRegistry, CVService
from jobsearch_skill.errors import CVBuildError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _analysis(run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "job_fingerprint": fingerprint,
        "role_summary": "Synthetic role",
        "required_qualifications": [],
        "preferred_qualifications": [],
        "strong_matches": [],
        "partial_matches": [],
        "material_gaps": [],
        "cv_comparison": [],
        "recommended_cv": "Avery Example CV",
        "recommendation_rationale": "Public fixture",
        "customization": {"worthwhile": True, "rationale": "Public fixture"},
        "evidence_references": [],
    }


@pytest.fixture
def build_setup(tmp_path: Path):
    home = tmp_path / "PRIVATE_BUILD_HOME_CANARY" / ".jobsearch"
    root = home / "cvs" / "avery"
    root.mkdir(parents=True)
    home.chmod(0o700)
    fixture_root = Path(__file__).parents[1] / "fixtures" / "latex-cv"
    tex = root / "resume.tex"
    tex.write_bytes((fixture_root / "resume.tex").read_bytes())
    original_hash = hashlib.sha256(tex.read_bytes()).hexdigest()
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
    preferences = {
        "schema_version": 1,
        "default_cv": "Avery Example CV",
        "cvs": [
            {
                "name": "Avery Example CV",
                "root": "cvs/avery",
                "tex": "resume.tex",
                "assets": [],
            }
        ],
        "browser": "builtin",
        "platform_priority": ["workday"],
        "submission_mode": "manual",
        "generated_artifacts": {
            "naming_template": "{company}-{role}-{run_id}",
            "retain_days": 30,
            "retain_failed_builds": True,
            "retain_build_logs": True,
        },
    }
    store.write_yaml(home / "preferences.yaml", preferences, "preferences.v1")
    registry = CVRegistry(preferences, home)
    selection = registry.resolve(None, for_customization=True)
    context = make_job_context(
        job_url="https://example.invalid/jobs/7",
        company="Synthetic Systems",
        role="Backend Engineer",
        description="Build reliable Python APIs.",
    )
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis(run.run_id, str(context["job_fingerprint"])))
    run = runs.select_cv(
        run.run_id,
        {"name": selection.name, "path": str(tex), "customized": True},
    )
    service = CVService(home, store, runs, registry)
    evidence = service.evidence(run.run_id, selection)
    facts = json.loads((fixture_root / "evidence.json").read_text(encoding="utf-8"))
    facts["source_hash"] = evidence.source_hash
    facts["source_hashes"] = [
        {"source_ref": ref, "sha256": digest}
        for ref, digest in sorted(evidence.source_hashes.items())
    ]
    service.store_facts(run.run_id, selection, facts)
    prepared = service.prepare(run.run_id, selection)
    return service, selection, run, prepared, tex, original_hash


def test_build_creates_verified_pdf_without_mutating_original(build_setup) -> None:
    service, _, run, prepared, original, original_hash = build_setup

    result = service.build(run.run_id)

    assert result.verified is True
    assert result.pdf.exists() and result.pdf.stat().st_size > 0
    assert result.page_count >= 1
    assert "Avery Example" in result.extracted_text
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    assert stat.S_IMODE(result.pdf.stat().st_mode) == 0o600
    assert service.run_store.get(run.run_id).phase == "cv_selected"
    manifest = service._read_manifest(prepared.manifest_path)
    assert manifest["status"] == "verified"
    assert manifest["verification"]["identity_present"] is True


def test_build_uses_exact_safe_latexmk_invocation(build_setup, monkeypatch) -> None:
    service, _, run, prepared, _, _ = build_setup
    actual_run = subprocess.run
    observed: dict[str, object] = {}

    def recording_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return actual_run(command, **kwargs)

    monkeypatch.setattr("jobsearch_skill.cv.subprocess.run", recording_run)

    service.build(run.run_id)

    assert observed["command"] == [
        "latexmk",
        "-pdf",
        "-interaction=nonstopmode",
        "-halt-on-error",
        prepared.tex.name,
    ]
    assert observed["cwd"] == prepared.tex.parent
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["timeout"] == 120


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout"])
def test_build_tool_failures_preserve_private_redacted_log(
    build_setup, monkeypatch, failure: str
) -> None:
    service, _, run, _, _, _ = build_setup
    private_path = str(service.home)
    canary = f"Avery Example avery@example.invalid {private_path}"

    def failed_run(command, **kwargs):
        if failure == "missing":
            raise FileNotFoundError(canary)
        if failure == "timeout":
            partial = Path(kwargs["cwd"]) / "partial.aux"
            partial.write_text(canary, encoding="utf-8")
            partial.chmod(0o644)
            raise subprocess.TimeoutExpired(command, 120, output=canary, stderr=canary)
        return subprocess.CompletedProcess(command, 2, stdout=canary, stderr=canary)

    monkeypatch.setattr("jobsearch_skill.cv.subprocess.run", failed_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    error = caught.value
    assert error.log_path is not None and error.log_path.exists()
    log = error.log_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(error.log_path.stat().st_mode) == 0o600
    assert "Avery Example" not in log
    assert "avery@example.invalid" not in log
    assert private_path not in log
    assert "fallback" not in error.decision
    assert private_path not in str(error)
    if failure == "timeout":
        partial = next(error.log_path.parent.rglob("partial.aux"))
        assert stat.S_IMODE(partial.stat().st_mode) == 0o600


def test_build_rejects_manifest_tex_escape_before_subprocess(build_setup, monkeypatch) -> None:
    service, _, run, prepared, _, _ = build_setup
    manifest = dict(prepared.manifest)
    manifest["expected_tex"] = "../manifest.yaml"
    service._atomic_manifest(prepared.manifest_path, manifest)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr("jobsearch_skill.cv.subprocess.run", unexpected_run)

    with pytest.raises(CVBuildError):
        service.build(run.run_id)


@pytest.mark.parametrize("output_kind", ["malformed", "zero", "encrypted", "no_text", "symlink"])
def test_build_rejects_unverifiable_pdf_with_safe_log(
    build_setup, monkeypatch, output_kind: str
) -> None:
    service, _, run, prepared, _, _ = build_setup

    def fake_run(command, **kwargs):
        output = prepared.tex.with_suffix(".pdf")
        if output_kind == "malformed":
            output.write_bytes(b"not a pdf")
        elif output_kind == "zero":
            output.write_bytes(b"")
        elif output_kind == "symlink":
            outside = service.home.parent / "outside.pdf"
            outside.write_bytes(b"outside")
            output.symlink_to(outside)
        else:
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            if output_kind == "encrypted":
                writer.encrypt("secret")
            with output.open("wb") as stream:
                writer.write(stream)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("jobsearch_skill.cv.subprocess.run", fake_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.log_path is not None
    assert caught.value.log_path.exists()
    assert "fallback" not in caught.value.decision


def test_build_rejects_identity_mismatch_without_fallback(build_setup) -> None:
    service, _, run, prepared, original, original_hash = build_setup
    assert prepared.tex is not None
    prepared.tex.write_text(
        prepared.tex.read_text(encoding="utf-8").replace("Avery Example", "Other Person"),
        encoding="utf-8",
    )

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.log_path is not None
    assert "fallback" not in caught.value.decision
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash


def test_pdf_only_selection_is_not_silently_built(tmp_path: Path) -> None:
    home = tmp_path / ".jobsearch"
    root = home / "cvs" / "pdf-only"
    root.mkdir(parents=True)
    pdf = root / "resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf.open("wb") as stream:
        writer.write(stream)
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
    registry = CVRegistry(
        {"default_cv": "PDF", "cvs": [{"name": "PDF", "root": "cvs/pdf-only", "pdf": "resume.pdf", "assets": []}]},
        home,
    )
    selection = registry.resolve(None)
    context = make_job_context(
        job_url="https://example.invalid/jobs/pdf",
        company="Synthetic Systems",
        role="Backend Engineer",
        description="Public fixture",
    )
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis(run.run_id, str(context["job_fingerprint"])))
    runs.select_cv(run.run_id, {"name": "PDF", "path": str(pdf), "customized": False})
    service = CVService(home, store, runs, registry)
    service.prepare(run.run_id, selection)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.decision == "review_required"


def test_cli_cv_commands_emit_value_free_versioned_envelopes(
    build_setup, capsys: pytest.CaptureFixture[str]
) -> None:
    service, _, run, prepared, _, _ = build_setup
    home = service.home

    assert main(["--home", str(home), "cv", "evidence", "--run-id", run.run_id]) == 0
    evidence_payload = json.loads(capsys.readouterr().out)
    assert evidence_payload["command"] == "cv.evidence"
    assert set(evidence_payload["result"]) == {"evidence_ref", "source_count", "status"}

    facts_ref = next(
        ref for ref in service.run_store.get(run.run_id).data["generated_artifacts"]
        if "/cv-facts-" in f"/{ref}"
    )
    assert main(
        [
            "--home",
            str(home),
            "cv",
            "facts",
            "--run-id",
            run.run_id,
            "--input",
            str(home / facts_ref),
        ]
    ) == 0
    facts_payload = json.loads(capsys.readouterr().out)
    assert facts_payload["command"] == "cv.facts"
    assert set(facts_payload["result"]) == {
        "education_count",
        "employment_count",
        "facts_ref",
        "project_count",
        "status",
    }

    assert main(["--home", str(home), "cv", "prepare", "--run-id", run.run_id]) == 0
    prepare_payload = json.loads(capsys.readouterr().out)
    assert prepare_payload["command"] == "cv.prepare"
    assert set(prepare_payload["result"]) == {"copied_count", "manifest_ref", "status"}

    assert main(["--home", str(home), "cv", "build", "--run-id", run.run_id]) == 0
    build_payload = json.loads(capsys.readouterr().out)
    assert build_payload["command"] == "cv.build"
    assert set(build_payload["result"]) == {"page_count", "pdf_ref", "status", "verified"}

    combined = json.dumps(
        [evidence_payload, facts_payload, prepare_payload, build_payload], sort_keys=True
    )
    assert "Avery Example" not in combined
    assert "avery@example.invalid" not in combined
    assert str(home) not in combined
    assert str(prepared.tex) not in combined


def test_cli_build_error_does_not_expose_log_path_or_compiler_values(
    build_setup, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    service, _, run, _, _, _ = build_setup
    canary = f"Avery Example avery@example.invalid {service.home}"

    def failed_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout=canary, stderr=canary)

    monkeypatch.setattr("jobsearch_skill.cv.subprocess.run", failed_run)

    assert main(["--home", str(service.home), "cv", "build", "--run-id", run.run_id]) == 5
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload == {
        "schema_version": 1,
        "ok": False,
        "command": "cv.build",
        "reason_code": "cv_build_failed",
        "warnings": [],
    }
    assert "build-redacted.log" not in captured.out
    assert "Avery Example" not in captured.out
    assert "avery@example.invalid" not in captured.out
    assert str(service.home) not in captured.out
