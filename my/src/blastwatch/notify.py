"""Delivery adapters. 'outbox' needs no credentials: messages are stored and an
extension officer / operator can forward them (WhatsApp/SMS) while the pilot proves value.
'telegram' sends directly when TELEGRAM_BOT_TOKEN is set. Add WhatsApp/SMS adapters here
only after the pilot shows farmers actually use the channel."""
import os

import httpx


def send(channel: str, contact: str, body: str) -> str:
    if channel == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return "failed:no_token"
        try:
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": contact, "text": body}, timeout=15)
            return "sent" if r.status_code == 200 else f"failed:{r.status_code}"
        except httpx.HTTPError as e:
            return f"failed:{type(e).__name__}"
    return "outbox"
