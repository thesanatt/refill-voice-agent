# Prescription Refill Voice Agent

A voice agent that takes prescription refill requests over a (simulated) phone call and continues over (simulated) SMS if the call drops. The problem statement is in [docs/TAKE_HOME.md](docs/TAKE_HOME.md), kickoff clarifications in [docs/KICKOFF_NOTES.md](docs/KICKOFF_NOTES.md), and design decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

Status: the text loop and the voice call both work. The SMS fallback and latency work are next.

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

## Run the text loop

```bash
uv run python -m refill.cli
uv run python -m refill.cli --channel sms
```

Type as the patient. `/state` prints the session, and `/drop` simulates a dropped call. The default channel uses spoken wording, and `--channel sms` uses written wording.

The database is `refill.db`, created and seeded on first run. Delete it to start clean.

## Tests

```bash
uv run python -m tests.exit_checks
uv run python -m tests.run_scenarios --set tuned --runs 3
uv run python -m tests.run_scenarios --set holdout --runs 3
```

These run typed conversations against a temporary database and assert on the stored rows. `tuned` holds the scenarios the code was fixed against, and `holdout` holds wording written afterwards.

With the server running, this places real WebRTC calls using an AI patient voice, and reports the gap from the end of the patient's speech to the first agent audio:

```bash
uv run python -m tests.voice_call --script happy --calls 5
uv run python -m tests.voice_call --script price_cut_off
```

The other scripts are `barge_in` and `acknowledgement`. Synthetic speech can make turn detection behave differently from a real voice, so treat these timings as a guide.

## Layout

| File | Job |
|---|---|
| `refill/core.py` | `handle_turn`: one turn for any channel |
| `refill/steps.py` | The next step, derived from stored fields, and which tools are allowed |
| `refill/tools.py` | The tool layer: gates, evidence checks, validation and one transaction per write |
| `refill/reader.py` | The one LLM call that reads a message |
| `refill/render.py` | Every line the agent says, in spoken and written form |
| `refill/guard.py` | The check on free text from the model |
| `refill/voice.py` | The Pipecat pipeline, and the bridge between it and the core |
| `refill/server.py`, `refill/web/index.html` | The local server and the call page |
| `refill/schema.sql`, `refill/seed.sql` | Tables and demo data |
