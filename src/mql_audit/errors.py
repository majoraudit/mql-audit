class MqlAuditError(Exception):
    """Base exception for mql-audit."""


class InputValidationError(MqlAuditError, ValueError):
    """An input document is malformed or incompatible with the program."""


class UnsupportedSchemaError(InputValidationError):
    """An input uses a schema version this release cannot evaluate."""


class EvaluationError(MqlAuditError):
    """The program cannot be evaluated for the supplied profile."""


class SolveTimeoutError(MqlAuditError, TimeoutError):
    """CP-SAT did not prove the requested result before its time limit."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"CP-SAT timed out during {stage!r}; no audit result was returned")
        self.stage = stage
