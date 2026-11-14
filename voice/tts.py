import logging
import queue
import threading

log = logging.getLogger(__name__)


class Speaker:
    """pyttsx3 is not thread-safe, so one thread owns the engine and all speech goes through a queue."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self.utterances = 0
        self.speaking = threading.Event()
        self._failed = False
        threading.Thread(target=self._run, daemon=True, name="tts").start()

    def say(self, text: str, wait: bool = True) -> None:
        print(f"EDITH: {text}")
        if self._failed:
            return
        done = threading.Event()
        self._queue.put((text, done))
        if wait:
            while not done.wait(0.5):  # wake up so Ctrl+C works on Windows
                pass

    def _run(self) -> None:
        try:
            import comtypes  # installed with pyttsx3 on Windows; SAPI needs COM initialised on this thread
            import pyttsx3

            comtypes.CoInitialize()
            engine = pyttsx3.init()
        except Exception:
            log.exception("text-to-speech unavailable")
            self._failed = True
            while True:  # release anyone already waiting, without speaking
                self._queue.get()[1].set()
        while True:
            text, done = self._queue.get()
            self.utterances += 1
            self.speaking.set()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                log.exception("speech failed")
            finally:
                self.speaking.clear()
                done.set()
