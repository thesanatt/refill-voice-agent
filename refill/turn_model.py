"""Decide how much silence to wait for before replying.

Each turn is a small Bayesian decision. The question the agent just asked says whether the words so far answer
it, which shifts the odds that the patient is still talking. Those odds then fall as silence grows, at the rate
real pauses end. The agent replies once the risk of cutting the patient off is below an accepted level, which
is a cost choice. See D15 in docs/DECISIONS.md and tests/fit_turn_model.py.
"""
import math
import re
from collections import deque
from difflib import SequenceMatcher
from statistics import NormalDist

from loguru import logger

from refill.steps import Step

ANSWERED, HANGING, OPEN = "answered", "hanging", "open"
YES_NO_STEPS = (Step.PHARMACY, Step.INSURANCE, Step.CONFIRM)

PRIOR_ODDS_STILL_TALKING = 0.25
STILL_TALKING_LIKELIHOOD_RATIO = {ANSWERED: 0.383, HANGING: 5.714, OPEN: 0.63}
PAUSE_LOG_MEAN = -0.388
PAUSE_LOG_SCALE = 0.596
ACCEPTED_CUT_OFF_RISK = {Step.IDENTIFY: 0.05}
DEFAULT_CUT_OFF_RISK = 0.10
OWN_VOICE_SHARE = 0.75
OWN_VOICE_PROOF_WORDS = 5
OWN_VOICE_RUN_WORDS = 2

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
SPOKEN_DAY = (
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth"
    r"|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|twenty[- ]\w+|thirtieth|thirty[- ]first)"
)
DAY = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{SPOKEN_DAY})"
YEAR = r"(?:\d{4}|\d{2}\b|nineteen[- ]\w+(?:[- ]\w+)?)"
FULL_DATE = re.compile(
    rf"\b{MONTH}\b.*\b{DAY}\b.*\b{YEAR}|\b{DAY}\b.*\b{MONTH}\b.*\b{YEAR}|\b{YEAR}\b.*\b{MONTH}\b.*\b{DAY}\b"
    r"|\b\d{1,2}[/ -]\d{1,2}[/ -]\d{2,4}\b",
    re.IGNORECASE,
)
IDENTITY_STARTED = re.compile(rf"\b(name|birth|born|{MONTH}|nineteen|{SPOKEN_DAY})\b|\d", re.IGNORECASE)
SHORT_ANSWER = re.compile(
    r"\b(yes|yeah|yep|yup|no|nope|sure|okay|ok|correct|right|send it|go ahead|please|don'?t know|not sure|hold on"
    r"|one (?:second|moment|minute)|wait|never ?mind|same|cvs|walgreens|walmart|main street|oak|market|pharmacist)\b",
    re.IGNORECASE,
)
MEDICATION_ANSWER = re.compile(
    r"\b(lisinopril|metformin|atorvastatin|blood pressure|diabetes|cholesterol|first|second|third|all|both|one|two"
    r"|three|\d)\b",
    re.IGNORECASE,
)
FILLER_ONLY = re.compile(r"^\W*(?:(?:um+|uh+|er+|ah+|hmm+|mm+|so|well|like)\W*)+$", re.IGNORECASE)
TRAILING_OFF = re.compile(
    r"\b(um+|uh+|er+|my|the|a|an|and|or|but|to|for|of|with)\W*$|\bmy (?:correct|right|old|new|current)\W*$",
    re.IGNORECASE,
)
UNFINISHED = re.compile(r"\b(is|are|was|were|am|at|on|in|i|i'm|i'd|i'll|it's|need|want|like|have|has)\W*$", re.IGNORECASE)
DIRECT_ANSWER = re.compile(
    r"^\W*(?:(?:um+|uh+|well)\W+)?(yes|yeah|yep|yup|no|nope|okay|ok|sure|correct|right)\b", re.IGNORECASE
)


def question_asked(line):
    if "last name" in line or line.endswith("full name?"):
        return "name"
    if "born" in line or ("date of birth" in line and "full name" not in line):
        return "date"
    return "both"


def slot_state(step, question, text):
    if FILLER_ONLY.match(text) or TRAILING_OFF.search(text):
        return HANGING
    if step in YES_NO_STEPS and DIRECT_ANSWER.match(text):
        return ANSWERED
    if UNFINISHED.search(text):
        return HANGING
    if step is Step.IDENTIFY and question == "name":
        return ANSWERED if len(text.split()) <= 4 else OPEN
    if step is Step.IDENTIFY:
        if FULL_DATE.search(text) or (question == "date" and len(text.split()) <= 2):
            return ANSWERED
        return HANGING if IDENTITY_STARTED.search(text) else OPEN
    pattern = MEDICATION_ANSWER if step is Step.MEDICATION else SHORT_ANSWER
    return ANSWERED if pattern.search(text) else OPEN


def required_silence(state, step):
    accepted_risk = ACCEPTED_CUT_OFF_RISK.get(step, DEFAULT_CUT_OFF_RISK)
    accepted_odds = accepted_risk / (1 - accepted_risk)
    odds_before_silence = PRIOR_ODDS_STILL_TALKING * STILL_TALKING_LIKELIHOOD_RATIO[state]
    pause_survival = accepted_odds / odds_before_silence
    if pause_survival >= 1:
        return 0.0
    return math.exp(PAUSE_LOG_MEAN + PAUSE_LOG_SCALE * NormalDist().inv_cdf(1 - pause_survival))


def spoken_words(text):
    return re.findall(r"[a-z']+", text.lower().replace("-", " ").replace("milligrams", "mg"))


class OwnVoice:
    """Spots the agent's words coming back through the patient's microphone. See D29."""

    def __init__(self):
        self._lines = deque(maxlen=2)
        self.proven = False

    def expect(self, text):
        self._lines.append(spoken_words(text))

    def _matches(self, heard):
        line = [word for words in self._lines for word in words]
        return SequenceMatcher(None, heard, line, autojunk=False).get_matching_blocks()

    def repeats_start(self, text):
        heard = spoken_words(text)
        return bool(heard and self._lines) and self._lines[-1][: len(heard)] == heard

    def sounds_like_line(self, text):
        heard = spoken_words(text)
        if not heard:
            return not re.search(r"\d", text)
        matches = self._matches(heard)
        opening = len(heard) >= OWN_VOICE_RUN_WORDS and self.repeats_start(text)
        if not self.proven and (opening or max(match.size for match in matches) >= OWN_VOICE_PROOF_WORDS):
            logger.info(f"Own voice proven by: {text}")
            self.proven = True
        return sum(match.size for match in matches) / len(heard) >= OWN_VOICE_SHARE

    def without_line(self, text):
        words = text.split()
        positions = [index for index, word in enumerate(words) if spoken_words(word)]
        heard = ["".join(spoken_words(words[index])) for index in positions]
        repeated = set()
        for match in self._matches(heard):
            if match.size >= OWN_VOICE_RUN_WORDS:
                repeated |= set(range(positions[match.a], positions[match.a + match.size - 1] + 1))
        return " ".join(word for index, word in enumerate(words) if index not in repeated)
