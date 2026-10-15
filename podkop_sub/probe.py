"""Проверка ссылок из подписки ДО записи на роутер (в LXC, тем же выходом, что и роутер).

1. TCP: открывается ли порт сервера (быстро, без зависимостей).
2. Reality: поднимается временный sing-box с одним этим узлом и через него
   запрашивается generate_204. Ловит устаревшие sni/sid. Нужен бинарник sing-box.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .links import Link
from .i18n import _

PROBE_URL = "http://www.gstatic.com/generate_204"


def tcp_ok(host: str, port: str | int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def outbound_from_link(link: Link) -> dict | None:
    """Outbound для sing-box. None — ссылка не VLESS+Reality+TCP, такую не умеем проверять."""
    if link.scheme != "vless":
        return None
    q = {k: v[0] for k, v in parse_qs(urlsplit(link.raw).query).items()}
    if q.get("security") != "reality" or q.get("type", "tcp") != "tcp":
        return None
    ob = {"type": "vless", "tag": "probe", "server": link.host,
          "server_port": int(link.port), "uuid": link.userinfo}
    if q.get("flow"):
        ob["flow"] = q["flow"]
    ob["tls"] = {"enabled": True, "server_name": q.get("sni", ""),
                 "utls": {"enabled": True, "fingerprint": q.get("fp", "chrome")},
                 "reality": {"enabled": True, "public_key": q.get("pbk", ""),
                             "short_id": q.get("sid", "")}}
    return ob


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if tcp_ok("127.0.0.1", port, 0.5):
            return True
        time.sleep(0.2)
    return False


def reality_ok(link: Link, binary: str, timeout: float = 15.0) -> bool | None:
    """True/False — результат; None — проверка невозможна (нет бинарника или не тот тип ссылки)."""
    ob = outbound_from_link(link)
    if ob is None or not binary or not os.path.exists(binary):
        return None
    port = _free_port()
    conf = {"log": {"disabled": True},
            "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [ob]}
    with tempfile.TemporaryDirectory(prefix="podkop-probe-") as d:
        path = Path(d) / "probe.json"
        path.write_text(json.dumps(conf), encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            proc = subprocess.Popen([binary, "run", "-c", str(path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        try:
            if not _wait_port(port, 5.0):
                return False
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": f"http://127.0.0.1:{port}"}))
            try:
                with opener.open(PROBE_URL, timeout=timeout) as resp:
                    return resp.status in (200, 204)
            except (urllib.error.URLError, OSError, ValueError):
                return False
        finally:
            proc.kill()
            proc.wait()


class Prober:
    """Проверка кандидатов с кэшем и лимитом на один запуск."""

    def __init__(self, mode: str, binary: str, timeout: float, max_probes: int, log=None):
        self.mode = mode            # off | tcp | full
        self.binary = binary
        self.timeout = timeout
        self.left = max_probes
        self.log = log or (lambda m: None)
        self.cache: dict[str, bool] = {}
        self.notes: dict[str, str] = {}
        self._warned = False

    def __call__(self, link: Link) -> bool:
        if self.mode == "off":
            return True
        if link.raw in self.cache:
            return self.cache[link.raw]
        if self.left <= 0:
            return False  # лимит на запуск исчерпан: отложим до следующего раза
        self.left -= 1
        ok, why = self._check(link)
        self.cache[link.raw] = ok
        self.notes[link.raw] = why
        return ok

    def _check(self, link: Link) -> tuple[bool, str]:
        if not tcp_ok(link.host, link.port):
            return False, _("port does not open")
        if self.mode != "full":
            return True, _("TCP ok")
        res = reality_ok(link, self.binary, self.timeout)
        if res is None:
            if not self._warned:
                self.log(_("Warning: sing-box not found, the full Reality check is skipped "
                           "(needs {b}); only TCP was checked.").format(b=self.binary))
                self._warned = True
            return True, _("TCP ok (Reality not checked)")
        return (True, _("Reality ok")) if res else (False, _("Reality does not respond"))

    @property
    def deferred(self) -> bool:
        return self.left <= 0
