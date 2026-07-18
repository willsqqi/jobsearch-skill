from __future__ import annotations

import os
import re
import stat
import subprocess
import tomllib
import csv
import json
import tempfile
from collections.abc import Mapping
from importlib import resources
from io import BytesIO
from pathlib import Path

import yaml
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jobsearch_skill.cv_security import (
    copy_bytes_atomic,
    isolated_latex_environment,
    safe_read_relative,
    validate_tex_bytes,
)
from jobsearch_skill.errors import (
    CVBuildError,
    ConfigurationError,
    SchemaValidationError,
    StorageValidationError,
)
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


APPLICATION_FIELDNAMES = (
    "schema_version",
    "application_id",
    "company",
    "role",
    "location",
    "url",
    "job_fingerprint",
    "cv_name",
    "cv_path",
    "analysis_ref",
    "artifact_ref",
    "applied_at",
    "status",
    "workday_id",
    "updated_at",
)

_PROFILE_CATEGORIES = (
    "identity",
    "contact",
    "address",
    "work_authorization",
    "compensation",
    "demographics",
    "disability",
    "veteran",
    "legal_attestations",
    "links",
)

_READINESS_PATHS = (
    ("identity", "first_name"),
    ("identity", "last_name"),
    ("contact", "email"),
    ("contact", "phone"),
    ("address", "line1"),
    ("address", "city"),
    ("address", "region"),
    ("address", "postal_code"),
    ("address", "country"),
    ("work_authorization",),
    ("compensation", "target"),
    ("compensation", "currency"),
    ("compensation", "period"),
    ("demographics", "gender"),
    ("demographics", "race_ethnicity"),
    ("disability", "status"),
    ("veteran", "status"),
    ("legal_attestations", "accurate_information"),
    ("legal_attestations", "background_check_consent"),
    ("legal_attestations", "non_compete_restriction"),
)

_SYNTHETIC_MARKER = ".synthetic-bootstrap.json"
_SYNTHETIC_MARKER_TEXT = json.dumps(
    {"schema_version": 1, "kind": "jobsearch-synthetic-home"},
    sort_keys=True,
) + "\n"


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    xdg_root = environment.get("XDG_CONFIG_HOME", "").strip()
    if xdg_root:
        return Path(xdg_root).expanduser() / "jobsearch-skill" / "config.toml"
    return Path.home() / ".config" / "jobsearch-skill" / "config.toml"


def _unsafe_directory(path: Path) -> bool:
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    return not path.is_dir() or metadata.st_uid != os.getuid() or bool(mode & 0o022)


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path.parent
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def validate_private_home(home: Path, repo_root: Path) -> Path:
    """Resolve and validate a private home without creating or changing it."""

    if home.exists() and home.is_symlink():
        raise ConfigurationError(
            "home_symlink: private home must not be a symbolic link",
            reason_code="home_symlink",
        )
    resolved_home = home.expanduser().resolve()
    resolved_repo = repo_root.expanduser().resolve()
    if resolved_home == resolved_repo or resolved_home.is_relative_to(resolved_repo):
        raise ConfigurationError(
            "home_inside_repo: private home must be outside the public repository",
            reason_code="home_inside_repo",
        )
    if resolved_home.exists():
        if not resolved_home.is_dir():
            raise ConfigurationError(
                "home_type: private home must be a directory",
                reason_code="home_type",
            )
        metadata = resolved_home.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ConfigurationError(
                "home_mode: an existing private home must be owned by the user with mode 0700",
                reason_code="home_mode",
            )
    parent = _nearest_existing_parent(resolved_home)
    if _unsafe_directory(parent):
        raise ConfigurationError(
            "unsafe_parent: private home requires a user-owned parent without group/world writes",
            reason_code="unsafe_parent",
        )
    return resolved_home


def _read_pointer(config_path: Path) -> Path:
    if config_path.is_symlink():
        raise ConfigurationError(
            "pointer_symlink: private_home_pointer must not be a symbolic link",
            reason_code="pointer_symlink",
        )
    metadata = config_path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ConfigurationError(
            "pointer_type: private_home_pointer must be a user-owned regular file",
            reason_code="pointer_type",
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigurationError(
            "pointer_mode: private_home_pointer must have mode 0600",
            reason_code="pointer_mode",
        )
    parent = _nearest_existing_parent(config_path.resolve())
    if _unsafe_directory(parent):
        raise ConfigurationError(
            "pointer_parent: private_home_pointer has unsafe parent permissions",
            reason_code="pointer_parent",
        )
    try:
        with config_path.open("rb") as stream:
            pointer = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            "private_home_pointer: pointer file is malformed",
            reason_code="private_home_pointer",
        ) from error
    private_home = pointer.get("private_home")
    if not isinstance(private_home, str) or not private_home.strip():
        raise ConfigurationError(
            "private_home_pointer: private_home must be a nonempty path string",
            reason_code="private_home_pointer",
        )
    pointer_path = Path(private_home).expanduser()
    if not pointer_path.is_absolute():
        raise ConfigurationError(
            "private_home_pointer: private_home must be an absolute path",
            reason_code="private_home_pointer",
        )
    return pointer_path


def resolve_private_home(
    explicit: Path | None,
    repo_root: Path,
    config_path: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit, environment, then local pointer private-home configuration."""

    environment = os.environ if environ is None else environ
    if explicit is not None:
        candidate = explicit
    elif environment.get("JOBSEARCH_HOME", "").strip():
        candidate = Path(environment["JOBSEARCH_HOME"])
    elif config_path.exists() or config_path.is_symlink():
        candidate = _read_pointer(config_path)
    else:
        raise ConfigurationError(
            "private_home_missing: run `jobsearch configure PRIVATE_HOME` before Browser work",
            reason_code="private_home_missing",
        )
    return validate_private_home(candidate, repo_root)


def _make_private_directories(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    if not candidate.is_dir():
        raise ConfigurationError(
            "home_parent: private home parent must be a directory",
            reason_code="home_parent",
        )
    try:
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
    except OSError as error:
        raise ConfigurationError(
            "home_create: unable to create private home",
            reason_code="home_create",
        ) from error


def configure_private_home(private_home: Path, config_path: Path, repo_root: Path) -> Path:
    """Validate a private home and atomically configure its local pointer."""

    resolved_home = validate_private_home(private_home, repo_root)
    pointer = Path(os.path.abspath(config_path.expanduser()))
    if pointer.is_symlink():
        raise ConfigurationError(
            "pointer_symlink: private_home_pointer must not be a symbolic link",
            reason_code="pointer_symlink",
        )
    config_directory = pointer.parent
    if not config_directory.exists():
        if not config_directory.parent.is_dir():
            raise ConfigurationError(
                "config_parent: the config parent directory is unavailable",
                reason_code="config_parent",
            )
        try:
            config_directory.mkdir(mode=0o700)
            config_directory.chmod(0o700)
        except OSError as error:
            raise ConfigurationError(
                "config_create: unable to create app config directory",
                reason_code="config_create",
            ) from error
    elif not config_directory.is_dir() or _unsafe_directory(config_directory):
        raise ConfigurationError(
            "config_parent: app config directory is unsafe",
            reason_code="config_parent",
        )

    text = f"private_home = {json.dumps(str(resolved_home))}\n"

    def validate_pointer(candidate: str) -> None:
        value = tomllib.loads(candidate)
        if value != {"private_home": str(resolved_home)}:
            raise ValueError("pointer contract mismatch")

    SafeStore(SchemaRegistry(), config_directory).write_text(pointer, text, validate_pointer)
    return resolved_home


def _profile_template() -> dict[str, object]:
    return {"schema_version": 1, **{category: {} for category in _PROFILE_CATEGORIES}}


def _preferences_template() -> dict[str, object]:
    return {
        "schema_version": 1,
        "default_cv": "unconfigured",
        "cvs": [
            {
                "name": "unconfigured",
                "root": "UNCONFIGURED",
                "pdf": "UNCONFIGURED.pdf",
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


def bootstrap_private_home(home: Path, registry: SchemaRegistry) -> list[Path]:
    """Create private, schema-valid bootstrap data without overwriting user files."""

    resolved_home = home.expanduser().resolve()
    _make_private_directories(resolved_home)
    metadata = resolved_home.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ConfigurationError(
            "home_mode: private home must be owned by the user with mode 0700",
            reason_code="home_mode",
        )
    directories = [resolved_home / name for name in ("generated", "runs", "backups", "logs")]
    for directory in directories:
        _make_private_directories(directory)
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise ConfigurationError(
                "home_mode: private directory must have mode 0700",
                reason_code="home_mode",
            )

    store = SafeStore(registry, resolved_home / "backups")
    documents: tuple[tuple[str, dict[str, object], str], ...] = (
        ("profile.yaml", _profile_template(), "profile.v1"),
        ("preferences.yaml", _preferences_template(), "preferences.v1"),
        ("questions.yaml", {"schema_version": 1, "questions": []}, "questions.v1"),
    )
    for filename, value, contract in documents:
        path = resolved_home / filename
        if path.exists():
            store.ensure_private_file(path)
            store.read_yaml(path, contract)
        else:
            store.write_yaml(path, value, contract)
    applications = resolved_home / "applications.csv"
    if not applications.exists():
        store.rewrite_csv(
            applications,
            APPLICATION_FIELDNAMES,
            [],
            "application-record.v1",
            1,
        )
    else:
        store.ensure_private_file(applications)
        _validate_applications(applications, registry)
    return [
        *(resolved_home / filename for filename, _, _ in documents),
        applications,
        *directories,
    ]


def _synthetic_resources() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, str],
]:
    root = resources.files("jobsearch_skill.data.synthetic")
    documents: list[dict[str, object]] = []
    try:
        for filename in ("profile.yaml", "preferences.yaml", "questions.yaml"):
            value = yaml.safe_load(root.joinpath(filename).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("synthetic document is not an object")
            documents.append(value)
        tex_sources = {
            candidate.name: candidate.read_text(encoding="utf-8")
            for candidate in root.iterdir()
            if candidate.name.endswith(".tex")
        }
        if not tex_sources:
            raise ValueError("synthetic CV sources are unavailable")
    except (FileNotFoundError, ModuleNotFoundError, TypeError, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise ConfigurationError(
            "synthetic_resources: packaged synthetic resources are unavailable",
            reason_code="synthetic_resources",
        ) from error
    return documents[0], documents[1], documents[2], tex_sources


def _synthetic_pdf_anchor(tex: str) -> str:
    matches = re.findall(
        r"^% jobsearch-pdf-anchor: ([A-Za-z0-9][A-Za-z0-9 -]{0,127})$",
        tex,
        flags=re.MULTILINE,
    )
    if len(matches) != 1 or matches[0] not in tex:
        raise ValueError("synthetic PDF anchor is unavailable")
    return matches[0]


def _verify_synthetic_pdf(pdf: bytes, expected_anchor: str) -> None:
    try:
        reader = PdfReader(BytesIO(pdf))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (EOFError, IndexError, KeyError, OSError, PyPdfError, TypeError, ValueError) as error:
        raise ValueError("synthetic PDF is invalid") from error
    if (
        reader.is_encrypted
        or len(reader.pages) < 1
        or "Avery Example" not in text
        or expected_anchor not in text
    ):
        raise ValueError("synthetic PDF verification failed")


def _exact_text(expected: str):
    def validate(value: str) -> None:
        if value != expected:
            raise ValueError("text mismatch")

    return validate


def _compile_synthetic_cv(tex: str) -> bytes:
    """Compile and verify the packaged public CV in an isolated temporary directory."""

    try:
        validate_tex_bytes(tex.encode("utf-8"), set())
        expected_anchor = _synthetic_pdf_anchor(tex)
        with tempfile.TemporaryDirectory(prefix="jobsearch-synthetic-") as name:
            root = Path(name)
            root.chmod(0o700)
            source = root / "resume.tex"
            copy_bytes_atomic(tex.encode("utf-8"), source)
            completed = subprocess.run(
                ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", source.name],
                cwd=root,
                env=isolated_latex_environment(root),
                shell=False,
                check=False,
                capture_output=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise OSError("synthetic CV compiler failed")
            pdf = safe_read_relative(root, Path("resume.pdf"))
            _verify_synthetic_pdf(pdf, expected_anchor)
            return pdf
    except (
        CVBuildError,
        EOFError,
        IndexError,
        KeyError,
        OSError,
        PyPdfError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as error:
        raise ConfigurationError(
            "synthetic_cv_build: packaged synthetic CV could not be verified",
            reason_code="synthetic_cv_build",
        ) from error


def bootstrap_synthetic_home(home: Path, registry: SchemaRegistry) -> list[Path]:
    """Create an idempotent evaluation home from packaged public-only resources."""

    resolved_home = home.expanduser().resolve()
    marker = resolved_home / _SYNTHETIC_MARKER
    profile_path = resolved_home / "profile.yaml"
    profile, preferences, questions, tex_sources = _synthetic_resources()
    expected_documents = (
        (profile_path, profile, "profile.v1"),
        (resolved_home / "preferences.yaml", preferences, "preferences.v1"),
        (resolved_home / "questions.yaml", questions, "questions.v1"),
    )

    if profile_path.exists() and not marker.exists():
        raise ConfigurationError(
            "synthetic_bootstrap_conflict: refusing to replace a non-synthetic profile",
            reason_code="synthetic_bootstrap_conflict",
        )

    if marker.exists():
        store = SafeStore(registry, resolved_home / "backups")
        store.ensure_private_file(marker)
        try:
            marker_text = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(
                "synthetic_bootstrap_conflict: synthetic marker is unavailable",
                reason_code="synthetic_bootstrap_conflict",
            ) from error
        if marker_text != _SYNTHETIC_MARKER_TEXT:
            raise ConfigurationError(
                "synthetic_bootstrap_conflict: synthetic marker is invalid",
                reason_code="synthetic_bootstrap_conflict",
            )
        for path, expected, contract in expected_documents:
            if store.read_yaml(path, contract) != expected:
                raise ConfigurationError(
                    "synthetic_bootstrap_conflict: synthetic document was changed",
                    reason_code="synthetic_bootstrap_conflict",
                )
        cv_root = resolved_home / "synthetic-cv"
        try:
            for filename, tex in tex_sources.items():
                persisted_pdf = safe_read_relative(
                    cv_root, Path(filename).with_suffix(".pdf")
                )
                if (
                    safe_read_relative(cv_root, Path(filename)) != tex.encode("utf-8")
                    or not persisted_pdf
                ):
                    raise OSError("synthetic CV changed")
                _verify_synthetic_pdf(persisted_pdf, _synthetic_pdf_anchor(tex))
        except (CVBuildError, OSError, ValueError) as error:
            raise ConfigurationError(
                "synthetic_bootstrap_conflict: synthetic CV was changed",
                reason_code="synthetic_bootstrap_conflict",
            ) from error
        cv_paths = [
            cv_root / filename
            for source in tex_sources
            for filename in (source, Path(source).with_suffix(".pdf").name)
        ]
        return [marker, *(path for path, _, _ in expected_documents), *cv_paths]

    pdfs = {
        Path(filename).with_suffix(".pdf").name: _compile_synthetic_cv(tex)
        for filename, tex in tex_sources.items()
    }
    created = bootstrap_private_home(resolved_home, registry)
    store = SafeStore(registry, resolved_home / "backups")
    for path, value, contract in expected_documents:
        store.write_yaml(path, value, contract)
    cv_root = resolved_home / "synthetic-cv"
    _make_private_directories(cv_root)
    for filename, tex in tex_sources.items():
        store.write_text(cv_root / filename, tex, _exact_text(tex))
    for filename, pdf in pdfs.items():
        copy_bytes_atomic(pdf, cv_root / filename)
    store.write_text(
        marker,
        _SYNTHETIC_MARKER_TEXT,
        _exact_text(_SYNTHETIC_MARKER_TEXT),
    )
    cv_paths = [
        cv_root / filename
        for source in tex_sources
        for filename in (source, Path(source).with_suffix(".pdf").name)
    ]
    return [*created, marker, *cv_paths]


def _validate_applications(path: Path, registry: SchemaRegistry) -> None:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            first_line = stream.readline().rstrip("\r\n")
            reader = csv.DictReader(stream)
            if (
                first_line != "# schema_version=1"
                or tuple(reader.fieldnames or ()) != APPLICATION_FIELDNAMES
            ):
                raise StorageValidationError(
                    "document_header: applications CSV has an invalid versioned header",
                    reason_code="document_header",
                )
            for row in reader:
                try:
                    candidate = dict(row)
                    if candidate.get("schema_version") == "1":
                        candidate["schema_version"] = 1
                    registry.validate("application-record.v1", candidate)
                except SchemaValidationError as error:
                    raise StorageValidationError(
                        "storage_validation: applications row violates its contract",
                        reason_code="storage_validation",
                        field_path=error.field_path,
                    ) from error
    except (OSError, UnicodeError, csv.Error) as error:
        raise StorageValidationError(
            "storage_read: unable to read applications CSV",
            reason_code="storage_read",
        ) from error


def _missing_profile_paths(profile: Mapping[str, object]) -> list[str]:
    missing: list[str] = []
    for parts in _READINESS_PATHS:
        value: object = profile
        found = True
        for part in parts:
            if not isinstance(value, Mapping) or part not in value:
                found = False
                break
            value = value[part]
        if not found or value in (None, "", [], {}):
            missing.append("$.profile." + ".".join(parts))
    return missing


def validate_private_documents(
    home: Path, registry: SchemaRegistry, *, ready: bool = False
) -> list[str]:
    """Validate bootstrapped documents and return only missing readiness paths."""

    store = SafeStore(registry, home / "backups")
    profile = store.read_yaml(home / "profile.yaml", "profile.v1")
    preferences = store.read_yaml(home / "preferences.yaml", "preferences.v1")
    store.read_yaml(home / "questions.yaml", "questions.v1")
    _validate_applications(home / "applications.csv", registry)
    if not ready:
        return []
    missing = _missing_profile_paths(profile)
    if preferences.get("default_cv") == "unconfigured":
        missing.append("$.preferences.default_cv")
    if preferences.get("cvs") == _preferences_template()["cvs"]:
        missing.append("$.preferences.cvs")
    return missing
