import json

import pytest
from openai.types import Model
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.models import ChatRequest, ChatResult
from app.openai_compat import (
    STREAM_DONE,
    Stream,
    UnsupportedRequest,
    completion_body,
    error_body,
    models_body,
    stream_chunk,
    translate_request,
)
from app.thresholds import DEFAULT_MAX_TOKENS, MAX_MAX_TOKENS

USER = {"role": "user", "content": "Hello"}


@pytest.fixture(autouse=True)
def settings(use_settings):
    return use_settings()


def body(**fields) -> dict:
    return {"model": "bootcamp", "messages": [USER], **fields}


def chat_result(**overrides) -> ChatResult:
    fields = {
        "text": "Hi there!",
        "finish_reason": "stop",
        "model": "claude-haiku-5-5",
        "input_tokens": 12,
        "output_tokens": 5,
    }
    return ChatResult(**{**fields, **overrides})


def chat_request(**overrides) -> ChatRequest:
    return translate_request(body(**overrides))


def parse_sse(text: str) -> list[dict | str]:
    """Split SSE text into its events, JSON-decoded except for the [DONE] marker."""
    events = []
    for line in text.split("\n\n"):
        if not line:
            continue
        assert line.startswith("data: ")
        payload = line.removeprefix("data: ")
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


# translate_request: the simplest request


def test_minimal_request_gets_defaults():
    request = translate_request(body())

    assert request == ChatRequest(
        requested_model="bootcamp",
        model="claude-haiku-5-5",
        system=None,
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=DEFAULT_MAX_TOKENS,
        stream=False,
        ignored_params=[],
    )


# translate_request: model


@pytest.mark.parametrize("requested", ["bootcamp", "claude-haiku-5-5"])
def test_alias_and_real_id_both_resolve_to_the_generation_model(requested):
    request = translate_request(body(model=requested))

    assert request.requested_model == requested
    assert request.model == "claude-haiku-5-5"


def test_alias_follows_settings(use_settings):
    use_settings(model_alias="class", generation_model="claude-next")

    request = translate_request(body(model="class"))

    assert (request.requested_model, request.model) == ("class", "claude-next")
    with pytest.raises(UnsupportedRequest):
        translate_request(body(model="bootcamp"))


@pytest.mark.parametrize("requested", ["gpt-4o", "claude-opus-5-5", "Bootcamp"])
def test_unknown_model_is_a_404(requested):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(model=requested))

    assert raised.value.status == 404
    assert raised.value.code == "model_not_found"
    assert raised.value.param == "model"
    assert requested in raised.value.message


@pytest.mark.parametrize("requested", [None, "", 7])
def test_missing_model_is_a_400(requested):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request({"model": requested, "messages": [USER]})

    assert raised.value.status == 400
    assert raised.value.param == "model"


# translate_request: messages


@pytest.mark.parametrize(
    ("messages", "system", "turns"),
    [
        pytest.param(
            [{"role": "system", "content": "Be brief."}, USER],
            "Be brief.",
            [{"role": "user", "content": "Hello"}],
            id="system message becomes system",
        ),
        pytest.param(
            [{"role": "developer", "content": "Be brief."}, USER],
            "Be brief.",
            [{"role": "user", "content": "Hello"}],
            id="developer role counts as system",
        ),
        pytest.param(
            [
                {"role": "system", "content": "Be brief."},
                USER,
                {"role": "developer", "content": "Use plain words."},
            ],
            "Be brief.\n\nUse plain words.",
            [{"role": "user", "content": "Hello"}],
            id="system messages anywhere join with a blank line",
        ),
        pytest.param(
            [{"role": "system", "content": ""}, USER],
            None,
            [{"role": "user", "content": "Hello"}],
            id="empty system message is skipped",
        ),
        pytest.param(
            [
                USER,
                {"role": "assistant", "content": "Hi! How can I help?"},
                {"role": "user", "content": "What is RAG?"},
            ],
            None,
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi! How can I help?"},
                {"role": "user", "content": "What is RAG?"},
            ],
            id="user and assistant map one to one",
        ),
        pytest.param(
            [USER, {"role": "user", "content": "Are you there?"}],
            None,
            [{"role": "user", "content": "Hello\n\nAre you there?"}],
            id="consecutive user messages merge",
        ),
        pytest.param(
            [
                USER,
                {"role": "assistant", "content": "One."},
                {"role": "assistant", "content": "Two."},
                {"role": "user", "content": "Three?"},
            ],
            None,
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "One.\n\nTwo."},
                {"role": "user", "content": "Three?"},
            ],
            id="consecutive assistant messages merge",
        ),
        pytest.param(
            [USER, {"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}],
            "Be brief.",
            [{"role": "user", "content": "Hello\n\nHi"}],
            id="users separated only by a system message merge",
        ),
        pytest.param(
            [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            None,
            [{"role": "user", "content": "Hello"}],
            id="content as a list with one text part",
        ),
        pytest.param(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Line one"},
                        {"type": "text", "text": "Line two"},
                    ],
                }
            ],
            None,
            [{"role": "user", "content": "Line one\nLine two"}],
            id="several text parts join with a newline",
        ),
        pytest.param(
            [{"role": "system", "content": [{"type": "text", "text": "Be brief."}]}, USER],
            "Be brief.",
            [{"role": "user", "content": "Hello"}],
            id="system content as a list of parts",
        ),
    ],
)
def test_messages_translate(messages, system, turns):
    request = translate_request(body(messages=messages))

    assert request.system == system
    assert request.messages == turns


@pytest.mark.parametrize(
    ("messages", "param"),
    [
        pytest.param(None, "messages", id="missing"),
        pytest.param([], "messages", id="empty list"),
        pytest.param("Hello", "messages", id="not a list"),
        pytest.param(["Hello"], "messages[0]", id="message is not an object"),
        pytest.param([{"content": "Hello"}], "messages[0].role", id="no role"),
        pytest.param([{"role": "narrator", "content": "Hi"}], "messages[0].role", id="bad role"),
        pytest.param([{"role": "user"}], "messages[0].content", id="no content"),
        pytest.param([{"role": "user", "content": 42}], "messages[0].content", id="bad content"),
        pytest.param([{"role": "user", "content": "  "}], "messages[0].content", id="blank"),
        pytest.param(
            [{"role": "user", "content": [{"type": "text"}]}],
            "messages[0].content",
            id="text part without text",
        ),
        pytest.param(
            [{"role": "user", "content": ["Hello"]}], "messages[0].content", id="part not an object"
        ),
        pytest.param(
            [USER, {"role": "assistant", "content": None}],
            "messages[1].content",
            id="assistant with null content",
        ),
        pytest.param([{"role": "system", "content": "Be brief."}], "messages", id="only system"),
        pytest.param(
            [{"role": "assistant", "content": "Hi"}, USER], "messages", id="starts with assistant"
        ),
        pytest.param(
            [USER, {"role": "assistant", "content": "The answer is"}],
            "messages",
            id="ends with assistant (prefill)",
        ),
    ],
)
def test_malformed_messages_are_a_400(messages, param):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request({"model": "bootcamp", "messages": messages})

    assert raised.value.status == 400
    assert raised.value.param == param


def test_body_that_is_not_an_object_is_a_400():
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(["not", "a", "dict"])

    assert raised.value.status == 400


# translate_request: unsupported features


@pytest.mark.parametrize(
    ("fields", "param"),
    [
        ({"tools": [{"type": "function", "function": {"name": "lookup"}}]}, "tools"),
        ({"tool_choice": "auto"}, "tool_choice"),
        ({"functions": [{"name": "lookup"}]}, "functions"),
        ({"function_call": "auto"}, "function_call"),
        ({"n": 2}, "n"),
    ],
)
def test_unsupported_features_are_a_400(fields, param):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(**fields))

    assert raised.value.status == 400
    assert raised.value.param == param
    assert raised.value.code in ("unsupported_parameter", "unsupported_value")


@pytest.mark.parametrize("fields", [{"n": 1}, {"n": None}, {"tools": []}, {"tools": None}])
def test_harmless_values_of_unsupported_features_pass(fields):
    assert translate_request(body(**fields)).messages == [USER]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://example.com/rash.png"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
    ],
)
def test_image_and_audio_parts_are_a_400(part):
    messages = [{"role": "user", "content": [{"type": "text", "text": "What is this?"}, part]}]

    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(messages=messages))

    assert raised.value.status == 400
    assert raised.value.code == "unsupported_content"
    assert raised.value.param == "messages[0].content"
    assert part["type"] in raised.value.message


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        {"role": "function", "name": "lookup", "content": "42"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
    ],
)
def test_tool_messages_are_a_400(message):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(messages=[USER, message, USER]))

    assert raised.value.status == 400
    assert raised.value.param == "messages[1]"


# translate_request: max_tokens


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, DEFAULT_MAX_TOKENS),
        ({"max_tokens": None}, DEFAULT_MAX_TOKENS),
        ({"max_tokens": 200}, 200),
        ({"max_completion_tokens": 300}, 300),
        ({"max_tokens": 200, "max_completion_tokens": 300}, 300),
        ({"max_tokens": MAX_MAX_TOKENS}, MAX_MAX_TOKENS),
        ({"max_tokens": MAX_MAX_TOKENS + 1}, MAX_MAX_TOKENS),
        ({"max_completion_tokens": 1_000_000}, MAX_MAX_TOKENS),
        ({"max_tokens": 1}, 1),
    ],
)
def test_max_tokens_defaults_and_caps(fields, expected):
    assert translate_request(body(**fields)).max_tokens == expected


@pytest.mark.parametrize(
    ("fields", "param"),
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": -5}, "max_tokens"),
        ({"max_tokens": "200"}, "max_tokens"),
        ({"max_tokens": 1.5}, "max_tokens"),
        ({"max_tokens": True}, "max_tokens"),
        ({"max_completion_tokens": 0}, "max_completion_tokens"),
    ],
)
def test_bad_max_tokens_is_a_400(fields, param):
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(**fields))

    assert raised.value.status == 400
    assert raised.value.param == param


# translate_request: stream and ignored params


@pytest.mark.parametrize(
    ("fields", "expected"),
    [({}, False), ({"stream": False}, False), ({"stream": None}, False), ({"stream": True}, True)],
)
def test_stream_flag(fields, expected):
    assert translate_request(body(**fields)).stream is expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("temperature", 0),
        ("top_p", 0.9),
        ("top_k", 40),
        ("frequency_penalty", 0.5),
        ("presence_penalty", 0.5),
        ("seed", 7),
        ("logprobs", False),
        ("top_logprobs", 3),
    ],
)
def test_each_sampling_param_is_dropped_and_reported(name, value):
    request = translate_request(body(**{name: value}))

    assert request.ignored_params == [name]
    assert name not in request.model_dump()


def test_only_params_actually_sent_are_reported():
    request = translate_request(body(temperature=0.2, seed=1, top_p=None, user="student-7"))

    assert request.ignored_params == ["temperature", "seed"]


# completion_body


def test_completion_body_shape():
    request = chat_request(temperature=0.2)

    result = completion_body(chat_result(), request)

    assert result["id"].startswith("chatcmpl-")
    assert result["object"] == "chat.completion"
    assert isinstance(result["created"], int)
    assert result["model"] == "bootcamp"
    assert result["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there!", "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ]
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    assert result["ignored_params"] == ["temperature"]


def test_completion_body_echoes_the_model_name_the_student_sent():
    request = chat_request(model="claude-haiku-5-5")

    assert completion_body(chat_result(), request)["model"] == "claude-haiku-5-5"


def test_completion_ids_are_unique():
    request = chat_request()

    assert (
        completion_body(chat_result(), request)["id"]
        != completion_body(chat_result(), request)["id"]
    )


@pytest.mark.parametrize("finish_reason", ["stop", "length", "content_filter"])
def test_openai_package_parses_completion_body(finish_reason):
    request = chat_request(seed=7)
    result = completion_body(chat_result(finish_reason=finish_reason), request)

    # Round-trip through JSON first: that is what the openai package actually receives
    parsed = ChatCompletion.model_validate(json.loads(json.dumps(result)), strict=True)

    assert parsed.choices[0].message.content == "Hi there!"
    assert parsed.choices[0].message.role == "assistant"
    assert parsed.choices[0].finish_reason == finish_reason
    assert parsed.usage.total_tokens == 17
    assert parsed.model == "bootcamp"
    assert parsed.model_extra["ignored_params"] == ["seed"]


# stream_chunk


def stream_text(request: ChatRequest, items: list) -> str:
    """Drive stream_chunk the way the endpoint will: open, then one call per item."""
    stream = Stream(request)
    return stream_chunk(stream) + "".join(stream_chunk(stream, item) for item in items)


def test_stream_events_in_order():
    request = chat_request(stream=True, temperature=0.2)
    text = stream_text(request, ["Hi ", "there!", chat_result(finish_reason="length")])

    assert text.endswith(STREAM_DONE)
    role, first, second, final, usage, done = parse_sse(text)

    assert role["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert first["choices"][0]["delta"] == {"content": "Hi "}
    assert second["choices"][0]["delta"] == {"content": "there!"}
    assert [e["choices"][0]["finish_reason"] for e in (role, first, second)] == [None, None, None]

    assert final["choices"][0]["delta"] == {}
    assert final["choices"][0]["finish_reason"] == "length"

    assert usage["choices"] == []
    assert usage["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    assert usage["ignored_params"] == ["temperature"]

    assert done == "[DONE]"


def test_stream_chunks_share_id_created_and_requested_model():
    request = chat_request(stream=True)
    events = parse_sse(stream_text(request, ["Hi", chat_result()]))[:-1]

    assert len({event["id"] for event in events}) == 1
    assert events[0]["id"].startswith("chatcmpl-")
    assert len({event["created"] for event in events}) == 1
    assert {event["model"] for event in events} == {"bootcamp"}
    assert {event["object"] for event in events} == {"chat.completion.chunk"}


def test_each_stream_gets_its_own_id():
    request = chat_request(stream=True)

    assert Stream(request).id != Stream(request).id


def test_stream_chunk_is_one_sse_event_per_delta():
    chunk = stream_chunk(Stream(chat_request(stream=True)), 'say "hi"\n\nnow')

    assert chunk.startswith("data: ")
    assert chunk.endswith("\n\n")
    assert chunk.count("\n\n") == 1
    assert parse_sse(chunk)[0]["choices"][0]["delta"]["content"] == 'say "hi"\n\nnow'


def test_openai_package_parses_every_stream_chunk():
    request = chat_request(stream=True, top_p=0.9)
    events = parse_sse(stream_text(request, ["Hi ", "there!", chat_result()]))

    assert events.pop() == "[DONE]"
    chunks = [ChatCompletionChunk.model_validate(event, strict=True) for event in events]

    assert chunks[0].choices[0].delta.role == "assistant"
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hi there!"
    assert chunks[-2].choices[0].finish_reason == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage.total_tokens == 17
    assert chunks[-1].model_extra["ignored_params"] == ["top_p"]


# error_body and models_body


def test_error_body_shape():
    assert error_body("Rate limit reached", "rate_limit_error", code="rate_limit_exceeded") == {
        "error": {
            "message": "Rate limit reached",
            "type": "rate_limit_error",
            "param": None,
            "code": "rate_limit_exceeded",
        }
    }


def test_error_body_from_an_unsupported_request():
    with pytest.raises(UnsupportedRequest) as raised:
        translate_request(body(model="gpt-4o"))
    error = raised.value

    result = error_body(error.message, "invalid_request_error", error.code, error.param)

    assert result["error"]["code"] == "model_not_found"
    assert result["error"]["param"] == "model"
    assert "gpt-4o" in result["error"]["message"]


def test_models_body_lists_the_alias_and_the_real_model():
    result = models_body()

    assert result["object"] == "list"
    assert [model["id"] for model in result["data"]] == ["bootcamp", "claude-haiku-5-5"]
    for model in result["data"]:
        parsed = Model.model_validate(model, strict=True)
        assert parsed.object == "model"


def test_models_body_lists_one_model_when_the_alias_is_the_real_id(use_settings):
    use_settings(model_alias="claude-haiku-5-5")

    assert [model["id"] for model in models_body()["data"]] == ["claude-haiku-5-5"]
