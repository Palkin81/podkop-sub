"""Разбор, нормализация, фильтрация и слияние ссылок подписки."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

LINK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_RI_BASE = 0x1F1E6  # Regional Indicator Symbol Letter A


@dataclass(frozen=True)
class Link:
    raw: str        # нормализованная ссылка, именно она пишется в конфиг
    scheme: str
    userinfo: str   # uuid / user:pass
    host: str
    port: str
    name: str       # раскодированное название после '#'
    flag: str       # эмодзи-флаг или ''
    cc: str         # код страны (NL, US ...) или ''
    city: str
    country: str
    sni: str = ""   # параметры, которые провайдер ротирует
    sid: str = ""

    @property
    def key(self) -> tuple:
        """Ключ «того же сервера». Провайдер ротирует sni/sid и адреса, поэтому
        по названию (город, страна) узнаём сервер, а host/port считаем параметрами.
        У ссылок без названия ключом остаются хост и порт."""
        if self.name:
            return (self.scheme, self.userinfo, self.name)
        return (self.scheme, self.userinfo, self.host, self.port)

    @property
    def id(self) -> str:
        return key_id(self.key)

    def label(self, emoji: bool = True) -> str:
        tag = f"[{self.cc or '??'}]"
        place = ", ".join(x for x in (self.city, self.country) if x) or self.host
        flag = f"{self.flag} " if emoji and self.flag else ""
        return f"{flag}{tag} {place}"


def key_id(key: tuple) -> str:
    """Короткий стабильный идентификатор ссылки (для защищённого списка)."""
    return hashlib.sha1("\x1f".join(key).encode("utf-8")).hexdigest()[:12]


def raw_id(raw: str) -> str:
    link = parse_link(raw)
    return link.id if link else key_id(("raw", raw))


def same_server(link: Link, host: str, port: str, sni: str) -> bool:
    """Совпадает ли ссылка с узлом запущенного sing-box (sni сверяем, если он задан)."""
    return (link.host == host and link.port == str(port)
            and (not link.sni or link.sni == sni))


def normalize(line: str) -> str:
    """Приводит ссылку к виду из конфига podkop: host:port/?query#name."""
    line = line.strip()
    base, hsep, frag = line.partition("#")
    head, qsep, query = base.partition("?")
    idx = head.find("://")
    rest = head[idx + 3:] if idx >= 0 else head
    if qsep and "/" not in rest:
        head += "/"
    return head + qsep + query + hsep + frag


def _split_authority(rest: str) -> tuple[str, str, str]:
    authority = rest.split("/", 1)[0]
    userinfo, _, hostport = authority.rpartition("@")
    if hostport.startswith("["):
        end = hostport.find("]")
        host = hostport[: end + 1]
        port = hostport[end + 2:] if hostport[end + 1: end + 2] == ":" else ""
    else:
        host, sep, port = hostport.rpartition(":")
        if not sep:
            host, port = hostport, ""
    return userinfo, host, port


def _b64(text: str) -> bytes:
    compact = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
    compact += "=" * (-len(compact) % 4)
    return base64.b64decode(compact)


def _parse_vmess(payload: str) -> tuple[str, str, str, str] | None:
    try:
        data = json.loads(_b64(payload).decode("utf-8", "replace"))
        return (str(data.get("id", "")), str(data.get("add", "")),
                str(data.get("port", "")), str(data.get("ps", "")))
    except (ValueError, binascii.Error):
        return None


def _split_name(name: str) -> tuple[str, str, str, str]:
    flag = cc = ""
    s = name.strip()
    if len(s) >= 2 and all(_RI_BASE <= ord(c) <= _RI_BASE + 25 for c in s[:2]):
        flag = s[:2]
        cc = "".join(chr(ord("A") + ord(c) - _RI_BASE) for c in s[:2])
        s = s[2:].strip()
    parts = [p.strip() for p in s.split(",")]
    city = parts[0] if parts else ""
    country = parts[1] if len(parts) > 1 else ""
    return flag, cc, city, country


def parse_link(line: str) -> Link | None:
    line = line.strip()
    if not LINK_RE.match(line):
        return None
    raw = normalize(line)
    head, _, frag = raw.partition("#")
    head = head.partition("?")[0]
    scheme, _, rest = head.partition("://")
    name = unquote(frag)
    if scheme.lower() == "vmess":
        parsed = _parse_vmess(rest)
        if parsed:
            userinfo, host, port, ps = parsed
            name = name or ps
        else:
            userinfo, host, port = "", "", ""
    else:
        userinfo, host, port = _split_authority(rest)
    flag, cc, city, country = _split_name(name)
    query = parse_qs(urlsplit(raw).query)
    sni = (query.get("sni") or query.get("peer") or [""])[0]
    sid = (query.get("sid") or [""])[0]
    return Link(raw, scheme.lower(), userinfo, host, port, name, flag, cc, city, country,
                sni, sid)


def parse_many(lines: list[str]) -> list[Link]:
    out: dict[tuple, Link] = {}
    for line in lines:
        link = parse_link(line)
        if link and link.key not in out:
            out[link.key] = link
    return list(out.values())


def apply_filters(links: list[Link], types: list[str], exclude: list[str],
                  prefer: list[str]) -> list[Link]:
    types_l = {t.lower() for t in types}
    excl = {c.upper() for c in exclude}
    pref = [c.upper() for c in prefer]
    out = [l for l in links
           if (not types_l or l.scheme in types_l) and (l.cc or "??") not in excl]
    idx = {cc: i for i, cc in enumerate(pref)}
    out.sort(key=lambda l: (idx.get(l.cc or "??", len(pref)), l.cc, l.city))
    return out


def countries(links: list[Link]) -> list[tuple[str, str, str, int]]:
    """[(cc, flag, название страны, число ссылок)] по алфавиту кода."""
    info: dict[str, list] = {}
    for l in links:
        rec = info.setdefault(l.cc or "??", [l.flag, l.country, 0])
        rec[2] += 1
        if not rec[1] and l.country:
            rec[1] = l.country
    return [(cc, v[0], v[1], v[2]) for cc, v in sorted(info.items())]


def compute(current: list[str], selected: list[Link], action: str) -> list[str]:
    """Новый список ссылок для секции.

    replace — ровно выбранные; append — старые + новые (старые не трогаем);
    merge — старые, у совпавших по ключу подставлена свежая версия, + новые.
    """
    sel_raw = [l.raw for l in selected]
    if action == "replace":
        return sel_raw
    cur_keys = {}
    for raw in current:
        link = parse_link(raw)
        cur_keys[raw] = link.key if link else ("raw", raw)
    known = set(cur_keys.values())
    fresh = {l.key: l.raw for l in selected}
    if action == "append":
        added = [l.raw for l in selected if l.key not in known]
        return list(current) + added
    if action == "merge":
        merged = [fresh.get(cur_keys[raw], raw) for raw in current]
        merged += [l.raw for l in selected if l.key not in known]
        return merged
    raise ValueError(f"неизвестное действие: {action}")


def diff(old: list[str], new: list[str]) -> dict[str, list[str]]:
    def keyed(items: list[str]) -> dict[tuple, str]:
        res = {}
        for raw in items:
            link = parse_link(raw)
            res[link.key if link else ("raw", raw)] = raw
        return res

    o, n = keyed(old), keyed(new)
    return {
        "added": [n[k] for k in n if k not in o],
        "removed": [o[k] for k in o if k not in n],
        "updated": [n[k] for k in n if k in o and o[k] != n[k]],
        "same": [n[k] for k in n if k in o and o[k] == n[k]],
    }
