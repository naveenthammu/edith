import pytest

from skills import naukri


@pytest.fixture
def daily(tmp_path, monkeypatch):
    """naukri wired to temp files, a fake browser refresh, and a recorded speaker."""
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(naukri, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(naukri, "LAST_FILE", tmp_path / "last.txt")
    monkeypatch.setattr(naukri, "TRIED_FILE", tmp_path / "tried.txt")
    said, runs = [], []
    monkeypatch.setattr(naukri, "announce", said.append)
    result = {"text": "Your Naukri profile is refreshed for today."}

    def fake_refresh():  # the real one drives a browser
        runs.append(1)
        return result["text"]

    monkeypatch.setattr(naukri, "refresh_naukri", fake_refresh)
    return naukri, said, runs, result


def test_daily_refresh_says_why_it_failed(daily):
    nk, said, _, result = daily
    result["text"] = "error: the Naukri login has expired. Ask me to log in to Naukri again."
    nk.daily_once("2026-11-16")
    assert said[-1] == "Naukri refresh failed, sir: the Naukri login has expired. Ask me to log in to Naukri again."


def test_daily_refresh_stays_quiet_on_success(daily):
    nk, said, _, _ = daily
    nk.daily_once("2026-11-16")
    assert len(said) == 1  # only the "browser is about to open" notice


def test_daily_refresh_tries_once_a_day_across_restarts(daily):
    nk, _, runs, result = daily
    result["text"] = "error: the Naukri login has expired. Ask me to log in to Naukri again."
    nk.daily_once("2026-11-16")
    nk.daily_once("2026-11-16")  # EDITH restarted the same day: no second browser pop-up
    assert len(runs) == 1
    nk.daily_once("2026-11-17")
    assert len(runs) == 2


def test_daily_refresh_waits_for_first_login(daily, tmp_path):
    nk, said, runs, _ = daily
    (tmp_path / "profile").rmdir()
    nk.daily_once("2026-11-16")
    assert runs == [] and said == []
