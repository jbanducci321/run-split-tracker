import logging
import os

import requests

logger = logging.getLogger("run-split-tracker")

DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
USER_ID = os.environ.get("DISCORD_USER_ID", "")

_dm_channel_id = None


def _get_dm_channel_id():
    global _dm_channel_id
    if _dm_channel_id:
        return _dm_channel_id
    if not (BOT_TOKEN and USER_ID):
        logger.warning("DISCORD_BOT_TOKEN or DISCORD_USER_ID not set - skipping DM")
        return None

    resp = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        json={"recipient_id": USER_ID},
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.warning("Failed to open Discord DM channel: %s %s", resp.status_code, resp.text)
        return None

    _dm_channel_id = resp.json()["id"]
    return _dm_channel_id


def send_dm(message: str) -> bool:
    channel_id = _get_dm_channel_id()
    if not channel_id:
        return False

    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        json={"content": message},
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.warning("Failed to send Discord DM: %s %s", resp.status_code, resp.text)
        return False
    return True
