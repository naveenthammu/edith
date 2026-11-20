import threading

import numpy as np

from types import SimpleNamespace

from voice.stt import Listener, clean_transcript, collect_speech, heard_self


class FakeMic:
    def __init__(self, buffered):
        self.buffered, self.dropped = list(buffered), False

    def drop(self):
        self.buffered, self.dropped = [], True

    def chunks(self):
        yield from self.buffered
        while True:
            yield np.zeros(1600, dtype=np.float32)


def fake_listener(mic):
    lst = object.__new__(Listener)  # skip loading Whisper
    lst.threshold, lst.speaker, lst.mic = 0.01, None, mic
    lst.model = SimpleNamespace(transcribe=lambda audio, **kw: (
        [SimpleNamespace(text=f" {len(audio)} samples", no_speech_prob=0.0, avg_logprob=-0.2, compression_ratio=1.0)],
        None))
    return lst


def test_listen_keep_buffer_uses_words_said_right_after_wake_word():
    speech = [np.full(1600, 0.2, dtype=np.float32)] * 5
    mic = FakeMic(speech)
    assert fake_listener(mic).listen(start_timeout=0.6, keep_buffer=True) != ""
    assert mic.dropped is False


def test_listen_drops_old_audio_by_default():
    mic = FakeMic([np.full(1600, 0.2, dtype=np.float32)] * 5)
    assert fake_listener(mic).listen(start_timeout=0.6) is None  # old speech dropped, then only silence
    assert mic.dropped is True


def seg(text, no_speech=0.1, logprob=-0.3, ratio=1.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)


def test_clean_transcript_keeps_real_speech():
    assert clean_transcript([seg(" Open notepad.")]) == "Open notepad."


def test_clean_transcript_drops_noise_and_loops():
    assert clean_transcript([seg(" Thank you.", no_speech=0.9, logprob=-1.5)]) == ""
    assert clean_transcript([seg(" Edith, Edith, Edith, Edith, Edith.", ratio=3.0)]) == ""
    assert clean_transcript([seg(" Edith, Edith.")]) == ""  # wake word alone: nothing to do
    assert clean_transcript([seg(" Edith, close all the apps.")]) == "Edith, close all the apps."


def test_clean_transcript_drops_short_loops_whisper_scores_as_normal():
    # From the log: noise came out as this and EDITH called shutdown_pc.
    assert clean_transcript([seg(" Shut it, shut it, shut it, shut it.")]) == ""
    # A loop split over segments: each one alone scores under the 2.4 ratio limit.
    babble = [seg(" I like to play games that, like, you know,"), seg(" like, you know, like, you know,"),
              seg(" like, you know, like, you know, You")]
    assert clean_transcript(babble) == ""


def test_clean_transcript_keeps_short_natural_repeats():
    assert clean_transcript([seg(" No, no, no.")]) == "No, no, no."
    assert clean_transcript([seg(" Volume up, up.")]) == "Volume up, up."

CHUNK = 0.1  # seconds per chunk


def quiet():
    return np.zeros(1600, dtype=np.float32)


def loud():
    return np.full(1600, 0.2, dtype=np.float32)


def run(chunks, **kw):
    params = dict(chunk_sec=CHUNK, threshold=0.01, start_timeout=1.0, silence=0.5, max_len=3.0)
    params.update(kw)
    return collect_speech(iter(chunks), **params)


def test_returns_none_when_nobody_speaks():
    assert run([quiet()] * 20) is None


def test_records_speech_until_silence():
    audio = run([quiet()] * 3 + [loud()] * 4 + [quiet()] * 10)
    # 3 pre-roll quiet chunks + 4 loud chunks + 5 quiet chunks (0.5 s of silence ends the recording)
    assert len(audio) == 12 * 1600


def test_pre_roll_keeps_at_most_three_chunks():
    audio = run([quiet()] * 8 + [loud()] * 2 + [quiet()] * 10)
    assert len(audio) == (3 + 2 + 5) * 1600


def test_stops_at_max_length():
    audio = run([loud()] * 100)
    assert len(audio) == 30 * 1600


def test_short_pause_does_not_stop_recording():
    audio = run([loud()] * 2 + [quiet()] * 3 + [loud()] * 2 + [quiet()] * 10)
    assert len(audio) == (2 + 3 + 2 + 5) * 1600


def test_heard_self():
    assert not heard_self(2, SimpleNamespace(utterances=2, speaking=threading.Event()))
    assert heard_self(2, SimpleNamespace(utterances=3, speaking=threading.Event()))
    speaking = threading.Event()
    speaking.set()
    assert heard_self(2, SimpleNamespace(utterances=2, speaking=speaking))


def test_muted_mic_yields_silence(monkeypatch):
    mic = real_mic_with_fake_device(monkeypatch)
    capture(mic, 1.0)
    chunks = mic.chunks()
    assert next(chunks).sum() == 1600
    mic.muted.set()
    assert next(chunks).sum() == 0


def real_mic_with_fake_device(monkeypatch, **kwargs):
    """Only replace the sound card; exercise Mic's real callback and queue."""
    import sys
    from voice.stt import Mic

    class InputStream:
        def __init__(self, **options):
            self.callback = options["callback"]

        def start(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=InputStream))
    return Mic(**kwargs)


def capture(mic, value):
    data = np.full((1600, 1), value, dtype=np.float32)
    mic.stream.callback(data, 1600, None, None)


def test_mute_discards_buffered_audio_and_never_enqueues_muted_samples(monkeypatch):
    mic = real_mic_with_fake_device(monkeypatch)
    capture(mic, 0.25)
    assert not mic.q.empty()
    mic.muted.set()
    assert mic.q.empty()
    for _ in range(5):
        capture(mic, 0.75)
    assert mic.q.empty()
    assert np.count_nonzero(next(mic.chunks())) == 0
    mic.muted.clear()
    capture(mic, 0.5)
    assert np.all(next(mic.chunks()) == 0.5)


def test_slow_consumer_keeps_recent_audio_with_a_fixed_buffer_limit(monkeypatch):
    mic = real_mic_with_fake_device(monkeypatch)
    for n in range(100):
        capture(mic, n / 100)
    # At most two seconds: a long model/tool call cannot retain room audio forever.
    assert 1 <= mic.q.qsize() <= 20
    frames = [mic.q.get_nowait() for _ in range(mic.q.qsize())]
    assert np.all(frames[0] >= 0.8)
    assert np.all(frames[-1] == np.float32(0.99))


def test_unmute_drops_any_stale_audio_before_accepting_new_capture(monkeypatch):
    mic = real_mic_with_fake_device(monkeypatch)
    mic.muted.set()
    # A stale producer must not let a pre-unmute frame become a new command.
    mic.q.put(np.full(1600, 0.75, dtype=np.float32))
    mic.muted.clear()
    assert mic.q.empty()
    capture(mic, 0.25)
    assert np.all(next(mic.chunks()) == 0.25)


def test_mute_change_during_transcription_invalidates_the_previous_command():
    mic = FakeMic([loud()] * 5)
    mic.capture_revision = 0
    listener = fake_listener(mic)

    def transcribe(audio, **kwargs):
        mic.capture_revision += 1  # mute changed after capture but before the model answered
        return [seg("Send that message")], None

    listener.model.transcribe = transcribe
    assert listener.listen(start_timeout=0.6, keep_buffer=True) is None


def test_mute_change_during_capture_discards_the_utterance_before_transcription():
    mic = FakeMic([])
    mic.capture_revision = 0

    def chunks():
        yield loud()
        mic.capture_revision += 1
        while True:
            yield quiet()

    mic.chunks = chunks
    listener = fake_listener(mic)

    def forbidden_transcription(*args, **kwargs):
        raise AssertionError("Audio invalidated by mute must never reach transcription")

    listener.model.transcribe = forbidden_transcription
    assert listener.listen(start_timeout=0.6, keep_buffer=True) is None


def test_mute_waits_for_inflight_callback_then_removes_its_audio(monkeypatch):
    mic = real_mic_with_fake_device(monkeypatch)
    copying, release, muting, muted = (threading.Event() for _ in range(4))

    class InflightSamples:
        def __getitem__(self, _slice):
            return self

        def copy(self):
            copying.set()
            assert release.wait(2), "test did not release callback"
            return np.full(1600, 0.75, dtype=np.float32)

    def mute():
        muting.set()
        mic.muted.set()
        muted.set()

    capture_thread = threading.Thread(target=mic.stream.callback, args=(InflightSamples(),))
    mute_thread = threading.Thread(target=mute)
    capture_thread.start()
    try:
        assert copying.wait(2)
        mute_thread.start()
        assert muting.wait(2)
        # Mute cannot finish while the in-flight producer can still publish audio.
        assert not muted.wait(0.05)
    finally:
        release.set()
        capture_thread.join(2)
        if mute_thread.ident is not None:
            mute_thread.join(2)
    assert muted.is_set()
    assert not capture_thread.is_alive() and not mute_thread.is_alive()
    assert mic.q.empty()
