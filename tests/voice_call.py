"""Place a real WebRTC call to a running server, speaking a patient script with an ElevenLabs voice.

Start the server first with `uv run python -m refill.server`. The gap reported for each turn runs from the
end of the patient's speech to the first agent audio received here, on one clock. It leaves out microphone
and speaker hardware, and synthetic speech can make turn detection behave differently from a real voice.
"""
import argparse
import asyncio
import contextlib
import fractions
import wave
import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from dotenv import load_dotenv

load_dotenv()

SERVER = "http://127.0.0.1:7860"
CACHE_DIR = Path(__file__).parent / ".audio_cache"
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
VOICE_MODEL = "eleven_flash_v2_5"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
SILENCE_RMS = 60
AGENT_LOUD_RMS = 250
AGENT_QUIET_SECONDS = 1.2
INTERRUPTED_AUDIO_TAIL_SECONDS = 0.7
TURN_TIMEOUT_SECONDS = 25
SPEAKING_MARGIN_SECONDS = 2


@dataclass(frozen=True)
class Say:
    text: str
    over_agent_after: float | None = None
    key: str | None = None


def press(key):
    return Say(text="", key=key)


IDENTITY = Say("My name is Sarah Chen, and my date of birth is March fifteenth, nineteen eighty-five.")
SCRIPTS = {
    "happy": [
        IDENTITY,
        Say("I need the blood pressure one."),
        Say("Yes, that's the right pharmacy."),
        Say("Yes, that's still my insurance."),
        Say("Yes, please send it in."),
    ],
    "short_answers": [
        IDENTITY,
        Say("Lisinopril."),
        Say("Yes."),
        Say("Yes, that's right."),
        Say("Yes, please."),
    ],
    "barge_in": [
        IDENTITY,
        Say("Metformin, please.", over_agent_after=2.0),
        Say("Yes."),
        Say("Yes."),
        Say("Yes."),
    ],
    "acknowledgement": [
        IDENTITY,
        Say("Mm-hmm.", over_agent_after=1.0),
        Say("The cholesterol one."),
    ],
    "declines_pharmacy": [
        IDENTITY,
        Say("The first one."),
        Say("No."),
        Say("Walgreens."),
        Say("I don't know."),
        Say("Yes."),
    ],
    "first_name_only": [
        Say("My name is Sarah, and my date of birth is March fifteenth, nineteen eighty-five."),
        Say("Sarah Chen."),
        Say("The first one."),
    ],
    "keypad": [
        IDENTITY,
        Say("The first one."),
        press("*"),
        Say("Metformin."),
        Say("Yes."),
        press("#"),
        Say("The first one."),
        Say("Yes."),
        Say("Yes."),
        Say("Yes."),
    ],
    "price_cut_off": [
        IDENTITY,
        Say("Lisinopril."),
        Say("Yes."),
        Say("Yes."),
        Say("Yes, send it.", over_agent_after=1.5),
        Say("Yes."),
    ],
}


class PatientMicrophone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, idle_sound=None):
        super().__init__()
        self._queue = deque()
        self._idle_sound = idle_sound
        self._samples_sent = 0
        self._started_at = None
        self.finished_speaking = asyncio.Event()

    def speak(self, pcm):
        self.finished_speaking.clear()
        self._queue.extend(pcm[i : i + FRAME_SAMPLES] for i in range(0, len(pcm), FRAME_SAMPLES))

    async def wait_until_spoken(self):
        seconds = len(self._queue) * FRAME_SAMPLES / SAMPLE_RATE + SPEAKING_MARGIN_SECONDS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.finished_speaking.wait(), seconds)

    def _idle_chunk(self):
        if self._idle_sound is None:
            return np.zeros(FRAME_SAMPLES, dtype=np.int16)
        return self._idle_sound[np.arange(self._samples_sent, self._samples_sent + FRAME_SAMPLES) % len(self._idle_sound)]

    async def recv(self):
        if self._started_at is None:
            self._started_at = time.monotonic()
        target = self._started_at + self._samples_sent / SAMPLE_RATE
        await asyncio.sleep(max(0, target - time.monotonic()))
        chunk = self._idle_chunk()
        if self._queue:
            samples = self._queue.popleft()
            chunk[: len(samples)] = samples
            if not self._queue:
                self.finished_speaking.set()
        frame = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._samples_sent
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._samples_sent += FRAME_SAMPLES
        return frame


class AgentEar:
    def __init__(self):
        self.last_loud_at = None
        self.recording = []

    async def listen(self, track):
        try:
            while True:
                frame = await track.recv()
                self.recording.append(frame.to_ndarray().tobytes())
                self.recording_rate = frame.sample_rate
                self.recording_channels = len(frame.layout.channels)
                samples = frame.to_ndarray().astype(np.float32)
                if np.sqrt(np.mean(samples * samples)) > AGENT_LOUD_RMS:
                    self.last_loud_at = time.monotonic()
        except MediaStreamError:
            return

    async def wait_until_quiet(self):
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            quiet = self.last_loud_at and time.monotonic() - self.last_loud_at >= AGENT_QUIET_SECONDS
            if quiet:
                return
            await asyncio.sleep(0.02)

    async def wait_for_speech_after(self, moment):
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.last_loud_at and self.last_loud_at > moment:
                return self.last_loud_at
            await asyncio.sleep(0.005)
        return None


def trimmed(pcm):
    frames = pcm[: len(pcm) // FRAME_SAMPLES * FRAME_SAMPLES].reshape(-1, FRAME_SAMPLES).astype(np.float32)
    loud = np.flatnonzero(np.sqrt(np.mean(frames * frames, axis=1)) > SILENCE_RMS)
    return pcm[: (loud[-1] + 1) * FRAME_SAMPLES] if len(loud) else pcm


async def patient_audio(http, text, voice=VOICE_ID, model=VOICE_MODEL):
    CACHE_DIR.mkdir(exist_ok=True)
    cached = CACHE_DIR / f"{hashlib.sha256(f'{voice}{model}{text}'.encode()).hexdigest()[:16]}.pcm"
    if not cached.exists():
        async with http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            json={"text": text, "model_id": model},
        ) as response:
            response.raise_for_status()
            cached.write_bytes(await response.read())
    return trimmed(np.frombuffer(cached.read_bytes(), dtype=np.int16))


def save_recording(ear, path):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(ear.recording_channels)
        output.setsampwidth(2)
        output.setframerate(ear.recording_rate)
        output.writeframes(b"".join(ear.recording))


async def place_call(script, recording_path=None):
    transcript = []
    microphone = PatientMicrophone()
    ear = AgentEar()
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

    await peer.setLocalDescription(await peer.createOffer())
    gaps = []
    async with aiohttp.ClientSession() as http:
        clips = [await patient_audio(http, step.text) if step.text else None for step in script]
        offer = {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type}
        async with http.post(f"{SERVER}/api/offer", json=offer) as response:
            answer = await response.json()
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    speech_ended_at = 0
    await ear.wait_for_speech_after(speech_ended_at)
    for step, clip in zip(script, clips):
        if step.over_agent_after is None:
            await ear.wait_until_quiet()
        else:
            await asyncio.sleep(step.over_agent_after)
        if step.key:
            channel.send(json.dumps({"type": "keypad", "key": step.key}))
            transcript.append(("key", step.key))
        else:
            microphone.speak(clip)
            await microphone.wait_until_spoken()
        speech_ended_at = time.monotonic()
        tail = 0 if step.over_agent_after is None else INTERRUPTED_AUDIO_TAIL_SECONDS
        first_audio_at = await ear.wait_for_speech_after(speech_ended_at + tail)
        if step.over_agent_after is None and first_audio_at:
            gaps.append(round((first_audio_at - speech_ended_at) * 1000))
    await ear.wait_until_quiet()
    await peer.close()
    if recording_path:
        save_recording(ear, recording_path)
    return gaps, transcript


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", choices=SCRIPTS, default="happy")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--record", type=Path, help="save the agent's audio from the last call to this WAV file")
    arguments = parser.parse_args()
    all_gaps = []
    for number in range(1, arguments.calls + 1):
        recording_path = arguments.record if number == arguments.calls else None
        gaps, transcript = await place_call(SCRIPTS[arguments.script], recording_path)
        print(f"\ncall {number}")
        for role, text in transcript:
            print(f"  {role}: {text}")
        print("  gaps ms:", gaps)
        all_gaps += gaps
    all_gaps.sort()
    if all_gaps:
        print(f"\n{len(all_gaps)} turns: median {all_gaps[len(all_gaps) // 2]} ms, max {all_gaps[-1]} ms")


if __name__ == "__main__":
    asyncio.run(main())
