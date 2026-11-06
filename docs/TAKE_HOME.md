<!-- The take-home problem statement as provided by Scope Health ("Take-Home Problem - Prescription Refill Voice Agent", Google Doc: https://docs.google.com/document/d/1wx1cQnBEQ1p--jf1OglhdmZCIbaHlWMNHzT2YuYl9sw/edit). Copied verbatim on Nov 5, 2026; only the table markup was converted to Markdown. -->

# Prescription Refill Voice Agent

## Overview

Build a voice agent that helps patients request prescription refills over the phone, with the ability to seamlessly continue the conversation over SMS if the call ends before all information is collected.

A patient calls in, the agent verifies their identity, and walks them through the refill process: picking a medication, confirming their pharmacy, verifying insurance, and telling them their copay. If the patient hangs up at any point — intentionally or not — the system follows up via SMS to collect the remaining information and complete the refill request.

## The Flow

The agent first verifies the patient's identity by asking for their date of birth. Once confirmed, the agent collects four pieces of information to complete the refill:

1. **Which medication** — the patient has multiple active prescriptions on file; the agent helps them pick one
2. **Pharmacy** — confirm their pharmacy on file, or select a different one
3. **Insurance verification** — confirm their insurance information is still current
4. **Copay notification** — look up and communicate the copay amount (based on their insurance and medication)

Once all information is collected, a refill request is created in the database.

## Core Requirements

**Voice call with SMS fallback:**

- The patient can complete the entire flow over a single phone call
- If the call ends at *any* point before the refill request is complete, the system sends an SMS to continue collecting the remaining information
- The SMS conversation should pick up where the call left off — no re-collecting information already gathered
- The SMS follow-up should feel natural (e.g., "Looks like we got disconnected! I have your Lisinopril refill started — which pharmacy would you like to use?")

**Identity verification:**

- The agent should verify the patient's identity at the start of the call by asking for their date of birth
- Happy path is sufficient — the patient provides the correct DOB and is verified

**Database-backed state:**

- Patient data, prescription data, and refill request state should live in a real database (any engine is fine)
- Seed data should include at least one demo patient with:
  - Multiple active prescriptions
  - A pharmacy on file
  - Insurance information
- A copay lookup (can be a simple table or map: insurance plan + medication -> copay amount)
- The final output is a completed refill request record with all collected fields

**Runnable locally:**

- We'll do a live walkthrough together, running it on your machine. I’d like to be able to run it locally as well but it’s fine if some manual setup is needed, perfect devex is not the focus here
- A deployed version is not required

## Expected Limitations

The baseline solution does **not** need to:

- Be deployable — running locally is sufficient
- Support authentication or multiple user accounts
- Handle off-happy-path gracefully (e.g., wrong DOB, confused patient, adversarial input)
- Survive a server restart mid-conversation (it's fine if in-progress sessions are lost)
- Optimize for latency — it's fine if responses are a bit slow
- Have a polished UI — whatever interface you choose for simulating calls and texts just needs to be functional
- Have thorough test coverage or CI

The goal is a working demo of the core flow, not a production system.

## Demo Data

Seed your database with something like the following (feel free to adjust names, numbers, and add additional data as you see fit):

**Patient:** Sarah Chen, DOB 1985-03-15, Phone (555) 867-5309

**Prescriptions:**

| Medication | Dosage | Frequency |
| :-: | :-: | :-: |
| Lisinopril | 10mg | Once daily |
| Metformin | 500mg | Twice daily |
| Atorvastatin | 20mg | Once daily |

**Pharmacy on file:** CVS Pharmacy, 1234 Main St

**Insurance:** Aetna PPO, Member ID ANT-88912

**Copay schedule:**

| Medication | Aetna PPO |
| :-: | :-: |
| Lisinopril | $5 |
| Metformin | $10 |
| Atorvastatin | $15 |

## Stack

Your choice — any language, framework, voice/LLM provider, and database. The only hard requirement is a real database (not in-memory-only).

## Extensions

Once the core flow works, you're welcome to take the project in any direction you'd like. Add polish, robustness, generalization - whatever you find interesting. Here are some ideas, but these are just suggestions:

- **Multiple refills in one session**: handle "I'd like to refill my Lisinopril and my Metformin" in a single interaction
- **Real phone calls**: wire up Twilio or similar so the voice call works over an actual phone line (we can provide credentials)
- **Real SMS**: wire up actual text messaging (we can provide OpenPhone credentials - sending texts over twilio is actually pretty annoying)
- **Off-script handling**: gracefully handle patients who give unexpected answers, change their mind mid-flow, ask clarifying questions, or provide the wrong date of birth
- **Multiple patients**: rather than a single demo patient, support looking up patients by phone number or other identifier
