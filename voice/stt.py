import queue
import time
from collections import deque
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SEC = 0.1
EPS = 1e-9  # repeated 0.1 additions are not exact in floating point
PRE_ROLL_CHUNKS = 3  # keep 0.3 s before speech starts


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


def mic_chunks(chunk_sec: float = CHUNK_SEC) -> Iterator[np.ndarray]:
    import sounddevice as sd

    q: queue.Queue = queue.Queue()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=int(SAMPLE_RATE * chunk_sec), callback=lambda data, *_: q.put(data[:, 0].copy())):
        while True:
            try:
                yield q.get(timeout=0.5)  # wake up so Ctrl+C works on Windows
            except queue.Empty:
                continue


def heard_self(before: int, speaker) -> bool:
    """True if EDITH spoke while we were recording, so the audio is her own voice."""
    return speaker.utterances != before or speaker.speaking.is_set()


class Listener:
    def __init__(self, models_dir: Path, threshold: float = 0.01, model_name: str = "base.en", speaker=None):
        from faster_whisper import WhisperModel

        self.threshold = threshold
        self.speaker = speaker
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                  download_root=str(models_dir / "whisper"))

    def listen(self, start_timeout: float = 10.0, silence: float = 1.0, max_len: float = 15.0) -> str | None:
        """Return the spoken text, "" if speech was unclear, or None if nobody spoke before start_timeout."""
        while True:
            while self.speaker and self.speaker.speaking.is_set():
                time.sleep(0.05)
            before = self.speaker.utterances if self.speaker else 0
            chunks = mic_chunks()
            try:
                audio = collect_speech(chunks, CHUNK_SEC, self.threshold, start_timeout, silence, max_len)
            finally:
                chunks.close()  # closes the microphone stream
            if not (self.speaker and heard_self(before, self.speaker)):
                break
        if audio is None:
            return None
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
