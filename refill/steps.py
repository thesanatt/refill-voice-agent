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


def allowed_tools(session):
    always = {"request_pharmacist", "end_call", "respond"}
    if identity_locked(session):
        return always
    if not session["verified_at"]:
        return always | {"verify_identity"}
    field_tools = {"select_medication", "confirm_pharmacy", "confirm_insurance"}
    if next_step(session) is Step.CONFIRM and session["price_stated_at"]:
        return always | field_tools | {"submit_request"}
    return always | field_tools
