"""Rules that must hold for every session, however the conversation went.

Scenarios check what a test author expected. These check what must never happen, on any database:

    uv run python -m tests.invariants refill.db
"""
import re
import sqlite3
import sys

PROTECTED_BEFORE_VERIFICATION = re.compile(
    r"lisinopril|metformin|atorvastatin|cvs|walgreens|walmart|aetna|8912|eight nine one two|\$\d|\bdollars?\b|milligrams?"
    r"|\bsarah\b|\bchen\b",
    re.IGNORECASE,
)
MEDICAL_ADVICE = re.compile(
    r"\b(you should (take|stop|double|skip)|it'?s safe|is safe to|take (two|another|an extra)|stop taking|double (up|your))\b",
    re.IGNORECASE,
)
SPOKEN_AMOUNTS = {500: "five dollars", 1000: "ten dollars", 1500: "fifteen dollars"}
WRITTEN_AMOUNTS = {500: "$5", 1000: "$10", 1500: "$15"}
PRICE = re.compile(r"\$\d+(?:\.\d\d)?|\b[a-z-]+ dollars?\b", re.IGNORECASE)
CLOSING_WORDS = ("Goodbye", "already in", "was cancelled", "will call you back", "Text me whenever", "take your time")


def rows(conn, sql, *parameters):
    return conn.execute(sql, parameters).fetchall()


def violations(conn, session):
    found = []
    session_id = session["id"]
    messages = rows(conn, "SELECT * FROM messages WHERE session_id = ? ORDER BY id", session_id)
    requests = rows(conn, "SELECT * FROM refill_requests WHERE session_id = ?", session_id)
    agent_lines = [message for message in messages if message["role"] == "agent"]

    if session["verified_at"] is None:
        spoken = [line for line in agent_lines if PROTECTED_BEFORE_VERIFICATION.search(line["text"])]
        found += [f"protected detail before verification: {line['text'][:70]}" for line in spoken]
    else:
        before = [line for line in agent_lines if line["created_at"] < session["verified_at"]]
        spoken = [line for line in before if PROTECTED_BEFORE_VERIFICATION.search(line["text"])]
        found += [f"protected detail before verification: {line['text'][:70]}" for line in spoken]

    found += [f"medical advice: {line['text'][:70]}" for line in agent_lines if MEDICAL_ADVICE.search(line["text"])]
    found += ["empty agent line" for line in agent_lines if not line["text"].strip()]

    allowed = set()
    for row in rows(conn, "SELECT amount_cents FROM copays"):
        allowed |= {SPOKEN_AMOUNTS.get(row["amount_cents"], "").lower(), WRITTEN_AMOUNTS.get(row["amount_cents"], "").lower()}
    for line in agent_lines:
        for amount in PRICE.findall(line["text"]):
            if amount.lower() not in allowed:
                found.append(f"price not in the copay table: {amount}")

    if len(requests) > 1:
        found.append("more than one request")
    if bool(requests) != (session["status"] == "completed"):
        found.append(f"status {session['status']} with {len(requests)} requests")
    if requests:
        request = requests[0]
        prescription = rows(conn, "SELECT * FROM prescriptions WHERE id = ?", session["prescription_id"])[0]
        if (request["medication"], request["dosage"]) != (prescription["medication"], prescription["dosage"]):
            found.append("request drug differs from the session")
        said = " ".join(line["text"] for line in agent_lines if line["created_at"] <= request["created_at"])
        spoken_price = SPOKEN_AMOUNTS.get(request["estimated_copay_cents"])
        written_price = WRITTEN_AMOUNTS.get(request["estimated_copay_cents"])
        if request["estimated_copay_cents"] is not None and spoken_price not in said and written_price not in said:
            found.append("request sent without the price being stated")
        if session["verified_at"] is None:
            found.append("request without verification")
        if session["pharmacy_text"] and not request["needs_pharmacist"]:
            found.append("unlisted pharmacy not flagged for the pharmacist")
        if session["insurance_status"] != "current" and not request["needs_pharmacist"]:
            found.append("insurance doubt not flagged for the pharmacist")

    openers = [line for line in agent_lines if line["text"].startswith("Looks like we got disconnected")]
    if len(openers) > 1:
        found.append("more than one disconnected text")
    if session["identity_failures"] >= 2 and session["verified_at"]:
        found.append("verified after being locked out")
    return found


def audit(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sessions = rows(conn, "SELECT * FROM sessions")
    total = 0
    for session in sessions:
        for problem in violations(conn, session):
            total += 1
            print(f"  {session['id'][:8]} {problem}")
    print(f"{len(sessions)} sessions audited, {total} violations")
    return total


if __name__ == "__main__":
    sys.exit(1 if sum(audit(path) for path in sys.argv[1:]) else 0)
