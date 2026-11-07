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
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFrame,
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
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from refill.core import (
    cancel_check_pending,
    close_call,
    handle_turn,
    mark_reply_unheard,
    open_session,
    record_first_audio,
    record_line,
    refill_keyterms,
    say_hearing_lost,
)
from refill.reader import warm_up
from refill.steps import Step
from refill.tools import TOOL_DEFINITIONS, definitions_for
from refill.turn_model import OwnVoice, question_asked, required_silence, slot_state

load_dotenv()

DEMO_CALLER_PHONE = "(555) 867-5309"
RECORDINGS_DIR = Path(os.environ.get("REFILL_RECORDINGS_DIR", Path(__file__).parent.parent / "recordings"))
STT_MODEL = "universal-3-6-pro"
TTS_MODEL = "inworld-tts-2"
TTS_VOICE = "Ashley"
KEYPAD_COMMANDS = {"*": "go back", "#": "main menu", "1": "cancel it", "2": "keep going"}
CANCEL_CHECK_KEYS = {"1", "2"}
LOST_CONNECTION_SECONDS = 4.0
WORKING_PHRASES = ("One moment.", "Let me check that.", "Just a second.")
WORKING_PHRASE_AFTER_SECONDS = 1.5
MAX_WORKING_PHRASES = 3
IDLE_PROMPT_SECONDS = 10.0
HOLD_SECONDS = 30.0
EARLY_CUT_OFF_SECONDS = 1.5
ANSWER_WINDOW_SECONDS = 1.5
ROOM_NOISE_WAIT_SECONDS = 1.5
OWN_VOICE_TAIL_SECONDS = 1.5
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


class RefillSpeechInput(AssemblyAISTTService):
    async def use_keyterms(self, keyterms):
        self._settings.keyterms_prompt = keyterms
        await self._send_update_configuration(keyterms_prompt=keyterms)


class SlotAwareStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    def __init__(self):
        super().__init__()
        self._step = Step.IDENTIFY
        self._question = "both"
        self._vad_stopped_at = None
        self._words_settled_timer = None

    def expect(self, step, question):
        self._step = step
        self._question = question

    def _remaining_wait(self, text):
        needed = required_silence(slot_state(self._step, self._question, text), self._step)
        elapsed = self._stop_secs + time.monotonic() - self._vad_stopped_at
        return max(0.0, needed - elapsed)

    async def _stop_words_settled_timer(self):
        if self._words_settled_timer:
            await self.task_manager.cancel_task(self._words_settled_timer)
            self._words_settled_timer = None

    async def _restart_words_settled_timer(self):
        await self._stop_words_settled_timer()
        needed = required_silence(slot_state(self._step, self._question, self._text), self._step)
        wait = max(ROOM_NOISE_WAIT_SECONDS, needed)
        self._words_settled_timer = self.task_manager.create_task(self._end_turn_after(wait), f"{self}::words_settled")

    async def _end_turn_after(self, wait):
        await asyncio.sleep(wait)
        self._words_settled_timer = None
        await self.report_user_turn_stopped()

    async def cleanup(self):
        await self._stop_words_settled_timer()
        await super().cleanup()

    async def process_frame(self, frame):
        await super().process_frame(frame)
        if isinstance(frame, InterimTranscriptionFrame) and self._words_settled_timer:
            await self._restart_words_settled_timer()

    async def handle_user_turn_stopped(self):
        self._vad_stopped_at = None
        await self._stop_words_settled_timer()
        await super().handle_user_turn_stopped()

    async def _handle_vad_user_started_speaking(self, frame):
        self._vad_stopped_at = None
        await super()._handle_vad_user_started_speaking(frame)
        if self._text:
            await self._restart_words_settled_timer()

    async def _handle_vad_user_stopped_speaking(self, frame):
        await self._stop_words_settled_timer()
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
        if self._vad_user_speaking:
            await self._restart_words_settled_timer()


class SpeechOverAgentStrategy(MinWordsUserTurnStartStrategy):
    def __init__(self):
        super().__init__(min_words=1)
        self._question_seconds = 0.0
        self._question_ends_at = 0.0
        self._line_stopped_at = 0.0
        self._own_voice = OwnVoice()
        self._turn_began_over_line = False
        self._turn_began_after_line = False

    def expect_line(self, text):
        question = text[: text.rfind("?") + 1] or text
        self._question_seconds = len(question.split()) / SPOKEN_WORDS_PER_SECOND
        self._own_voice.expect(text)

    def question_was_heard(self):
        return time.monotonic() >= self._question_ends_at - ANSWER_WINDOW_SECONDS

    def without_own_voice(self, text):
        voice = self._own_voice
        over_line = self._turn_began_over_line
        if over_line and (voice.repeats_start(text) or (voice.sounds_like_line(text) and voice.proven)):
            return ""
        if voice.proven and (self._turn_began_over_line or self._turn_began_after_line):
            return voice.without_line(text)
        return text

    async def process_frame(self, frame):
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._turn_began_over_line = self._bot_speaking
            self._turn_began_after_line = time.monotonic() - self._line_stopped_at < OWN_VOICE_TAIL_SECONDS
            if not self._bot_speaking:
                await self.report_user_turn_started()
        return await super().process_frame(frame)

    async def _handle_bot_started_speaking(self, frame):
        await super()._handle_bot_started_speaking(frame)
        self._question_ends_at = time.monotonic() + self._question_seconds

    async def _handle_bot_stopped_speaking(self, frame):
        await super()._handle_bot_stopped_speaking(frame)
        self._line_stopped_at = time.monotonic()

    async def _handle_transcription(self, frame):
        if not self._bot_speaking:
            await self.report_user_turn_started()
            return
        finished = isinstance(frame, TranscriptionFrame)
        voice = self._own_voice
        own_voice = voice.sounds_like_line(frame.text)
        acknowledged = not self.question_was_heard() and normalized_speech(frame.text) in ACKNOWLEDGEMENTS
        if acknowledged or (finished and (voice.repeats_start(frame.text) or (own_voice and voice.proven))):
            await self.trigger_reset_aggregation()
        elif finished or not own_voice:
            await self.report_user_turn_started()


class CallBridge(FrameProcessor):
    def __init__(self, session_id, speech_input, stop_strategy, start_strategy):
        super().__init__()
        self._session_id = session_id
        self._speech_input = speech_input
        self._stop_strategy = stop_strategy
        self._start_strategy = start_strategy
        self._working_phrases_used = 0
        self._turns = asyncio.Queue()
        self._worker = None
        self._task = None
        self._generation = 0
        self._reply_unfinished = False
        self._reading_turn = False
        self._line_was_cut = False
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
        if key in CANCEL_CHECK_KEYS and not await asyncio.to_thread(cancel_check_pending, self._session_id):
            return
        await self.broadcast_interruption()
        await self._handle_interruption()
        self._merge_next_turn = False
        self._reply_unfinished = True
        self._reading_turn = True
        self._line_started_at = None
        await self._turns.put((command, f"Pressed {key}: {command}"))

    async def handle_lost_hearing(self):
        self._ending = True
        await self._speak_line(await asyncio.to_thread(say_hearing_lost, self._session_id))

    async def handle_idle(self):
        self._idle_prompts += 1
        if self._idle_prompts == 1:
            await self._speak_line(await asyncio.to_thread(record_line, self._session_id))
        elif self._idle_prompts == 2:
            await self._speak_line(await asyncio.to_thread(record_line, self._session_id, "Take your time."))
        elif self._idle_prompts == 3:
            await self.handle_lost_hearing()

    async def _queue_turn(self, context):
        messages = context.get_messages()
        last = messages[-1] if messages else {}
        if self._ending:
            return
        if last.get("role") != "user":
            await self._handle_empty_turn()
            return
        text = self._start_strategy.without_own_voice((last.get("content") or "").strip())
        if not text:
            await self._handle_empty_turn()
            return
        self._reply_unfinished = True
        self._reading_turn = True
        self._line_started_at = None
        self._idle_prompts = 0
        await self._turns.put((text, None))

    async def _handle_empty_turn(self):
        if self._line_was_cut:
            await self._speak_line(await asyncio.to_thread(record_line, self._session_id))

    async def _run_turns(self):
        while True:
            text, shown_as = await self._turns.get()
            if self._ending:
                continue
            latest_words = text
            if self._merge_next_turn:
                text = f"{self._unanswered_text} {text}".strip()
                self._merge_next_turn = False
            generation = self._generation
            await self._show("patient", shown_as or text)
            reply = await self._reply_with_working_phrase(text, generation)
            self._reading_turn = not self._turns.empty()
            self._unanswered_text = "" if reply.advanced else latest_words
            if generation != self._generation and not reply.ends_conversation:
                await asyncio.to_thread(mark_reply_unheard, self._session_id)
                continue
            await self._apply_step(reply)
            self._ending = reply.ends_conversation
            if reply.text:
                await self._speak_line(reply.text)
            else:
                self._reply_unfinished = False

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
        self._line_was_cut = False
        await self._show("agent", text)
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    async def _apply_step(self, reply):
        self._stop_strategy.expect(reply.step, question_asked(reply.text))
        timeout = HOLD_SECONDS if reply.holding else IDLE_PROMPT_SECONDS
        await self.push_frame(UserIdleTimeoutUpdateFrame(timeout=timeout), FrameDirection.UPSTREAM)
        verified = reply.step not in (Step.IDENTIFY, Step.DONE)
        if verified and not self._keyterms_sent:
            self._keyterms_sent = True
            await self._speech_input.use_keyterms(await asyncio.to_thread(refill_keyterms, self._session_id))

    async def _handle_interruption(self):
        self._generation += 1
        if not self._reply_unfinished:
            return
        self._reply_unfinished = False
        self._line_was_cut = True
        started_at = self._line_started_at
        started_late = started_at is not None and time.monotonic() - started_at >= EARLY_CUT_OFF_SECONDS
        self._merge_next_turn = not started_late
        if self._reading_turn:
            return
        question_heard = started_at is not None and self._start_strategy.question_was_heard()
        await asyncio.to_thread(mark_reply_unheard, self._session_id, question_heard)

    async def _handle_line_finished(self):
        self._reply_unfinished = False
        if self._ending and self._task:
            await self._task.queue_frame(EndFrame())

    async def _show(self, role, text):
        message = {"role": role, "text": text}
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))


async def run_call(connection, caller_phone=DEMO_CALLER_PHONE):
    session_id, greeting = await asyncio.to_thread(open_session, caller_phone, "voice")
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt = RefillSpeechInput(
        api_key=os.environ["ASSEMBLYAI_API_KEY"],
        settings=AssemblyAISTTService.Settings(model=STT_MODEL, language_codes=[Language.EN]),
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
    bridge = CallBridge(session_id, stt, stop_strategy, start_strategy)
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

    @stt.event_handler("on_usable_changed")
    async def on_speech_input_changed(_stt, usable):
        if not usable:
            await bridge.handle_lost_hearing()

    @tts.event_handler("on_usable_changed")
    async def on_speech_output_changed(_tts, usable):
        if not usable:
            await end_call()

    @user_turns.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(_aggregator):
        await bridge.handle_idle()

    @recorder.event_handler("on_audio_data")
    async def on_audio_data(_recorder, audio, sample_rate, num_channels):
        await asyncio.to_thread(save_recording, session_id, audio, sample_rate, num_channels)

    last_heartbeat = None

    async def watch_for_lost_connection():
        while True:
            await asyncio.sleep(1)
            if last_heartbeat and time.monotonic() - last_heartbeat > LOST_CONNECTION_SECONDS:
                await end_call()
                return

    async def end_call():
        await recorder.stop_recording()
        await close_call(session_id)
        await task.cancel()

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, _sender):
        nonlocal last_heartbeat
        if isinstance(message, dict) and message.get("type") == "heartbeat":
            last_heartbeat = time.monotonic()
        if isinstance(message, dict) and message.get("type") == "keypad" and message.get("key") in KEYPAD_COMMANDS:
            await task.queue_frame(InputDTMFFrame(button=KeypadEntry(message["key"])))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        await recorder.start_recording()
        await bridge.greet(greeting)
        asyncio.ensure_future(warm_up(TOOL_LISTS))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await end_call()

    watcher = asyncio.ensure_future(watch_for_lost_connection())
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        watcher.cancel()
        await close_call(session_id)
