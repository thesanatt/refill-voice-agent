import asyncio
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from refill.db import add_message, load_session, transaction, update_session, utc_now
from refill.reader import OPENING_MESSAGE, read_message, state_note
from refill.render import (
    NO_PROGRESS_OUTCOMES,
    after_close_line,
    compose_reply,
    greeting,
    load_facts,
    pending_question,
)
from refill.steps import FINISHED_STATUSES, Step, allowed_tools, next_step
from refill.tools import definitions_for, mentions_refill_details, run_tool

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


def drop_session(session_id):
    with transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'dropped' WHERE id = ? AND status = 'active'", (session_id,))


def mark_reply_unheard(session_id):
    with transaction() as conn:
        update_session(conn, session_id, price_stated_at=None)
        conn.execute(
            """
            UPDATE messages SET interrupted = 1
            WHERE id = (SELECT max(id) FROM messages WHERE session_id = ? AND role = 'agent')
            """,
            (session_id,),
        )


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


def conversation_history(conn, session_id):
    rows = conn.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    messages = [{"role": "assistant" if r["role"] == "agent" else "user", "content": r["text"]} for r in rows]
    return [{"role": "user", "content": OPENING_MESSAGE}, *messages]


def load_turn(conn, session, just_verified=False):
    facts = load_facts(conn, session)
    return Turn(
        step=next_step(session).value,
        history=conversation_history(conn, session["id"]),
        note=state_note(session, facts, just_verified),
        tools=definitions_for(allowed_tools(session)),
    )


def begin_turn(session_id, text):
    with transaction() as conn:
        session = load_session(conn, session_id)
        add_message(conn, session_id, session["channel"], "patient", text)
        if session["status"] in FINISHED_STATUSES:
            line = after_close_line(session, load_facts(conn, session))
            add_message(conn, session_id, session["channel"], "agent", line)
            return Reply(line, ends_conversation=True)
        return load_turn(conn, session)


def turn_after_verification(session_id):
    with transaction() as conn:
        return load_turn(conn, load_session(conn, session_id), just_verified=True)


def everything_the_patient_said(turn):
    return " ".join(m["content"] for m in turn.history[1:] if m["role"] == "user")


def calls_to_run(reading):
    field_calls = [call for call in reading.calls if call.name != "respond"]
    return list({call.name: call for call in field_calls or reading.calls[:1]}.values())


def run_calls(session_id, calls, pending_step, patient_text):
    return [
        outcome
        for call in calls
        for outcome in run_tool(session_id, call.name, call.arguments, pending_step, patient_text)
    ]


def finish_turn(session_id, turn, text, calls, outcomes, error_code, llm_ms, started):
    made_progress = any(outcome.code not in NO_PROGRESS_OUTCOMES for outcome in outcomes)
    with transaction() as conn:
        session = load_session(conn, session_id)
        unclear_turns = 0 if made_progress else session["unclear_turns"] + 1
        update_session(conn, session_id, unclear_turns=unclear_turns)
        session = load_session(conn, session_id)
        composed = compose_reply(session, load_facts(conn, session), outcomes, turn.note, text)
        if composed.asked_pending and next_step(session) is Step.CONFIRM:
            update_session(conn, session_id, price_stated_at=utc_now())
        add_message(conn, session_id, session["channel"], "agent", composed.text)
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
        composed.text,
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
        reading = await read_message(turn.history, turn.note, turn.tools)
        calls = calls_to_run(reading)
        outcomes = await asyncio.to_thread(run_calls, session_id, calls, Step(turn.step), text)
        patient_text = everything_the_patient_said(turn)
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
