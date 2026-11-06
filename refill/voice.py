import asyncio
import os
import re
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    InputDTMFFrame,
    InterruptionFrame,
    LLMContextFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    STTUpdateSettingsFrame,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator, LLMUserAggregatorParams
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.inworld.tts import InworldTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from refill.core import (
    drop_session,
    handle_turn,
    mark_reply_unheard,
    open_session,
    pending_line,
    record_first_audio,
    refill_keyterms,
)
from refill.reader import warm_up
from refill.steps import Step
from refill.tools import TOOL_DEFINITIONS, definitions_for
from refill.turn_model import question_asked, required_silence, slot_state

load_dotenv()

DEMO_CALLER_PHONE = "(555) 867-5309"
RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"
STT_MODEL = "universal-3-6-pro"
TTS_MODEL = "inworld-tts-2"
TTS_VOICE = "Ashley"
KEYPAD_COMMANDS = {"*": "go back", "#": "main menu"}
WORKING_PHRASES = ("One moment.", "Let me check that.", "Just a second.")
WORKING_PHRASE_AFTER_SECONDS = 1.5
MAX_WORKING_PHRASES = 3
IDLE_PROMPT_SECONDS = 10.0
HOLD_SECONDS = 30.0
EARLY_CUT_OFF_SECONDS = 1.5
ANSWER_WINDOW_SECONDS = 1.5
SPOKEN_WORDS_PER_SECOND = 2.5
ACKNOWLEDGEMENTS = {"mm hm", "mm hmm", "mhm", "mm", "hmm", "uh huh", "okay", "ok", "yeah", "right"}


def normalized_speech(text):
    return re.sub(r"[^a-z ]", "", text.lower().replace("-", " ")).strip()


TOOL_LISTS = [
    definitions_for({"verify_identity", "request_pharmacist", "end_call", "respond"}),
    definitions_for({tool["name"] for tool in TOOL_DEFINITIONS} - {"verify_identity", "submit_request"}),
    definitions_for({tool["name"] for tool in TOOL_DEFINITIONS} - {"verify_identity"}),
]


def save_recording(session_id, audio, sample_rate, num_channels):
    RECORDINGS_DIR.mkdir(exist_ok=True)
    with wave.open(str(RECORDINGS_DIR / f"{session_id}.wav"), "wb") as recording:
        recording.setnchannels(num_channels)
        recording.setsampwidth(2)
        recording.setframerate(sample_rate)
        recording.writeframes(audio)


class SlotAwareStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    def __init__(self):
        super().__init__()
        self._step = Step.IDENTIFY
        self._question = "both"
        self._vad_stopped_at = None

    def expect(self, step, question):
        self._step = step
        self._question = question

    def _remaining_wait(self, text):
        needed = required_silence(slot_state(self._step, self._question, text), self._step)
        elapsed = self._stop_secs + time.monotonic() - self._vad_stopped_at
        return max(0.0, needed - elapsed)

    async def handle_user_turn_stopped(self):
        self._vad_stopped_at = None
        await super().handle_user_turn_stopped()

    async def _handle_vad_user_started_speaking(self, frame):
        self._vad_stopped_at = None
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(self, frame):
        self._vad_stopped_at = time.monotonic()
        self._stop_secs = frame.stop_secs
        self._user_speech_timeout = self._remaining_wait(self._text)
        await super()._handle_vad_user_stopped_speaking(frame)

    async def _handle_transcription(self, frame):
        vad_stopped = self._vad_stopped_at is not None
        wait = self._remaining_wait(self._text + frame.text) if vad_stopped else 0.0
        if wait > 0:
            self._user_speech_wait_done = False
        await super()._handle_transcription(frame)
        if vad_stopped and not self._user_speech_wait_done:
            self._user_speech_timeout = wait
            await self._restart_user_speech_timer()


class SpeechOverAgentStrategy(MinWordsUserTurnStartStrategy):
    def __init__(self):
        super().__init__(min_words=1)
        self._line_seconds = 0.0
        self._line_ends_at = 0.0

    def expect_line(self, text):
        self._line_seconds = len(text.split()) / SPOKEN_WORDS_PER_SECOND

    async def process_frame(self, frame):
        if isinstance(frame, VADUserStartedSpeakingFrame) and not self._bot_speaking:
            await self.report_user_turn_started()
        return await super().process_frame(frame)

    async def _handle_bot_started_speaking(self, frame):
        await super()._handle_bot_started_speaking(frame)
        self._line_ends_at = time.monotonic() + self._line_seconds

    async def _handle_transcription(self, frame):
        line_has_time_left = time.monotonic() < self._line_ends_at - ANSWER_WINDOW_SECONDS
        if self._bot_speaking and line_has_time_left and normalized_speech(frame.text) in ACKNOWLEDGEMENTS:
            await self.trigger_reset_aggregation()
        else:
            await self.report_user_turn_started()


class CallBridge(FrameProcessor):
    def __init__(self, session_id, stop_strategy, start_strategy):
        super().__init__()
        self._session_id = session_id
        self._stop_strategy = stop_strategy
        self._start_strategy = start_strategy
        self._working_phrases_used = 0
        self._turns = asyncio.Queue()
        self._worker = None
        self._task = None
        self._generation = 0
        self._reply_unfinished = False
        self._line_started_at = None
        self._unanswered_text = ""
        self._merge_next_turn = False
        self._ending = False
        self._idle_prompts = 0
        self._keyterms_sent = False

    def attach_task(self, task):
        self._task = task

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._worker = self.create_task(self._run_turns())
            return
        if isinstance(frame, (EndFrame, CancelFrame)) and self._worker:
            await self.cancel_task(self._worker)
            self._worker = None
        if isinstance(frame, LLMContextFrame):
            await self._queue_turn(frame.context)
            return
        if isinstance(frame, InputDTMFFrame):
            await self._handle_key(frame.button.value)
            return
        if isinstance(frame, InterruptionFrame):
            await self._handle_interruption()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._line_started_at = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._handle_line_finished()
        await self.push_frame(frame, direction)

    async def greet(self, line):
        await self._speak_line(line)

    async def _handle_key(self, key):
        command = KEYPAD_COMMANDS.get(key)
        if command is None:
            return
        await self.broadcast_interruption()
        await self._handle_interruption()
        self._merge_next_turn = False
        self._reply_unfinished = True
        self._line_started_at = None
        await self._turns.put((command, f"Pressed {key}: {command}"))

    async def handle_idle(self):
        self._idle_prompts += 1
        if self._idle_prompts == 1:
            await self._speak_line(await asyncio.to_thread(pending_line, self._session_id))
        elif self._idle_prompts == 2:
            await self._speak_line("Take your time.")

    async def _queue_turn(self, context):
        messages = context.get_messages()
        text = (messages[-1].get("content") or "").strip() if messages else ""
        if not text:
            return
        self._reply_unfinished = True
        self._line_started_at = None
        self._idle_prompts = 0
        await self._turns.put((text, None))

    async def _run_turns(self):
        while True:
            text, shown_as = await self._turns.get()
            if self._merge_next_turn:
                text = f"{self._unanswered_text} {text}".strip()
                self._merge_next_turn = False
            generation = self._generation
            await self._show("patient", shown_as or text)
            reply = await self._reply_with_working_phrase(text, generation)
            self._unanswered_text = "" if reply.advanced else text
            if generation != self._generation and not reply.ends_conversation:
                await asyncio.to_thread(mark_reply_unheard, self._session_id)
                continue
            await self._apply_step(reply)
            self._ending = reply.ends_conversation
            await self._speak_line(reply.text)

    async def _reply_with_working_phrase(self, text, generation):
        reply_task = self.create_task(handle_turn(self._session_id, text))
        finished, _ = await asyncio.wait({reply_task}, timeout=WORKING_PHRASE_AFTER_SECONDS)
        may_speak = generation == self._generation and self._working_phrases_used < MAX_WORKING_PHRASES
        if not finished and may_speak:
            phrase = WORKING_PHRASES[self._working_phrases_used % len(WORKING_PHRASES)]
            self._working_phrases_used += 1
            await self._speak_line(phrase)
        return await reply_task

    async def _speak_line(self, text):
        self._start_strategy.expect_line(text)
        self._reply_unfinished = True
        await self._show("agent", text)
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    async def _apply_step(self, reply):
        self._stop_strategy.expect(reply.step, question_asked(reply.text))
        timeout = HOLD_SECONDS if reply.holding else IDLE_PROMPT_SECONDS
        await self.push_frame(UserIdleTimeoutUpdateFrame(timeout=timeout), FrameDirection.UPSTREAM)
        verified = reply.step not in (Step.IDENTIFY, Step.DONE)
        if verified and not self._keyterms_sent:
            self._keyterms_sent = True
            keyterms = await asyncio.to_thread(refill_keyterms, self._session_id)
            delta = AssemblyAISTTService.Settings(keyterms_prompt=keyterms)
            await self.push_frame(STTUpdateSettingsFrame(delta=delta), FrameDirection.UPSTREAM)

    async def _handle_interruption(self):
        self._generation += 1
        if not self._reply_unfinished:
            return
        self._reply_unfinished = False
        started_at = self._line_started_at
        started_late = started_at is not None and time.monotonic() - started_at >= EARLY_CUT_OFF_SECONDS
        self._merge_next_turn = not started_late
        await asyncio.to_thread(mark_reply_unheard, self._session_id)

    async def _handle_line_finished(self):
        self._reply_unfinished = False
        if self._ending and self._task:
            await self._task.queue_frame(EndFrame())

    async def _show(self, role, text):
        message = {"role": role, "text": text}
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))


async def run_call(connection):
    session_id, greeting = await asyncio.to_thread(open_session, DEMO_CALLER_PHONE, "voice")
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt = AssemblyAISTTService(
        api_key=os.environ["ASSEMBLYAI_API_KEY"],
        settings=AssemblyAISTTService.Settings(model=STT_MODEL),
    )
    tts = InworldTTSService(
        api_key=os.environ["INWORLD_API_KEY"],
        settings=InworldTTSService.Settings(model=TTS_MODEL, voice=TTS_VOICE),
    )
    stop_strategy = SlotAwareStopStrategy()
    start_strategy = SpeechOverAgentStrategy()
    user_turns = LLMUserAggregator(
        LLMContext(),
        params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(start=[start_strategy], stop=[stop_strategy]),
            user_idle_timeout=IDLE_PROMPT_SECONDS,
        ),
    )
    bridge = CallBridge(session_id, stop_strategy, start_strategy)
    recorder = AudioBufferProcessor(num_channels=2)
    latency = UserBotLatencyObserver()
    task = PipelineTask(
        Pipeline([transport.input(), stt, user_turns, bridge, tts, transport.output(), recorder]),
        params=PipelineParams(enable_metrics=True),
        observers=[latency],
    )
    bridge.attach_task(task)

    @latency.event_handler("on_latency_measured")
    async def on_latency_measured(_observer, latency_seconds):
        await asyncio.to_thread(record_first_audio, session_id, round(latency_seconds * 1000))

    @user_turns.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(_aggregator):
        await bridge.handle_idle()

    @recorder.event_handler("on_audio_data")
    async def on_audio_data(_recorder, audio, sample_rate, num_channels):
        await asyncio.to_thread(save_recording, session_id, audio, sample_rate, num_channels)

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, _sender):
        if isinstance(message, dict) and message.get("type") == "keypad" and message.get("key") in KEYPAD_COMMANDS:
            await task.queue_frame(InputDTMFFrame(button=KeypadEntry(message["key"])))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        await recorder.start_recording()
        await bridge.greet(greeting)
        asyncio.ensure_future(warm_up(TOOL_LISTS))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await recorder.stop_recording()
        await asyncio.to_thread(drop_session, session_id)
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
