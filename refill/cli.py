import argparse
import asyncio
import sys

from refill.core import finish_call, handle_turn, open_session
from refill.db import init_db, load_session, transaction
from refill.steps import next_step

DEMO_CALLER_PHONE = "(555) 867-5309"
SESSION_FIELDS = (
    "channel", "status", "verified_at", "prescription_id", "pharmacy_id", "pharmacy_text", "insurance_status",
    "needs_pharmacist", "pharmacist_reason",
)  # fmt: skip


def print_state(session_id):
    with transaction() as conn:
        session = load_session(conn, session_id)
        request = conn.execute("SELECT * FROM refill_requests WHERE session_id = ?", (session_id,)).fetchone()
    print(f"\nnext step: {next_step(session).value}")
    print({field: session[field] for field in SESSION_FIELDS})
    print(f"refill request: {dict(request) if request else None}")


async def main():
    parser = argparse.ArgumentParser(description="Talk to the refill agent in the terminal.")
    parser.add_argument("--channel", choices=("voice", "sms"), default="voice")
    arguments = parser.parse_args()

    init_db()
    session_id, opening_line = open_session(DEMO_CALLER_PHONE, arguments.channel)
    print(f"agent: {opening_line}")
    for raw_line in sys.stdin:
        text = raw_line.strip()
        if text == "/drop":
            print(f"text: {finish_call(session_id)}")
            continue
        if text == "/state":
            print_state(session_id)
        elif text:
            if not sys.stdin.isatty():
                print(f"patient: {text}")
            reply = await handle_turn(session_id, text)
            print(f"agent: {reply.text}")
            if reply.ends_conversation:
                break
    print_state(session_id)


if __name__ == "__main__":
    asyncio.run(main())
