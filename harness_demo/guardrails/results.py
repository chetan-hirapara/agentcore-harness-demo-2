"""Shapes returned by gates.

A gate returns a plain dict because it is serialised straight back to the
model as the tool result. Two keys are always present: `blocked` and,
when blocked, `reason`. `fix` is structured remediation so the agent can
self-correct instead of guessing.
"""
from pydantic import ValidationError


def blocked_from_validation(reason: str, e: ValidationError) -> dict:
    return {"blocked": True, "reason": reason,
            "fix": [{"field": list(err["loc"]), "problem": err["msg"]}
                    for err in e.errors()]}
