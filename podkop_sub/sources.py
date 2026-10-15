"""Несколько подписок: скачиваем все включённые, объединяем, помним, чья ссылка.

Правила:
  * все подписки пишутся в одну секцию podkop;
  * фильтры (типы, исключённые и предпочитаемые страны) общие для всех подписок;
  * одинаковые серверы берём из первой подписки по порядку списка;
  * у каждой ссылки, пришедшей из подписки, есть владелец (имя подписки) в state["owners"];
    ссылки без владельца (добавлены вручную) синхронизация не удаляет никогда;
  * если подписка не скачалась, её ссылки не трогаем.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .i18n import _
from .links import Link, apply_filters, parse_link
from .subscription import Subscription, SubscriptionError, fetch


@dataclass
class Fetched:
    sources: dict[str, Subscription | SubscriptionError] = field(default_factory=dict)
    ok: list[str] = field(default_factory=list)            # подписки, которые скачались
    errors: dict[str, str] = field(default_factory=dict)   # имя -> текст ошибки
    raw_links: list[Link] = field(default_factory=list)    # все ссылки успешных подписок, без фильтров
    owner_of: dict[tuple, str] = field(default_factory=dict)  # link.key -> имя подписки
    links: list[Link] = field(default_factory=list)        # после общих фильтров

    def require(self) -> None:
        """Нужна хотя бы одна скачанная подписка, иначе работать не с чем."""
        if not self.ok:
            detail = "; ".join(f"{n}: {e}" for n, e in self.errors.items())
            raise SubscriptionError(detail or _("no subscriptions are set up (Settings → Subscriptions)"))


def enabled_sources(cfg) -> list[dict]:
    return [s for s in cfg["subscriptions"] if s.get("enabled", True) and s.get("url")]


def download(cfg) -> dict[str, Subscription | SubscriptionError]:
    """Скачивает каждую включённую подписку один раз (без повторов — их делает вызывающий код)."""
    fetch_cfg = cfg["fetch"]
    out: dict[str, Subscription | SubscriptionError] = {}
    for src in enabled_sources(cfg):
        try:
            out[src["name"]] = fetch(src["url"], fetch_cfg["user_agent"], fetch_cfg["timeout"])
        except SubscriptionError as exc:
            out[src["name"]] = exc
    return out


def merge(cfg, downloaded: dict[str, Subscription | SubscriptionError]) -> Fetched:
    res = Fetched(sources=dict(downloaded))
    seen: set[tuple] = set()
    for name, got in downloaded.items():
        if isinstance(got, SubscriptionError):
            res.errors[name] = str(got)
            continue
        res.ok.append(name)
        for link in got.links:
            if link.key in seen:      # первая подписка выигрывает
                continue
            seen.add(link.key)
            res.raw_links.append(link)
            res.owner_of[link.key] = name
    f = cfg["filters"]
    res.links = apply_filters(res.raw_links, f["types"], f["exclude_countries"], f["prefer_countries"])
    return res


def fetch_all(cfg) -> Fetched:
    return merge(cfg, download(cfg))


def reconcile_owners(raws: list[str], owners: dict, owner_of: dict, configured: set[str]) -> dict:
    """Владельцы для ссылок, которые будут записаны на роутер.

    Ссылка из скачанной подписки получает её имя. Если подписки не скачались (или её нет в
    списке скачанных), прежний владелец сохраняется, пока подписка существует в настройках.
    Ссылки, которых нет в подписках и у которых не было владельца, остаются ручными.
    """
    out: dict[str, str] = {}
    for raw in raws:
        link = parse_link(raw)
        if link is None:
            continue
        if link.key in owner_of:
            out[link.id] = owner_of[link.key]
        elif owners.get(link.id) in configured:
            out[link.id] = owners[link.id]
    return out
