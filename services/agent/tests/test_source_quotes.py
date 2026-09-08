"""R13 adapter boundaries; model/tool stubs only, no live inference."""

import asyncio
from datetime import UTC, datetime
from threading import Event

import pytest
from pydantic import ValidationError

from borrowed_steps.infrastructure.strands_interpreter import _Extraction
from test_strands_adapter import TEXT, StubInventory, _interpreter, install_stub_agent


@pytest.mark.parametrize(
    ("text", "quote", "expected"),
    [
        ("Leela needs a walking frame.", "walking frame", "WALKER"),
        ("Kavin needs crutches.", "crutches", "CRUTCHES"),
        ("Meena needs WHEELCHAIRS.", "WHEELCHAIRS", "WHEELCHAIR"),
        ("Meena needs a wheelchair.", "WHEELCHAIR", None),
        ("Meena needs a wheelchair.", "walker", None),
        ("Meena needs a wheelchair, not a medical bed.", "medical bed", None),
        ("A walker or crutches for Ravi.", "walker", None),
        ("A walker or crutches for Ravi.", "walker or crutches", None),
        ("Kavin needs crutches.", None, None),
    ],
)
def test_model_quote_is_required_and_whole_input_ambiguity_still_wins(
    monkeypatch: pytest.MonkeyPatch, text: str, quote: str | None, expected: str | None
) -> None:
    install_stub_agent(
        monkeypatch,
        structured=_Extraction(
            borrower_label=None, equipment_kind=quote, pickup_location=None, due_at=None
        ),
    )
    result = asyncio.run(_interpreter().interpret(text, StubInventory(), Event()))
    assert result.draft.equipment_kind == expected
    assert result.inventory_tool_calls == 1


def test_all_null_candidates_stay_null_even_with_every_field_in_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stub_agent(
        monkeypatch,
        structured=_Extraction(
            borrower_label=None, equipment_kind=None, pickup_location=None, due_at=None
        ),
    )
    result = asyncio.run(_interpreter().interpret(TEXT, StubInventory(), Event()))
    assert result.missing_fields == (
        "borrower_label",
        "equipment_kind",
        "pickup_location",
        "due_at",
    )


@pytest.mark.parametrize("quoted", ["2026-09-14T14:30:00+05:30", "2026-09-14T09:00:00Z", None])
def test_offset_normalization_requires_the_actual_model_returned_source_quote(
    monkeypatch: pytest.MonkeyPatch, quoted: str | None
) -> None:
    text = "Kavin needs crutches by 2026-09-14T14:30:00+05:30."
    install_stub_agent(
        monkeypatch,
        structured=_Extraction(
            borrower_label="Kavin", equipment_kind="crutches", pickup_location=None, due_at=quoted
        ),
    )
    result = asyncio.run(_interpreter().interpret(text, StubInventory(), Event()))
    expected = (
        datetime(2026, 9, 14, 9, 0, tzinfo=UTC) if quoted == "2026-09-14T14:30:00+05:30" else None
    )
    assert result.draft.due_at == expected


@pytest.mark.parametrize("key", ["borrower_label", "equipment_kind", "pickup_location", "due_at"])
def test_omitting_any_required_source_key_is_malformed(key: str) -> None:
    payload = dict.fromkeys(("borrower_label", "equipment_kind", "pickup_location", "due_at"))
    del payload[key]
    with pytest.raises(ValidationError):
        _Extraction.model_validate(payload)
