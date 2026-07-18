from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jobsearch_skill.cli import main
from jobsearch_skill.cv import CVRegistry, CVService
from jobsearch_skill.errors import CVBuildError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _write_text_pdf(path: Path, text: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    page[NameObject("/Resources")] = resources
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


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
    pdf = root / "resume.pdf"
    _write_text_pdf(pdf, "Avery Example original declared PDF")
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
                "pdf": "resume.pdf",
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
    facts["run_id"] = run.run_id
    facts["source_hash"] = evidence.source_hash
    facts["source_hashes"] = [
        {"source_ref": ref, "sha256": digest}
        for ref, digest in sorted(evidence.source_hashes.items())
    ]
    service.store_facts(run.run_id, selection, facts)
    prepared = service.prepare(run.run_id, selection)
    return service, selection, run, prepared, tex, original_hash


def _rewrite_prepared_tex_reference(service, run, prepared, reference: str) -> None:
    manifest = dict(prepared.manifest)
    original_reference = str(manifest["expected_tex"])
    original_hash = str(manifest["source_hash"])
    original_tex = prepared.source_dir / original_reference
    rewritten_tex = prepared.source_dir / reference
    original_tex.rename(rewritten_tex)
    source_hashes = {
        (reference if key == original_reference else str(key)): str(digest)
        for key, digest in manifest["source_hashes"].items()
    }
    source_hash = service._aggregate_hash(source_hashes)
    manifest.update(
        {
            "source_hash": source_hash,
            "source_hashes": source_hashes,
            "copied_files": [
                reference if item == original_reference else item
                for item in manifest["copied_files"]
            ],
            "expected_tex": reference,
        }
    )
    prepared.manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    prepared.manifest_path.chmod(0o600)

    old_facts_path = service.facts_path(run.run_id, original_hash)
    facts = service.store.read_json(old_facts_path, "cv-facts.v1")
    facts["source_hash"] = source_hash
    facts["source_hashes"] = service._hash_entries(source_hashes)
    new_facts_path = service.facts_path(run.run_id, source_hash)
    new_facts_path.write_text(json.dumps(facts), encoding="utf-8")
    new_facts_path.chmod(0o600)

    old_evidence_path = (
        service.run_store.runs_dir
        / run.run_id
        / f"cv-evidence-{original_hash}.json"
    )
    evidence = service.store.read_json(old_evidence_path, "cv-evidence.v1")
    evidence["source_hash"] = source_hash
    evidence["source_hashes"] = service._hash_entries(source_hashes)
    for source in evidence["sources"]:
        if source["source_ref"] == original_reference:
            source["source_ref"] = reference
    new_evidence_path = (
        service.run_store.runs_dir / run.run_id / f"cv-evidence-{source_hash}.json"
    )
    new_evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    new_evidence_path.chmod(0o600)


@pytest.mark.parametrize("helper", ["curl", "gs"])
def test_build_rejects_latexmk_shell_substitution_filename_before_execution(
    build_setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper: str
) -> None:
    service, _, run, prepared, _, _ = build_setup
    reference = f"resume`{helper}`.tex"
    _rewrite_prepared_tex_reference(service, run, prepared, reference)
    sentinel = tmp_path / f"{helper}-helper-executed"
    bin_dir = tmp_path / "adversarial-bin"
    bin_dir.mkdir()
    helper_path = bin_dir / helper
    helper_path.write_text('#!/bin/sh\n: > "$PWN_TARGET"\n', encoding="utf-8")
    helper_path.chmod(0o700)
    from jobsearch_skill import cv_build

    actual_environment = cv_build.isolated_latex_environment

    def adversarial_environment(root: Path) -> dict[str, str]:
        environment = actual_environment(root)
        environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
        environment["PWN_TARGET"] = str(sentinel)
        return environment

    monkeypatch.setattr(
        "jobsearch_skill.cv_build.isolated_latex_environment",
        adversarial_environment,
    )

    with pytest.raises(CVBuildError):
        service.build(run.run_id)

    assert not sentinel.exists()


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


def test_customize_persists_grounded_claims_and_builds_separate_source(build_setup) -> None:
    service, selection, run, prepared, original, original_hash = build_setup
    assert prepared.tex is not None
    original_prepared_hash = hashlib.sha256(prepared.tex.read_bytes()).hexdigest()
    rewritten_claim = "Built Python API services."
    customized_tex = prepared.tex.read_text(encoding="utf-8").replace(
        "Built Python API services with deterministic tests and careful error handling.",
        rewritten_claim,
    )
    request = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": customized_tex,
        "claims": [
            {
                "claim": rewritten_claim,
                "evidence_anchors": ["Built Python API services"],
                "fact_refs": ["/employment/0"],
            }
        ],
        "unsupported_requirements": ["Kubernetes"],
    }

    customization = service.customize(run.run_id, selection, request)
    result = service.build(run.run_id)
    manifest = service._read_manifest(prepared.manifest_path)

    assert customization["claims"] == request["claims"]
    assert result.verified is True
    assert rewritten_claim in result.extracted_text
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256(prepared.tex.read_bytes()).hexdigest() == original_prepared_hash
    customized_path = prepared.root / str(manifest["customized_tex"])
    assert customized_path.is_file()
    assert customized_path != prepared.tex
    assert stat.S_IMODE(customized_path.stat().st_mode) == 0o600
    assert manifest["customized_tex_sha256"] == hashlib.sha256(
        customized_path.read_bytes()
    ).hexdigest()
    assert manifest["claim_evidence_ref"] == "customization.json"


def test_customize_rejects_claim_without_original_evidence(build_setup) -> None:
    service, selection, run, prepared, _, _ = build_setup
    assert prepared.tex is not None
    unsupported = "Operated Kubernetes clusters in production."
    customized_tex = prepared.tex.read_text(encoding="utf-8").replace(
        "Built Python API services with deterministic tests and careful error handling.",
        unsupported,
    )
    request = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": customized_tex,
        "claims": [
            {
                "claim": unsupported,
                "evidence_anchors": ["Kubernetes"],
                "fact_refs": ["/employment/0"],
            }
        ],
        "unsupported_requirements": [],
    }

    with pytest.raises(CVBuildError) as caught:
        service.customize(run.run_id, selection, request)

    assert caught.value.reason_code == "cv_customization_anchor"
    assert not (prepared.root / "customization.json").exists()
    assert not (prepared.root / "customized").exists()


@pytest.mark.parametrize(
    "unsupported",
    [
        "Managed Python API services.",
        r"Built Python API services with 99\% availability.",
    ],
)
def test_build_rechecks_every_customized_line_against_persisted_claims(
    build_setup, unsupported: str
) -> None:
    service, selection, run, prepared, _, _ = build_setup
    assert prepared.tex is not None
    rewritten_claim = "Built Python API services."
    request = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": prepared.tex.read_text(encoding="utf-8").replace(
            "Built Python API services with deterministic tests and careful error handling.",
            rewritten_claim,
        ),
        "claims": [
            {
                "claim": rewritten_claim,
                "evidence_anchors": ["Built Python API services"],
                "fact_refs": ["/employment/0"],
            }
        ],
        "unsupported_requirements": ["Kubernetes"],
    }
    service.customize(run.run_id, selection, request)
    manifest = service._read_manifest(prepared.manifest_path)
    customized_path = prepared.root / str(manifest["customized_tex"])
    tampered = customized_path.read_text(encoding="utf-8").replace(
        rewritten_claim, unsupported
    )
    customized_path.write_text(tampered, encoding="utf-8")
    customized_path.chmod(0o600)
    request["customized_tex"] = tampered
    request["claims"] = [
        {
            "claim": unsupported,
            "evidence_anchors": ["Built Python API services"],
            "fact_refs": ["/employment/0"],
        }
    ]
    service.store.write_json(
        prepared.root / "customization.json", request, "cv-customization.v1"
    )
    manifest["customized_tex_sha256"] = hashlib.sha256(
        tampered.encode("utf-8")
    ).hexdigest()
    service._atomic_manifest(prepared.manifest_path, manifest)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_customization_mismatch"


def test_customize_requires_explicit_tex_selection_binding(build_setup) -> None:
    service, _, _, _, _, _ = build_setup
    selection = service.cv_registry.resolve(None, for_customization=False)
    assert selection.pdf is not None and selection.tex is not None
    context = make_job_context(
        job_url="https://example.invalid/jobs/pdf-bound",
        company="Synthetic Systems",
        role="PDF-bound role",
        description="Public fixture.",
    )
    run = service.run_store.start(context)
    service.run_store.save_analysis(
        run.run_id, _analysis(run.run_id, str(context["job_fingerprint"]))
    )
    service.run_store.select_cv(
        run.run_id,
        {"name": selection.name, "path": str(selection.pdf), "customized": False},
    )
    evidence = service.evidence(run.run_id, selection)
    facts = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "latex-cv" / "evidence.json").read_text(
            encoding="utf-8"
        )
    )
    facts.update(
        {
            "run_id": run.run_id,
            "source_hash": evidence.source_hash,
            "source_hashes": service._hash_entries(evidence.source_hashes),
        }
    )
    service.store_facts(run.run_id, selection, facts)
    prepared = service.prepare(run.run_id, selection)
    assert prepared.tex is not None

    with pytest.raises(CVBuildError) as caught:
        service.customize(
            run.run_id,
            selection,
            {
                "schema_version": 1,
                "run_id": run.run_id,
                "cv_name": selection.name,
                "source_hash": prepared.manifest["source_hash"],
                "customized_tex": prepared.tex.read_text(encoding="utf-8"),
                "claims": [],
                "unsupported_requirements": [],
            },
        )

    assert caught.value.reason_code == "cv_customization_source"


def test_customize_recovers_orphan_files_and_failed_request(build_setup) -> None:
    service, selection, run, prepared, _, _ = build_setup
    assert prepared.tex is not None
    invalid_tex = prepared.tex.read_text(encoding="utf-8").replace("\\end{document}", "")
    first = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": invalid_tex,
        "claims": [],
        "unsupported_requirements": [],
    }
    orphan = prepared.root / "customized" / str(prepared.manifest["expected_tex"])
    orphan.parent.mkdir(mode=0o700)
    orphan.write_text("interrupted write", encoding="utf-8")
    orphan.chmod(0o600)

    service.customize(run.run_id, selection, first)
    with pytest.raises(CVBuildError):
        service.build(run.run_id)
    assert service._read_manifest(prepared.manifest_path)["status"] == "failed"

    corrected = dict(first)
    corrected["customized_tex"] = prepared.tex.read_text(encoding="utf-8")
    service.customize(run.run_id, selection, corrected)
    manifest = service._read_manifest(prepared.manifest_path)

    assert manifest["status"] == "prepared"
    assert manifest["customized_tex_sha256"] == hashlib.sha256(
        corrected["customized_tex"].encode("utf-8")
    ).hexdigest()
    assert service.build(run.run_id).verified is True


def test_customize_repairs_missing_manifest_binding(build_setup) -> None:
    service, selection, run, prepared, _, _ = build_setup
    assert prepared.tex is not None
    request = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": prepared.tex.read_text(encoding="utf-8"),
        "claims": [],
        "unsupported_requirements": [],
    }
    service.customize(run.run_id, selection, request)
    manifest = service._read_manifest(prepared.manifest_path)
    for key in ("customized_tex", "customized_tex_sha256", "claim_evidence_ref"):
        manifest.pop(key)
    service._atomic_manifest(prepared.manifest_path, manifest)

    assert service.customize(run.run_id, selection, request) == request
    repaired = service._read_manifest(prepared.manifest_path)
    assert repaired["customized_tex"] == "customized/resume.tex"
    assert repaired["claim_evidence_ref"] == "customization.json"


def test_build_preserves_same_stem_declared_pdf_and_uses_separate_output(
    build_setup,
) -> None:
    service, selection, run, prepared, _, _ = build_setup
    assert selection.pdf is not None and prepared.pdf is not None
    original_pdf_hash = hashlib.sha256(selection.pdf.read_bytes()).hexdigest()
    prepared_pdf_hash = hashlib.sha256(prepared.pdf.read_bytes()).hexdigest()

    result = service.build(run.run_id)

    assert result.pdf != prepared.pdf
    assert result.pdf.relative_to(prepared.root).as_posix() == "output/resume.pdf"
    assert hashlib.sha256(selection.pdf.read_bytes()).hexdigest() == original_pdf_hash
    assert hashlib.sha256(prepared.pdf.read_bytes()).hexdigest() == prepared_pdf_hash
    assert prepared.manifest["source_hashes"]["resume.pdf"] == prepared_pdf_hash
    repeated = service.build(run.run_id)
    assert repeated.pdf == result.pdf


def test_build_keeps_declared_source_immutable_while_compiling(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    assert prepared.pdf is not None
    declared_pdf_hash = hashlib.sha256(prepared.pdf.read_bytes()).hexdigest()
    actual_run = service._run_compiler
    observed_cwd: Path | None = None

    def recording_run(command, *, cwd, env):
        nonlocal observed_cwd
        observed_cwd = Path(cwd)
        assert prepared.pdf is not None and prepared.pdf.exists()
        assert hashlib.sha256(prepared.pdf.read_bytes()).hexdigest() == declared_pdf_hash
        return actual_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(service, "_run_compiler", recording_run)

    service.build(run.run_id)

    assert observed_cwd is not None
    assert observed_cwd != prepared.source_dir
    assert observed_cwd.is_relative_to(prepared.root)
    assert not observed_cwd.exists()
    assert hashlib.sha256(prepared.pdf.read_bytes()).hexdigest() == declared_pdf_hash


def test_build_removes_stale_compiler_workspace_before_running(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    stale_workspace = prepared.root / ".compiler-work-stale"
    stale_workspace.mkdir(mode=0o700)
    stale_log = stale_workspace / "resume.log"
    stale_log.write_text("RAW_STALE_COMPILER_CANARY", encoding="utf-8")
    stale_log.chmod(0o600)
    actual_run = service._run_compiler

    def recording_run(command, *, cwd, env):
        assert not stale_workspace.exists()
        return actual_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(service, "_run_compiler", recording_run)

    service.build(run.run_id)

    assert not stale_workspace.exists()
    assert not list(prepared.root.glob(".compiler-work-*"))


def test_build_purges_stale_output_before_compiler_execution(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    stale_output = prepared.root / "output" / "resume.pdf"
    stale_output.parent.mkdir(mode=0o700)
    stale_output.write_bytes(b"stale private output")
    stale_output.chmod(0o600)

    def failed_run(command, **kwargs):
        assert not stale_output.exists()
        assert not stale_output.parent.exists()
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="")

    monkeypatch.setattr(service, "_run_compiler", failed_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_build_failed"
    assert not stale_output.parent.exists()


def test_build_verifies_pdf_bytes_before_publishing_output(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup

    def successful_run(command, **kwargs):
        compiled = Path(kwargs["cwd"]) / Path(command[-1]).with_suffix(".pdf")
        _write_text_pdf(compiled, "Avery Example verified in memory")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    from jobsearch_skill import cv_build

    actual_reader = cv_build.PdfReader

    def reader_before_publication(stream):
        assert not (prepared.root / "output").exists()
        return actual_reader(stream)

    monkeypatch.setattr(service, "_run_compiler", successful_run)
    monkeypatch.setattr(cv_build, "PdfReader", reader_before_publication)

    result = service.build(run.run_id)

    assert result.verified is True
    assert result.pdf.exists()


def test_build_normalizes_page_extraction_failure_without_publishing_output(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup

    def successful_run(command, **kwargs):
        compiled = Path(kwargs["cwd"]) / Path(command[-1]).with_suffix(".pdf")
        _write_text_pdf(compiled, "Avery Example parser fixture")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class BrokenPage:
        def extract_text(self) -> str:
            raise TypeError("synthetic parser failure")

    class BrokenReader:
        is_encrypted = False
        pages = [BrokenPage()]

    monkeypatch.setattr(service, "_run_compiler", successful_run)
    monkeypatch.setattr("jobsearch_skill.cv_build.PdfReader", lambda stream: BrokenReader())

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_pdf_verification"
    assert not (prepared.root / "output").exists()
    assert caught.value.log_path is not None and caught.value.log_path.exists()


def test_build_rejects_stale_compiler_workspace_symlink_without_following(
    build_setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, _, run, prepared, _, _ = build_setup
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    canary = outside / "must-remain"
    canary.write_text("outside", encoding="utf-8")
    stale_link = prepared.root / ".compiler-work-stale"
    stale_link.symlink_to(outside, target_is_directory=True)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("compiler must not run with an unsafe stale workspace")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_generated_output_unsafe"
    assert canary.read_text(encoding="utf-8") == "outside"
    assert stale_link.is_symlink()


def test_build_uses_exact_safe_latexmk_invocation(build_setup, monkeypatch) -> None:
    service, _, run, prepared, _, _ = build_setup
    actual_popen = subprocess.Popen
    observed: dict[str, object] = {}

    class RecordingProcess:
        def __init__(self, process: subprocess.Popen[str]) -> None:
            self._process = process

        @property
        def pid(self) -> int:
            return self._process.pid

        @property
        def returncode(self) -> int | None:
            return self._process.returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            observed.setdefault("communicate_timeouts", []).append(timeout)
            return self._process.communicate(timeout=timeout)

    def recording_popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        observed["cwd_mode"] = stat.S_IMODE(Path(kwargs["cwd"]).stat().st_mode)
        return RecordingProcess(actual_popen(command, **kwargs))

    monkeypatch.setattr("jobsearch_skill.cv_build.subprocess.Popen", recording_popen)

    service.build(run.run_id)

    assert observed["command"] == [
        "latexmk",
        "-pdf",
        "-interaction=nonstopmode",
        "-halt-on-error",
        prepared.tex.name,
    ]
    compiler_cwd = Path(observed["cwd"])
    assert compiler_cwd != prepared.tex.parent
    assert compiler_cwd.parent == prepared.root
    assert compiler_cwd.name.startswith(".compiler-work-")
    assert observed["cwd_mode"] == 0o700
    assert not compiler_cwd.exists()
    assert observed["shell"] is False
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE
    assert observed["text"] is True
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
    assert observed["start_new_session"] is True
    assert observed["communicate_timeouts"] == [120.0]
    environment = observed["env"]
    assert environment["shell_escape"] == "0"
    assert environment["openin_any"] == "p"
    assert environment["openout_any"] == "p"
    assert environment["HOME"].startswith(str(prepared.root))
    assert environment["XDG_CONFIG_HOME"].startswith(str(prepared.root))
    assert environment["LATEXMKRCSYS"].startswith(str(prepared.root))
    assert environment["LATEXMKRC"].startswith(str(prepared.root))
    assert "PRIVATE_AMBIENT_TOKEN" not in environment.values()


def test_build_normalizes_invalid_compiler_utf8_to_value_free_diagnostics(
    build_setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, _, run, prepared, _, _ = build_setup
    bin_dir = tmp_path / "invalid-byte-bin"
    bin_dir.mkdir()
    latexmk = bin_dir / "latexmk"
    latexmk.write_text(
        "#!/bin/sh\n"
        "printf '\\377'\n"
        "printf '\\376' >&2\n"
        "exit 2\n",
        encoding="utf-8",
    )
    latexmk.chmod(0o700)
    monkeypatch.setattr(
        "jobsearch_skill.cv_build.isolated_latex_environment",
        lambda root: {"PATH": str(bin_dir)},
    )

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_build_failed"
    assert caught.value.log_path is not None
    log = json.loads(caught.value.log_path.read_text(encoding="utf-8"))
    assert log["stdout_present"] is True
    assert log["stderr_present"] is True
    assert not (prepared.root / "output").exists()


@pytest.mark.parametrize("ambient_kind", ["home", "system"])
def test_build_does_not_execute_ambient_latexmk_rc(
    build_setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ambient_kind: str
) -> None:
    service, _, run, _, _, _ = build_setup
    sentinel = tmp_path / f"{ambient_kind}-rc-executed"
    malicious_rc = tmp_path / f"{ambient_kind}.latexmkrc"
    malicious_rc.write_text(f'system("touch {sentinel}");\n', encoding="utf-8")
    if ambient_kind == "home":
        ambient_home = tmp_path / "ambient-home"
        ambient_home.mkdir()
        (ambient_home / ".latexmkrc").write_bytes(malicious_rc.read_bytes())
        monkeypatch.setenv("HOME", str(ambient_home))
    else:
        monkeypatch.setenv("LATEXMKRCSYS", str(malicious_rc))
    monkeypatch.setenv("PRIVATE_AMBIENT_TOKEN", "PRIVATE_AMBIENT_TOKEN")

    result = service.build(run.run_id)

    assert result.verified is True
    assert not sentinel.exists()


def test_build_rejects_project_latexmk_rc_before_execution(build_setup, tmp_path: Path) -> None:
    service, _, run, prepared, _, _ = build_setup
    sentinel = tmp_path / "project-rc-executed"
    (prepared.source_dir / ".latexmkrc").write_text(
        f'system("touch {sentinel}");\n', encoding="utf-8"
    )

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_prepared_tampered"
    assert not sentinel.exists()


def test_real_build_invokes_no_ghostscript_or_network_helpers(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    actual_run = service._run_compiler
    observed_database = ""

    def recording_run(command, *, cwd, env):
        nonlocal observed_database
        result = actual_run(command, cwd=cwd, env=env)
        database = Path(cwd) / Path(command[-1]).with_suffix(".fdb_latexmk")
        observed_database = database.read_text(encoding="utf-8", errors="replace")
        return result

    monkeypatch.setattr(service, "_run_compiler", recording_run)

    service.build(run.run_id)

    database = observed_database.casefold()
    assert "ghostscript" not in database
    assert "ps2pdf" not in database
    assert "curl " not in database
    assert not prepared.tex.with_suffix(".log").exists()
    assert not prepared.tex.with_suffix(".fls").exists()
    assert not prepared.tex.with_suffix(".fdb_latexmk").exists()
    assert not prepared.tex.with_suffix(".aux").exists()


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout"])
def test_build_tool_failures_preserve_private_redacted_log(
    build_setup, monkeypatch, failure: str
) -> None:
    service, _, run, _, _, _ = build_setup
    private_path = str(service.home)
    stdout_canary = f"RAW_STDOUT_TOKEN_91 Avery Example {private_path}"
    stderr_canary = "RAW_STDERR_TOKEN_73 avery@example.invalid PRIVATE_VALUE_FRAGMENT"

    def failed_run(command, **kwargs):
        if failure == "missing":
            raise FileNotFoundError(stdout_canary)
        sidecar = Path(kwargs["cwd"]) / "compiler-private.log"
        sidecar.write_text(stderr_canary, encoding="utf-8")
        if failure == "timeout":
            partial = Path(kwargs["cwd"]) / "partial.aux"
            partial.write_text(stdout_canary, encoding="utf-8")
            partial.chmod(0o644)
            raise subprocess.TimeoutExpired(
                command, 120, output=stdout_canary, stderr=stderr_canary
            )
        return subprocess.CompletedProcess(
            command, 2, stdout=stdout_canary, stderr=stderr_canary
        )

    monkeypatch.setattr(service, "_run_compiler", failed_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    error = caught.value
    assert error.log_path is not None and error.log_path.exists()
    log_text = error.log_path.read_text(encoding="utf-8")
    log = json.loads(log_text)
    assert stat.S_IMODE(error.log_path.stat().st_mode) == 0o600
    assert set(log) == {
        "output_present",
        "page_count",
        "reason_code",
        "return_code",
        "schema_version",
        "stage",
        "stderr_present",
        "stdout_present",
        "timed_out",
    }
    assert log["schema_version"] == 1
    assert log["reason_code"] == {
        "missing": "cv_build_tool_missing",
        "nonzero": "cv_build_failed",
        "timeout": "cv_build_timeout",
    }[failure]
    assert log["stage"] == "compiler"
    assert log["return_code"] == (2 if failure == "nonzero" else -1)
    assert log["timed_out"] is (failure == "timeout")
    assert log["stdout_present"] is (failure in {"nonzero", "timeout"})
    assert log["stderr_present"] is (failure in {"nonzero", "timeout"})
    assert log["output_present"] is False
    assert log["page_count"] == 0
    for fragment in (
        "RAW_STDOUT_TOKEN_91",
        "RAW_STDERR_TOKEN_73",
        "PRIVATE_VALUE_FRAGMENT",
        "Avery",
        "example.invalid",
        "PRIVATE_BUILD_HOME_CANARY",
        private_path,
    ):
        assert fragment not in log_text
    assert "fallback" not in error.decision
    assert private_path not in str(error)
    assert not (error.log_path.parent / "source" / "compiler-private.log").exists()
    assert not (error.log_path.parent / "source" / "partial.aux").exists()


def test_build_timeout_kills_compiler_process_group_before_cleanup(
    build_setup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, _, run, prepared, _, _ = build_setup
    assert prepared.pdf is not None
    declared_pdf_hash = hashlib.sha256(prepared.pdf.read_bytes()).hexdigest()
    ready = tmp_path / "child-ready"
    sentinel = tmp_path / "late-child-write"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    latexmk = bin_dir / "latexmk"
    latexmk.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import signal\n"
        "import time\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "    os.closerange(0, 256)\n"
        f"    open({str(ready)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(1.3)\n"
        f"    open({str(sentinel)!r}, 'wb').close()\n"
        "    time.sleep(10)\n"
        "else:\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )
    latexmk.chmod(0o700)
    run_compiler = service._run_compiler

    def run_local_compiler(command, **kwargs):
        return run_compiler([str(latexmk)], **kwargs)

    monkeypatch.setattr(service, "_run_compiler", run_local_compiler)
    monkeypatch.setattr(
        "jobsearch_skill.cv_build.isolated_latex_environment",
        lambda root: {"PATH": str(bin_dir)},
    )
    monkeypatch.setattr("jobsearch_skill.cv_build._BUILD_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr("jobsearch_skill.cv_build._TERMINATE_GRACE_SECONDS", 0.1)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_build_timeout"
    assert ready.exists()
    child_pid = int(ready.read_text(encoding="utf-8"))
    child_deadline = time.monotonic() + 0.5
    while True:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= child_deadline:
            pytest.fail("compiler child remained alive after bounded group cleanup")
        time.sleep(0.01)
    time.sleep(0.5)
    assert not sentinel.exists()
    assert hashlib.sha256(prepared.pdf.read_bytes()).hexdigest() == declared_pdf_hash
    assert sorted(path.name for path in prepared.source_dir.iterdir()) == [
        "resume.pdf",
        "resume.tex",
    ]
    assert not (prepared.root / "output").exists()


def test_timeout_cleanup_checks_the_group_after_the_leader_pipes_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedLeader:
        pid = 43123

        def __init__(self) -> None:
            self.communicate_timeouts: list[float] = []

        def communicate(self, *, timeout: float):
            self.communicate_timeouts.append(timeout)
            return "", ""

    leader = ExitedLeader()
    group_alive = True
    delivered: list[signal.Signals] = []

    def fake_killpg(process_group: int, sent_signal: signal.Signals | int) -> None:
        nonlocal group_alive
        assert process_group == leader.pid
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        delivered.append(signal.Signals(sent_signal))
        if sent_signal == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr("jobsearch_skill.cv_build.os.killpg", fake_killpg)
    monkeypatch.setattr("jobsearch_skill.cv_build._TERMINATE_GRACE_SECONDS", 0.01)

    CVService._terminate_process_group(leader)  # type: ignore[arg-type]

    assert delivered == [signal.SIGTERM, signal.SIGKILL]
    assert group_alive is False
    assert leader.communicate_timeouts
    assert all(timeout <= 0.01 for timeout in leader.communicate_timeouts)


def test_verified_rebuild_revalidates_prepared_source_bytes(build_setup) -> None:
    service, _, run, prepared, _, _ = build_setup
    service.build(run.run_id)
    assert prepared.tex is not None
    prepared.tex.write_bytes(prepared.tex.read_bytes() + b"\n% tampered")

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_prepared_tampered"


def test_verified_rebuild_rejects_changed_output_pdf(build_setup) -> None:
    service, _, run, _, _, _ = build_setup
    result = service.build(run.run_id)
    result.pdf.write_bytes(result.pdf.read_bytes() + b"\nchanged verified artifact")

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_pdf_verification"


@pytest.mark.parametrize("document_kind", ["facts", "evidence"])
def test_build_revalidates_evidence_text_and_fact_anchors(
    build_setup, monkeypatch: pytest.MonkeyPatch, document_kind: str
) -> None:
    service, _, run, prepared, _, _ = build_setup
    source_hash = str(prepared.manifest["source_hash"])
    if document_kind == "facts":
        path = service.facts_path(run.run_id, source_hash)
        document = service.store.read_json(path, "cv-facts.v1")
        document["projects"][0]["evidence_anchor"] = "FABRICATED_EVIDENCE_ANCHOR"
        service.store.write_json(path, document, "cv-facts.v1")
    else:
        path = service.run_store.runs_dir / run.run_id / f"cv-evidence-{source_hash}.json"
        document = service.store.read_json(path, "cv-evidence.v1")
        document["sources"][0]["text"] = "FABRICATED_EVIDENCE_TEXT"
        service.store.write_json(path, document, "cv-evidence.v1")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("compiler must not run for unbound evidence")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code in {"cv_evidence_mismatch", "cv_facts_mismatch"}


def test_build_rejects_manifest_tex_escape_before_subprocess(build_setup, monkeypatch) -> None:
    service, _, run, prepared, _, _ = build_setup
    manifest = dict(prepared.manifest)
    manifest["expected_tex"] = "../manifest.yaml"
    prepared.manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    prepared.manifest_path.chmod(0o600)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError):
        service.build(run.run_id)


def test_build_rejects_manifest_omitting_declared_pdf(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    manifest = dict(prepared.manifest)
    manifest["expected_pdf"] = None
    prepared.manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    prepared.manifest_path.chmod(0o600)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess must not run for an incomplete manifest")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_prepare_conflict"


@pytest.mark.parametrize("field", ["run_id", "cv_name", "source_hash", "source_hashes"])
def test_build_rebinds_facts_to_manifest_and_run(build_setup, monkeypatch, field: str) -> None:
    service, _, run, prepared, _, _ = build_setup
    source_hash = prepared.manifest["source_hash"]
    path = service.facts_path(run.run_id, source_hash)
    facts = service.store.read_json(path, "cv-facts.v1")
    if field == "source_hashes":
        facts[field] = [{"source_ref": "resume.tex", "sha256": "f" * 64}]
    elif field == "source_hash":
        facts[field] = "f" * 64
    else:
        facts[field] = "run_transplanted" if field == "run_id" else "Transplanted CV"
    service.store.write_json(path, facts, "cv-facts.v1")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("compiler must not run for transplanted facts")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)
    assert caught.value.reason_code == "cv_facts_mismatch"


def test_build_binds_prepared_artifacts_to_the_run_selected_cv(
    build_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, run, prepared, _, _ = build_setup
    transplanted_name = "Transplanted CV"
    manifest = service._read_manifest(prepared.manifest_path)
    manifest["source_cv_name"] = transplanted_name
    service._atomic_manifest(prepared.manifest_path, manifest)
    source_hash = str(manifest["source_hash"])

    facts_path = service.facts_path(run.run_id, source_hash)
    facts = service.store.read_json(facts_path, "cv-facts.v1")
    facts["cv_name"] = transplanted_name
    service.store.write_json(facts_path, facts, "cv-facts.v1")

    evidence_path = (
        service.run_store.runs_dir
        / run.run_id
        / f"cv-evidence-{source_hash}.json"
    )
    evidence = service.store.read_json(evidence_path, "cv-evidence.v1")
    evidence["cv_name"] = transplanted_name
    service.store.write_json(evidence_path, evidence, "cv-evidence.v1")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("compiler must not run for a transplanted CV")

    monkeypatch.setattr(service, "_run_compiler", unexpected_run)

    with pytest.raises(CVBuildError) as caught:
        service.build(run.run_id)

    assert caught.value.reason_code == "cv_manifest_mismatch"


@pytest.mark.parametrize("output_kind", ["malformed", "zero", "encrypted", "no_text", "symlink"])
def test_build_rejects_unverifiable_pdf_with_safe_log(
    build_setup, monkeypatch, output_kind: str
) -> None:
    service, _, run, prepared, _, _ = build_setup

    def fake_run(command, **kwargs):
        output = Path(kwargs["cwd"]) / Path(command[-1]).with_suffix(".pdf")
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

    monkeypatch.setattr(service, "_run_compiler", fake_run)

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

    assert prepared.tex is not None
    rewritten_claim = "Built Python API services."
    request = {
        "schema_version": 1,
        "run_id": run.run_id,
        "cv_name": prepared.selection.name,
        "source_hash": prepared.manifest["source_hash"],
        "customized_tex": prepared.tex.read_text(encoding="utf-8").replace(
            "Built Python API services with deterministic tests and careful error handling.",
            rewritten_claim,
        ),
        "claims": [
            {
                "claim": rewritten_claim,
                "evidence_anchors": ["Built Python API services"],
                "fact_refs": ["/employment/0"],
            }
        ],
        "unsupported_requirements": ["Kubernetes"],
    }
    request_path = home / "runs" / run.run_id / "customization-input.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    request_path.chmod(0o600)
    assert main(
        [
            "--home",
            str(home),
            "cv",
            "customize",
            "--run-id",
            run.run_id,
            "--input",
            str(request_path),
        ]
    ) == 0
    customize_payload = json.loads(capsys.readouterr().out)
    assert customize_payload["command"] == "cv.customize"
    assert set(customize_payload["result"]) == {
        "claim_count",
        "customization_ref",
        "customized_tex_ref",
        "status",
    }

    assert main(["--home", str(home), "cv", "build", "--run-id", run.run_id]) == 0
    build_payload = json.loads(capsys.readouterr().out)
    assert build_payload["command"] == "cv.build"
    assert set(build_payload["result"]) == {"page_count", "pdf_ref", "status", "verified"}

    combined = json.dumps(
        [evidence_payload, facts_payload, prepare_payload, customize_payload, build_payload],
        sort_keys=True,
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

    monkeypatch.setattr(
        "jobsearch_skill.cv_build.CVBuildMixin._run_compiler",
        staticmethod(failed_run),
    )

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
