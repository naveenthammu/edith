from voice.wake import heard


def test_heard_matches_whole_word():
    assert heard("edith", "edith")
    assert heard("hey edith", "edith")
    assert not heard("[unk]", "edith")
    assert not heard("", "edith")
    assert not heard("editha", "edith")
