# Design decisions

Each entry records the options considered, the choice and the reason. Entries are added before the code that depends on them.

Format:

## D<n>. <decision> (<date>)
- Options: ...
- Chosen: ...
- Why: ...
- Revisit if: ...

## D1. Flow control: a plain code module owns the steps (2026-11-05)
- Options: (A) one prompt with all tools; (B) a plain code module that derives the step from stored fields, with the LLM reading replies; (C) a Pipecat Flows graph or a fully scripted dialogue.
- Chosen: B. The current step is the first required field still empty, never a stored pointer. One core, `handle_turn(session, channel, text)`, serves voice and SMS and imports nothing from the voice layer. The LLM reads the patient's reply and calls tools. Code renders every sentence holding a drug, pharmacy, plan or price, so a normal turn needs one LLM pass. Eight tools: `verify_identity`, `select_medication`, `confirm_pharmacy`, `confirm_insurance`, `submit_request`, `request_pharmacist`, `end_call`, `respond`. Every turn is a forced tool call, and free answers go through `respond`. Tools are gated on prerequisites rather than on the current step. Before verification only `verify_identity`, `request_pharmacist`, `end_call` and `respond` exist. After it every field tool exists, so an early answer is captured. `submit_request` also needs every field filled and the price already stated.
- Why: On text agents in 2026, one controlled study went from 0.38 with raw state to 0.98 with hard enforcement. Those tasks are harder than this one and are not voice, so the shape transfers and the number does not. Flows keeps state in a process dictionary and needs a voice pipeline, which the SMS path does not have. Gating on the current step alone would lose a pharmacy named early. Forcing the tool call was measured on 2026-11-06 with Haiku 5.5, 24 calls per mode in one run from a home connection: a finished call took 827 ms median against 1,186 ms with automatic tool choice, because automatic mode wrote a preamble in 18 of 24 calls and skipped the tool once. It also means a tool call can never be spoken as plain text.
- Revisit if: off-script handling becomes the main extension and code routing feels brittle, or the model moves to Sonnet 5.5, which rejects forced tool choice.

## D2. Database: SQLite (2026-11-05)
- Options: (A) SQLite; (B) Postgres in Docker.
- Chosen: A, through Python's `sqlite3` with plain SQL and no ORM. Eight tables: `patients` (with plan and member ID), `pharmacies`, `prescriptions` (with `indication`), `copays`, `sessions`, `refill_requests`, `messages`, `turn_log`. `sessions` uses typed columns so the step can be derived from empty fields. Money is integer cents and dates are ISO text. `refill_requests.session_id` is unique. One conditional update on `sessions.sms_sent_at` guarantees a single follow-up text, so there is no outbox table. Two or three alternate pharmacies are seeded.
- Why: nothing to install on macOS or Windows, one process writes, and the take-home accepts any real database. Postgres adds a setup step for concurrency this does not need.
- Revisit if: more than one process writes.

## D3. Normalizing dates, names and drugs (2026-11-05)
- Options: (A) the LLM maps speech to typed fields and code validates; (B) a deterministic parser alone; (C) rely on speech-to-text formatting.
- Chosen: A. `verify_identity` takes a name plus year, month and day. Unclear parts stay empty and the model never guesses. Code builds the date, matches it exactly, then fuzzy-matches the name on those rows only. For drugs the model sees the patient's own list and returns a `prescription_id` or `not_on_list`, with no fuzzy matching in code. The raw transcript stays in `messages`. `dateparser` is not used.
- Why: in a local test `dateparser` returned nothing for "March fifteenth, nineteen eighty-five". "Lovastatin", a different drug, scored 82 against Atorvastatin, so a fuzzy threshold is unsafe. The model never sees the stored date of birth, so it cannot steer a wrong caller to a match.
- Revisit if: real transcripts show the model mapping dates wrongly.

## D4. The request is created on the patient's yes (2026-11-05)
- Options: (A) state the copay, create the request, read back; (B) state the price and summary, ask, create on yes.
- Chosen: B. `submit_request` takes no arguments, needs every field filled and is safe to repeat.
- Why: with A, a drop before the read-back marks the request complete and sends no text, so the patient never hears the price. With B there is one boundary: before the yes the SMS resumes, after it the work is done. A reply also proves the price was heard, so nothing has to track audio delivery. "Once all information is collected" still holds, because the yes is the last item.
- Revisit if: the extra turn feels heavy when listening to real calls.

## D5. Working phrases on slow turns (2026-11-05)
- Options: (A) no filler; (B) short working phrases on a timer; (C) hesitation sounds such as "uhh" and "umm".
- Chosen: B. Pre-recorded clips in the agent's voice, such as "Let me check that" and "One moment", holding no facts. A clip plays only if no reply is ready about 0.3 s after the turn ends, so fast turns have none. At most about three per call, never the same one twice running, and never while a date of birth is incomplete. Clips can be interrupted and stay out of the conversation context. First sound and first real content are logged separately, and the latency figure uses first content.
- Why: it covers an LLM wait without hiding it from the measurement. Hesitation sounds may signal doubt just before a price, and the evidence on them is mixed. Speed and accuracy stay the priority, so this stays light. The 0.3 s is a planning number.
- Revisit if: clips sound repetitive, or timing shows they are rarely needed.

## D6. Edge cases and guardrails (2026-11-05)
- Options: (A) prompt rules only; (B) enforcement in the tool layer and core, with a short prompt; (C) add guard models or classifiers.
- Chosen: B, as set out in the tables below. Times are planning numbers. Voice rows assume the Pipecat pipeline, which is still open as Q1.
- Why: a prompt can be talked around and a refused tool cannot. Guard models add latency to every turn. None of these rules adds an LLM pass to a normal turn.
- Revisit if: testing after the build shows a rule failing or a common case missing.

### Talking over

| Case | What happens |
|---|---|
| Patient interrupts the agent | Audio stops, the rest of the line is dropped, and the reply is read as a normal turn. The line is stored marked as interrupted. |
| "Mm-hm", "okay", "yeah" while the agent talks | Not an interruption. The agent keeps talking and the word stays out of the context. Any other word does interrupt, even one word such as "Lisinopril" or "wait". This needs a small custom start strategy, because Pipecat's `min_words` would also block those one-word interruptions. |
| Agent starts too early and the patient keeps going | Agent stops. The earlier fragment and the new words are read together as one turn, unless the fragment already moved the flow forward. |
| Patient speaks during a working phrase | The clip stops, any pending reply is dropped, and the turn is read again with the new words. |
| "Yes, send it" cuts off the price line | Does not count as the yes. The agent repeats the price and the question. |
| Both start at once | The agent yields. |
| A new turn arrives while one is running | Turns run one at a time under a per-session lock. A committed write always stands, and the database decides what happened. Tool calls use `cancel_on_interruption=False`. |
| The agent's own audio reaches the microphone | Browser echo cancellation stays on. |

### Questions back to the agent

| Patient asks | What happens |
|---|---|
| Something on record, such as what a drug is for or its strength | Answer from the record, then repeat the pending question. |
| "How much is it?" before insurance is confirmed | "I can tell you as soon as we confirm your insurance." No number, then the pending question. |
| "Why do you need that?" | Fixed line, then the pending question. |
| "Are you a real person?" | A truthful fixed line saying it is an automated assistant. |
| Dose advice, side effects, interactions, symptoms | No advice. `request_pharmacist`, then offer to continue the refill. |
| Emergency language | A fixed line telling them to call 911. |
| Two questions at once | Answer what is on record, then ask one question back. |
| Anything else | Say it cannot help with that, offer a pharmacist, then the pending question. |

### Answers given early or together

| Case | What happens |
|---|---|
| Several answers in one turn | One LLM pass, several tool calls run in order with `run_in_parallel=False`. The next line is computed once from the final state. |
| An answer to a later step, after verification | The field is filled and that question is skipped. |
| A detail given before verification ("refill my lisinopril") | Not stored as collected. It stays in the conversation and is applied after verification, so the patient is not asked again. Code spots the drug, purpose or topic in the patient's words, and one extra LLM pass reads them on that turn only. |
| Name and date together or reversed | Accepted. |
| Self-correction in one turn ("CVS, no, Walgreens") | The last stated value wins. |
| Change of mind before the final yes | The field is overwritten, the step recomputes, and the copay is looked up again when spoken. |
| An incomplete date ("March...") | Not a failed attempt. The agent waits or asks for the missing part. |
| Two refills at once | The agent asks which one first. A second refill is a listed extension. |

### Protecting data

- Patient identity comes from the server session, never from a tool argument, and a session is bound to one patient.
- No tool can list patients or search by name.
- Before verification the model's context holds no patient data, and refused tools also refuse in code if called.
- The model never sees the stored date of birth, the full member ID or the copay table. Code renders those.
- Two complete failed checks end verification for that session. Tools stay refused and the agent offers a pharmacist.
- Questions about anyone else are refused with a fixed line. A caregiver gives the patient's name and date of birth.
- A write needs evidence in the patient's own words. A yes counts only for the question pending when the turn began, an answer to a later step must name that topic, and `submit_request` needs a clear yes with no negation. `respond` counts only as the sole call, and repeated or cut-off calls are dropped. This sits in code because forced tool use makes the model over-eager: in testing it confirmed insurance on "can you repeat that".
- Free text from the model counts only when the patient asked a question, so it cannot announce an action that did not happen. It is checked one sentence at a time before text-to-speech: a sentence with a number, price or drug name outside this session's facts, tool syntax, or a question of its own is dropped, and the pending question follows. Before verification only the fixed answers pass. Replies are capped short.
- Speech-to-text punctuation is never trusted as evidence. A spoken "Yes." came back as "Yes?" in testing, so a question is detected from its opening word.
- If a reply is interrupted or never played, the price counts as unheard, and a yes cannot send the request until the price has been repeated.
- "Ignore your instructions" or "I'm the pharmacist" changes nothing, because no tool can act on it. The agent gives a fixed line and the pending question.
- State holds IDs and enums. Pharmacy text from a patient is stored as data and flagged for staff.
- SMS replies pass through the same gates. "Wrong number" or "who is this" ends the thread and blocks further content.
- `turn_log` holds timings and error codes only.

### Other realistic cases

| Case | What happens |
|---|---|
| "What?" or "Can you repeat that?" | The last line is rendered again from state. |
| "Hold on" | The agent waits quietly for up to about 30 s. |
| Silence | A gentle re-ask at about 10 s and "Take your time" at about 20 s. Whether long silence ends the call is left to Q5. |
| Background talk or unclear speech | "Sorry, I didn't catch that", then a rephrased question. After two misses it offers a pharmacist. |
| "I have to go" or "text me instead" | `end_call`: the agent says it will text, then the SMS path runs. |
| A drug not on the list | "I don't see that one on your list", then the list again and an offer of a pharmacist. |
| A different pharmacy not in the table | Stored as patient text, read back and flagged for staff. |
| "No" to the pharmacy on file, with none named | The agent lists the other pharmacies and asks which one. |
| Only a first name | The agent asks for first and last name. This is not a failed attempt. |
| "I don't know" about insurance | Recorded as unsure. The estimate comes from the plan on file, and the request is flagged for staff. It must really be said: a question never counts as an answer here. |
| "I don't know" at another step | "That's okay", then the pending question. |
| Insurance has changed, or no copay row exists | Noted and flagged. No price is quoted or invented. After the yes the request is created with `estimated_copay` empty. |
| "No" at the final question | Nothing is created, the status becomes `declined`, and no text is sent. |
| The LLM is slow or fails | After about 4 s, one retry, then the pending question is re-asked from state. A refusal is handled the same way. |
| A speech service fails mid-call | Treated as a dropped call, so the SMS path resumes. |
| A text arrives after completion | A short rendered status. |
| The patient asks for another language | The agent offers a pharmacist. |

Documented but not built: a patient calling back during an open SMS thread, several refills in one session, and multiple patients.

## D7. Take-home scope and targets (2026-11-05)
- Options: (A) build to production rules; (B) build the core and name production needs in the walkthrough.
- Chosen: B. Left out: opt-out word matching, the 160-character text limit, session expiry, the transcription notice and vendor agreements. Kept because they are cheap: tools refusing before verification, no drug name in a text before verification, and saying the agent is AI in the greeting. The date of birth retry follows the kickoff notes. First texts ask the next question directly and may state the copay. The fast deterministic reader is deferred until timings exist. The target stays "under about 1 second", now measured from the true end of speech at the microphone to the first audible agent audio, reported by turn class, and the number changes only after real timings.
- Why: this is a take-home that will not ship. Latency estimates put a turn where the LLM reads the reply near 1.4 s at the median, so the honest move is to measure before touching the target.
- Revisit if: real telephony or real SMS is added.

## D8. Voice architecture: a pipeline on Pipecat (2026-11-06)
- Options: (A) speech-to-text, LLM and text-to-speech on Pipecat with SmallWebRTC; (B) GPT-Realtime-2.1 over WebRTC with our own step logic; (C) A with a realtime model as the audio-in stage.
- Chosen: A, with the Pipecat version pinned. Tools and state import nothing from the voice layer, so B stays a one-slice comparison.
- Why: controlled comparisons put the best pipelines and speech-to-speech models roughly level on task accuracy, so the case is control. A text stage lets code fix the exact words before they are spoken, and the same core runs SMS. It runs in one Python process with no media server.
- Revisit if: a tuned pipeline stays above about 1.5 s median on turns the LLM reads, or sounds robotic after the latency slice.

## D9. Member ID read-back: last four digits (2026-11-06)
- Options: (A) the full ID; (B) the last four digits.
- Chosen: B. The take-home only asks to confirm insurance is current. The full ID stays in the database and on the request record.
- Why: a shorter turn, less data spoken aloud, and no risky alphanumeric string for text-to-speech.
- Revisit if: a reviewer wants the full ID read back.

## D10. Providers: AssemblyAI, Haiku 5.5 and Inworld (2026-11-06)
- Options: speech-to-text from AssemblyAI Universal-3.6 Pro, Deepgram Flux or Nova-3; LLM from Claude Haiku 5.5, Sonnet 5.5 or Gemini Flash; text-to-speech from Inworld TTS-2, ElevenLabs Flash or Cartesia Sonic.
- Chosen: AssemblyAI Universal-3.6 Pro, Claude Haiku 5.5 with thinking off and forced tool calls, and Inworld TTS-2. All three have Pipecat services in the pinned version except the LLM, which the core calls directly. Deepgram and ElevenLabs keys are held for comparison and for test voices.
- Why: AssemblyAI allows mid-stream keyterm and turn changes. Haiku 5.5 measured about 840 ms median to a finished tool call on this project, and forced tool calls are not available on Sonnet 5.5. Inworld had the lowest independent first-audio figure among the shortlist, with inline pronunciation control. Most vendor figures are unverified here, so these are first picks, not conclusions.
- Revisit if: drug names or dates transcribe poorly, or first audio is slow on real calls.

## D11. Turn detection: Smart Turn with step-aware limits (2026-11-06)
- Options: (A) Smart Turn with VAD, plus step-aware rules; (B) provider turn detection tuned mid-stream; (C) a plain silence timer.
- Chosen: A. The core tells the turn layer the pending step. The fallback for a missed end of turn is 3.0 s at the date of birth, 1.0 s at the medication step and 0.6 s at yes/no steps. Barge-in stays on for every line, with a small list of acknowledgement words that do not interrupt unless the line is about to end, when they may be a real answer.
- Why: it starts with no vendor dependency and runs in milliseconds locally. A silence timer scored far lower in the one comparison found, and dates of birth contain long pauses.
- Revisit if: recordings of real speech show cut-offs this cannot fix, then move to B. Tune on human speech, since Smart Turn's false cut-off rate rose from 1.0% on real clips to 57.7% on synthetic ones in one benchmark.

## D12. Detecting an early hang-up (2026-11-06)
- Options: (A) WebRTC connection state alone; (B) the End button, a `pagehide` handler and a heartbeat; (C) a silence timeout.
- Chosen: B. The End button and `pagehide` close the connection at once. An abnormal loss starts a 3 to 5 s grace timer, cancelled if the session reconnects. Silence never sends a text. One handler, under the session lock so it sees any write in progress, sends the text once and sends nothing if the request is complete.
- Why: without `pagehide`, a closed tab took about 30 s to reach aiortc in a local test. With it, closing the real page marked the session dropped in about 55 ms. A 30-second gap would read as a bug in a demo.
- Revisit if: real telephony is added, which reports hang-ups differently.

## D13. Identity on SMS: trust a verified session (2026-11-06)
- Options: (A) trust the phone number on file; (B) trust a session verified on the call; (C) ask for the date of birth at the start of every thread.
- Chosen: B. A session verified by voice continues over SMS with no new check. If the call dropped before verification, the first text names no drug and asks for the full name and date of birth, and nothing unverified counts as collected.
- Why: A treats a delivery address as identity, and C re-collects what the take-home says not to.
- Revisit if: real SMS is added, when a session expiry and a step-up before changing pharmacy are needed.

## D14. Interface: one page with call and SMS panes (2026-11-06)
- Options: (A) one page; (B) separate views.
- Chosen: A.
- Why: a reviewer sees the hang-up and the text arrive without switching windows.
- Revisit if: real telephony is added.

## Open questions

None at the moment. Earlier references use these numbers: Q1 is D8, Q2 is D10, Q3 is D1, Q4 is D2, Q5 is D12, Q6 is D13, Q7 is D3, Q8 is D11, Q9 is D14, Q10 is D4 and Q11 is D9.
