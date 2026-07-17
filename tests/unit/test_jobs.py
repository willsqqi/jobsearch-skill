from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch_skill.errors import JobContextError, SchemaValidationError
from jobsearch_skill.jobs import make_job_context
from jobsearch_skill.schema import SchemaRegistry


def test_job_fixture_normalizes_to_packaged_contract() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "jobs" / "backend-engineer.json").read_text()
    )

    context = make_job_context(**fixture)

    SchemaRegistry().validate("job-context.v1", context)
    assert context["canonical_url"] == "https://careers.example.invalid/jobs/backend-engineer?job_id=7"
    assert str(context["captured_at"]).endswith("Z")


def test_job_context_schema_requires_captured_at() -> None:
    context = make_job_context(job_url="https://example.invalid/jobs/7", description="Build APIs")
    context.pop("captured_at")

    with pytest.raises(SchemaValidationError, match="schema_validation"):
        SchemaRegistry().validate("job-context.v1", context)


def test_job_fingerprint_ignores_url_tracking_parameters() -> None:
    first = make_job_context(
        job_url="https://example.invalid/j/7?source=a&utm_campaign=spring&id=7",
        description="Build APIs",
    )
    second = make_job_context(
        job_url="https://EXAMPLE.invalid/j/7?id=7&source=b&utm_campaign=summer#details",
        description="Build APIs",
    )

    assert first["job_fingerprint"] == second["job_fingerprint"]
    assert first["canonical_url"] == "https://example.invalid/j/7?id=7"


def test_job_fingerprint_normalizes_whitespace_but_changes_for_meaningful_content() -> None:
    first = make_job_context(
        job_url="https://example.invalid/j/7?posting=7",
        company=" Example   Engineering ",
        role="Backend\nEngineer",
        description="Build  reliable\n APIs",
    )
    same = make_job_context(
        job_url="https://example.invalid/j/7?posting=7",
        company="Example Engineering",
        role="Backend Engineer",
        description="Build reliable APIs",
    )
    changed = make_job_context(
        job_url="https://example.invalid/j/7?posting=7",
        company="Example Engineering",
        role="Backend Engineer",
        description="Build reliable batch pipelines",
    )

    assert first["job_fingerprint"] == same["job_fingerprint"]
    assert first["job_fingerprint"] != changed["job_fingerprint"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.invalid/job/7",
        "https://user:secret@example.invalid/job/7",
        "https:///job/7",
        "not a url",
    ],
)
def test_job_context_rejects_unsafe_or_malformed_urls_without_echoing_input(url: str) -> None:
    with pytest.raises(JobContextError) as error:
        make_job_context(job_url=url, description="Build APIs")

    assert error.value.reason_code == "job_url_invalid"
    assert url not in str(error.value)
