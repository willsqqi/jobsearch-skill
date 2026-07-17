"""Conservative selection of declared CV sources and PDFs."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jobsearch_skill.errors import CVSelectionError

_EXTERNAL_TEX_COMMAND = re.compile(
    r"\\(?:includegraphics|input|include|usepackage)(?:\[[^\]]*\])?\s*\{[^}]+\}"
)


@dataclass(frozen=True)
class CVSelection:
    name: str
    root: Path
    pdf: Path | None
    tex: Path | None
    assets: tuple[Path, ...]


class CVRegistry:
    """Resolve only CV files deliberately declared in validated preferences."""

    def __init__(self, preferences: Mapping[str, object], home: Path) -> None:
        self._preferences = preferences
        self._home = home.expanduser().resolve()

    def list(self) -> tuple[CVSelection, ...]:
        return tuple(self._registered_selection(entry) for entry in self._entries())

    def resolve(self, reference: str | None, *, for_customization: bool = False) -> CVSelection:
        if reference is None:
            default = self._preferences.get("default_cv")
            if not isinstance(default, str):
                raise self._error("cv_registry_invalid")
            return self._registered_by_name(default, for_customization=for_customization)
        if not isinstance(reference, str) or not reference.strip():
            raise self._error("cv_reference_invalid")
        if self._is_explicit_path(reference):
            return self._explicit_selection(reference, for_customization=for_customization)
        return self._registered_by_name(reference, for_customization=for_customization)

    @staticmethod
    def _error(reason_code: str, message: str | None = None) -> CVSelectionError:
        messages = {
            "cv_assets_unregistered": "cv_assets_unregistered: customization requires registry-declared assets",
            "cv_customization_requires_tex": "cv_customization_requires_tex: customization requires a LaTeX source",
        }
        return CVSelectionError(message or messages.get(reason_code, "cv_selection_invalid: CV selection is unavailable"), reason_code=reason_code)

    @staticmethod
    def _is_explicit_path(reference: str) -> bool:
        candidate = Path(reference).expanduser()
        return candidate.is_absolute() or "/" in reference or "\\" in reference or candidate.suffix != ""

    def _entries(self) -> tuple[Mapping[str, object], ...]:
        entries = self._preferences.get("cvs")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise self._error("cv_registry_invalid")
        result: list[Mapping[str, object]] = []
        names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                raise self._error("cv_registry_invalid")
            key = entry["name"].casefold()
            if not key or key in names:
                raise self._error("cv_name_ambiguous")
            names.add(key)
            result.append(entry)
        return tuple(result)

    def _registered_by_name(self, reference: str, *, for_customization: bool) -> CVSelection:
        matches = [entry for entry in self._entries() if entry["name"].casefold() == reference.casefold()]
        if len(matches) != 1:
            raise self._error("cv_name_missing" if not matches else "cv_name_ambiguous")
        selection = self._registered_selection(matches[0])
        if for_customization and selection.tex is None:
            raise self._error("cv_customization_requires_tex")
        return selection

    def _resolve_under(self, base: Path, relative: object) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise self._error("cv_registry_invalid")
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(self._home):
            raise self._error("cv_registry_invalid")
        return candidate

    def _declared_file(self, root: Path, value: object) -> Path:
        home_candidate = self._resolve_under(self._home, value)
        path = home_candidate if home_candidate.exists() else self._resolve_under(root, value)
        self._readable_file(path, registered=True)
        return path

    def _registered_selection(self, entry: Mapping[str, object]) -> CVSelection:
        root = self._resolve_under(self._home, entry.get("root"))
        if not root.is_dir():
            raise self._error("cv_registry_invalid")
        tex = self._declared_file(root, entry["tex"]) if "tex" in entry else None
        pdf = self._declared_file(root, entry["pdf"]) if "pdf" in entry else None
        assets_value = entry.get("assets")
        if not isinstance(assets_value, Sequence) or isinstance(assets_value, (str, bytes)):
            raise self._error("cv_registry_invalid")
        assets = tuple(self._declared_file(root, asset) for asset in assets_value)
        if len(set(assets)) != len(assets):
            raise self._error("cv_registry_invalid")
        name = entry.get("name")
        if not isinstance(name, str):
            raise self._error("cv_registry_invalid")
        return CVSelection(name=name, root=root, pdf=pdf, tex=tex, assets=assets)

    def _explicit_selection(self, reference: str, *, for_customization: bool) -> CVSelection:
        path = Path(reference).expanduser().resolve()
        self._readable_file(path, registered=False)
        suffix = path.suffix.casefold()
        if suffix == ".pdf":
            if for_customization:
                raise self._error("cv_customization_requires_tex")
            return CVSelection(name=path.stem, root=path.parent, pdf=path, tex=None, assets=())
        if suffix == ".tex":
            if for_customization and self._has_external_dependencies(path):
                raise self._error("cv_assets_unregistered")
            return CVSelection(name=path.stem, root=path.parent, pdf=None, tex=path, assets=())
        raise self._error("cv_type_invalid")

    def _readable_file(self, path: Path, *, registered: bool) -> None:
        try:
            if not path.is_file() or not os.access(path, os.R_OK):
                raise OSError
            with path.open("rb"):
                pass
        except OSError as error:
            raise self._error("cv_registry_invalid" if registered else "cv_path_unavailable") from error

    @staticmethod
    def _has_external_dependencies(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CVRegistry._error("cv_path_unavailable") from error
        return _EXTERNAL_TEX_COMMAND.search(text) is not None
