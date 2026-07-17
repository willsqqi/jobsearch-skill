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
