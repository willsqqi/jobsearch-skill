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
