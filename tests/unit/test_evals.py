from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def test_every_evaluation_case_matches_the_public_rubric() -> None:
    root = Path(__file__).parents[2] / "evals"
    schema = json.loads((root / "rubric.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    for path in sorted(root.glob("*/case.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        validator.validate(case)
        expected = [behavior["id"] for behavior in case["expected_behaviors"]]
        forbidden = [behavior["id"] for behavior in case["forbidden_behaviors"]]
        assert len(expected) == len(set(expected))
        assert len(forbidden) == len(set(forbidden))
        assert set(expected).isdisjoint(forbidden)
        assert case["minimum_pass_count"] <= len(expected)
