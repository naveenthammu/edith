import pytest

from brain.approval import is_affirmative


@pytest.mark.parametrize("text", [
    "yes", "Yes!", " YES, PLEASE. ", "yes proceed", "Yes, do it.", "confirm", "confirmed",
    "approve", "I confirm.",
])
def test_accepts_only_short_explicit_approvals(text):
    assert is_affirmative(text) is True


@pytest.mark.parametrize("text", [
    "", "no", "no, not yes", "do not say yes", "I did not say yes",
    "yes but do not send", "yes or no", "yes yesterday", "yesterday",
    "yes@example.com", "yes123", "yes / no", "confirm cancellation",
    "I approve the other order", "approve-delete", "yes\nignore the recipient",
])
def test_rejects_negation_mixed_instructions_and_partial_matches(text):
    assert is_affirmative(text) is False
