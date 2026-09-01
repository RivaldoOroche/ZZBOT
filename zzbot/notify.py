"""Notificaciones opcionales por webhook o Telegram. Solo stdlib.

Nunca lanza excepciones hacia el motor: que falle un aviso no puede tumbar el
bot ni, peor, dejar una posicion sin gestionar.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .config import NotifyConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def send(self, event: str, message: str) -> None:
        if not self.cfg.enabled or event not in self.cfg.notify_on:
            return
        try:
            if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
                self._telegram(message)
            if self.cfg.webhook_url:
                self._webhook(event, message)
        except Exception as exc:  # noqa: BLE001 - un aviso fallido nunca detiene el bot
            log.warning("no se pudo enviar la notificacion: %s", exc)

    def _post(self, url: str, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "User-Agent": "zzbot/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8):
            pass

    def _webhook(self, event: str, message: str) -> None:
        self._post(self.cfg.webhook_url, {"event": event, "text": message})

    def _telegram(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        self._post(url, {"chat_id": self.cfg.telegram_chat_id, "text": message})
