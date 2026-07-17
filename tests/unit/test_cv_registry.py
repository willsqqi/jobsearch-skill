from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobsearch_skill.cv import CVRegistry
from jobsearch_skill.errors import CVSelectionError
from jobsearch_skill.schema import SchemaRegistry


@pytest.fixture
def registry(tmp_path: Path) -> CVRegistry:
    preferences_path = Path(__file__).parents[1] / "fixtures" / "private-home" / "preferences.yaml"
    preferences = yaml.safe_load(preferences_path.read_text())
    assert isinstance(preferences, dict)
    SchemaRegistry().validate("preferences.v1", preferences)
    for directory in ("cvs/swe/figures", "cvs/de", "cvs/research"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for stem in ("swe", "de", "research"):
        root = tmp_path / "cvs" / stem
        (root / "resume.tex").write_text("\\documentclass{article}\\begin{document}Synthetic\\end{document}")
        (root / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic")
    for asset in ("architecture.png", "results.png"):
        (tmp_path / "cvs" / "swe" / "figures" / asset).write_bytes(b"synthetic")
    return CVRegistry(preferences, tmp_path)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(None, "SWE"), ("DE", "DE"), ("Research", "Research"), ("sWe", "SWE")],
)
def test_cv_registry_resolves_default_and_registered_names(
    registry: CVRegistry, reference: str | None, expected: str
) -> None:
    assert registry.resolve(reference).name == expected


def test_registered_assets_are_declared_and_keep_declared_order(registry: CVRegistry) -> None:
    selection = registry.resolve("SWE", for_customization=True)

    assert [asset.name for asset in selection.assets] == ["architecture.png", "results.png"]


def test_explicit_pdf_path_can_apply_but_cannot_customize(registry: CVRegistry, tmp_path: Path) -> None:
    pdf = tmp_path / "candidate.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")

    assert registry.resolve(str(pdf)).pdf == pdf.resolve()
    with pytest.raises(CVSelectionError, match="LaTeX source"):
        registry.resolve(str(pdf), for_customization=True)


def test_explicit_tex_path_can_be_customized(registry: CVRegistry, tmp_path: Path) -> None:
    tex = tmp_path / "candidate.tex"
    tex.write_text("\\documentclass{article}\\begin{document}Synthetic\\end{document}")

    selection = registry.resolve(str(tex), for_customization=True)

    assert selection.tex == tex.resolve()
    assert selection.pdf is None


@pytest.mark.parametrize("parent_name", ["folder with space", "résumé-parent", ".hidden"])
def test_explicit_path_allows_nonportable_parent_components(
    registry: CVRegistry, tmp_path: Path, parent_name: str
) -> None:
    parent = tmp_path / parent_name
    parent.mkdir()
    tex = parent / "candidate.tex"
    tex.write_text(
        "\\documentclass{article}\\begin{document}Synthetic\\end{document}"
    )

    selection = registry.resolve(str(tex), for_customization=True)

    assert selection.tex == tex.absolute()


def test_unregistered_tex_with_external_assets_cannot_be_customized(
    registry: CVRegistry, tmp_path: Path
) -> None:
    tex = tmp_path / "candidate.tex"
    tex.write_text("\\documentclass{article}\\includegraphics{chart.png}\\begin{document}X\\end{document}")

    with pytest.raises(CVSelectionError) as error:
        registry.resolve(str(tex), for_customization=True)

    assert error.value.reason_code == "cv_assets_unregistered"


@pytest.mark.parametrize(
    "dependency",
    [
        "\\input details",
        "\\bibliography{references}",
        "\\addbibresource{refs.bib}",
        "\\lstinputlisting[language=Python]{code.py}",
        "\\verbatiminput{snippet.txt}",
        "\\import{chapters/}{overview}",
        "\\subimport{chapters/}{overview}",
        "\\includepdf[pages=-]{appendix.pdf}",
        "\\usepackage{styles/custom}",
    ],
)
def test_unregistered_tex_with_common_active_file_commands_cannot_be_customized(
    registry: CVRegistry, tmp_path: Path, dependency: str
) -> None:
    tex = tmp_path / "candidate.tex"
    tex.write_text(f"\\documentclass{{article}}\n{dependency}\n\\begin{{document}}X\\end{{document}}")

    with pytest.raises(CVSelectionError) as error:
        registry.resolve(str(tex), for_customization=True)

    assert error.value.reason_code == "cv_assets_unregistered"


def test_unregistered_tex_ignores_commented_commands_and_standard_packages(
    registry: CVRegistry, tmp_path: Path
) -> None:
    tex = tmp_path / "candidate.tex"
    tex.write_text(
        "\\documentclass{article}\n% \\input hidden\n\\usepackage{geometry}\n"
        "\\begin{document}Synthetic\\end{document}"
    )

    assert registry.resolve(str(tex), for_customization=True).tex == tex.resolve()


def test_registered_files_always_resolve_relative_to_declared_root(tmp_path: Path) -> None:
    root = tmp_path / "cvs" / "swe"
    root.mkdir(parents=True)
    (tmp_path / "resume.pdf").write_bytes(b"home PDF")
    (tmp_path / "asset.png").write_bytes(b"home asset")
    (root / "resume.pdf").write_bytes(b"root PDF")
    (root / "asset.png").write_bytes(b"root asset")
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {"name": "SWE", "root": "cvs/swe", "pdf": "resume.pdf", "assets": ["asset.png"]}
            ],
        },
        tmp_path,
    )

    selection = registry.resolve("SWE")

    assert selection.pdf == (root / "resume.pdf").resolve()
    assert selection.pdf.read_bytes() == b"root PDF"
    assert selection.assets == ((root / "asset.png").resolve(),)


def test_registered_absolute_root_allows_nonportable_parent_components(
    tmp_path: Path,
) -> None:
    root = tmp_path / "CV Library" / "résumé" / ".current"
    root.mkdir(parents=True)
    pdf = root / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {
                    "name": "SWE",
                    "root": str(root),
                    "pdf": "resume.pdf",
                    "assets": [],
                }
            ],
        },
        tmp_path,
    )

    assert registry.resolve("SWE").pdf == pdf.absolute()


def test_registered_file_symlink_is_rejected_without_resolving_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cvs" / "swe"
    root.mkdir(parents=True)
    target = root / "target.pdf"
    target.write_bytes(b"%PDF-1.4 synthetic")
    (root / "resume.pdf").symlink_to(target)
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {
                    "name": "SWE",
                    "root": "cvs/swe",
                    "pdf": "resume.pdf",
                    "assets": [],
                }
            ],
        },
        tmp_path,
    )

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve("SWE")

    assert caught.value.reason_code == "cv_registry_invalid"


def test_registered_nested_directory_symlink_is_rejected_without_traversal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cvs" / "swe"
    root.mkdir(parents=True)
    (root / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic")
    outside = tmp_path / "outside-assets"
    outside.mkdir()
    (outside / "asset.png").write_bytes(b"synthetic")
    (root / "figures").symlink_to(outside, target_is_directory=True)
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {
                    "name": "SWE",
                    "root": "cvs/swe",
                    "pdf": "resume.pdf",
                    "assets": ["figures/asset.png"],
                }
            ],
        },
        tmp_path,
    )

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve("SWE")

    assert caught.value.reason_code == "cv_registry_invalid"


def test_registered_root_ancestor_symlink_is_rejected_without_traversal(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    root = outside / "swe"
    root.mkdir(parents=True)
    (root / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (tmp_path / "cvs").symlink_to(outside, target_is_directory=True)
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {
                    "name": "SWE",
                    "root": "cvs/swe",
                    "pdf": "resume.pdf",
                    "assets": [],
                }
            ],
        },
        tmp_path,
    )

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve("SWE")

    assert caught.value.reason_code == "cv_registry_invalid"


def test_explicit_file_symlink_is_rejected_without_resolving_target(
    registry: CVRegistry, tmp_path: Path
) -> None:
    target = tmp_path / "target.tex"
    target.write_text(
        "\\documentclass{article}\\begin{document}Synthetic\\end{document}"
    )
    declared = tmp_path / "declared.tex"
    declared.symlink_to(target)

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve(str(declared), for_customization=True)

    assert caught.value.reason_code == "cv_path_unavailable"


@pytest.mark.parametrize("filename", ["candidate.TEX", "candidate.PDF"])
def test_explicit_reference_requires_an_exact_supported_extension(
    registry: CVRegistry, tmp_path: Path, filename: str
) -> None:
    candidate = tmp_path / filename
    candidate.write_bytes(
        b"%PDF-1.4 synthetic"
        if candidate.suffix == ".PDF"
        else b"\\documentclass{article}\\begin{document}Synthetic\\end{document}"
    )

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve(str(candidate))

    assert caught.value.reason_code == "cv_type_invalid"


@pytest.mark.parametrize(
    "filename",
    [
        "resume name.tex",
        "resume`curl`.tex",
        "resume$(curl).tex",
        "resume;curl.tex",
        "resume'quote.tex",
        'resume"quote.tex',
        "resume*.tex",
        ".resume.tex",
        "résumé.tex",
    ],
)
def test_registered_reference_requires_portable_components(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "cvs" / "swe"
    root.mkdir(parents=True)
    (root / filename).write_text(
        "\\documentclass{article}\\begin{document}Synthetic\\end{document}"
    )
    registry = CVRegistry(
        {
            "default_cv": "SWE",
            "cvs": [
                {
                    "name": "SWE",
                    "root": "cvs/swe",
                    "tex": filename,
                    "assets": [],
                }
            ],
        },
        tmp_path,
    )

    with pytest.raises(CVSelectionError) as caught:
        registry.resolve("SWE", for_customization=True)

    assert caught.value.reason_code == "cv_registry_invalid"


def test_registered_dotted_name_resolves_before_explicit_path_interpretation(tmp_path: Path) -> None:
    root = tmp_path / "cvs" / "swe-v2"
    root.mkdir(parents=True)
    (root / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic")
    registry = CVRegistry(
        {
            "default_cv": "SWE.v2",
            "cvs": [{"name": "SWE.v2", "root": "cvs/swe-v2", "pdf": "resume.pdf", "assets": []}],
        },
        tmp_path,
    )

    assert registry.resolve("swe.V2").name == "SWE.v2"


@pytest.mark.parametrize("reference", ["missing.pdf", "unknown.docx"])
def test_invalid_explicit_cv_references_are_safe(registry: CVRegistry, reference: str) -> None:
    with pytest.raises(CVSelectionError) as error:
        registry.resolve(reference)

    assert str(reference) not in str(error.value)
