from __future__ import annotations

import json

from jobsearch_skill.redact import Diagnostic, Redactor, format_diagnostic


def test_formatted_diagnostics_never_include_values_or_canaries() -> None:
    canaries = (
        "Avery Synthetic",
        "/private/cv/Avery/resume.pdf",
        "175000",
        "token-SYNTHETIC-secret",
    )
    redactor = Redactor(canaries)
    diagnostic = Diagnostic(
        field_id="identity.full_name",
        decision="ask",
        reason_code="missing_required_field",
    )
    context = {
        "name": canaries[0],
        "path": canaries[1],
        "salary": canaries[2],
        "nested": {"access_token": canaries[3], "safe_count": 2},
    }

    formatted = format_diagnostic(diagnostic, redactor=redactor, context=context)

    assert json.loads(formatted)["diagnostic"] == {
        "field_id": "identity.full_name",
        "decision": "ask",
        "reason_code": "missing_required_field",
    }
    assert formatted.count("[REDACTED]") == 4
    assert not any(canary in formatted for canary in canaries)


def test_redactor_recursively_redacts_value_bearing_keys_and_canary_substrings() -> None:
    redactor = Redactor(["SECRET_CANARY"])
    value = {
        "profile": {"email": "person@example.invalid", "decision": "ask"},
        "message": "prefix SECRET_CANARY suffix",
        "items": [{"phone": "555-0100"}, "SECRET_CANARY"],
    }

    assert redactor.redact(value) == {
        "profile": {"email": "[REDACTED]", "decision": "ask"},
        "message": "prefix [REDACTED] suffix",
        "items": [{"phone": "[REDACTED]"}, "[REDACTED]"],
    }


def test_redactor_removes_canaries_from_mapping_keys_and_diagnostic_fields() -> None:
    canary = "PRIVATE_KEY_CANARY"
    redactor = Redactor([canary])
    diagnostic = Diagnostic(
        field_id=f"field.{canary}",
        decision="ask",
        reason_code=f"reason_{canary}",
    )
    context = {f"prefix-{canary}-suffix": "ordinary identifier"}

    formatted = format_diagnostic(diagnostic, redactor=redactor, context=context)
    payload = json.loads(formatted)

    assert canary not in formatted
    assert payload["diagnostic"] == {
        "field_id": "field.[REDACTED]",
        "decision": "ask",
        "reason_code": "reason_[REDACTED]",
    }
    assert payload["context"] == {"prefix-[REDACTED]-suffix": "ordinary identifier"}


def test_redactor_covers_nested_identity_and_job_values_but_keeps_safe_identifiers() -> None:
    redactor = Redactor([])
    value = {
        "field_id": "workday.personal-information",
        "identity": {
            "first_name": "PrivateFirst",
            "last_name": "PrivateLast",
        },
        "job": {
            "company": "Private Company",
            "role": "Private Role",
            "location": "Private Location",
        },
        "decision": "fill",
        "reason_code": "profile_match",
    }

    assert redactor.redact(value) == {
        "field_id": "workday.personal-information",
        "identity": {
            "first_name": "[REDACTED]",
            "last_name": "[REDACTED]",
        },
        "job": {
            "company": "[REDACTED]",
            "role": "[REDACTED]",
            "location": "[REDACTED]",
        },
        "decision": "fill",
        "reason_code": "profile_match",
    }
