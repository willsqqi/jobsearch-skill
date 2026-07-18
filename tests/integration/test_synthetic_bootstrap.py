from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def installed_jobsearch(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Install the current package into an isolated target before invoking it."""

    repo_root = Path(__file__).parents[2]
    installed = tmp_path / "installed"
    shutil.copytree(repo_root / "src" / "jobsearch_skill", installed / "jobsearch_skill")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(installed)
        return subprocess.run(
            [sys.executable, "-m", "jobsearch_skill", *arguments],
            cwd=installed,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    return run


def test_synthetic_bootstrap_uses_only_packaged_resources(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"

    result = installed_jobsearch("--home", str(home), "bootstrap", "--synthetic")

    assert result.returncode == 0, result.stdout
    assert "Avery Example" in (home / "profile.yaml").read_text(encoding="utf-8")
    assert (home / "synthetic-cv" / "resume.tex").is_file()
    assert (home / "synthetic-cv" / "resume.pdf").is_file()


def test_synthetic_bootstrap_is_private_ready_and_byte_stable(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0
    tracked = [
        home / "profile.yaml",
        home / "preferences.yaml",
        home / "questions.yaml",
        home / ".synthetic-bootstrap.json",
        home / "synthetic-cv" / "resume.tex",
        home / "synthetic-cv" / "resume.pdf",
    ]
    original = {path: path.read_bytes() for path in tracked}

    repeated = installed_jobsearch("--home", str(home), "bootstrap", "--synthetic")
    ready = installed_jobsearch("--home", str(home), "validate", "--ready")

    assert repeated.returncode == 0
    assert ready.returncode == 0
    assert json.loads(ready.stdout)["missing_fields"] == []
    assert {path: path.read_bytes() for path in tracked} == original
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in tracked)


def test_synthetic_bootstrap_refuses_to_replace_an_ordinary_profile(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap").returncode == 0
    profile = home / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "# PRIVATE_CANARY\n", encoding="utf-8")
    original = profile.read_bytes()

    result = installed_jobsearch("--home", str(home), "bootstrap", "--synthetic")

    assert result.returncode == 3
    assert json.loads(result.stdout)["reason_code"] == "synthetic_bootstrap_conflict"
    assert profile.read_bytes() == original


def test_synthetic_bootstrap_rejects_a_changed_pdf(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0
    pdf = home / "synthetic-cv" / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nchanged but nonempty\n")

    result = installed_jobsearch("--home", str(home), "bootstrap", "--synthetic")

    assert result.returncode == 3
    assert json.loads(result.stdout)["reason_code"] == "synthetic_bootstrap_conflict"
