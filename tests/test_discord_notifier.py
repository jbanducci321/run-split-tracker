"""discord_notifier: settings are read when a DM is sent, so values loaded from
.env after the import (how the app starts on a laptop) are still used.
Discord itself is faked - nothing is sent."""
from types import SimpleNamespace

import discord_notifier


def test_token_and_user_set_after_import_are_used(monkeypatch):
    requests_made = []

    def fake_post(url, headers=None, **kwargs):
        requests_made.append((url, headers))
        return SimpleNamespace(status_code=200, json=lambda: {"id": "dm-channel-1"}, text="")

    monkeypatch.setattr(discord_notifier.requests, "post", fake_post)
    monkeypatch.setattr(discord_notifier, "_dm_channel_ids", {})
    # Set only now - after discord_notifier was imported, like load_dotenv() on a laptop.
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token-from-dotenv")
    monkeypatch.setenv("DISCORD_USER_ID", "123")

    assert discord_notifier.send_dm("hello") is True
    open_channel, send_message = requests_made
    assert open_channel[1]["Authorization"] == "Bot token-from-dotenv"
    assert send_message[0].endswith("/channels/dm-channel-1/messages")


def test_missing_token_fails_quietly(monkeypatch):
    monkeypatch.setattr(discord_notifier, "_dm_channel_ids", {})
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_USER_ID", "123")
    assert discord_notifier.send_dm("hello") is False  # logged, never raised
