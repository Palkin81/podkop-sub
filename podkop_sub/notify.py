"""Уведомления в Telegram (необязательные)."""
from __future__ import annotations

import json
import os
import urllib.request
from .i18n import _


class Notifier:
    def __init__(self, cfg):
        self.n = cfg["notify"]
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.n["telegram_token"] and self.n["telegram_chat_id"])

    def send(self, text: str, kind: str = "change") -> bool:
        """kind: change | error — учитывает переключатели on_change / on_error."""
        if not self.enabled:
            return False
        if kind == "change" and not self.n["on_change"]:
            return False
        if kind == "error" and not self.n["on_error"]:
            return False
        return self._post(text)

    def test(self) -> tuple[bool, str]:
        ok = self._post(_("✅ podkop-sub: test message, notifications work."))
        return ok, self.last_error

    def _post(self, text: str) -> bool:
        token = str(self.n["telegram_token"])
        base = os.environ.get("PODKOP_SUB_TG_API", "https://api.telegram.org")
        payload = json.dumps({"chat_id": str(self.n["telegram_chat_id"]),
                              "text": text[:4000],
                              "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(f"{base}/bot{token}/sendMessage", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # сеть, TLS, HTTP-ошибки, неверный токен
            self.last_error = str(exc).replace(token, "***")
            return False
        if not body.get("ok"):
            self.last_error = str(body.get("description", _("reply without ok"))).replace(token, "***")
            return False
        self.last_error = ""
        return True
