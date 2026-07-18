from __future__ import annotations

import hashlib
import os
import stat
import base64
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from jobsearch_skill.cv import CVRegistry, CVSelection, CVService
from jobsearch_skill.errors import CVBuildError
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
    (root / "declared.png").write_bytes(_PNG)
    (root / "undeclared.txt").write_text("must not be copied\n", encoding="utf-8")
    preferences = {
        "default_cv": "Avery Example CV",
        "cvs": [
            {
                "name": "Avery Example CV",
                "root": "cvs/avery",
                "tex": "resume.tex",
                "assets": ["declared.png"],
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
        "declared.png",
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
    nested = root / "figures" / "diagram.png"
    nested.parent.mkdir()
    nested.write_bytes(_PNG)

    prepared = service.prepare(run.run_id, replace(selection, assets=(nested,)))

    assert (prepared.source_dir / "figures" / "diagram.png").read_bytes() == nested.read_bytes()


def test_prepare_copy_failure_leaves_no_partial_manifest(
    cv_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, selection, run, _ = cv_setup
    calls = 0
    original = service._copy_bytes_atomic

    def fail_second(data: bytes, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CVBuildError("copy failed", reason_code="cv_source_invalid")
        original(data, target)

    monkeypatch.setattr(service, "_copy_bytes_atomic", fail_second)

    with pytest.raises(CVBuildError):
        service.prepare(run.run_id, selection)

    assert not list(service.generated_root.rglob("manifest.yaml"))
    assert not list(service.generated_root.rglob(run.run_id))


@pytest.mark.parametrize("value", [".", ".."])
def test_slug_rejects_dot_components(cv_setup, value: str) -> None:
    service, _, _, _ = cv_setup

    with pytest.raises(CVBuildError):
        service._slug(value)


@pytest.mark.parametrize(
    "source",
    [
        r"\documentclass{article}\immediate\write18{curl https://example.invalid}",
        r"\documentclass{article}\input{|gs --version}",
        r"\documentclass{article}\csname input\endcsname{/etc/passwd}",
        r"\documentclass{article}\catcode`\@=0",
        r"\documentclass{article}\usepackage{shellesc}",
        r"\documentclass{standalone}\begin{document}Avery Example\end{document}",
        r"\documentclass{article}\special{ps: plotfile secret}",
        (
            r"\documentclass{article}\usepackage{graphicx}\begin{document}"
            r"\includegraphics*{/etc/passwd}\end{document}"
        ),
    ],
)
def test_prepare_rejects_active_tex_surfaces_before_copy(cv_setup, source: str) -> None:
    service, selection, run, _ = cv_setup
    assert selection.tex is not None
    selection.tex.write_text(source, encoding="utf-8")

    with pytest.raises(CVBuildError, match="unsafe"):
        service.prepare(run.run_id, selection)

    assert not list(service.generated_root.rglob("manifest.yaml"))


@pytest.mark.parametrize(
    "graphics",
    [
        r"\includegraphics *{/etc/passwd}",
        "\\includegraphics% ignored comment\n * {../outside.png}",
        r"\includegraphics * junk {declared.png}",
        r"\includegraphics * [width=1cm] {undeclared.png}",
        (
            r"\includegraphics * {declared.png}"
            r"\includegraphics * {/etc/passwd}"
        ),
    ],
)
def test_prepare_rejects_every_unconsumed_or_undeclared_graphics_variant(
    cv_setup, graphics: str
) -> None:
    service, selection, run, _ = cv_setup
    assert selection.tex is not None
    selection.tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n"
        f"{graphics}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    with pytest.raises(CVBuildError, match="unsafe"):
        service.prepare(run.run_id, selection)


@pytest.mark.parametrize(
    "graphics",
    [
        r"\includegraphics * {declared.png}",
        r"\includegraphics * [width=1cm] {declared.png}",
        "\\includegraphics% ignored comment\n * [width=1cm] {declared.png}",
        r"\includegraphics {declared}",
    ],
)
def test_prepare_accepts_fully_consumed_declared_graphics_variants(
    cv_setup, graphics: str
) -> None:
    service, selection, run, _ = cv_setup
    assert selection.tex is not None
    selection.tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n"
        f"{graphics}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    prepared = service.prepare(run.run_id, selection)

    assert prepared.tex is not None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (".latexmkrc", b"system('false')"),
        ("latexmkrc", b"system('false')"),
        ("custom.sty", b"\\immediate\\write18{false}"),
        ("custom.cls", b"active class"),
        ("payload.tex", b"active input"),
        ("payload.lua", b"os.execute('false')"),
        ("payload.pl", b"system('false')"),
        ("payload.sh", b"#!/bin/sh\nfalse"),
        ("fake.png", b"not a png"),
    ],
)
def test_prepare_rejects_executable_or_invalid_declared_assets(
    cv_setup, name: str, content: bytes
) -> None:
    service, selection, run, root = cv_setup
    asset = root / name
    asset.write_bytes(content)

    with pytest.raises(CVBuildError, match="unsafe"):
        service.prepare(run.run_id, replace(selection, assets=(asset,)))


@pytest.mark.parametrize(
    "filename",
    [
        "-e.tex",
        ".resume.tex",
        "resume name.tex",
        "resume`curl`.tex",
        "resume$(curl).tex",
        "resume;curl.tex",
        "resume'quote.tex",
        'resume"quote.tex',
        "resume*.tex",
        "resume\n.tex",
        "résumé.tex",
        "resume.TEX",
        "resume.txt",
    ],
)
def test_prepare_rejects_nonportable_declared_reference(
    cv_setup, filename: str
) -> None:
    service, selection, _, root = cv_setup
    assert selection.tex is not None
    unsafe_tex = root / filename
    unsafe_tex.write_bytes(selection.tex.read_bytes())

    with pytest.raises(CVBuildError) as caught:
        service._declared_inputs(replace(selection, tex=unsafe_tex))

    assert caught.value.reason_code in {"cv_source_invalid", "cv_source_unsafe"}


def test_prepare_hashes_and_copies_the_same_opened_bytes(
    cv_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, selection, run, _ = cv_setup
    assert selection.tex is not None
    original = selection.tex.read_bytes()
    from jobsearch_skill import cv_security

    original_safe_read = cv_security.safe_read_relative
    changed = False

    def mutate_after_read(root: Path, relative: Path) -> bytes:
        nonlocal changed
        value = original_safe_read(root, relative)
        if root == selection.root and relative == selection.tex.relative_to(root) and not changed:
            changed = True
            selection.tex.write_bytes(value + b"\n% changed after descriptor read")
        return value

    monkeypatch.setattr(cv_security, "safe_read_relative", mutate_after_read)

    prepared = service.prepare(run.run_id, selection)

    assert changed is True
    assert prepared.tex is not None
    assert prepared.tex.read_bytes() == original
    assert prepared.manifest["source_hashes"]["resume.tex"] == hashlib.sha256(original).hexdigest()


def test_prepare_rejects_post_copy_digest_mismatch(
    cv_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, selection, run, _ = cv_setup
    original_copy = service._copy_bytes_atomic

    def corrupt_copy(data: bytes, target: Path) -> None:
        original_copy(data, target)
        if target.name == "resume.tex":
            target.write_bytes(target.read_bytes() + b"corrupt")

    monkeypatch.setattr(service, "_copy_bytes_atomic", corrupt_copy)

    with pytest.raises(CVBuildError, match="digest"):
        service.prepare(run.run_id, selection)

    assert not list(service.generated_root.rglob("manifest.yaml"))


def test_prepare_normalizes_every_generated_directory_to_0700(cv_setup) -> None:
    service, selection, run, _ = cv_setup

    prepared = service.prepare(run.run_id, selection)

    relative = prepared.root.relative_to(service.generated_root)
    current = service.generated_root
    for component in relative.parts:
        current /= component
        assert stat.S_IMODE(current.stat().st_mode) == 0o700


def test_manifest_never_stores_the_original_absolute_cv_root(cv_setup) -> None:
    service, selection, run, _ = cv_setup

    prepared = service.prepare(run.run_id, selection)

    assert "source_root" not in prepared.manifest
    assert str(selection.root) not in prepared.manifest_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation", ["delete", "change"])
def test_repeat_prepare_revalidates_every_copied_source(cv_setup, mutation: str) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    assert prepared.tex is not None
    if mutation == "delete":
        prepared.tex.unlink()
    else:
        prepared.tex.write_bytes(prepared.tex.read_bytes() + b"changed")

    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, selection)


@pytest.mark.parametrize("extra", [".latexmkrc", "resume.aux", "extra-directory"])
def test_repeat_prepare_rejects_every_extra_source_entry(cv_setup, extra: str) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    path = prepared.source_dir / extra
    if extra == "extra-directory":
        path.mkdir(mode=0o700)
    else:
        path.write_text("inert local fixture\n", encoding="utf-8")
        path.chmod(0o600)

    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, selection)


@pytest.mark.parametrize("target", ["destination", "source", "manifest", "input"])
def test_repeat_prepare_requires_private_modes(cv_setup, target: str) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    assert prepared.tex is not None
    path = {
        "destination": prepared.root,
        "source": prepared.source_dir,
        "manifest": prepared.manifest_path,
        "input": prepared.tex,
    }[target]
    path.chmod(0o755 if path.is_dir() else 0o644)

    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, selection)


def test_repeat_prepare_requires_private_mode_for_nested_directories(cv_setup) -> None:
    service, selection, run, root = cv_setup
    nested_asset = root / "figures" / "diagram.png"
    nested_asset.parent.mkdir(mode=0o700)
    nested_asset.write_bytes(_PNG)
    nested_selection = replace(selection, assets=(nested_asset,))
    prepared = service.prepare(run.run_id, nested_selection)
    (prepared.source_dir / "figures").chmod(0o755)

    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, nested_selection)


def test_manifest_rejects_casefold_colliding_source_references(cv_setup) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    manifest = dict(prepared.manifest)
    source_hashes = dict(manifest["source_hashes"])
    source_hashes["Declared.png"] = source_hashes["declared.png"]
    manifest["source_hashes"] = source_hashes
    manifest["copied_files"] = list(source_hashes)
    manifest["source_hash"] = service._aggregate_hash(source_hashes)

    with pytest.raises(CVBuildError, match="conflict"):
        service._validate_manifest_semantics(manifest)


def test_manifest_rejects_casefold_colliding_directory_references(cv_setup) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    manifest = dict(prepared.manifest)
    source_hashes = dict(manifest["source_hashes"])
    digest = source_hashes["declared.png"]
    source_hashes["Figures/first.png"] = digest
    source_hashes["figures/second.png"] = digest
    manifest["source_hashes"] = source_hashes
    manifest["copied_files"] = list(source_hashes)
    manifest["source_hash"] = service._aggregate_hash(source_hashes)

    with pytest.raises(CVBuildError, match="conflict"):
        service._validate_manifest_semantics(manifest)


@pytest.mark.parametrize(
    "field", ["copied_files", "expected_tex", "source_hash", "source_hashes"]
)
def test_repeat_prepare_rejects_manifest_source_mapping_mismatch(cv_setup, field: str) -> None:
    service, selection, run, _ = cv_setup
    prepared = service.prepare(run.run_id, selection)
    manifest = dict(prepared.manifest)
    if field == "copied_files":
        manifest[field] = ["declared.png", "resume.tex"]
    elif field == "expected_tex":
        manifest[field] = "declared.png"
    elif field == "source_hash":
        manifest[field] = "f" * 64
    else:
        manifest[field] = {"../resume.tex": "f" * 64}
    prepared.manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    prepared.manifest_path.chmod(0o600)

    with pytest.raises(CVBuildError, match="conflict"):
        service.prepare(run.run_id, selection)
