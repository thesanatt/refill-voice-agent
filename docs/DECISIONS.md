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
- Chosen: B. The current step is the first required field still empty, never a stored pointer. One core, `handle_turn(session, channel, text)`, serves voice and SMS and imports nothing from the voice layer. The LLM reads the patient's reply and calls tools. Code renders every sentence holding a drug, pharmacy, plan or price, so a normal turn needs one LLM pass. Nine tools: `verify_identity`, `select_medication`, `confirm_pharmacy`, `confirm_insurance`, `change_answer`, `submit_request`, `request_pharmacist`, `end_call`, `respond`. Every turn is a forced tool call, and free answers go through `respond`. Tools are gated on prerequisites rather than on the current step. Before verification only `verify_identity`, `request_pharmacist`, `end_call` and `respond` exist. After it every field tool exists, so an early answer is captured. `submit_request` also needs every field filled and the price already stated.
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
- Chosen: B. Short lines in the agent's voice, such as "Let me check that" and "One moment", holding no facts. A line plays only if no reply is ready 1.5 s after the turn ends, so normal turns have none. At most about three per call, never the same one twice running, and never while a date of birth is incomplete. Clips can be interrupted and stay out of the conversation context. First sound and first real content are logged separately, and the latency figure uses first content.
- Why: it covers a slow LLM reply without hiding it from the measurement. Hesitation sounds may signal doubt just before a price, and the evidence on them is mixed. A first setting of 1.0 s sat at the LLM's median, so the phrase fired on nearly every LLM turn and delayed the real answer behind it. 1.5 s keeps it for slow turns.
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
| Emergency language | A fixed line telling them to call 911, said once per call. |
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
| Change of mind before the final yes | The field is overwritten, the step recomputes, and the copay is looked up again when spoken. "Change my medicine" or "go back" with no new value clears that answer through `change_answer`. |
| An incomplete date ("March...") | Not a failed attempt. The agent waits or asks for the missing part. |
| Several refills at once ("all 3", "both", two drug names) | Code catches this: the agent does one refill at a time and asks which first. A second refill is a listed extension. |

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
| Background talk or unclear speech | "Sorry, I didn't catch that", then the question again. After two misses it mentions the keys or a pharmacist, and a third identical line is rephrased. |
| A greeting, "how are you" or "what can you do" | A short fixed answer, then the pending question. |
| A second identity attempt inside one breath | An attempt only counts if the patient heard the reply, so fragments of one sentence cannot cause a lockout. A name given surname first still matches. |
| The drug list is needed again | Read in full once. After that the agent says it can read the list again, and does so on request. |
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
- Chosen: B. Left out: opt-out word matching, the 160-character text limit, session expiry, the transcription notice and vendor agreements. Kept because they are cheap: tools refusing before verification, no drug name in a text before verification, and saying the agent is AI in the greeting. The date of birth retry follows the kickoff notes. First texts ask the next question directly and may state the copay. The target stays "under about 1 second", now measured from the true end of speech at the microphone to the first audible agent audio, reported by turn class, and the number changes only after real timings.
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

## D11. Turn detection: Smart Turn with step-aware limits (2026-11-06, replaced by D15)
- Options: (A) Smart Turn with VAD, plus step-aware rules; (B) provider turn detection tuned mid-stream; (C) a plain silence timer.
- Chosen: A. The core tells the turn layer the pending step. The fallback for a missed end of turn is 3.0 s at the date of birth, 1.0 s at the medication step and 0.6 s at yes/no steps. Barge-in stays on for every line, with a small list of acknowledgement words that do not interrupt unless the line is about to end, when they may be a real answer.
- Why: it starts with no vendor dependency and runs in milliseconds locally. A silence timer scored far lower in the one comparison found, and dates of birth contain long pauses.
- Revisit if: recordings of real speech show cut-offs this cannot fix, then move to B. Tune on human speech, since Smart Turn's false cut-off rate rose from 1.0% on real clips to 57.7% on synthetic ones in one benchmark.

## D12. Detecting an early hang-up (2026-11-06)
- Options: (A) WebRTC connection state alone; (B) the End button, a `pagehide` handler and a heartbeat; (C) a silence timeout.
- Chosen: B. The End button and `pagehide` close the connection at once. The page sends a heartbeat every second, and 4 s without one counts as a lost connection. Silence never sends a text. One handler, `finish_call`, runs under the session lock so it sees any write in progress, sends the text once and sends nothing if the request is complete or identity failed. There is no reconnect, because a browser demo has no way back into a dropped call.
- Why: without `pagehide`, a closed tab took about 30 s to reach aiortc in a local test. With it, closing the real page marked the session dropped in about 55 ms, and End call put the text on the page in about 0.1 s. A silent connection produced the text in 4.1 s. A 30-second gap would read as a bug in a demo.
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

## D15. Turn-taking: a slot-aware Bayesian decision replaces Smart Turn (2026-11-06)
- Options: (A) keep Smart Turn with step limits (D11); (B) one fixed silence limit; (C) decide from what the pending question needs, with silence weighed by how real pauses end.
- Chosen: C, in `refill/turn_model.py`. The words so far are classed as answered, hanging or open for the pending question. That sets the odds the patient is still talking, and those odds fall with silence at the rate a log-normal pause model predicts. The agent replies once the cut-off risk drops below 10%, or 5% at identity. In practice "answered" adds no wait, "open" about 0.5 s and "hanging" about 1.6 to 2.0 s. Smart Turn is no longer used. This follows the cost-based approach of Raux and Eskenazi (2009).
- Why: on real calls Smart Turn added no measurable information: it said complete 21 of 33 times when the patient had finished and 4 of 5 when they had not, a likelihood ratio of 0.88, with wide intervals. It sent 11 of 35 turns to the silence fallback and let fragments like "Um," end a turn. The agent already knows what it asked, and audio alone does not. Scored on held-out calls, this halves the mean wait (1.54 s to 0.77 s) and cuts the 90th percentile from 3.0 s to 0.92 s, at about the same expected cut-offs (2.3 to 2.6 of 11). The data is small: 58 turns, 11 still talking and 12 pauses from one speaker, so priors matter and the constants are a starting point. `tests/fit_turn_model.py` refits them.
- Revisit if: more human calls are collected, especially older or slower speakers, or a different speaker shows a different pause pattern.

## D16. Exact answers skip the LLM (2026-11-06)
- Options: (A) the LLM reads every turn; (B) code reads a short list of exact answers and the LLM reads the rest; (C) speculative LLM reads on partial transcripts.
- Chosen: B. A bare yes or no at a yes/no step, or an exact drug name at the medication step, goes straight to the same tool layer with the same gates. Requests for several refills, or about someone else's records, are also handled here. Anything else goes to the LLM. Timing is why D7 deferred this, and timings now exist.
- Why: the LLM read is the largest stage at about 0.9 s, and it is the only route found to under 1 s. On replayed human speech these turns reached first audio in about 0.65 to 0.9 s. C could save perhaps 0.25 s on the rest and is the next lever, at more complexity.
- Revisit if: a reviewer wants every turn to go through the model.

## D17. What the model sees and how many tools it may call (2026-11-06)
- Options: (A) full history, several tools per turn; (B) full history, one tool per turn; (C) only the last exchange after verification, several tools per turn.
- Chosen: C. Identity turns keep the whole conversation, and the extra read after verification does too.
- Why: the model re-recorded old answers it could see, which took output from about 42 to about 150 tokens and added about 0.6 s. B looked better on speed and call counts, but failed on correctness: the model re-recorded first and stopped, so "no, don't send it" failed 3 of 3 runs. C removes the cause, and typed tests showed no loss of accuracy.
- Revisit if: a real call shows the model losing a reference to something said two turns earlier.

## D18. Two phone keys: go back and main menu (2026-11-06)
- Options: (A) no keys; (B) a full keypad with numbered choices at every step; (C) two fixed keys.
- Chosen: C. Star goes back one answer and pound returns to the main menu. They are always the same, sit on the page during a call, and also work from a computer keyboard. They arrive as `InputDTMFFrame`, so a real phone line would use the same path. Each key becomes the words "go back" or "main menu" and runs through the same turn handler, gates and logging. Everything else stays spoken.
- Why: speech recognition is the part most likely to fail a confused or stuck caller, and a key always works. Keys reply in about 0.3 s because they skip speech-to-text and the LLM. A 12-key pad was built and tested first, and dropped at the engineer's request: changing labels are more to read, and typing a date needs 0, which callers expect to mean a person.
- Revisit if: real telephony is added, when the agent must announce the keys and callers cannot see them.

## D19. Conditions for real phone calls (2026-11-06)
- Options: (A) connect Twilio as the app stands; (B) add a recording notice and lock down the public routes as part of that work.
- Chosen: B. Before anyone calls a real number, the agent says the call is recorded, or recording is off on the phone line. A public tunnel exposes only the Twilio and Quo routes, not the call page or `/api/offer`, and every inbound request has its Twilio or Quo signature verified.
- Why: every call is saved today and nothing says so, and the server is safe only because it listens on localhost. A tunnel as things stand would let anyone with the URL start calls on paid keys.
- Revisit if: the phone work is dropped.

## D20. SMS stays simulated (2026-11-06)
- Options: (A) real texts through OpenPhone; (B) the simulated chat view from the kickoff notes.
- Chosen: B, agreed with Scope Health. Their numbers that can text US phones run production workflows, and a new registration is not worth it for a demo.
- Why: the take-home is judged on how the fallback resumes from state, which is the same in both. Keeping `send_text` behind one function leaves real SMS as a small change later.
- Revisit if: a registered number becomes available.
- Update: calls stay simulated in the browser too, also agreed with Scope Health, so D19 no longer applies. Poor phone audio is simulated by `tests/phone_line.py` instead.

## D21. A bare yes only answers a question the patient heard (2026-11-06)
- Options: (A) leave it, since only the price is gated today; (B) ignore any reply that was interrupted; (C) treat a line as heard once the patient cuts in during its last 1.5 s, otherwise mark the question unheard, and refuse a yes or no to an unheard question unless the patient names the topic.
- Chosen: C, and the same test now decides whether a cut-off price counts as stated.
- Why: on a real call the patient said "Um, okay. Go ahead." with a short gap. "Um, okay" confirmed the pharmacy, and "Go ahead" then landed about a second into the insurance question and recorded it as confirmed, so the patient never heard it. They said so: "You never told me my Aetna number." B would throw away real early answers such as "Yes" over the end of a line, which people do all the time. The voice speaks 2.2 to 4.2 words a second, so the estimate of 2.5 errs towards unheard, and a wrong guess costs one repeated question.
- Revisit if: word timings from the speech service make the estimate unnecessary.

## D22. Wanting another pharmacy is a stored fact (2026-11-06)
- Options: (A) keep it implied by the last agent line; (B) store it on the session.
- Chosen: B, as `wants_other_pharmacy`. The pending question reads it, so it survives a re-ask and a dropped call, and a bare yes no longer means the pharmacy on file while the patient is choosing another one.
- Why: "I would like to change my pharmacy" got "Sorry, I didn't catch that" twice on one real call, and once more on another, because nothing was recorded yet to clear. After any unclear turn the agent also fell back to asking about CVS again, right after the patient refused it.
- Revisit if: more steps need a sub-question, when a small stored sub-step would be cleaner than one flag.

## D23. A question the agent cannot answer still gets an answer (2026-11-06)
- Options: (A) repeat the pending question; (B) say a fixed line, then repeat it.
- Chosen: B. If the guard removes the model's whole answer, the patient hears "I can't help with that, but a pharmacist can." and then the pending question. "Is this call recorded?" and "When will it be ready?" also get fixed answers, because real callers asked the first one twice.
- Why: "Is this call recorded?" and "Any discount?" were met with the same question again, as if the patient had said nothing. That is the most robotic thing the agent did on real calls.
- Revisit if: the fixed line turns out to send too many callers to a pharmacist.

## D24. When the agent can no longer hear or speak, the call falls back to text (2026-11-06)
- Options: (A) leave the call open; (B) retry forever; (C) say so if it still can, end the call, and let the normal dropped-call path send the text.
- Chosen: C. Pipecat already retries a broken speech connection three times, and then reports the service as unusable. That event now ends the call.
- Why: during testing the speech service refused a session mid-call, and the agent went on speaking to a caller it could no longer hear, with no way out but hanging up. A dropped call is exactly what the SMS path is for, so nothing new is needed after the call ends. The same applies to a caller the agent simply cannot pick up, so a third idle timeout takes this path too, after 30 s of silence, or 90 s after "hold on".
- Revisit if: a second speech provider is added, when failing over would beat ending the call.

## D25. The agent ignores speech that is not meant for it (2026-11-06)
- Options: (A) answer everything the speech service hears; (B) let the model mark speech as background, stay silent on it, and move the call to text if it keeps happening; (C) rely on noise suppression alone.
- Chosen: B, with voice focus tested separately (D27). The model has a `background` reading for a TV, a radio or a person talking to someone else. On a call the agent says nothing unless that speech cut off its question, when it finishes the question. After six turns in a row it cannot use, it says it is having trouble hearing and moves to text. Keys and "say that again" do not count towards the six.
- Why: with a news broadcast playing at 10 dB below the caller, the agent replied to the TV every few seconds for more than three minutes, and the simulated caller never got a word in. No data was wrong, but a real person would have hung up.
- Revisit if: silence on a real caller's unclear speech turns out to feel like the line went dead. The idle prompt after 10 s is the safety net for that today.

## D26. Speech recognition is pinned to English, and only a real transcript counts as a turn (2026-11-06)
- Options for language: (A) leave automatic detection on; (B) pin English. Options for empty turns: (A) keep reading the last message in Pipecat's context; (B) accept only `user` messages, and handle a turn with no transcript in code.
- Chosen: B for both. When a turn ends with no transcript and the agent's line was cut, the agent repeats the pending question, and otherwise it stays quiet. Turns that arrive after the call has ended are dropped.
- Why: an Indian-accented caller on a noisy line came through in Hindi script, so a correct name and date never reached the model. Pipecat's recovery for an empty turn adds an instruction as a `developer` message. The bridge read the last message whatever its role, so that instruction was fed to the agent as the patient's words. A late turn also reopened a closed call and landed in the SMS thread.
- Revisit if: the line needs to serve Spanish or other languages, when the language has to become a deliberate choice.

## D27. Voice focus stays off (2026-11-06)
- Options: (A) AssemblyAI's `near-field` voice focus, which removes background chatter; (B) leave it off.
- Chosen: B. In an A/B of 12 simulated calls per arm, on the same callers and lines, near-field finished 6 calls against 8 without it, and needed 48 re-asks against 26. Neither arm recorded a wrong value.
- Why: on these lines it removed parts of the caller's speech as well as the background. Twelve calls is a small sample, so this is no verdict on the feature, but nothing showed a benefit.
- Revisit if: real phone audio replaces the simulation, since the feature is meant for handsets.

## D28. A later request blocks a stale text from sending a second refill (2026-11-06)
- Options: (A) block any repeat of a drug for a patient; (B) block a text thread only when that drug was sent after the text went out.
- Chosen: B. The old thread closes with "already in".
- Why: a call dropped at the last step, the patient called back and finished, then answered the old text with yes, and two requests were sent for one refill. A wider block would stop anyone from running the demo twice, and a real pharmacy's refill-too-soon rule belongs in its own system.
- Revisit if: requests start carrying a fill date.

## D29. The agent ignores its own voice (2026-11-06)
- Options: (A) trust the phone or browser to cancel echo and let a bad line fall back to text; (B) cancel echo on the server against the audio it sent; (C) compare what is transcribed with the line being spoken.
- Chosen: C. Echo starts where the line starts, so a finished transcript that is exactly the opening words of the line now playing is dropped, whoever said it, and it never interrupts. Two or more opening words, or five of the line's words in a row, prove echo for the rest of the call. Once proven, finished transcripts made mostly of the line are dropped too, and runs of two or more of its words are removed from any turn that started over the line or within 1.5 seconds of it ending. While the agent talks, a partial transcript made of its own line does not interrupt it.
- Why: with 18 dB of echo and no cancelling, the agent cut itself off every couple of words and a silent caller had Lisinopril picked by the agent's own "I see Lisinopril". The first version needed five words in a row as proof, and that failed: the recognizer finished fragments like "Hi," so early that every line was cut before five words arrived. A plain word match would throw away real answers, since "Metformin" and "for blood pressure" repeat the list, so before proof those still go through as normal.
- Cost: a real "Hi" over the greeting, or any word that opens the line being spoken, is ignored. On a proven echo call, an answer spoken over the agent that mostly quotes the line has to be repeated once the line ends. A one-word partial matching the line waits for the finished transcript before interrupting, on every call. B is much more code for a case phones and browsers mostly handle already.
- Revisit if: real phone audio shows echo that the transcript cannot match.

## D30. Keyterms go into the live speech session after verification (2026-11-07)
- Options: (A) keep Pipecat's settings update, which reconnects; (B) send the pharmacy's terms when the session opens; (C) send the patient's terms over the open session with an `UpdateConfiguration` message.
- Chosen: C, through a small subclass of Pipecat's service, since Pipecat only updates two other settings live. The terms are also stored, so a network reconnect keeps them.
- Why: D12 assumed keyterms could change mid-stream, but Pipecat 1.15.0 reconnects for that setting. Every call used two speech sessions and dropped about half a second of audio as the patient was about to name a drug, and on the free tier's 5 new sessions a minute 28 of 120 test calls were refused. B fixed that and caused something worse: with drug names boosted during identity, "My full name" was heard as "Metformin", read as an early answer, and a refill for a drug nobody named went through. Keyterms belong only where those words are expected. AssemblyAI's API reference lists `keyterms_prompt` as updatable, and a live probe was accepted with no error or disconnect.
- Cost: `_send_update_configuration` is a private Pipecat method, so an upgrade could break it.
- Revisit if: Pipecat adds keyterms to its live settings.

## D31. A question counts as heard when the question was spoken (2026-11-07)
- Options: (A) keep timing the whole line; (B) time up to the last question mark; (C) let a repeated yes through after enough tries.
- Chosen: B, and repair lines stop growing: "Let me finish the details first." is said once in a row, and "Sorry, I need this to keep going." becomes "One more time."
- Why: the keypad hint comes after the question, so a cut during the hint marked a fully spoken question as unheard. In a cafe the background voices cut every re-ask, each one longer than the last, and a patient saying "Yes, that's fine" was refused for four minutes. C would weaken D21, which protects the send step, so the limit in D32 handles what is left.
- Revisit if: word timings from speech output replace the words-per-second estimate.

## D32. Six turns without progress of any kind move a call to text (2026-11-07)
- Options: (A) keep D25, where only an unclear turn can trigger the move; (B) count every turn that changes nothing, including refused answers and repeated identity follow-ups.
- Chosen: B, with a run-together name such as "Sarachin" now matched against the full name when the date is exact, so that this limit is reached less often.
- Why: refused turns raised the count but could never fire it, so calls ran to the test's 241-second limit. A patient should reach a working channel in under a minute, not repeat themselves until they hang up.
- Revisit if: patients who are slow but progressing get moved to text in real calls.

## D33. In a noisy room, words that have stopped arriving end the turn (2026-11-07)
- Options: (A) wait for the voice detector to hear silence, as Pipecat does; (B) raise the detector's threshold; (C) end the turn when no new words have arrived while the detector still reports speech; (D) C plus a cap on the total wait.
- Chosen: C, starting from a finished transcript. The wait is at least 1.5 seconds and longer if the slot model says the answer is unfinished. It only runs while the detector is stuck on, so quiet calls take the same path as before.
- Why: background voices kept the detector on, so a finished answer sat for 6 to 22 seconds. The caller repeated themselves and the conversation fell a turn out of step. B would also lose quiet callers. A cap would help a TV, which keeps producing words, but it would cut off anyone giving a long answer in a quiet room.
- Result: on street calls most worst gaps fell from 6-7 seconds to 2-3 and calls got shorter. Cafe calls did not improve, with gaps still 16 to 24 seconds. Asking the recognizer for its final words with `ForceEndpoint` when partial words went stale made no difference over 12 calls, so that code was removed.
- Cost: cafe voices and continuous TV speech still hold turns open, so those calls stay slow and often fall back to text.
- Revisit if: noise is rare in real calls, or a speaker-aware detector replaces this.

## D34. Ending a call needs words that support it (2026-11-07)
- Options: (A) trust the model's `end_call`, as before; (B) gate it on the patient's words like every other tool that changes state.
- Chosen: B. A refused `end_call` just asks the pending question again.
- Why: in about 1 run in 15, the model read "and i go to the CVS by the church" as the patient leaving, and the call ended on the first sentence. It was the one action with no evidence check.
- Cost: a goodbye in words the gate does not know gets a repeat of the question instead of a close.
- Revisit if: real calls show patients stuck trying to leave.

## D35. Noise during the model read no longer loses the answer being read (2026-11-07)
- Options: (A) skip the unheard mark while a turn is being read, since the turn loop already marks a dropped reply once the read is done; (B) keep the mark and stop refusing when the same question played in full earlier; (C) ignore speech starts during reads.
- Chosen: A.
- Why: the 120-call matrix had 22 lines of "Let me finish the details first" in 17 calls. 16 were right: "Yes... correct" split in two and the tail landed on a question that had not played. The other 6 were a bug. A speech start during the read (room noise, in these calls) saw a reply that had not played and marked the question unheard, so the gate refused an answer to a question the patient had heard. One British cafe call lost "Yes, that's fine." four times until it ran out of time. A only moves the mark to after the read, so a new question that never played still counts as unheard and the price still has to be heard. B needs more state and could let a price through unheard. C would lose real talk-over.
- Test: `room_noise_during_the_read_keeps_the_answer` plays short babble inside the read. It failed 2 of 4 runs before the fix and passed 6 of 6 after.
- Revisit if: a reply dropped by noise needs its own repair line.

## D36. A pharmacist flag for a clinical question needs more than agreement (2026-11-07)
- Options: (A) leave it, since a needless callback is the safe side; (B) refuse `clinical_question` when every word in the message is plain agreement; (C) a clinical word list that the message must match.
- Chosen: B.
- Why: after "A pharmacist can help with that. Should I send it to CVS?", "Yes, that one is fine." was flagged in 4 of 12 runs, and the patient heard "I can't advise on that". I first called this a 1 in 200 slip, which was the rate over the whole typed set, not on this path. B cannot block a real report, because any word outside the small agreement list lets the flag through, so "yes I feel dizzy" still works. C would miss symptoms nobody listed. Urgent and crisis flags stay ungated.
- Cost: a vague "is that okay?" gets the question again instead of a callback.
- Revisit if: patients report something clinical using only those words.

## D37. Common confirmations skip the model (2026-11-07)
- Options: (A) keep the short list of exact yes phrases; (B) add common general confirmations; (C) add every phrase the test callers use.
- Chosen: B.
- Why: on a single clean call the median gap was 1.67 s against a target of about 1 s, and the model read took 786 ms of that. "Yes that's right", "yes that's fine" and "yes ma'am" have one meaning at a yes or no question, and the exact path still goes through every tool gate. C would only make the benchmark look better, so sentences like "Yes, that's still my insurance" stay with the model.
- Cost: longer answers still pay for the read, so the target is not met on those turns.
- Revisit if: a phrase on the list turns out to mean something else at one of those steps.

## D38. A yes and a cancel in one answer asks again (2026-11-07)
- Options: (A) when the answer at the send question has both a yes and a cancel word, ask the question again; (B) check every decline with a second question before ending; (C) keep ending the call on any cancel word.
- Chosen: A.
- Why: in the final matrix "Yes, you can send." came back as "Yes, you cancel". The cancel word ended the call with "No problem, I won't send it", so a patient who said yes lost the refill. The words disagree, so code should not pick one. B costs every real decline an extra turn, and A costs only the rare mixed answer.
- Cost: "Yes, cancel it" now takes one more turn. A mixed answer misheard again and again ends in the text fallback, not a refill.
- Revisit if: mixed answers show up often enough that a clearer prompt ("yes or no") is worth it.

## Open questions

None at the moment. Earlier references use these numbers: Q1 is D8, Q2 is D10, Q3 is D1, Q4 is D2, Q5 is D12, Q6 is D13, Q7 is D3, Q8 is D11, Q9 is D14, Q10 is D4 and Q11 is D9.
