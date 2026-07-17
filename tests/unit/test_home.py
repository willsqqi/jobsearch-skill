from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobsearch_skill.errors import ConfigurationError
from jobsearch_skill.home import (
    default_config_path,
    resolve_private_home,
    validate_private_home,
)


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
