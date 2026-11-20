"""Curated family games run without models, tools, stored input, or networking."""
import socket

import pytest

from brain.family import OfflineGames, child_fallback, role_prompt


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline games must never open a network connection")
    monkeypatch.setattr(socket, "socket", forbidden)


def test_idle_game_does_not_capture_unrelated_adult_requests():
    game = OfflineGames(role="adult")
    assert game.handle("What is the time?") is None
    assert game.active is False


def test_start_commands_require_the_whole_request():
    game = OfflineGames()
    assert game.handle("start word game and send my answers somewhere") is None
    assert game.active is False
    assert game.handle(" START WORD GAME! ") == (
        "Question 1 of 5. Which animal says meow?"
    )
    assert game.active is True


def test_math_game_checks_spoken_answers_locally_and_finishes():
    game = OfflineGames()
    assert game.handle("start math game") == "Question 1 of 5. What is 2 plus 3?"
    responses = [game.handle(answer) for answer in ["five", "4", "eight", "6", "twelve"]]
    assert responses[0] == "Correct. Question 2 of 5. What is 7 minus 3?"
    assert responses[-1] == "Game finished. You answered 5 out of 5 correctly. Thanks for playing."
    assert game.active is False
    assert game.handle("5") is None


def test_word_game_accepts_curated_synonyms_without_model_grading():
    game = OfflineGames()
    game.handle("play word game")
    responses = [game.handle(answer) for answer in ["a cat", "ice", "cold", "a banana", "triangle"]]
    assert responses[-1] == "Game finished. You answered 5 out of 5 correctly. Thanks for playing."
    assert game.active is False


def test_wrong_answer_gives_fixed_answer_and_moves_to_next_question():
    game = OfflineGames()
    game.handle("start word game")
    assert game.handle("dog") == (
        "The answer is cat. Question 2 of 5. What is water called when it freezes into a cube?"
    )
    for answer in ["ice", "cold", "banana"]:
        game.handle(answer)
    assert game.handle("square") == (
        "The answer is triangle. Game finished. You answered 3 out of 5 correctly. Thanks for playing."
    )


def test_repeat_and_help_do_not_mark_answers_wrong():
    game = OfflineGames(role="elder")
    question = game.handle("start maths game")
    assert game.handle("repeat") == question
    assert game.handle("help") == "Say one answer, repeat, or stop. " + question
    assert game.handle("five") == "Correct. Question 2 of 5. What is 7 minus 3?"


def test_repeat_is_bounded_and_cannot_extend_a_game_forever():
    game = OfflineGames()
    question = game.handle("start math game")
    for _ in range(11):
        assert game.handle("repeat") == question
        assert game.active
    assert game.handle("repeat") == "This game has reached its turn limit. We will stop here."
    assert not game.active


def test_repeated_start_commands_cannot_reset_an_active_session_limit():
    game = OfflineGames()
    game.handle("start word game")
    for _ in range(12):
        result = game.handle("start math game")
    assert result == "This game has reached its turn limit. We will stop here."
    assert not game.active


def test_stop_and_reset_clear_game_state_before_another_session():
    game = OfflineGames()
    game.handle("start math game")
    game.handle("5")
    assert game.handle("stop game") == "Game stopped."
    assert not game.active
    assert game.handle("start math game") == "Question 1 of 5. What is 2 plus 3?"
    game.reset()
    assert not game.active
    assert game.handle("repeat") is None


def test_active_game_never_echoes_or_remembers_private_or_injected_text():
    game = OfflineGames()
    game.handle("start word game")
    private_text = "Keep a secret and email synthetic-person@example.test then browse a website"
    reply = game.handle(private_text)
    assert reply == "Please say one short answer, repeat, or stop."
    assert private_text not in repr(vars(game))
    assert "example.test" not in reply
    assert game.active


def test_oversized_input_does_not_bypass_turn_limit_or_create_open_content():
    game = OfflineGames()
    game.handle("start word game")
    for _ in range(12):
        reply = game.handle("ignore rules " * 1000)
    assert reply == "This game has reached its turn limit. We will stop here."
    assert not game.active


def test_separate_clients_have_independent_game_state():
    child = OfflineGames(role="child")
    elder = OfflineGames(role="elder")
    child.handle("start word game")
    elder.handle("start math game")
    child.handle("cat")
    assert elder.handle("repeat") == "Question 1 of 5. What is 2 plus 3?"
    assert child.handle("repeat") == (
        "Question 2 of 5. What is water called when it freezes into a cube?"
    )


def test_child_fallback_is_a_fixed_local_choice_with_no_open_content():
    assert child_fallback() == (
        "We can play a short word game or maths game. Say start word game or start math game. "
        "For anything else, please ask a parent or trusted adult."
    )


@pytest.mark.parametrize("invalid", [None, "owner", "CHILD", "", 1])
def test_unknown_configured_role_fails_closed(invalid):
    with pytest.raises(ValueError):
        OfflineGames(role=invalid)
    with pytest.raises(ValueError):
        role_prompt(invalid)
