"""Fail-closed CV input snapshots and restricted LaTeX preflight."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jobsearch_skill.errors import CVBuildError

if TYPE_CHECKING:
    from jobsearch_skill.cv_models import CVSelection


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COMMAND_RE = re.compile(r"\\(?P<name>[A-Za-z@]+|.)")
_DOCUMENT_CLASS_RE = re.compile(
    r"\\documentclass(?:\s*\[(?P<options>[^\]]*)\])?\s*\{(?P<name>[^}]*)\}",
    re.IGNORECASE,
)
_PACKAGE_RE = re.compile(
    r"\\usepackage(?:\s*\[(?P<options>[^\]]*)\])?\s*\{(?P<names>[^}]*)\}",
    re.IGNORECASE,
)
_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{(?P<name>[^}]*)\}")
_GRAPHICS_RE = re.compile(
    r"\\includegraphics\*?(?:\s*\[(?P<options>[^\]]*)\])?\s*\{(?P<name>[^}]*)\}",
    re.IGNORECASE,
)
_SAFE_OPTION_RE = re.compile(r"[A-Za-z0-9 ,.=_:+\-/]*")
_SAFE_COMMANDS = {
    "documentclass",
    "usepackage",
    "pagestyle",
    "begin",
    "end",
    "Large",
    "large",
    "LARGE",
    "huge",
    "Huge",
    "bfseries",
    "itshape",
    "section",
    "subsection",
    "textbf",
    "textit",
    "emph",
    "hfill",
    "quad",
    "qquad",
    "newline",
    "linebreak",
    "pagebreak",
    "noindent",
    "item",
    "includegraphics",
}
_SAFE_CONTROL_SYMBOLS = {"\\", "%", "&", "#", "_", "$", "{", "}", " "}
_SAFE_PACKAGES = {"geometry", "graphicx"}
_SAFE_ENVIRONMENTS = {"document", "center", "itemize", "enumerate"}


@dataclass(frozen=True)
class InputSnapshot:
    path: Path
    relative: Path
    data: bytes
    sha256: str
    kind: str


def security_error(reason_code: str = "cv_source_unsafe") -> CVBuildError:
    messages = {
        "cv_source_unsafe": "cv_source_unsafe: declared CV input is unsafe",
        "cv_source_invalid": "cv_source_invalid: declared CV input is unavailable",
        "cv_asset_outside_root": "cv_asset_outside_root: input is outside the declared CV root",
        "cv_copy_digest": "cv_copy_digest: copied CV input digest does not match",
        "cv_path_unsafe": "cv_path_unsafe: generated CV path is unavailable",
    }
    return CVBuildError(messages[reason_code], reason_code=reason_code)


def is_safe_ref(value: str) -> bool:
    if not value or value in {".", ".."} or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(
            part not in {"", ".", ".."} and not part.startswith("-")
            for part in path.parts
        )
    )


def aggregate_hash(source_hashes: dict[str, str]) -> str:
    serialized = json.dumps(
        source_hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _open_root(root: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise security_error()
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise security_error("cv_source_invalid") from error


def safe_read_relative(root: Path, relative: Path) -> bytes:
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise security_error("cv_source_invalid")
    root_fd = _open_root(root)
    directory_fd = root_fd
    opened_directories: list[int] = []
    file_fd: int | None = None
    try:
        for component in relative.parts[:-1]:
            directory_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            opened_directories.append(directory_fd)
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not regular")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise OSError("changed while reading")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise OSError("short read")
        return data
    except OSError as error:
        raise security_error("cv_source_invalid") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(opened_directories):
            os.close(descriptor)
        os.close(root_fd)


def _strip_tex_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        for index, character in enumerate(line):
            if character != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                line = line[:index]
                break
        lines.append(line)
    return "\n".join(lines)


def _safe_options(value: str | None) -> bool:
    return value is None or _SAFE_OPTION_RE.fullmatch(value) is not None


def validate_tex_bytes(data: bytes, asset_refs: set[str]) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise security_error() from error
    if not text.strip() or "\x00" in text or "^^" in text:
        raise security_error()
    source = _strip_tex_comments(text)
    classes = list(_DOCUMENT_CLASS_RE.finditer(source))
    if (
        len(classes) != 1
        or classes[0].group("name").strip() != "article"
        or not _safe_options(classes[0].group("options"))
    ):
        raise security_error()
    for package in _PACKAGE_RE.finditer(source):
        names = {name.strip() for name in package.group("names").split(",")}
        if not names or not names.issubset(_SAFE_PACKAGES) or not _safe_options(
            package.group("options")
        ):
            raise security_error()
    for environment in _ENVIRONMENT_RE.finditer(source):
        if environment.group("name").strip() not in _SAFE_ENVIRONMENTS:
            raise security_error()
    for graphics in _GRAPHICS_RE.finditer(source):
        name = graphics.group("name").strip()
        candidates = {name, f"{name}.png"}
        if (
            not _safe_options(graphics.group("options"))
            or not is_safe_ref(name)
            or not candidates.intersection(asset_refs)
        ):
            raise security_error()
    for command in _COMMAND_RE.finditer(source):
        name = command.group("name")
        if name[0].isalpha() or name[0] == "@":
            if name not in _SAFE_COMMANDS:
                raise security_error()
        elif name not in _SAFE_CONTROL_SYMBOLS:
            raise security_error()
    return text


def validate_png(data: bytes) -> None:
    if not data.startswith(_PNG_SIGNATURE):
        raise security_error()
    offset = len(_PNG_SIGNATURE)
    saw_header = False
    saw_end = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise security_error()
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise security_error()
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        actual_crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise security_error()
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                raise security_error()
            saw_header = True
        if chunk_type == b"IEND":
            if length != 0 or end != len(data):
                raise security_error()
            saw_end = True
        offset = end
    if not saw_header or not saw_end:
        raise security_error()


def snapshot_declared_inputs(selection: CVSelection) -> tuple[InputSnapshot, ...]:
    root = selection.root
    candidates = tuple(path for path in (selection.tex, selection.pdf, *selection.assets) if path)
    if not candidates:
        raise security_error("cv_source_invalid")
    snapshots: list[InputSnapshot] = []
    seen: set[str] = set()
    asset_refs: set[str] = set()
    for path in candidates:
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            reason = "cv_asset_outside_root" if path in selection.assets else "cv_source_invalid"
            raise security_error(reason) from error
        reference = relative.as_posix()
        if not is_safe_ref(reference) or reference.casefold() in seen:
            raise security_error("cv_source_invalid")
        seen.add(reference.casefold())
        data = safe_read_relative(root, relative)
        if path in selection.assets:
            if path.suffix.casefold() != ".png":
                raise security_error()
            validate_png(data)
            kind = "asset"
            asset_refs.add(reference)
        elif path == selection.tex:
            if path.suffix.casefold() != ".tex":
                raise security_error()
            kind = "tex"
        elif path == selection.pdf:
            if path.suffix.casefold() != ".pdf" or not data.startswith(b"%PDF-"):
                raise security_error()
            kind = "pdf"
        else:
            raise security_error()
        snapshots.append(
            InputSnapshot(
                path=path,
                relative=relative,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
                kind=kind,
            )
        )
    tex = next((snapshot for snapshot in snapshots if snapshot.kind == "tex"), None)
    if tex is not None:
        validate_tex_bytes(tex.data, asset_refs)
    return tuple(snapshots)


def ensure_private_directory(path: Path, boundary: Path) -> None:
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise security_error("cv_path_unsafe") from error
    current = boundary
    try:
        if current.is_symlink() or not current.is_dir():
            raise OSError
        current.chmod(0o700)
        for component in relative.parts:
            current = current / component
            current.mkdir(mode=0o700, exist_ok=True)
            if current.is_symlink() or not current.is_dir():
                raise OSError
            current.chmod(0o700)
    except OSError as error:
        raise security_error("cv_path_unsafe") from error


def copy_bytes_atomic(data: bytes, target: Path) -> str:
    temporary: Path | None = None
    expected = hashlib.sha256(data).hexdigest()
    try:
        descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        target.chmod(0o600)
        actual = hashlib.sha256(safe_read_relative(target.parent, Path(target.name))).hexdigest()
        if actual != expected:
            raise security_error("cv_copy_digest")
        return actual
    except CVBuildError:
        raise
    except OSError as error:
        raise security_error("cv_source_invalid") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def reject_latexmk_rc(source_dir: Path) -> None:
    try:
        for candidate in source_dir.iterdir():
            if candidate.name.casefold() in {".latexmkrc", "latexmkrc"}:
                raise security_error()
    except OSError as error:
        raise security_error("cv_path_unsafe") from error


def _private_text(path: Path, text: str) -> None:
    copy_bytes_atomic(text.encode("utf-8"), path)


def isolated_latex_environment(root: Path) -> dict[str, str]:
    latexmk = shutil.which("latexmk")
    pdflatex = shutil.which("pdflatex")
    perl = shutil.which("perl")
    if not latexmk or not pdflatex or not perl:
        raise CVBuildError(
            "cv_build_tool_missing: required LaTeX tool is unavailable",
            reason_code="cv_build_tool_missing",
        )
    isolation = root / ".latex-isolation"
    ensure_private_directory(isolation, root)
    directories = {
        name: isolation / name
        for name in ("home", "xdg", "texmf-home", "texmf-config", "texmf-var", "cache", "tmp")
    }
    for directory in directories.values():
        ensure_private_directory(directory, root)
    rc_path = isolation / "trusted-latexmkrc"
    _private_text(rc_path, "$auto_rc_use = 0;\n")
    command_directories = list(
        dict.fromkeys(
            [
                str(Path(latexmk).parent),
                str(Path(pdflatex).parent),
                str(Path(perl).parent),
                "/usr/bin",
                "/bin",
            ]
        )
    )
    return {
        "PATH": os.pathsep.join(command_directories),
        "HOME": str(directories["home"]),
        "XDG_CONFIG_HOME": str(directories["xdg"]),
        "TEXMFHOME": str(directories["texmf-home"]),
        "TEXMFCONFIG": str(directories["texmf-config"]),
        "TEXMFVAR": str(directories["texmf-var"]),
        "TEXMFCACHE": str(directories["cache"]),
        "TMPDIR": str(directories["tmp"]),
        "LATEXMKRCSYS": str(rc_path),
        "LATEXMKRC": str(rc_path),
        "shell_escape": "0",
        "shell_escape_commands": "",
        "openin_any": "p",
        "openout_any": "p",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
