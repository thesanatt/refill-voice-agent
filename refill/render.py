from dataclasses import dataclass

from refill.guard import CANNOT_ANSWER, UNSURE, asked_something, safe_answer
from refill.steps import Step, identity_locked, next_step

AGENT_NAME = "Demo Pharmacy"
ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
STREET_WORDS = {"St": "Street", "Ave": "Avenue", "Rd": "Road", "Blvd": "Boulevard"}
ACKNOWLEDGEMENT = "Great."
IDENTITY_FOLLOW_UPS = {
    "missing_name": "And your full name?",
    "first_name_only": "And your last name?",
    "missing_date": "And your date of birth?",
    "missing_year": "And what year were you born?",
    "missing_month_day": "And what month and day were you born?",
    "invalid_date": "That date didn't sound right. Can you say your date of birth again?",
}
NO_PROGRESS_OUTCOMES = {"unclear", "background", "refused", "unchanged", "repeat", "unsure", *IDENTITY_FOLLOW_UPS}


@dataclass(frozen=True)
class Facts:
    first_name: str | None = None
    prescriptions: tuple = ()
    chosen: dict | None = None
    pharmacy_on_file: dict | None = None
    pharmacy: dict | None = None
    pharmacy_text: str | None = None
    pharmacies: tuple = ()
    insurance_plan: str | None = None
    member_id: str | None = None
    copay_cents: int | None = None


@dataclass(frozen=True)
class Composed:
    text: str
    asked_pending: bool
    blocked_answer: bool = False
    holding: bool = False


def load_facts(conn, session):
    if session["patient_id"] is None:
        return Facts()
    patient = conn.execute("SELECT * FROM patients WHERE id = ?", (session["patient_id"],)).fetchone()
    prescriptions = conn.execute(
        "SELECT * FROM prescriptions WHERE patient_id = ? AND active = 1 ORDER BY id", (patient["id"],)
    ).fetchall()
    pharmacies = conn.execute("SELECT * FROM pharmacies ORDER BY id").fetchall()
    by_id = {pharmacy["id"]: pharmacy for pharmacy in pharmacies}
    chosen = next((p for p in prescriptions if p["id"] == session["prescription_id"]), None)
    copay = None
    if chosen is not None and session["insurance_status"] in ("current", "unsure"):
        copay = conn.execute(
            "SELECT amount_cents FROM copays WHERE insurance_plan = ? AND medication = ?",
            (patient["insurance_plan"], chosen["medication"]),
        ).fetchone()
    return Facts(
        first_name=patient["full_name"].split()[0],
        prescriptions=tuple(prescriptions),
        chosen=chosen,
        pharmacy_on_file=by_id[patient["pharmacy_id"]],
        pharmacy=by_id.get(session["pharmacy_id"]),
        pharmacy_text=session["pharmacy_text"],
        pharmacies=tuple(pharmacies),
        insurance_plan=patient["insurance_plan"],
        member_id=patient["member_id"],
        copay_cents=copay["amount_cents"] if copay else None,
    )


def number_words(number):
    if number >= 100:
        return str(number)
    if number < 20:
        return ONES[number]
    tens, ones = divmod(number, 10)
    return TENS[tens] if not ones else f"{TENS[tens]}-{ONES[ones]}"


def dollars(cents, channel):
    whole, remainder = divmod(cents, 100)
    if channel == "sms":
        return f"${whole}.{remainder:02d}" if remainder else f"${whole}"
    spoken = f"{number_words(whole)} dollar{'' if whole == 1 else 's'}"
    return f"{spoken} and {number_words(remainder)} cents" if remainder else spoken


def dosage_text(dosage, channel):
    return dosage if channel == "sms" else dosage.replace("mg", " milligrams")


def drug_with_dosage(prescription, channel):
    return f"{prescription['medication']} {dosage_text(prescription['dosage'], channel)}"


def spoken_street(address):
    words = address.split()[1:]
    return " ".join(STREET_WORDS.get(word, word) for word in words)


def pharmacy_label(pharmacy, channel):
    if channel == "sms":
        return f"{pharmacy['name']}, {pharmacy['address']}"
    return f"{pharmacy['name']} on {spoken_street(pharmacy['address'])}"


def pharmacy_written(facts):
    return pharmacy_label(facts.pharmacy, "sms") if facts.pharmacy else facts.pharmacy_text


def chosen_pharmacy(facts, channel):
    return pharmacy_label(facts.pharmacy, channel) if facts.pharmacy else facts.pharmacy_text


def member_id_ending(facts, channel):
    last_four = facts.member_id[-4:]
    return last_four if channel == "sms" else " ".join(ONES[int(digit)] for digit in last_four)


def joined(items, conjunction):
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def medication_list(facts, channel):
    if channel == "sms":
        return joined([f"{p['medication']} ({p['indication']})" for p in facts.prescriptions], "or")
    return joined([f"{p['medication']} for {p['indication']}" for p in facts.prescriptions], "and")


def confirm_line(facts, channel):
    if facts.copay_cents is None:
        cost = "I can't give you a price for that, so the pharmacy will confirm the cost."
    else:
        cost = f"Your estimated copay is {dollars(facts.copay_cents, channel)}."
    summary = f"That's {drug_with_dosage(facts.chosen, channel)} to {chosen_pharmacy(facts, channel)}."
    return f"{cost} {summary} Should I send that in?"


def other_pharmacies_question(facts, channel):
    others = [pharmacy_label(p, channel) for p in facts.pharmacies if p["id"] != facts.pharmacy_on_file["id"]]
    return f"Which pharmacy would you like instead? I have {joined(others, 'and')}, or you can name another one."


def greeting():
    return (
        f"Hi, I'm the AI assistant for {AGENT_NAME}. You can ask for a pharmacist at any time. "
        "What's your full name and date of birth?"
    )


def pending_question(session, facts, list_already_read=False):
    channel = session["channel"]
    step = next_step(session)
    if step is Step.IDENTIFY:
        if identity_locked(session):
            return "I'm not able to verify that. Would you like a pharmacist to call you back?"
        if session["identity_failures"]:
            return "Can you tell me your date of birth again?"
        return "What's your full name and date of birth?"
    if step is Step.MEDICATION and list_already_read:
        return "Which one do you need? I can read the list again."
    if step is Step.MEDICATION:
        return f"I see {medication_list(facts, channel)}. Which one do you need?"
    if step is Step.PHARMACY and session["wants_other_pharmacy"]:
        return other_pharmacies_question(facts, channel)
    if step is Step.PHARMACY:
        return f"Should I send it to {pharmacy_label(facts.pharmacy_on_file, channel)}?"
    if step is Step.INSURANCE:
        ending = member_id_ending(facts, channel)
        return f"Is {facts.insurance_plan}, ID ending in {ending}, still your insurance?"
    return confirm_line(facts, channel)


def lead_for(outcome, session, facts):
    channel = session["channel"]
    match outcome.code:
        case "verified":
            return f"Thanks, {facts.first_name}."
        case "selected":
            return f"{drug_with_dosage(facts.chosen, channel)}, got it."
        case "not_on_list":
            return "I don't see that one on your list, but a pharmacist can help with it."
        case "pharmacy_on_file":
            return ACKNOWLEDGEMENT
        case "pharmacy_changed":
            return f"Okay, {pharmacy_label(facts.pharmacy, channel)}."
        case "pharmacy_noted":
            return f"Okay, I'll note {outcome.detail}. The pharmacy will arrange the transfer."
        case "insurance_changed":
            return "Thanks, I'll flag that for the pharmacy."
        case "insurance_unsure":
            return "No problem, the pharmacy can check that."
        case "unsure":
            return "That's okay."
        case "answer_cleared":
            return "No problem."
        case "started_over":
            return "No problem, let's start again."
        case "nothing_to_change":
            return "We're at the first question."
        case "one_at_a_time":
            return "I can do one refill at a time, so let's start with one."
        case "identity_mismatch":
            return "I couldn't match that."
        case "clinical_question":
            return "I can't advise on that, but I've asked a pharmacist to call you back."
        case "asked_for_person":
            return "I've asked a pharmacist to call you back."
    return None


def first_text(session, facts):
    """The opening text after a dropped call. It names no drug until the session is verified."""
    step = next_step(session)
    opening = "Looks like we got disconnected!"
    if step is Step.IDENTIFY:
        return f"{opening} What's your full name and date of birth?"
    if step is Step.MEDICATION:
        return f"{opening} Which medication would you like to refill: {medication_list(facts, 'sms')}?"
    refill = f"I have your {facts.chosen['medication']} refill started."
    if step is Step.PHARMACY and not session["wants_other_pharmacy"]:
        on_file = pharmacy_label(facts.pharmacy_on_file, "sms")
        return f"{opening} {refill} Which pharmacy would you like to use? I have {on_file} on file."
    if step is Step.CONFIRM:
        return f"{opening} {pending_question(session, facts)}"
    return f"{opening} {refill} {pending_question(session, facts)}"


def closing_line(session):
    if session["channel"] == "sms" and session["status"] == "dropped":
        return "No problem. Text me whenever you're ready to finish."
    match session["status"]:
        case "completed":
            return "Your request is in, and the pharmacy will take it from here. Goodbye."
        case "declined":
            return "No problem, I won't send it. Goodbye." if session["verified_at"] else "No problem. Goodbye."
        case "handed_off":
            return "A pharmacist will call you back. Goodbye."
    return "No problem, I'll text you so we can finish. Goodbye."


def after_close_line(session, facts):
    match session["status"]:
        case "completed":
            return f"Your {facts.chosen['medication']} request is already in. The pharmacy will take it from here."
        case "declined":
            return "That request was cancelled. Call us any time to start again."
    return "A pharmacist will call you back."


def trouble_hint(session):
    if session["channel"] == "voice" and session["verified_at"]:
        return "You can also press star to go back, or pound for the main menu."
    return "You can also ask for a pharmacist."


def reply_to_no_reading(patient_text, codes):
    if UNSURE.search(patient_text):
        return "That's okay."
    if "unclear" in codes and asked_something(patient_text):
        return CANNOT_ANSWER
    return "Sorry, I didn't catch that."


def compose_reply(session, facts, outcomes, known_facts, patient_text, list_already_read=False):
    codes = [outcome.code for outcome in outcomes]
    if "already_requested" in codes:
        line = f"Your {facts.chosen['medication']} request is already in, so I won't send another. Goodbye."
        return Composed(line, asked_pending=False)
    if session["status"] != "active":
        return Composed(closing_line(session), asked_pending=False)
    if "urgent" in codes:
        line = "If this is an emergency, please hang up and call 911. I've also asked a pharmacist to call you back."
        return Composed(line, asked_pending=False)
    if "crisis" in codes:
        line = (
            "I'm so sorry you're going through this. Please call or text 988 now, or call 911 if you're in danger. "
            "I've also asked a pharmacist to call you back."
        )
        return Composed(line, asked_pending=False)
    if "hold" in codes:
        return Composed("Sure, take your time.", asked_pending=False, holding=True)
    if "background" in codes and session["channel"] == "voice":
        if session["question_unheard"]:
            return Composed(pending_question(session, facts, list_already_read), asked_pending=True)
        return Composed("", asked_pending=False)
    follow_up = next((IDENTITY_FOLLOW_UPS[code] for code in codes if code in IDENTITY_FOLLOW_UPS), None)
    if follow_up:
        return Composed(follow_up, asked_pending=False)

    lines, blocked_answer = [], False
    for outcome in outcomes:
        if outcome.code == "answer":
            answer = safe_answer(outcome.detail, known_facts, bool(session["verified_at"]), patient_text)
            blocked_answer = answer != outcome.detail.strip()
            lines.append(answer)
        else:
            lines.append(lead_for(outcome, session, facts))
    lines = [line for line in lines if line]
    if len(lines) > 1 and ACKNOWLEDGEMENT in lines:
        lines.remove(ACKNOWLEDGEMENT)
    price_unheard = next_step(session) is Step.CONFIRM and not session["price_stated_at"]
    no_progress = all(code in NO_PROGRESS_OUTCOMES or code == "answer" for code in codes)
    if "refused" in codes and no_progress and (price_unheard or session["question_unheard"]):
        if session["unclear_turns"] < 2:
            lines.append("Let me finish the details first.")
    elif not lines and "repeat" not in codes and no_progress:
        lines.append(reply_to_no_reading(patient_text, codes))
    read_list_again = "repeat" in codes or "unsure" in codes
    lines.append(pending_question(session, facts, list_already_read and not read_list_again))
    if session["unclear_turns"] >= 2:
        lines.append(trouble_hint(session))
    return Composed(" ".join(lines), asked_pending=True, blocked_answer=blocked_answer)
