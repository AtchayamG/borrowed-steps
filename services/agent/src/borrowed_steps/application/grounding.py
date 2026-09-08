"""Deterministic grounding of extracted candidates against the source text.

Standard library only, and no model involved. Everything a model proposes has to
survive these checks before it can appear in a draft:

* a value must equal the evidence substring the extractor claimed for it, and
  that substring must actually occur in the intake text;
* an equipment kind needs one unambiguous lexical match in the text;
* a due date must be a complete timezone-aware ISO-8601 timestamp that also
  passes the existing M1 window rule.

Anything absent, unsupported or unverifiable becomes ``None`` so a human is
asked. This is a lexical check that bounds unsupported output. It is not
evidence of understanding, and a person still reviews every draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from borrowed_steps.application.interpreter import DraftRequest
from borrowed_steps.domain.errors import ValidationFailedError
from borrowed_steps.domain.models import (
    EquipmentKind,
)
from borrowed_steps.domain.rules import validate_due_at
from borrowed_steps.isotime import normalise

__all__ = [
    "FIELD_ORDER",
    "REASONS",
    "GroundingResult",
    "ground_due_at",
    "ground_equipment_kind",
    "ground_text_field",
    "kinds_present",
    "missing_fields",
]

FIELD_ORDER: tuple[str, ...] = (
    "borrower_label",
    "equipment_kind",
    "pickup_location",
    "due_at",
)

# Why a field ended up as it did. These are safe to log: they name the rule that
# fired, never the intake text, the candidate value or any model reasoning.
ACCEPTED = "accepted"
ABSENT_CANDIDATE = "absent_candidate"
ABSENT_EVIDENCE = "absent_evidence"
EVIDENCE_NOT_IN_SOURCE = "evidence_not_in_source"
VALUE_EVIDENCE_MISMATCH = "value_evidence_mismatch"
LENGTH_REJECTED = "length_rejected"
PARSE_FAILURE = "parse_failure"
WINDOW_FAILURE = "window_failure"
NO_KIND_IN_SOURCE = "no_kind_in_source"
AMBIGUOUS_KINDS = "ambiguous_kinds"
KIND_MISMATCH = "kind_mismatch"

REASONS: tuple[str, ...] = (
    ACCEPTED,
    ABSENT_CANDIDATE,
    ABSENT_EVIDENCE,
    EVIDENCE_NOT_IN_SOURCE,
    VALUE_EVIDENCE_MISMATCH,
    LENGTH_REJECTED,
    PARSE_FAILURE,
    WINDOW_FAILURE,
    NO_KIND_IN_SOURCE,
    AMBIGUOUS_KINDS,
    KIND_MISMATCH,
)


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """A grounded value, plus the reason code explaining that outcome."""

    value: object | None
    reason: str

    @property
    def accepted(self) -> bool:
        return self.reason == ACCEPTED


_KIND_PATTERN = re.compile(
    r"\b(wheelchairs?|walking\s+frames?|walkers?|crutch(?:es)?)\b",
    re.IGNORECASE,
)

_KIND_BY_STEM: tuple[tuple[str, EquipmentKind], ...] = (
    ("wheelchair", EquipmentKind.WHEELCHAIR),
    ("walking frame", EquipmentKind.WALKER),
    ("walker", EquipmentKind.WALKER),
    ("crutch", EquipmentKind.CRUTCHES),
)


def _kind_of(term: str) -> EquipmentKind | None:
    collapsed = re.sub(r"\s+", " ", term.strip().lower())
    for stem, kind in _KIND_BY_STEM:
        if collapsed.startswith(stem):
            return kind
    return None


def kinds_present(text: str) -> set[EquipmentKind]:
    """Every equipment kind the text names outright."""
    found: set[EquipmentKind] = set()
    for match in _KIND_PATTERN.finditer(text):
        kind = _kind_of(match.group(0))
        if kind is not None:
            found.add(kind)
    return found


def ground_text_field(
    candidate: str | None, evidence: str | None, text: str, max_length: int
) -> GroundingResult:
    """Accept a free-text field only when it is a verified quote from the text.

    Only surrounding whitespace is trimmed; the value itself is never rewritten.
    """
    if candidate is None:
        return GroundingResult(None, ABSENT_CANDIDATE)
    if evidence is None:
        return GroundingResult(None, ABSENT_EVIDENCE)
    if evidence not in text:
        return GroundingResult(None, EVIDENCE_NOT_IN_SOURCE)
    if candidate != evidence:
        return GroundingResult(None, VALUE_EVIDENCE_MISMATCH)
    trimmed = candidate.strip()
    if not 1 <= len(trimmed) <= max_length:
        return GroundingResult(None, LENGTH_REJECTED)
    return GroundingResult(trimmed, ACCEPTED)


def ground_equipment_kind(
    candidate: EquipmentKind | None, evidence: str | None, text: str
) -> GroundingResult:
    """Accept a kind only when the text names exactly one and the extractor agrees."""
    present = kinds_present(text)
    if not present:
        return GroundingResult(None, NO_KIND_IN_SOURCE)
    if len(present) > 1:
        return GroundingResult(None, AMBIGUOUS_KINDS)
    (only,) = present
    if candidate is None:
        return GroundingResult(None, ABSENT_CANDIDATE)
    if candidate is not only:
        return GroundingResult(None, KIND_MISMATCH)
    if evidence is None:
        return GroundingResult(None, ABSENT_EVIDENCE)
    if evidence.lower() not in text.lower():
        return GroundingResult(None, EVIDENCE_NOT_IN_SOURCE)
    if _kind_of_first_term(evidence) is not only:
        return GroundingResult(None, KIND_MISMATCH)
    return GroundingResult(only, ACCEPTED)


def _kind_of_first_term(evidence: str) -> EquipmentKind | None:
    match = _KIND_PATTERN.search(evidence)
    return None if match is None else _kind_of(match.group(0))


def ground_due_at(
    candidate: str | None, evidence: str | None, text: str, now: datetime
) -> GroundingResult:
    """Accept a due date only from a complete timezone-aware ISO-8601 quote.

    A relative phrase, a bare date or a timestamp with no offset all yield
    ``None``, so the human supplies the date instead of the model guessing one.
    """
    if candidate is None:
        return GroundingResult(None, ABSENT_CANDIDATE)
    if evidence is None:
        return GroundingResult(None, ABSENT_EVIDENCE)
    if evidence not in text:
        return GroundingResult(None, EVIDENCE_NOT_IN_SOURCE)
    if candidate != evidence:
        return GroundingResult(None, VALUE_EVIDENCE_MISMATCH)
    try:
        parsed = normalise(datetime.fromisoformat(candidate.strip()))
    except ValueError:
        return GroundingResult(None, PARSE_FAILURE)
    try:
        validate_due_at(parsed, now)
    except ValidationFailedError:
        return GroundingResult(None, WINDOW_FAILURE)
    return GroundingResult(parsed, ACCEPTED)


def missing_fields(draft: DraftRequest) -> tuple[str, ...]:
    """The null fields of ``draft``, in the order the contract fixes."""
    values = (
        draft.borrower_label,
        draft.equipment_kind,
        draft.pickup_location,
        draft.due_at,
    )
    return tuple(name for name, value in zip(FIELD_ORDER, values, strict=True) if value is None)
