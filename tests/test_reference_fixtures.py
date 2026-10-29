from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mql_audit import Auditor, audit_ir


def _fixture_path() -> Path:
    configured = os.environ.get("MQL_REFERENCE_FIXTURES")
    if configured:
        return Path(configured)
    sibling = (
        Path(__file__).parents[2]
        / "major-requirement-parser"
        / "docs"
        / "fixtures"
        / "semantic-ir"
        / "reference-fixtures.json"
    )
    if sibling.exists():
        return sibling
    pytest.skip("set MQL_REFERENCE_FIXTURES to run upstream conformance fixtures")


def _fixtures() -> dict:
    return json.loads(_fixture_path().read_text())


def test_all_normative_fixture_outcomes() -> None:
    document = _fixtures()
    for fixture in document["fixtures"]:
        auditor = Auditor(
            fixture["program"], document["institution"], fixture["catalog"]
        )
        for case in fixture["cases"]:
            expected = case["expected"]
            result = auditor.audit(case["transcript"], case["profile"])
            assert result.status == expected["status"], fixture["fixture_id"]
            assert result.program_complete == expected["program_complete"]
            assert result.choices == expected["choices"]
            assert result.objective.satisfied_required_roots == expected["objective"][
                "satisfied_required_roots"
            ]

            if result.status != "INDETERMINATE":
                observed = {
                    item.requirement_id: (
                        item.active,
                        item.q,
                        item.lower,
                        item.upper,
                        item.satisfied,
                    )
                    for item in result.requirements
                }
                wanted = {
                    item["requirement_id"]: (
                        item["active"],
                        item["q"],
                        item["lower"],
                        item["upper"],
                        item["satisfied"],
                    )
                    for item in expected["requirements"]
                }
                assert observed == wanted


def test_audit_result_is_json_serializable() -> None:
    document = _fixtures()
    fixture = document["fixtures"][0]
    case = fixture["cases"][0]
    result = audit_ir(
        fixture["program"],
        document["institution"],
        fixture["catalog"],
        case["transcript"],
        case["profile"],
    )
    assert json.loads(result.json())["status"] == "COMPLETE"

