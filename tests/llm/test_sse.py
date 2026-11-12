"""SSE decoder tests: incremental decoding, framing, UTF-8 boundaries."""

from __future__ import annotations

from deeppyro.llm.sse import SseDecoder, decode_sse_text, format_sse, iter_sse


def test_decodes_single_event() -> None:
    events = decode_sse_text("data: hello\n\n")
    assert [event.data for event in events] == ["hello"]
    assert events[0].event is None
    assert events[0].id is None


def test_ignores_comments_and_blank_lines() -> None:
    document = ": keep-alive\n\ndata: one\n\ndata: two\n\n"
    assert [event.data for event in decode_sse_text(document)] == ["one", "two"]


def test_multiline_data_is_joined_with_newlines() -> None:
    events = decode_sse_text("data: line1\ndata: line2\n\n")
    assert events[0].data == "line1\nline2"


def test_event_and_id_fields_are_surfaced() -> None:
    event = decode_sse_text("event: delta\nid: 42\ndata: {}\n\n")[0]
    assert event.event == "delta"
    assert event.id == "42"
    assert event.data == "{}"


def test_crlf_terminators() -> None:
    document = "data: a\r\n\r\ndata: b\r\n\r\n"
    assert [event.data for event in decode_sse_text(document)] == ["a", "b"]


def test_value_whitespace_is_stripped_once() -> None:
    events = decode_sse_text("data:nospace\n\ndata:  two-spaces\n\n")
    assert [event.data for event in events] == ["nospace", " two-spaces"]


def test_incremental_feed_across_chunk_boundary() -> None:
    decoder = SseDecoder()
    assert decoder.feed("data: hel") == []
    events = decoder.feed("lo\n\n")
    assert [event.data for event in events] == ["hello"]


def test_incremental_feed_bytes_multibyte_split() -> None:
    payload = "data: 你好\n\n".encode()
    # split the UTF-8 sequence of the first Chinese character across two reads
    cut = payload.index("你".encode()) + 1
    decoder = SseDecoder()
    assert decoder.feed(payload[:cut]) == []
    events = decoder.feed(payload[cut:])
    assert [event.data for event in events] == ["你好"]


def test_flush_dispatches_event_without_trailing_blank_line() -> None:
    decoder = SseDecoder()
    assert decoder.feed("data: tail") == []
    events = decoder.flush()
    assert [event.data for event in events] == ["tail"]


def test_flush_is_idempotent_for_empty_decoder() -> None:
    decoder = SseDecoder()
    assert decoder.flush() == []
    assert decoder.flush() == []


async def test_iter_sse_from_byte_chunks() -> None:
    chunks = [b"data: he", b"llo\n\n", b"data: [DONE]\n\n"]

    async def source():
        for chunk in chunks:
            yield chunk

    events = [event.data async for event in iter_sse(source())]
    assert events == ["hello", "[DONE]"]


def test_format_sse_round_trip() -> None:
    document = format_sse(["a", '{"k": 1}'])
    assert document == 'data: a\n\ndata: {"k": 1}\n\n'
    assert [event.data for event in decode_sse_text(document)] == ["a", '{"k": 1}']
