"""Загрузка и декодирование подписки."""
from __future__ import annotations

import base64
import binascii
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from .links import LINK_RE, Link, parse_many
from .i18n import _


class SubscriptionError(Exception):
    pass


@dataclass
class Subscription:
    links: list[Link]
    meta: dict = field(default_factory=dict)  # из заголовка subscription-userinfo


def decode_body(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise SubscriptionError(_("the subscription returned an empty reply"))
    if LINK_RE.match(text):
        decoded = text
    else:
        compact = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
        compact += "=" * (-len(compact) % 4)
        try:
            decoded = base64.b64decode(compact).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            raise SubscriptionError(_("could not decode base64: {e}").format(e=exc)) from exc
    lines = [ln.strip() for ln in decoded.splitlines()]
    lines = [ln for ln in lines if LINK_RE.match(ln)]
    if not lines:
        raise SubscriptionError(_("the reply has no links"))
    return lines


def _parse_userinfo(header: str | None) -> dict:
    meta: dict = {}
    if not header:
        return meta
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k and v.strip().isdigit():
            meta[k] = int(v)
    return meta


def fetch(url: str, user_agent: str = "podkop-sub/1.0", timeout: int = 20) -> Subscription:
    if not url:
        raise SubscriptionError(_("subscription link is not set (Settings → Subscription)"))
    req = urllib.request.Request(url, headers={"User-Agent": user_agent or "podkop-sub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            meta = _parse_userinfo(resp.headers.get("subscription-userinfo"))
    except Exception as exc:  # сеть, TLS, HTTP-ошибки
        raise SubscriptionError(_("could not download the subscription: {e}").format(e=exc)) from exc
    return Subscription(parse_many(decode_body(body)), meta)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in (_("B"), _("KB"), _("MB"), _("GB"), _("TB")):
        if size < 1024 or unit == _("TB"):
            return f"{size:.1f} {unit}" if unit != _("B") else f"{int(size)} {unit}"
        size /= 1024
    return str(n)


def describe_meta(meta: dict) -> str:
    if not meta:
        return ""
    used = meta.get("upload", 0) + meta.get("download", 0)
    parts = []
    if "total" in meta:
        parts.append(_("traffic {used} of {total}").format(used=human_bytes(used), total=human_bytes(meta["total"])))
    if meta.get("expire"):
        parts.append(_("until {d}").format(d=datetime.fromtimestamp(meta["expire"]).strftime("%d.%m.%Y")))
    return ", ".join(parts)
