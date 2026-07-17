from __future__ import annotations

import base64
import json
import stat
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter

from jobsearch_skill.cv import CVRegistry, CVService
from jobsearch_skill.errors import CVBuildError, CVFactsError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.runs import RunStore
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
def evidence_setup(tmp_path: Path):
    home = tmp_path / ".jobsearch"
    root = home / "cvs" / "avery"
    root.mkdir(parents=True)
    fixture_root = Path(__file__).parents[1] / "fixtures" / "latex-cv"
    (root / "resume.tex").write_bytes((fixture_root / "resume.tex").read_bytes())
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
    registry = CVRegistry(
        {
            "default_cv": "Avery Example CV",
            "cvs": [
                {
                    "name": "Avery Example CV",
                    "root": "cvs/avery",
                    "tex": "resume.tex",
                    "assets": [],
                }
            ],
        },
        home,
    )
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
        {"name": selection.name, "path": str(selection.tex), "customized": True},
    )
    service = CVService(home, store, runs, registry)
    return service, selection, run, fixture_root


def _facts(fixture_root: Path, evidence) -> dict[str, object]:
    value = json.loads((fixture_root / "evidence.json").read_text(encoding="utf-8"))
    value["source_hash"] = evidence.source_hash
    value["run_id"] = evidence.run_id
    value["source_hashes"] = [
        {"source_ref": source_ref, "sha256": digest}
        for source_ref, digest in sorted(evidence.source_hashes.items())
    ]
    return value


def test_evidence_is_source_bound_private_and_contains_only_supported_text(evidence_setup) -> None:
    service, selection, run, _ = evidence_setup

    evidence = service.evidence(run.run_id, selection)

    assert evidence.source_hash
    assert evidence.source_hashes.keys() == {"resume.tex"}
    assert "Built Python API services" in evidence.extracted_text
    assert "Kubernetes" not in evidence.extracted_text
    assert evidence.path.parent == service.run_store.runs_dir / run.run_id
    assert stat.S_IMODE(evidence.path.stat().st_mode) == 0o600
    repeated = service.evidence(run.run_id, selection)
    assert repeated.path == evidence.path
    assert repeated.path.read_bytes() == evidence.path.read_bytes()


def test_cv_facts_require_matching_source_hash_and_literal_evidence(evidence_setup) -> None:
    service, selection, run, fixture_root = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    facts = _facts(fixture_root, evidence)

    stored = service.store_facts(run.run_id, selection, facts)

    assert stored["education"][0]["institution"] == "Example University"
    assert stored["employment"][0]["title"] == "Software Engineering Intern"
    assert stat.S_IMODE(service.facts_path(run.run_id, evidence.source_hash).stat().st_mode) == 0o600

    unsupported = deepcopy(facts)
    unsupported["projects"][0]["evidence_anchor"] = "Kubernetes production cluster"
    with pytest.raises(CVFactsError, match="evidence anchor"):
        service.store_facts(run.run_id, selection, unsupported)


@pytest.mark.parametrize("category", ["education", "employment", "projects"])
def test_each_claim_category_requires_a_nonempty_literal_anchor(
    evidence_setup, category: str
) -> None:
    service, selection, run, fixture_root = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    facts = _facts(fixture_root, evidence)
    facts[category][0]["evidence_anchor"] = ""

    with pytest.raises(CVFactsError):
        service.store_facts(run.run_id, selection, facts)


def test_stale_or_mismatched_facts_cannot_be_reused(evidence_setup) -> None:
    service, selection, run, fixture_root = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    facts = _facts(fixture_root, evidence)

    mismatched = deepcopy(facts)
    mismatched["source_hash"] = "f" * 64
    with pytest.raises(CVFactsError, match="source"):
        service.store_facts(run.run_id, selection, mismatched)

    assert selection.tex is not None
    selection.tex.write_text(selection.tex.read_text() + "\n% changed source", encoding="utf-8")
    with pytest.raises(CVFactsError, match="source"):
        service.store_facts(run.run_id, selection, facts)


def test_facts_reject_wrong_cv_identity(evidence_setup) -> None:
    service, selection, run, fixture_root = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    facts = _facts(fixture_root, evidence)
    facts["cv_name"] = "Another CV"

    with pytest.raises(CVFactsError, match="source"):
        service.store_facts(run.run_id, selection, facts)


@pytest.mark.parametrize(
    "field", ["run_id", "cv_name", "source_hash", "source_hashes", "sources"]
)
def test_persisted_evidence_is_rebound_to_current_run_selection_and_aggregate(
    evidence_setup, field: str
) -> None:
    service, selection, run, _ = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    document = service.store.read_json(evidence.path, "cv-evidence.v1")
    if field == "source_hash":
        document[field] = "f" * 64
    elif field == "source_hashes":
        document[field] = [{"source_ref": "resume.tex", "sha256": "f" * 64}]
    elif field == "sources":
        document[field] = [
            {
                "source_ref": "resume.tex",
                "kind": "tex",
                "text": "Injected unsupported evidence",
            }
        ]
    else:
        document[field] = "transplanted"
    service.store.write_json(evidence.path, document, "cv-evidence.v1")

    with pytest.raises(CVBuildError, match="evidence"):
        service.evidence(run.run_id, selection)


def test_facts_require_the_current_run_id(evidence_setup) -> None:
    service, selection, run, fixture_root = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    facts = _facts(fixture_root, evidence)
    facts["run_id"] = "run_transplanted"

    with pytest.raises(CVFactsError, match="source"):
        service.store_facts(run.run_id, selection, facts)


def test_persisted_evidence_rejects_duplicate_source_hash_reference(
    evidence_setup,
) -> None:
    service, selection, run, _ = evidence_setup
    evidence = service.evidence(run.run_id, selection)
    document = service.store.read_json(evidence.path, "cv-evidence.v1")
    document["source_hashes"] = [
        {"source_ref": "resume.tex", "sha256": "f" * 64},
        *document["source_hashes"],
    ]
    service.store.write_json(evidence.path, document, "cv-evidence.v1")

    with pytest.raises(CVBuildError, match="evidence"):
        service.evidence(run.run_id, selection)


def test_persisted_evidence_rejects_reordered_source_hashes(evidence_setup) -> None:
    service, selection, run, _ = evidence_setup
    asset = selection.root / "declared.png"
    asset.write_bytes(_PNG)
    selection_with_asset = replace(selection, assets=(asset,))
    evidence = service.evidence(run.run_id, selection_with_asset)
    document = service.store.read_json(evidence.path, "cv-evidence.v1")
    document["source_hashes"] = list(reversed(document["source_hashes"]))
    service.store.write_json(evidence.path, document, "cv-evidence.v1")

    with pytest.raises(CVBuildError, match="evidence"):
        service.evidence(run.run_id, selection_with_asset)


@pytest.mark.parametrize("kind", ["malformed", "encrypted", "no_text"])
def test_pdf_evidence_failures_use_fixed_safe_errors(tmp_path: Path, kind: str) -> None:
    home = tmp_path / "PRIVATE_PDF_EVIDENCE_CANARY" / ".jobsearch"
    root = home / "cvs" / "pdf"
    root.mkdir(parents=True)
    pdf = root / "resume.pdf"
    if kind == "malformed":
        pdf.write_bytes(b"PRIVATE_PDF_CONTENT_CANARY")
    else:
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        if kind == "encrypted":
            writer.encrypt("private-password")
        with pdf.open("wb") as stream:
            writer.write(stream)
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
    registry = CVRegistry(
        {"default_cv": "PDF", "cvs": [{"name": "PDF", "root": "cvs/pdf", "pdf": "resume.pdf", "assets": []}]},
        home,
    )
    selection = registry.resolve(None)
    context = make_job_context(
        job_url="https://example.invalid/jobs/pdf-evidence",
        company="Synthetic Systems",
        role="Backend Engineer",
        description="Public fixture",
    )
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis(run.run_id, str(context["job_fingerprint"])))
    runs.select_cv(run.run_id, {"name": "PDF", "path": str(pdf), "customized": False})

    with pytest.raises(Exception) as caught:
        CVService(home, store, runs, registry).evidence(run.run_id, selection)

    assert "PRIVATE_PDF_CONTENT_CANARY" not in str(caught.value)
    assert "PRIVATE_PDF_EVIDENCE_CANARY" not in str(caught.value)
