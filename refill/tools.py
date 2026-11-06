import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from refill.db import load_session, transaction, update_session, utc_now
from refill.guard import QUESTION, UNSURE
from refill.render import load_facts, pharmacy_written
from refill.steps import MAX_IDENTITY_FAILURES, Step, allowed_tools

NAME_MATCH_THRESHOLD = 0.85
MAX_PHARMACY_TEXT_LENGTH = 80
PENDING_TOOLS = {
    Step.MEDICATION: "select_medication",
    Step.PHARMACY: "confirm_pharmacy",
    Step.INSURANCE: "confirm_insurance",
}
AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|ya|sure|ok|okay|please|correct|right|send|go ahead|do it|sounds good|fine|same|still"
    r"|perfect|great|absolutely|definitely|works|mm-?hm+|uh-?huh)\b",
    re.IGNORECASE,
)
NEGATIVE = re.compile(r"\b(no|nope|not|don't|dont|wait|stop|cancel|never ?mind|hold on|hang on)\b", re.IGNORECASE)
CHANGE_REQUEST = re.compile(
    r"\b(change|different|another|other|wrong|go back|previous|switch|instead|start over|back up|main menu)\b",
    re.IGNORECASE,
)
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


RESPOND_KINDS = ["answer", "repeat", "hold", "unsure", "unclear"]
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
        {"reason": {"type": "string", "enum": ["clinical_question", "asked_for_person", "urgent"]}},
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


def name_similarity(spoken, on_file):
    in_order = SequenceMatcher(None, normalized_name(spoken), normalized_name(on_file)).ratio()
    any_order = SequenceMatcher(None, sorted_name(spoken), sorted_name(on_file)).ratio()
    return max(in_order, any_order)


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
    if len(normalized_name(arguments["full_name"]).split()) < 2:
        return [Outcome("first_name_only")]
    if not date_given:
        return [Outcome("missing_date")]
    try:
        date_of_birth = date(expanded_year(year), month, day)
    except ValueError:
        return [Outcome("invalid_date")]

    patient = match_patient(conn, arguments["full_name"], date_of_birth)
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


def confirm_pharmacy(conn, session, arguments):
    pharmacy = conn.execute("SELECT id FROM pharmacies WHERE id = ?", (arguments["pharmacy_id"],)).fetchone()
    if pharmacy is not None:
        if pharmacy["id"] == session["pharmacy_id"]:
            return [Outcome("unchanged")]
        update_session(conn, session["id"], pharmacy_id=pharmacy["id"], pharmacy_text=None, price_stated_at=None)
        on_file = conn.execute("SELECT pharmacy_id FROM patients WHERE id = ?", (session["patient_id"],)).fetchone()
        return [Outcome("pharmacy_on_file" if pharmacy["id"] == on_file["pharmacy_id"] else "pharmacy_changed")]
    other_pharmacy = (arguments["other_pharmacy"] or "").strip()[:MAX_PHARMACY_TEXT_LENGTH]
    if not other_pharmacy:
        return [Outcome("pharmacy_declined")]
    if other_pharmacy == session["pharmacy_text"]:
        return [Outcome("unchanged")]
    update_session(conn, session["id"], pharmacy_id=None, pharmacy_text=other_pharmacy, price_stated_at=None)
    return [Outcome("pharmacy_noted", other_pharmacy)]


def confirm_insurance(conn, session, arguments):
    status = arguments["status"]
    if status == session["insurance_status"]:
        return [Outcome("unchanged")]
    update_session(conn, session["id"], insurance_status=status, price_stated_at=None)
    return [Outcome(f"insurance_{status}")]


def submit_request(conn, session, _arguments):
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
    update_session(conn, session["id"], status="declined" if locked or arguments["reason"] == "declined" else "dropped")
    return [Outcome(arguments["reason"])]


def change_answer(conn, session, arguments):
    answered = [field for field, value in ANSWERED_FIELDS.items() if value(session)]
    if not answered:
        return [Outcome("nothing_to_change")]
    if arguments["field"] == "all":
        everything = {name: None for fields in CLEARED_FIELDS.values() for name in fields}
        update_session(conn, session["id"], price_stated_at=None, **everything)
        return [Outcome("started_over")]
    field = answered[-1] if arguments["field"] == "previous" else arguments["field"]
    if field not in answered:
        return [Outcome("unchanged")]
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
    "pharmacy": {"pharmacy_id": None, "pharmacy_text": None},
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
}


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
    if name == "submit_request":
        return bool(AFFIRMATIVE.search(patient_text)) and not NEGATIVE.search(patient_text)
    if name == "change_answer":
        return bool(CHANGE_REQUEST.search(patient_text))
    if name not in ("select_medication", "confirm_pharmacy", "confirm_insurance"):
        return True
    is_pending = PENDING_TOOLS.get(pending_step) == name
    if name == "select_medication":
        return is_pending or names_prescription(conn, arguments["prescription_id"], patient_text)
    if name == "confirm_pharmacy" and arguments["pharmacy_id"] is None and not arguments["other_pharmacy"]:
        return is_pending and bool(NEGATIVE.search(patient_text))
    if name == "confirm_insurance" and QUESTION.search(patient_text):
        return False
    if name == "confirm_insurance" and arguments["status"] == "unsure":
        return is_pending and bool(UNSURE.search(patient_text))
    if not is_pending:
        return bool(TOPIC_WORDS[name].search(patient_text))
    if not confirms_what_is_on_file(conn, session, name, arguments):
        return True
    says_yes = (
        AFFIRMATIVE.search(patient_text)
        or TOPIC_WORDS[name].search(patient_text)
        or (name == "confirm_pharmacy" and names_pharmacy(conn, arguments["pharmacy_id"], patient_text))
    )
    return bool(says_yes) and not QUESTION.search(patient_text)


def run_tool(session_id, name, arguments, pending_step, patient_text):
    with transaction() as conn:
        session = load_session(conn, session_id)
        if name not in allowed_tools(session):
            return [Outcome("refused")]
        if not message_supports(conn, session, name, arguments, pending_step, patient_text):
            return [Outcome("refused")]
        return HANDLERS[name](conn, session, arguments)
