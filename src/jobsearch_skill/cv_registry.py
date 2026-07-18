"""Conservative resolution of registry-declared and explicit CV files."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from jobsearch_skill.cv_models import CVSelection
from jobsearch_skill.cv_security import (
    is_safe_ref,
    safe_read_relative,
)
from jobsearch_skill.errors import CVBuildError, CVSelectionError


_FILE_LOADING_COMMAND = re.compile(
    r"\\(?:addbibresource|attachfile|bibliography|include|includeanimation|includegraphics|"
    r"includepdf|input|inputminted|loadglsentries|lstinputlisting|subfile|subimport|"
    r"verbatiminput|import)(?![A-Za-z@])",
    re.IGNORECASE,
)
_USEPACKAGE_COMMAND = re.compile(
    r"\\usepackage(?![A-Za-z@])(?:\s*\[[^\]]*\])?\s*\{(?P<packages>[^}]*)\}",
    re.IGNORECASE,
)


class CVRegistry:
    """Resolve only CV files deliberately declared in validated preferences."""

    def __init__(self, preferences: Mapping[str, object], home: Path) -> None:
        self._preferences = preferences
        self._home = home.expanduser().resolve()

    def list(self) -> tuple[CVSelection, ...]:
        return tuple(self._registered_selection(entry) for entry in self._entries())

    def resolve(
        self, reference: str | None, *, for_customization: bool = False
    ) -> CVSelection:
        if reference is None:
            default = self._preferences.get("default_cv")
            if not isinstance(default, str):
                raise self._error("cv_registry_invalid")
            return self._registered_by_name(default, for_customization=for_customization)
        if not isinstance(reference, str) or not reference.strip():
            raise self._error("cv_reference_invalid")
        entries = self._entries()
        matches = [
            entry for entry in entries if entry["name"].casefold() == reference.casefold()
        ]
        if matches:
            return self._registered_by_name(reference, for_customization=for_customization)
        if self._is_explicit_path(reference):
            return self._explicit_selection(reference, for_customization=for_customization)
        raise self._error("cv_name_missing")

    @staticmethod
    def _error(reason_code: str, message: str | None = None) -> CVSelectionError:
        messages = {
            "cv_assets_unregistered": (
                "cv_assets_unregistered: customization requires registry-declared assets"
            ),
            "cv_customization_requires_tex": (
                "cv_customization_requires_tex: customization requires a LaTeX source"
            ),
        }
        return CVSelectionError(
            message
            or messages.get(
                reason_code, "cv_selection_invalid: CV selection is unavailable"
            ),
            reason_code=reason_code,
        )

    @staticmethod
    def _is_explicit_path(reference: str) -> bool:
        candidate = Path(reference).expanduser()
        return (
            candidate.is_absolute()
            or "/" in reference
            or "\\" in reference
            or candidate.suffix != ""
        )

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

    def _registered_by_name(
        self, reference: str, *, for_customization: bool
    ) -> CVSelection:
        matches = [
            entry
            for entry in self._entries()
            if entry["name"].casefold() == reference.casefold()
        ]
        if len(matches) != 1:
            raise self._error(
                "cv_name_missing" if not matches else "cv_name_ambiguous"
            )
        selection = self._registered_selection(matches[0])
        if for_customization and selection.tex is None:
            raise self._error("cv_customization_requires_tex")
        return selection

    def _registry_root(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise self._error("cv_registry_invalid")
        raw = Path(value).expanduser()
        if raw.is_absolute():
            candidate = Path(os.path.abspath(raw))
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise self._error("cv_registry_invalid")
        else:
            if not is_safe_ref(value):
                raise self._error("cv_registry_invalid")
            candidate = self._home / raw
            self._validate_declared_directory(self._home, raw)
        if not raw.is_absolute() and not candidate.is_relative_to(self._home):
            raise self._error("cv_registry_invalid")
        return candidate

    @staticmethod
    def _validate_declared_directory(root: Path, relative: Path) -> None:
        root_descriptor: int | None = None
        opened_directories: list[int] = []
        try:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
                raise OSError
            root_descriptor = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            directory_descriptor = root_descriptor
            for component in relative.parts:
                directory_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
                opened_directories.append(directory_descriptor)
            if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
                raise OSError
        except OSError as error:
            raise CVRegistry._error("cv_registry_invalid") from error
        finally:
            for descriptor in reversed(opened_directories):
                os.close(descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def _declared_file(self, root: Path, value: object, *, suffix: str) -> Path:
        if not isinstance(value, str) or not value:
            raise self._error("cv_registry_invalid")
        raw = Path(value)
        if raw.is_absolute() or not is_safe_ref(value) or raw.suffix != suffix:
            raise self._error("cv_registry_invalid")
        path = root / raw
        try:
            safe_read_relative(root, raw)
        except CVBuildError as error:
            raise self._error("cv_registry_invalid") from error
        return path

    def _registered_selection(self, entry: Mapping[str, object]) -> CVSelection:
        root = self._registry_root(entry.get("root"))
        try:
            root_info = root.stat(follow_symlinks=False)
        except OSError as error:
            raise self._error("cv_registry_invalid") from error
        if not stat.S_ISDIR(root_info.st_mode):
            raise self._error("cv_registry_invalid")
        tex = (
            self._declared_file(root, entry["tex"], suffix=".tex")
            if "tex" in entry
            else None
        )
        pdf = (
            self._declared_file(root, entry["pdf"], suffix=".pdf")
            if "pdf" in entry
            else None
        )
        assets_value = entry.get("assets")
        if not isinstance(assets_value, Sequence) or isinstance(
            assets_value, (str, bytes)
        ):
            raise self._error("cv_registry_invalid")
        assets = tuple(
            self._declared_file(root, asset, suffix=".png")
            for asset in assets_value
        )
        if len({asset.relative_to(root).as_posix().casefold() for asset in assets}) != len(
            assets
        ):
            raise self._error("cv_registry_invalid")
        name = entry.get("name")
        if not isinstance(name, str):
            raise self._error("cv_registry_invalid")
        return CVSelection(name=name, root=root, pdf=pdf, tex=tex, assets=assets)

    def _explicit_selection(
        self, reference: str, *, for_customization: bool
    ) -> CVSelection:
        path = Path(os.path.abspath(Path(reference).expanduser()))
        if not is_safe_ref(path.name):
            raise self._error("cv_path_unavailable")
        self._readable_file(path, registered=False)
        suffix = path.suffix
        if suffix == ".pdf":
            if for_customization:
                raise self._error("cv_customization_requires_tex")
            return CVSelection(
                name=path.stem, root=path.parent, pdf=path, tex=None, assets=()
            )
        if suffix == ".tex":
            if for_customization and self._has_external_dependencies(path):
                raise self._error("cv_assets_unregistered")
            return CVSelection(
                name=path.stem, root=path.parent, pdf=None, tex=path, assets=()
            )
        raise self._error("cv_type_invalid")

    def _readable_file(self, path: Path, *, registered: bool) -> None:
        self._read_file_bytes(path, registered=registered)

    @staticmethod
    def _read_file_bytes(path: Path, *, registered: bool) -> bytes:
        descriptor: int | None = None
        try:
            if not hasattr(os, "O_NOFOLLOW"):
                raise OSError
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise OSError
            descriptor = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise OSError
            data = b"".join(chunks)
            if len(data) != opened.st_size:
                raise OSError
            return data
        except OSError as error:
            reason = "cv_registry_invalid" if registered else "cv_path_unavailable"
            raise CVRegistry._error(reason) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _has_external_dependencies(path: Path) -> bool:
        try:
            text = CVRegistry._read_file_bytes(
                path, registered=False
            ).decode("utf-8")
        except (CVSelectionError, UnicodeError) as error:
            raise CVRegistry._error("cv_path_unavailable") from error
        uncommented = CVRegistry._strip_tex_comments(text)
        if _FILE_LOADING_COMMAND.search(uncommented) is not None:
            return True
        for match in _USEPACKAGE_COMMAND.finditer(uncommented):
            packages = (package.strip() for package in match.group("packages").split(","))
            if any(
                package
                and (
                    package.startswith(".")
                    or "/" in package
                    or "\\" in package
                    or package.casefold().endswith(".sty")
                )
                for package in packages
            ):
                return True
        return False

    @staticmethod
    def _strip_tex_comments(text: str) -> str:
        lines: list[str] = []
        for line in text.splitlines():
            for index, character in enumerate(line):
                if character != "%":
                    continue
                preceding_backslashes = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == "\\":
                    preceding_backslashes += 1
                    cursor -= 1
                if preceding_backslashes % 2 == 0:
                    line = line[:index]
                    break
            lines.append(line)
        return "\n".join(lines)
