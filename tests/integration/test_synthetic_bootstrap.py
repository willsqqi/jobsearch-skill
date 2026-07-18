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

from jobsearch_skill.jobs import make_job_context


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


def test_synthetic_bootstrap_registers_the_two_evaluation_cvs(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0

    result = installed_jobsearch("--home", str(home), "cv", "list")

    assert result.returncode == 0
    cvs = json.loads(result.stdout)["cvs"]
    assert [cv["name"] for cv in cvs] == ["SWE", "DE"]
    assert (home / "synthetic-cv" / "de-resume.tex").is_file()
    assert (home / "synthetic-cv" / "de-resume.pdf").is_file()


def test_synthetic_runtime_stores_unselected_candidate_facts_through_cli(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0
    context_path = home / "candidate-job-context.json"
    context = make_job_context(
        job_url="https://example.invalid/jobs/candidate-facts",
        company="Synthetic Systems",
        role="Platform Software Engineer",
        description="Public synthetic role.",
    )
    context_path.write_text(json.dumps(context), encoding="utf-8")
    started = installed_jobsearch(
        "--home", str(home), "run", "start", "--job-context", str(context_path)
    )
    run_id = json.loads(started.stdout)["result"]["run_id"]

    inspected = installed_jobsearch(
        "--home", str(home), "cv", "inspect", "--run-id", run_id, "--cv", "SWE"
    )
    evidence_ref = json.loads(inspected.stdout)["result"]["evidence_ref"]
    evidence = json.loads((home / evidence_ref).read_text(encoding="utf-8"))
    facts_path = home / "candidate-input.json"
    facts_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "cv_name": "SWE",
                "source_hash": evidence["source_hash"],
                "source_hashes": evidence["source_hashes"],
                "identity": {
                    "full_name": {
                        "value": "Avery Example",
                        "evidence_anchor": "Avery Example",
                    }
                },
                "education": [],
                "employment": [],
                "skills": [
                    {
                        "name": "Python",
                        "evidence_anchor": "Python REST APIs",
                    }
                ],
                "projects": [],
            }
        ),
        encoding="utf-8",
    )

    stored = installed_jobsearch(
        "--home",
        str(home),
        "cv",
        "candidate-facts",
        "--run-id",
        run_id,
        "--cv",
        "SWE",
        "--input",
        str(facts_path),
    )
    shown = installed_jobsearch(
        "--home", str(home), "run", "show", "--run-id", run_id
    )

    assert inspected.returncode == 0, inspected.stdout
    assert stored.returncode == 0, stored.stdout
    assert json.loads(stored.stdout)["result"]["facts_ref"].startswith(
        f"runs/{run_id}/cv-candidates/"
    )
    state = json.loads(shown.stdout)["result"]
    assert state["phase"] == "created"
    assert state["selected_cv"] is None
    assert state["generated_artifacts"] == []

    analysis_path = home / "candidate-analysis.json"
    analysis_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "job_fingerprint": context["job_fingerprint"],
                "role_summary": "Synthetic role",
                "required_qualifications": [],
                "preferred_qualifications": [],
                "strong_matches": [],
                "partial_matches": [],
                "material_gaps": [],
                "cv_comparison": [],
                "recommended_cv": "SWE",
                "recommendation_rationale": "Public synthetic evidence",
                "customization": {
                    "worthwhile": True,
                    "rationale": "Public synthetic evidence",
                },
                "evidence_references": [],
            }
        ),
        encoding="utf-8",
    )
    assert installed_jobsearch(
        "--home",
        str(home),
        "run",
        "analyze",
        "--run-id",
        run_id,
        "--analysis",
        str(analysis_path),
    ).returncode == 0
    assert installed_jobsearch(
        "--home",
        str(home),
        "run",
        "select-cv",
        "--run-id",
        run_id,
        "--cv",
        "SWE",
        "--for-customization",
    ).returncode == 0

    promoted = installed_jobsearch(
        "--home",
        str(home),
        "cv",
        "facts",
        "--run-id",
        run_id,
        "--candidate",
    )

    assert promoted.returncode == 0, promoted.stdout
    promoted_state = json.loads(
        installed_jobsearch(
            "--home", str(home), "run", "show", "--run-id", run_id
        ).stdout
    )["result"]
    assert promoted_state["selected_cv"]["customized"] is True
    assert promoted_state["selected_cv"]["path"].endswith("/resume.tex")
    assert len(promoted_state["generated_artifacts"]) == 2


def test_candidate_cli_failures_use_versioned_value_free_envelopes(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / "PRIVATE_CANDIDATE_HOME_CANARY" / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0
    context_path = home / "job-context.json"
    context_path.write_text(
        json.dumps(
            make_job_context(
                job_url="https://example.invalid/jobs/candidate-errors",
                company="Synthetic Systems",
                role="Platform Software Engineer",
                description="Public synthetic role.",
            )
        ),
        encoding="utf-8",
    )
    started = installed_jobsearch(
        "--home", str(home), "run", "start", "--job-context", str(context_path)
    )
    run_id = json.loads(started.stdout)["result"]["run_id"]

    missing_cv = installed_jobsearch(
        "--home", str(home), "cv", "inspect", "--run-id", run_id, "--cv", "PRIVATE_CV_CANARY"
    )
    missing_input = installed_jobsearch(
        "--home",
        str(home),
        "cv",
        "candidate-facts",
        "--run-id",
        run_id,
        "--cv",
        "SWE",
        "--input",
        str(home / "PRIVATE_INPUT_CANARY.json"),
    )

    assert missing_cv.returncode != 0
    assert missing_input.returncode != 0
    assert json.loads(missing_cv.stdout) == {
        "schema_version": 1,
        "ok": False,
        "command": "cv.inspect",
        "reason_code": "cv_name_missing",
        "warnings": [],
    }
    assert json.loads(missing_input.stdout) == {
        "schema_version": 1,
        "ok": False,
        "command": "cv.candidate-facts",
        "reason_code": "storage_read",
        "warnings": [],
    }
    assert "PRIVATE_CANDIDATE_HOME_CANARY" not in missing_cv.stdout + missing_input.stdout
    assert "PRIVATE_CV_CANARY" not in missing_cv.stdout
    assert "PRIVATE_INPUT_CANARY" not in missing_input.stdout


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


def test_synthetic_bootstrap_rejects_a_valid_pdf_swapped_between_cvs(
    tmp_path: Path,
    installed_jobsearch: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    home = tmp_path / ".jobsearch-eval"
    assert installed_jobsearch("--home", str(home), "bootstrap", "--synthetic").returncode == 0
    cv_root = home / "synthetic-cv"
    (cv_root / "de-resume.pdf").write_bytes((cv_root / "resume.pdf").read_bytes())

    result = installed_jobsearch("--home", str(home), "bootstrap", "--synthetic")

    assert result.returncode == 3
    assert json.loads(result.stdout)["reason_code"] == "synthetic_bootstrap_conflict"
