"""Конфиг приложения (TOML, права 600)."""
from __future__ import annotations

import copy
import json
import os
import tomllib
from .i18n import _
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "podkop-sub"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS: dict = {
    # подписки: [[subscriptions]] name / url / enabled; все попадают в одну секцию podkop
    "subscriptions": [],
    "fetch": {"user_agent": "podkop-sub/1.0", "timeout": 20},
    "router": {
        "host": "",
        "port": 22,
        "user": "root",
        "key_path": str(CONFIG_DIR / "id_ed25519"),
        "section": "Proxy",
    },
    "filters": {"types": ["vless"], "exclude_countries": [], "prefer_countries": []},
    "update": {"action": "replace"},
    "ui": {"show_emoji": True, "language": "ru"},
    "backup": {
        "dir": str(Path.home() / ".local" / "share" / "podkop-sub" / "backups"),
        "keep": 20,
    },
    # ссылки, которые скрипт не трогает (формат строки: "<id> <описание>")
    "protect": {"links": [], "reviewed": False},
    "sync": {
        "remove_missing": True,       # удалять ссылки, которых больше нет в подписке
        "refresh_cooldown_min": 90,   # одну мёртвую ссылку заменяем не чаще, чем раз в N минут
        "min_links": 0,               # 0 — защита от «куцей» подписки выключена
        "latency_timeout_ms": 3000,
        "latency_attempts": 2,
        "fetch_attempts": 3,
        "probe": "full",              # проверка новых ссылок до записи: off | tcp | full (TCP + Reality)
        "probe_max": 20,              # сколько кандидатов проверяем за один запуск
        "probe_timeout_sec": 15,
        "singbox_bin": "/usr/local/bin/sing-box",
    },
    "apply": {"method": "reload", "fallback_restart": True, "wait_sec": 3},
    "notify": {
        "telegram_token": "",
        "telegram_chat_id": "",
        "on_change": True,
        "on_error": True,
        "error_threshold": 3,
    },
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def dumps(cfg: dict) -> str:
    out = []
    for section, values in cfg.items():
        if isinstance(values, list):  # массив таблиц: [[subscriptions]]
            for item in values:
                out.append(f"[[{section}]]")
                out.extend(f"{k} = {_val(v)}" for k, v in item.items())
                out.append("")
            continue
        out.append(f"[{section}]")
        out.extend(f"{k} = {_val(v)}" for k, v in values.items())
        out.append("")
    return "\n".join(out)


class Config:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.data = copy.deepcopy(DEFAULTS)
        self.exists = False
        self.migrated = False
        if self.path.is_file():
            with open(self.path, "rb") as fh:
                raw = tomllib.load(fh)
            _merge(self.data, raw)
            self._migrate(raw)
            self.exists = True

    def _migrate(self, raw: dict) -> None:
        """Старый конфиг с одной подпиской ([subscription]) превращаем в список из одного источника."""
        legacy = raw.get("subscription")
        if not isinstance(legacy, dict):
            return
        if "subscriptions" not in raw and legacy.get("url"):
            self.data["subscriptions"] = [{"name": "main", "url": str(legacy["url"]), "enabled": True}]
        for key in ("user_agent", "timeout"):
            if key in legacy:
                self.data["fetch"][key] = legacy[key]
        self.data.pop("subscription", None)
        self.migrated = True

    def __getitem__(self, section: str) -> dict:
        return self.data[section]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dumps(self.data))
        os.replace(tmp, self.path)
        self.exists = True

    @property
    def subscriptions(self) -> list[dict]:
        return self.data["subscriptions"]

    @property
    def configured(self) -> bool:
        has_url = any(s.get("url") for s in self.subscriptions)
        return self.exists and has_url and bool(self.data["router"]["host"])

    def expand(self, section: str, key: str) -> Path:
        return Path(os.path.expanduser(self.data[section][key]))
