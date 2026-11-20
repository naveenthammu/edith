"""Queued, cancellable speech with one owner for all native engine calls."""
import logging
import math
import platform
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
POLL_MS = 20


@dataclass
class _Utterance:
    text: str
    generation: int
    done: threading.Event = field(default_factory=threading.Event)
    outcome: str | None = None


class Speaker:
    """Own the speech engine on one thread, including rate changes and purge.

    A factory returns an adapter with start(text), wait_until_done(timeout_ms),
    stop(), set_rate(wpm), and close(). start/stop must be nonblocking, and poll
    must respect its timeout. The factory itself also runs on the owner thread.

    The pending queue is bounded. Blocking say calls wait for capacity and are
    cancelled by stop; nonblocking calls raise queue.Full rather than dropping
    an accepted message. stop releases callers immediately; speaking remains
    set until the native worker confirms completion/purge. No barge-in detector
    or hardware interruption latency guarantee is implied.
    """

    def __init__(self, *, engine_factory=None, max_pending: int = 16):
        if type(max_pending) is not int or not 1 <= max_pending <= 256:
            raise ValueError("max_pending must be an integer between 1 and 256")
        self._condition = threading.Condition()
        self._pending: deque[_Utterance] = deque()
        self._max_pending = max_pending
        self._active = None
        self._generation = 0
        self._rate = 180
        self._last_text = None
        self._failed = False
        self._closed = False
        self.utterances = 0
        self.speaking = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(engine_factory or _speech_engine,),
                                        daemon=True, name="tts")
        self._thread.start()

    @property
    def generation(self) -> int:
        """Capture before slow work, then pass as say's expected_generation."""
        with self._condition:
            return self._generation

    def _enqueue_locked(self, text, wait, expected_generation=None):
        generation = self._generation
        if expected_generation is not None and expected_generation != generation:
            return None
        while len(self._pending) >= self._max_pending:
            if self._closed or self._failed or generation != self._generation:
                return None
            if not wait:
                raise queue.Full("Speech queue is full")
            self._condition.wait()
        if self._closed or self._failed or generation != self._generation:
            return None
        item = _Utterance(text, generation)
        self._pending.append(item)
        self._condition.notify_all()
        return item

    @staticmethod
    def _wait(item):
        if item is not None:
            while not item.done.wait(0.5):  # permit Ctrl+C on Windows
                pass

    def say(self, text: str, wait: bool = True, *, expected_generation=None) -> None:
        """Speak unless an optional admission token predates the last stop."""
        if not isinstance(text, str):
            raise TypeError("Speech text must be a string")
        if not text.strip():
            return
        with self._condition:
            item = self._enqueue_locked(text, wait, expected_generation)
        if item is not None:
            print(f"EDITH: {text}")
        if wait:
            self._wait(item)

    def say_tracked(self, text: str, *, expected_generation=None) -> str:
        """Wait for completed/cancelled/failed, without logging spoken content.

        Completed means the native engine reported completion, not that anyone
        heard or acknowledged it. Cancellation releases the caller immediately.
        """
        if not isinstance(text, str):
            raise TypeError("Speech text must be a string")
        if not text.strip():
            return "failed"
        with self._condition:
            if expected_generation is not None and expected_generation != self._generation:
                return "cancelled"
            item = self._enqueue_locked(text, True, expected_generation)
            if item is None:
                return "failed" if self._failed and not self._closed else "cancelled"
        self._wait(item)
        return item.outcome or "failed"

    @staticmethod
    def _finish_locked(item, outcome):
        if item.outcome is None:
            item.outcome = outcome
        item.done.set()

    def _cancel_locked(self, outcome="cancelled"):
        self._generation += 1
        while self._pending:
            self._finish_locked(self._pending.popleft(), outcome)
        if self._active is not None:
            self._finish_locked(self._active, outcome)
        self._condition.notify_all()

    def stop(self) -> None:
        """Cancel current and pending speech; native purge runs on the worker."""
        with self._condition:
            self._cancel_locked()

    def set_rate(self, words_per_minute: int) -> None:
        """Request 80..260 WPM; native voices may apply changes next utterance.

        SAPI uses a voice-relative rate, so its WPM setting is approximate.
        """
        if type(words_per_minute) is not int or not 80 <= words_per_minute <= 260:
            raise ValueError("Speech rate must be an integer between 80 and 260 WPM")
        with self._condition:
            self._rate = words_per_minute
            self._condition.notify_all()

    def repeat_last(self, wait: bool = True) -> bool:
        """Queue the last successfully completed result, never cancelled text."""
        with self._condition:
            if self._last_text is None:
                return False
            item = self._enqueue_locked(self._last_text, wait)
        if wait:
            self._wait(item)
        return item is not None

    def close(self, timeout: float = 1.0) -> bool:
        """Cancel, close on the owner thread, and report whether it exited.

        A stuck native driver/factory cannot be forcibly killed by Python;
        returning False makes that limitation visible to the caller.
        """
        with self._condition:
            self._closed = True
            self._cancel_locked()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def _play(self, engine, item):
        with self._condition:
            if item.generation != self._generation or self._closed:
                item.outcome = item.outcome or "cancelled"
                return
            rate = self._rate
            engine.set_rate(rate)
            # Start and cancellation are linearized by the same lock. Native
            # start is asynchronous, so this never holds the lock for playback.
            self.utterances += 1
            self.speaking.set()
            engine.start(item.text)
        while True:
            with self._condition:
                cancelled = item.generation != self._generation or self._closed
                if not cancelled and rate != self._rate:
                    rate = self._rate
                    engine.set_rate(rate)
            if cancelled:
                engine.stop()
                deadline = time.monotonic() + 2.0
                while not engine.wait_until_done(POLL_MS):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Speech engine did not acknowledge cancellation")
                return
            if engine.wait_until_done(POLL_MS):
                with self._condition:
                    if item.generation == self._generation and not self._closed:
                        self._last_text = item.text
                        item.outcome = "completed"
                    else:
                        item.outcome = item.outcome or "cancelled"
                return

    def _run(self, factory):
        engine = None
        try:
            engine = factory()
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    item = self._pending.popleft()
                    self._active = item
                    self._condition.notify_all()
                try:
                    self._play(engine, item)
                finally:
                    with self._condition:
                        self.speaking.clear()
                        self._active = None
                        self._finish_locked(item, "failed")
                        self._condition.notify_all()
        except Exception:
            # Native exception messages can echo the spoken text.
            log.error("Text-to-speech unavailable; speech disabled")
            with self._condition:
                self._failed = True
                self._cancel_locked("failed")
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    log.error("Speech engine purge failed")
                try:
                    engine.close()
                except Exception:
                    log.error("Speech engine close failed")
            self.speaking.clear()


class _SapiEngine:
    def __init__(self, voice, uninitialize):
        self.voice = voice
        self._uninitialize = uninitialize

    def start(self, text):
        # Async | IsNotXML. Never allow model text to execute SAPI XML/audio tags.
        self.voice.Speak(text, 1 | 16)

    def wait_until_done(self, timeout_ms):
        return bool(self.voice.WaitUntilDone(timeout_ms))

    def stop(self):
        self.voice.Speak("", 1 | 2 | 16)  # Async | PurgeBeforeSpeak | IsNotXML

    def set_rate(self, words_per_minute):
        # SAPI exposes -10..10 relative steps, not calibrated WPM. Use an
        # approximate 180-WPM baseline; actual speed depends on installed voice.
        # Ten steps span a threefold rate change in the SAPI specification.
        self.voice.Rate = max(-10, min(10, round(10 * math.log(words_per_minute / 180, 3))))

    def close(self):
        self.voice = None  # release COM reference before uninitializing owner
        self._uninitialize()


class _Pyttsx3Engine:
    """Pump the driver's external loop so the owner can process cancellation.

    Availability and interrupt latency depend on the installed driver. A driver
    lacking an external iterator fails closed instead of using blocking speech.
    """

    def __init__(self, engine):
        self.engine = engine
        self._tokens = []
        self._loop_started = False
        self._completed = None
        self._error = None
        self._stopping = False
        try:
            self._tokens.append(engine.connect("error", self._on_error))
            self._tokens.append(engine.connect("finished-utterance", self._on_finished))
            engine.startLoop(False)
            self._loop_started = True
            engine.iterate()  # fail here if this driver lacks an external loop
        except Exception:
            try:
                engine.stop()
            finally:
                self.close()
            raise

    def _on_error(self, name, exception):
        self._error = exception

    def _on_finished(self, name, completed):
        self._completed = bool(completed)

    def start(self, text):
        self._completed = None
        self._error = None
        self._stopping = False
        self.engine.say(text)

    def wait_until_done(self, timeout_ms):
        self.engine.iterate()
        if self._error is not None:
            raise RuntimeError("Speech driver reported an error") from self._error
        if self._completed is False and not self._stopping:
            raise RuntimeError("Speech driver did not complete the utterance")
        if (self._completed is True or self._stopping) and not self.engine.isBusy():
            return True
        time.sleep(timeout_ms / 1000)
        return False

    def stop(self):
        self._stopping = True
        self.engine.stop()

    def set_rate(self, words_per_minute):
        self.engine.setProperty("rate", words_per_minute)

    def close(self):
        try:
            if self._loop_started:
                self._loop_started = False
                self.engine.endLoop()
        finally:
            while self._tokens:
                self.engine.disconnect(self._tokens.pop())


def _speech_engine():
    """Create a native adapter on its sole owner thread, never in the caller."""
    if platform.system() == "Windows":
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        try:
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            return _SapiEngine(voice, comtypes.CoUninitialize)
        except Exception:
            comtypes.CoUninitialize()
            raise
    import pyttsx3

    return _Pyttsx3Engine(pyttsx3.init())
