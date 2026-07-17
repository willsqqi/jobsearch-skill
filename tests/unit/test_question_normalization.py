from __future__ import annotations

import pytest

from jobsearch_skill.questions import canonical_question_id, normalize_question


def test_normalization_preserves_negation() -> None:
    positive = normalize_question("Are you authorized to work in the U.S.?")
    negative = normalize_question("Are you NOT authorized to work in the U.S.?")

    assert positive != negative
    assert "not" in negative


def test_normalization_treats_punctuated_acronym_as_same_letters() -> None:
    assert normalize_question("Work in the U.S.?") == normalize_question("Work in the US?")


def test_normalization_preserves_numbers_currency_percentages_and_units() -> None:
    normalized = normalize_question("Expected compensation: $120,000.50 USD / year (10%)?")

    assert "$" in normalized
    assert "120,000.50" in normalized
    assert "usd" in normalized
    assert "year" in normalized
    assert "10%" in normalized
    assert normalized != normalize_question("Expected compensation: $120,000.50 USD / month (10%)?")
    assert normalize_question("Temperature: -5 °C") != normalize_question("Temperature: 5 °C")


@pytest.mark.parametrize("signed", ["-5", "- 5", "−5", "− 5", "﹣ 5", "－5"])
def test_normalization_preserves_spaced_ascii_and_unicode_minus_signs(signed: str) -> None:
    assert normalize_question(f"Temperature: {signed} °C") == normalize_question(
        "Temperature: -5 °C"
    )
    assert normalize_question(f"Temperature: {signed} °C") != normalize_question(
        "Temperature: 5 °C"
    )


@pytest.mark.parametrize("signed", ["+5", "+ 5", "＋ 5"])
def test_normalization_preserves_spaced_ascii_and_unicode_plus_signs(signed: str) -> None:
    assert normalize_question(f"Adjustment: {signed}%") == normalize_question("Adjustment: +5%")
    assert normalize_question(f"Adjustment: {signed}%") != normalize_question("Adjustment: 5%")


def test_normalization_keeps_irrelevant_hyphens_insignificant() -> None:
    assert normalize_question("Is this a full-time role?") == normalize_question(
        "Is this a full time role?"
    )


def test_normalization_uses_unicode_compatibility_and_case_folding() -> None:
    assert normalize_question("Ｕ．Ｓ． WORK AUTHORIZATION") == normalize_question(
        "US work authorization"
    )
    assert normalize_question("Straße") == normalize_question("STRASSE")


def test_canonical_id_is_stable_and_uses_only_identity_fields() -> None:
    first = canonical_question_id(
        "Are you authorized to work in the U.S.?",
        "boolean",
        {"company": "Synthetic Systems", "kind": "company"},
    )
    reordered = canonical_question_id(
        "  ARE YOU AUTHORIZED TO WORK IN THE US!!! ",
        "boolean",
        {"kind": "company", "company": "Synthetic Systems"},
    )

    assert first == reordered
    assert first.startswith("q_")
    assert len(first) == 18
    assert first[2:].isalnum() and first[2:] == first[2:].lower()
