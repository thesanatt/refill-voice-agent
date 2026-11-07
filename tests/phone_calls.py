"""Place simulated phone calls and check what each one wrote to the database.

Each call uses a real WebRTC connection to its own server and temporary database. The caller is a voice and a
line condition from tests/phone_line.py, and it reacts to what the agent asks, as a person would, instead of
reading a fixed script. So a misheard date costs another try, not a broken test.

    uv run python -m tests.phone_calls --conditions landline,cafe --personas plain,older --calls 1

The result that matters is "wrong": a call that recorded a value the caller never gave. "moved_to_text" means
the agent could not hear the caller and handed over to the SMS path, as designed. Timings are less
reliable, because several calls share this machine. The "recorded" caller needs clips cut from real calls in
tests/.audio_cache/human, which git ignores.
"""
import argparse
import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription

from tests.phone_line import CONDITIONS, SAMPLE_RATE, PhoneLine, at_speech_level
from tests.voice_call import AgentEar, CACHE_DIR, PatientMicrophone, patient_audio

MULTILINGUAL_MODEL = "eleven_multilingual_v2"
MAX_TURNS = 18
PATIENCE_SECONDS = 12
CALL_TIMEOUT_SECONDS = 240
SERVERS = 4
FIRST_PORT = 7871
NEW_SESSIONS_PER_MINUTE = 4
MAX_REFUSED_RETRIES = 3
HUMAN_CLIPS = CACHE_DIR / "human"
COPAY_CENTS = {1: 500, 2: 1000, 3: 1500}
REPROMPTS = ("didn't catch", "couldn't match", "Let me finish", "One more time")
BABBLE_LINES = [
    ("CwhRBWXzGAHq8TQ4Fs17", "So I told him we could meet on Tuesday, but he said the fourteenth works better for the whole team, and I just laughed."),
    ("FGY2WhTYpPnrIDTdsKH5", "Can I get a large coffee with oat milk and two of the blueberry muffins, and could you warm those up for me please?"),
    ("JBFqnCBsd6RMkjVDRZzb", "The thing about that restaurant is the parking, you always end up three blocks away and it starts raining the moment you get out."),
    ("cgSgspJ2msm6clMCkdW9", "No, no, she called me back yesterday, and honestly I don't think the insurance is going to cover any of it this year."),
    ("nPczCjzI2devNBz1zQrb", "We should have left at nine, I told you, now the traffic on the highway is backed up all the way to the bridge."),
    ("Xb7hH8MSUJpSbSDYk0k2", "Yes, I'd like to return these shoes, I bought them on the twenty first of last month and they're simply the wrong size."),
]
TV_LINE = (
    "onwK4e9ZLuTAKqWW03F9",
    "Good evening, and welcome to the six o'clock news. Tonight, city officials approved a budget of four hundred million "
    "dollars for road repairs starting in March. Doctors are warning that flu cases rose by fifteen percent this week. "
    "Yes, you heard that right, and if you are over sixty five you should talk to your pharmacist about a booster. "
    "In sports, the home team won again last night, their third straight win since the first of the month.",
)


@dataclass(frozen=True)
class Persona:
    voice: str
    identity: str
    name: str
    date: str
    drugs: dict
    yes: str = "Yes."
    send: str = "Yes, please."
    model: str = MULTILINGUAL_MODEL


DRUGS_BY_NAME = {1: "Lisinopril, please.", 2: "Metformin, please.", 3: "Atorvastatin, please."}
PERSONAS = {
    "plain": Persona(
        "EXAVITQu4vr4xnSDxMaL",
        "My name is Sarah Chen, and my date of birth is March fifteenth, nineteen eighty-five.",
        "Sarah Chen.", "March fifteenth, nineteen eighty-five.", DRUGS_BY_NAME,
    ),
    "older_man": Persona(
        "pqHfZKP75CvOlQylNhV4",
        "Yes, hello. This is Sarah Chen. I was born March the fifteenth, nineteen hundred and eighty-five.",
        "It's Sarah Chen.", "March the fifteenth, nineteen eighty-five.",
        {1: "It's the Lisinopril for my blood pressure.", 2: "I need my Metformin, for the diabetes.", 3: "The Atorvastatin, the cholesterol pill."},
        yes="Yes, that's right.", send="Yes, go ahead and send that.",
    ),
    "indian": Persona(
        "0LjAz1phBntZLQj6ZwNP",
        "Sarah Chen, date of birth is fifteenth March, nineteen eighty five.",
        "Sarah Chen.", "Fifteenth March, nineteen eighty five.",
        {1: "Lisinopril tablet, please.", 2: "I want Metformin only.", 3: "Atorvastatin tablet."},
        yes="Yes, correct.", send="Yes, you can send.",
    ),
    "spanish": Persona(
        "n3yMmKmTfVCEM13Kk2lp",
        "My name is Sarah Chen, I was born the fifteen of March of nineteen eighty five.",
        "Sarah Chen.", "The fifteen of March, nineteen eighty five.",
        {1: "The Lisinopril, for the pressure.", 2: "The Metformin, please.", 3: "The one of the cholesterol, Atorvastatin."},
        yes="Yes, is correct.", send="Yes, please send it.",
    ),
    "mandarin": Persona(
        "2Zf5wdKeS7ZKPgAQm5Jy",
        "Sarah Chen. Birthday is March fifteen, nineteen eighty five.",
        "Sarah Chen.", "March fifteen, nineteen eighty five.",
        {1: "I need Lisinopril.", 2: "I need Metformin.", 3: "I need Atorvastatin."},
        yes="Yes.", send="Okay, send.",
    ),
    "arabic": Persona(
        "Os2frcqCuUz8b9F93RuI",
        "Hello, my name Sarah Chen, my birthday is fifteen March nineteen eighty five.",
        "Sarah Chen.", "Fifteen March nineteen eighty five.",
        {1: "Lisinopril please.", 2: "Metformin please.", 3: "Atorvastatin please."},
        yes="Yes, yes.", send="Yes, send it please.",
    ),
    "british": Persona(
        "Xb7hH8MSUJpSbSDYk0k2",
        "Hello, it's Sarah Chen, date of birth the fifteenth of March nineteen eighty-five.",
        "Sarah Chen.", "The fifteenth of March, nineteen eighty-five.",
        {1: "It's the Lisinopril, please.", 2: "The Metformin, please.", 3: "The Atorvastatin, thanks."},
        yes="Yes, that's fine.", send="Yes please, send it through.",
    ),
    "southern": Persona(
        "EcLnnHrXwfwGqXRRPWks",
        "Hey there, this is Sarah Chen, and I was born on March fifteenth of eighty five.",
        "Sarah Chen.", "March fifteenth, eighty five.",
        {1: "I need my Lisinopril, hon.", 2: "I need my Metformin.", 3: "I'm needin' the Atorvastatin."},
        yes="Yes ma'am.", send="Sure, go on and send it.",
    ),
    "rushed": Persona(
        "MnUw1cSnpiLoLhpd3Hqp",
        "Hi Sarah Chen three fifteen eighty five.",
        "Sarah Chen.", "Three fifteen eighty five.",
        {1: "Lisinopril.", 2: "Metformin.", 3: "Atorvastatin."},
        yes="Yep.", send="Yep send it.",
    ),
    "recorded": Persona(
        "recorded", "identity", "identity", "identity",
        {1: "first_one", 2: "second_one", 3: "cholesterol"},
        yes="yes", send="okay", model="",
    ),
}


@dataclass(frozen=True)
class Goal:
    rx: int
    pharmacy: int = 1


@dataclass
class CallResult:
    persona: str
    condition: str
    rx: int
    outcome: str = "unfinished"
    problems: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    conversation: list = field(default_factory=list)
    reprompts: int = 0
    seconds: float = 0.0


def next_words(agent_line, persona, goal, last_words):
    """What this caller would say next, given the last thing the agent said."""
    line = agent_line
    if "Goodbye" in line or "pharmacist will call" in line:
        return None
    if "take your time" in line:
        return last_words
    if "Would you like a pharmacist" in line:
        return persona.send
    if "full name and date of birth" in line:
        return persona.identity
    if "last name" in line or "full name" in line:
        return persona.name
    if "date of birth" in line or "born" in line:
        return persona.date
    if "Which one do you need" in line:
        return persona.drugs[goal.rx]
    if "Should I send it to" in line or "still your insurance" in line:
        return persona.yes
    if "Should I send that in" in line:
        return persona.send
    return last_words or persona.identity


async def clip_for(http, persona, words):
    if persona.voice == "recorded":
        pcm = np.frombuffer((HUMAN_CLIPS / f"{words}.pcm").read_bytes(), dtype=np.int16)
    else:
        pcm = await patient_audio(http, words, persona.voice, persona.model)
    return pcm.astype(np.float64) / 32767


async def synthesized(http, voice, text):
    return (await patient_audio(http, text, voice, MULTILINGUAL_MODEL)).astype(np.float64) / 32767


async def background_speech(http):
    clips = [at_speech_level(await synthesized(http, voice, text)) for voice, text in BABBLE_LINES]
    length = max(len(clip) for clip in clips)
    babble = sum(np.resize(clip, length) for clip in clips)
    return babble, await synthesized(http, *TV_LINE)


def int16(signal):
    return (np.clip(signal, -1, 1) * 32767).astype(np.int16)


class SessionPacer:
    """AssemblyAI limits new sessions per minute, and a refused one breaks that call for a reason unrelated to it."""

    def __init__(self):
        self.opened = []
        self.lock = asyncio.Lock()

    async def wait_for_a_slot(self):
        async with self.lock:
            now = time.monotonic()
            self.opened = [moment for moment in self.opened if now - moment < 62]
            if len(self.opened) >= NEW_SESSIONS_PER_MINUTE:
                await asyncio.sleep(62 - (now - self.opened[0]))
            self.opened.append(time.monotonic())


class TestServer:
    def __init__(self, port, folder, environment=None):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.database = folder / f"server_{port}.db"
        paths = {"REFILL_DB_PATH": str(self.database), "REFILL_RECORDINGS_DIR": str(folder)}
        environment = {**os.environ, **paths, **(environment or {})}
        command = ["uv", "run", "--quiet", "uvicorn", "refill.server:app", "--port", str(port), "--log-level", "warning"]
        log = open(folder / f"server_{port}.log", "w")
        self.process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)

    async def ready(self):
        for _ in range(200):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"server on port {self.port} did not start")

    def stop(self):
        self.process.terminate()


def judged(result, server, goal):
    conn = sqlite3.connect(server.database)
    conn.row_factory = sqlite3.Row
    session = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
    request = conn.execute("SELECT * FROM refill_requests WHERE session_id = ?", (session["id"],)).fetchone()
    said = conn.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id", (session["id"],)).fetchall()
    conn.close()
    result.conversation = [(row["role"], row["text"]) for row in said]
    result.reprompts = sum(any(word in text for word in REPROMPTS) for role, text in result.conversation if role == "agent")
    if session["prescription_id"] not in (None, goal.rx):
        result.problems.append(f"drug {session['prescription_id']}, wanted {goal.rx}")
    if session["pharmacy_id"] not in (None, goal.pharmacy) or session["pharmacy_text"]:
        result.problems.append(f"pharmacy {session['pharmacy_id']} {session['pharmacy_text']}, wanted {goal.pharmacy}")
    if session["insurance_status"] not in (None, "current"):
        result.problems.append(f"insurance {session['insurance_status']}")
    if request and request["estimated_copay_cents"] != COPAY_CENTS[goal.rx]:
        result.problems.append(f"copay {request['estimated_copay_cents']}")
    if result.problems:
        result.outcome = "wrong"
    elif request:
        result.outcome = "correct"
    elif session["identity_failures"] >= 2:
        result.outcome = "locked_out"
    elif session["status"] == "handed_off":
        result.outcome = "handed_off"
    elif session["sms_sent_at"]:
        result.outcome = "moved_to_text"
    return result


async def talk(microphone, ear, transcript, persona, goal, line, http, result):
    last_words = None
    await ear.wait_for_speech_after(0)
    for _ in range(MAX_TURNS):
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ear.wait_until_quiet(), PATIENCE_SECONDS)
        agent_lines = [text for role, text in transcript if role == "agent"]
        words = next_words(agent_lines[-1] if agent_lines else "", persona, goal, last_words)
        if words is None:
            return
        last_words = words
        microphone.speak(int16(line.carry(await clip_for(http, persona, words))))
        await microphone.wait_until_spoken()
        speech_ended_at = time.monotonic()
        first_audio_at = await ear.wait_for_speech_after(speech_ended_at)
        if first_audio_at:
            result.gaps.append(round((first_audio_at - speech_ended_at) * 1000))


async def place_call(server, http, persona_name, condition, goal, speech, seed):
    persona = PERSONAS[persona_name]
    line = PhoneLine(condition, speech, seed)
    result = CallResult(persona_name, condition, goal.rx)
    transcript = []
    microphone = PatientMicrophone(int16(line.idle_sound()))
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

    started = time.monotonic()
    await peer.setLocalDescription(await peer.createOffer())
    offer = {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type}
    async with http.post(f"{server.url}/api/offer", json=offer) as response:
        answer = await response.json()
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(talk(microphone, ear, transcript, persona, goal, line, http, result), CALL_TIMEOUT_SECONDS)
    await peer.close()
    await asyncio.sleep(1.0)
    result.seconds = round(time.monotonic() - started, 1)
    return judged(result, server, goal)


def refused_by_speech_service(result):
    return any("trouble hearing" in text for _, text in result.conversation) and len(result.conversation) <= 4


async def worker(server, http, jobs, speech, output, pacer):
    await server.ready()
    retries = Counter()
    while jobs:
        persona_name, condition, goal, seed = jobs.pop()
        await pacer.wait_for_a_slot()
        try:
            result = await place_call(server, http, persona_name, condition, goal, speech, seed)
        except Exception as error:
            print(f"  error {persona_name}/{condition}: {error!r}", flush=True)
            continue
        if refused_by_speech_service(result) and retries[seed] < MAX_REFUSED_RETRIES:
            retries[seed] += 1
            jobs.append((persona_name, condition, goal, seed))
            print(f"  retry {persona_name}/{condition}: speech service refused the session", flush=True)
            await asyncio.sleep(30)
            continue
        output.append(result)
        print(
            f"  {len(output):3d} {result.outcome:10s} {persona_name:10s} {condition:16s} rx{goal.rx} "
            f"reprompts={result.reprompts} {result.seconds:.0f}s {result.problems or ''}",
            flush=True,
        )


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def summary(results):
    print(f"\n{len(results)} calls: {dict(Counter(result.outcome for result in results))}")
    for label, key in (("condition", "condition"), ("persona", "persona")):
        groups = defaultdict(list)
        for result in results:
            groups[getattr(result, key)].append(result)
        print(f"\nby {label}: correct/calls, wrong, re-asks per call, median gap ms")
        for name, group in sorted(groups.items()):
            gaps = [gap for result in group for gap in result.gaps]
            correct = sum(result.outcome == "correct" for result in group)
            wrong = sum(result.outcome == "wrong" for result in group)
            reprompts = sum(result.reprompts for result in group) / len(group)
            median = percentile(gaps, 0.5) if gaps else "-"
            print(f"  {name:16s} {correct}/{len(group)}  wrong {wrong}  re-asks {reprompts:.1f}  gap {median}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--personas", default=",".join(PERSONAS))
    parser.add_argument("--calls", type=int, default=1, help="calls for each persona and condition")
    parser.add_argument("--servers", type=int, default=SERVERS)
    parser.add_argument("--out", type=Path, help="write every call to this JSON file")
    arguments = parser.parse_args()

    conditions = arguments.conditions.split(",")
    personas = arguments.personas.split(",")
    jobs = [
        (persona, condition, Goal(rx=(i + j + repeat) % 3 + 1), 1000 * repeat + 31 * i + j)
        for i, persona in enumerate(personas)
        for j, condition in enumerate(conditions)
        for repeat in range(arguments.calls)
    ]
    jobs.reverse()
    folder = Path(tempfile.mkdtemp(prefix="phone_calls_"))
    print(f"{len(jobs)} calls, logs in {folder}", flush=True)
    results = []
    servers = [TestServer(FIRST_PORT + number, folder) for number in range(min(arguments.servers, len(jobs)))]
    try:
        async with aiohttp.ClientSession() as http:
            speech = await background_speech(http)
            for name in personas:
                persona = PERSONAS[name]
                for words in (persona.identity, persona.name, persona.date, persona.yes, persona.send, *persona.drugs.values()):
                    await clip_for(http, persona, words)
            pacer = SessionPacer()
            await asyncio.gather(*(worker(server, http, jobs, speech, results, pacer) for server in servers))
    finally:
        for server in servers:
            server.stop()
    summary(results)
    if arguments.out:
        arguments.out.write_text(json.dumps([vars(result) for result in results], indent=1))


if __name__ == "__main__":
    asyncio.run(main())
