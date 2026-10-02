import json
import logging
import os

import requests

logger = logging.getLogger("run-split-tracker")

DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
USER_ID = os.environ.get("DISCORD_USER_ID", "")

_dm_channel_ids = {}  # recipient user id -> DM channel id


def _get_dm_channel_id(user_id):
    if user_id in _dm_channel_ids:
        return _dm_channel_ids[user_id]
    if not (BOT_TOKEN and user_id):
        logger.warning("DISCORD_BOT_TOKEN or recipient user id not set - skipping DM")
        return None

    resp = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        json={"recipient_id": user_id},
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.warning("Failed to open Discord DM channel: %s %s", resp.status_code, resp.text)
        return None

    _dm_channel_ids[user_id] = resp.json()["id"]
    return _dm_channel_ids[user_id]


def send_dm(message, user_id=None, image_png=None, label=None):
    """DM a user (you, by default), optionally with a PNG attached. Never raises.

    Every call logs one line - "DM sent" or "DM FAILED" - with who it was for
    (label, or the user id) and the message, so the logs show each DM's fate.
    """
    recipient = label or ("you" if not user_id or user_id == USER_ID else f"user {user_id}")
    attachment = " [+ image]" if image_png else ""
    try:
        channel_id = _get_dm_channel_id(user_id or USER_ID)
        if not channel_id:
            logger.warning("DM FAILED to %s (couldn't open a DM channel): %s%s", recipient, message, attachment)
            return False

        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {BOT_TOKEN}"}
        if image_png:
            payload = {"content": message, "attachments": [{"id": 0, "filename": "route.png"}]}
            resp = requests.post(
                url, headers=headers, timeout=20,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("route.png", image_png, "image/png")},
            )
        else:
            resp = requests.post(url, headers=headers, json={"content": message}, timeout=10)
    except requests.RequestException as exc:
        logger.warning("DM FAILED to %s (%s: %s): %s%s", recipient, type(exc).__name__, exc, message, attachment)
        return False

    if resp.status_code >= 300:
        logger.warning("DM FAILED to %s (Discord %s %s): %s%s", recipient, resp.status_code, resp.text, message, attachment)
        return False
    logger.info("DM sent to %s: %s%s", recipient, message, attachment)
    return True
