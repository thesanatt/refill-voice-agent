import asyncio
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from refill.db import add_message, load_session, transaction, update_session, utc_now
from refill.reader import OPENING_MESSAGE, exact_reading, read_message, state_note
from refill.render import (
    NO_PROGRESS_OUTCOMES,
    after_close_line,
    compose_reply,
    first_text,
    greeting,
    load_facts,
    pending_question,
)
from refill.steps import FINISHED_STATUSES, Step, allowed_tools, identity_locked, next_step
from refill.tools import definitions_for, mentions_refill_details, run_tool

LAST_EXCHANGE_MESSAGES = 3
NO_OPEN_REQUEST_TEXT = "I don't have an open request for this number. Please call us to start a refill."
IDENTITY_FAILURE_LINES = ("I couldn't match that", "I'm not able to verify that")

session_locks = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class Reply:
    text: str
    step: Step = Step.DONE
    ends_conversation: bool = False
    holding: bool = False
    advanced: bool = True


@dataclass(frozen=True)
class Turn:
    step: str
    history: list
    note: str
    tools: list
    exact: object = None


def open_session(caller_phone, channel):
    session_id = uuid4().hex
    opening_line = greeting()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (id, channel, caller_phone, created_at) VALUES (?, ?, ?, ?)",
            (session_id, channel, caller_phone, utc_now()),
        )
        add_message(conn, session_id, channel, "agent", opening_line)
    return session_id, opening_line


def send_text(conn, session_id, text):
    add_message(conn, session_id, "sms", "agent", text)


def finish_call(session_id):
    """End a call however it ended. Safe to run twice, and sends at most one text."""
    with transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'dropped' WHERE id = ? AND status = 'active'", (session_id,))
        session = load_session(conn, session_id)
        if session["status"] != "dropped":
            return None
        if identity_locked(session):
            update_session(conn, session_id, needs_pharmacist=1)
            return None
        claimed = conn.execute(
            "UPDATE sessions SET sms_sent_at = ?, channel = 'sms' WHERE id = ? AND sms_sent_at IS NULL",
            (utc_now(), session_id),
        ).rowcount
        if not claimed:
            return None
        session = load_session(conn, session_id)
        text = first_text(session, load_facts(conn, session))
        if next_step(session) is Step.CONFIRM:
            update_session(conn, session_id, price_stated_at=utc_now())
        send_text(conn, session_id, text)
        return text


async def close_call(session_id):
    async with session_locks[session_id]:
        return await asyncio.to_thread(finish_call, session_id)


def latest_text_session(phone):
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT id FROM sessions WHERE caller_phone = ? AND sms_sent_at IS NOT NULL
            ORDER BY sms_sent_at DESC LIMIT 1
            """,
            (phone,),
        ).fetchone()
    return row["id"] if row else None


def text_thread(phone, after_id):
    session_id = latest_text_session(phone)
    if session_id is None:
        return []
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, role, text FROM messages
            WHERE session_id = ? AND channel = 'sms' AND id > ? ORDER BY id
            """,
            (session_id, after_id),
        ).fetchall()
    return [dict(row) for row in rows]


async def receive_text(phone, text):
    session_id = await asyncio.to_thread(latest_text_session, phone)
    if session_id is None:
        return Reply(NO_OPEN_REQUEST_TEXT, ends_conversation=True)
    return await handle_turn(session_id, text)


def mark_reply_unheard(session_id):
    with transaction() as conn:
        session = load_session(conn, session_id)
        last_line = last_agent_lines(conn, session_id, 1)[0]
        attempt_went_unheard = last_line.startswith(IDENTITY_FAILURE_LINES)
        failures = max(0, session["identity_failures"] - attempt_went_unheard)
        update_session(conn, session_id, price_stated_at=None, identity_failures=failures)
        conn.execute(
            """
            UPDATE messages SET interrupted = 1
            WHERE id = (SELECT max(id) FROM messages WHERE session_id = ? AND role = 'agent')
            """,
            (session_id,),
        )


def last_agent_lines(conn, session_id, count):
    rows = conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND role = 'agent' ORDER BY id DESC LIMIT ?",
        (session_id, count),
    )
    return [row["text"] for row in rows]


def pending_line(session_id):
    with transaction() as conn:
        session = load_session(conn, session_id)
        return pending_question(session, load_facts(conn, session))


def refill_keyterms(session_id):
    with transaction() as conn:
        facts = load_facts(conn, load_session(conn, session_id))
    drugs = [prescription["medication"] for prescription in facts.prescriptions]
    pharmacies = [pharmacy["name"] for pharmacy in facts.pharmacies]
    return [*drugs, *pharmacies, facts.insurance_plan]


def record_first_audio(session_id, milliseconds):
    with transaction() as conn:
        conn.execute(
            """
            UPDATE turn_log SET first_audio_ms = ?
            WHERE id = (SELECT max(id) FROM turn_log WHERE session_id = ?)
            """,
            (milliseconds, session_id),
        )


def conversation_history(conn, session_id, last_exchange_only=False):
    rows = conn.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    messages = [{"role": "assistant" if r["role"] == "agent" else "user", "content": r["text"]} for r in rows]
    if last_exchange_only:
        return messages[-LAST_EXCHANGE_MESSAGES:]
    return [{"role": "user", "content": OPENING_MESSAGE}, *messages]


def load_turn(conn, session, text=None, just_verified=False):
    facts = load_facts(conn, session)
    return Turn(
        step=next_step(session).value,
        history=conversation_history(conn, session["id"], bool(session["verified_at"]) and not just_verified),
        note=state_note(session, facts, just_verified),
        tools=definitions_for(allowed_tools(session)),
        exact=exact_reading(session, facts, text) if text else None,
    )


def begin_turn(session_id, text):
    with transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'active' WHERE id = ? AND status = 'dropped'", (session_id,))
        session = load_session(conn, session_id)
        add_message(conn, session_id, session["channel"], "patient", text)
        if session["status"] in FINISHED_STATUSES:
            line = after_close_line(session, load_facts(conn, session))
            add_message(conn, session_id, session["channel"], "agent", line)
            return Reply(line, ends_conversation=True)
        return load_turn(conn, session, text)


def turn_after_verification(session_id):
    with transaction() as conn:
        return load_turn(conn, load_session(conn, session_id), just_verified=True)


def everything_the_patient_said(session_id):
    with transaction() as conn:
        rows = conn.execute("SELECT text FROM messages WHERE session_id = ? AND role = 'patient'", (session_id,))
        return " ".join(row["text"] for row in rows)


def calls_to_run(reading):
    field_calls = [call for call in reading.calls if call.name != "respond"]
    return list({call.name: call for call in field_calls or reading.calls[:1]}.values())


def run_calls(session_id, calls, pending_step, patient_text):
    return [
        outcome
        for call in calls
        for outcome in run_tool(session_id, call.name, call.arguments, pending_step, patient_text)
    ]


def rephrased_if_repeated(conn, session_id, text):
    recent = last_agent_lines(conn, session_id, 2)
    if len(recent) == 2 and all(line.endswith(text) for line in recent):
        return f"Sorry, I need this to keep going. {text}"
    return text


def list_already_read(conn, session_id):
    row = conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND role = 'agent' AND text LIKE '%Which one do you need%'",
        (session_id,),
    ).fetchone()
    return row is not None


def finish_turn(session_id, turn, text, calls, outcomes, error_code, llm_ms, started):
    made_progress = any(outcome.code not in NO_PROGRESS_OUTCOMES for outcome in outcomes)
    with transaction() as conn:
        session = load_session(conn, session_id)
        unclear_turns = 0 if made_progress else session["unclear_turns"] + 1
        update_session(conn, session_id, unclear_turns=unclear_turns)
        session = load_session(conn, session_id)
        facts = load_facts(conn, session)
        composed = compose_reply(session, facts, outcomes, turn.note, text, list_already_read(conn, session_id))
        if composed.asked_pending and next_step(session) is Step.CONFIRM:
            update_session(conn, session_id, price_stated_at=utc_now())
        reply_text = rephrased_if_repeated(conn, session_id, composed.text)
        add_message(conn, session_id, session["channel"], "agent", reply_text)
        conn.execute(
            """
            INSERT INTO turn_log (session_id, step, tools, llm_ms, total_ms, error_code, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn.step,
                ",".join(call.name for call in calls),
                llm_ms,
                round((perf_counter() - started) * 1000),
                error_code or ("guard_blocked" if composed.blocked_answer else None),
                utc_now(),
            ),
        )
    return Reply(
        reply_text,
        step=next_step(session),
        ends_conversation=session["status"] != "active",
        holding=composed.holding,
        advanced=next_step(session).value != turn.step,
    )


async def handle_turn(session_id, text):
    async with session_locks[session_id]:
        started = perf_counter()
        turn = await asyncio.to_thread(begin_turn, session_id, text)
        if isinstance(turn, Reply):
            return turn
        reading = turn.exact or await read_message(turn.history, turn.note, turn.tools)
        calls = calls_to_run(reading)
        outcomes = await asyncio.to_thread(run_calls, session_id, calls, Step(turn.step), text)
        patient_text = everything_the_patient_said(session_id)
        if any(outcome.code == "verified" for outcome in outcomes) and mentions_refill_details(session_id, patient_text):
            follow_up = await asyncio.to_thread(turn_after_verification, session_id)
            second_reading = await read_message(follow_up.history, follow_up.note, follow_up.tools)
            second_calls = calls_to_run(second_reading)
            outcomes += await asyncio.to_thread(
                run_calls, session_id, second_calls, Step(follow_up.step), patient_text
            )
            calls = [*calls, *second_calls]
        llm_ms = round((perf_counter() - started) * 1000)
        return await asyncio.to_thread(
            finish_turn, session_id, turn, text, calls, outcomes, reading.error_code, llm_ms, started
        )
