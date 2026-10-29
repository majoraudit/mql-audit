# mql-audit

`mql-audit` is the reference CP-SAT degree-audit engine for MQL 0.2 semantic IR.
It consumes a compiled MQL program, institution schema, course catalog, transcript,
and student profile and returns a deterministic audit with assignments, progress,
effects, explanations, and transcript-course usefulness.

## Install

```bash
python -m pip install mql-audit
```

Python 3.9 or newer is required.

## Use

```python
from mql_audit import audit_source

result = audit_source(
    source=open("computer_science_ba.mql").read(),
    source_path="computer_science_ba.mql",
    institution=open("yale-institution.json").read(),
    catalog=open("catalog.json").read(),
    transcript=open("transcript.json").read(),
    profile=open("profile.json").read(),
)

print(result.status)
print(result.json(indent=2))
```

For repeated audits of one program, compile once:

```python
from mql_audit import Auditor

auditor = Auditor(program_ir, institution, catalog)
result = auditor.audit(transcript, profile)
```

## Semantics

The engine implements MQL RFC 0001 and semantic IR schema `0.2.0`. Degree
completion is a feasibility result. For incomplete transcripts, CP-SAT selects an
explanatory witness by maximizing satisfied required roots, maximizing exact
normalized progress, and preferring course evidence over manual approvals.

Each audit also reports whether every existing transcript attempt is directly
used, can count after an objective-preserving reassignment, is blocked, or is
ineligible for each requirement.

## Scope

This release audits completed transcript attempts. It does not synthesize future
courses or perform multi-term planning.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy src/mql_audit
python -m build
```

## License

MIT
