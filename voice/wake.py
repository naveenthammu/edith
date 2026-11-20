import json
import logging
import shutil
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
VOSK_NAME = "vosk-model-small-en-us-0.15"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_NAME}.zip"


@dataclass(frozen=True)
class WakeSnapshot:
    """State associated with the accepted wake audio, not sampled by its caller later."""
    token: tuple
    speech_count: int | None


def _snapshot(mic, speaker, current_generation):
    return WakeSnapshot((getattr(mic, 'capture_revision', None),
                         getattr(speaker, 'generation', None), current_generation()),
                        getattr(speaker, 'utterances', None))


def _interrupted(mic, speaker):
    muted = getattr(mic, 'muted', None)
    return ((speaker is not None and speaker.speaking.is_set())
            or (muted is not None and muted.is_set()))


def heard(text: str, word: str) -> bool:
    return word in text.split()


def ensure_vosk_model(models_dir: Path) -> Path:
    target = models_dir / VOSK_NAME
    if target.is_dir():
        return target
    archive = models_dir / f"{VOSK_NAME}.zip"
    log.info("downloading %s", VOSK_URL)
    urllib.request.urlretrieve(VOSK_URL, archive)
    tmp = models_dir / (VOSK_NAME + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(tmp)
    (tmp / VOSK_NAME).rename(target)  # only a complete model ever appears at target
    shutil.rmtree(tmp, ignore_errors=True)
    archive.unlink()
    return target


class WakeWord:
    def __init__(self, model_path: Path, word: str = "edith"):
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        self.model = Model(str(model_path))
        self.word = word

    def wait(self, mic, *, speaker=None, clock=time.monotonic,
             current_generation=lambda: None) -> WakeSnapshot | None:
        """Return the accepted audio's immutable state; later audio stays buffered.

        The caller must retain this receipt while acquiring its conversation lock.
        Recheck cancellation after native recognition, which may release the GIL.
        """
        import numpy as np
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self.model, SAMPLE_RATE, json.dumps([self.word, "[unk]"]))
        mic.drop()  # older audio may hold EDITH saying her own name
        baseline = _snapshot(mic, speaker, current_generation)
        quiet_after = 0.0
        for chunk in mic.chunks():
            captured = _snapshot(mic, speaker, current_generation)
            if captured != baseline or _interrupted(mic, speaker):
                rec.Reset()
                mic.drop()
                baseline = captured
                quiet_after = clock() + 0.15
                continue
            if clock() < quiet_after:
                continue
            data = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result())["text"]
            else:
                text = json.loads(rec.PartialResult())["partial"]
            after = _snapshot(mic, speaker, current_generation)
            if after != captured or _interrupted(mic, speaker):
                rec.Reset()
                mic.drop()
                baseline = after
                quiet_after = clock() + 0.15
                continue
            if heard(text, self.word):
                return captured
