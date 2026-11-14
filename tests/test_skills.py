from skills import web


def test_resolve_app_known_names():
    assert web.resolve_app("Notepad") == "notepad.exe"
    assert web.resolve_app(" vs code ") == "code"
    assert web.resolve_app("photoshop") is None


def test_normalize_url_adds_scheme_and_blocks_others():
    assert web.normalize_url("youtube.com") == "https://youtube.com"
    assert web.normalize_url("http://example.com") == "http://example.com"
    assert web.normalize_url("file:///C:/Windows") is None
    assert web.normalize_url("javascript:alert(1)") is None


def test_open_website_rejects_bad_scheme(monkeypatch):
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", opened.append)
    assert web.open_website("file:///C:/secret.txt").startswith("error")
    assert opened == []
    web.open_website("github.com")
    assert opened == ["https://github.com"]
