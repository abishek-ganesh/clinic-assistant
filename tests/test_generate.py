import json

import httpx
import pytest
import respx
from anthropic.types import Message, ParsedMessage

from app import generate as generate_module
from app.generate import Answer, build_prompt, chat, chat_stream, generate
from app.models import ChatRequest, ChatResult, Passage

OLLAMA_URL = "http://localhost:11434/api/chat"
SAMPLING_PARAMS = {"temperature", "top_p", "top_k"}

PASSAGES = [
    Passage(
        rank=1,
        section_slug="refills",
        section="Prescription refills",
        heading="Turnaround",
        content="Refills take up to three business days.",
        similarity=0.82,
    ),
    Passage(
        rank=2,
        section_slug="hours",
        section="Hours and locations",
        heading=None,
        content="The clinic is open Monday to Friday, 8 am to 6 pm.",
        similarity=0.41,
    ),
]

THINKING_BLOCK = {"type": "thinking", "thinking": "", "signature": "sig"}


def message_fields(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 210, "output_tokens": 35},
    }


def parsed_message(answer: dict | None, stop_reason: str = "end_turn", thinking: bool = True):
    """A structured-output response the way the SDK returns it: thinking first, then text."""
    content = [THINKING_BLOCK] if thinking else []
    if answer is not None:
        content.append({"type": "text", "text": json.dumps(answer), "parsed_output": answer})
    return ParsedMessage[Answer].model_validate(message_fields(content, stop_reason))


def text_message(text: str, stop_reason: str = "end_turn") -> Message:
    content = [THINKING_BLOCK, {"type": "text", "text": text}]
    return Message.model_validate(message_fields(content, stop_reason))


class FakeStream:
    """Stands in for the SDK's stream helper: an async context manager with text_stream."""

    def __init__(self, deltas: list[str], final: Message):
        self._deltas = deltas
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    async def text_stream(self):
        for delta in self._deltas:
            yield delta

    async def get_final_message(self) -> Message:
        return self._final


class FakeMessages:
    def __init__(self, response=None, deltas=None):
        self.response = response
        self.deltas = deltas or []
        self.calls: list[tuple[str, dict]] = []

    async def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        return self.response

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.response

    def stream(self, **kwargs):
        self.calls.append(("stream", kwargs))
        return FakeStream(self.deltas, self.response)


class FakeClient:
    def __init__(self, response=None, deltas=None):
        self.messages = FakeMessages(response, deltas)


@pytest.fixture
def fake_client(use_settings):
    """Install a fake Anthropic client and remove it afterwards."""
    use_settings(anthropic_api_key="sk-ant-test")

    def _install(response=None, deltas=None) -> FakeMessages:
        client = FakeClient(response, deltas)
        generate_module.set_client(client)
        return client.messages

    yield _install
    generate_module.set_client(None)


def chat_request(**overrides) -> ChatRequest:
    fields = {
        "requested_model": "bootcamp",
        "model": "claude-haiku-5-5",
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Say hello"}],
        "max_tokens": 256,
        "stream": False,
        "ignored_params": [],
    }
    return ChatRequest(**{**fields, **overrides})


# build_prompt


def test_build_prompt_numbers_passages_with_section_and_heading(use_settings):
    use_settings()

    prompt = build_prompt("How long do refills take?", PASSAGES)

    assert prompt.user == (
        "Handbook passages:\n\n"
        "[1] Prescription refills: Turnaround\n"
        "Refills take up to three business days.\n\n"
        "[2] Hours and locations\n"
        "The clinic is open Monday to Friday, 8 am to 6 pm.\n\n"
        "Question: How long do refills take?"
    )


def test_build_prompt_system_states_every_rule(use_settings):
    use_settings(clinic_name="Family Clinic")

    system = build_prompt("anything", PASSAGES).system

    assert "Family Clinic" in system
    assert "only from the numbered handbook passages" in system
    assert "[1]" in system
    assert "answerable to false" in system
    assert "Never give medical advice" in system
    assert "dosing" in system
    assert "short" in system


# generate


async def test_generate_sends_the_prompt_with_low_effort_and_no_sampling(fake_client):
    answer = {"answer": "Up to three business days [1].", "citations": [1], "answerable": True}
    messages = fake_client(parsed_message(answer))

    await generate("How long do refills take?", PASSAGES)

    method, sent = messages.calls[0]
    prompt = build_prompt("How long do refills take?", PASSAGES)
    assert method == "parse"
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["system"] == prompt.system
    assert sent["messages"] == [{"role": "user", "content": prompt.user}]
    assert sent["output_config"] == {"effort": "low"}
    assert sent["output_format"] is Answer
    assert sent["max_tokens"] >= 1024
    assert not SAMPLING_PARAMS & set(sent)
    assert "fallbacks" not in sent
    assert "thinking" not in sent


async def test_generate_reads_the_answer_after_a_thinking_block(fake_client):
    answer = {"answer": "Up to three business days [1].", "citations": [1], "answerable": True}
    fake_client(parsed_message(answer, thinking=True))

    result = await generate("How long do refills take?", PASSAGES)

    assert result.text == "Up to three business days [1]."
    assert result.citations == [1]
    assert result.answerable is True
    assert result.refused is False
    assert result.provider == "anthropic"
    assert result.model == "claude-haiku-5-5"
    assert result.input_tokens == 210
    assert result.output_tokens == 35
    assert result.prompt == build_prompt("How long do refills take?", PASSAGES)


async def test_generate_drops_citations_that_are_not_passage_ranks(fake_client):
    answer = {"answer": "See [2] and [7].", "citations": [2, 7, 0, 2, -1], "answerable": True}
    fake_client(parsed_message(answer))

    result = await generate("When are you open?", PASSAGES)

    assert result.citations == [2]


async def test_generate_passes_through_not_answerable(fake_client):
    answer = {"answer": "The handbook does not cover that.", "citations": [], "answerable": False}
    fake_client(parsed_message(answer))

    result = await generate("Do you validate parking?", PASSAGES)

    assert result.answerable is False
    assert result.refused is False
    assert result.text == "The handbook does not cover that."


async def test_generate_refusal_returns_refused_with_empty_text(fake_client):
    fake_client(parsed_message(None, stop_reason="refusal", thinking=False))

    result = await generate("something the model declines", PASSAGES)

    assert result.refused is True
    assert result.answerable is False
    assert result.text == ""
    assert result.citations == []
    assert result.input_tokens == 210


async def test_generate_raises_when_thinking_used_the_whole_budget(fake_client):
    fake_client(parsed_message(None, stop_reason="max_tokens"))

    with pytest.raises(RuntimeError, match="max_tokens"):
        await generate("How long do refills take?", PASSAGES)


async def test_generate_lets_sdk_errors_propagate(fake_client):
    messages = fake_client()

    async def boom(**_kwargs):
        raise ConnectionError("upstream down")

    messages.parse = boom

    with pytest.raises(ConnectionError):
        await generate("How long do refills take?", PASSAGES)


# chat


async def test_chat_sends_the_request_with_low_effort_and_no_sampling(fake_client):
    messages = fake_client(text_message("Hello!"))

    await chat(chat_request())

    method, sent = messages.calls[0]
    assert method == "create"
    assert sent == {
        "model": "claude-haiku-5-5",
        "max_tokens": 256,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Say hello"}],
        "output_config": {"effort": "low"},
    }


async def test_chat_omits_system_when_there_is_none(fake_client):
    messages = fake_client(text_message("Hello!"))

    await chat(chat_request(system=None))

    assert "system" not in messages.calls[0][1]


async def test_chat_reads_text_after_a_thinking_block(fake_client):
    fake_client(text_message("Hello!"))

    result = await chat(chat_request())

    assert result == ChatResult(
        text="Hello!",
        finish_reason="stop",
        model="claude-haiku-5-5",
        input_tokens=210,
        output_tokens=35,
    )


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
    ],
)
async def test_chat_maps_stop_reasons(fake_client, stop_reason, finish_reason):
    fake_client(text_message("Hi", stop_reason=stop_reason))

    result = await chat(chat_request())

    assert result.finish_reason == finish_reason


# chat_stream


async def test_chat_stream_yields_deltas_then_exactly_one_result(fake_client):
    messages = fake_client(text_message("Hello there!"), deltas=["Hel", "lo ", "there!"])

    items = [item async for item in chat_stream(chat_request(stream=True))]

    assert items[:-1] == ["Hel", "lo ", "there!"]
    assert items[-1] == ChatResult(
        text="Hello there!",
        finish_reason="stop",
        model="claude-haiku-5-5",
        input_tokens=210,
        output_tokens=35,
    )
    assert sum(isinstance(item, ChatResult) for item in items) == 1

    method, sent = messages.calls[0]
    assert method == "stream"
    assert sent["output_config"] == {"effort": "low"}
    assert not SAMPLING_PARAMS & set(sent)


async def test_chat_stream_reports_a_refusal_as_content_filter(fake_client):
    fake_client(text_message("", stop_reason="refusal"), deltas=[])

    items = [item async for item in chat_stream(chat_request(stream=True))]

    assert len(items) == 1
    assert items[0].finish_reason == "content_filter"


# client and fallback


def test_get_client_builds_one_client_and_reuses_it(use_settings):
    use_settings(anthropic_api_key="sk-ant-test")
    generate_module.set_client(None)
    try:
        first = generate_module.get_client()
        assert generate_module.get_client() is first
        assert first.api_key == "sk-ant-test"
    finally:
        generate_module.set_client(None)


async def test_missing_key_in_production_raises_everywhere(use_settings):
    use_settings(app_env="production")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await generate("hi", PASSAGES)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await chat(chat_request())
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        [item async for item in chat_stream(chat_request(stream=True))]


def ollama_response(content: str, done_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "llama3.2",
            "message": {"role": "assistant", "content": content},
            "done_reason": done_reason,
            "prompt_eval_count": 90,
            "eval_count": 12,
        },
    )


@respx.mock
async def test_generate_falls_back_to_ollama_without_a_key(use_settings):
    use_settings()
    answer = {"answer": "Three business days [1].", "citations": [1, 9], "answerable": True}
    route = respx.post(OLLAMA_URL).mock(return_value=ollama_response(json.dumps(answer)))

    result = await generate("How long do refills take?", PASSAGES)

    assert result.provider == "ollama"
    assert result.model == "llama3.2"
    assert result.text == "Three business days [1]."
    assert result.citations == [1]
    assert (result.input_tokens, result.output_tokens) == (90, 12)

    sent = json.loads(route.calls[0].request.content)
    prompt = build_prompt("How long do refills take?", PASSAGES)
    assert sent["model"] == "llama3.2"
    assert sent["stream"] is False
    assert sent["format"] == Answer.model_json_schema()
    assert sent["messages"] == [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": prompt.user},
    ]


@respx.mock
async def test_chat_falls_back_to_ollama_without_a_key(use_settings):
    use_settings()
    route = respx.post(OLLAMA_URL).mock(return_value=ollama_response("Hello!", "length"))

    result = await chat(chat_request())

    assert result == ChatResult(
        text="Hello!", finish_reason="length", model="llama3.2", input_tokens=90, output_tokens=12
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Say hello"},
    ]
    assert sent["options"] == {"num_predict": 256}


@respx.mock
async def test_chat_stream_falls_back_to_ollama_without_a_key(use_settings):
    use_settings()
    respx.post(OLLAMA_URL).mock(return_value=ollama_response("Hello!"))

    items = [item async for item in chat_stream(chat_request(stream=True))]

    assert items[0] == "Hello!"
    assert isinstance(items[1], ChatResult)
    assert items[1].model == "llama3.2"
    assert len(items) == 2
