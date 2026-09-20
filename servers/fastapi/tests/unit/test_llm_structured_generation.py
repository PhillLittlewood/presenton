import asyncio
import itertools

import pytest
from fastapi import HTTPException
from llmai.shared import (
    AssistantMessage,
    ResponseStreamCompletionChunk,
    ResponseStreamContentChunk,
    SystemMessage,
    UserMessage,
)

from utils import llm_utils
from utils.llm_utils import (
    _generate_structured_content,
    extract_structured_content,
    structured_validation_feedback_messages,
)

GOOD = '{"slides": [0, 1, 2]}'


class FakeClient:
    """Stands in for an llmai client; `script` yields the stream events."""

    def __init__(self, script):
        self.script = script
        self.emitted = 0
        self.closed = False

    def generate(self, **_kwargs):
        def events():
            try:
                for event in self.script():
                    self.emitted += 1
                    yield event
            finally:
                self.closed = True

        return events()


def content(text):
    return ResponseStreamContentChunk(chunk=text)


def run(client, **env):
    return asyncio.run(
        _generate_structured_content(
            client, disconnect_checker=None, model="m", messages=[UserMessage(content="hi")]
        )
    )


def test_normal_reply_is_returned_as_before():
    client = FakeClient(
        lambda: [content(GOOD), ResponseStreamCompletionChunk(content={"slides": [0, 1, 2]})]
    )
    assert run(client) == {"slides": [0, 1, 2]}


def test_reply_wrapped_in_fences_is_recovered_when_the_client_rejects_it():
    def script():
        yield content("```json\n" + GOOD + "\n```")
        raise RuntimeError("500: Expecting value: line 1 column 1 (char 0)")

    assert run(FakeClient(script)) == {"slides": [0, 1, 2]}


def test_think_block_and_prose_are_recovered_from_streamed_text():
    def script():
        yield content("<think>the user wants {layouts}</think>\nHere it is: " + GOOD)
        yield ResponseStreamCompletionChunk(content=None)

    assert run(FakeClient(script)) == {"slides": [0, 1, 2]}


def test_model_that_never_stops_after_its_json_is_cut_off_and_the_json_used():
    def script():
        yield content(GOOD)
        for _ in itertools.islice(itertools.repeat("\n"), 200_000):
            yield content("\n")

    client = FakeClient(script)
    assert run(client) == {"slides": [0, 1, 2]}
    assert client.emitted < 5_000  # stopped early rather than draining the stream
    assert client.closed


def test_endless_whitespace_without_any_json_becomes_a_clear_error():
    def script():
        for _ in range(200_000):
            yield content(" ")

    client = FakeClient(script)
    with pytest.raises(HTTPException) as error:
        run(client)
    assert error.value.status_code == 400
    assert "never finished" in error.value.detail
    assert client.emitted < 10_000


def test_runaway_reply_is_stopped_at_the_configured_limit(monkeypatch):
    monkeypatch.setenv("LLM_STRUCTURED_MAX_CHARS", "500")

    def script():
        yield content('{"slides": [')
        for _ in range(100_000):
            yield content("1, ")

    client = FakeClient(script)
    with pytest.raises(HTTPException) as error:
        run(client)
    assert "exceeded 500 characters" in error.value.detail
    assert client.emitted < 1_000


def test_time_limit_stops_a_generation_that_never_ends(monkeypatch):
    monkeypatch.setenv("LLM_STRUCTURED_MAX_SECONDS", "1")

    def script():
        import time

        yield content('{"slides": [')
        for _ in range(1_000):
            time.sleep(0.01)
            yield content("1,")

    with pytest.raises(HTTPException) as error:
        run(FakeClient(script))
    assert "longer than 1 seconds" in error.value.detail


def test_lenient_extraction_helper_handles_wrapped_json():
    assert extract_structured_content("```json\n" + GOOD + "\n```") == {"slides": [0, 1, 2]}
    assert extract_structured_content("nothing to see") is None


def test_validation_feedback_keeps_roles_alternating():
    messages = [SystemMessage(content="sys"), UserMessage(content="task")]
    messages.extend(structured_validation_feedback_messages({"slides": [1]}, ["too short"]))
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert isinstance(messages[2], AssistantMessage)
    assert "too short" in messages[3].content


def test_layout_structure_call_does_not_rerun_generation_for_validation(monkeypatch):
    from models.presentation_layout import PresentationLayoutModel
    from models.presentation_outline_model import PresentationOutlineModel, SlideOutlineModel
    from utils.llm_calls import generate_presentation_structure as module

    seen = {}

    async def fake_retries(*_args, **kwargs):
        seen.update(kwargs)
        return {"slides": [0, 0]}

    monkeypatch.setattr(module, "generate_structured_with_schema_retries", fake_retries)
    monkeypatch.setattr(module, "get_client", lambda **_: object())
    monkeypatch.setattr(module, "get_llm_config", lambda **_: None)
    monkeypatch.setattr(module, "get_model", lambda: "m")
    monkeypatch.setattr(module, "get_messages", lambda *a, **k: [])
    layout = PresentationLayoutModel(name="t", slides=[])
    outline = PresentationOutlineModel(
        slides=[SlideOutlineModel(content="a"), SlideOutlineModel(content="b")]
    )
    asyncio.run(module.generate_presentation_structure(outline, layout))
    assert seen["validate_schema_max_loop_count"] == 1
