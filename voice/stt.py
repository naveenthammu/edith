import queue
import re
import time
from collections import deque
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SEC = 0.1
EPS = 1e-9  # repeated 0.1 additions are not exact in floating point
PRE_ROLL_CHUNKS = 3  # keep 0.3 s before speech starts
# Biases Whisper toward EDITH's words: short commands like "notepad" were heard as "not bad".
VOCABULARY = ("Edith, open notepad, calculator, VS Code, Chrome, YouTube, WhatsApp, Instagram, Gmail, "
              "weather, Wikipedia, volume up, volume down, mute, screenshot, battery, remind me, note, "
              "lock the PC, shutdown, switch to offline mode, switch to online mode, Naukri profile.")


def clean_transcript(segments, wake_word: str = "edith") -> str:
    """Join Whisper segments, dropping ones Whisper itself marks as likely noise.

    Thresholds are Whisper's own defaults: silence = no_speech_prob > 0.6 with avg_logprob < -1,
    and a repetition loop = compression_ratio > 2.4 (e.g. "Edith, Edith, Edith" from the prompt bias).
    """
    kept = [s.text.strip() for s in segments
            if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) and s.compression_ratio <= 2.4]
    text = " ".join(kept).strip()
    words = re.findall(r"[a-z]+", text.lower())
    return "" if words and set(words) == {wake_word} else text  # only the wake word: nothing to act on


def collect_speech(chunks: Iterator[np.ndarray], chunk_sec: float, threshold: float,
                   start_timeout: float, silence: float, max_len: float) -> np.ndarray | None:
    """Return audio from just before the first loud chunk until `silence` seconds of quiet, or None if nobody spoke."""
    frames: list[np.ndarray] = []
    pre_roll: deque[np.ndarray] = deque(maxlen=PRE_ROLL_CHUNKS)  # quiet first syllables sit below the threshold
    waited = quiet = 0.0
    for chunk in chunks:
        loud = float(np.sqrt(np.mean(chunk ** 2))) > threshold
        if not frames:
            if not loud:
                pre_roll.append(chunk)
                waited += chunk_sec
                if waited + EPS >= start_timeout:
                    return None
                continue
            frames.extend(pre_roll)
        frames.append(chunk)
        quiet = 0.0 if loud else quiet + chunk_sec
        if quiet + EPS >= silence or len(frames) * chunk_sec + EPS >= max_len:
            break
    return np.concatenate(frames) if frames else None


ECHO_TAIL = 0.15  # seconds of EDITH's own voice still reaching the mic after the speaker goes quiet


class Mic:
    """One microphone stream for the whole session, shared by the wake word and the listener.

    Reopening the mic for every utterance lost the words said right after "Edith": in the log,
    "Edith, what's the time now" in one breath came out as "Clip of time now".
    """

    def __init__(self, chunk_sec: float = CHUNK_SEC):
        import sounddevice as sd

        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=int(SAMPLE_RATE * chunk_sec),
                                     callback=lambda data, *_: self.q.put(data[:, 0].copy()))
        self.stream.start()

    def drop(self) -> None:
        """Forget audio already captured, such as EDITH's own voice."""
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def chunks(self) -> Iterator[np.ndarray]:
        while True:
            try:
                yield self.q.get(timeout=0.5)  # wake up so Ctrl+C works on Windows
            except queue.Empty:
                continue


def heard_self(before: int, speaker) -> bool:
    """True if EDITH spoke while we were recording, so the audio is her own voice."""
    return speaker.utterances != before or speaker.speaking.is_set()


class Listener:
    def __init__(self, models_dir: Path, threshold: float = 0.01, model_name: str = "small.en", speaker=None,
                 mic: "Mic | None" = None):
        from faster_whisper import WhisperModel

        self.threshold = threshold
        self.speaker = speaker
        self.mic = mic or Mic()
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                  download_root=str(models_dir / "whisper"))

    def listen(self, start_timeout: float = 10.0, silence: float = 1.0, max_len: float = 15.0,
               keep_buffer: bool = False) -> str | None:
        """Return the spoken text, "" if speech was unclear, or None if nobody spoke before start_timeout.

        keep_buffer=True starts from audio the mic already holds: the rest of a sentence that began with "Edith".
        """
        while True:
            if self.speaker and self.speaker.speaking.is_set():
                while self.speaker.speaking.is_set():
                    time.sleep(0.05)
                time.sleep(ECHO_TAIL)
                keep_buffer = False  # the buffer now holds EDITH's own voice
            if not keep_buffer:
                self.mic.drop()
            before = self.speaker.utterances if self.speaker else 0
            audio = collect_speech(self.mic.chunks(), CHUNK_SEC, self.threshold, start_timeout, silence, max_len)
            if not (self.speaker and heard_self(before, self.speaker)):
                break
            keep_buffer = False
        if audio is None:
            return None
        segments, _ = self.model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                            initial_prompt=VOCABULARY)
        return clean_transcript(segments)
