"""What stage two actually puts on the wire: the schema in the prompt, source only.

BS-003-R11 adds the generated JSON schema to the stage-two system prompt, as
Ollama's structured-output guidance asks, while the request's ``format``
parameter keeps carrying the same schema. These tests read the **outgoing
request** rather than the module constant, because the constant agreeing with
itself would not show what the model is sent.

Offline only. The transport is the instrumented fake already used by the
cancellation tests; no model runs and no socket is opened.
"""

from __future__ import annotations

import asyncio
import json
from threading import Event
from typing import Any, ClassVar

import ollama
import pytest

from borrowed_steps.config import DEFAULT_ASSISTANT_HOST, DEFAULT_ASSISTANT_MODEL
from borrowed_steps.infrastructure.strands_interpreter import (
    _EXTRACTION_SCHEMA_HEADING,
    _EXTRACTION_SYSTEM_PROMPT,
    _EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA,
    _EXTRACTION_USER_PROMPT,
    StrandsOllamaInterpreter,
    _Extraction,
    _extraction_system_prompt,
)
from test_cancellation import ScriptedClient
from test_strands_adapter import TEXT, StubClock, StubInventory


class RecordingClient(ScriptedClient):
    """The existing two-stage fake, plus a copy of every outgoing request."""

    requests: ClassVar[list[dict[str, Any]]] = []

    async def chat(self, **request: object) -> object:
        RecordingClient.requests.append(dict(request))
        return await super().chat(**request)


@pytest.fixture(autouse=True)
def _fake_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    ScriptedClient.created = []
    ScriptedClient.block_on = set()
    ScriptedClient.close_error = None
    ScriptedClient.close_error_times = 1
    RecordingClient.requests = []
    monkeypatch.setattr(ollama, "AsyncClient", RecordingClient)


def _interpreter() -> StrandsOllamaInterpreter:
    return StrandsOllamaInterpreter(
        host=DEFAULT_ASSISTANT_HOST,
        model_id=DEFAULT_ASSISTANT_MODEL,
        clock=StubClock(),
        deadline_seconds=30.0,
    )


def _stage_two_request() -> dict[str, Any]:
    """Run the real interpretation and return the one non-streaming request."""

    async def main() -> None:
        await _interpreter().interpret(TEXT, StubInventory(), Event())

    asyncio.run(main())

    schema_requests = [r for r in RecordingClient.requests if r.get("stream") is False]
    assert len(schema_requests) == 1, "stage two is exactly one non-streaming request"
    return schema_requests[0]


def _role_content(request: dict[str, Any], role: str) -> list[str]:
    messages = request["messages"]
    assert isinstance(messages, list)
    found: list[str] = []
    for message in messages:
        if message.get("role") != role:
            continue
        content = message.get("content")
        assert isinstance(content, str), f"{role} content is not a plain string"
        found.append(content)
    return found


def test_the_outgoing_system_prompt_carries_the_generated_schema() -> None:
    """The schema the decoder is given is also written out in the prompt."""
    request = _stage_two_request()

    schema = request["format"]
    assert schema == _Extraction.model_json_schema(), "format is the generated schema"

    system_prompts = _role_content(request, "system")
    assert len(system_prompts) == 1, "one system message"
    system_prompt = system_prompts[0]

    # Serialised from the very dict that travels as `format`, so this cannot
    # pass against a second, separately written copy of the schema.
    assert json.dumps(schema, separators=(",", ":")) in system_prompt
    assert _EXTRACTION_SCHEMA_HEADING in system_prompt


def test_the_schema_block_in_the_prompt_parses_and_names_every_required_key() -> None:
    """The prompt's schema section is real JSON, and it is the schema sent.

    Asserting the key names appear *somewhere* in the prompt would not show
    this: the worked example in the approved rules already mentions all four.
    So the section after the heading is parsed on its own.
    """
    request = _stage_two_request()
    system_prompt = _role_content(request, "system")[0]

    heading, separator, block = system_prompt.partition(_EXTRACTION_SCHEMA_HEADING)
    assert separator, "the schema heading is missing from the outgoing prompt"
    assert heading, "the rules come before the schema"

    embedded = json.loads(block)
    assert embedded == request["format"], "the prompt's schema is the one sent as format"

    required = embedded["required"]
    assert set(required) == {"borrower_label", "equipment_kind", "pickup_location", "due_at"}
    for key in required:
        assert key in embedded["properties"], f"{key} is required but not described"


def test_the_semantic_rules_are_still_the_start_of_the_prompt() -> None:
    """The schema is added to the approved rules, not in place of them."""
    request = _stage_two_request()
    system_prompt = _role_content(request, "system")[0]

    assert system_prompt.startswith(_EXTRACTION_SYSTEM_PROMPT), (
        "the approved extraction rules must still open the system prompt"
    )


def test_the_user_message_is_still_the_source_wrapper_alone() -> None:
    """Nothing from stage one may reach stage two's user message."""
    request = _stage_two_request()

    user_messages = _role_content(request, "user")
    assert user_messages == [_EXTRACTION_USER_PROMPT.format(text=TEXT)]

    only = user_messages[0]
    # The agent conversation, its tool and its inventory result all exist in
    # this run; none of them may have leaked into the source-only message.
    for leak in ("read_inventory", "AVAILABLE", "checked", "inventory", "schema"):
        assert leak not in only, f"{leak!r} reached the source-only user message"


def test_the_request_still_pins_the_native_schema_parameters() -> None:
    """Adding the schema to the prompt does not change how it is enforced."""
    request = _stage_two_request()

    assert request["stream"] is False
    assert request["format"] == _Extraction.model_json_schema()
    assert request["options"]["temperature"] == 0.0
    assert request["model"] == DEFAULT_ASSISTANT_MODEL
    assert "tools" not in request or not request["tools"], "stage two carries no tools"


def test_there_is_one_schema_and_it_is_generated() -> None:
    """No second, hand-maintained copy: one call, serialised once."""
    assert _extraction_system_prompt() == _EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA

    schema = json.dumps(_Extraction.model_json_schema(), separators=(",", ":"))
    assert _EXTRACTION_SYSTEM_PROMPT_WITH_SCHEMA.count(schema) == 1
