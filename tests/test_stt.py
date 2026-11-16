import threading

import numpy as np

from types import SimpleNamespace

from voice.stt import clean_transcript, collect_speech, heard_self


def seg(text, no_speech=0.1, logprob=-0.3, ratio=1.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)


def test_clean_transcript_keeps_real_speech():
    assert clean_transcript([seg(" Open notepad.")]) == "Open notepad."


def test_clean_transcript_drops_noise_and_loops():
    assert clean_transcript([seg(" Thank you.", no_speech=0.9, logprob=-1.5)]) == ""
    assert clean_transcript([seg(" Edith, Edith, Edith, Edith, Edith.", ratio=3.0)]) == ""
    assert clean_transcript([seg(" Edith, Edith.")]) == ""  # wake word alone: nothing to do
    assert clean_transcript([seg(" Edith, close all the apps.")]) == "Edith, close all the apps."

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
