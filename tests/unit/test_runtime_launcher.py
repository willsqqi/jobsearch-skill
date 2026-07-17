import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher_module():
    path = Path("scripts/jobsearch")
    loader = importlib.machinery.SourceFileLoader("jobsearch_launcher", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_launcher_requires_private_runtime(tmp_path, launcher_module) -> None:
    result = launcher_module.resolve_runtime(tmp_path / ".jobsearch")
    assert result.ok is False
    assert result.reason_code == "runtime_not_installed"


def test_launcher_prefers_environment_home(monkeypatch, tmp_path, launcher_module) -> None:
    configured_home = tmp_path / "configured"
    monkeypatch.setenv("JOBSEARCH_HOME", str(configured_home))
    assert launcher_module.resolve_home() == configured_home


def test_launcher_reads_local_pointer(monkeypatch, tmp_path, launcher_module) -> None:
    pointer = tmp_path / "jobsearch-home"
    configured_home = tmp_path / "configured"
    pointer.write_text(str(configured_home))
    os.chmod(pointer, 0o600)
    monkeypatch.delenv("JOBSEARCH_HOME", raising=False)
    assert launcher_module.resolve_home(pointer) == configured_home
