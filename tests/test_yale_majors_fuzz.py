from __future__ import annotations

import hashlib
import json
import os
import random
import re
from pathlib import Path
from typing import Any, Iterator

import pylibmql
import pytest

from mql_audit import Auditor

HERE = Path(__file__).parent
COURSES_PATH = HERE / "courses_2627.json"
SAMPLES_PER_PROGRAM = 24
LANGUAGES = {
    "ARBC": "Arabic",
    "CHNS": "Chinese",
    "FREN": "French",
    "GMAN": "German",
    "GREK": "Greek",
    "HEBR": "Hebrew",
    "ITAL": "Italian",
    "JAPN": "Japanese",
    "KREN": "Korean",
    "LATN": "Latin",
    "PORT": "Portuguese",
    "RUSS": "Russian",
    "SPAN": "Spanish",
}


def _majors_root() -> Path:
    configured = os.environ.get("MQL_MAJORS_ROOT")
    candidates = [
        Path(configured) if configured else None,
        HERE.parents[1] / "majors" / "majors" / "2627",
        HERE.parents[1] / "majors" / "2627",
    ]
    for candidate in candidates:
        if candidate and (candidate / "yale-institution.json").is_file():
            return candidate
    pytest.skip("set MQL_MAJORS_ROOT to the majoraudit/majors majors/2627 directory")


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _format(course: dict[str, Any], flags: set[str], subject: str, number: int) -> str:
    text = " ".join(
        str(course.get(key) or "") for key in ("title", "description", "classnotes", "regnotes")
    ).lower()
    flag_text = " ".join(flags).lower()
    if "senior project" in text or "senior project" in flag_text:
        return "senior_project"
    if "independent study" in text or "directed study" in text:
        return "independent_study"
    if "research" in course.get("title", "").lower():
        return "research"
    if "studio" in text or "studio" in flag_text:
        return "studio"
    if (
        "laboratory" in text
        or " lab" in flag_text
        or str(number).endswith("5")
        and subject in {"CHEM", "PHYS"}
    ):
        return "lab"
    if course.get("colsem") or course.get("fysem") or course.get("sysem") or "seminar" in flag_text:
        return "seminar"
    if any(skill.startswith("L") for skill in course.get("skills") or []):
        return "language"
    if "tutorial" in text:
        return "tutorial"
    if "colloquium" in text:
        return "colloquium"
    return "lecture"


def _language(subject: str, flags: set[str]) -> str:
    taught = next((flag for flag in flags if " Taught in " in flag), "")
    if taught:
        return taught.rsplit(" Taught in ", 1)[1]
    return LANGUAGES.get(subject, "English")


@pytest.fixture(scope="session")
def yale_catalog() -> dict[str, Any]:
    exported = json.loads(COURSES_PATH.read_text())["data"]["courses"]
    merged: dict[str, dict[str, Any]] = {}
    for offering in exported:
        flags = {item["flag"]["flag_text"] for item in offering.get("course_flags", [])}
        distributionals = set(offering.get("areas") or []) | set(offering.get("skills") or [])
        for listing in offering.get("listings", []):
            subject = listing["subject"]
            number_text = listing["number"]
            match = re.search(r"\d+", number_text)
            if match is None:
                continue
            number = int(match.group())
            canonical_id = f"{subject}{number_text}".replace(" ", "")
            record = merged.setdefault(
                canonical_id,
                {
                    "canonical_course_id": canonical_id,
                    "subject": subject,
                    "number": number,
                    "code": canonical_id,
                    "credits": 0,
                    "attributes": set(),
                    "distributionals": set(),
                    "format": "lecture",
                    "language": "English",
                },
            )
            record["credits"] = max(
                record["credits"], round(float(offering.get("credits") or 0) * 2)
            )
            record["attributes"].update(flags)
            record["distributionals"].update(distributionals)
            inferred_format = _format(offering, flags, subject, number)
            if record["format"] == "lecture" or inferred_format != "lecture":
                record["format"] = inferred_format
            record["language"] = _language(subject, flags)
    courses = []
    for record in merged.values():
        record["attributes"] = sorted(record["attributes"])
        record["distributionals"] = sorted(record["distributionals"])
        courses.append(record)
    return {"schema_version": "0.2.0", "credit_scale": 2, "courses": courses}


@pytest.fixture(scope="session")
def institution() -> dict[str, Any]:
    return json.loads((_majors_root() / "yale-institution.json").read_text())


def _program_paths() -> list[Path]:
    return sorted(_majors_root().glob("*/*.mql"))


def _requirement_ids(program: dict[str, Any]) -> set[str]:
    return {
        str(node["meta"]["id"])
        for requirement in program["requirements"]
        for node in _walk(requirement)
        if "meta" in node and "obligation" in node
    }


def _course_pool(program: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    literals = {
        value for node in _walk(program) for value in node.values() if isinstance(value, str)
    }
    for node in _walk(program):
        for value in node.values():
            if isinstance(value, list):
                literals.update(item for item in value if isinstance(item, str))

    def is_relevant(course: dict[str, Any]) -> bool:
        return bool(
            {course["canonical_course_id"], course["code"], course["subject"]} & literals
            or set(course["attributes"]) & literals
            or set(course["distributionals"]) & literals
            or course["format"] in literals
            or course["language"] in literals
        )

    relevant = [course for course in catalog["courses"] if is_relevant(course)]
    remainder = [course for course in catalog["courses"] if not is_relevant(course)]
    return relevant + remainder


def _profile(program: dict[str, Any], sample: int, rng: random.Random) -> dict[str, Any]:
    choices = {
        str(choice["meta"]["id"]): rng.choice(choice["alternatives"])
        for choice in program.get("choices", [])
        if choice["owner"] == "DECLARED"
    }
    facts = []
    for index, fact in enumerate(program.get("facts", [])):
        if sample % 6 == 1 or (sample % 6 > 1 and (sample + index) % 2 == 0):
            facts.append({"fact_type": fact["fact_type"], "parameters": fact.get("parameters", {})})
    return {
        "schema_version": "0.2.0",
        "class_year": rng.choice([2026, 2027, 2028, 2029]),
        "declared_choices": choices,
        "facts": facts,
    }


def _transcript(
    program: dict[str, Any], pool: list[dict[str, Any]], sample: int, rng: random.Random
) -> dict[str, Any]:
    # Grow from an empty record to realistic major-sized transcripts while varying every attempt field.
    count = [0, 1, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32][sample % 14]
    selected = rng.sample(pool[: min(len(pool), max(200, count * 8))], k=count)
    approval_tokens = sorted(
        {
            value
            for node in _walk(program)
            if isinstance(node.get("field"), dict) and node["field"].get("field") == "approved_for"
            for value in (
                node.get("value") if isinstance(node.get("value"), list) else [node.get("value")]
            )
            if isinstance(value, str)
        }
    )
    approval_pool = approval_tokens or [
        "major",
        "electives",
        "senior",
        "outside_elective",
        "research_assistant",
    ]
    attempts = []
    for index, course in enumerate(selected):
        mode = rng.choice(["letter", "letter", "letter", "crdf", "pass_fail"])
        attempts.append(
            {
                "attempt_id": f"fuzz-{sample}-{index}",
                "canonical_course_id": course["canonical_course_id"],
                "earned_credit_units": course["credits"],
                "grade": rng.choice(["C", "C+", "B", "B+", "A-", "A"])
                if mode == "letter"
                else None,
                "grading_mode": mode,
                "graduate_grade": None,
                "source": rng.choice(
                    ["yale", "yale", "yale", "summer", "study_abroad", "transfer"]
                ),
                "term": rng.choice(["2024FA", "2025SP", "2025FA", "2026SP", "2026FA", "2027SP"]),
                "year_of_study": rng.randint(1, 4),
                "enrolled_subject": course["subject"],
                "approved_for": rng.sample(approval_pool, k=rng.choice([0, 0, 0, 1])),
            }
        )
    return {"schema_version": "0.2.0", "credit_scale": 2, "attempts": attempts}


@pytest.mark.slow
@pytest.mark.parametrize("program_path", _program_paths(), ids=lambda path: path.stem)
def test_yale_major_fuzz(
    program_path: Path,
    institution: dict[str, Any],
    yale_catalog: dict[str, Any],
) -> None:
    compiled = pylibmql.compile_program(
        program_path.read_text(), str(program_path), json.dumps(institution)
    )
    program = json.loads(compiled.json())
    auditor = Auditor(program, institution, yale_catalog)
    requirements = _requirement_ids(program)
    pool = _course_pool(program, yale_catalog)
    seed = int.from_bytes(hashlib.sha256(program_path.name.encode()).digest()[:8], "big")
    rng = random.Random(seed)

    for sample in range(SAMPLES_PER_PROGRAM):
        transcript = _transcript(program, pool, sample, rng)
        result = auditor.audit(transcript, _profile(program, sample, rng))
        attempt_ids = {attempt["attempt_id"] for attempt in transcript["attempts"]}

        assert result.status in {"COMPLETE", "INCOMPLETE"}
        assert result.program_complete == (result.status == "COMPLETE")
        assert all(item.attempt_id in attempt_ids for item in result.assignments)
        assert all(item.requirement_id in requirements for item in result.assignments)
        assert len({(item.attempt_id, item.requirement_id) for item in result.assignments}) == len(
            result.assignments
        )
        assert all(item.q >= 0 and item.lower >= 0 for item in result.requirements)
        assert len(result.candidate_usefulness) == len(attempt_ids) * len(result.requirements)
        assert json.loads(result.json())["status"] == result.status
