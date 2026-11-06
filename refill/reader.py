from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

from refill.guard import FIXED_ANSWERS
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
- select_medication: match the patient's words, including a purpose such as "the blood pressure one", to an id in \
the latest system note. Pass null if they name something not on the list. Do not pick the closest name.
- confirm_pharmacy: "yes" means the id of the pharmacy on file. Use another listed id, or other_pharmacy for one \
not listed. If the patient says no without naming a pharmacy, pass null for both.
- confirm_insurance: status is "current" if it is still right, "changed" if the patient says it changed, and \
"unsure" for "I don't know" or "I'm not sure".
- submit_request: only when the patient says yes to sending the request.
- request_pharmacist: use clinical_question for dose advice, side effects, interactions or symptoms, \
asked_for_person when they ask for a person or pharmacist, and urgent for emergency language such as chest pain, \
trouble breathing, overdose or self-harm.
- end_call: use patient_leaving for "I have to go" or "text me instead", and declined for "no" to sending the \
request, "never mind", or "no" to a pharmacist calling back.

respond, for everything else:
- kind "repeat" when the patient asks you to say something again, or to hear a price or detail again.
- kind "hold" only when the patient asks you to wait, such as "hold on" or "one second".
- kind "unsure" when the patient says they don't know or aren't sure about the pending question.
- kind "unclear" for background talk or anything you cannot understand.
- kind "answer" with text, only when the patient asked a question. Write one short plain statement, with no \
questions, no prices and no lists. What a medication on the list is for, its strength and how often it is taken \
are on record, so answer those from the latest system note.

Fixed answers, to be used word for word:
""" + "\n".join(f'- {situation}: "{answer}"' for situation, answer in FIXED_ANSWERS.items())

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
    lines = [f"Pending question: {PENDING_QUESTIONS[step]}."]
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
    lines.append(f"Insurance plan on file: {facts.insurance_plan}.")
    lines.append(f"Already recorded: {recorded_fields(session, facts) or 'nothing yet'}.")
    if just_verified:
        lines.append("The caller was just verified. Record the answers they gave anywhere in this conversation.")
    return "\n".join(lines)


async def read_message(history, note, tools):
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            tool_choice={"type": "any"},
            messages=[*history, {"role": "system", "content": note}],
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
