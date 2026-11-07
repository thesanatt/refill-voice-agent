"""Real calls that test timing and failure, which typed tests cannot reach.

Each move is a script spoken over a real WebRTC call to its own server, followed by checks on the database:
talking over the agent at chosen moments, slow speech, noise, the agent's own voice leaking back into the
microphone, a call dropped at every step and finished by text, and each outside service failing.

    uv run python -m tests.phone_moves
    uv run python -m tests.phone_moves drop_at_pharmacy stt_outage
"""
import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription

from tests.phone_calls import NEW_SESSIONS_PER_MINUTE, FIRST_PORT, TestServer, int16
from tests.voice_call import INTERRUPTED_AUDIO_TAIL_SECONDS, AgentEar, PatientMicrophone, patient_audio

VOICE = "EXAVITQu4vr4xnSDxMaL"
RATE = 16000
ECHO_DELAY_SECONDS = 0.12
ECHO_GAIN = 0.12
BABBLE_GAIN = 0.35
BABBLE = "Two lattes and a muffin."
IDENTITY = "My name is Sarah Chen, and my date of birth is March fifteenth, nineteen eighty-five."


@dataclass(frozen=True)
class Say:
    text: str = ""
    pieces: tuple = ()
    gap: float = 0.0
    over_agent_after: float | None = None
    noise: bool = False
    silence: float = 0.0
    key: str | None = None
    hang_up: bool = False
    babble_after: float | None = None


@dataclass(frozen=True)
class Move:
    script: tuple
    want: dict = field(default_factory=dict)
    hear: tuple = ()
    avoid: tuple = ()
    texts: tuple = ()
    echo: bool = False
    environment: dict = field(default_factory=dict)


YES = Say("Yes.")
TO_PHARMACY = (Say(IDENTITY), Say("Lisinopril."))
TO_INSURANCE = (*TO_PHARMACY, YES)
TO_CONFIRM = (*TO_INSURANCE, YES)
FINISH_BY_TEXT = ("yes", "yes", "yes", "yes")
COMPLETE = {"status": "completed", "prescription_id": 1, "pharmacy_id": 1, "insurance_status": "current"}

MOVES = {
    "fragment_lands_on_unheard_question": Move(
        (*TO_PHARMACY, Say("Um, okay."), Say("Go ahead.", over_agent_after=0.5), YES, YES),
        want=COMPLETE, hear=("Let me finish",),
    ),
    "yes_over_the_end_of_the_question": Move(
        (*TO_INSURANCE, Say("Yes.", over_agent_after=4.8), YES, YES),
        want=COMPLETE, avoid=("Let me finish",),
    ),
    "yes_over_the_start_of_the_price": Move(
        (*TO_CONFIRM, Say("Yes, send it.", over_agent_after=1.2), YES),
        want=COMPLETE, hear=("Let me finish",),
    ),
    "yes_over_the_end_of_the_price": Move(
        (*TO_CONFIRM, Say("Yes, send it.", over_agent_after=6.8),),
        want=COMPLETE, avoid=("Let me finish",),
    ),
    "answer_over_the_list": Move(
        (Say(IDENTITY), Say("Metformin, please.", over_agent_after=2.0), YES, YES, YES, YES),
        want={**COMPLETE, "prescription_id": 2},
    ),
    "bare_drug_over_the_list": Move(
        (Say(IDENTITY), Say("Metformin.", over_agent_after=2.5)),
        want={"prescription_id": 2},
    ),
    "quoting_the_list_over_the_list": Move(
        (Say(IDENTITY), Say("Metformin for diabetes.", over_agent_after=3.5)),
        want={"prescription_id": 2},
    ),
    "mm_hmm_does_not_stop_the_agent": Move(
        (Say(IDENTITY), Say("Mm-hmm.", over_agent_after=1.5), Say("The cholesterol one.")),
        want={"prescription_id": 3}, avoid=("didn't catch",),
    ),
    "noise_burst_does_not_stop_the_agent": Move(
        (Say(IDENTITY), Say(noise=True, over_agent_after=2.0), Say("Lisinopril.")),
        want={"prescription_id": 1}, avoid=("didn't catch",),
    ),
    "room_noise_during_the_read_keeps_the_answer": Move(
        (*TO_PHARMACY, Say("Yes, that's fine.", babble_after=0.75), Say(hang_up=True)),
        want={"pharmacy_id": 1}, hear=("Aetna",), avoid=("Let me finish",),
    ),
    "slow_date_with_long_pauses": Move(
        (Say(pieces=("My name is Sarah Chen.", "March fifteenth,", "nineteen eighty-five."), gap=1.3), Say("Lisinopril.")),
        want={"prescription_id": 1, "identity_failures": 0},
    ),
    "very_slow_date": Move(
        (Say(pieces=("Sarah Chen.", "March.", "The fifteenth.", "Nineteen.", "Eighty-five."), gap=1.8), Say("Lisinopril."), Say("Lisinopril.")),
        want={"prescription_id": 1, "identity_failures": 0},
    ),
    "side_talk_then_answer": Move(
        (Say(IDENTITY), Say("Hold on honey, I'm on the phone."), Say(silence=5), Say("Sorry about that. Metformin.")),
        want={"prescription_id": 2},
    ),
    "long_silence_gets_prompted": Move(
        (Say(IDENTITY), Say(silence=32), Say("Lisinopril.")),
        want={"prescription_id": 1}, hear=("Take your time",),
    ),
    "keys_go_back_and_restart": Move(
        (*TO_PHARMACY, Say(key="*"), Say("Metformin."), YES, Say(key="#"), Say("The first one."), YES, YES, YES, YES),
        want=COMPLETE,
    ),
    "cancel_is_checked_on_the_keypad": Move(
        (*TO_CONFIRM, Say("No, don't send it."), Say(key="2"), Say("No, don't send it."), Say(key="1")),
        want={"status": "declined", "prescription_id": 1}, hear=("Press 1 to cancel it", "keep going", "won't send"),
    ),
    "agent_voice_leaks_into_the_microphone": Move(
        (*TO_CONFIRM, YES, YES), want=COMPLETE, echo=True,
    ),
    "echo_alone_picks_nothing": Move(
        (Say(IDENTITY), Say(silence=14)), want={"prescription_id": None}, echo=True,
    ),
    "echo_call_answers_in_the_agents_words": Move(
        (*TO_CONFIRM, Say("Send it."), Say("Send it.")), want=COMPLETE, echo=True,
    ),
    "drop_at_identity": Move(
        (Say(hang_up=True),),
        want=COMPLETE, texts=("Sarah Chen 3/15/85", "Lisinopril", *FINISH_BY_TEXT[:3]),
    ),
    "drop_at_medication": Move(
        (Say(IDENTITY), Say(hang_up=True)),
        want=COMPLETE, texts=("Lisinopril", *FINISH_BY_TEXT[:3]),
    ),
    "drop_at_pharmacy": Move(
        (*TO_PHARMACY, Say(hang_up=True)),
        want=COMPLETE, texts=FINISH_BY_TEXT[:3],
    ),
    "drop_at_insurance": Move(
        (*TO_INSURANCE, Say(hang_up=True)),
        want=COMPLETE, texts=FINISH_BY_TEXT[:2],
    ),
    "drop_at_confirm": Move(
        (*TO_CONFIRM, Say(hang_up=True)),
        want=COMPLETE, texts=FINISH_BY_TEXT[:1],
    ),
    "speech_recognition_is_down": Move(
        (Say(IDENTITY), Say(silence=6)),
        want={"status": "dropped"}, hear=("trouble hearing",),
        environment={"ASSEMBLYAI_API_KEY": "not-a-real-key"},
    ),
    "speech_output_is_down": Move(
        (Say(silence=4),),
        want={"status": "dropped"}, environment={"INWORLD_API_KEY": "not-a-real-key"},
    ),
    "the_model_is_down": Move(
        (Say(IDENTITY), Say(IDENTITY)),
        want={"status": "dropped", "verified_at": None},
        environment={"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"},
    ),
}


async def audio_for(http, step):
    if step.noise:
        burst = np.random.default_rng(1).standard_normal(int(0.3 * RATE)) * 0.25
        return int16(burst * np.exp(-8 * np.arange(len(burst)) / len(burst)))
    if step.pieces:
        pause = np.zeros(int(step.gap * RATE), dtype=np.int16)
        parts = [await patient_audio(http, piece, VOICE) for piece in step.pieces]
        joined = [part for clip in parts for part in (clip, pause)][:-1]
        return np.concatenate(joined)
    if step.babble_after is not None:
        pause = np.zeros(int(step.babble_after * RATE), dtype=np.int16)
        backwards = (await patient_audio(http, BABBLE, VOICE))[::-1] * BABBLE_GAIN
        return np.concatenate((await patient_audio(http, step.text, VOICE), pause, backwards.astype(np.int16)))
    return await patient_audio(http, step.text, VOICE) if step.text else None


class LeakyMicrophone(PatientMicrophone):
    """Mixes the agent's audio back in, as a speakerphone with no echo cancelling does."""

    def __init__(self, ear):
        super().__init__()
        self._ear = ear
        self._frames_read = 0
        self._delay_frames = int(ECHO_DELAY_SECONDS * 50)

    def _idle_chunk(self):
        frames = self._ear.recording
        available = len(frames) - self._delay_frames
        if available <= self._frames_read:
            return super()._idle_chunk()
        raw = np.frombuffer(frames[self._frames_read], dtype=np.int16)
        self._frames_read += 1
        channels = getattr(self._ear, "recording_channels", 1)
        mono = raw.reshape(-1, channels)[:, 0].astype(np.float64)
        step = len(mono) / 320
        return (mono[(np.arange(320) * step).astype(int)] * ECHO_GAIN).astype(np.int16)


async def speak_script(move, server, http):
    transcript = []
    ear = AgentEar()
    microphone = LeakyMicrophone(ear) if move.echo else PatientMicrophone()
    peer = RTCPeerConnection()
    peer.addTrack(microphone)
    channel = peer.createDataChannel("chat", ordered=True)

    @channel.on("message")
    def on_message(raw):
        message = json.loads(raw)
        if message.get("role") in ("agent", "patient"):
            transcript.append((message["role"], message["text"]))

    @peer.on("track")
    def on_track(track):
        asyncio.ensure_future(ear.listen(track))

    clips = [await audio_for(http, step) for step in move.script]
    await peer.setLocalDescription(await peer.createOffer())
    offer = {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type}
    async with http.post(f"{server.url}/api/offer", json=offer) as response:
        answer = await response.json()
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    await ear.wait_for_speech_after(0)
    for step, clip in zip(move.script, clips):
        if step.over_agent_after is None:
            await ear.wait_until_quiet()
        else:
            await asyncio.sleep(step.over_agent_after)
        if step.hang_up:
            break
        if step.silence:
            await asyncio.sleep(step.silence)
            continue
        if step.key and channel.readyState != "open":
            break
        if step.key:
            channel.send(json.dumps({"type": "keypad", "key": step.key}))
        else:
            microphone.speak(clip)
            await microphone.wait_until_spoken()
        tail = 0 if step.over_agent_after is None else INTERRUPTED_AUDIO_TAIL_SECONDS
        await ear.wait_for_speech_after(time.monotonic() + tail)
    else:
        await ear.wait_until_quiet()
    await peer.close()
    await asyncio.sleep(1.5)
    for text in move.texts:
        async with http.post(f"{server.url}/api/sms", json={"text": text}) as response:
            await response.read()


def problems_with(move, server):
    conn = sqlite3.connect(server.database)
    conn.row_factory = sqlite3.Row
    session = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
    messages = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session["id"],)).fetchall()
    conn.close()
    said = " ".join(row["text"] for row in messages if row["role"] == "agent")
    problems = [f"{key}={session[key]!r}, wanted {value!r}" for key, value in move.want.items() if session[key] != value]
    problems += [f"never said {phrase!r}" for phrase in move.hear if phrase not in said]
    problems += [f"said {phrase!r}" for phrase in move.avoid if phrase in said]
    if move.texts and session["sms_sent_at"] is None:
        problems.append("no text was sent")
    return problems, messages


async def run_move(name, move, folder, port):
    server = TestServer(port, folder, move.environment)
    await server.ready()
    try:
        async with aiohttp.ClientSession() as http:
            await speak_script(move, server, http)
        return problems_with(move, server)
    finally:
        server.stop()


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--show", action="store_true", help="print every conversation")
    arguments = parser.parse_args()
    names = arguments.names or list(MOVES)
    folder = Path(tempfile.mkdtemp(prefix="phone_moves_"))
    failed = 0
    for number, name in enumerate(names):
        started = time.monotonic()
        problems, messages = await run_move(name, MOVES[name], folder, FIRST_PORT + 20 + number)
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'pass'} {name} ({time.monotonic() - started:.0f}s) {problems or ''}", flush=True)
        if problems or arguments.show:
            for row in messages:
                mark = " [cut]" if row["interrupted"] else ""
                print(f"    {row['channel'][:3]} {row['role'][:3]}{mark}: {row['text'][:120]}", flush=True)
        await asyncio.sleep(max(0, 60 / NEW_SESSIONS_PER_MINUTE - (time.monotonic() - started)))
    print(f"\n{len(names) - failed}/{len(names)} moves passed, logs in {folder}")


if __name__ == "__main__":
    asyncio.run(main())
