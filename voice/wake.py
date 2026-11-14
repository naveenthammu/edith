import json
import logging
import queue
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
    with zipfile.ZipFile(archive) as z:
        z.extractall(models_dir)
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
                data = q.get()
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result())["text"]
                else:
                    text = json.loads(rec.PartialResult())["partial"]
                if heard(text, self.word):
                    return
