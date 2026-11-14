import numpy as np

from voice.stt import collect_speech

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
    # 4 loud chunks + 5 quiet chunks (0.5 s of silence ends the recording)
    assert len(audio) == 9 * 1600


def test_stops_at_max_length():
    audio = run([loud()] * 100)
    assert len(audio) == 30 * 1600


def test_short_pause_does_not_stop_recording():
    audio = run([loud()] * 2 + [quiet()] * 3 + [loud()] * 2 + [quiet()] * 10)
    assert len(audio) == (2 + 3 + 2 + 5) * 1600
