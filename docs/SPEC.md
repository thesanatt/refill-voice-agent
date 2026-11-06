# Problem statement: Prescription Refill Voice Agent

## Overview

A patient calls in, the agent verifies their identity and walks them through the refill: picking a medication, confirming their pharmacy, verifying insurance and telling them their copay. If the patient hangs up at any point, intentionally or not, the system follows up by SMS to collect the remaining information and complete the refill request.

## The flow

The agent first verifies the patient's identity. Once confirmed, it collects four pieces of information:
1. **Which medication:** the patient has several active prescriptions on file; the agent helps them pick one.
2. **Pharmacy:** confirm the pharmacy on file, or select a different one.
3. **Insurance verification:** confirm the insurance on file is still current.
4. **Copay notification:** look up the copay for their insurance and medication, and tell them the amount.

Once everything is collected, a refill request is created in the database.

## Core requirements

**Voice call with SMS fallback**
- The patient can complete the entire flow in a single call.
- If the call ends at any point before the request is complete, the system sends an SMS to collect the remaining information.
- The SMS conversation picks up where the call left off, with no re-collecting of information already gathered.
- The SMS follow-up feels natural, for example: "Looks like we got disconnected! I have your Lisinopril refill started. Which pharmacy would you like to use?"

**Identity verification**
- Verified at the start of the call. The happy path (correct details) is sufficient.

**Database-backed state**
- Patient data, prescription data and refill request state live in a real database (any engine).
- Seed data: at least one demo patient with multiple active prescriptions, a pharmacy on file and insurance information.
- A copay lookup: insurance plan plus medication gives the copay amount.
- The final output is a completed refill request record with all collected fields.

**Runnable locally**
- Live walkthrough on this machine; some manual setup is acceptable. No deployment needed.

## Expected limitations (not required)

Deployment, authentication or multiple accounts, graceful off-happy-path handling, surviving a server restart mid-conversation, a polished UI, thorough tests or CI.

## Demo data

**Patient:** Sarah Chen, DOB 1985-03-15, phone (555) 867-5309

| Medication | Dosage | Frequency |
|---|---|---|
| Lisinopril | 10mg | Once daily |
| Metformin | 500mg | Twice daily |
| Atorvastatin | 20mg | Once daily |

**Pharmacy on file:** CVS Pharmacy, 1234 Main St
**Insurance:** Aetna PPO, Member ID ANT-88912

| Medication | Aetna PPO copay |
|---|---|
| Lisinopril | $5 |
| Metformin | $10 |
| Atorvastatin | $15 |

Only copay amounts are needed from insurance; coverage details are out of scope.

## Stack

Any language, framework, voice or LLM provider and database. The one hard requirement is a real database, not in-memory only. API keys for model providers can be requested.

## Extensions (only after the core flow works well)

Multiple refills in one session; real phone calls (Twilio or similar); real SMS (OpenPhone); off-script handling (unexpected answers, changes of mind, wrong date of birth); multiple patients looked up by phone number or another identifier.

## Clarifications from the kickoff call (Nov 3, 2026)

- **Identity:** match on two identifiers, full name and date of birth. If the name matches but the date of birth doesn't, asking the patient to repeat the date of birth is a reasonable recovery.
- **Simulation:** no telephony or SMS vendor is needed. A browser button that starts and ends a simulated call, and a chat interface for the simulated SMS, are enough.
- **Latency:** this replaces the latency line under expected limitations. Responses should feel conversational; multi-second pauses make for a poor demo.
- **Approaches:** speech-to-speech models (audio in, audio out) and the three-model pipeline (speech-to-text, LLM, text-to-speech, often assembled with a framework such as Pipecat) are both acceptable.
- **Review:** the demo covers the happy path including the SMS fallback, how the agent sounds and stays on task, and a walkthrough of the design: the agent loop, its tools, how conversation state is managed, and how the SMS fallback resumes from that state.
- **Priority:** the core flow working well comes before any extension.
- **Process record:** the coding-agent session transcripts are included in the repository.
