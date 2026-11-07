"""Simulate what a phone line and a real room do to a caller's voice.

Everything runs at 16 kHz on float samples in [-1, 1]. Noise keeps playing between sentences, as it does on a
real line, so turn detection hears it too. There is no AMR encoder in stock ffmpeg, so low-rate Opus stands in
for a poor mobile codec. Background speech comes from real synthesized voices, so it contains actual words.
"""
import subprocess

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
SPEECH_LEVEL_DB = -26.0
MU = 255


def rms_db(signal):
    return 20 * np.log10(np.sqrt(np.mean(signal * signal)) + 1e-9)


def speech_level_db(signal):
    """The level of a typical speech frame. A median, so one loud burst in a real recording cannot set it."""
    frames = signal[: len(signal) // FRAME_SAMPLES * FRAME_SAMPLES].reshape(-1, FRAME_SAMPLES)
    energy = np.sqrt((frames * frames).mean(axis=1))
    return 20 * np.log10(np.median(energy[energy > energy.max() * 0.02]) + 1e-9)


def at_speech_level(signal):
    return np.clip(signal * 10 ** ((SPEECH_LEVEL_DB - speech_level_db(signal)) / 20), -1, 1)


def shaped_noise(samples, rng, tilt_db_per_octave):
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    frequencies = np.fft.rfftfreq(samples, 1 / SAMPLE_RATE)
    spectrum *= np.maximum(frequencies, 20.0) ** (tilt_db_per_octave / 6.02)
    noise = np.fft.irfft(spectrum, samples)
    return noise / np.sqrt(np.mean(noise * noise))


def band_limited(signal, low=300.0, high=3400.0):
    spectrum = np.fft.rfft(signal)
    frequencies = np.fft.rfftfreq(len(signal), 1 / SAMPLE_RATE)
    rising = np.clip((frequencies - low * 0.7) / (low * 0.3), 0, 1)
    falling = np.clip((high * 1.1 - frequencies) / (high * 0.1), 0, 1)
    return np.fft.irfft(spectrum * rising * falling, len(signal))


def g711(signal):
    """Sample at 8 kHz and pass through 8-bit mu-law, the codec of an ordinary phone call."""
    narrow = np.clip(signal[::2], -1, 1)
    compressed = np.sign(narrow) * np.log1p(MU * np.abs(narrow)) / np.log1p(MU)
    quantized = np.round(compressed * 127) / 127
    expanded = np.sign(quantized) * ((1 + MU) ** np.abs(quantized) - 1) / MU
    return band_limited(np.interp(np.arange(len(signal)) / 2, np.arange(len(expanded)), expanded), 100.0, 3800.0)


def opus(signal, bitrate):
    """Encode and decode with Opus at a low bit rate, as a congested mobile or internet call does."""
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()
    raw = ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1"]
    encode = ["ffmpeg", "-v", "error", *raw, "-i", "pipe:0", "-c:a", "libopus", "-b:a", bitrate]
    encoded = subprocess.run([*encode, "-application", "voip", "-f", "ogg", "pipe:1"], input=pcm, capture_output=True, check=True)
    decode = ["ffmpeg", "-v", "error", "-i", "pipe:0", *raw, "pipe:1"]
    decoded = subprocess.run(decode, input=encoded.stdout, capture_output=True, check=True)
    output = np.frombuffer(decoded.stdout, dtype="<i2").astype(np.float64) / 32767
    return np.pad(output, (0, max(0, len(signal) - len(output))))[: len(signal)]


def room_echo(signal, rng, seconds=0.45):
    """A speakerphone in a room: the voice arrives with a decaying tail of reflections."""
    taps = int(seconds * SAMPLE_RATE)
    response = rng.standard_normal(taps) * np.exp(-6.9 * np.arange(taps) / taps)
    response[0] = 6.0
    size = len(signal) + taps
    wet = np.fft.irfft(np.fft.rfft(signal, size) * np.fft.rfft(response, size), size)[: len(signal)]
    return wet * np.sqrt(np.mean(signal * signal) / (np.mean(wet * wet) + 1e-12))


def packet_loss(signal, rng, loss, burst_frames):
    """Lose 20 ms frames in bursts (a Gilbert-Elliott chain) with a short repeat, as a decoder conceals them."""
    output = signal.copy()
    enter_bad = loss / (burst_frames * (1 - loss))
    leave_bad = 1 / burst_frames
    bad, run = False, 0
    for start in range(0, len(output) - FRAME_SAMPLES, FRAME_SAMPLES):
        bad = rng.random() >= leave_bad if bad else rng.random() < enter_bad
        run = run + 1 if bad else 0
        if bad:
            previous = output[max(0, start - FRAME_SAMPLES) : start]
            fade = 0.6 ** run if run <= 3 else 0.0
            output[start : start + len(previous)] = previous * fade
    return output


def dropouts(signal, rng, per_second, shortest=0.2, longest=0.6):
    """Silence whole stretches, as a cell handoff does."""
    output = signal.copy()
    for _ in range(rng.poisson(per_second * len(signal) / SAMPLE_RATE)):
        length = int(rng.uniform(shortest, longest) * SAMPLE_RATE)
        start = rng.integers(0, max(1, len(signal) - length))
        output[start : start + length] = 0
    return output


CONDITIONS = {
    "clean": {},
    "landline": {"codec": "g711"},
    "mobile_good": {"codec": "opus12", "noise": ("hiss", 32)},
    "mobile_weak": {"codec": "opus8", "noise": ("hiss", 26), "loss": (0.05, 3)},
    "mobile_breaking": {"codec": "g711", "noise": ("hiss", 24), "loss": (0.15, 4), "dropouts": 0.25},
    "car": {"codec": "opus12", "noise": ("road", 8)},
    "street": {"codec": "opus12", "noise": ("street", 5)},
    "cafe": {"codec": "g711", "noise": ("babble", 8)},
    "tv_on": {"codec": "g711", "noise": ("tv", 10)},
    "speakerphone": {"codec": "g711", "echo": True, "gain_db": -8, "noise": ("hiss", 18)},
    "quiet_mic": {"codec": "g711", "gain_db": -20, "noise": ("hiss", 30)},
    "clipped_mic": {"codec": "g711", "gain_db": 20},
}


class PhoneLine:
    def __init__(self, condition, background_speech, seed):
        self.settings = CONDITIONS[condition]
        self.rng = np.random.default_rng(seed)
        self.babble, self.tv = background_speech
        self.bed = self._noise_bed(60 * SAMPLE_RATE)
        self.bed_position = 0

    def _noise_bed(self, samples):
        kind, snr = self.settings.get("noise", (None, 0))
        if kind is None:
            return np.zeros(samples)
        sources = {
            "hiss": lambda: shaped_noise(samples, self.rng, -1),
            "road": lambda: shaped_noise(samples, self.rng, -3),
            "street": lambda: shaped_noise(samples, self.rng, -5) + 1.2 * unit(np.resize(self.babble, samples)),
            "babble": lambda: unit(np.resize(self.babble, samples)),
            "tv": lambda: unit(np.resize(self.tv, samples)),
        }
        return unit(sources[kind]()) * 10 ** ((SPEECH_LEVEL_DB - snr) / 20)

    def _next_noise(self, samples):
        indexes = (self.bed_position + np.arange(samples)) % len(self.bed)
        self.bed_position = int(indexes[-1]) + 1
        return self.bed[indexes]

    def _through_the_network(self, signal):
        settings = self.settings
        codec = settings.get("codec")
        if codec:
            signal = band_limited(signal)
        if codec == "g711":
            signal = g711(signal)
        elif codec in ("opus8", "opus12"):
            signal = opus(signal, codec.removeprefix("opus") + "k")
        if "loss" in settings:
            signal = packet_loss(signal, self.rng, *settings["loss"])
        if "dropouts" in settings:
            signal = dropouts(signal, self.rng, settings["dropouts"])
        return signal

    def carry(self, speech):
        """Take clean speech at the caller's mouth and return what reaches the server."""
        signal = at_speech_level(speech)
        if self.settings.get("echo"):
            signal = room_echo(signal, self.rng)
        signal = signal * 10 ** (self.settings.get("gain_db", 0) / 20)
        return self._through_the_network(np.clip(signal + self._next_noise(len(signal)), -1, 1))

    def idle_sound(self, seconds=30):
        return self._through_the_network(self._next_noise(int(seconds * SAMPLE_RATE)))


def unit(signal):
    return signal / (np.sqrt(np.mean(signal * signal)) + 1e-12)
