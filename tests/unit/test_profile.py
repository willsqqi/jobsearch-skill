from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobsearch_skill.errors import ProfileLookupError
from jobsearch_skill.profile import lookup_profile_value


@pytest.fixture
def profile() -> dict[str, object]:
    value = yaml.safe_load(
        (Path(__file__).parents[1] / "fixtures" / "private-home" / "profile.yaml").read_text()
    )
    assert isinstance(value, dict)
    return value


def test_profile_lookup_returns_declared_string_boolean_and_number_values(
    profile: dict[str, object]
) -> None:
    assert lookup_profile_value(profile, "identity.first_name") == "Casey"
    assert lookup_profile_value(profile, "work_authorization.USA.authorized") is True
    assert lookup_profile_value(profile, "compensation.target") == 100


def test_profile_lookup_rejects_missing_path_without_echoing_value(profile: dict[str, object]) -> None:
    profile["identity"] = {"first_name": "PRIVATE_PROFILE_CANARY"}

    with pytest.raises(ProfileLookupError) as error:
        lookup_profile_value(profile, "identity.last_name")

    assert error.value.reason_code == "profile_path_missing"
    assert "PRIVATE_PROFILE_CANARY" not in str(error.value)


@pytest.mark.parametrize("semantic_key", ["identity..first_name", "identity.__class__", "unknown.value"])
def test_profile_lookup_rejects_unapproved_paths(profile: dict[str, object], semantic_key: str) -> None:
    with pytest.raises(ProfileLookupError) as error:
        lookup_profile_value(profile, semantic_key)

    assert error.value.reason_code == "profile_path_invalid"
