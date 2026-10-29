from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class EvaluationContext:
    course: Mapping[str, Any]
    attempt: Mapping[str, Any]
    profile: Mapping[str, Any]
    offering: Optional[Mapping[str, Any]] = None


def _target(context: EvaluationContext, target: str) -> Mapping[str, Any]:
    if target == "CATALOG_COURSE":
        return context.course
    if target == "TRANSCRIPT_ATTEMPT":
        return context.attempt
    if target == "STUDENT_PROFILE":
        return context.profile
    if target == "OFFERING":
        return context.offering or {}
    return {}


def _field_value(ref: Mapping[str, Any], context: EvaluationContext) -> Any:
    record = _target(context, str(ref["target"]))
    field = str(ref["field"])
    if ref["target"] == "CATALOG_COURSE":
        aliases = {
            "credits": "credit_units",
            "distributionals": "distributions",
            "code": "codes",
        }
        field = aliases.get(field, field)
    elif ref["target"] == "TRANSCRIPT_ATTEMPT":
        if field == "year_of_study" and field not in record:
            level = record.get("academic_level")
            if isinstance(level, int):
                return level
            return {"first_year": 1, "freshman": 1, "sophomore": 2, "junior": 3, "senior": 4}.get(level)
        if field == "enrolled_subject" and field not in record:
            code = str(record.get("enrolled_under_code", "")).replace(" ", "")
            return "".join(ch for ch in code if not ch.isdigit())
    return record.get(field)


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _course_code_compare(
    value: Any, expected: Any, mode: str, context: EvaluationContext
) -> Tuple[Any, Any]:
    canonical = context.attempt.get("canonical_course_id") or context.course.get(
        "canonical_course_id"
    )
    enrolled = str(context.attempt.get("enrolled_under_code", "")).replace(" ", "")
    codes = set(context.course.get("codes", []))
    codes.add(str(canonical))
    if mode == "CANONICAL":
        value = canonical
    elif mode == "ENROLLED_AS":
        value = enrolled
    else:
        value = codes | {enrolled}
    return value, expected


def evaluate_predicate(
    predicate: Mapping[str, Any],
    context: EvaluationContext,
    institution: Mapping[str, Any],
) -> bool:
    kind = str(predicate["kind"])
    if kind == "AND":
        return all(evaluate_predicate(p, context, institution) for p in predicate["operands"])
    if kind == "OR":
        return any(evaluate_predicate(p, context, institution) for p in predicate["operands"])
    if kind == "NOT":
        return not evaluate_predicate(predicate["operand"], context, institution)
    ref = predicate["field"]
    value = _field_value(ref, context)
    if kind in {"IS_NULL", "IS_UNKNOWN"}:
        return value is None
    if value is None:
        return False
    expected = predicate.get("value")
    if ref.get("type") == "COURSE_CODE":
        value, expected = _course_code_compare(
            value, expected, str(ref.get("course_code_match", "CANONICAL")), context
        )
    value, expected = _normalize(value), _normalize(expected)
    if ref.get("type") == "GRADE":
        order = list(institution.get("grade_order", []))
        if value not in order or expected not in order:
            return False
        value, expected = order.index(value), order.index(expected)
    operator = str(predicate["operator"])
    if operator == "EQ":
        return expected in value if isinstance(value, set) else value == expected
    if operator == "NE":
        return expected not in value if isinstance(value, set) else value != expected
    if operator == "LT":
        return bool(value < expected)
    if operator == "LE":
        return bool(value <= expected)
    if operator == "GT":
        return bool(value > expected)
    if operator == "GE":
        return bool(value >= expected)
    if operator == "IN":
        if isinstance(value, (set, list, tuple)):
            return any(v in expected for v in value)
        return value in expected
    if operator == "CONTAINS":
        return expected in value
    if operator == "OVERLAPS":
        return bool(set(value) & set(expected))
    return False


def predicate_fields(predicate: Mapping[str, Any]) -> Set[Tuple[str, str]]:
    kind = predicate.get("kind")
    if kind in {"AND", "OR"}:
        result: Set[Tuple[str, str]] = set()
        for operand in predicate["operands"]:
            result.update(predicate_fields(operand))
        return result
    if kind == "NOT":
        return predicate_fields(predicate["operand"])
    field = predicate.get("field")
    if isinstance(field, Mapping):
        return {(str(field["target"]), str(field["field"]))}
    return set()


def field_value(
    ref: Mapping[str, Any], context: EvaluationContext, institution: Mapping[str, Any]
) -> Any:
    value = _field_value(ref, context)
    if ref.get("type") == "GRADE" and value is not None:
        order: Sequence[str] = institution.get("grade_order", [])
        return order.index(value) if value in order else None
    return value
