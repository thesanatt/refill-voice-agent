# QA

This file covers what I tested, what the tests found and what still fails. Numbers are first runs unless I say otherwise. Synthetic voices on a simulated phone line stand in for real callers, so the call numbers show failure modes. They are not a success rate for real patients.

## Results on the submitted code

| Test | What it covers | Result |
|---|---|---|
| Exit checks | the flow end to end, by text | 0 failures |
| Typed scenarios, 2 runs each | 403 scenarios in five sets | 805/806, 0 invariant violations. The miss is the known store-hours flake |
| Hostile input | forged tool calls, racing messages, junk text | 0 failures, 0 violations |
| SMS fallback | a call dropped at every step, finished by text | 15/15 |
| Call moves | talk-over, noise, keypad, the cancel check, echo, drops and outages | 27/27 |
| Call matrix | 10 caller voices x 12 line conditions | 98 correct, 21 moved to text, 0 locked out, 0 wrong, 1 declined by a mishearing |
| Invariants on every call database | rules that must hold however a call went | 146 sessions in 30 databases, 0 violations |
| Shared link | 3 full calls from my Mac to the Railway link, 2 forced through the relay only | all finished as scripted, median reply 1.12 to 1.42 s |

In the matrix, "correct" means the refill went in with the right drug, pharmacy and insurance. "Moved to text" is the designed fallback after six turns without progress or three unanswered prompts: the call ends and the SMS picks up at the same step. "Locked out" means identity failed twice and a pharmacist was offered. "Wrong" means a drug, pharmacy or insurance answer was written that the caller did not give. That is the number that has to stay at 0. Pharmacist callback flags are counted separately under known limits.

D38 and D39 came after the call matrix. D39 adds a check before a cancel on a call: "Do you want to cancel this refill? Press 1 to cancel it, or 2 to keep going." No matrix caller cancels, so those calls never reach it, and the matrix numbers are from the code just before D38. Everything else ran again on the code as submitted: every typed set twice, the exit checks, hostile input, SMS fallback and all the moves. D39 changed the expected ending of 8 typed scenarios where a caller cancels on a call. Each now answers the check with "Yes." The one typed miss was `edge_asks_store_hours`, the flaky path under known limits. It passed 6 of 6 on a rerun, and it has now passed 17 of 20 runs.

## How the tests work

- **Typed sets.** `tuned` holds the scenarios I fixed bugs against, and `holdout` was written afterwards with new wording. `real` replays failures from my own calls. `blind` and `blind2` were written by a separate agent that never saw the code. Every run is also checked against `tests/invariants.py`: no protected data before verification, no medical advice, prices from the copay table, at most one request per session, no request before verification or before the price was stated, no verification after a lockout, and at most one disconnected text.
- **Hostile input.** These call the tool layer directly, so they hold even if the model is fooled completely.
- **Phone line.** `tests/phone_line.py` runs the caller's voice through G.711 or low-rate Opus (standing in for AMR, which stock ffmpeg lacks), bursty packet loss, dropouts, room echo and a noise bed that keeps playing between sentences. Cafe and TV backgrounds are made from real synthesized speech, so they contain actual words.
- **Caller.** The caller answers whatever the agent asks and repeats itself when it is not heard, so a misheard date costs another try rather than breaking the test.
- **Moves.** Scripted calls that need real timing: talking over the agent at chosen moments, slow speech, noise bursts, noise during the model read, side talk, long silence, the keypad, the agent's own voice leaking into the microphone, a drop at every step, and each outside service failing.

## Latency

Single calls, one at a time, measured at the caller from the end of the patient's speech to the first agent audio:

| Script | Turns | Median | Max |
|---|---|---|---|
| `happy`: full sentences, read by the model | 15 | 1740 ms | 3311 ms |
| `short_answers`: "Yes.", "Yes, that's right." | 15 | 861 ms | 2161 ms |

The target is about 1 second. Short answers meet it and turns the model reads do not. On the server, a model-read turn breaks down like this: the VAD waits 200 ms of silence, the turn ends a median 243 ms after that, the model read takes 786 ms and the first audio leaves 117 ms after the text is ready. The rest is WebRTC and the caller. The name and date turn is the slowest at about 2 seconds, because verification can trigger a second read for early answers.

The per-condition gaps in the matrix come from 4 calls running at once over degraded audio, so they are not a latency measure.

## What testing found

- **Blind set 1** first run: 90/152. About 49 failures were mistakes in the scenarios. 13 were real bugs, and the code was then fixed against this set.
- **Blind set 2** first run, before any change: 91/100 with 0 violations. 5 test mistakes, 1 safe over-caution and 3 real gaps.
- **Speech sessions refused.** Updating recognizer keyterms through Pipecat settings reconnected the speech session, and the free AssemblyAI tier (5 new sessions a minute) refused 28 of 120 calls.
- **The only wrong write in any call run** came from the first fix for that. Keyterms set when the call connected made "My full name" come back as "Metformin", which was recorded as an early answer. Keyterms now go into the live session only after verification (D30).
- **The agent's own voice** leaking into a speakerphone microphone could pick a drug for a silent caller (D29).
- **Real yeses refused** because "heard" was timed to the end of the keypad hint, and loops that ran four minutes because only unclear turns counted toward the limit (D31, D32).
- **Calls ended on a misread.** The model sometimes read "and i go to the CVS by the church" as the patient leaving, about 1 run in 15. Ending a call now needs supporting words (D34).
- **Noise during the model read** marked the question unheard and refused the answer being read. This hit 6 lines in 120 calls, and one cafe call ran out of time (D35).
- **A false pharmacist flag** on "Yes, that one is fine." fired in 4 of 12 runs on that path. I first called it 1 in 200, which was the rate over the whole typed set, not on this path (D36).
- **A yes taken as a decline.** In the final matrix "Yes, you can send." was transcribed "Yes, you cancel". The cancel word ended the call without sending the refill. An answer with both words got the question again (D38). Then every cancel on a call got a check with the keypad offered (D39).

## Call matrix over time

| Run | Code | Correct | Moved to text | Locked out | Wrong |
|---|---|---|---|---|---|
| First full run | keyterm reconnect bug | 70 | 48 | 2 | 0 |
| After D30 first version | connect-time keyterms | 94 | 22 | 3 | 1 |
| Frozen before D35 to D37 | | 100 | 19 | 1 | 0 |
| Final | code just before D38 | 98 | 21 | 0 | 0 |

One run on the final code is left out of these numbers. The Mac went to sleep during it, and the power log matches every stall to the second. 8 calls lost their audio and two moves failed at the same moments. I reran the moves and the matrix under `caffeinate`.

The final run adds up to 120 with one call that fits none of the columns: the patient said yes and the refill was declined. That is D38, below.

### The final run in detail

| Line condition | Correct of 10 | Re-asks per call |
|---|---|---|
| clean | 10 | 0.0 |
| landline (G.711) | 10 | 0.2 |
| quiet mic | 10 | 0.4 |
| clipped mic | 10 | 0.3 |
| car | 10 | 0.6 |
| mobile, good | 9 | 0.4 |
| mobile, weak | 9 | 0.1 |
| mobile, breaking up | 9 | 1.5 |
| speakerphone | 9 | 0.7 |
| cafe | 8 | 1.5 |
| street | 4 | 0.3 |
| TV on | 0 | 2.9 |

| Caller | Correct of 12 |
|---|---|
| British | 11 |
| older man | 11 |
| Spanish accent | 11 |
| Arabic accent | 10 |
| Mandarin accent | 10 |
| plain | 10 |
| my recorded voice | 10 |
| Southern | 10 |
| rushed | 8 |
| Indian accent | 7 |

Why the 22 calls that did not finish by voice ended as they did:

| Cause | Calls |
|---|---|
| TV speech fills the transcript and holds the turn open | 10 |
| a one-word yes in street or weak-signal noise produced no transcript | 6 |
| the name never came through (cafe babble, street noise, a Devanagari transcript) | 3 |
| "Lisinopril" heard as "April" on a breaking line | 1 |
| the test caller's 4-minute limit, after a cafe turned "3 15 85" into "$315.85" | 1 |
| "Yes, you can send" heard as "Yes, you cancel", declined (fixed in D38) | 1 |

Each of the 21 texts picked up at the step where the call stopped. A two-part answer such as "Yes, correct." split into two turns in 20 of the 120 calls, and 7 of those were the Indian-accent voice. The second half lands on the next question and is refused, so the patient hears that question again. The Indian-accent voice still failed mostly to noise, and the split was part of only two failures.

## Known limits

- The accents are synthetic voices, and my real recordings are one speaker. There is no AMR codec.
- With a TV on, every call falls back to text. Continuous speech holds the turn open and the gaps reach 15 to 20 seconds.
- In a cafe, gaps are long and calls often fall back to text.
- A short "Yep" in street or car noise sometimes produces no transcript at all. After two prompts the call moves to text.
- A misheard name with a right date leads to a lockout, because the retry asks only for the date. That follows `CLAUDE.md` and the kickoff notes, which cover a wrong date. A retry that asks for both is the alternative. The final run had no lockouts, but all 3 in the run left out for sleep were this case ("Star Ocean", "Sarah's Pet", "Sir Chen").
- Pharmacist callbacks can be flagged that nobody needed. In the final run a TV saying "talk to your pharmacist" set one, and "Is the lisinopril cheap?" heard as "clean" set another. Both were in the 146 audited sessions. Neither breaks a rule, but each would cost a pharmacist a call.
- A drug name misheard on a breaking line ("April" for Lisinopril) is never guessed, so the call moves to text.
- A family member who gives the patient's name and date of birth verifies as the patient.
- The recognizer once wrote a date in Devanagari, even with English pinned.
- "Yes... correct" can split into two turns. The tail lands on the next question, which is correctly refused, so the patient hears that question one extra time.
- After the agent offers a pharmacist for an off-topic question, the model sometimes reads the next "yes" as only a clinical flag. The gate refuses the flag, and the patient hears one "Sorry, I didn't catch that" (2 of 12 runs).
- A model read times out after 4 seconds with one retry. Under heavy parallel load this produced one "Sorry, I didn't catch that" in a typed run.
- Keyterms go to AssemblyAI through `_send_update_configuration`, a private Pipecat method.
- The echo test mixes the agent's voice back only while the caller is silent, so it does not cover double talk.
- The shared link adds 0.25 to 0.56 s to the median reply (relay and hosting), measured on three calls against 0.86 s locally.
- The relay's free plan carries 500 MB a month, a few hundred calls. Past that, calls that need the relay fail until the next month.
- A cancel answered with "Yes, you can send" misheard as "Yes, you cancel" a second time would still cancel. The keys are offered so the caller doesn't have to rely on speech.

## Running the tests

```bash
uv run python -m tests.exit_checks
uv run python -m tests.run_scenarios --set blind2 --runs 2
uv run python -m tests.hostile_inputs
uv run python -m tests.sms_fallback --runs 3
uv run python -m tests.phone_moves
uv run python -m tests.phone_calls --servers 4 --out matrix.json
uv run python -m tests.voice_call --script short_answers --calls 3
```

The call tests need the AssemblyAI, Inworld, ElevenLabs and Anthropic keys in `.env`. `phone_moves` and `phone_calls` start their own servers on temporary databases. `voice_call` calls the server you started, so its calls land in `refill.db`. Keep the machine awake during call runs, for example with `caffeinate -ims`.
