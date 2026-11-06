"""Refit the turn model from labelled human calls and compare policies on held-out calls.

tests/labelled_turns.json holds patient turns from real calls. A turn is labelled still_talking when the system
merged it into the next turn. Pauses are gaps inside a patient's turn, measured from the stereo recordings.
"""
import json
import math
from collections import Counter
from pathlib import Path
from statistics import NormalDist

from refill import turn_model
from refill.steps import Step
from refill.turn_model import ANSWERED, HANGING, OPEN, required_silence, slot_state

DATA = json.loads((Path(__file__).parent / "labelled_turns.json").read_text(encoding="utf-8"))
STATES = (ANSWERED, HANGING, OPEN)
PRIOR_LOG_MEAN, PRIOR_STRENGTH, PRIOR_SHAPE, PRIOR_RATE = math.log(0.6), 1.0, 2.0, 0.6
FINAL_TRANSCRIPT_SECONDS = 0.46
CUT_OFF_COST_SECONDS = 3.0
LIMITS_BEFORE = {Step.IDENTIFY: 3.0, Step.MEDICATION: 1.0}


def pause_posterior(pauses):
    logs = [math.log(pause) for pause in pauses]
    n, mean = len(logs), sum(logs) / len(logs)
    squares = sum((value - mean) ** 2 for value in logs)
    strength = PRIOR_STRENGTH + n
    log_mean = (PRIOR_STRENGTH * PRIOR_LOG_MEAN + n * mean) / strength
    shape = PRIOR_SHAPE + n / 2
    rate = PRIOR_RATE + squares / 2 + PRIOR_STRENGTH * n * (mean - PRIOR_LOG_MEAN) ** 2 / (2 * strength)
    return log_mean, math.sqrt(rate * (strength + 1) / (shape * strength))


def evidence(turns):
    talking = Counter(turn["state"] for turn in turns if turn["still_talking"])
    finished = Counter(turn["state"] for turn in turns if not turn["still_talking"])
    n_talking, n_finished = sum(talking.values()), sum(finished.values())
    ratios = {s: ((talking[s] + 1) / (n_talking + 3)) / ((finished[s] + 1) / (n_finished + 3)) for s in STATES}
    return (n_talking + 1) / (n_finished + 1), ratios


def score(turns, wait_for, pause_model):
    pause_chance = NormalDist(*pause_model)
    waits, cut_offs = [], 0.0
    for held_out in sorted({turn["call"] for turn in turns}):
        prior_odds, ratios = evidence([turn for turn in turns if turn["call"] != held_out])
        for turn in (turn for turn in turns if turn["call"] == held_out):
            wait = max(wait_for(turn, prior_odds, ratios), FINAL_TRANSCRIPT_SECONDS)
            if turn["still_talking"]:
                cut_offs += 1 - pause_chance.cdf(math.log(wait))
            else:
                waits.append(wait)
    waits.sort()
    mean = sum(waits) / len(waits)
    return waits[len(waits) // 2], mean, waits[int(len(waits) * 0.9)], cut_offs, mean + CUT_OFF_COST_SECONDS * cut_offs / len(turns)


def bayesian_wait(turn, prior_odds, ratios):
    turn_model.PRIOR_ODDS_STILL_TALKING = prior_odds
    turn_model.STILL_TALKING_LIKELIHOOD_RATIO = ratios
    return required_silence(turn["state"], turn["step"])


def main():
    turns = [dict(turn, step=Step(turn["step"])) for turn in DATA["turns"]]
    for turn in turns:
        turn["state"] = slot_state(turn["step"], turn["question"], turn["text"])
    log_mean, log_scale = pause_posterior(DATA["pause_seconds"])
    turn_model.PAUSE_LOG_MEAN, turn_model.PAUSE_LOG_SCALE = log_mean, log_scale
    prior_odds, ratios = evidence(turns)
    print(f"{len(turns)} turns, {sum(turn['still_talking'] for turn in turns)} still talking")
    print(f"prior odds {prior_odds:.3f}, likelihood ratios {({s: round(r, 3) for s, r in ratios.items()})}")
    print(f"pause log mean {log_mean:.3f}, log scale {log_scale:.3f}")
    policies = {
        "fixed 0.5 s": lambda *_: 0.5,
        "fixed 1.0 s": lambda *_: 1.0,
        "fixed 2.0 s": lambda *_: 2.0,
        "step limits before": lambda turn, *_: LIMITS_BEFORE.get(turn["step"], 0.6),
        "slot-aware Bayesian": bayesian_wait,
    }
    print(f"{'policy':24}{'median':>8}{'mean':>7}{'p90':>7}{'cut-offs':>10}{'cost':>7}")
    for name, policy in policies.items():
        median, mean, p90, cut_offs, cost = score(turns, policy, (log_mean, log_scale))
        print(f"{name:24}{median:8.2f}{mean:7.2f}{p90:7.2f}{cut_offs:10.1f}{cost:7.2f}")


if __name__ == "__main__":
    main()
