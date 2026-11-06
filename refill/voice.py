import asyncio
import os
import re
import time

from dotenv import load_dotenv
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.inworld.tts import InworldTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy
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
from refill.steps import Step

load_dotenv()

DEMO_CALLER_PHONE = "(555) 867-5309"
STT_MODEL = "universal-3-6-pro"
TTS_MODEL = "inworld-tts-2"
TTS_VOICE = "Ashley"
DATE_OF_BIRTH_SILENCE_LIMIT_SECONDS = 3.0
SILENCE_LIMITS = {Step.IDENTIFY: DATE_OF_BIRTH_SILENCE_LIMIT_SECONDS, Step.MEDICATION: 1.0}
YES_NO_SILENCE_LIMIT_SECONDS = 0.6
IDLE_PROMPT_SECONDS = 10.0
HOLD_SECONDS = 30.0
EARLY_CUT_OFF_SECONDS = 1.5
ANSWER_WINDOW_SECONDS = 1.5
SPOKEN_WORDS_PER_SECOND = 2.5
ACKNOWLEDGEMENTS = {"mm hm", "mm hmm", "mhm", "mm", "hmm", "uh huh", "okay", "ok", "yeah", "right"}


def normalized_speech(text):
    return re.sub(r"[^a-z ]", "", text.lower().replace("-", " ")).strip()


class StepAwareSmartTurn(LocalSmartTurnAnalyzerV3):
    def set_silence_limit(self, seconds):
        self._stop_ms = seconds * 1000


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
    def __init__(self, session_id, smart_turn, start_strategy):
        super().__init__()
        self._session_id = session_id
        self._smart_turn = smart_turn
        self._start_strategy = start_strategy
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
        if isinstance(frame, InterruptionFrame):
            await self._handle_interruption()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._line_started_at = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._handle_line_finished()
        await self.push_frame(frame, direction)

    async def greet(self, line):
        await self._speak_line(line)

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
        await self._turns.put(text)

    async def _run_turns(self):
        while True:
            text = await self._turns.get()
            if self._merge_next_turn:
                text = f"{self._unanswered_text} {text}".strip()
                self._merge_next_turn = False
            generation = self._generation
            await self._show("patient", text)
            reply = await handle_turn(self._session_id, text)
            self._unanswered_text = "" if reply.advanced else text
            if generation != self._generation and not reply.ends_conversation:
                await asyncio.to_thread(mark_reply_unheard, self._session_id)
                continue
            await self._apply_step(reply)
            self._ending = reply.ends_conversation
            await self._speak_line(reply.text)

    async def _speak_line(self, text):
        self._start_strategy.expect_line(text)
        self._reply_unfinished = True
        await self._show("agent", text)
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    async def _apply_step(self, reply):
        self._smart_turn.set_silence_limit(SILENCE_LIMITS.get(reply.step, YES_NO_SILENCE_LIMIT_SECONDS))
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
    smart_turn = StepAwareSmartTurn(params=SmartTurnParams(stop_secs=DATE_OF_BIRTH_SILENCE_LIMIT_SECONDS))
    start_strategy = SpeechOverAgentStrategy()
    user_turns = LLMUserAggregator(
        LLMContext(),
        params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                start=[start_strategy],
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn)],
            ),
            user_idle_timeout=IDLE_PROMPT_SECONDS,
        ),
    )
    bridge = CallBridge(session_id, smart_turn, start_strategy)
    latency = UserBotLatencyObserver()
    task = PipelineTask(
        Pipeline([transport.input(), stt, user_turns, bridge, tts, transport.output()]),
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        await bridge.greet(greeting)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await asyncio.to_thread(drop_session, session_id)
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
