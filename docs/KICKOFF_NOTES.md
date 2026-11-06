# Kickoff call notes (Nov 3, 2026)

Clarifications to `docs/TAKE_HOME.md` from the kickoff call.

- **Identity:** match on two identifiers, full name and date of birth. If the name matches but the date of birth doesn't, asking the patient to repeat the date of birth is a reasonable recovery. Beyond that, the happy path is enough.
- **Simulation:** no telephony or SMS vendor is needed. A browser button that starts and ends a simulated call, and a chat interface for the simulated SMS, are enough.
- **Latency:** this replaces the latency line under Expected Limitations. Responses should feel conversational; multi-second pauses make for a poor demo.
- **Approaches:** speech-to-speech models (audio in, audio out) and the three-model pipeline (speech-to-text, LLM, text-to-speech, often assembled with a framework such as Pipecat) are both acceptable. API keys for model providers are available on request.
- **Data:** the demo data is enough. Keep the schema simple; only copay amounts are needed from insurance.
- **Review:** the demo covers the happy path including the SMS fallback, how the agent sounds and stays on task, and a walkthrough of the design: the agent loop, its tools, how conversation state is managed, and how the SMS fallback resumes from that state.
- **Priority:** the core flow working well comes before any extension.
- **Process record:** the coding-agent session transcripts are included in the repository.
