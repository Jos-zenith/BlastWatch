"""Farmer message delivery.

- "outbox": the message is queued for an officer to review and forward by WhatsApp/SMS.
  This is the default while the pilot proves farmers act on the messages.
- "telegram": sent through the Telegram Bot API when TELEGRAM_BOT_TOKEN is set. The contact
  is the chat id, and a bot can only message farmers who have started a chat with it.
  Not yet tested against real chats.
"""
import httpx

from . import config

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(channel: str, contact: str, body: str, client: httpx.Client | None = None) -> str:
    """Returns the message status: "queued", "sent" or "failed:<reason>"."""
    if channel != "telegram":
        return "queued"
    if not config.TELEGRAM_BOT_TOKEN:
        return "failed:no_token"
    if client is None:
        with httpx.Client(timeout=15) as own:
            return send(channel, contact, body, own)
    try:
        resp = client.post(TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN),
                           json={"chat_id": contact, "text": body})
    except httpx.HTTPError as exc:
        return f"failed:{type(exc).__name__}"
    return "sent" if resp.status_code == 200 else f"failed:{resp.status_code}"
