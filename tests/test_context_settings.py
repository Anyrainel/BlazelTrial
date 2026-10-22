"""Context maintenance boundaries are centrally configured and validated."""

import pytest
from pydantic import ValidationError

from chatbot.config import Settings


def test_context_turn_settings_from_environment(monkeypatch):
    monkeypatch.setenv("HISTORY_WINDOW_STEP", "4")
    monkeypatch.setenv("SUMMARY_CHUNK_TURNS", "12")
    monkeypatch.setenv("SUMMARY_KEEP_RECENT_TURNS", "6")
    settings = Settings(_env_file=None)
    assert (
        settings.history_window_step,
        settings.summary_chunk_turns,
        settings.summary_keep_recent_turns,
    ) == (4, 12, 6)


@pytest.mark.parametrize(
    "field,value",
    [
        ("history_window_step", 0),
        ("summary_chunk_turns", 0),
        ("summary_keep_recent_turns", -1),
        ("history_token_budget", -1),
    ],
)
def test_invalid_context_boundaries_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
