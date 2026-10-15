"""Состояние между запусками (для cron): когда запускали, чем кончилось, кулдауны."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "last_run": 0,           # unix-время последнего запуска sync
    "last_status": "",       # ok | changed | error
    "last_message": "",
    "fetch_failures": 0,     # подряд неудачных скачиваний подписки
    "replaced_at": {},       # id ссылки -> unix-время последней замены
}


class State:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def state_for(cfg) -> State:
    """Файл состояния лежит рядом с бэкапами (так он изолирован вместе с конфигом)."""
    return State(Path(os.path.expanduser(cfg["backup"]["dir"])).parent / "state.json")
