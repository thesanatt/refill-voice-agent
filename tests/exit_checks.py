"""Check the exit conditions of the text slice: drops, refused tools and what the model can see."""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ["REFILL_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "exit_checks.db")

from refill.core import begin_turn, drop_session, handle_turn, open_session
from refill.db import init_db, load_session, transaction
from refill.reader import state_note
from refill.render import load_facts
from refill.steps import Step, allowed_tools, next_step
from refill.tools import run_tool
ID = "Sarah Chen, March 15 1985"
async def main():
    init_db(); bad = 0
    print("-- drop at each step: state, then the step a resume would use")
    script = [ID, "lisinopril", "yes", "yes"]
    for n, want in enumerate(["identify", "medication", "pharmacy", "insurance", "confirm"]):
        sid, _ = open_session("(555) 867-5309", "voice")
        for text in script[:n]:
            await handle_turn(sid, text)
        drop_session(sid); drop_session(sid)
        with transaction() as c:
            s = load_session(c, sid); req = c.execute("SELECT count(*) FROM refill_requests WHERE session_id=?", (sid,)).fetchone()[0]
        got = next_step(s).value; ok = got == want and s["status"] == "dropped" and req == 0
        bad += not ok
        print(f"  after {n} answers: status={s['status']} next={got} request_rows={req} rx={s['prescription_id']} pharm={s['pharmacy_id']} ins={s['insurance_status']} {'ok' if ok else 'FAIL'}")
    print("-- refused tools (called directly, bypassing the model)")
    sid, _ = open_session("(555) 867-5309", "voice")
    for name, args in [("select_medication", {"prescription_id": 1}), ("confirm_pharmacy", {"pharmacy_id": 1, "other_pharmacy": None}), ("confirm_insurance", {"still_current": True}), ("submit_request", {})]:
        o = run_tool(sid, name, args, Step.IDENTIFY, "yes"); ok = o[0].code == "refused"; bad += not ok
        print(f"  before verification {name}: {o[0].code} {'ok' if ok else 'FAIL'}")
    await handle_turn(sid, ID)
    for label, name, args, text in [("submit before fields", "submit_request", {}, "yes"), ("other patient's prescription id", "select_medication", {"prescription_id": 999}, "lisinopril")]:
        o = run_tool(sid, name, args, Step.MEDICATION, text); ok = o[0].code in ("refused", "not_on_list"); bad += not ok
        print(f"  {label}: {o[0].code} {'ok' if ok else 'FAIL'}")
    print("-- submit needs the price stated, then is safe to repeat")
    sid, _ = open_session("(555) 867-5309", "voice")
    for text in [ID, "lisinopril", "yes"]:
        await handle_turn(sid, text)
    with transaction() as c:
        s = load_session(c, sid)
    print("  allowed at insurance step includes submit:", "submit_request" in allowed_tools(s))
    bad += "submit_request" in allowed_tools(s)
    await handle_turn(sid, "yes")
    with transaction() as c:
        s = load_session(c, sid)
    print("  allowed after price stated includes submit:", "submit_request" in allowed_tools(s))
    for attempt in (1, 2):
        o = run_tool(sid, "submit_request", {}, Step.CONFIRM, "yes")
        print(f"  submit attempt {attempt}: {o[0].code}")
    with transaction() as c:
        n = c.execute("SELECT count(*) FROM refill_requests WHERE session_id=?", (sid,)).fetchone()[0]
    print(f"  request rows for session: {n} {'ok' if n == 1 else 'FAIL'}"); bad += n != 1
    print("-- what the model can see before verification")
    sid, _ = open_session("(555) 867-5309", "voice")
    turn = begin_turn(sid, "hello")
    leak = [w for w in ("Lisinopril", "Metformin", "CVS", "Aetna", "1985", "ANT-") if w in turn.note + str(turn.history)]
    print("  note:", turn.note.replace("\n", " | ")); print("  tools:", sorted(t["name"] for t in turn.tools)); print("  leaked terms:", leak or "none"); bad += bool(leak)
    print("-- what the model can see after verification")
    sid, _ = open_session("(555) 867-5309", "voice"); await handle_turn(sid, ID); turn = begin_turn(sid, "hello")
    leak = [w for w in ("1985-03-15", "ANT-88912", "88912", "copay", "1000", "1500") if w in turn.note]
    print("  forbidden terms in note:", leak or "none"); bad += bool(leak)
    print("\nexit check failures:", bad)
if __name__ == "__main__":
    asyncio.run(main())
