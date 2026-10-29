from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Union

JsonObject = Dict[str, Any]
JsonInput = Union[str, bytes, Mapping[str, Any]]


def load_json(value: JsonInput, label: str) -> JsonObject:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


@dataclass(frozen=True)
class AuditOptions:
    max_time_seconds: Optional[float] = None
    num_search_workers: int = 1
    random_seed: int = 0

    def __post_init__(self) -> None:
        if self.max_time_seconds is not None and self.max_time_seconds <= 0:
            raise ValueError("max_time_seconds must be positive")
        if self.num_search_workers < 1:
            raise ValueError("num_search_workers must be positive")


@dataclass(frozen=True)
class Rational:
    numerator: int
    denominator: int = 1

    @classmethod
    def from_fraction(cls, value: Fraction) -> "Rational":
        return cls(value.numerator, value.denominator)


@dataclass(frozen=True)
class Assignment:
    attempt_id: str
    requirement_id: str
    constraint_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rejection:
    attempt_id: str
    requirement_id: str
    constraint_id: str
    reason: str


@dataclass(frozen=True)
class RequirementResult:
    requirement_id: str
    active: bool
    q: int
    lower: int
    upper: Optional[int]
    satisfied: bool
    progress: Rational


@dataclass(frozen=True)
class ObjectiveValue:
    satisfied_required_roots: int
    progress: Rational
    manual_approval_uses: int


@dataclass(frozen=True)
class TraceEvent:
    step: int
    semantic_id: str
    result: str
    rationale: str


@dataclass(frozen=True)
class CandidateUsefulness:
    attempt_id: str
    requirement_id: str
    outcome: str
    preserves_objective: bool
    blocking_constraint_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return {
            "kind": "candidate_usefulness",
            "attempt_id": self.attempt_id,
            "requirement_id": self.requirement_id,
            "outcome": self.outcome,
            "preserves_objective": self.preserves_objective,
            "blocking_constraint_ids": list(self.blocking_constraint_ids),
        }


@dataclass(frozen=True)
class AuditResult:
    status: str
    program_complete: Optional[bool]
    choices: JsonObject
    effects: List[JsonObject]
    assignments: List[Assignment]
    rejections: List[Rejection]
    requirements: List[RequirementResult]
    totals: List[JsonObject]
    objective: ObjectiveValue
    trace: List[TraceEvent]
    candidate_usefulness: List[CandidateUsefulness] = field(default_factory=list)
    case_id: str = "audit"

    def to_dict(self) -> JsonObject:
        def convert(value: Any) -> Any:
            if isinstance(value, Rational):
                return {"numerator": value.numerator, "denominator": value.denominator}
            if hasattr(value, "__dataclass_fields__"):
                return {k: convert(v) for k, v in asdict(value).items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return {
            "case_id": self.case_id,
            "status": self.status,
            "program_complete": self.program_complete,
            "choices": convert(self.choices),
            "effects": convert(self.effects),
            "assignments": convert(self.assignments),
            "rejections": convert(self.rejections),
            "requirements": convert(self.requirements),
            "totals": convert(self.totals),
            "objective": convert(self.objective),
            "alternate_witnesses": [item.to_dict() for item in self.candidate_usefulness],
            "trace": convert(self.trace),
        }

    def json(self, *, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)
