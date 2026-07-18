"""Approved dotted-key access to schema-backed private profiles."""

from __future__ import annotations

from collections.abc import Mapping
import re

from jobsearch_skill.errors import ProfileLookupError

_FIELDS = {
    "identity": {"first_name", "middle_name", "last_name", "preferred_name", "full_name", "pronouns"},
    "contact": {"email", "phone", "phone_country_code"},
    "address": {"line1", "line2", "city", "region", "postal_code", "country"},
    "compensation": {"target", "minimum", "currency", "period", "notes"},
    "demographics": {"gender", "race_ethnicity", "sexual_orientation"},
    "disability": {"status", "effective_date"},
    "veteran": {"status", "protected_categories"},
    "legal_attestations": {
        "accurate_information",
        "background_check_consent",
        "non_compete_restriction",
    },
    "links": {"linkedin", "github", "portfolio", "website"},
}
_WORK_AUTHORIZATION_FIELDS = {
    "authorized",
    "status",
    "sponsorship_required",
    "future_sponsorship_required",
    "notes",
}
_COUNTRY_CODE = re.compile(r"^[A-Z]{2,3}$")


def _approved_parts(semantic_key: str) -> tuple[str, ...] | None:
    if not isinstance(semantic_key, str) or not semantic_key or semantic_key.startswith("."):
        return None
    parts = tuple(semantic_key.split("."))
    if any(not part or part.startswith("_") for part in parts):
        return None
    if len(parts) == 2 and parts[0] in _FIELDS and parts[1] in _FIELDS[parts[0]]:
        return parts
    if (
        len(parts) == 3
        and parts[0] == "work_authorization"
        and _COUNTRY_CODE.fullmatch(parts[1])
        and parts[2] in _WORK_AUTHORIZATION_FIELDS
    ):
        return parts
    return None


def lookup_profile_value(profile: Mapping[str, object], semantic_key: str) -> object:
    """Resolve one approved leaf without falling back to attributes or sequences."""

    parts = _approved_parts(semantic_key)
    if parts is None:
        raise ProfileLookupError(
            "profile_path_invalid: requested profile field is not approved",
            reason_code="profile_path_invalid",
        )
    value: object = profile
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            raise ProfileLookupError(
                "profile_path_missing: requested profile field is unavailable",
                reason_code="profile_path_missing",
            )
        value = value[part]
    return value
