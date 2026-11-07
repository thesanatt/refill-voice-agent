# Prescription Refill Voice Agent

A voice agent for a pharmacy phone line. A patient calls, verifies their identity and requests a refill: they pick a medication, confirm the pharmacy and insurance, hear their copay and say yes. If the call drops first, the conversation continues by text from the step where it stopped. The phone call and the texts are both simulated in the browser.

**Try it:** [refill-line-production.up.railway.app](https://refill-line-production.up.railway.app/?code=6f828e8858) in Chrome or Edge, with the microphone allowed. The demo patient is Sarah Chen, born March 15, 1985. [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) walks through every feature, and the call page links to it.

## How it works

- AssemblyAI Universal-3 Pro streaming turns speech into text, Claude Haiku 5.5 reads each message into tool calls, and Inworld speaks the reply. Pipecat runs the pipeline over WebRTC.
- Every answer is written to SQLite as it happens, and the current step is derived from the stored fields. Nothing collected lives only in the model's context.
- A tool layer checks each call against the state and against the patient's own words before anything is written. Sentences that carry a drug, pharmacy, plan or price are built by code from tool results, never generated.
- One turn function serves both channels, so the text fallback resumes exactly where the call stopped and never asks again for an answer it has.

## Documents

| File | What it holds |
|---|---|
| [docs/TAKE_HOME.md](docs/TAKE_HOME.md) | The problem statement, verbatim |
| [docs/KICKOFF_NOTES.md](docs/KICKOFF_NOTES.md) | Clarifications from the kickoff call |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every design decision, D1 to D40, with the options and the reason |
| [docs/QA.md](docs/QA.md) | What I tested, the results and the known limits |
| [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) | How to try every feature on the shared link |
| [agent-logs/](agent-logs/) | The coding-agent sessions, with API keys redacted |

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches the right Python version itself. Then copy the environment file and add your keys.

macOS or Linux:

```bash
cp .env.example .env
uv sync
```

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
uv sync
```

The text loop needs `ANTHROPIC_API_KEY`. A voice call also needs `ASSEMBLYAI_API_KEY` and `INWORLD_API_KEY`. `ELEVENLABS_API_KEY` is only used by the automated caller in `tests/`.

## Make a voice call

```bash
uv run python -m refill.server
```

Open http://127.0.0.1:7860 in Chrome, allow the microphone and press Start call. The demo patient is Sarah Chen, born March 15, 1985.

During a call, the page shows four keys, which also work from a computer keyboard: `*` goes back one answer, `#` returns to the main menu, and `1` and `2` answer the check before a cancel ("Press 1 to cancel it, or 2 to keep going").

If a call ends before the request is complete, a text appears in the pane beside the call and the conversation continues there from the step where it stopped.

Each call is saved to `recordings/<session id>.wav`, which git ignores. The patient is on the left channel and the agent on the right.

## Deploy a shared link

The `Dockerfile` runs the same server on Railway (D40). Railway takes no inbound UDP, so call audio goes through a TURN relay, and the server and the page both read the relay list from `TURN_ICE_SERVERS`. Set these on the host, never in a committed file:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY`, `ASSEMBLYAI_API_KEY`, `INWORLD_API_KEY` | as locally |
| `TURN_ICE_SERVERS` | the relay's ICE server list as JSON, from the relay provider |
| `DEMO_ACCESS_CODE` | any string. The link only works as `https://<host>/?code=<it>` |
| `REFILL_DB_PATH`, `REFILL_RECORDINGS_DIR` | paths on a volume, for example `/data/refill.db` and `/data/recordings` |
| `MAX_ACTIVE_CALLS` | calls at once, 3 by default |

Then deploy with `railway up`. Leave `DEMO_ACCESS_CODE` and `TURN_ICE_SERVERS` unset locally, so local calls stay direct.

## Run the text loop

```bash
uv run python -m refill.cli
uv run python -m refill.cli --channel sms
```

Type as the patient. `/state` prints the session, and `/drop` simulates a dropped call. The default channel uses spoken wording, and `--channel sms` uses written wording.

The database is `refill.db`, created and seeded on first run. Delete it to start clean.

## Tests

Results, what the tests found and the known limits are in `docs/QA.md`.

```bash
uv run python -m tests.exit_checks
uv run python -m tests.sms_fallback --runs 3
uv run python -m tests.run_scenarios --set tuned --runs 3
uv run python -m tests.run_scenarios --set holdout --runs 3
```

These run typed conversations against a temporary database and assert on the stored rows. `tuned` holds the scenarios the code was fixed against, and `holdout` holds wording written afterwards. `real` replays failures from real calls, and `blind` and `blind2` were written by a separate agent that could not see the code. Every run also applies the rules in `tests/invariants.py`, which you can point at any database:

```bash
uv run python -m tests.invariants refill.db
```

Attacks that skip the model, such as forged tool calls, racing messages and junk input:

```bash
uv run python -m tests.hostile_inputs
```

Simulated phone calls run over real WebRTC, each on its own server and temporary database. `tests/phone_line.py` degrades the audio with 8 kHz codecs, packet loss, dropouts, room echo and background noise that keeps playing between sentences. The caller reacts to what the agent asks, so a misheard date costs another try rather than a broken test. AssemblyAI limits a free account to 5 new sessions a minute, so the calls pace themselves.

```bash
uv run python -m tests.phone_calls --conditions landline,cafe --personas plain,older_man
uv run python -m tests.phone_moves
```

`phone_moves` covers what needs real timing: talking over the agent at chosen moments, slow speech, noise, the agent's voice leaking into the microphone, a call dropped at every step and finished by text, and each outside service failing.

With the server running, this places real WebRTC calls using an AI patient voice, and reports the gap from the end of the patient's speech to the first agent audio:

```bash
uv run python -m tests.voice_call --script happy --calls 5
uv run python -m tests.voice_call --script price_cut_off
```

The other scripts are `short_answers`, `barge_in`, `acknowledgement`, `declines_pharmacy`, `first_name_only`, `keypad` and `cancel_check`. To call a shared link instead, add `--server https://<host> --code <code>`, and `--relay-only` to send the audio through the relay only, the way a strict office network would. Synthetic speech can make turn detection behave differently from a real voice, so treat these timings as a guide.

To refit the turn model from labelled real calls and compare policies on held-out calls:

```bash
uv run python -m tests.fit_turn_model
```

## Layout

| File | Job |
|---|---|
| `refill/core.py` | `handle_turn`: one turn for any channel |
| `refill/steps.py` | The next step, derived from stored fields, and which tools are allowed |
| `refill/tools.py` | The tool layer: gates, evidence checks, validation and one transaction per write |
| `refill/reader.py` | The one LLM call that reads a message |
| `refill/render.py` | Every line the agent says, in spoken and written form |
| `refill/guard.py` | The check on free text from the model |
| `refill/turn_model.py` | The Bayesian decision on how long to wait before replying |
| `refill/voice.py` | The Pipecat pipeline, and the bridge between it and the core |
| `refill/server.py`, `refill/web/index.html` | The server and the call page |
| `refill/web/guide.html`, `docs/TESTING_GUIDE.md` | The testing guide, also served at `/guide` |
| `Dockerfile` | The image for the shared link |
| `refill/schema.sql`, `refill/seed.sql` | Tables and demo data |
