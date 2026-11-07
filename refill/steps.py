from enum import StrEnum

MAX_IDENTITY_FAILURES = 2
FINISHED_STATUSES = ("completed", "declined", "handed_off")


class Step(StrEnum):
    IDENTIFY = "identify"
    MEDICATION = "medication"
    PHARMACY = "pharmacy"
    INSURANCE = "insurance"
    CONFIRM = "confirm"
    DONE = "done"


def next_step(session):
    if session["status"] in FINISHED_STATUSES:
        return Step.DONE
    if not session["verified_at"]:
        return Step.IDENTIFY
    if not session["prescription_id"]:
        return Step.MEDICATION
    if not (session["pharmacy_id"] or session["pharmacy_text"]):
        return Step.PHARMACY
    if not session["insurance_status"]:
        return Step.INSURANCE
    return Step.CONFIRM


def identity_locked(session):
    return session["identity_failures"] >= MAX_IDENTITY_FAILURES


def checking_cancel(session):
    """A cancel on a call waits for one more answer, with keys offered (D39)."""
    return bool(session["cancel_pending"]) and session["channel"] == "voice"


def allowed_tools(session):
    always = {"request_pharmacist", "end_call", "respond"}
    if identity_locked(session):
        return always
    if not session["verified_at"]:
        return always | {"verify_identity"}
    field_tools = {"select_medication", "confirm_pharmacy", "confirm_insurance", "change_answer"}
    tools = always | field_tools
    if next_step(session) is Step.CONFIRM and session["price_stated_at"]:
        tools |= {"submit_request"}
    return (tools | {"keep_going"}) if checking_cancel(session) else tools
