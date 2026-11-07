import asyncio
import re
import string
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

from refill.guard import FIXED_ANSWERS, QUESTION
from refill.steps import Step, identity_locked, next_step

load_dotenv()

MODEL = "claude-haiku-5-5"
MAX_OUTPUT_TOKENS = 320
LLM_TIMEOUT_SECONDS = 4.0
OPENING_MESSAGE = "(The conversation has started.)"
PENDING_QUESTIONS = {
    Step.IDENTIFY: "the caller's full name and date of birth",
    Step.MEDICATION: "which medication to refill",
    Step.PHARMACY: "whether to use the pharmacy on file",
    Step.INSURANCE: "whether the insurance on file is still current",
    Step.CONFIRM: "whether to send the refill request",
}
CHOOSING_PHARMACY = "which pharmacy to use instead of the one on file"

SYSTEM_PROMPT = """\
You read messages from patients of Demo Pharmacy who are calling or texting about a prescription refill. \
Every reply you give is one or more tool calls. Code says the next line to the patient, including every drug, \
pharmacy, plan and price, so never write those lines yourself.

Reading a message:
- Act on the patient's latest message. Earlier answers are already recorded, and the latest system note lists \
them, so do not record them again. Use earlier messages only to understand words like "that one".
- Call a tool for every new answer, including answers to questions not asked yet, in the order given.
- Use only what the patient said. Pass null for anything unclear. Never guess or correct a name or date.
- If the patient corrects themselves, use the last value they gave.
- A bare "yes" or "no" answers the pending question in the latest system note.
- Call each tool at most once, and never add respond alongside another tool. A tool call needs no words.
- Call a field tool only if the latest message answers that exact thing. Never fill in an answer for the patient.

verify_identity:
- Call it whenever the caller gives any part of their name or date of birth, with null for what is missing. \
Use the name and date from anywhere in the conversation.
- "March fifteenth, nineteen eighty-five" is year 1985, month 3, day 15. A two-digit year stays two digits.
- Pass the name exactly as given, even if it is only a first name. Never add a name they did not say.
- If the caller says what they want before giving a name or date, use respond with kind "repeat".

Other tools:
- select_medication: match the patient's words, including a purpose such as "the blood pressure one" or the brand \
name of a drug on the list, to an id in the latest system note. Pass null if they name something not on the list. \
Do not pick the closest name.
- confirm_pharmacy: "yes" means the id of the pharmacy on file. Use another listed id, or other_pharmacy for one \
not listed. If the patient says no, or asks about or for another pharmacy without naming one, pass null for both. \
If they have no pharmacy yet, or only describe where one is, pass their words as other_pharmacy.
- confirm_insurance: status is "current" if it is still right, "changed" if the patient says it changed, and \
"unsure" for "I don't know" or "I'm not sure".
- submit_request: only when the patient says yes to sending the request.
- request_pharmacist: use clinical_question for dose advice, side effects, interactions or symptoms, \
asked_for_person when they ask for a person or pharmacist, urgent for emergency language such as chest pain, \
trouble breathing or overdose, and crisis for self-harm or not wanting to live. Never use urgent if the patient \
says it is not an emergency. Also use clinical_question if they say the strength they take differs from the list.
- change_answer: for "I want to change my medicine", "wrong one", or "go back", when the new value is not given. \
Use the field named, previous for "go back", or all for "start over" or "main menu".
- end_call: use patient_leaving only for "I have to go now" or "text me instead", when they still want the refill. \
Use declined when they no longer want it: "no" to sending the request, "never mind", "forget it", "wrong number", \
"I'll call back later", or "no" to a pharmacist calling back.

respond, for everything else:
- kind "repeat" when the patient asks you to say something again, asks what their medications are, or asks to \
hear a price or detail again.
- kind "hold" only when the patient asks you to wait, such as "hold on", "one second" or "let me think".
- kind "repeat" also when the caller claims to have answered already but gives nothing new.
- kind "unsure" when the patient says they don't know or aren't sure about the pending question.
- kind "background" for speech clearly not meant for you: a TV or radio, news, or someone talking to another person.
- kind "unclear" for anything else you cannot understand.
- kind "answer" with text, only when the patient asked a question. Write one short plain statement, with no \
questions, no prices and no lists. What a medication on the list is for, its strength and how often it is taken \
are on record, so answer those from the latest system note. Never answer from general knowledge: if the note does \
not say it, use the fixed answer for anything else.
- If the caller refuses to give their name or date of birth, use the fixed answer for why you need it.

Fixed answers, to be used word for word:
""" + "\n".join(f'- {situation}: "{answer}"' for situation, answer in FIXED_ANSWERS.items())

FILLERS = re.compile(r"\b(um+|uh+|er+|ah+|hmm+|ahem|coughs?|coughing|sneezes?|sniffs?)\b", re.IGNORECASE)
PUNCTUATION = str.maketrans("", "", string.punctuation.replace("'", ""))
SOMEONE_ELSE = re.compile(
    r"\bmy (?:husband|wife|spouse|partner|son|daughter|mom|mother|dad|father|brother|sister|friend|neighbor)\b"
    r".*\b(prescription|medication|medicine|record|refill|pharmacy|insurance)",
    re.IGNORECASE,
)
ANOTHER_PHARMACY = re.compile(r"\b(other|another|different)\b.*\bpharmac", re.IGNORECASE)
DONT_SEND = re.compile(
    r"\b(don'?t|do not|not|never)\b.{0,15}\bsend\b|\bhold off\b|\bcancel\b|\bnvm\b|\bnever ?mind\b|\bforget it\b",
    re.IGNORECASE,
)
PLAIN_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead)\b", re.IGNORECASE)
CORRECTION = re.compile(r"\b(no|not|wait|actually|instead|sorry|meant|wrong|rather|change)\b", re.IGNORECASE)
SEVERAL_REFILLS = re.compile(r"\b(all (?:of them|three|3)|both|(?:first|last) (?:two|2))\b", re.IGNORECASE)
GO_BACK = {"go back", "back", "go back please", "previous step", "go to the previous step", "go back one step"}
MAIN_MENU = {"main menu", "start over", "start again", "go to the main menu"}
REPEAT = {"repeat", "repeat that", "say that again", "can you repeat that", "what", "pardon"}
ASK_FOR_PHARMACIST = {
    "pharmacist", "pharmacist please", "a pharmacist", "talk to a pharmacist", "speak to a pharmacist", "operator",
    "representative", "a person", "a real person",
}  # fmt: skip
STILL_THERE = {
    "are you there", "hello are you there", "hello hello are you there", "you there", "can you hear me",
    "hello can you hear me", "is anyone there", "anyone there",
}  # fmt: skip
WAIT_A_MOMENT = {
    "let me think", "let me see", "let me check", "hold on", "hang on", "one second", "one moment", "one sec",
    "just a second", "just a moment", "give me a second", "give me a moment", "hold on a second", "wait a second",
}  # fmt: skip
STOP_TEXTING = {"stop", "unsubscribe", "stop texting", "stop texting me", "quit"}
NOT_SURE = {"not sure", "i'm not sure", "i am not sure", "i don't know", "i dont know", "dont know", "idk", "no idea"}
EXACT_YES = {
    "yes", "yeah", "yep", "yup", "yes please", "yeah please", "correct", "thats right", "that's right", "sure", "ok",
    "okay", "yes it is", "go ahead", "send it", "yes send it", "okay send it", "please do", "sounds good", "y", "ya",
    "yea", "k", "kk", "👍", "✅", "yes that's right", "yes thats right", "yes that is right", "yes that's correct",
    "yes thats correct", "yes that is correct", "that's correct", "thats correct", "that is correct", "yes correct",
    "yes it's correct", "yes it is correct", "it's correct", "yes that's fine", "yes thats fine", "that's fine",
    "thats fine", "yes ma'am", "yes maam", "yes sir", "yes yes", "yes please send it", "yes send it please",
    "send it please", "yep send it", "yeah send it", "yes go ahead", "yes please go ahead", "yes please do",
}  # fmt: skip
EXACT_NO = {"no", "nope", "no thanks", "no thank you", "n", "nah"}
EXACT_CALLS = {
    (Step.PHARMACY, "yes"): lambda facts: (
        "confirm_pharmacy", {"pharmacy_id": facts.pharmacy_on_file["id"], "other_pharmacy": None}
    ),
    (Step.PHARMACY, "no"): lambda facts: ("confirm_pharmacy", {"pharmacy_id": None, "other_pharmacy": None}),
    (Step.INSURANCE, "yes"): lambda facts: ("confirm_insurance", {"status": "current"}),
    (Step.INSURANCE, "no"): lambda facts: ("confirm_insurance", {"status": "changed"}),
    (Step.CONFIRM, "yes"): lambda facts: ("submit_request", {}),
    (Step.CONFIRM, "no"): lambda facts: ("end_call", {"reason": "declined"}),
}

client = anthropic.AsyncAnthropic(timeout=LLM_TIMEOUT_SECONDS, max_retries=1)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reading:
    calls: tuple = ()
    error_code: str | None = None


def recorded_fields(session, facts):
    recorded = []
    if facts.chosen:
        recorded.append(f"medication id {facts.chosen['id']}")
    if facts.pharmacy:
        recorded.append(f"pharmacy id {facts.pharmacy['id']}")
    if facts.pharmacy_text:
        recorded.append(f"pharmacy {facts.pharmacy_text}")
    if session["insurance_status"]:
        recorded.append(f"insurance {session['insurance_status']}")
    if session["pharmacist_reason"]:
        recorded.append("pharmacist callback requested")
    return ", ".join(recorded)


def state_note(session, facts, just_verified=False):
    if identity_locked(session):
        return "Verification failed. Pending question: whether the caller wants a pharmacist to call back."
    step = next_step(session)
    choosing = step is Step.PHARMACY and session["wants_other_pharmacy"]
    lines = [f"Pending question: {CHOOSING_PHARMACY if choosing else PENDING_QUESTIONS[step]}."]
    if not session["verified_at"]:
        lines.append("The caller is not verified, and you have no patient data.")
        return "\n".join(lines)
    lines.append("Prescriptions:")
    lines += [
        f"- id {p['id']}: {p['medication']} {p['dosage']}, {p['frequency'].lower()}, for {p['indication']}"
        for p in facts.prescriptions
    ]
    lines.append("Pharmacies:")
    on_file_id = facts.pharmacy_on_file["id"]
    lines += [
        f"- id {p['id']}: {p['name']}, {p['address']}{' (on file)' if p['id'] == on_file_id else ''}"
        for p in facts.pharmacies
    ]
    lines.append(f"Insurance on file: {facts.insurance_plan}, member ID ending in {facts.member_id[-4:]}.")
    lines.append(f"Already recorded: {recorded_fields(session, facts) or 'nothing yet'}.")
    if just_verified:
        lines.append("The caller was just verified. Record the answers they gave anywhere in this conversation.")
    return "\n".join(lines)


def request_settings(tools):
    return {
        "model": MODEL,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "tools": tools,
    }


async def warm_up(tool_lists):
    opening = [{"role": "user", "content": OPENING_MESSAGE}]
    await asyncio.gather(
        *(client.messages.create(max_tokens=0, messages=opening, **request_settings(tools)) for tools in tool_lists),
        return_exceptions=True,
    )


def spoken_words(text):
    return FILLERS.sub(" ", text.lower()).translate(PUNCTUATION).split()


def names_several_prescriptions(facts, text):
    lowered = text.lower()
    return sum(p["medication"].lower() in lowered or p["indication"] in lowered for p in facts.prescriptions) >= 2


def pharmacy_names(pharmacy):
    name = pharmacy["name"].lower()
    return {name, name.split()[0]}


def navigation_call(session, words):
    if not words or words in REPEAT:
        return ToolCall("respond", {"kind": "repeat", "text": None})
    if words in WAIT_A_MOMENT:
        return ToolCall("respond", {"kind": "hold", "text": None})
    if words in STILL_THERE:
        return ToolCall("respond", {"kind": "answer", "text": FIXED_ANSWERS["Whether you are still there, or can hear them"]})
    if session["channel"] == "sms" and words in STOP_TEXTING:
        return ToolCall("end_call", {"reason": "declined"})
    if words in ASK_FOR_PHARMACIST:
        return ToolCall("request_pharmacist", {"reason": "asked_for_person"})
    if words in GO_BACK or words in MAIN_MENU:
        if not session["verified_at"]:
            return ToolCall("respond", {"kind": "repeat", "text": None})
        return ToolCall("change_answer", {"field": "previous" if words in GO_BACK else "all"})
    return None


def exact_reading(session, facts, text):
    """Handle the few answers that need no interpretation, using the same tools the model would call."""
    words = " ".join(spoken_words(text))
    navigation = navigation_call(session, words)
    if navigation:
        return Reading(calls=(navigation,))
    if not session["verified_at"]:
        return None
    if SOMEONE_ELSE.search(text):
        refusal = FIXED_ANSWERS["Anyone else's records, or another patient"]
        return Reading(calls=(ToolCall("respond", {"kind": "answer", "text": refusal}),))
    if SEVERAL_REFILLS.search(text) or (names_several_prescriptions(facts, text) and not CORRECTION.search(text)):
        return Reading(calls=(ToolCall("respond", {"kind": "one_at_a_time", "text": None}),))
    step = next_step(session)
    if step is Step.INSURANCE and words in NOT_SURE:
        return Reading(calls=(ToolCall("confirm_insurance", {"status": "unsure"}),))
    if words in NOT_SURE:
        return Reading(calls=(ToolCall("respond", {"kind": "unsure", "text": None}),))
    if step is Step.PHARMACY:
        pharmacy = next((p for p in facts.pharmacies if words in pharmacy_names(p)), None)
        if pharmacy:
            return Reading(calls=(ToolCall("confirm_pharmacy", {"pharmacy_id": pharmacy["id"], "other_pharmacy": None}),))
        if session["wants_other_pharmacy"]:
            return None
        if QUESTION.search(text) and ANOTHER_PHARMACY.search(text):
            return Reading(calls=(ToolCall("confirm_pharmacy", {"pharmacy_id": None, "other_pharmacy": None}),))
    if step is Step.MEDICATION:
        chosen = next((p for p in facts.prescriptions if p["medication"].lower() == words), None)
        return Reading(calls=(ToolCall("select_medication", {"prescription_id": chosen["id"]}),)) if chosen else None
    if step is Step.CONFIRM and DONT_SEND.search(text) and not any(name in words for p in facts.pharmacies for name in pharmacy_names(p)):
        if PLAIN_YES.search(text):
            return Reading(calls=(ToolCall("respond", {"kind": "unclear", "text": None}),))
        return Reading(calls=(ToolCall("end_call", {"reason": "declined"}),))
    answer = "yes" if words in EXACT_YES else "no" if words in EXACT_NO else None
    call = EXACT_CALLS.get((step, answer))
    if call is None:
        return None
    name, arguments = call(facts)
    return Reading(calls=(ToolCall(name, arguments),))


async def read_message(history, note, tools):
    try:
        response = await client.messages.create(
            max_tokens=MAX_OUTPUT_TOKENS,
            tool_choice={"type": "any"},
            messages=[*history, {"role": "system", "content": note}],
            **request_settings(tools),
        )
    except anthropic.AuthenticationError:
        raise
    except anthropic.APITimeoutError:
        return Reading(error_code="llm_timeout")
    except anthropic.RateLimitError:
        return Reading(error_code="llm_rate_limited")
    except anthropic.APIConnectionError:
        return Reading(error_code="llm_connection")
    except anthropic.APIStatusError as error:
        return Reading(error_code=f"llm_status_{error.status_code}")
    if response.stop_reason not in ("tool_use", "max_tokens"):
        return Reading(error_code=f"llm_stop_{response.stop_reason}")
    blocks = [block for block in response.content if block.type == "tool_use"]
    if response.stop_reason == "max_tokens":
        blocks = blocks[:-1]
    return Reading(calls=tuple(ToolCall(block.name, block.input) for block in blocks))
