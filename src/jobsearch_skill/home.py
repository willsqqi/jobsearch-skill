from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path

from jobsearch_skill.errors import ConfigurationError


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
