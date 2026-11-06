# Prescription Refill Voice Agent

A voice agent for a pharmacy phone line. A patient calls, verifies their identity and requests a refill: they pick a medication, confirm the pharmacy, confirm their insurance and hear their copay. If the call drops before the request is complete, the conversation continues over SMS from the step where it stopped.

The problem statement, demo data and kickoff clarifications are in `docs/SPEC.md`. Design decisions are logged in `docs/DECISIONS.md`.

## Scope

In scope:
- One demo patient (Sarah Chen) seeded in a real database, with prescriptions, a pharmacy, insurance and a copay table.
- A browser page that simulates the phone call (start and end the call, microphone in, agent audio out) and a chat view that simulates SMS.
- The full happy path by voice, and the SMS fallback from any point in the call.
- A completed refill request record holding every collected field.

Not yet: real telephony or SMS providers, authentication, multiple patients, recovery after a server restart, a polished UI, test suites and CI. The extensions in `docs/SPEC.md` start only after the core flow works end to end, several runs in a row.

## The flow

1. Greet the caller, ask for their full name and date of birth, and match both against the database. On a mismatch, ask once more for the date of birth.
2. Medication: list the active prescriptions; the patient picks one.
3. Pharmacy: confirm the pharmacy on file, or record a different one.
4. Insurance: read back the plan and member ID on file and confirm they are current.
5. Copay: look up plan plus medication and state the amount.
6. Create the refill request, read back a short summary and end the call.

## Conversation state

- Each step's result is written to the database as it happens: a session record holds the patient, the current step, the collected fields and the channel. Nothing collected lives only in the model's context.
- The state record, not the model, decides what has been collected. The model reads and writes it through tools.
- When a call ends before completion, the system sends a simulated SMS that says what is done and asks the next question, for example: "Looks like we got disconnected! I have your Lisinopril refill started. Which pharmacy would you like to use?"
- The SMS conversation resumes from the stored step and never asks again for a field already collected.

## Quality bar

- Conversational speed: measure the time from the end of the patient's speech to the first agent audio, log it for every turn, and keep it low (target: under about 1 second). Stream wherever possible.
- The agent stays on the workflow: short spoken sentences, one question at a time. Every medication, pharmacy, plan and price it says comes from a tool result.
- Spoken dates and names are normalized before lookup (for example, "March fifteenth, nineteen eighty-five").
- Runs locally on macOS and Windows with written setup steps. Secrets live in `.env`, which is never committed; `.env.example` lists the variables.

## How we work

- Architecture choices are made explicitly. The coding agent proposes two or three options with their tradeoffs, the engineer chooses, and the choice and the reason go into `docs/DECISIONS.md` before the code is written.
- Build in thin vertical slices: first a text-only agent loop with tools and database state, then voice, then the SMS fallback, then latency work.
- After each slice, run the happy path end to end, then commit with a clear message.
- Keep the code small and readable: clear names over comments, no dead code.
- Coding-agent session transcripts are exported to `agent-logs/` at the end.
