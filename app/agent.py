"""The booking agent: a tool-use loop where the model can look and propose, and only code books.

One call to run_turn() is one turn of the conversation. The model has three tools:
check_availability reads the schedule, propose_slot ends the turn with a signed proposal for
the patient to confirm, and book_slot runs only when the request carries that signature back.
"""

import datetime
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from app import schedule
from app.config import get_settings
from app.generate import EFFORT, get_client
from app.models import Slot
from app.thresholds import AGENT_MAX_MODEL_CALLS, AVAILABILITY_MAX_SLOTS, PROPOSAL_TTL_SECONDS

logger = logging.getLogger(__name__)

# Room for a short reply or a tool call plus the model's thinking, which counts toward the cap.
AGENT_MAX_TOKENS = 2048

CLINIC_TIMEZONE = ZoneInfo("America/New_York")

VISIT_LABELS = {
    "returning": "returning patient visit (20 minutes)",
    "physical": "annual physical (40 minutes)",
    "telehealth": "telehealth video visit (20 minutes)",
}

SYSTEM_PROMPT = """\
You are the appointment booking assistant for {clinic_name}, a fictional primary care clinic. \
Today is {today}, clinic time (Eastern).

Your only job is to help a patient find an open appointment and book it.

How booking works:
- Call check_availability to see open slots. Never invent a time, a provider or a slot_id: use \
only what check_availability returned in this turn. Earlier turns of the conversation do not \
carry slot ids, so call it again whenever you need them.
- Online booking covers three visit types: returning (a 20 minute visit for a returning \
patient), physical (a 40 minute annual physical) and telehealth (a 20 minute video visit for a \
returning patient). It opens tomorrow and covers the next two weeks.
- New patient visits and same-day visits are booked by phone at (555) 555-0100, not here.
- When the patient has picked one specific slot, call propose_slot with its slot_id. The \
patient then sees Confirm and Decline buttons. You cannot book without their confirmation, so \
do not ask "shall I book it?" in text: propose the slot.
- Call book_slot only when the patient's latest message says they pressed Confirm. That \
message contains the slot_id. If book_slot returns an error, explain it plainly and offer \
other times.
- After book_slot succeeds, tell the patient the appointment is booked and give the \
confirmation code exactly as returned.
- If the patient declines a proposal, offer other open times.
- If the patient has not said what kind of visit or roughly when, show a few of the soonest \
options or ask one short question.

Limits:
- Do not ask for a name, date of birth, phone number, insurance or any other personal detail. \
This demo collects none.
- Never give medical advice, a diagnosis, triage or medication dosing. If asked, say you can \
only book appointments and suggest calling the clinic.
- If the patient describes a possible emergency (such as chest pain, trouble breathing, signs \
of a stroke, severe bleeding or thoughts of self-harm), do not book anything. Tell them to \
call 911 now or go to the nearest emergency room.
- Reply in short plain text, no Markdown. List at most five options, one per line."""

TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "List open appointment slots, soonest first. Call this before proposing a slot "
            "and whenever the patient asks about times, days, providers or visit types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visit_type": {
                    "type": "string",
                    "enum": list(schedule.VISIT_TYPES),
                    "description": "Only slots of this visit type. Omit for all types.",
                },
                "on_or_after": {
                    "type": "string",
                    "description": "Earliest date to look at, as YYYY-MM-DD. Omit for tomorrow.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"How many slots to return, 1 to {AVAILABILITY_MAX_SLOTS}. "
                    "Default 5.",
                },
            },
        },
    },
    {
        "name": "propose_slot",
        "description": (
            "Show the patient one slot with Confirm and Decline buttons. This ends your turn. "
            "The slot_id must come from check_availability in this turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"slot_id": {"type": "string"}},
            "required": ["slot_id"],
        },
    },
    {
        "name": "book_slot",
        "description": (
            "Book a slot the patient has confirmed with the Confirm button. It fails unless "
            "the patient confirmed that exact slot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"slot_id": {"type": "string"}},
            "required": ["slot_id"],
        },
    },
]

# Written by code, never by a model, so they are the same every time.
REFUSAL_MESSAGE = (
    "I can't help with that here. I can only book appointments. For anything medical, call "
    "the clinic at (555) 555-0100. In an emergency, call 911."
)
STEP_LIMIT_MESSAGE = (
    "I could not finish that in a reasonable number of steps, so I stopped. Please try again "
    "with the visit type and the day you would like."
)
NO_CONFIRMATION_ERROR = (
    "Not booked. Booking needs the patient's confirmation of this exact slot, and this "
    "request does not carry a valid one (none was sent, it expired, or it is for another "
    "slot). Call check_availability and then propose_slot so the patient can confirm."
)


@dataclass
class Turn:
    """What one turn produced: the reply, the tool calls behind it, and what it cost."""

    system: str
    outcome: str = "replied"  # replied, proposed, booked, refused or step_limit
    reply: str = ""
    steps: list[dict] = field(default_factory=list)
    proposal: dict | None = None
    booking: dict | None = None
    model: str | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def build_system_prompt() -> str:
    """The system prompt with the clinic name and today's date in clinic time."""
    today = datetime.datetime.now(CLINIC_TIMEZONE).strftime("%A, %B %-d, %Y")
    return SYSTEM_PROMPT.format(clinic_name=get_settings().clinic_name, today=today)


def confirm_message(slot_id: str) -> str:
    """The user turn the server writes when the patient presses Confirm."""
    return f"[The patient pressed Confirm for slot {slot_id}.] Yes, please book it."


DECLINE_MESSAGE = "[The patient pressed Decline.] Not that one. What else is open?"


def _signature(slot_id: str, expires_at: int) -> str:
    """HMAC over the slot and its expiry. Only this server can produce it."""
    secret = get_settings().supabase_secret_key
    if not secret:
        raise RuntimeError("SUPABASE_SECRET_KEY must be set")
    # Derived the way hash_ip derives its key: one secret, one key per use
    key = hashlib.sha256(b"clinic-assistant:booking-confirmation:" + secret.encode()).digest()
    return hmac.new(key, f"{slot_id}|{expires_at}".encode(), hashlib.sha256).hexdigest()


def sign_confirmation(slot_id: str) -> dict:
    """The confirmation the page sends back when the patient presses Confirm."""
    expires_at = int(time.time()) + PROPOSAL_TTL_SECONDS
    return {
        "slot_id": slot_id,
        "expires_at": expires_at,
        "signature": _signature(slot_id, expires_at),
    }


def confirmation_allows(confirmation: dict | None, slot_id: str) -> bool:
    """True only for an unexpired confirmation that this server signed for this exact slot."""
    if not isinstance(confirmation, dict):
        return False
    expires_at = confirmation.get("expires_at")
    signature = confirmation.get("signature")
    # bool is an int in Python, so rule it out by exact type
    if type(expires_at) is not int or not isinstance(signature, str):
        return False
    if confirmation.get("slot_id") != slot_id or expires_at < time.time():
        return False
    # compare_digest takes the same time whether the first or the last character differs
    return hmac.compare_digest(signature, _signature(slot_id, expires_at))


async def _check_availability(tool_input: dict, seen: dict[str, Slot]) -> tuple[dict, bool]:
    """Run the lookup tool. Model-written arguments are validated like any other input."""
    visit_type = tool_input.get("visit_type")
    if visit_type is not None and visit_type not in schedule.VISIT_TYPES:
        return {"error": f"visit_type must be one of {', '.join(schedule.VISIT_TYPES)}."}, True
    on_or_after = tool_input.get("on_or_after")
    if on_or_after is not None:
        try:
            on_or_after = datetime.date.fromisoformat(on_or_after)
        except (TypeError, ValueError):
            return {"error": "on_or_after must be a date like 2026-12-01."}, True
    limit = tool_input.get("limit", 5)
    if type(limit) is not int:
        return {"error": "limit must be a whole number."}, True
    limit = min(max(limit, 1), AVAILABILITY_MAX_SLOTS)

    slots = await schedule.check_availability(visit_type, on_or_after, limit)
    for slot in slots:
        seen[slot.slot_id] = slot
    result = {
        "slots": [
            {"slot_id": slot.slot_id, "visit_type": slot.visit_type, "when": slot.display}
            for slot in slots
        ]
    }
    if not slots:
        result["note"] = "No open slots match. Online booking covers the next two weeks."
    return result, False


async def _book_slot(tool_input: dict, confirmation: dict | None, ip_hash: str, turn: Turn):
    """Run the booking tool. This is the only place a booking can happen."""
    slot_id = tool_input.get("slot_id")
    # The model asking is not enough. The signature proves a person pressed Confirm on a
    # proposal this server made for this slot.
    if not isinstance(slot_id, str) or not confirmation_allows(confirmation, slot_id):
        return {"error": NO_CONFIRMATION_ERROR}, True
    try:
        booked = await schedule.book(slot_id, ip_hash)
    except schedule.BookingError as error:
        return {"error": f"Not booked. {error} Offer the patient other times."}, True
    turn.outcome = "booked"
    turn.booking = {
        "confirmation_code": booked.confirmation_code,
        "slot_id": booked.slot_id,
        "visit_type": booked.visit_type,
        "display": booked.display,
    }
    return {"booked": True, **turn.booking}, False


def _propose_slot(tool_input: dict, seen: dict[str, Slot], turn: Turn) -> tuple[dict, bool]:
    """Turn a slot the model picked into a signed proposal for the patient."""
    slot_id = tool_input.get("slot_id")
    slot = seen.get(slot_id) if isinstance(slot_id, str) else None
    if slot is None:
        return {
            "error": "Unknown slot_id. Propose only a slot_id that check_availability "
            "returned in this turn."
        }, True
    label = VISIT_LABELS.get(slot.visit_type, slot.visit_type)
    if turn.booking is None:
        turn.outcome = "proposed"
    turn.proposal = {"slot": slot.model_dump(), "confirmation": sign_confirmation(slot.slot_id)}
    turn.reply = (
        f"I can book {slot.display}, a {label}. Press Confirm to book it, or Decline to see "
        "other times."
    )
    return {"proposed": True, "waiting_for": "the patient to press Confirm or Decline"}, False


def _booked_fallback(turn: Turn) -> str:
    """The confirmation text when a booking happened but the model did not get to write one."""
    return (
        f"Your appointment is booked: {turn.booking['display']}. Your confirmation code is "
        f"{turn.booking['confirmation_code']}."
    )


async def run_turn(messages: list[dict], confirmation: dict | None, ip_hash: str) -> Turn:
    """One turn: call the model, run the tools it asks for, repeat until it replies.

    messages is the conversation as plain text turns, ending with the patient's message.
    confirmation is whatever the page sent back from a Confirm click, verified here.
    """
    settings = get_settings()
    turn = Turn(system=build_system_prompt())
    conversation = list(messages)
    # Slots check_availability returned in this turn: the only ones that can be proposed
    seen: dict[str, Slot] = {}

    for _ in range(AGENT_MAX_MODEL_CALLS):
        try:
            # No temperature, top_p or top_k: Haiku 5.5 rejects them.
            response = await get_client().messages.create(
                model=settings.generation_model,
                max_tokens=AGENT_MAX_TOKENS,
                system=turn.system,
                tools=TOOLS,
                messages=conversation,
                output_config={"effort": EFFORT},
            )
        except Exception:
            # A booking is a side effect that already happened: report it, don't lose it
            if turn.booking is None:
                raise
            logger.exception("model call failed after a booking")
            turn.reply = _booked_fallback(turn)
            return turn

        turn.model = response.model
        turn.model_calls += 1
        turn.input_tokens += response.usage.input_tokens
        turn.output_tokens += response.usage.output_tokens

        # A refusal is a normal 200 response, so check for it before reading any content
        if response.stop_reason == "refusal" and turn.booking is None:
            turn.outcome = "refused"
            turn.reply = REFUSAL_MESSAGE
            return turn

        if response.stop_reason != "tool_use":
            # Read blocks by type: thinking blocks can come before the text
            text = "".join(block.text for block in response.content if block.type == "text")
            turn.reply = text.strip() or (
                _booked_fallback(turn) if turn.booking else STEP_LIMIT_MESSAGE
            )
            return turn

        # The whole content goes back unchanged: thinking blocks carry a signature the API
        # checks, and each tool_result below must answer a tool_use block in this message
        conversation.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_input = block.input if isinstance(block.input, dict) else {}
            if block.name == "check_availability":
                result, is_error = await _check_availability(tool_input, seen)
            elif block.name == "propose_slot":
                result, is_error = _propose_slot(tool_input, seen, turn)
            elif block.name == "book_slot":
                result, is_error = await _book_slot(tool_input, confirmation, ip_hash, turn)
            else:
                result, is_error = {"error": f"Unknown tool {block.name}."}, True

            turn.steps.append(
                {"tool": block.name, "input": tool_input, "result": result, "is_error": is_error}
            )
            # A proposal ends the turn: the next move belongs to the patient, not the model
            if turn.proposal is not None:
                return turn
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                    "is_error": is_error,
                }
            )
        # Every result goes back in one user message, in the order the model asked
        conversation.append({"role": "user", "content": results})

    if turn.booking is None:
        turn.outcome = "step_limit"
        turn.reply = STEP_LIMIT_MESSAGE
    else:
        turn.reply = _booked_fallback(turn)
    return turn
