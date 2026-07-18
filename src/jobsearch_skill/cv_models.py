"""Public CV data models shared by the focused service modules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CVSelection:
    name: str
    root: Path
    pdf: Path | None
    tex: Path | None
    assets: tuple[Path, ...]


@dataclass(frozen=True)
class PreparedCV:
    run_id: str
    selection: CVSelection
    root: Path
    source_dir: Path
    tex: Path | None
    pdf: Path | None
    manifest_path: Path
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class CVEvidence:
    run_id: str
    cv_name: str
    source_hash: str
    source_hashes: Mapping[str, str]
    extracted_text: str
    path: Path
    reference: str


@dataclass(frozen=True)
class CVBuildResult:
    run_id: str
    pdf: Path
    verified: bool
    page_count: int
    extracted_text: str
    manifest_path: Path
    pdf_reference: str
