"""Загрузка и декодирование подписки."""
from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .links import LINK_RE, Link, parse_many
from .i18n import _

# Схемы мобильных приложений, которые "оборачивают" HTTP-ссылку
_APP_SCHEMES = re.compile(
    r"^(incy|v2rayng|v2ray|clash|hiddify|streisand|shadowrocket|quantumult|surfboard)"
    r"://(add|subscribe|import)/",
    re.IGNORECASE,
)

# Query-параметры, которые содержат вложенный URL подписки
_URL_PARAMS = ("url", "target", "link", "sub", "subscribe", "import")


class SubscriptionError(Exception):
    pass


def resolve_url(url: str) -> str:
    """Извлекает реальный HTTP(S)-URL подписки из любой "обёртки":

    1. Deep-link приложений: incy://add/https://..., clash://install-config?url=...
    2. Шортлинки с параметром: https://link.example.com/?url=https://real-sub.com/...
    3. Обычные HTTPS-ссылки возвращаются без изменений.
    """
    url = url.strip()

    # Если уже есть https:// или http:// — проверяем параметры
    if url.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        for key in _URL_PARAMS:
            if key in params:
                inner = urllib.parse.unquote(params[key][0])
                if inner.startswith("http"):
                    return resolve_url(inner)
        return url  # Нет вложенного URL — просто возвращаем как есть

    # Deep-link: схема_приложения://action/https://...
    m = _APP_SCHEMES.match(url)
    if m:
        rest = url[m.end():]
        inner = urllib.parse.unquote(rest)
        # Вложенный URL должен начинаться с http:// или https://
        idx = inner.find("https://")
        if idx == -1:
            idx = inner.find("http://")
        if idx != -1:
            return resolve_url(inner[idx:])
        # Если нет http — пытаемся достать из хвоста
        idx = inner.rstrip("/").rfind("://")
        if idx != -1:
            return inner[idx:]

    # Обычный случай: что-то непонятное, если там есть http — пытаемся достать
    idx = url.find("https://")
    if idx == -1:
        idx = url.find("http://")
    if idx != -1:
        return url[idx:]

    return url


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


def _load_file_links(path: Path) -> list[str]:
    """Читает файл со ссылками: одна ссылка на строку (vless://, vmess://, и т.д.).
    Пустые строки и комментарии (#) игнорируются."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SubscriptionError(_("could not read file: {e}").format(e=exc)) from exc
    lines = [ln.strip() for ln in text.splitlines()]
    links = [ln for ln in lines if LINK_RE.match(ln)]
    if not links:
        raise SubscriptionError(_("the reply has no links"))
    return links


def _load_file_subs(path: Path, user_agent: str, timeout: int) -> Subscription:
    """Читает файл со списком URL подписок (один URL на строку).
    Каждый URL загружается и все ссылки объединяются."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SubscriptionError(_("could not read file: {e}").format(e=exc)) from exc

    urls = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    # Если строки выглядят как ссылки (vless:// и т.д.) — это файл ссылок, не URL
    sub_urls = [u for u in urls if u.startswith(("http://", "https://"))]
    if not sub_urls:
        # Нет HTTP-ссылок — пробуем как файл готовых ссылок
        links_raw = [u for u in urls if LINK_RE.match(u)]
        if not links_raw:
            raise SubscriptionError(_("the reply has no links"))
        return Subscription(parse_many(links_raw))

    # Загружаем каждый URL и объединяем все ссылки
    all_links: list[str] = []
    errors: list[str] = []
    for sub_url in sub_urls:
        try:
            sub = fetch(sub_url, user_agent, timeout)
            all_links.extend(lnk.raw for lnk in sub.links)
        except SubscriptionError as exc:
            errors.append(f"{sub_url}: {exc}")

    if not all_links:
        detail = "; ".join(errors)
        raise SubscriptionError(_("could not download the subscription: {e}").format(e=detail))

    return Subscription(parse_many(all_links))


def fetch(url: str, user_agent: str = "podkop-sub/1.0", timeout: int = 20) -> Subscription:
    if not url:
        raise SubscriptionError(_("subscription link is not set (Settings → Subscription)"))

    # Обработка file:// — локальный файл
    if url.startswith("file://"):
        path = Path(urllib.parse.unquote(url[7:]))
        if not path.exists():
            raise SubscriptionError(_("could not read file: {e}").format(e=_("file not found: {p}").format(p=path)))
        # Определяем тип файла по содержимому первой непустой строки
        try:
            first = next((ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                          if ln.strip() and not ln.strip().startswith("#")), "")
        except OSError as exc:
            raise SubscriptionError(_("could not read file: {e}").format(e=exc)) from exc
        if first.startswith(("http://", "https://")):
            # Файл со списком URL подписок
            return _load_file_subs(path, user_agent, timeout)
        else:
            # Файл с готовыми ссылками (vless://, vmess:// и т.д.)
            return Subscription(parse_many(_load_file_links(path)))

    # Раскрываем deep-links и шортлинки
    actual_url = resolve_url(url)
    req = urllib.request.Request(actual_url, headers={"User-Agent": user_agent or "podkop-sub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            meta = _parse_userinfo(resp.headers.get("subscription-userinfo"))
    except Exception as exc:
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
