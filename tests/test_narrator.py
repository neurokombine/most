"""Рассказчик: stream-json → человеческие сообщения, разрезанные под лимит."""
import json

from bridge.narrator import chunk, to_messages, assistant_texts, TELEGRAM_LIMIT, MAX_LIMIT


def ev(obj):
    return obj


SAMPLE = [
    {"type": "system", "subtype": "init", "session_id": "abc"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Смотрю остатки."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Готово."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Остатки посчитаны."},
]


def test_assistant_texts_are_picked_in_order():
    assert assistant_texts(SAMPLE) == ["Смотрю остатки.", "Готово."]


def test_final_answer_is_the_result_line():
    messages = to_messages(SAMPLE, limit=TELEGRAM_LIMIT)
    assert messages
    assert "Остатки посчитаны." in messages[-1]


def test_broken_json_lines_are_skipped_not_fatal():
    from bridge.narrator import parse_stream
    raw = "\n".join([
        json.dumps(SAMPLE[0], ensure_ascii=False),
        "{это не json",
        "",
        json.dumps(SAMPLE[-1], ensure_ascii=False),
    ])
    events = parse_stream(raw)
    assert len(events) == 2


def test_error_result_is_told_humanly():
    events = [{"type": "result", "subtype": "error_during_execution",
               "is_error": True, "result": "boom"}]
    messages = to_messages(events, limit=TELEGRAM_LIMIT)
    assert messages
    assert "boom" in messages[-1] or "не получилось" in messages[-1].lower()


def test_empty_stream_still_says_something():
    messages = to_messages([], limit=TELEGRAM_LIMIT)
    assert messages and messages[0].strip()


def test_chunk_keeps_short_text_whole():
    assert chunk("короткий ответ", 4096) == ["короткий ответ"]


def test_chunk_never_exceeds_limit():
    text = "\n".join(f"строка номер {i} " + "х" * 50 for i in range(400))
    parts = chunk(text, TELEGRAM_LIMIT)
    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)


def test_chunk_splits_on_line_boundaries_when_it_can():
    text = "\n".join("строка " + "я" * 100 for _ in range(60))
    parts = chunk(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_chunk_hard_splits_a_single_endless_line():
    text = "ф" * 9000
    parts = chunk(text, 4096)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == text


def test_max_limit_is_smaller_than_telegram():
    assert MAX_LIMIT == 4000
    assert TELEGRAM_LIMIT == 4096
    parts = chunk("я" * 4050, MAX_LIMIT)
    assert all(len(p) <= MAX_LIMIT for p in parts)
    assert len(parts) == 2


# --- этап 2: что нейросеть успела сказать до остановки -----------------------

def test_partial_text_is_assembled_from_live_deltas(narrator=None):
    from bridge import narrator
    events = [
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "Считаю "}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "август"}}},
    ]
    assert narrator.partial_text(events) == "Считаю август"


def test_partial_text_prefers_the_longer_of_delta_and_finished_reply():
    from bridge import narrator
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Начала."}]}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta",
                                                     "text": "Начала. И посчитала август."}}},
    ]
    assert "август" in narrator.partial_text(events)


def test_partial_text_of_an_empty_stream_is_empty():
    from bridge import narrator
    assert narrator.partial_text([]) == ""


def test_cost_is_taken_from_the_result_event():
    """«Сколько стоило» лежит только в событии result — больше нигде."""
    from bridge import narrator
    events = [{"type": "assistant", "message": {"content": []}},
              {"type": "result", "subtype": "success", "result": "Готово.",
               "total_cost_usd": 0.0731}]
    assert narrator.cost_of(events) == 0.0731


def test_cost_of_a_stream_without_a_price_is_unknown():
    from bridge import narrator
    assert narrator.cost_of([{"type": "result", "result": "Готово."}]) is None
    assert narrator.cost_of([]) is None
    assert narrator.cost_of([{"type": "result", "total_cost_usd": "дорого"}]) is None
