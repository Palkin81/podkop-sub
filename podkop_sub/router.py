"""Работа с роутером OpenWrt по SSH: чтение и запись списка ссылок через uci."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from .i18n import _

NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RouterError(Exception):
    pass


def parse_status(text: str) -> dict | None:
    """Разбирает JSON из `podkop get_sing_box_status`."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Router:
    def __init__(self, host: str, port: int = 22, user: str = "root",
                 key_path: str | Path = "", section: str = "Proxy",
                 timeout: int = 30, ssh_bin: str | None = None,
                 config_file: str | None = None):
        if not NAME_RE.match(section):
            raise RouterError(_("invalid section name: {n!r}").format(n=section))
        self.host, self.port, self.user = host, int(port), user
        self.key_path = str(Path(os.path.expanduser(str(key_path)))) if key_path else ""
        self.section = section
        self.timeout = timeout
        # переменные окружения нужны только тестам
        self.ssh_bin = ssh_bin or os.environ.get("PODKOP_SUB_SSH", "ssh")
        self.config_file = config_file or os.environ.get(
            "PODKOP_SUB_CONFIG_FILE", "/etc/config/podkop")
        self.podkop_bin = os.environ.get("PODKOP_SUB_BIN", "/usr/bin/podkop")
        self.initd = os.environ.get("PODKOP_SUB_INITD", "/etc/init.d/podkop")
        self.singbox_config = os.environ.get("PODKOP_SUB_SINGBOX_CONFIG", "")

    # --- низкий уровень -------------------------------------------------
    def _ssh(self, remote_cmd: str, stdin: str | None = None, check: bool = True,
             timeout: float | None = None) -> str:
        cmd = [self.ssh_bin, "-p", str(self.port),
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new"]
        if self.key_path:
            cmd += ["-i", self.key_path, "-o", "IdentitiesOnly=yes"]
        cmd += [f"{self.user}@{self.host}", remote_cmd]
        try:
            res = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                                 timeout=timeout or self.timeout)
        except FileNotFoundError as exc:
            raise RouterError(_("ssh not found (apt install openssh-client)")) from exc
        except subprocess.TimeoutExpired as exc:
            raise RouterError(_("SSH connection timed out")) from exc
        if check and res.returncode != 0:
            msg = (res.stderr or res.stdout).strip() or _("exit code {c}").format(c=res.returncode)
            raise RouterError(f"SSH/uci: {msg}")
        return res.stdout

    # --- чтение ---------------------------------------------------------
    def test(self) -> None:
        self._ssh("uci -q show podkop >/dev/null && echo ok")

    def sections(self) -> list[str]:
        out = self._ssh("uci -q show podkop")
        names = []
        for line in out.splitlines():
            m = re.match(r"^podkop\.([A-Za-z0-9_]+)=section$", line.strip())
            if m:
                names.append(m.group(1))
        return names

    def get_links(self) -> list[str]:
        out = self._ssh(f"uci -q show podkop.{self.section}.selector_proxy_links", check=False)
        out = out.strip()
        if "=" not in out:
            return []
        return shlex.split(out.split("=", 1)[1])

    # --- запись ---------------------------------------------------------
    def set_links(self, links: list[str]) -> None:
        sec = f"podkop.{self.section}.selector_proxy_links"
        lines = ["set -e", "trap 'uci revert podkop' EXIT",
                 f"uci -q delete {sec} || true"]
        lines += [f"uci add_list {sec}={shlex.quote(l)}" for l in links]
        lines += ["uci commit podkop", "trap - EXIT"]
        self._ssh("sh -s", stdin="\n".join(lines) + "\n")

    # --- бэкапы ---------------------------------------------------------
    def backup(self, directory: str | Path, keep: int = 20) -> Path:
        content = self._ssh(f"cat {shlex.quote(self.config_file)}")
        if not content.strip():
            raise RouterError(_("the podkop config file is empty, no backup was made"))
        directory = Path(os.path.expanduser(str(directory)))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"podkop-{datetime.now():%Y%m%d-%H%M%S-%f}.conf"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        old = sorted(directory.glob("podkop-*.conf"))[:-keep] if keep > 0 else []
        for item in old:
            item.unlink(missing_ok=True)
        return path

    @staticmethod
    def list_backups(directory: str | Path) -> list[Path]:
        directory = Path(os.path.expanduser(str(directory)))
        return sorted(directory.glob("podkop-*.conf"), reverse=True)

    def restore(self, backup: Path) -> None:
        content = Path(backup).read_text(encoding="utf-8")
        if "config " not in content:
            raise RouterError(_("the backup file does not look like a podkop config"))
        tmp = "/tmp/podkop.restore"
        cmd = (f"cat > {tmp} && cp {tmp} {shlex.quote(self.config_file)} && rm -f {tmp}")
        self._ssh(cmd, stdin=content)

    # --- сервис ---------------------------------------------------------
    def sing_box_status(self) -> dict | None:
        """Состояние sing-box по данным самого podkop (так же, как в LuCI).

        `/etc/init.d/podkop status` для этого не годится: init-скрипт podkop
        запускает короткоживущую команду, и procd всегда считает его неактивным.
        """
        out = self._ssh(f"{shlex.quote(self.podkop_bin)} get_sing_box_status 2>/dev/null",
                        check=False)
        return parse_status(out)

    def sing_box_process_running(self) -> bool:
        out = self._ssh("pgrep sing-box >/dev/null 2>&1 && echo yes || echo no", check=False)
        return out.strip() == "yes"

    def sing_box_running(self) -> bool:
        st = self.sing_box_status()
        if st is not None:
            return str(st.get("running", "")).strip().lower() in ("1", "true", "yes")
        return self.sing_box_process_running()

    def service(self, action: str) -> str:
        if action not in {"restart", "reload"}:
            raise RouterError(_("invalid action"))
        return self._ssh(f"{shlex.quote(self.initd)} {action} 2>&1", check=False, timeout=120)

    # --- запущенный sing-box и Clash API --------------------------------
    @property
    def group_tag(self) -> str:
        return f"{self.section}-out"

    def _singbox_config_path(self) -> str:
        if self.singbox_config:
            return self.singbox_config
        out = self._ssh("uci -q get podkop.settings.config_path", check=False).strip()
        return out if re.match(r"^[A-Za-z0-9_./-]+$", out) else "/etc/sing-box/config.json"

    def running_outbounds(self) -> list[dict]:
        """Узлы секции в конфиге, с которым реально работает sing-box.

        jq на роутерах OpenWrt часто собран без регулярных выражений, поэтому
        JSON читаем здесь. Теги узлов — Proxy-1-out, Proxy-2-out… (по номеру).
        """
        text = self._ssh(f"cat {shlex.quote(self._singbox_config_path())}")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RouterError(_("could not parse the sing-box config: {e}").format(e=exc)) from exc
        prefix = f"{self.section}-"
        result = []
        for ob in data.get("outbounds", []):
            tag = str(ob.get("tag", ""))
            if not (tag.startswith(prefix) and tag.endswith("-out")):
                continue
            if not tag[len(prefix):-len("-out")].isdigit():
                continue
            tls = ob.get("tls") or {}
            reality = tls.get("reality") or {}
            result.append({"tag": tag, "type": ob.get("type", ""),
                           "server": str(ob.get("server", "")),
                           "port": str(ob.get("server_port", "")),
                           "sni": str(tls.get("server_name", "")),
                           "sid": str(reality.get("short_id", ""))})
        return result

    def _clash(self, *args: str, timeout: float | None = None) -> str:
        for a in args:
            if not TAG_RE.match(a):
                raise RouterError(_("invalid argument: {a!r}").format(a=a))
        cmd = " ".join([shlex.quote(self.podkop_bin), "clash_api", *args])
        return self._ssh(f"{cmd} 2>/dev/null", check=False, timeout=timeout)

    def active_tag(self) -> str | None:
        """Какой узел сейчас выбран в группе секции (поле `now` у Selector)."""
        try:
            data = json.loads(self._clash("get_proxies"))
        except ValueError:
            return None
        group = (data.get("proxies") or {}).get(self.group_tag) or {}
        return group.get("now") or None

    def _latency_once(self, tags: list[str], timeout_ms: int) -> dict[str, int | None]:
        """Тест задержки узлов параллельно. int>0 — жив, 0 — не ответил, None — тест не удался."""
        for t in tags:
            if not TAG_RE.match(t):
                raise RouterError(_("invalid tag: {t!r}").format(t=t))
        podkop = shlex.quote(self.podkop_bin)
        script = "\n".join(
            [f"for t in {' '.join(shlex.quote(t) for t in tags)}; do",
             f"  ( r=$({podkop} clash_api get_proxy_latency \"$t\" {int(timeout_ms)} "
             "2>/dev/null | tr -d ' \\n\\r\\t'); printf '%s\\t%s\\n' \"$t\" \"$r\" ) &",
             "done", "wait", ""])
        out = self._ssh("sh -s", stdin=script, check=False,
                        timeout=timeout_ms / 1000 + 30)
        result: dict[str, int | None] = {t: None for t in tags}
        for line in out.splitlines():
            tag, _, body = line.partition("\t")
            if tag not in result:
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue  # пусто или мусор: тест не удался, это не «мёртв»
            if not isinstance(data, dict):
                continue
            delay = data.get("delay")
            result[tag] = int(delay) if isinstance(delay, (int, float)) and delay > 0 else 0
        return result

    def latencies(self, tags: list[str], timeout_ms: int = 3000,
                  attempts: int = 2) -> dict[str, int | None]:
        """Задержка каждого узла: жив если хоть одна попытка успешна."""
        best: dict[str, int | None] = {t: None for t in tags}
        pending = list(tags)
        for _ in range(max(1, attempts)):
            if not pending:
                break
            for tag, ms in self._latency_once(pending, timeout_ms).items():
                if ms and ms > 0:
                    best[tag] = ms
                elif ms == 0 and best[tag] is None:
                    best[tag] = 0
            pending = [t for t in pending if not best[t]]
        return best

    def switch_active(self, tag: str) -> bool:
        out = self._clash("set_group_proxy", self.group_tag, tag)
        try:
            return bool(json.loads(out).get("success"))
        except (ValueError, AttributeError):
            return False

    def logs(self, lines: int = 40) -> str:
        return self._ssh(
            f"logread 2>/dev/null | grep -E 'podkop|sing-box' | tail -n {int(lines)}",
            check=False)
