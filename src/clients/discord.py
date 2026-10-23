from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests
from src.common.clock import now_kst
from src.common.redact import redact


logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Discord Webhook wrapper. Drop-in replacement for legacy send_message()."""

    def __init__(self, webhook_url: str, timeout: float = 5.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, msg: str, *, include_timestamp: bool = True) -> bool:
        content = redact(self._format(msg, include_timestamp))
        try:
            res = requests.post(
                self.webhook_url,
                json={"content": content},
                timeout=self.timeout,
            )
            res.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning("Discord send failed: %s", type(e).__name__)
            return False

    def send_embed(self, embed: dict[str, Any]) -> bool:
        try:
            res = requests.post(
                self.webhook_url,
                json={"embeds": [embed]},
                timeout=self.timeout,
            )
            res.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning("Discord embed send failed: %s", type(e).__name__)
            return False

    @staticmethod
    def _format(msg: str, include_timestamp: bool) -> str:
        if not include_timestamp:
            return str(msg)
        ts = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{ts}] {msg}"
