# Testing the refill line

This is a voice agent for a pharmacy phone line. You call it from the browser, verify as the demo patient and request a refill. If the call drops before the refill is in, the agent texts you and you finish by text. The phone call and the texts are both simulated on the same page.

This guide shows you how to try every part of it. It also covers what I measured and what still fails. The first test takes two minutes. The rest are short tries, and you can do them in any order.

## Before you start

- Use Chrome or Edge on a laptop and allow the microphone. Headphones help, but the agent also ignores its own voice on a speaker.
- The **Start call** button is on the left and the texts are on the right. Your browser gets its own made-up number, so other testers never see your texts.
- Calls are recorded and transcribed so I can review them. Please use the demo patient below and none of your own details.
- The keys under the call button work like a phone keypad. You can also type them: `1` and `2` answer the cancel check, `*` goes back one question and `#` starts over.

**Demo patient**

| Detail | Value |
|---|---|
| Name and date of birth | Sarah Chen, March 15, 1985 |
| Prescriptions | Lisinopril 10 mg (blood pressure), Metformin 500 mg (diabetes), Atorvastatin 20 mg (cholesterol) |
| Pharmacy on file | CVS Pharmacy, 1234 Main St |
| Other pharmacies in the system | Walgreens, 500 Oak Ave, and Walmart Pharmacy, 88 Market St |
| Insurance | Aetna PPO, member ID ending in 8912 |
| Copays | Lisinopril $5, Metformin $10, Atorvastatin $15 |

## 1. The normal call

1. Press **Start call** and wait for the greeting.
2. Say "Sarah Chen, March fifteenth, nineteen eighty-five."
3. Pick a medication, for example "Metformin."
4. Say "yes" to the pharmacy and "yes" to the insurance.
5. Listen to the copay and the summary, then say "yes."

The agent says the request is in and hangs up. Short answers like "yes" get a reply in about a second. Answers the model has to read take one and a half to two seconds, and a short working phrase covers the wait. Every drug, pharmacy, plan and price you hear comes from the database. The model never writes those sentences.

## 2. Try to break it

### Identity and privacy

| Try this | What should happen |
|---|---|
| Ask "What medications am I on?" before giving your name | It gives no data and asks for your name and date of birth |
| Give the right name with a wrong date (March 14) | It asks for the date of birth once more |
| Get the date wrong twice | It stops verifying and offers a pharmacist callback |
| Say the date oddly: "three fifteen eighty-five" or "the fifteenth of March, '85" | It still matches |
| Ask about someone else's prescriptions | It refuses |

### Talking like a person

| Try this | What should happen |
|---|---|
| Say everything at once: "Sarah Chen, March 15 1985, I need my Metformin" | It verifies you and skips the medication question |
| Talk over the agent in the middle of a sentence | It stops and listens. A short "mm-hm" does not stop it |
| Change your mind: "actually, make that Lisinopril" | It switches and moves on |
| Press `*` | It goes back one question |
| Press `#` | It clears your answers and starts again after verification |
| Say "hold on" | "Sure, take your time." |
| Stay quiet after a question | It asks again, then says "Take your time." After the third quiet prompt it moves you to text |
| Ask "What is Metformin for?" | "Metformin is for diabetes," from the prescription list |
| Ask "Is it safe with alcohol?" | It gives no medical advice and flags a pharmacist to call you back |
| Say "I want to talk to a pharmacist" | It flags a callback and keeps the call going |
| Say "I have chest pain" | It tells you to call 911 and flags a pharmacist |
| Ask for two refills at once | It does one at a time |

### Different answers

| Try this | What should happen |
|---|---|
| "No, send it to the Walgreens on Oak" | It switches to the listed Walgreens |
| "No, send it to the Rite Aid on State Street" | It notes that pharmacy for a transfer |
| "No, I have new insurance" | It flags that for the pharmacy |
| "I'm not sure" about the insurance | It says the pharmacy can check |

### Cancelling

Cancelling is the one answer that ends a call with nothing sent, so on a call the agent checks it once.

| Try this | What should happen |
|---|---|
| At the summary say "No, don't send it" | "Just to be sure. Do you want to cancel this refill? Press 1 to cancel it, or 2 to keep going." |
| Then press `1` or say "yes" | It cancels and hangs up |
| Or press `2` or say "no" | It reads the summary again |
| Or say "No, send it" | It sends the refill |
| Cancel by text instead | It cancels right away, because a typed text can't be misheard |

### The call drops

This is the main extension. End the call partway through with **End call**.

| Try this | What should happen |
|---|---|
| Hang up after picking a medication | A text arrives: "Looks like we got disconnected! I have your Metformin refill started..." with the next question |
| Answer by text until it is done | It picks up from the step where the call stopped and never asks again for something you already answered |
| Hang up before verifying | The text asks for your name and date of birth and names no drug |
| Hang up at the summary | The text gives the price and asks whether to send it |
| Text "stop" | It stops and waits |
| Call back after a drop and finish on the call | The refill goes in once. A yes to the old text does not send a second one |

## 3. What is built

- **Voice pipeline:** AssemblyAI Universal-3 Pro streaming speech-to-text, then Claude Haiku 5.5 calling tools, then Inworld text-to-speech. It runs on Pipecat over WebRTC.
- **State lives in the database, not in the model.** Each answer is written to SQLite as it happens, and the current step is worked out from the stored fields. The model reads the patient's words into tool calls. The tool layer checks every call against the state and against the patient's actual words before anything is written.
- **Code says the facts.** Sentences that carry a drug, pharmacy, plan or price are built from tool results, never generated.
- **Turn-taking:** talking over the agent, early answers, questions back to the agent, a guard against the agent's own voice, short working phrases while the model reads, and a fall back to text when a call can't hear the caller.
- **SMS fallback** from any step, resuming at the derived step.
- **A refill request record** holding every collected field, created only after the patient heard the price and said yes.
- **Keypad:** `1` and `2` for the cancel check, `*` to go back and `#` to start over.

The design decisions and the reasons behind them are in `docs/DECISIONS.md`.

## 4. What testing showed

The full results are in `docs/QA.md`. In short:

| Test | Result |
|---|---|
| Typed scenarios, 2 runs each, five sets (two written by an agent that never saw the code) | 805/806, 0 rule violations. The miss is a known flaky path |
| Hostile input: forged tool calls, racing messages, junk text | 0 failures |
| A call dropped at every step and finished by text | 15/15 |
| 27 scripted calls that need real timing (talk-over, noise, keypad, the cancel check, echo, outages) | 27/27 |
| 120 calls: 10 synthetic voices x 12 phone-line conditions | 98 correct, 21 moved to text, 0 locked out, 0 wrong |
| Median reply time on a single local call | 0.86 s for short answers, 1.74 s for answers the model reads |
| 3 calls to this link from my Mac, 2 forced through the relay only | all finished, median reply 1.12 to 1.42 s |

"Wrong" means a drug, pharmacy or insurance answer written that the caller did not give. It stayed at 0 in every final run.

## 5. Known limits

- With a TV talking in the background, calls fall back to text. Busy streets and cafes often do too.
- The target is about one second, and answers the model has to read miss it.
- If the agent mishears your name but gets the date right, the retry asks only for the date, so you can end up locked out.
- A family member who knows the patient's name and date of birth verifies as the patient.
- The test voices are synthetic, and my own recordings are one speaker.
- The relay that carries call audio on the shared link has a free monthly allowance. If it runs out, calls from strict office networks may not connect.

## 6. If something goes wrong

Tell me roughly when you called and what you said. Every call has a transcript and a recording, so I can find it.
