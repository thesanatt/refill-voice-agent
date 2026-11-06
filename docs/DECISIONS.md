# Design decisions

Each entry records the options considered, the choice and the reason. Entries are added before the code that depends on them.

Format:

## D<n>. <decision> (<date>)
- Options: ...
- Chosen: ...
- Why: ...
- Revisit if: ...

## Open questions

1. Voice architecture: a speech-to-speech model (for example, a realtime API) or a speech-to-text, LLM and text-to-speech pipeline (for example, Pipecat). Weigh latency, control over turn-taking, tool calling, transcripts and cost.
2. Providers and models for each part.
3. Flow control: how much of the step order lives in code (a state machine the tools enforce) and how much in the prompt.
4. Database engine and schema: patients, prescriptions, pharmacies, insurance, copays, sessions, refill requests.
5. Detecting an early hang-up: socket close, an explicit end button, silence timeout. When exactly is the SMS sent?
6. The SMS channel: does it confirm identity again, or trust the phone number on file?
7. Normalizing spoken dates and names before lookup.
8. Turn detection and interruptions (barge-in).
9. Interface: one page with call and SMS panes, or separate views.
