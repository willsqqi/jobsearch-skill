"""Deterministic normalization for externally observed job descriptions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobsearch_skill.contracts import JOB_CONTEXT_CONTRACT
from jobsearch_skill.errors import JobContextError
from jobsearch_skill.schema import SchemaRegistry

_TRACKING_PARAMETERS = {"source", "ref", "referrer", "gclid", "fbclid", "mc_cid", "mc_eid"}


def _normalized_text(value: str) -> str:
    if not isinstance(value, str):
        raise JobContextError(
            "job_context_invalid: job context fields must be strings",
            reason_code="job_context_invalid",
        )
    return " ".join(value.split())


def _normalized_values(values: Sequence[str]) -> list[str]:
    if isinstance(values, str):
        raise JobContextError(
            "job_context_invalid: job context collections must contain strings",
            reason_code="job_context_invalid",
        )
    try:
        return [_normalized_text(value) for value in values]
    except TypeError as error:
        raise JobContextError(
            "job_context_invalid: job context collections must contain strings",
            reason_code="job_context_invalid",
        ) from error


def canonicalize_job_url(job_url: str) -> str:
    """Return a safe, stable HTTP(S) job URL without common tracking parameters."""

    if not isinstance(job_url, str) or not job_url.strip() or any(char.isspace() for char in job_url):
        raise JobContextError("job_url_invalid: job URL is not supported", reason_code="job_url_invalid")
    try:
        parts = urlsplit(job_url)
        port = parts.port
    except ValueError as error:
        raise JobContextError("job_url_invalid: job URL is not supported", reason_code="job_url_invalid") from error
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise JobContextError("job_url_invalid: job URL is not supported", reason_code="job_url_invalid")
    try:
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise JobContextError("job_url_invalid: job URL is not supported", reason_code="job_url_invalid") from error
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None else f"{host}:{port}"
    query = sorted(
        (
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_PARAMETERS and not key.casefold().startswith("utm_")
        ),
        key=lambda item: (item[0], item[1]),
    )
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", urlencode(query), ""))


def _fingerprint(company: str, role: str, canonical_url: str, description: str) -> str:
    serialized = json.dumps(
        {
            "canonical_url": canonical_url,
            "company": company,
            "description": description,
            "role": role,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def make_job_context(
    *,
    job_url: str,
    description: str,
    company: str = "",
    role: str = "",
    location: str = "",
    required: Sequence[str] = (),
    preferred: Sequence[str] = (),
    technologies: Sequence[str] = (),
    employment_type: str = "",
    responsibilities: Sequence[str] = (),
) -> dict[str, object]:
    """Build and validate one normalized job-context.v1 document."""

    canonical_url = canonicalize_job_url(job_url)
    normalized_technologies = list(dict.fromkeys(_normalized_values(technologies)))
    context: dict[str, object] = {
        "schema_version": 1,
        "job_url": job_url,
        "canonical_url": canonical_url,
        "company": _normalized_text(company),
        "role": _normalized_text(role),
        "location": _normalized_text(location),
        "employment_type": _normalized_text(employment_type),
        "description": _normalized_text(description),
        "responsibilities": _normalized_values(responsibilities),
        "required_qualifications": _normalized_values(required),
        "preferred_qualifications": _normalized_values(preferred),
        "technologies": normalized_technologies,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    context["job_fingerprint"] = _fingerprint(
        context["company"], context["role"], canonical_url, context["description"]
    )
    SchemaRegistry().validate(JOB_CONTEXT_CONTRACT, context)
    return context
