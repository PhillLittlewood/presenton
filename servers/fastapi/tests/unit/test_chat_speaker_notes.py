import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from llmai.shared import AssistantMessage, AssistantToolCall, UserMessage

from models.sql.slide import SlideModel
from services.chat import service as chat_service_module
from services.chat.memory_layer import PresentationChatMemoryLayer
from services.chat.prompts import build_system_prompt
from services.chat.schemas import SetSpeakerNoteInput
from services.chat.service import (
    UNBACKED_EDIT_NUDGE,
    PresentationChatService,
)
from services.chat.tools import ChatTools


class _Session:
    """Just enough of an AsyncSession for set_speaker_note."""

    def __init__(self, slides):
        self.slides = slides
        self.added = []
        self.commit_count = 0

    async def scalar(self, _statement):
        return self.slides[0] if self.slides else None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, _value):
        return None


def _slide(**overrides) -> SlideModel:
    values = dict(
        presentation=uuid.uuid4(),
        layout_group="general",
        layout="general:title",
        index=0,
        content={"title": "Pineapples"},
        speaker_note="",
    )
    values.update(overrides)
    return SlideModel(**values)


def _memory(session, presentation_type="standard"):
    return PresentationChatMemoryLayer(
        session,
        uuid.uuid4(),
        presentation_type=presentation_type,
    )


# ---------------------------------------------------------------- memory layer


def test_set_speaker_note_writes_only_the_note():
    slide = _slide()
    original_id = slide.id
    session = _Session([slide])

    result = asyncio.run(
        _memory(session).set_speaker_note(index=0, speaker_note="  Talk about bromelain.  ")
    )

    assert result["saved"] is True
    assert result["slide_number"] == 1
    assert slide.speaker_note == "Talk about bromelain."
    assert slide.content["__speaker_note__"] == "Talk about bromelain."
    assert slide.content["title"] == "Pineapples"
    assert slide.id == original_id
    assert session.commit_count == 1


def test_set_speaker_note_on_smart_slide_does_not_touch_content():
    slide = _slide(content={"title": "Pineapples"}, html_content="<section/>")
    session = _Session([slide])

    result = asyncio.run(
        _memory(session, "smart").set_speaker_note(index=0, speaker_note="Notes")
    )

    assert result["saved"] is True
    assert slide.speaker_note == "Notes"
    assert "__speaker_note__" not in slide.content


def test_set_speaker_note_reports_failure_when_slide_is_missing():
    session = _Session([])

    result = asyncio.run(_memory(session).set_speaker_note(index=4, speaker_note="Notes"))

    assert result["saved"] is False
    assert "index 4" in result["message"]
    assert session.commit_count == 0


def test_set_speaker_note_rejects_blank_note():
    slide = _slide(speaker_note="existing")
    session = _Session([slide])

    result = asyncio.run(_memory(session).set_speaker_note(index=0, speaker_note="   "))

    assert result["saved"] is False
    assert slide.speaker_note == "existing"
    assert session.commit_count == 0


def test_resolve_speaker_note_keeps_existing_when_content_has_none():
    resolve = PresentationChatMemoryLayer._resolve_speaker_note

    assert resolve({"title": "x"}, "existing note") == "existing note"
    assert resolve({"__speaker_note__": ""}, "existing note") == "existing note"
    assert resolve({"__speaker_note__": "new note"}, "existing note") == "new note"
    assert resolve({"title": "x"}, None) == ""


# ------------------------------------------------------------------ chat tools


@pytest.mark.parametrize("presentation_type", ["standard", "smart"])
def test_set_speaker_note_tool_is_exposed(presentation_type):
    memory = Mock()
    memory.presentation_type = presentation_type

    names = [tool.name for tool in ChatTools(memory).get_tool_definitions()]

    assert "setSpeakerNote" in names


def test_set_speaker_note_tool_delegates_to_memory():
    memory = Mock()
    memory.presentation_type = "standard"
    memory.set_speaker_note = AsyncMock(
        return_value={"saved": True, "message": "Speaker note saved for slide 13."}
    )

    result = asyncio.run(
        ChatTools(memory).execute_tool_call(
            AssistantToolCall(
                id="1",
                name="setSpeakerNote",
                arguments=json.dumps({"index": 12, "speakerNote": " Hello. "}),
            )
        )
    )

    assert result["ok"] is True
    memory.set_speaker_note.assert_awaited_once_with(index=12, speaker_note="Hello.")


def test_tool_that_saved_nothing_is_reported_as_failed():
    memory = Mock()
    memory.presentation_type = "standard"
    memory.set_speaker_note = AsyncMock(
        return_value={"saved": False, "message": "No slide found at index 99."}
    )

    result = asyncio.run(
        ChatTools(memory).execute_tool_call(
            AssistantToolCall(
                id="1",
                name="setSpeakerNote",
                arguments=json.dumps({"index": 99, "speakerNote": "Hello."}),
            )
        )
    )

    assert result["ok"] is False
    assert "No slide found at index 99" in result["error"]


def test_blank_note_is_rejected_by_schema():
    with pytest.raises(ValueError):
        SetSpeakerNoteInput(index=0, speakerNote="   ")


def test_prompts_tell_the_model_about_speaker_notes():
    assert "setSpeakerNote" in build_system_prompt("", "", "standard")
    assert "setSpeakerNote" in build_system_prompt("", "", "smart")


# ------------------------------------------------- service: sanitize + guard

sanitize = PresentationChatService._sanitize_tool_call_arguments


def test_sanitize_keeps_valid_arguments_untouched():
    arguments = '{"index": 0, "speakerNote": "ok"}'
    assert sanitize(arguments) == arguments


def test_sanitize_replaces_unparseable_arguments_with_valid_json():
    # Truncated mid-value, like the "Expecting value: ... char 280" failure.
    assert json.loads(sanitize('{"index": 0, "content": ')) == {}
    assert sanitize(None) == "{}"
    assert sanitize("   ") == "{}"
    assert sanitize("not json at all") == "{}"


def test_sanitize_repairs_loose_json():
    repaired = json.loads(sanitize("{index: 2, 'speakerNote': 'hi',}"))
    assert repaired == {"index": 2, "speakerNote": "hi"}


def test_echoed_tool_calls_always_contain_valid_json():
    broken = AssistantToolCall(id="1", name="saveSlide", arguments='{"content": {"a": ')

    messages = PresentationChatService._append_sanitized_assistant_tool_turn(
        [],
        content=None,
        tool_calls=[broken],
    )

    echoed = messages[-1].tool_calls[0]
    assert json.loads(echoed.arguments) == {}
    assert echoed.id == "1" and echoed.name == "saveSlide"
    # The original object (which is what gets executed) is not mutated.
    assert broken.arguments == '{"content": {"a": '


@pytest.mark.parametrize(
    "text",
    [
        "I have added professional speaking notes to Slide 13.",
        "I've created a new slide about pineapples.",
        "I updated the title.",
        "I successfully saved the notes.",
    ],
)
def test_claims_of_a_change_are_detected(text):
    assert PresentationChatService._claims_unperformed_edit(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Which slide would you like the notes on?",
        "Slide 3 covers pricing and the roadmap.",
        "I could not do that because the slide does not exist.",
        "",
        None,
    ],
)
def test_ordinary_replies_are_not_flagged(text):
    assert PresentationChatService._claims_unperformed_edit(text) is False


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def generate(self, **kwargs):
        self.requests.append(kwargs["messages"])
        return self._responses.pop(0)


def _service_with(client, tools):
    service = PresentationChatService.__new__(PresentationChatService)
    service._tools = tools
    return service


def _patch_llm(monkeypatch, client):
    monkeypatch.setattr(chat_service_module, "get_client", lambda config=None: client)
    monkeypatch.setattr(chat_service_module, "get_llm_config", lambda: None)
    monkeypatch.setattr(chat_service_module, "get_model", lambda: "test-model")
    monkeypatch.setattr(chat_service_module, "build_chat_llm_tools", lambda _t: [])
    monkeypatch.setattr(
        chat_service_module,
        "get_generate_kwargs",
        lambda **kwargs: {"messages": kwargs["messages"]},
    )


def test_reply_that_claims_an_edit_without_a_tool_call_is_pushed_back(monkeypatch):
    note_call = AssistantToolCall(
        id="call-1",
        name="setSpeakerNote",
        arguments=json.dumps({"index": 12, "speakerNote": "Pineapples contain bromelain."}),
    )
    client = _FakeClient(
        [
            SimpleNamespace(
                content="I have added speaker notes to Slide 13.", tool_calls=[]
            ),
            SimpleNamespace(content=None, tool_calls=[note_call]),
            SimpleNamespace(content="Speaker notes saved on slide 13.", tool_calls=[]),
        ]
    )
    _patch_llm(monkeypatch, client)
    tools = Mock()
    tools.get_tool_definitions = Mock(return_value=[])
    tools.slides_missing_speaker_notes = Mock(return_value=[])
    tools.execute_tool_call = AsyncMock(
        return_value={"ok": True, "tool": "setSpeakerNote", "result": {"saved": True}}
    )
    service = _service_with(client, tools)

    text, called = asyncio.run(
        service._run_llm_with_tools([UserMessage(content="Add speaker notes")])
    )

    assert called == ["setSpeakerNote"]
    assert text == "Speaker notes saved on slide 13."
    tools.execute_tool_call.assert_awaited_once()
    second_request = client.requests[1]
    assert isinstance(second_request[-2], AssistantMessage)
    assert UNBACKED_EDIT_NUDGE in json.dumps(
        [m.model_dump(mode="json") for m in second_request[-1:]]
    )


def test_guard_pushes_back_only_once(monkeypatch):
    client = _FakeClient(
        [
            SimpleNamespace(content="I have added the notes.", tool_calls=[]),
            SimpleNamespace(content="I have added the notes, really.", tool_calls=[]),
        ]
    )
    _patch_llm(monkeypatch, client)
    tools = Mock()
    tools.get_tool_definitions = Mock(return_value=[])
    tools.slides_missing_speaker_notes = Mock(return_value=[])
    tools.execute_tool_call = AsyncMock()
    service = _service_with(client, tools)

    text, called = asyncio.run(
        service._run_llm_with_tools([UserMessage(content="Add speaker notes")])
    )

    assert called == []
    assert text == "I have added the notes, really."
    assert len(client.requests) == 2
    tools.execute_tool_call.assert_not_awaited()


# ------------------------------------------ new slides must get speaker notes


def _tools_with_memory(**async_methods):
    memory = Mock()
    memory.presentation_type = "standard"
    for name, value in async_methods.items():
        setattr(memory, name, AsyncMock(return_value=value))
    return ChatTools(memory), memory


def _call(tools, name, **arguments):
    return asyncio.run(
        tools.execute_tool_call(
            AssistantToolCall(id="c", name=name, arguments=json.dumps(arguments))
        )
    )


def test_new_slide_without_note_is_tracked_until_a_note_is_set():
    tools, memory = _tools_with_memory(
        save_slide={
            "saved": True,
            "action": "created",
            "slide_id": "slide-a",
            "index": 12,
            "has_speaker_note": False,
            "speaker_note_missing": True,
            "next_step": "Call setSpeakerNote with index 12",
        },
        set_speaker_note={
            "saved": True,
            "action": "speaker_note_set",
            "slide_id": "slide-a",
            "index": 12,
        },
    )

    created = _call(
        tools,
        "addNewSlideLayout",
        content="{}",
        layoutId="general:title",
        index=12,
        speakerNote=None,
    )
    assert created["ok"] is True
    assert created["result"]["speaker_note_missing"] is True
    assert tools.slides_missing_speaker_notes() == [12]

    _call(tools, "setSpeakerNote", index=12, speakerNote="Talk about pineapples.")
    assert tools.slides_missing_speaker_notes() == []


def test_new_slide_created_with_a_note_is_not_tracked():
    tools, memory = _tools_with_memory(
        save_slide={
            "saved": True,
            "action": "created",
            "slide_id": "slide-b",
            "index": 3,
            "has_speaker_note": True,
        }
    )

    _call(
        tools,
        "addNewSlideLayout",
        content="{}",
        layoutId="general:title",
        index=3,
        speakerNote="Already has a note.",
    )

    assert tools.slides_missing_speaker_notes() == []
    assert memory.save_slide.await_args.kwargs["speaker_note"] == "Already has a note."


def test_tracking_resets_every_turn():
    tools, _ = _tools_with_memory(
        save_slide={
            "saved": True,
            "action": "created",
            "slide_id": "slide-c",
            "index": 1,
            "speaker_note_missing": True,
        }
    )
    _call(tools, "saveSlide", content="{}", layoutId="x", index=1,
          replaceOldSlideAtIndex=False)
    assert tools.slides_missing_speaker_notes() == [1]

    tools.set_turn_context("a new request")
    assert tools.slides_missing_speaker_notes() == []


def test_model_is_asked_for_notes_before_finishing_a_new_slide(monkeypatch):
    save_call = AssistantToolCall(
        id="call-1",
        name="saveSlide",
        arguments=json.dumps({"index": 12}),
    )
    note_call = AssistantToolCall(
        id="call-2",
        name="setSpeakerNote",
        arguments=json.dumps({"index": 12, "speakerNote": "Pineapples aid digestion."}),
    )
    client = _FakeClient(
        [
            SimpleNamespace(content=None, tool_calls=[save_call]),
            SimpleNamespace(content="Created the pineapple slide.", tool_calls=[]),
            SimpleNamespace(content=None, tool_calls=[note_call]),
            SimpleNamespace(content="Created the slide with speaker notes.", tool_calls=[]),
        ]
    )
    _patch_llm(monkeypatch, client)

    pending = {"value": []}
    tools = Mock()
    tools.get_tool_definitions = Mock(return_value=[])
    tools.slides_missing_speaker_notes = Mock(side_effect=lambda: list(pending["value"]))

    async def execute(tool_call):
        if tool_call.name == "saveSlide":
            pending["value"] = [12]
        else:
            pending["value"] = []
        return {"ok": True, "tool": tool_call.name, "result": {"saved": True}}

    tools.execute_tool_call = AsyncMock(side_effect=execute)
    service = _service_with(client, tools)

    text, called = asyncio.run(
        service._run_llm_with_tools([UserMessage(content="Create a pineapple slide")])
    )

    assert called == ["saveSlide", "setSpeakerNote"]
    assert text == "Created the slide with speaker notes."
    nudge = json.dumps([m.model_dump(mode="json") for m in client.requests[2][-1:]])
    assert "Slide 13" in nudge and "setSpeakerNote" in nudge


def test_notes_nudge_is_sent_only_once(monkeypatch):
    save_call = AssistantToolCall(id="call-1", name="saveSlide", arguments="{}")
    client = _FakeClient(
        [
            SimpleNamespace(content=None, tool_calls=[save_call]),
            SimpleNamespace(content="Done.", tool_calls=[]),
            SimpleNamespace(content="Done, no notes.", tool_calls=[]),
        ]
    )
    _patch_llm(monkeypatch, client)
    tools = Mock()
    tools.get_tool_definitions = Mock(return_value=[])
    tools.slides_missing_speaker_notes = Mock(return_value=[2])
    tools.execute_tool_call = AsyncMock(
        return_value={"ok": True, "tool": "saveSlide", "result": {"saved": True}}
    )
    service = _service_with(client, tools)

    text, _ = asyncio.run(
        service._run_llm_with_tools([UserMessage(content="Create a slide")])
    )

    assert text == "Done, no notes."
    assert len(client.requests) == 3
