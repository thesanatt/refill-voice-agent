# Prescription Refill Voice Agent

A voice agent that takes prescription refill requests over a (simulated) phone call and continues over (simulated) SMS if the call drops. The problem statement is in [docs/TAKE_HOME.md](docs/TAKE_HOME.md), kickoff clarifications in [docs/KICKOFF_NOTES.md](docs/KICKOFF_NOTES.md), and design decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

Status: the text agent loop works in the terminal. Voice and the SMS fallback are next.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches the right Python version itself. Then copy the environment file and add your keys. The text loop only needs `ANTHROPIC_API_KEY`.

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

## Run the text loop

```bash
uv run python -m refill.cli
uv run python -m refill.cli --channel sms
```

Type as the patient. The demo patient is Sarah Chen, born March 15, 1985. `/state` prints the session, and `/drop` simulates a dropped call. The default channel uses spoken wording, and `--channel sms` uses written wording.

The database is `refill.db`, created and seeded on first run. Delete it to start clean.

## Layout

| File | Job |
|---|---|
| `refill/core.py` | `handle_turn`: one turn for any channel |
| `refill/steps.py` | The next step, derived from stored fields, and which tools are allowed |
| `refill/tools.py` | The tool layer: gates, validation and one transaction per write |
| `refill/reader.py` | The one LLM call that reads a message |
| `refill/render.py` | Every line the agent says, in spoken and written form |
| `refill/guard.py` | The check on free text from the model |
| `refill/schema.sql`, `refill/seed.sql` | Tables and demo data |
