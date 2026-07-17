from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from jobsearch_skill.cv import CVRegistry, CVSelection, CVService
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


def _hashes(selection: CVSelection) -> dict[str, str]:
    paths = tuple(path for path in (selection.tex, selection.pdf, *selection.assets) if path)
    return {
        path.relative_to(selection.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


@pytest.fixture
def cv_setup(tmp_path: Path):
    home = tmp_path / ".jobsearch"
    root = home / "cvs" / "avery"
    root.mkdir(parents=True)
    fixture = Path(__file__).parents[1] / "fixtures" / "latex-cv" / "resume.tex"
    (root / "resume.tex").write_bytes(fixture.read_bytes())
    (root / "declared.txt").write_text("declared public asset\n", encoding="utf-8")
    (root / "undeclared.txt").write_text("must not be copied\n", encoding="utf-8")
    preferences = {
        "default_cv": "Avery Example CV",
        "cvs": [
            {
                "name": "Avery Example CV",
                "root": "cvs/avery",
                "tex": "resume.tex",
                "assets": ["declared.txt"],
            }
        ],
    }
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
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
        {"name": selection.name, "path": str(selection.tex), "customized": True},
    )
    service = CVService(home, store, runs, registry)
    return service, selection, run, root


def test_prepare_copies_only_declared_inputs_and_preserves_source_hashes(cv_setup) -> None:
    service, selection, run, root = cv_setup
    before = _hashes(selection)

    prepared = service.prepare(run.run_id, selection)

    assert prepared.tex is not None
    assert prepared.tex.parent.is_relative_to(service.generated_root)
    assert _hashes(selection) == before
    assert prepared.manifest["source_hashes"] == before
    assert sorted(path.relative_to(prepared.source_dir).as_posix() for path in prepared.source_dir.rglob("*") if path.is_file()) == [
        "declared.txt",
        "resume.tex",
    ]
    assert not (prepared.source_dir / root.joinpath("undeclared.txt").name).exists()
    assert stat.S_IMODE(prepared.tex.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.tex.stat().st_mode) == 0o600


def test_prepare_rejects_asset_outside_declared_cv_root(cv_setup, tmp_path: Path) -> None:
    service, selection, run, _ = cv_setup
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"public fixture")

    with pytest.raises(CVBuildError, match="declared CV root"):
        service.prepare(run.run_id, replace(selection, assets=(outside,)))


def test_prepare_is_idempotent_and_rejects_changed_sources(cv_setup) -> None:
    service, selection, run, _ = cv_setup
    first = service.prepare(run.run_id, selection)
    first_manifest = first.manifest_path.read_bytes()

    repeated = service.prepare(run.run_id, selection)
    assert repeated.manifest_path.read_bytes() == first_manifest

    assert selection.tex is not None
    selection.tex.write_text(selection.tex.read_text() + "\n% changed", encoding="utf-8")
    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, selection)


def test_untrusted_job_names_cannot_escape_generated_root(tmp_path: Path) -> None:
    home = tmp_path / ".jobsearch"
    root = home / "cvs" / "avery"
    root.mkdir(parents=True)
    tex = root / "resume.tex"
    tex.write_text("\\documentclass{article}\\begin{document}Avery Example\\end{document}")
    schemas = SchemaRegistry()
    store = SafeStore(schemas, home / "backups")
    runs = RunStore(store, home / "runs")
    registry = CVRegistry(
        {"default_cv": "Avery", "cvs": [{"name": "Avery", "root": "cvs/avery", "tex": "resume.tex", "assets": []}]},
        home,
    )
    selection = registry.resolve(None, for_customization=True)
    context = make_job_context(
        job_url="https://example.invalid/jobs/escape",
        company="../../outside",
        role="..\\Backend/../../Engineer",
        description="Public fixture",
    )
    run = runs.start(context)
    runs.save_analysis(run.run_id, _analysis(run.run_id, str(context["job_fingerprint"])))
    runs.select_cv(run.run_id, {"name": selection.name, "path": str(tex), "customized": True})

    prepared = CVService(home, store, runs, registry).prepare(run.run_id, selection)

    assert prepared.source_dir.resolve().is_relative_to((home / "generated").resolve())
    assert not (tmp_path / "outside").exists()


def test_prepare_rejects_symlink_special_and_duplicate_sources(cv_setup, tmp_path: Path) -> None:
    service, selection, run, root = cv_setup
    outside = tmp_path / "target.txt"
    outside.write_text("public fixture", encoding="utf-8")
    symlink = root / "linked.txt"
    symlink.symlink_to(outside)
    with pytest.raises(CVBuildError):
        service.prepare(run.run_id, replace(selection, assets=(symlink,)))

    fifo = root / "special.pipe"
    os.mkfifo(fifo)
    with pytest.raises(CVBuildError):
        service.prepare(run.run_id, replace(selection, assets=(fifo,)))

    assert selection.tex is not None
    with pytest.raises(CVBuildError):
        service.prepare(run.run_id, replace(selection, assets=(selection.tex,)))


def test_prepare_preserves_nested_relative_asset_paths(cv_setup) -> None:
    service, selection, run, root = cv_setup
    nested = root / "figures" / "diagram.txt"
    nested.parent.mkdir()
    nested.write_text("public diagram", encoding="utf-8")

    prepared = service.prepare(run.run_id, replace(selection, assets=(nested,)))

    assert (prepared.source_dir / "figures" / "diagram.txt").read_bytes() == nested.read_bytes()


def test_prepare_copy_failure_leaves_no_partial_manifest(
    cv_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, selection, run, _ = cv_setup
    calls = 0
    original = service._copy_atomic

    def fail_second(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CVBuildError("copy failed", reason_code="cv_source_invalid")
        original(source, target)

    monkeypatch.setattr(service, "_copy_atomic", fail_second)

    with pytest.raises(CVBuildError):
        service.prepare(run.run_id, selection)

    assert not list(service.generated_root.rglob("manifest.yaml"))
    assert not list(service.generated_root.rglob(run.run_id))


@pytest.mark.parametrize("value", [".", ".."])
def test_slug_rejects_dot_components(cv_setup, value: str) -> None:
    service, _, _, _ = cv_setup

    with pytest.raises(CVBuildError):
        service._slug(value)
