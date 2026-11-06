"""Drop a call at every step, continue over SMS, and assert on the stored rows. Uses a temporary database."""
import argparse
import asyncio
import os
import tempfile
from pathlib import Path

os.environ["REFILL_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "sms_fallback.db")

from refill.core import finish_call, handle_turn, open_session, receive_text, text_thread  # noqa: E402
from refill.db import init_db, load_session, transaction  # noqa: E402
from refill.steps import next_step  # noqa: E402

PHONE = "(555) 867-5309"
IDENTITY = "Sarah Chen, March 15 1985"
VOICE_LINES = [IDENTITY, "lisinopril", "yes", "yes", "yes"]
ASKED_ALREADY = {
    1: ["full name", "date of birth"],
    2: ["full name", "Which medication", "Which one do you need"],
    3: ["full name", "Which one do you need", "Should I send it to", "Which pharmacy"],
    4: ["full name", "Which one do you need", "Should I send it to", "Which pharmacy", "still your insurance"],
}
DRUGS = ("Lisinopril", "Metformin", "Atorvastatin")


async def drop_at(answers):
    session_id, _ = open_session(PHONE, "voice")
    for text in VOICE_LINES[:answers]:
        await handle_turn(session_id, text)
    first = finish_call(session_id)
    repeat = finish_call(session_id)
    problems = []
    if repeat is not None:
        problems.append("a second text was sent")
    if not first or not first.startswith("Looks like we got disconnected!"):
        problems.append(f"bad first text: {first}")
    if answers == 0 and any(drug in first for drug in DRUGS):
        problems.append("named a drug before verification")
    sms_lines = VOICE_LINES[answers:]
    replies = [first]
    for text in sms_lines:
        reply = await receive_text(PHONE, text)
        with transaction() as conn:
            replies.append(conn.execute(
                "SELECT text FROM messages WHERE session_id = ? AND role = 'agent' ORDER BY id DESC LIMIT 1", (session_id,)
            ).fetchone()["text"])
        if reply.ends_conversation:
            break
    for line in replies:
        problems += [f"asked again: {line[:70]}" for phrase in ASKED_ALREADY.get(answers, []) if phrase in line]
    with transaction() as conn:
        session = load_session(conn, session_id)
        request = conn.execute("SELECT * FROM refill_requests WHERE session_id = ?", (session_id,)).fetchone()
        texts_sent = conn.execute(
            "SELECT count(*) AS n FROM messages WHERE session_id = ? AND channel = 'sms' AND role = 'agent' AND text LIKE 'Looks like%'",
            (session_id,),
        ).fetchone()["n"]
    if texts_sent != 1:
        problems.append(f"{texts_sent} opening texts stored")
    if session["status"] != "completed" or request is None:
        problems.append(f"not completed: {session['status']}, step {next_step(session).value}")
    elif (request["medication"], request["pharmacy"], request["estimated_copay_cents"]) != (
        "Lisinopril", "CVS Pharmacy, 1234 Main St", 500,
    ):
        problems.append(f"wrong request: {dict(request)}")
    return answers, first, problems


async def finished_calls_send_nothing():
    problems = []
    session_id, _ = open_session(PHONE, "voice")
    for text in VOICE_LINES:
        await handle_turn(session_id, text)
    if finish_call(session_id) is not None:
        problems.append("completed call sent a text")
    session_id, _ = open_session(PHONE, "voice")
    for text in ["Sarah Chen, March 16 1985", "March 17 1985"]:
        await handle_turn(session_id, text)
    if finish_call(session_id) is not None:
        problems.append("locked call sent a text")
    return problems


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    init_db()
    failures = total = 0
    for run in range(arguments.runs):
        for answers in range(5):
            total += 1
            step, first, problems = await drop_at(answers)
            if run == 0:
                print(f"after {step} answers: {first}")
            if problems:
                failures += 1
                print(f"  FAIL after {step} answers: {problems}")
    edge = await finished_calls_send_nothing()
    print(f"\npassed {total - failures}/{total} drop runs | finished calls: {edge or 'no text sent'}")
    print("thread lookup:", len(text_thread(PHONE, 0)), "messages in the latest thread")


if __name__ == "__main__":
    asyncio.run(main())
