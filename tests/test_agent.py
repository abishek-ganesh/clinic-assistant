"""The booking loop with a scripted fake model and a fake schedule: no network anywhere."""

import json

import pytest
from anthropic.types import Message

from app import agent, schedule
from app import generate as generate_module
from app.models import Slot
from app.thresholds import AGENT_MAX_MODEL_CALLS, PROPOSAL_TTL_SECONDS

SLOT = Slot(
    slot_id="patel:2026-12-01T09:40",
    provider="Dr. Patel",
    visit_type="returning",
    starts_at="2026-12-01T09:40:00-05:00",
    display="Tue Dec 1, 9:40 AM with Dr. Patel (Main Street)",
)
OTHER = "kim:2026-12-01T13:00"
THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}
ASK = [{"role": "user", "content": "Earliest returning visit, please."}]


def reply(*blocks: dict, stop_reason: str = "end_turn") -> Message:
    """A model response the way the SDK returns it: a thinking block first, then the rest."""
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [THINKING, *blocks],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    )


def text(value: str) -> Message:
    return reply({"type": "text", "text": value})


def call(name: str, tool_input: dict, tool_id: str = "toolu_1") -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def calls(*blocks: dict) -> Message:
    return reply(*blocks, stop_reason="tool_use")


class FakeMessages:
    """Plays back scripted responses and keeps what each request sent."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def create(self, **arguments):
        # The loop keeps appending to its list, so keep a copy of what this call saw
        self.requests.append({**arguments, "messages": list(arguments["messages"])})
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSchedule:
    def __init__(self):
        self.lookups = []
        self.booked = []
        self.fail_with = None

    async def check_availability(self, visit_type=None, on_or_after=None, limit=5):
        self.lookups.append((visit_type, on_or_after, limit))
        return [SLOT]

    async def book(self, slot_id, ip_hash):
        if self.fail_with:
            raise schedule.BookingError(self.fail_with)
        self.booked.append((slot_id, ip_hash))
        return schedule.Confirmation("FC-3F9A2C", slot_id, "returning", SLOT.display)


@pytest.fixture
def fakes(monkeypatch, use_settings):
    """Script the model with fakes.model(...). The schedule answers from memory."""
    use_settings(supabase_secret_key="sb_secret_test", anthropic_api_key="sk-ant-test")
    fake_schedule = FakeSchedule()
    monkeypatch.setattr(schedule, "check_availability", fake_schedule.check_availability)
    monkeypatch.setattr(schedule, "book", fake_schedule.book)

    class Fakes:
        schedule = fake_schedule
        messages = None

        def model(self, *script):
            self.messages = FakeMessages(script)
            generate_module.set_client(type("Client", (), {"messages": self.messages})())

    yield Fakes()
    generate_module.set_client(None)


def tool_results(request: dict) -> list[dict]:
    """The tool_result blocks in the last message of a recorded request."""
    return request["messages"][-1]["content"]


# The signature


def test_a_confirmation_allows_only_its_own_slot(fakes):
    confirmation = agent.sign_confirmation(SLOT.slot_id)
    assert agent.confirmation_allows(confirmation, SLOT.slot_id)
    assert not agent.confirmation_allows(confirmation, OTHER)
    assert not agent.confirmation_allows({**confirmation, "slot_id": OTHER}, OTHER)


def test_a_tampered_or_malformed_confirmation_is_rejected(fakes):
    good = agent.sign_confirmation(SLOT.slot_id)
    later = {**good, "expires_at": good["expires_at"] + 3600}
    assert not agent.confirmation_allows(later, SLOT.slot_id)
    assert not agent.confirmation_allows({**good, "signature": "0" * 64}, SLOT.slot_id)
    assert not agent.confirmation_allows({**good, "signature": None}, SLOT.slot_id)
    assert not agent.confirmation_allows(
        {**good, "expires_at": str(good["expires_at"])}, SLOT.slot_id
    )
    assert not agent.confirmation_allows({**good, "expires_at": True}, SLOT.slot_id)
    assert not agent.confirmation_allows({"slot_id": SLOT.slot_id}, SLOT.slot_id)
    assert not agent.confirmation_allows(None, SLOT.slot_id)
    assert not agent.confirmation_allows("yes", SLOT.slot_id)


def test_a_confirmation_expires(fakes, monkeypatch):
    confirmation = agent.sign_confirmation(SLOT.slot_id)
    now = agent.time.time()
    monkeypatch.setattr(agent.time, "time", lambda: now + PROPOSAL_TTL_SECONDS - 5)
    assert agent.confirmation_allows(confirmation, SLOT.slot_id)
    monkeypatch.setattr(agent.time, "time", lambda: now + PROPOSAL_TTL_SECONDS + 5)
    assert not agent.confirmation_allows(confirmation, SLOT.slot_id)


def test_a_confirmation_signed_with_another_key_is_rejected(fakes, use_settings):
    confirmation = agent.sign_confirmation(SLOT.slot_id)
    use_settings(supabase_secret_key="a-different-secret")
    assert not agent.confirmation_allows(confirmation, SLOT.slot_id)


# The loop


async def test_a_plain_reply_takes_one_model_call(fakes):
    fakes.model(text("Which day works for you?"))
    turn = await agent.run_turn(ASK, None, "ip")
    assert (turn.outcome, turn.reply, turn.steps) == ("replied", "Which day works for you?", [])
    assert (turn.model_calls, turn.input_tokens, turn.output_tokens) == (1, 100, 10)

    request = fakes.messages.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert [tool["name"] for tool in request["tools"]] == [
        "check_availability", "propose_slot", "book_slot",
    ]  # fmt: skip
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & request.keys()
    assert "911" in request["system"] and "Family Clinic" in request["system"]
    assert request["messages"][-1]["role"] == "user"  # no prefill


async def test_a_proposal_ends_the_turn_with_a_signed_confirmation(fakes):
    fakes.model(
        calls(call("check_availability", {"visit_type": "returning", "limit": 3})),
        calls(call("propose_slot", {"slot_id": SLOT.slot_id})),
        text("never reached"),
    )
    turn = await agent.run_turn(ASK, None, "ip")

    assert turn.outcome == "proposed"
    assert turn.model_calls == 2 and len(fakes.messages.script) == 1
    assert (turn.input_tokens, turn.output_tokens) == (200, 20)
    assert [step["tool"] for step in turn.steps] == ["check_availability", "propose_slot"]
    assert turn.proposal["slot"] == SLOT.model_dump()
    assert agent.confirmation_allows(turn.proposal["confirmation"], SLOT.slot_id)
    assert SLOT.display in turn.reply and "Confirm" in turn.reply
    assert fakes.schedule.lookups == [("returning", None, 3)]
    assert fakes.schedule.booked == []


async def test_thinking_blocks_and_tool_results_go_back_unchanged(fakes):
    first = calls(call("check_availability", {}, "toolu_a"))
    fakes.model(first, text("Tue Dec 1, 9:40 AM is open."))
    await agent.run_turn(ASK, None, "ip")

    second_request = fakes.messages.requests[1]["messages"]
    assert second_request[0] == ASK[0]
    # The assistant turn is the response's own content list, thinking block included
    assert second_request[1] == {"role": "assistant", "content": first.content}
    assert second_request[1]["content"][0].type == "thinking"
    [result] = second_request[2]["content"]
    assert (result["type"], result["tool_use_id"], result["is_error"]) == (
        "tool_result",
        "toolu_a",
        False,
    )
    assert json.loads(result["content"])["slots"][0]["slot_id"] == SLOT.slot_id


async def test_book_slot_without_a_confirmation_books_nothing(fakes):
    fakes.model(
        calls(call("book_slot", {"slot_id": SLOT.slot_id})),
        text("I need you to confirm first."),
    )
    turn = await agent.run_turn(ASK, None, "ip")

    assert fakes.schedule.booked == []
    assert turn.outcome == "replied" and turn.booking is None
    assert turn.steps[0]["is_error"] and "propose_slot" in turn.steps[0]["result"]["error"]
    [result] = tool_results(fakes.messages.requests[1])
    assert result["is_error"] is True


@pytest.mark.parametrize("change", ["signature", "expired", "other_slot"])
async def test_book_slot_with_a_bad_confirmation_books_nothing(fakes, monkeypatch, change):
    confirmation = agent.sign_confirmation(SLOT.slot_id)
    target = SLOT.slot_id
    if change == "signature":
        confirmation["signature"] = "f" * 64
    elif change == "expired":
        now = agent.time.time()
        monkeypatch.setattr(agent.time, "time", lambda: now + PROPOSAL_TTL_SECONDS + 1)
    else:
        target = OTHER  # a real signature, but the model tries to spend it on another slot

    fakes.model(calls(call("book_slot", {"slot_id": target})), text("Let me propose again."))
    turn = await agent.run_turn(ASK, confirmation, "ip")
    assert fakes.schedule.booked == []
    assert turn.booking is None and turn.steps[0]["is_error"]


async def test_a_confirmed_slot_is_booked_and_the_model_writes_the_confirmation(fakes):
    confirmation = agent.sign_confirmation(SLOT.slot_id)
    fakes.model(
        calls(call("book_slot", {"slot_id": SLOT.slot_id})),
        text("You're booked. Your confirmation code is FC-3F9A2C."),
    )
    messages = [*ASK, {"role": "assistant", "content": "I can book..."},
                {"role": "user", "content": agent.confirm_message(SLOT.slot_id)}]  # fmt: skip
    turn = await agent.run_turn(messages, confirmation, "iphash")

    assert fakes.schedule.booked == [(SLOT.slot_id, "iphash")]
    assert turn.outcome == "booked"
    assert turn.booking["confirmation_code"] == "FC-3F9A2C"
    assert "FC-3F9A2C" in turn.reply
    [result] = tool_results(fakes.messages.requests[1])
    assert json.loads(result["content"])["confirmation_code"] == "FC-3F9A2C"
    assert SLOT.slot_id in messages[-1]["content"]


async def test_a_slot_taken_in_the_meantime_is_an_error_result_not_a_booking(fakes):
    fakes.schedule.fail_with = "slot_taken"
    fakes.model(calls(call("book_slot", {"slot_id": SLOT.slot_id})), text("That one just went."))
    turn = await agent.run_turn(ASK, agent.sign_confirmation(SLOT.slot_id), "ip")
    assert turn.outcome == "replied" and turn.booking is None
    assert "just booked by someone else" in turn.steps[0]["result"]["error"]


async def test_a_slot_the_model_made_up_cannot_be_proposed(fakes):
    fakes.model(
        calls(call("propose_slot", {"slot_id": "patel:2026-12-01T03:00"})),
        text("Let me check what is open."),
    )
    turn = await agent.run_turn(ASK, None, "ip")
    assert turn.proposal is None and turn.outcome == "replied"
    assert turn.steps[0]["is_error"]


@pytest.mark.parametrize(
    "tool_input",
    [{"visit_type": "surgery"}, {"on_or_after": "next Tuesday"}, {"limit": "many"}],
)
async def test_bad_tool_arguments_come_back_as_errors_without_a_lookup(fakes, tool_input):
    fakes.model(calls(call("check_availability", tool_input)), text("Sorry, one moment."))
    turn = await agent.run_turn(ASK, None, "ip")
    assert turn.steps[0]["is_error"] and fakes.schedule.lookups == []


async def test_the_limit_is_clamped_and_the_date_parsed(fakes):
    fakes.model(
        calls(call("check_availability", {"on_or_after": "2026-12-03", "limit": 500})),
        text("Here you go."),
    )
    await agent.run_turn(ASK, None, "ip")
    [(visit_type, on_or_after, limit)] = fakes.schedule.lookups
    assert (visit_type, on_or_after.isoformat(), limit) == (None, "2026-12-03", 10)


async def test_parallel_tool_calls_get_one_result_each_in_one_message(fakes):
    fakes.model(
        calls(
            call("check_availability", {"visit_type": "returning"}, "toolu_a"),
            call("check_availability", {"visit_type": "physical"}, "toolu_b"),
            call("nonsense", {}, "toolu_c"),
        ),
        text("Here are both."),
    )
    turn = await agent.run_turn(ASK, None, "ip")
    results = tool_results(fakes.messages.requests[1])
    assert [result["tool_use_id"] for result in results] == ["toolu_a", "toolu_b", "toolu_c"]
    assert [result["is_error"] for result in results] == [False, False, True]
    assert len(turn.steps) == 3


async def test_the_loop_stops_at_the_model_call_cap(fakes):
    fakes.model(*[calls(call("check_availability", {}))] * (AGENT_MAX_MODEL_CALLS + 3))
    turn = await agent.run_turn(ASK, None, "ip")
    assert turn.outcome == "step_limit" and turn.reply == agent.STEP_LIMIT_MESSAGE
    assert turn.model_calls == AGENT_MAX_MODEL_CALLS == len(fakes.messages.requests)
    assert turn.input_tokens == 100 * AGENT_MAX_MODEL_CALLS


async def test_a_refusal_gets_the_fixed_message(fakes):
    fakes.model(reply(stop_reason="refusal"))
    turn = await agent.run_turn(ASK, None, "ip")
    assert (turn.outcome, turn.reply) == ("refused", agent.REFUSAL_MESSAGE)
    assert turn.steps == []


async def test_a_model_failure_before_any_booking_is_raised(fakes):
    fakes.model(RuntimeError("upstream down"))
    with pytest.raises(RuntimeError):
        await agent.run_turn(ASK, None, "ip")


async def test_a_model_failure_after_a_booking_still_reports_the_booking(fakes):
    fakes.model(calls(call("book_slot", {"slot_id": SLOT.slot_id})), RuntimeError("upstream down"))
    turn = await agent.run_turn(ASK, agent.sign_confirmation(SLOT.slot_id), "ip")
    assert turn.outcome == "booked"
    assert "FC-3F9A2C" in turn.reply and SLOT.display in turn.reply
