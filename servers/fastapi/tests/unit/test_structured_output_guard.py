from utils.structured_output_guard import (
    JsonCompletionWatcher,
    MAX_CHARS_BEFORE_JSON,
    MAX_WHITESPACE_RUN,
    TRAILING_CHARS_AFTER_JSON,
    extract_json_object,
)

GOOD = '{"slides": [0, 1, {"note": "a } brace { in a string"}]}'


def feed(*chunks: str) -> JsonCompletionWatcher:
    watcher = JsonCompletionWatcher()
    for chunk in chunks:
        watcher.feed(chunk)
    return watcher


def test_json_completion_is_detected_across_tiny_chunks():
    watcher = feed(*GOOD)
    assert watcher.json_complete and watcher.chars_after_json == 0
    # A brace inside a string must not end the object early.
    assert not feed(*GOOD[:-3]).json_complete


def test_reasoning_blocks_are_ignored_even_when_tags_are_split():
    watcher = feed("<thi", "nk>weigh {options} ", "carefully</th", "ink>", *GOOD)
    assert watcher.json_complete
    # Reasoning text is excluded (a partial tag split across chunks may be
    # counted before it is recognised, hence the small tolerance).
    assert len(GOOD) <= watcher.visible_chars < len(GOOD) + 10
    assert not feed("<think>still thinking about {").json_complete


def test_a_model_that_keeps_writing_after_the_json_is_flagged():
    assert not feed(GOOD, "\n```").keeps_writing_after_json
    assert feed(GOOD, " " * (TRAILING_CHARS_AFTER_JSON + 1)).keeps_writing_after_json


def test_degenerate_output_is_reported():
    assert "whitespace" in feed("{" + " " * (MAX_WHITESPACE_RUN + 5)).degenerate_reason
    assert "no JSON" in feed("word " * (MAX_CHARS_BEFORE_JSON // 4)).degenerate_reason
    assert feed(GOOD).degenerate_reason is None


def test_extract_json_object_handles_common_wrappers():
    expected = {"slides": [0, 1, {"note": "a } brace { in a string"}]}
    for text in (
        GOOD,
        f"```json\n{GOOD}\n```",
        f"<think>maybe {{this}} one</think>\n{GOOD}",
        f"Sure! Here you go:\n{GOOD}\nHope that helps.",
        f"Okay.\n```json\n{GOOD}\n```\nDone",
    ):
        assert extract_json_object(text) == expected
    assert extract_json_object('{"slides": [1, 2') is None
    assert extract_json_object("no json at all") is None
    assert extract_json_object("") is None
