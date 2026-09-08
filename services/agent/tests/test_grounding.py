"""Grounding rules: what a model proposes only counts if the text supports it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from borrowed_steps.application.grounding import (
    ACCEPTED,
    AMBIGUOUS_KINDS,
    PARSE_FAILURE,
    WINDOW_FAILURE,
    ground_due_at,
    ground_equipment_kind,
    ground_text_field,
    kinds_present,
    missing_fields,
)
from borrowed_steps.application.interpreter import DraftRequest
from borrowed_steps.domain.models import EquipmentKind

NOW = datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC)
LABEL_MAX = 60
LOCATION_MAX = 120


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("needs a wheelchair", {EquipmentKind.WHEELCHAIR}),
        ("two Wheelchairs please", {EquipmentKind.WHEELCHAIR}),
        ("a walker", {EquipmentKind.WALKER}),
        ("a walking frame", {EquipmentKind.WALKER}),
        ("walking  frames", {EquipmentKind.WALKER}),
        ("one crutch", {EquipmentKind.CRUTCHES}),
        ("CRUTCHES", {EquipmentKind.CRUTCHES}),
        ("nothing relevant here", set()),
        ("a wheelchair or crutches", {EquipmentKind.WHEELCHAIR, EquipmentKind.CRUTCHES}),
    ],
)
def test_kinds_present(text: str, expected: set[EquipmentKind]) -> None:
    assert kinds_present(text) == expected


def test_kind_accepted_when_text_names_exactly_one() -> None:
    text = "Please hold a wheelchair for Monday."
    outcome = ground_equipment_kind(EquipmentKind.WHEELCHAIR, "wheelchair", text)
    assert outcome.value is EquipmentKind.WHEELCHAIR
    assert outcome.reason == ACCEPTED


def test_ambiguous_kinds_yield_none() -> None:
    text = "a wheelchair or maybe crutches"
    outcome = ground_equipment_kind(EquipmentKind.WHEELCHAIR, "wheelchair", text)
    assert outcome.value is None
    assert outcome.reason == AMBIGUOUS_KINDS


@pytest.mark.parametrize(
    ("candidate", "evidence"),
    [
        (None, "wheelchair"),
        (EquipmentKind.WALKER, "wheelchair"),
        (EquipmentKind.WHEELCHAIR, None),
        (EquipmentKind.WHEELCHAIR, "walker"),
        (EquipmentKind.WHEELCHAIR, "scooter"),
    ],
)
def test_kind_rejected_without_matching_evidence(
    candidate: EquipmentKind | None, evidence: str | None
) -> None:
    assert ground_equipment_kind(candidate, evidence, "Please hold a wheelchair.").value is None


def test_text_field_must_quote_the_source() -> None:
    text = "Borrower Meena R needs help."
    assert ground_text_field("Meena R", "Meena R", text, LABEL_MAX).value == "Meena R"


@pytest.mark.parametrize(
    ("candidate", "evidence"),
    [
        (None, "Meena R"),
        ("Meena R", None),
        ("Meena Raman", "Meena Raman"),  # not present in the text
        ("Meena Raman", "Meena R"),  # value does not equal its evidence
        ("", ""),  # empty after trimming
    ],
)
def test_text_field_rejects_unsupported_values(candidate: str | None, evidence: str | None) -> None:
    assert (
        ground_text_field(candidate, evidence, "Borrower Meena R needs help.", LABEL_MAX).value
        is None
    )


def test_text_field_enforces_m1_length_limits() -> None:
    long_label = "x" * 61
    text = f"Borrower {long_label} needs help."
    assert ground_text_field(long_label, long_label, text, LABEL_MAX).value is None
    assert ground_text_field(long_label, long_label, text, LOCATION_MAX).value == long_label


def test_due_at_accepts_a_complete_offset_timestamp() -> None:
    text = "Return by 2026-09-14T09:00:00Z please."
    grounded = ground_due_at("2026-09-14T09:00:00Z", "2026-09-14T09:00:00Z", text, NOW)
    assert grounded.value == NOW + timedelta(days=7)
    assert grounded.reason == ACCEPTED


def test_due_at_normalises_a_non_utc_offset() -> None:
    text = "Return by 2026-09-14T14:30:00+05:30 please."
    grounded = ground_due_at("2026-09-14T14:30:00+05:30", "2026-09-14T14:30:00+05:30", text, NOW)
    assert grounded.value == datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "candidate",
    [
        "next Tuesday",
        "2026-09-14",
        "2026-09-14T09:00:00",
        "2026-09-06T09:00:00Z",
        "2026-11-30T09:00:00Z",
        "tomorrow",
    ],
)
def test_due_at_rejects_relative_partial_and_out_of_window(candidate: str) -> None:
    text = f"Return by {candidate} please."
    outcome = ground_due_at(candidate, candidate, text, NOW)
    assert outcome.value is None
    assert outcome.reason in {PARSE_FAILURE, WINDOW_FAILURE}


def test_due_at_requires_the_quote_to_be_in_the_text() -> None:
    assert (
        ground_due_at("2026-09-14T09:00:00Z", "2026-09-14T09:00:00Z", "no date here", NOW).value
        is None
    )


def test_missing_fields_uses_the_contract_order() -> None:
    empty = DraftRequest(
        borrower_label=None, equipment_kind=None, pickup_location=None, due_at=None
    )
    assert missing_fields(empty) == (
        "borrower_label",
        "equipment_kind",
        "pickup_location",
        "due_at",
    )

    partial = DraftRequest(
        borrower_label="Meena R",
        equipment_kind=EquipmentKind.WHEELCHAIR,
        pickup_location=None,
        due_at=None,
    )
    assert missing_fields(partial) == ("pickup_location", "due_at")

    complete = DraftRequest(
        borrower_label="Meena R",
        equipment_kind=EquipmentKind.WHEELCHAIR,
        pickup_location="Velachery",
        due_at=NOW + timedelta(days=3),
    )
    assert missing_fields(complete) == ()
