import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from refill.db import load_session, transaction, update_session, utc_now
from refill.guard import QUESTION, UNSURE, asks_to_send, cancel_check_answer
from refill.render import load_facts, pharmacy_written
from refill.steps import MAX_IDENTITY_FAILURES, Step, allowed_tools, checking_cancel, identity_locked
from refill.turn_model import IDENTITY_STARTED

NAME_MATCH_THRESHOLD = 0.8
MAX_PHARMACY_TEXT_LENGTH = 80
PENDING_TOOLS = {
    Step.MEDICATION: "select_medication",
    Step.PHARMACY: "confirm_pharmacy",
    Step.INSURANCE: "confirm_insurance",
}
AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yea|yep|yup|ya|y|k|kk|sure|ok|okay|please|correct|right|send|go ahead|do it|sounds good|fine|same"
    r"|still|perfect|great|absolutely|definitely|works|current|active|valid|hasn'?t changed|mm-?hm+|uh-?huh)\b|👍|✅",
    re.IGNORECASE,
)
NEGATIVE = re.compile(r"\b(no|nope|not|don't|dont|wait|stop|cancel|never ?mind|hold on|hang on)\b", re.IGNORECASE)
LEAVING = re.compile(
    r"\b(bye|goodbye|(have|got|gotta|need|must|going)( to)? (go|run|leave)|go now|text me|text instead|just text|leaving|later"
    r"|driving|busy|wrong number|hold off|forget it|nvm|nah|n|unsubscribe|quit|hang up|can'?t talk)\b",
    re.IGNORECASE,
)
CHANGE_REQUEST = re.compile(
    r"\b(change|different|another|other|wrong|go back|previous|switch|instead|start over|back up|main menu)\b",
    re.IGNORECASE,
)
ASKS_FOR_A_PERSON = re.compile(
    r"pharmacist|person|human|some(?:one|body)|any(?:one|body)|representative|operator|agent|manager|nurse|doctor|staff",
    re.IGNORECASE,
)
PLAIN_AGREEMENT = set(
    "yes yeah yep yup sure ok okay correct right fine good great please that that's thats this one is it it's its "
    "thanks thank you".split()
)
DOSE_SPOKEN = re.compile(r"(\d+)\s*(?:mg|milligrams?)\b", re.IGNORECASE)
PHARMACY_FILLER_WORDS = set("the on at in by one store pharmacy my street st avenue ave please now".split())
TOPIC_WORDS = {
    "confirm_pharmacy": re.compile(r"pharmac|drug ?store|cvs|walgreens|walmart|send it to", re.IGNORECASE),
    "confirm_insurance": re.compile(r"insurance|aetna|coverage|policy|\bplan\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class Outcome:
    code: str
    detail: str = ""


def tool(name, description, properties):
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    }


RESPOND_KINDS = ["answer", "repeat", "hold", "unsure", "unclear", "background"]
NULLABLE_TEXT = {"type": ["string", "null"]}
NULLABLE_INTEGER = {"type": ["integer", "null"]}

TOOL_DEFINITIONS = [
    tool(
        "verify_identity",
        "Check the caller's full name and date of birth. Pass null for anything not yet given.",
        {
            "full_name": NULLABLE_TEXT,
            "birth_year": NULLABLE_INTEGER,
            "birth_month": NULLABLE_INTEGER,
            "birth_day": NULLABLE_INTEGER,
        },
    ),
    tool(
        "select_medication",
        "Record the prescription the patient chose. Pass null if what they named is not on their list.",
        {"prescription_id": NULLABLE_INTEGER},
    ),
    tool(
        "confirm_pharmacy",
        "Record the pharmacy. Use pharmacy_id for a listed pharmacy, otherwise other_pharmacy with the patient's words.",
        {"pharmacy_id": NULLABLE_INTEGER, "other_pharmacy": NULLABLE_TEXT},
    ),
    tool(
        "confirm_insurance",
        "Record whether the insurance on file is still current, has changed, or the patient is unsure.",
        {"status": {"type": "string", "enum": ["current", "changed", "unsure"]}},
    ),
    tool(
        "change_answer",
        "Clear an answer so it can be given again, when the patient wants to change it without saying the new value.",
        {"field": {"type": "string", "enum": ["medication", "pharmacy", "insurance", "previous", "all"]}},
    ),
    tool("submit_request", "Send the refill request after the patient says yes to sending it.", {}),
    tool(
        "request_pharmacist",
        "Ask a pharmacist to call the patient back.",
        {"reason": {"type": "string", "enum": ["clinical_question", "asked_for_person", "urgent", "crisis"]}},
    ),
    tool(
        "end_call",
        "End the conversation at the patient's request.",
        {"reason": {"type": "string", "enum": ["patient_leaving", "declined"]}},
    ),
    tool(
        "respond",
        "Use when no other tool fits.",
        {"kind": {"type": "string", "enum": RESPOND_KINDS}, "text": NULLABLE_TEXT},
    ),
]


def definitions_for(names):
    return [definition for definition in TOOL_DEFINITIONS if definition["name"] in names]


def normalized_name(name):
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def expanded_year(year):
    if year >= 100:
        return year
    this_century = date.today().year % 100
    return (1900 if year > this_century else 2000) + year


def joined_name(name):
    return normalized_name(name).replace(" ", "")


def name_similarity(spoken, on_file):
    in_order = SequenceMatcher(None, normalized_name(spoken), normalized_name(on_file)).ratio()
    any_order = SequenceMatcher(None, sorted_name(spoken), sorted_name(on_file)).ratio()
    run_together = SequenceMatcher(None, joined_name(spoken), joined_name(on_file)).ratio()
    return max(in_order, any_order, run_together)


def sorted_name(name):
    return " ".join(sorted(normalized_name(name).split()))


def match_patient(conn, full_name, date_of_birth):
    rows = conn.execute("SELECT * FROM patients WHERE date_of_birth = ?", (date_of_birth.isoformat(),))
    for row in rows:
        if name_similarity(full_name, row["full_name"]) >= NAME_MATCH_THRESHOLD:
            return row
    return None


def verify_identity(conn, session, arguments):
    year, month, day = (arguments[part] for part in ("birth_year", "birth_month", "birth_day"))
    date_given = None not in (year, month, day)
    if not arguments["full_name"]:
        return [Outcome("missing_name" if date_given else "missing_identity")]
    one_name = len(normalized_name(arguments["full_name"]).split()) < 2
    if one_name and not date_given:
        return [Outcome("first_name_only")]
    if not date_given:
        return [Outcome("missing_year" if month and day else "missing_month_day" if year else "missing_date")]
    try:
        date_of_birth = date(expanded_year(year), month, day)
    except ValueError:
        return [Outcome("invalid_date")]

    patient = match_patient(conn, arguments["full_name"], date_of_birth)
    if patient is None and one_name:
        return [Outcome("first_name_only")]
    if patient is None:
        failures = session["identity_failures"] + 1
        update_session(conn, session["id"], identity_failures=failures)
        return [Outcome("identity_failed" if failures >= MAX_IDENTITY_FAILURES else "identity_mismatch")]

    update_session(conn, session["id"], patient_id=patient["id"], verified_at=utc_now())
    return [Outcome("verified")]


def select_medication(conn, session, arguments):
    prescription = conn.execute(
        "SELECT id FROM prescriptions WHERE id = ? AND patient_id = ? AND active = 1",
        (arguments["prescription_id"], session["patient_id"]),
    ).fetchone()
    if prescription is None:
        return [Outcome("not_on_list")]
    if prescription["id"] == session["prescription_id"]:
        return [Outcome("unchanged")]
    update_session(conn, session["id"], prescription_id=prescription["id"], price_stated_at=None)
    return [Outcome("selected")]


def listed_pharmacy_id(conn, text):
    """A chain name alone, or with its listed street, means the listed store. "CVS on State Street" does not."""
    words = set(normalized_name(text or "").split()) - PHARMACY_FILLER_WORDS
    for pharmacy in conn.execute("SELECT * FROM pharmacies"):
        chain = set(normalized_name(pharmacy["name"]).split()) - PHARMACY_FILLER_WORDS
        street = set(normalized_name(pharmacy["address"]).split()) - PHARMACY_FILLER_WORDS
        if words & chain and not words - chain - street:
            return pharmacy["id"]
    return None


def confirm_pharmacy(conn, session, arguments):
    pharmacy_id = arguments["pharmacy_id"] or listed_pharmacy_id(conn, arguments["other_pharmacy"])
    pharmacy = conn.execute("SELECT id FROM pharmacies WHERE id = ?", (pharmacy_id,)).fetchone()
    if pharmacy is not None:
        if pharmacy["id"] == session["pharmacy_id"]:
            return [Outcome("unchanged")]
        chosen = {**CLEARED_FIELDS["pharmacy"], "pharmacy_id": pharmacy["id"]}
        update_session(conn, session["id"], price_stated_at=None, **chosen)
        on_file = conn.execute("SELECT pharmacy_id FROM patients WHERE id = ?", (session["patient_id"],)).fetchone()
        return [Outcome("pharmacy_on_file" if pharmacy["id"] == on_file["pharmacy_id"] else "pharmacy_changed")]
    other_pharmacy = (arguments["other_pharmacy"] or "").strip()[:MAX_PHARMACY_TEXT_LENGTH]
    if not other_pharmacy:
        update_session(conn, session["id"], wants_other_pharmacy=1)
        return [Outcome("pharmacy_declined")]
    if other_pharmacy == session["pharmacy_text"]:
        return [Outcome("unchanged")]
    noted = {**CLEARED_FIELDS["pharmacy"], "pharmacy_text": other_pharmacy}
    update_session(conn, session["id"], price_stated_at=None, **noted)
    return [Outcome("pharmacy_noted", other_pharmacy)]


def confirm_insurance(conn, session, arguments):
    status = arguments["status"]
    if status == session["insurance_status"]:
        return [Outcome("unchanged")]
    update_session(conn, session["id"], insurance_status=status, price_stated_at=None)
    return [Outcome(f"insurance_{status}")]


def sent_since_the_text(conn, session):
    return session["sms_sent_at"] and conn.execute(
        "SELECT 1 FROM refill_requests WHERE patient_id = ? AND prescription_id = ? AND created_at > ?",
        (session["patient_id"], session["prescription_id"], session["sms_sent_at"]),
    ).fetchone()


def submit_request(conn, session, _arguments):
    if sent_since_the_text(conn, session):
        update_session(conn, session["id"], status="declined")
        return [Outcome("already_requested")]
    facts = load_facts(conn, session)
    needs_pharmacist = bool(
        session["needs_pharmacist"] or session["pharmacy_text"] or session["insurance_status"] != "current"
    )
    conn.execute(
        """
        INSERT INTO refill_requests (
            session_id, patient_id, prescription_id, medication, dosage, pharmacy,
            insurance_plan, member_id, estimated_copay_cents, needs_pharmacist, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO NOTHING
        """,
        (
            session["id"],
            session["patient_id"],
            session["prescription_id"],
            facts.chosen["medication"],
            facts.chosen["dosage"],
            pharmacy_written(facts),
            facts.insurance_plan,
            facts.member_id,
            facts.copay_cents,
            needs_pharmacist,
            utc_now(),
        ),
    )
    update_session(conn, session["id"], status="completed")
    return [Outcome("submitted")]


def request_pharmacist(conn, session, arguments):
    if arguments["reason"] == session["pharmacist_reason"]:
        return [Outcome("unchanged")]
    update_session(conn, session["id"], needs_pharmacist=1, pharmacist_reason=arguments["reason"])
    if session["identity_failures"] >= MAX_IDENTITY_FAILURES:
        update_session(conn, session["id"], status="handed_off")
    return [Outcome(arguments["reason"])]


def end_call(conn, session, arguments):
    locked = session["identity_failures"] >= MAX_IDENTITY_FAILURES
    declined = locked or arguments["reason"] == "declined"
    first_cancel_on_a_call = session["verified_at"] and session["channel"] == "voice" and not session["cancel_pending"]
    if declined and not locked and first_cancel_on_a_call:
        update_session(conn, session["id"], cancel_pending=1)
        return [Outcome("confirm_cancel")]
    update_session(conn, session["id"], status="declined" if declined else "dropped")
    return [Outcome(arguments["reason"])]


def keep_going(_conn, _session, _arguments):
    return [Outcome("keep_going")]


def change_answer(conn, session, arguments):
    requested = arguments["field"]
    if requested == "pharmacy":
        cleared = {**CLEARED_FIELDS["pharmacy"], "wants_other_pharmacy": 1}
        update_session(conn, session["id"], price_stated_at=None, **cleared)
        return [Outcome("pharmacy_declined")]
    if requested == "previous" and session["wants_other_pharmacy"]:
        update_session(conn, session["id"], wants_other_pharmacy=0)
        return [Outcome("answer_cleared")]
    answered = [field for field, value in ANSWERED_FIELDS.items() if value(session)]
    if not answered:
        return [Outcome("nothing_to_change")]
    if requested == "all":
        everything = {name: value for fields in CLEARED_FIELDS.values() for name, value in fields.items()}
        update_session(conn, session["id"], price_stated_at=None, **everything)
        return [Outcome("started_over")]
    field = answered[-1] if requested == "previous" else requested
    if field in answered:
        update_session(conn, session["id"], price_stated_at=None, **CLEARED_FIELDS[field])
    return [Outcome("answer_cleared")]


def respond(_conn, _session, arguments):
    return [Outcome(arguments["kind"], arguments["text"] or "")]


ANSWERED_FIELDS = {
    "medication": lambda session: session["prescription_id"],
    "pharmacy": lambda session: session["pharmacy_id"] or session["pharmacy_text"],
    "insurance": lambda session: session["insurance_status"],
}
CLEARED_FIELDS = {
    "medication": {"prescription_id": None},
    "pharmacy": {"pharmacy_id": None, "pharmacy_text": None, "wants_other_pharmacy": 0},
    "insurance": {"insurance_status": None},
}

HANDLERS = {
    "verify_identity": verify_identity,
    "select_medication": select_medication,
    "confirm_pharmacy": confirm_pharmacy,
    "confirm_insurance": confirm_insurance,
    "change_answer": change_answer,
    "submit_request": submit_request,
    "request_pharmacist": request_pharmacist,
    "end_call": end_call,
    "respond": respond,
    "keep_going": keep_going,
}
LEAVES_THE_CANCEL_CHECK = {
    "select_medication", "confirm_pharmacy", "confirm_insurance", "change_answer", "submit_request", "keep_going",
}  # fmt: skip


def names_prescription(conn, prescription_id, patient_text):
    prescription = conn.execute("SELECT * FROM prescriptions WHERE id = ?", (prescription_id,)).fetchone()
    text = patient_text.lower()
    return prescription is not None and (
        prescription["medication"].lower() in text or prescription["indication"].lower() in text
    )


def names_pharmacy(conn, pharmacy_id, patient_text):
    pharmacy = conn.execute("SELECT * FROM pharmacies WHERE id = ?", (pharmacy_id,)).fetchone()
    street = pharmacy["address"].split()[1].lower() if pharmacy else None
    return street is not None and street in patient_text.lower()


def states_different_dose(session_id, patient_text):
    """True when the patient gives a strength that is not the one on file, which a pharmacist should check."""
    with transaction() as conn:
        session = load_session(conn, session_id)
        chosen = conn.execute("SELECT dosage FROM prescriptions WHERE id = ?", (session["prescription_id"],)).fetchone()
    return chosen is not None and bool(set(DOSE_SPOKEN.findall(patient_text)) - set(re.findall(r"\d+", chosen["dosage"])))


def mentions_refill_details(session_id, patient_text):
    with transaction() as conn:
        patient_id = load_session(conn, session_id)["patient_id"]
        prescriptions = conn.execute("SELECT id FROM prescriptions WHERE patient_id = ?", (patient_id,)).fetchall()
        names_a_drug = any(names_prescription(conn, row["id"], patient_text) for row in prescriptions)
    return names_a_drug or any(words.search(patient_text) for words in TOPIC_WORDS.values())


def confirms_what_is_on_file(conn, session, name, arguments):
    if name == "confirm_insurance":
        return arguments["status"] == "current"
    on_file = conn.execute("SELECT pharmacy_id FROM patients WHERE id = ?", (session["patient_id"],)).fetchone()
    return arguments["pharmacy_id"] == on_file["pharmacy_id"]


def message_supports(conn, session, name, arguments, pending_step, patient_text):
    if name == "verify_identity":
        return not session["identity_failures"] or bool(IDENTITY_STARTED.search(patient_text))
    if name == "request_pharmacist" and arguments["reason"] == "asked_for_person":
        return identity_locked(session) or bool(ASKS_FOR_A_PERSON.search(patient_text))
    if name == "request_pharmacist" and arguments["reason"] == "clinical_question":
        return not set(re.findall(r"[a-z']+", patient_text.lower())) <= PLAIN_AGREEMENT
    if name == "end_call" and arguments["reason"] == "declined" and checking_cancel(session):
        return cancel_check_answer(patient_text) == "cancel"
    if name == "end_call":
        return identity_locked(session) or bool(NEGATIVE.search(patient_text) or LEAVING.search(patient_text))
    if name == "submit_request" and checking_cancel(session):
        return cancel_check_answer(patient_text) == "keep" and asks_to_send(patient_text)
    if name == "submit_request":
        return bool(AFFIRMATIVE.search(patient_text)) and not NEGATIVE.search(patient_text)
    if name == "change_answer":
        return bool(CHANGE_REQUEST.search(patient_text))
    if name not in ("select_medication", "confirm_pharmacy", "confirm_insurance"):
        return True
    is_pending = PENDING_TOOLS.get(pending_step) == name
    if is_pending and session["question_unheard"] and name in TOPIC_WORDS and not TOPIC_WORDS[name].search(patient_text):
        return False
    if name == "select_medication":
        return is_pending or names_prescription(conn, arguments["prescription_id"], patient_text)
    if name == "confirm_pharmacy" and arguments["pharmacy_id"] is None and not arguments["other_pharmacy"]:
        return is_pending and bool(NEGATIVE.search(patient_text) or CHANGE_REQUEST.search(patient_text))
    if name == "confirm_insurance" and QUESTION.search(patient_text):
        return False
    if name == "confirm_insurance" and arguments["status"] == "unsure":
        return is_pending and bool(UNSURE.search(patient_text))
    if not is_pending:
        return bool(TOPIC_WORDS[name].search(patient_text))
    if not confirms_what_is_on_file(conn, session, name, arguments):
        return True
    if name == "confirm_pharmacy" and session["wants_other_pharmacy"]:
        return bool(TOPIC_WORDS[name].search(patient_text))
    says_yes = (
        AFFIRMATIVE.search(patient_text)
        or TOPIC_WORDS[name].search(patient_text)
        or (name == "confirm_pharmacy" and names_pharmacy(conn, arguments["pharmacy_id"], patient_text))
    )
    return bool(says_yes) and not QUESTION.search(patient_text)


def declines_pharmacy(name, arguments):
    if name == "change_answer":
        return arguments["field"] == "pharmacy"
    return name == "confirm_pharmacy" and not arguments["pharmacy_id"] and not arguments["other_pharmacy"]


def pharmacy_named_instead(conn, session, patient_text):
    """The one listed pharmacy the patient asked for by name, unless that name follows a "no" or "not"."""
    text = patient_text.lower()
    named = [p for p in conn.execute("SELECT * FROM pharmacies") if p["name"].split()[0].lower() in text]
    if len(named) != 1 or named[0]["id"] == session["pharmacy_id"]:
        return None
    chain = named[0]["name"].split()[0].lower()
    return None if re.search(rf"\b(no|not|don'?t|isn'?t)\b[^,.]*\b{chain}", text) else named[0]["id"]


def run_tool(session_id, name, arguments, pending_step, patient_text):
    with transaction() as conn:
        session = load_session(conn, session_id)
        named = pharmacy_named_instead(conn, session, patient_text) if declines_pharmacy(name, arguments) else None
        if named:
            name, arguments = "confirm_pharmacy", {"pharmacy_id": named, "other_pharmacy": None}
        if name not in allowed_tools(session):
            return [Outcome("refused")]
        if not message_supports(conn, session, name, arguments, pending_step, patient_text):
            return [Outcome("refused")]
        if session["cancel_pending"] and name in LEAVES_THE_CANCEL_CHECK:
            update_session(conn, session["id"], cancel_pending=0)
            session = load_session(conn, session["id"])
        return HANDLERS[name](conn, session, arguments)
