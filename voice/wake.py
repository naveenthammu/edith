import json
import logging
import queue
import shutil
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
VOSK_NAME = "vosk-model-small-en-us-0.15"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_NAME}.zip"


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

    def wait(self) -> None:
        """Block until the wake word is heard."""
        import sounddevice as sd
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self.model, SAMPLE_RATE, json.dumps([self.word, "[unk]"]))
        q: queue.Queue = queue.Queue()
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=4000, dtype="int16", channels=1,
                               callback=lambda data, *_: q.put(bytes(data))):
            while True:
                try:
                    data = q.get(timeout=0.5)  # wake up so Ctrl+C works on Windows
                except queue.Empty:
                    continue
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result())["text"]
                else:
                    text = json.loads(rec.PartialResult())["partial"]
                if heard(text, self.word):
                    return
