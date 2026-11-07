"""Run typed conversations through the core and assert on the database. Uses a temporary database."""
import argparse
import asyncio
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

os.environ["REFILL_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "scenarios.db")

from refill.core import handle_turn, open_session  # noqa: E402
from refill.db import DB_PATH, init_db, load_session, transaction  # noqa: E402
from tests.invariants import audit  # noqa: E402

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8"))
CALLER_PHONE = "(555) 867-5309"
PARALLEL_SESSIONS = 5


def observed_state(session, request):
    return {
        "status": session["status"],
        "rx": session["prescription_id"],
        "pharm": session["pharmacy_id"],
        "ins": session["insurance_status"],
        "flag": int(bool(session["needs_pharmacist"] or (request and request["needs_pharmacist"]))),
        "reason": session["pharmacist_reason"],
        "verified": bool(session["verified_at"]),
        "request": request is not None,
        "copay": request["estimated_copay_cents"] if request else None,
        "req_flag": request["needs_pharmacist"] if request else None,
    }


def find_problems(scenario, state, agent_lines):
    said = " ".join(agent_lines)
    problems = [f"{key}={state[key]} want {value}" for key, value in scenario["want"].items() if state[key] != value]
    problems += [f"missing '{phrase}'" for phrase in scenario["hear"] if phrase not in said]
    problems += [f"said '{phrase}'" for phrase in scenario["avoid"] if phrase in said]
    for line in agent_lines:
        sentences = re.split(r"(?<=[.!?])\s+", line)
        if len(sentences) != len(set(sentences)):
            problems.append(f"repeated sentence: {line[:80]}")
    return problems


async def run_scenario(name, scenario, gate):
    async with gate:
        session_id, _ = open_session(CALLER_PHONE, scenario["channel"])
        agent_lines = []
        for text in scenario["lines"]:
            reply = await handle_turn(session_id, text)
            agent_lines.append(reply.text)
            if reply.ends_conversation:
                break
    with transaction() as conn:
        session = load_session(conn, session_id)
        request = conn.execute("SELECT * FROM refill_requests WHERE session_id = ?", (session_id,)).fetchone()
        timings = [row["llm_ms"] for row in conn.execute("SELECT llm_ms FROM turn_log WHERE session_id = ?", (session_id,))]
    problems = find_problems(scenario, observed_state(session, request), agent_lines)
    return name, problems, timings, list(zip(scenario["lines"], agent_lines))


def percentile(sorted_values, fraction):
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=tuple(SCENARIOS), default="tuned", dest="scenario_set")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--dump", type=Path, help="write every conversation to this JSON file")
    parser.add_argument("names", nargs="*", help="only these scenarios")
    arguments = parser.parse_args()

    scenarios = SCENARIOS[arguments.scenario_set]
    names = arguments.names or list(scenarios)
    init_db()
    gate = asyncio.Semaphore(PARALLEL_SESSIONS)
    jobs = [run_scenario(name, scenarios[name], gate) for name in names for _ in range(arguments.runs)]
    results = await asyncio.gather(*jobs)

    passes, failures, timings = Counter(), {}, []
    for name, problems, turn_timings, conversation in results:
        timings += turn_timings
        if problems:
            failures.setdefault(name, (problems, conversation))
        else:
            passes[name] += 1
    print(f"passed {sum(passes.values())}/{len(results)} runs across {len(names)} scenarios")
    for name, (problems, conversation) in failures.items():
        print(f"  FAIL {name}: {passes[name]}/{arguments.runs} passed, {problems}")
        for patient, agent in conversation:
            print(f"    patient: {patient}\n    agent: {agent}")
    timings.sort()
    print(f"llm ms over {len(timings)} turns: p50 {percentile(timings, 0.5)} p95 {percentile(timings, 0.95)} max {timings[-1]}")
    audit(DB_PATH)
    if arguments.dump:
        conversations = {name: {"problems": problems, "turns": turns} for name, problems, _, turns in results}
        arguments.dump.write_text(json.dumps(conversations, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
