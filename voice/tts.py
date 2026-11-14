import logging
import queue
import threading

log = logging.getLogger(__name__)


class Speaker:
    """pyttsx3 is not thread-safe, so one thread owns the engine and all speech goes through a queue."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="tts").start()

    def say(self, text: str, wait: bool = True) -> None:
        print(f"EDITH: {text}")
        done = threading.Event()
        self._queue.put((text, done))
        if wait:
            done.wait()

    def _run(self) -> None:
        import comtypes  # installed with pyttsx3 on Windows; SAPI needs COM initialised on this thread
        import pyttsx3

        comtypes.CoInitialize()
        engine = pyttsx3.init()
        while True:
            text, done = self._queue.get()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                log.exception("speech failed")
            finally:
                done.set()
