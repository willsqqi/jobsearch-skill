from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from jobsearch_skill.errors import ConfigurationError
from jobsearch_skill.home import (
    bootstrap_private_home,
    configure_private_home,
    default_config_path,
    resolve_private_home,
    validate_private_documents,
    validate_private_home,
)
from jobsearch_skill.schema import SchemaRegistry
from jobsearch_skill.storage import SafeStore


def _write_pointer(path: Path, home: Path, *, mode: int = 0o600) -> None:
    path.write_text(f'private_home = "{home}"\n')
    path.chmod(mode)


def test_home_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "jobsearch-skill"
    repo.mkdir()
    explicit = tmp_path / "explicit" / ".jobsearch"
    env_home = tmp_path / "env" / ".jobsearch"
    pointer_home = tmp_path / "pointer" / ".jobsearch"
    pointer = tmp_path / "config.toml"
    _write_pointer(pointer, pointer_home)
    monkeypatch.setenv("JOBSEARCH_HOME", str(env_home))
    assert resolve_private_home(explicit, repo, pointer) == explicit.resolve()
    assert resolve_private_home(None, repo, pointer) == env_home.resolve()
    monkeypatch.delenv("JOBSEARCH_HOME")
    assert resolve_private_home(None, repo, pointer) == pointer_home.resolve()


def test_home_inside_repo_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "jobsearch-skill"
    repo.mkdir()
    with pytest.raises(ConfigurationError, match="outside the public repository"):
        validate_private_home(repo / ".jobsearch", repo)


def test_default_pointer_honors_nonempty_xdg_then_uses_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_path() == tmp_path / "xdg" / "jobsearch-skill" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert default_config_path().parts[-3:] == (".config", "jobsearch-skill", "config.toml")


def test_default_pointer_accepts_injected_environment(tmp_path: Path) -> None:
    assert default_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "jobsearch-skill" / "config.toml"
    )


def test_missing_configuration_is_safe_and_has_exit_code_three(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ConfigurationError) as error:
        resolve_private_home(None, repo, tmp_path / "missing.toml", environ={})
    assert error.value.exit_code == 3
    assert error.value.reason_code == "private_home_missing"
    assert "jobsearch configure" in str(error.value)


@pytest.mark.parametrize("contents", ["private_home = [", "other = 'value'", "private_home = ''"])
def test_malformed_pointer_is_rejected(tmp_path: Path, contents: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pointer = tmp_path / "config.toml"
    pointer.write_text(contents)
    pointer.chmod(0o600)
    with pytest.raises(ConfigurationError, match="private_home_pointer") as error:
        resolve_private_home(None, repo, pointer, environ={})
    assert error.value.exit_code == 3


def test_insecure_pointer_mode_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pointer = tmp_path / "config.toml"
    _write_pointer(pointer, tmp_path / "private", mode=0o644)
    with pytest.raises(ConfigurationError, match="pointer_mode"):
        resolve_private_home(None, repo, pointer, environ={})


def test_existing_private_home_must_be_private_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "private"
    home.mkdir(mode=0o755)
    with pytest.raises(ConfigurationError, match="home_mode"):
        validate_private_home(home, repo)
    home.chmod(0o700)
    assert validate_private_home(home, repo) == home.resolve()


def test_unsafe_nearest_existing_parent_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    with pytest.raises(ConfigurationError, match="unsafe_parent"):
        validate_private_home(unsafe / "nested" / ".jobsearch", repo)


def test_resolution_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "not-created" / ".jobsearch"
    assert validate_private_home(home, repo) == home.resolve()
    assert not home.exists()


def test_pointer_symlink_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "target.toml"
    _write_pointer(target, tmp_path / "private")
    pointer = tmp_path / "config.toml"
    pointer.symlink_to(target)
    with pytest.raises(ConfigurationError, match="pointer_symlink"):
        resolve_private_home(None, repo, pointer, environ={})


def test_configure_and_bootstrap_use_private_atomic_files(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    private_home = tmp_path / "cv-root" / ".jobsearch"
    config_parent = tmp_path / "config"
    config_parent.mkdir(mode=0o755)
    pointer = config_parent / "jobsearch.toml"

    configured = configure_private_home(private_home, pointer, repo_root)
    created = bootstrap_private_home(private_home, SchemaRegistry())

    assert configured == private_home.resolve()
    assert {path.name for path in created} >= {
        "profile.yaml",
        "preferences.yaml",
        "questions.yaml",
        "applications.csv",
        "generated",
        "runs",
        "backups",
        "logs",
    }
    assert tomllib.loads(pointer.read_text(encoding="utf-8")) == {
        "private_home": str(private_home.resolve())
    }
    assert stat.S_IMODE(config_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(pointer.stat().st_mode) == 0o600
    assert stat.S_IMODE(private_home.stat().st_mode) == 0o700
    for directory in ("generated", "runs", "backups", "logs"):
        assert stat.S_IMODE((private_home / directory).stat().st_mode) == 0o700
    for filename in ("profile.yaml", "preferences.yaml", "questions.yaml", "applications.csv"):
        assert stat.S_IMODE((private_home / filename).stat().st_mode) == 0o600


def test_bootstrap_is_idempotent_and_never_overwrites_existing_profile(tmp_path: Path) -> None:
    home = tmp_path / ".jobsearch"
    registry = SchemaRegistry()
    bootstrap_private_home(home, registry)
    profile_path = home / "profile.yaml"
    profile = SafeStore(registry, home / "backups").read_yaml(profile_path, "profile.v1")
    profile["identity"] = {"full_name": "PRIVATE_PROFILE_CANARY"}
    SafeStore(registry, home / "backups").write_yaml(profile_path, profile, "profile.v1")

    bootstrap_private_home(home, registry)

    assert SafeStore(registry, home / "backups").read_yaml(
        profile_path, "profile.v1"
    )["identity"] == {"full_name": "PRIVATE_PROFILE_CANARY"}


def test_bootstrap_seeds_valid_empty_structures_and_exact_csv_header(tmp_path: Path) -> None:
    home = tmp_path / ".jobsearch"
    registry = SchemaRegistry()
    bootstrap_private_home(home, registry)
    store = SafeStore(registry, home / "backups")

    store.read_yaml(home / "profile.yaml", "profile.v1")
    store.read_yaml(home / "preferences.yaml", "preferences.v1")
    store.read_yaml(home / "questions.yaml", "questions.v1")
    lines = (home / "applications.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# schema_version=1"
    assert lines[1].split(",") == [
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
    ]
    public_text = "\n".join(path.read_text(encoding="utf-8") for path in home.glob("*.yaml"))
    assert "Avery Example" not in public_text
    assert "Synthetic Systems" not in public_text


def test_ready_validation_returns_only_missing_field_paths(tmp_path: Path) -> None:
    home = tmp_path / ".jobsearch"
    registry = SchemaRegistry()
    bootstrap_private_home(home, registry)
    profile_path = home / "profile.yaml"
    profile = SafeStore(registry, home / "backups").read_yaml(profile_path, "profile.v1")
    profile["identity"] = {"full_name": "PRIVATE_READY_CANARY"}
    SafeStore(registry, home / "backups").write_yaml(profile_path, profile, "profile.v1")

    missing = validate_private_documents(home, registry, ready=True)

    assert missing
    assert all(field.startswith("$.") for field in missing)
    assert "PRIVATE_READY_CANARY" not in repr(missing)
