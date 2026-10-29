from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

import pylibmql
from ortools.sat.python import cp_model

from .errors import InputValidationError, SolveTimeoutError, UnsupportedSchemaError
from .models import (
    Assignment,
    AuditOptions,
    AuditResult,
    CandidateUsefulness,
    JsonInput,
    ObjectiveValue,
    Rational,
    Rejection,
    RequirementResult,
    TraceEvent,
    load_json,
)
from .predicates import EvaluationContext, evaluate_predicate, field_value, predicate_fields

SCHEMA_VERSION = "0.2.0"


def _meta_id(item: Mapping[str, Any]) -> str:
    return str(item["meta"]["id"])


def _flatten_requirements(requirements: Sequence[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], Dict[str, Optional[str]]]:
    result: List[Mapping[str, Any]] = []
    parents: Dict[str, Optional[str]] = {}

    def visit(requirement: Mapping[str, Any], parent: Optional[str]) -> None:
        requirement_id = _meta_id(requirement)
        result.append(requirement)
        parents[requirement_id] = parent
        for child in requirement.get("children", []):
            visit(child, requirement_id)

    for root in requirements:
        visit(root, None)
    return result, parents


def _descendants(requirement_id: str, parents: Mapping[str, Optional[str]]) -> Set[str]:
    result = {requirement_id}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in result and child not in result:
                result.add(child)
                changed = True
    return result


def _fact_matches(declaration: Mapping[str, Any], fact: Mapping[str, Any]) -> bool:
    if fact.get("fact_type") != declaration.get("fact_type"):
        return False
    expected = declaration.get("parameters", {})
    actual = fact.get("parameters", {})
    return all(actual.get(key) == value for key, value in expected.items())


@dataclass
class _SolveValues:
    status: int
    solver: cp_model.CpSolver
    complete: bool
    satisfied_roots: int
    constraint_score: int
    progress_scaled: int
    progress_scale: int
    manual_uses: int


class _Builder:
    def __init__(
        self,
        auditor: "Auditor",
        transcript: Mapping[str, Any],
        profile: Mapping[str, Any],
        *,
        forced: Optional[Tuple[str, str]] = None,
        target: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        self.auditor = auditor
        self.program = auditor.program
        self.institution = auditor.institution
        self.catalog = auditor.catalog
        self.transcript = transcript
        self.profile = profile
        self.options = auditor.options
        self.model = cp_model.CpModel()
        self.forced = forced
        self.target = target
        self.requirements, self.parents = _flatten_requirements(self.program["requirements"])
        self.requirement_by_id = {_meta_id(r): r for r in self.requirements}
        self.root_ids = [_meta_id(r) for r in self.program["requirements"]]
        self.attempts = list(transcript.get("attempts", []))
        self.attempt_by_id = {str(a["attempt_id"]): a for a in self.attempts}
        self.course_by_id = {
            str(c["canonical_course_id"]): c for c in self.catalog.get("courses", [])
        }
        self.contexts: Dict[str, EvaluationContext] = {}
        for attempt in self.attempts:
            attempt_id = str(attempt["attempt_id"])
            course = self.course_by_id.get(str(attempt["canonical_course_id"]), {})
            self.contexts[attempt_id] = EvaluationContext(course, attempt, profile)
        self.choice_values: Dict[str, Any] = {}
        self.choice_vars: Dict[Tuple[str, Any], cp_model.IntVar] = {}
        self.present_facts: Set[str] = set()
        self.approval_fact_ids: Set[str] = set()
        self.applied_effects: List[Dict[str, Any]] = []
        self.active_override: Dict[str, bool] = {}
        self.bound_override: Dict[Tuple[str, str], int] = {}
        self.obligation_override: Dict[str, str] = {}
        self.waived: Set[str] = set()
        self.permits: Dict[str, List[Mapping[str, Any]]] = {}
        self.x: Dict[Tuple[str, str], cp_model.IntVar] = {}
        self.eligible: Dict[Tuple[str, str], bool] = {}
        self.approval_match: Dict[Tuple[str, str], bool] = {}
        self.active: Dict[str, cp_model.IntVar] = {}
        self.q: Dict[str, cp_model.IntVar] = {}
        self.satisfied: Dict[str, cp_model.IntVar] = {}
        self.lower_values: Dict[str, List[int]] = {}
        self.upper_values: Dict[str, List[int]] = {}
        self.constraint_sat: Dict[str, cp_model.IntVar] = {}
        self.total_data: Dict[str, Tuple[List[cp_model.IntVar], int, str]] = {}
        self._used_cache: Dict[Tuple[str, Tuple[str, ...]], cp_model.IntVar] = {}
        self._prepare_profile()
        self._apply_static_effects()
        self._build_choices()
        self._build_requirements()
        self._build_exclusivity()
        self._build_constraints()
        self._build_forced()

    def _prepare_profile(self) -> None:
        facts = list(self.profile.get("facts", []))
        for declaration in self.program.get("facts", []):
            fact_id = _meta_id(declaration)
            if any(_fact_matches(declaration, fact) for fact in facts):
                self.present_facts.add(fact_id)
                if declaration.get("approval", False):
                    self.approval_fact_ids.add(fact_id)
        declared = self.profile.get("declared_choices", {})
        for choice in self.program.get("choices", []):
            choice_id = _meta_id(choice)
            owner = choice["owner"]
            if owner == "DECLARED":
                if choice_id not in declared:
                    raise _Indeterminate(choice_id)
                value = declared[choice_id]
                if value not in choice["alternatives"]:
                    raise InputValidationError(
                        f"declared choice {choice_id!r} has invalid value {value!r}"
                    )
                self.choice_values[choice_id] = value
            elif owner == "FACT":
                present = str(choice.get("fact_id")) in self.present_facts
                alternatives = choice["alternatives"]
                value = present if present in alternatives else alternatives[int(present)]
                self.choice_values[choice_id] = value
            elif owner == "PLANNING":
                raise _Indeterminate(choice_id)

    def _condition_static(self, condition: Mapping[str, Any]) -> Optional[bool]:
        kind = condition["kind"]
        if kind == "CONSTANT":
            return bool(condition["value"])
        if kind == "FACT_PRESENT":
            return str(condition["fact_id"]) in self.present_facts
        if kind == "CHOICE_EQUALS":
            choice_id = str(condition["choice_id"])
            if choice_id in self.choice_values:
                return self.choice_values[choice_id] == condition["equals"]
            return None
        if kind == "NOT":
            value = self._condition_static(condition["operand"])
            return None if value is None else not value
        if kind in {"AND", "OR"}:
            values = [self._condition_static(c) for c in condition["operands"]]
            if kind == "AND":
                if False in values:
                    return False
                return True if all(v is True for v in values) else None
            if True in values:
                return True
            return False if all(v is False for v in values) else None
        return None

    def _apply_static_effects(self) -> None:
        writes: Set[Tuple[str, str]] = set()
        for effect in self.program.get("effects", []):
            active = self._condition_static(effect["when"])
            if active is not True:
                continue
            effect_id = _meta_id(effect)
            target = str(effect["target_id"])
            kind = effect["kind"]
            write_key = (target, str(effect.get("bound", kind)))
            if write_key in writes:
                raise _InvalidEvaluation(f"conflicting effects write {write_key}")
            writes.add(write_key)
            if kind == "SET_ACTIVE":
                self.active_override[target] = bool(effect["active"])
            elif kind == "REPLACE_BOUND":
                self.bound_override[(target, str(effect["bound"]))] = int(effect["value"])
            elif kind == "WAIVE":
                self.waived.add(target)
            elif kind == "SUBSTITUTE_NODE":
                self.active_override[target] = False
                self.active_override[str(effect["replacement_id"])] = True
            elif kind == "ADD_OBLIGATION":
                self.obligation_override[target] = str(effect["obligation"])
            elif kind == "PERMIT_ASSIGNMENT":
                self.permits.setdefault(target, []).append(effect["predicate"])
            witness = next(
                (f for f in self.present_facts if self._condition_mentions_fact(effect["when"], f)),
                "condition",
            )
            self.applied_effects.append(
                {"effect_id": effect_id, "target_id": target, "witness": witness}
            )

    def _condition_mentions_fact(self, condition: Mapping[str, Any], fact_id: str) -> bool:
        if condition["kind"] == "FACT_PRESENT":
            return condition["fact_id"] == fact_id
        if condition["kind"] in {"AND", "OR"}:
            return any(self._condition_mentions_fact(c, fact_id) for c in condition["operands"])
        if condition["kind"] == "NOT":
            return self._condition_mentions_fact(condition["operand"], fact_id)
        return False

    def _build_choices(self) -> None:
        for choice in self.program.get("choices", []):
            choice_id = _meta_id(choice)
            if choice_id in self.choice_values:
                continue
            variables = []
            for alternative in choice["alternatives"]:
                var = self.model.new_bool_var(f"choice[{choice_id}={alternative}]")
                self.choice_vars[(choice_id, alternative)] = var
                variables.append(var)
            self.model.add_exactly_one(variables)

    def _condition(self, condition: Mapping[str, Any], name: str) -> Union[bool, cp_model.IntVar]:
        static = self._condition_static(condition)
        if static is not None:
            return static
        kind = condition["kind"]
        if kind == "CHOICE_EQUALS":
            return self.choice_vars[(str(condition["choice_id"]), condition["equals"])]
        if kind == "NOT":
            operand = self._condition(condition["operand"], name + ".not")
            if isinstance(operand, bool):
                return not operand
            result = self.model.new_bool_var(name)
            self.model.add(result + operand == 1)
            return result
        operands = [self._condition(c, f"{name}.{i}") for i, c in enumerate(condition["operands"])]
        if kind == "AND" and any(v is False for v in operands):
            return False
        if kind == "OR" and any(v is True for v in operands):
            return True
        variables = [v for v in operands if not isinstance(v, bool)]
        if not variables:
            return kind == "AND"
        result = self.model.new_bool_var(name)
        if kind == "AND":
            self.model.add_bool_and(variables).only_enforce_if(result)
            self.model.add_bool_or([v.Not() for v in variables]).only_enforce_if(result.Not())
        else:
            self.model.add_bool_or(variables).only_enforce_if(result)
            self.model.add_bool_and([v.Not() for v in variables]).only_enforce_if(result.Not())
        return result

    def _set_bool_equal(self, var: cp_model.IntVar, value: Union[bool, cp_model.IntVar]) -> None:
        if isinstance(value, bool):
            self.model.add(var == int(value))
        else:
            self.model.add(var == value)

    def _bound(self, requirement_id: str, bound: Mapping[str, Any], which: str) -> Tuple[Any, List[int]]:
        override = self.bound_override.get((requirement_id, which))
        if override is not None:
            return override, [override]
        if bound["kind"] == "FIXED":
            value = int(bound["value"])
            return value, [value]
        choice_id = str(bound["choice_id"])
        values = [int(v) for v in bound["cases"].values()]
        if choice_id in self.choice_values:
            return int(bound["cases"][str(self.choice_values[choice_id])]), values
        expression = sum(
            int(value) * self.choice_vars[(choice_id, self._coerce_case(choice_id, key))]
            for key, value in bound["cases"].items()
        )
        return expression, values

    def _coerce_case(self, choice_id: str, key: str) -> Any:
        alternatives = next(
            c["alternatives"] for c in self.program["choices"] if _meta_id(c) == choice_id
        )
        for alternative in alternatives:
            if str(alternative).lower() == key.lower():
                return alternative
        return key

    def _selector_match(self, requirement: Mapping[str, Any], attempt_id: str) -> Tuple[bool, bool]:
        selector = requirement["selector"]
        context = self.contexts[attempt_id]
        matched = False
        approval = False
        if selector["kind"] == "COURSE_LIST":
            matched = str(context.attempt["canonical_course_id"]) in selector["canonical_course_ids"]
        elif selector["kind"] == "PREDICATE":
            matched = evaluate_predicate(selector["predicate"], context, self.institution)
            fields = predicate_fields(selector["predicate"])
            approval = matched and ("TRANSCRIPT_ATTEMPT", "approved_for") in fields
        for predicate in self.permits.get(_meta_id(requirement), []):
            if evaluate_predicate(predicate, context, self.institution):
                matched, approval = True, True
        return matched, approval

    def _build_requirements(self) -> None:
        max_credits = sum(int(a.get("earned_credit_units", 0)) for a in self.attempts)
        declared_bounds = [
            int(value)
            for requirement in self.requirements
            for bound in (requirement.get("lower"), requirement.get("upper"))
            if bound is not None
            for value in (
                [bound["value"]]
                if bound["kind"] == "FIXED"
                else bound["cases"].values()
            )
        ]
        global_max = max(
            len(self.attempts),
            max_credits,
            len(self.requirements),
            *declared_bounds,
            1,
        )
        for requirement in self.requirements:
            requirement_id = _meta_id(requirement)
            self.active[requirement_id] = self.model.new_bool_var(f"active[{requirement_id}]")
            self.q[requirement_id] = self.model.new_int_var(0, global_max, f"q[{requirement_id}]")
            self.satisfied[requirement_id] = self.model.new_bool_var(
                f"satisfied[{requirement_id}]"
            )
        for requirement in self.requirements:
            requirement_id = _meta_id(requirement)
            active = self.active[requirement_id]
            override = self.active_override.get(requirement_id)
            own_condition: Union[bool, cp_model.IntVar]
            own_condition = override if override is not None else self._condition(
                requirement["active_when"], f"condition[{requirement_id}]"
            )
            parent = self.parents[requirement_id]
            if parent is None:
                self._set_bool_equal(active, own_condition)
            else:
                parent_active = self.active[parent]
                if own_condition is True:
                    self.model.add(active == parent_active)
                elif own_condition is False:
                    self.model.add(active == 0)
                else:
                    self.model.add(active <= parent_active)
                    self.model.add(active <= own_condition)
                    self.model.add(active >= parent_active + own_condition - 1)

            lower_expr, lower_values = self._bound(requirement_id, requirement["lower"], "LOWER")
            self.lower_values[requirement_id] = lower_values
            if requirement["upper"] is None:
                upper_expr = None
                upper_values = [max(len(self.attempts), max_credits, len(requirement["children"]))]
            else:
                upper_expr, upper_values = self._bound(
                    requirement_id, requirement["upper"], "UPPER"
                )
            self.upper_values[requirement_id] = upper_values
            q = self.q[requirement_id]
            sat = self.satisfied[requirement_id]

            assignment_vars = []
            if requirement["selector"]["kind"] != "CHILD_REQUIREMENTS":
                for attempt in self.attempts:
                    attempt_id = str(attempt["attempt_id"])
                    matched, approval = self._selector_match(requirement, attempt_id)
                    self.eligible[(attempt_id, requirement_id)] = matched
                    self.approval_match[(attempt_id, requirement_id)] = approval
                    if not matched:
                        continue
                    var = self.model.new_bool_var(f"x[{attempt_id},{requirement_id}]")
                    self.x[(attempt_id, requirement_id)] = var
                    self.model.add(var <= active)
                    assignment_vars.append(var)

            kind = requirement["contribution"]["kind"]
            if requirement_id in self.waived:
                self.model.add(q == lower_expr).only_enforce_if(active)
            elif kind in {"SLOT", "DISTINCT_ATTEMPTS", "ASSIGNMENTS"}:
                self.model.add(q == sum(assignment_vars))
                if kind == "SLOT":
                    self.model.add(q <= 1)
            elif kind == "CREDITS":
                fixed = requirement["contribution"].get("fixed_credit_units")
                terms = []
                for (attempt_id, rid), var in self.x.items():
                    if rid == requirement_id:
                        credit = int(fixed if fixed is not None else self.attempt_by_id[attempt_id].get("earned_credit_units", 0))
                        terms.append(credit * var)
                self.model.add(q == sum(terms))
            elif kind == "DISTINCT_CANONICAL_COURSES":
                canonical_vars = self._canonical_use_vars([requirement_id])
                self.model.add(q == sum(canonical_vars.values()))
            elif kind == "NESTED_SATISFACTION":
                child_ids = requirement["selector"].get("child_ids", [])
                self.model.add(q == sum(self.satisfied[cid] for cid in child_ids))
            else:
                self.model.add(q == 0)
            self.model.add(q == 0).only_enforce_if(active.Not())
            if upper_expr is not None:
                self.model.add(q <= upper_expr).only_enforce_if(active)
            self.model.add(sat <= active)
            self.model.add(q >= lower_expr).only_enforce_if(sat)
            self.model.add(q <= lower_expr - 1).only_enforce_if([active, sat.Not()])
            if requirement_id in self.waived:
                self.model.add(sat == active)

    def _sharing_pairs(self) -> Set[frozenset[str]]:
        pairs: Set[frozenset[str]] = set()
        for rule in self.program.get("sharing_rules", []):
            if self._condition_static(rule["active_when"]) is not True or rule["mode"] != "SHARE":
                continue
            ids = list(rule["requirement_ids"])
            for i, left in enumerate(ids):
                for right in ids[i + 1 :]:
                    pairs.add(frozenset((str(left), str(right))))
        return pairs

    def _build_exclusivity(self) -> None:
        sharing = self._sharing_pairs()
        for attempt in self.attempts:
            attempt_id = str(attempt["attempt_id"])
            reqs = [rid for aid, rid in self.x if aid == attempt_id]
            for i, left in enumerate(reqs):
                for right in reqs[i + 1 :]:
                    if frozenset((left, right)) not in sharing:
                        self.model.add(self.x[(attempt_id, left)] + self.x[(attempt_id, right)] <= 1)

    def _scope_ids(self, scope: Mapping[str, Any]) -> List[str]:
        kind = scope["kind"]
        if kind == "DIRECT":
            return [str(scope["requirement_id"])]
        if kind == "SUBTREE":
            return sorted(_descendants(str(scope["requirement_id"]), self.parents))
        if kind == "REQUIREMENTS":
            return [str(v) for v in scope["requirement_ids"]]
        if kind == "ROLES":
            roles = set(scope["roles"])
            return [_meta_id(r) for r in self.requirements if roles & set(r["roles"])]
        if kind == "ACTIVE_BRANCH":
            choice_id, branch = str(scope["choice_id"]), scope["branch"]
            return [
                _meta_id(r)
                for r in self.requirements
                if self._condition_mentions_choice(r["active_when"], choice_id, branch)
            ]
        return list(self.requirement_by_id)

    def _condition_mentions_choice(self, cond: Mapping[str, Any], choice: str, branch: Any) -> bool:
        if cond["kind"] == "CHOICE_EQUALS":
            return cond["choice_id"] == choice and cond["equals"] == branch
        if cond["kind"] in {"AND", "OR"}:
            return any(self._condition_mentions_choice(c, choice, branch) for c in cond["operands"])
        if cond["kind"] == "NOT":
            return self._condition_mentions_choice(cond["operand"], choice, branch)
        return False

    def _used_attempt_vars(self, requirement_ids: Sequence[str]) -> Dict[str, cp_model.IntVar]:
        key_ids = tuple(sorted(requirement_ids))
        result = {}
        for attempt in self.attempts:
            attempt_id = str(attempt["attempt_id"])
            key = (attempt_id, key_ids)
            if key in self._used_cache:
                result[attempt_id] = self._used_cache[key]
                continue
            variables = [self.x[(attempt_id, rid)] for rid in requirement_ids if (attempt_id, rid) in self.x]
            used = self.model.new_bool_var(f"used[{attempt_id},{','.join(key_ids)}]")
            if variables:
                self.model.add_max_equality(used, variables)
            else:
                self.model.add(used == 0)
            self._used_cache[key] = used
            result[attempt_id] = used
        return result

    def _canonical_use_vars(self, requirement_ids: Sequence[str]) -> Dict[str, cp_model.IntVar]:
        attempts = self._used_attempt_vars(requirement_ids)
        by_course: Dict[str, List[cp_model.IntVar]] = {}
        for attempt_id, used in attempts.items():
            canonical = str(self.attempt_by_id[attempt_id]["canonical_course_id"])
            by_course.setdefault(canonical, []).append(used)
        result = {}
        for canonical, variables in by_course.items():
            var = self.model.new_bool_var(f"canonical[{canonical},{','.join(sorted(requirement_ids))}]")
            self.model.add_max_equality(var, variables)
            result[canonical] = var
        return result

    def _predicate_attempts(self, predicate: Mapping[str, Any]) -> Set[str]:
        return {
            attempt_id
            for attempt_id, context in self.contexts.items()
            if evaluate_predicate(predicate, context, self.institution)
        }

    def _count_expression(
        self,
        scope_ids: Sequence[str],
        unit: str,
        predicate: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Any, List[cp_model.IntVar]]:
        allowed = self._predicate_attempts(predicate) if predicate is not None else set(self.attempt_by_id)
        used = self._used_attempt_vars(scope_ids)
        filtered = {aid: var for aid, var in used.items() if aid in allowed}
        if unit in {"ATTEMPTS", "DISTINCT_ATTEMPTS"}:
            return sum(filtered.values()), list(filtered.values())
        if unit in {"CREDIT_UNITS", "CREDITS"}:
            return sum(int(self.attempt_by_id[aid].get("earned_credit_units", 0)) * var for aid, var in filtered.items()), list(filtered.values())
        if unit in {"CANONICAL_COURSES", "DISTINCT_CANONICAL_COURSES"}:
            canonical = self._canonical_use_vars(scope_ids)
            eligible_canonical = {str(self.attempt_by_id[aid]["canonical_course_id"]) for aid in allowed}
            variables = [var for code, var in canonical.items() if code in eligible_canonical]
            return sum(variables), variables
        variables = [
            var
            for (attempt_id, rid), var in self.x.items()
            if rid in scope_ids and attempt_id in allowed
        ]
        return sum(variables), variables

    def _reified_comparison(self, expression: Any, comparison: str, amount: int, name: str) -> cp_model.IntVar:
        sat = self.model.new_bool_var(name)
        if comparison == "AT_LEAST":
            self.model.add(expression >= amount).only_enforce_if(sat)
            self.model.add(expression <= amount - 1).only_enforce_if(sat.Not())
        elif comparison == "AT_MOST":
            self.model.add(expression <= amount).only_enforce_if(sat)
            self.model.add(expression >= amount + 1).only_enforce_if(sat.Not())
        elif comparison == "EXACTLY":
            below = self.model.new_bool_var(name + ".below")
            above = self.model.new_bool_var(name + ".above")
            self.model.add(expression == amount).only_enforce_if(sat)
            self.model.add(expression <= amount - 1).only_enforce_if(below)
            self.model.add(expression >= amount + 1).only_enforce_if(above)
            self.model.add_exactly_one(sat, below, above)
        else:
            self.model.add(sat == 1)
        return sat

    def _build_constraints(self) -> None:
        for constraint in self.program.get("constraints", []):
            constraint_id = _meta_id(constraint)
            active = self._condition(constraint["active_when"], f"constraint_active[{constraint_id}]")
            if active is False:
                continue
            scope_ids = self._scope_ids(constraint["scope"])
            kind = constraint["kind"]
            sat: cp_model.IntVar
            if kind == "SELECTED_SET":
                expression, variables = self._count_expression(scope_ids, constraint["unit"], constraint["predicate"])
                comparison = constraint["comparison"]
                if comparison == "ALL":
                    all_expr, _ = self._count_expression(scope_ids, constraint["unit"])
                    sat = self._reified_comparison(expression - all_expr, "EXACTLY", 0, f"constraint[{constraint_id}]")
                else:
                    sat = self._reified_comparison(expression, comparison, int(constraint["amount"]), f"constraint[{constraint_id}]")
            elif kind == "LIMIT":
                expression, _ = self._count_expression(scope_ids, constraint["unit"], constraint["predicate"])
                sat = self._reified_comparison(expression, "AT_MOST", int(constraint["maximum"]), f"constraint[{constraint_id}]")
            elif kind == "TOTAL":
                expression, variables = self._count_expression(scope_ids, constraint["count"], constraint.get("predicate"))
                sat = self._reified_comparison(expression, "AT_LEAST", int(constraint["minimum"]), f"constraint[{constraint_id}]")
                self.total_data[constraint_id] = (variables, int(constraint["minimum"]), str(constraint["count"]))
            elif kind == "REPEAT":
                # Repeat policy limits attempts of each matching canonical course;
                # it does not cap the entire predicate-selected course family.
                used = self._used_attempt_vars(scope_ids)
                matching = self._predicate_attempts(constraint["predicate"])
                by_canonical: Dict[str, List[cp_model.IntVar]] = {}
                for attempt_id in matching:
                    canonical = str(
                        self.attempt_by_id[attempt_id]["canonical_course_id"]
                    )
                    by_canonical.setdefault(canonical, []).append(used[attempt_id])
                maximum = int(constraint["maximum_attempts"])
                course_sats = [
                    self._reified_comparison(
                        sum(variables),
                        "AT_MOST",
                        maximum,
                        f"constraint[{constraint_id},{canonical}]",
                    )
                    for canonical, variables in sorted(by_canonical.items())
                ]
                sat = self.model.new_bool_var(f"constraint[{constraint_id}]")
                if course_sats:
                    self.model.add_bool_and(course_sats).only_enforce_if(sat)
                    self.model.add_bool_or([v.Not() for v in course_sats]).only_enforce_if(
                        sat.Not()
                    )
                else:
                    self.model.add(sat == 1)
            elif kind == "GROUP":
                sat = self._build_group_constraint(constraint_id, constraint, scope_ids)
            elif kind == "ORDERING":
                sat = self._build_ordering_constraint(constraint_id, constraint, scope_ids)
            else:
                sat = self.model.new_bool_var(f"constraint[{constraint_id}]")
                self.model.add(sat == 1)
            if active is not True:
                gated = self.model.new_bool_var(f"constraint_gated[{constraint_id}]")
                self.model.add(gated == 1).only_enforce_if(active.Not())
                self.model.add(gated == sat).only_enforce_if(active)
                sat = gated
            self.constraint_sat[constraint_id] = sat

    def _build_group_constraint(self, cid: str, constraint: Mapping[str, Any], scope_ids: Sequence[str]) -> cp_model.IntVar:
        used = self._used_attempt_vars(scope_ids)
        groups: Dict[Any, List[cp_model.IntVar]] = {}
        for attempt_id, var in used.items():
            value = field_value(constraint["field"], self.contexts[attempt_id], self.institution)
            if value is not None:
                groups.setdefault(value, []).append(var)
        group_vars = []
        for value, variables in groups.items():
            group = self.model.new_bool_var(f"group[{cid},{value}]")
            self.model.add_max_equality(group, variables)
            group_vars.append(group)
            if constraint["mode"] == "MAX_PER_GROUP":
                self.model.add(sum(variables) <= int(constraint["amount"]))
            elif constraint["mode"] == "MIN_PER_GROUP":
                self.model.add(sum(variables) >= int(constraint["amount"])).only_enforce_if(group)
        count = sum(group_vars)
        mode, amount = constraint["mode"], int(constraint["amount"])
        if mode == "DISTINCT_AT_LEAST":
            return self._reified_comparison(count, "AT_LEAST", amount, f"constraint[{cid}]")
        if mode == "DISTINCT_EXACTLY":
            return self._reified_comparison(count, "EXACTLY", amount, f"constraint[{cid}]")
        if mode == "SAME":
            return self._reified_comparison(count, "AT_MOST", 1, f"constraint[{cid}]")
        sat = self.model.new_bool_var(f"constraint[{cid}]")
        self.model.add(sat == 1)
        return sat

    def _term_key(self, value: Any) -> int:
        text = str(value)
        digits = "".join(ch for ch in text if ch.isdigit())
        year = int(digits[:4]) if len(digits) >= 4 else 0
        suffix = 0 if "SP" in text.upper() else 1 if "SU" in text.upper() else 2
        return year * 3 + suffix

    def _build_ordering_constraint(self, cid: str, constraint: Mapping[str, Any], scope_ids: Sequence[str]) -> cp_model.IntVar:
        used = self._used_attempt_vars(scope_ids)
        before = self._predicate_attempts(constraint["before"])
        after = self._predicate_attempts(constraint["after"])
        violations = []
        for left in before:
            for right in after:
                if self._term_key(self.attempt_by_id[left].get("term")) >= self._term_key(self.attempt_by_id[right].get("term")):
                    both = self.model.new_bool_var(f"ordering_violation[{cid},{left},{right}]")
                    self.model.add_bool_and([used[left], used[right]]).only_enforce_if(both)
                    self.model.add_bool_or([used[left].Not(), used[right].Not()]).only_enforce_if(both.Not())
                    violations.append(both)
        sat = self.model.new_bool_var(f"constraint[{cid}]")
        if violations:
            self.model.add(sum(violations) == 0).only_enforce_if(sat)
            self.model.add(sum(violations) >= 1).only_enforce_if(sat.Not())
        else:
            self.model.add(sat == 1)
        return sat

    def _build_forced(self) -> None:
        if self.forced is None:
            return
        var = self.x.get(self.forced)
        if var is None:
            self.model.add(0 == 1)
        else:
            self.model.add(var == 1)

    def _progress_terms(self) -> Tuple[List[Any], int]:
        lowers = [value for rid in self.root_ids for value in self.lower_values[rid] if value > 0]
        scale = math.lcm(*lowers) if lowers else 1
        terms = []
        for rid in self.root_ids:
            requirement = self.requirement_by_id[rid]
            obligation = self.obligation_override.get(rid, requirement["obligation"])
            if obligation != "REQUIRED":
                continue
            bound = requirement["lower"]
            override = self.bound_override.get((rid, "LOWER"))
            if override is not None or bound["kind"] == "FIXED":
                lower = int(override if override is not None else bound["value"])
                if lower <= 0:
                    continue
                progress = self.model.new_int_var(0, lower, f"progress[{rid}]")
                self.model.add_min_equality(progress, [self.q[rid], lower])
                unsat_progress = self.model.new_int_var(0, lower, f"unsat_progress[{rid}]")
                self.model.add(unsat_progress == 0).only_enforce_if(self.satisfied[rid])
                self.model.add(unsat_progress == progress).only_enforce_if(self.satisfied[rid].Not())
                terms.append((scale // lower) * unsat_progress)
            else:
                choice_id = str(bound["choice_id"])
                for key, raw in bound["cases"].items():
                    lower = int(raw)
                    if lower <= 0:
                        continue
                    choice = self.choice_vars.get((choice_id, self._coerce_case(choice_id, key)))
                    if choice is None:
                        if self.choice_values.get(choice_id) != self._coerce_case(choice_id, key):
                            continue
                        choice = True
                    progress = self.model.new_int_var(0, lower, f"progress[{rid},{key}]")
                    self.model.add(progress <= self.q[rid])
                    self.model.add(progress <= lower)
                    if choice is not True:
                        self.model.add(progress == 0).only_enforce_if(choice.Not())
                    self.model.add(progress == 0).only_enforce_if(self.satisfied[rid])
                    terms.append((scale // lower) * progress)
        return terms, scale

    def _solver(self) -> cp_model.CpSolver:
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = self.options.num_search_workers
        solver.parameters.random_seed = self.options.random_seed
        if self.options.max_time_seconds is not None:
            solver.parameters.max_time_in_seconds = self.options.max_time_seconds
        return solver

    def _solve_stage(self, solver: cp_model.CpSolver, stage: str) -> int:
        status = solver.solve(self.model)
        if status == cp_model.UNKNOWN:
            raise SolveTimeoutError(stage)
        return status

    def solve(self) -> _SolveValues:
        constraints = list(self.constraint_sat.values())
        solver = self._solver()
        if self.target is None:
            required_root_ids = [
                rid
                for rid in self.root_ids
                if self.obligation_override.get(
                    rid, self.requirement_by_id[rid]["obligation"]
                )
                == "REQUIRED"
            ]
            for rid in required_root_ids:
                # An inactive branch is outside the program completion test.
                self.model.add(self.satisfied[rid] + (1 - self.active[rid]) >= 1)
            for var in constraints:
                self.model.add(var == 1)
            complete_status = self._solve_stage(solver, "completion feasibility")
            if complete_status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
                active_root_count = sum(
                    int(solver.value(self.active[rid])) for rid in required_root_ids
                )
                fresh = _Builder(
                    self.auditor,
                    self.transcript,
                    self.profile,
                    forced=self.forced,
                    target=(active_root_count, len(constraints), 0),
                )
                completed = fresh._solve_partial()
                completed.complete = True
                return completed
            # Rebuild without completion constraints for the explanatory solve.
            fresh = _Builder(self.auditor, self.transcript, self.profile, forced=self.forced, target=(-1, -1, -1))
            return fresh._solve_partial()
        return self._solve_partial()

    def _solve_partial(self) -> _SolveValues:
        roots = [
            self.satisfied[rid]
            for rid in self.root_ids
            if self.obligation_override.get(rid, self.requirement_by_id[rid]["obligation"]) == "REQUIRED"
        ]
        constraints = list(self.constraint_sat.values())
        progress_terms, scale = self._progress_terms()
        root_sum, constraint_sum, progress_sum = sum(roots), sum(constraints), sum(progress_terms)
        solver = self._solver()
        if self.target is not None and self.target != (-1, -1, -1):
            target_roots, target_constraints, target_progress = self.target
            self.model.add(root_sum == target_roots)
            self.model.add(constraint_sum == target_constraints)
            self.model.add(progress_sum == target_progress)
        else:
            self.model.maximize(root_sum)
            status = self._solve_stage(solver, "required-root optimization")
            if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
                return _SolveValues(status, solver, False, 0, 0, 0, scale, 0)
            optimum = int(solver.value(root_sum))
            self.model.add(root_sum == optimum)
            self.model.maximize(constraint_sum)
            self._solve_stage(solver, "constraint optimization")
            constraint_optimum = int(solver.value(constraint_sum))
            self.model.add(constraint_sum == constraint_optimum)
            self.model.maximize(progress_sum)
            self._solve_stage(solver, "progress optimization")
            progress_optimum = int(solver.value(progress_sum))
            self.model.add(progress_sum == progress_optimum)
        manual_terms = [
            var for key, var in self.x.items() if self.approval_match.get(key, False)
        ]
        manual_constant = len(self.applied_effects)
        manual = sum(manual_terms) + manual_constant
        self.model.minimize(manual)
        status = self._solve_stage(solver, "approval minimization")
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return _SolveValues(status, solver, False, 0, 0, 0, scale, 0)
        return _SolveValues(
            status,
            solver,
            False,
            int(solver.value(root_sum)),
            int(solver.value(constraint_sum)),
            int(solver.value(progress_sum)),
            scale,
            int(solver.value(manual)),
        )

    def _manual_value(self, solver: cp_model.CpSolver) -> int:
        return sum(
            int(solver.value(var))
            for key, var in self.x.items()
            if self.approval_match.get(key, False)
        ) + len(self.applied_effects)


class _Indeterminate(Exception):
    pass


class _InvalidEvaluation(Exception):
    pass


class Auditor:
    """Compile one MQL semantic program into repeatable transcript audits."""

    def __init__(
        self,
        program_ir: JsonInput,
        institution: JsonInput,
        catalog: JsonInput,
        *,
        options: Optional[AuditOptions] = None,
    ) -> None:
        try:
            self.program = load_json(program_ir, "program_ir")
            self.institution = load_json(institution, "institution")
            self.catalog = load_json(catalog, "catalog")
        except ValueError as exc:
            raise InputValidationError(str(exc)) from exc
        self.options = options or AuditOptions()
        self._validate_static_inputs()

    def _validate_static_inputs(self) -> None:
        for label, document in (("program", self.program), ("catalog", self.catalog)):
            version = document.get("schema_version")
            if version != SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"{label} schema version {version!r} is unsupported; expected {SCHEMA_VERSION}"
                )
        try:
            pylibmql.validate_semantic_ir(json.dumps(self.program))
        except ValueError as exc:
            raise InputValidationError(f"invalid semantic IR: {exc}") from exc
        if self.program.get("credit_scale") != self.catalog.get("credit_scale"):
            raise InputValidationError("program and catalog credit scales differ")
        ref = self.program.get("institution_schema", {})
        if ref.get("id") != self.institution.get("schema_id") or ref.get("version") != self.institution.get("version"):
            raise InputValidationError("program institution reference does not match institution schema")
        course_ids = [c.get("canonical_course_id") for c in self.catalog.get("courses", [])]
        if len(course_ids) != len(set(course_ids)):
            raise InputValidationError("catalog contains duplicate canonical_course_id values")

    def audit(self, transcript: JsonInput, profile: JsonInput) -> AuditResult:
        try:
            transcript_obj = load_json(transcript, "transcript")
            profile_obj = load_json(profile, "profile")
        except ValueError as exc:
            raise InputValidationError(str(exc)) from exc
        self._validate_dynamic_inputs(transcript_obj, profile_obj)
        try:
            builder = _Builder(self, transcript_obj, profile_obj)
            solved = builder.solve()
        except _Indeterminate as exc:
            return self._non_solved_result("INDETERMINATE", f"missing declared choice {exc.args[0]}")
        except _InvalidEvaluation as exc:
            return self._non_solved_result("INVALID", str(exc))
        return self._result(builder, solved, transcript_obj, profile_obj)

    def _validate_dynamic_inputs(self, transcript: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
        for label, document in (("transcript", transcript), ("profile", profile)):
            if document.get("schema_version") != SCHEMA_VERSION:
                raise UnsupportedSchemaError(f"{label} must use schema version {SCHEMA_VERSION}")
        if transcript.get("credit_scale") != self.program.get("credit_scale"):
            raise InputValidationError("program and transcript credit scales differ")
        attempts = transcript.get("attempts")
        if not isinstance(attempts, list):
            raise InputValidationError("transcript.attempts must be an array")
        attempt_ids = [a.get("attempt_id") for a in attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise InputValidationError("transcript contains duplicate attempt_id values")
        known = {c["canonical_course_id"] for c in self.catalog.get("courses", [])}
        missing = sorted({a.get("canonical_course_id") for a in attempts} - known)
        if missing:
            raise InputValidationError(f"transcript references unknown courses: {missing}")

    def _non_solved_result(self, status: str, reason: str) -> AuditResult:
        requirements, _ = _flatten_requirements(self.program["requirements"])
        inactive = []
        for requirement in requirements:
            lower_values = self._bound_values(requirement["lower"])
            upper = requirement.get("upper")
            upper_values = self._bound_values(upper) if upper is not None else []
            inactive.append(
                RequirementResult(
                    _meta_id(requirement),
                    False,
                    0,
                    min(lower_values),
                    min(upper_values) if upper_values else None,
                    False,
                    Rational(0, 1),
                )
            )
        return AuditResult(
            status=status,
            program_complete=None,
            choices={},
            effects=[],
            assignments=[],
            rejections=[],
            requirements=inactive,
            totals=[],
            objective=ObjectiveValue(0, Rational(0, 1), 0),
            trace=[TraceEvent(1, self.program.get("program_id", "program"), status.lower(), reason)],
        )

    @staticmethod
    def _bound_values(bound: Mapping[str, Any]) -> List[int]:
        if bound["kind"] == "FIXED":
            return [int(bound["value"])]
        return [int(value) for value in bound["cases"].values()]

    def _result(
        self,
        builder: _Builder,
        solved: _SolveValues,
        transcript: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> AuditResult:
        solver = solved.solver
        assignments = [
            Assignment(attempt_id, requirement_id, self._applicable_constraints(builder, requirement_id))
            for (attempt_id, requirement_id), var in sorted(builder.x.items())
            if solver.value(var)
        ]
        assigned = {(a.attempt_id, a.requirement_id) for a in assignments}
        requirements = []
        for requirement in builder.requirements:
            rid = _meta_id(requirement)
            active = bool(solver.value(builder.active[rid]))
            q = int(solver.value(builder.q[rid]))
            lower = self._resolved_bound(builder, requirement["lower"], rid, "LOWER", solver)
            upper = None if requirement["upper"] is None else self._resolved_bound(builder, requirement["upper"], rid, "UPPER", solver)
            satisfied = bool(solver.value(builder.satisfied[rid]))
            progress = Fraction(min(q, lower), lower) if active and lower > 0 else Fraction(0, 1)
            requirements.append(RequirementResult(rid, active, q, lower, upper, satisfied, Rational.from_fraction(progress)))
        all_constraints = all(bool(solver.value(v)) for v in builder.constraint_sat.values())
        complete = all(
            (not result.active) or result.satisfied
            for result in requirements
            if result.requirement_id in builder.root_ids
            and builder.obligation_override.get(result.requirement_id, builder.requirement_by_id[result.requirement_id]["obligation"]) == "REQUIRED"
        ) and all_constraints
        status = "COMPLETE" if complete else "INCOMPLETE"
        choices = dict(builder.choice_values)
        for (choice_id, alternative), var in builder.choice_vars.items():
            if solver.value(var):
                choices[choice_id] = alternative
        totals = self._totals(builder, solver)
        rejections = self._rejections(builder, assigned)
        candidate = self._candidate_usefulness(builder, solved, transcript, profile, assigned)
        trace = self._trace(builder, requirements, totals)
        progress = Fraction(solved.progress_scaled, solved.progress_scale)
        return AuditResult(
            status=status,
            program_complete=complete,
            choices=choices,
            effects=list(builder.applied_effects),
            assignments=assignments,
            rejections=rejections,
            requirements=requirements,
            totals=totals,
            objective=ObjectiveValue(solved.satisfied_roots, Rational.from_fraction(progress), solved.manual_uses),
            trace=trace,
            candidate_usefulness=candidate,
        )

    def _resolved_bound(self, builder: _Builder, bound: Mapping[str, Any], rid: str, which: str, solver: cp_model.CpSolver) -> int:
        override = builder.bound_override.get((rid, which))
        if override is not None:
            return override
        if bound["kind"] == "FIXED":
            return int(bound["value"])
        choice_id = str(bound["choice_id"])
        value = builder.choice_values.get(choice_id)
        if value is None:
            value = next(alt for (cid, alt), var in builder.choice_vars.items() if cid == choice_id and solver.value(var))
        return int(bound["cases"][str(value)])

    def _applicable_constraints(self, builder: _Builder, requirement_id: str) -> List[str]:
        result = []
        for constraint in self.program.get("constraints", []):
            if requirement_id in builder._scope_ids(constraint["scope"]):
                result.append(_meta_id(constraint))
        return result

    def _totals(self, builder: _Builder, solver: cp_model.CpSolver) -> List[Dict[str, Any]]:
        result = []
        for cid, (variables, minimum, count) in builder.total_data.items():
            attempt_ids = [aid for aid, used in builder._used_attempt_vars(builder._scope_ids(next(c for c in self.program["constraints"] if _meta_id(c) == cid)["scope"])).items() if solver.value(used)]
            credit_units = sum(int(builder.attempt_by_id[aid].get("earned_credit_units", 0)) for aid in attempt_ids)
            if count == "CREDIT_UNITS":
                value = credit_units
            elif count == "DISTINCT_ATTEMPTS":
                value = len(attempt_ids)
            elif count == "DISTINCT_CANONICAL_COURSES":
                value = len({builder.attempt_by_id[aid]["canonical_course_id"] for aid in attempt_ids})
            else:
                value = sum(int(solver.value(var)) for var in variables)
            result.append({"constraint_id": cid, "distinct_attempts": sorted(attempt_ids), "credit_units": credit_units, "minimum": minimum, "satisfied": value >= minimum})
        return result

    def _rejections(self, builder: _Builder, assigned: Set[Tuple[str, str]]) -> List[Rejection]:
        result = []
        for attempt in builder.attempts:
            aid = str(attempt["attempt_id"])
            for requirement in builder.requirements:
                rid = _meta_id(requirement)
                if (aid, rid) in assigned or builder.eligible.get((aid, rid), False):
                    continue
                result.append(Rejection(aid, rid, rid, "The attempt does not satisfy the requirement selector."))
        return result

    def _candidate_usefulness(
        self,
        builder: _Builder,
        solved: _SolveValues,
        transcript: Mapping[str, Any],
        profile: Mapping[str, Any],
        assigned: Set[Tuple[str, str]],
    ) -> List[CandidateUsefulness]:
        result = []
        target = (solved.satisfied_roots, solved.constraint_score, solved.progress_scaled)
        for attempt in builder.attempts:
            aid = str(attempt["attempt_id"])
            for requirement in builder.requirements:
                rid = _meta_id(requirement)
                key = (aid, rid)
                if key in assigned:
                    result.append(CandidateUsefulness(aid, rid, "DIRECT", True, []))
                elif not builder.eligible.get(key, False):
                    result.append(CandidateUsefulness(aid, rid, "INELIGIBLE", False, [rid]))
                else:
                    try:
                        alternate = _Builder(self, transcript, profile, forced=key, target=target).solve()
                        feasible = (
                            alternate.status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
                            and alternate.manual_uses == solved.manual_uses
                        )
                    except SolveTimeoutError:
                        raise
                    blocking = [] if feasible else self._applicable_constraints(builder, rid)
                    result.append(CandidateUsefulness(aid, rid, "REASSIGNMENT" if feasible else "BLOCKED", feasible, blocking))
        return result

    def _trace(self, builder: _Builder, requirements: Sequence[RequirementResult], totals: Sequence[Mapping[str, Any]]) -> List[TraceEvent]:
        trace = []
        step = 1
        for effect in builder.applied_effects:
            trace.append(TraceEvent(step, effect["effect_id"], "applied", f"Effect applied to {effect['target_id']} with witness {effect['witness']}."))
            step += 1
        for requirement in requirements:
            result = "inactive" if not requirement.active else "satisfied" if requirement.satisfied else "incomplete"
            trace.append(TraceEvent(step, requirement.requirement_id, result, f"Contribution {requirement.q}; required lower bound {requirement.lower}."))
            step += 1
        for total in totals:
            trace.append(TraceEvent(step, str(total["constraint_id"]), "satisfied" if total["satisfied"] else "incomplete", f"Observed {total['credit_units']} credit units; minimum {total['minimum']}."))
            step += 1
        return trace or [TraceEvent(1, str(self.program["program_id"]), "evaluated", "Audit evaluated.")]


def audit_ir(
    program_ir: JsonInput,
    institution: JsonInput,
    catalog: JsonInput,
    transcript: JsonInput,
    profile: JsonInput,
    *,
    options: Optional[AuditOptions] = None,
) -> AuditResult:
    return Auditor(program_ir, institution, catalog, options=options).audit(transcript, profile)


def audit_source(
    source: str,
    source_path: str,
    institution: JsonInput,
    catalog: JsonInput,
    transcript: JsonInput,
    profile: JsonInput,
    *,
    options: Optional[AuditOptions] = None,
) -> AuditResult:
    institution_obj = load_json(institution, "institution")
    compiled = pylibmql.compile_program(source, source_path, json.dumps(institution_obj))
    return audit_ir(compiled.json(), institution_obj, catalog, transcript, profile, options=options)
