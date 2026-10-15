"""Расписание запуска через /etc/cron.d: выражения, описание, ближайший запуск."""
from __future__ import annotations

import os
import re
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from .i18n import _

def day_names() -> list[str]:
    return [_("Sun"), _("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat")]


class CronError(ValueError):
    pass


class ScheduleError(Exception):
    pass


# --- пути (читаем окружение при вызове: так удобнее тестам) ----------------
def cron_path() -> Path:
    return Path(os.environ.get("PODKOP_SUB_CRON_FILE", "/etc/cron.d/podkop-sub"))


def log_path() -> Path:
    return Path(os.environ.get("PODKOP_SUB_LOG", "/var/log/podkop-sub.log"))


def logrotate_path() -> Path:
    return Path(os.environ.get("PODKOP_SUB_LOGROTATE_FILE", "/etc/logrotate.d/podkop-sub"))


# --- разбор cron-выражений -------------------------------------------------
def _field(spec: str, lo: int, hi: int) -> set[int]:
    if not spec:
        raise CronError(_("empty field"))
    values: set[int] = set()
    for part in spec.split(","):
        base, step = part, 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                raise CronError(_("invalid step in «{spec}»").format(spec=spec))
            step = int(step_s)
        if base == "*":
            a, b = lo, hi
        elif re.fullmatch(r"\d+-\d+", base):
            a, b = (int(x) for x in base.split("-"))
        elif re.fullmatch(r"\d+", base) and "/" not in part:
            a = b = int(base)
        else:
            raise CronError(_("field «{spec}» is not supported").format(spec=spec))
        if a < lo or b > hi or a > b:
            raise CronError(_("value out of range {lo}-{hi}: «{spec}»").format(lo=lo, hi=hi, spec=spec))
        values.update(range(a, b + 1, step))
    return values


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(_("need 5 fields: minute hour day month weekday"))
        self.expr = " ".join(parts)
        self.minute = _field(parts[0], 0, 59)
        self.hour = _field(parts[1], 0, 23)
        self.dom = _field(parts[2], 1, 31)
        self.month = _field(parts[3], 1, 12)
        self.dow = {d % 7 for d in _field(parts[4], 0, 7)}
        self._dom_star = parts[2].startswith("*")
        self._dow_star = parts[4].startswith("*")

    def _day_ok(self, t: datetime) -> bool:
        dom_ok = t.day in self.dom
        dow_ok = (t.weekday() + 1) % 7 in self.dow
        if self._dom_star or self._dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # так cron трактует, когда ограничены оба поля

    def next_after(self, moment: datetime) -> datetime | None:
        t = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=370)
        while t < limit:
            if t.month not in self.month:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hour:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minute:
                t += timedelta(minutes=1)
            else:
                return t
        return None


def validate(expr: str) -> str:
    normalized = " ".join(expr.split())
    Cron(normalized)
    return normalized


def next_run(expr: str, now: datetime | None = None) -> datetime | None:
    try:
        return Cron(expr).next_after(now or datetime.now())
    except CronError:
        return None


# --- построители и описание --------------------------------------------------
def every_hour(minute: int) -> str:
    return f"{minute} * * * *"


def every_n_hours(n: int, minute: int) -> str:
    return every_hour(minute) if n == 1 else f"{minute} */{n} * * *"


def daily(hour: int, minute: int) -> str:
    return f"{minute} {hour} * * *"


def weekly(days: list[int], hour: int, minute: int) -> str:
    return f"{minute} {hour} * * {','.join(str(d) for d in sorted(set(days)))}"


def describe(expr: str) -> str:
    try:
        Cron(expr)
    except CronError:
        return _("cron «{expr}»").format(expr=expr)
    m, h, dom, mon, dow = expr.split()
    if dom == "*" and mon == "*" and m.isdigit():
        mm = int(m)
        if dow == "*":
            if h == "*":
                return _("every hour at :{mm:02d}").format(mm=mm)
            if re.fullmatch(r"\*/\d+", h):
                return _("every {n} h at :{mm:02d}").format(n=h[2:], mm=mm)
            if re.fullmatch(r"\d+(,\d+)*", h):
                return _("every day at {times}").format(
                    times=", ".join(f"{int(x):02d}:{mm:02d}" for x in h.split(",")))
        elif h.isdigit() and re.fullmatch(r"[0-7](,[0-7])*", dow):
            names = day_names()
            days = ", ".join(names[int(d) % 7] for d in dow.split(","))
            return _("on {days} at {t}").format(days=days, t=f"{int(h):02d}:{mm:02d}")
    return _("cron «{expr}»").format(expr=expr)


# --- файл расписания ---------------------------------------------------------
def command(config_path: Path | str) -> str:
    exe = os.environ.get("PODKOP_SUB_EXE") or shutil.which("podkop-sub") or "/usr/local/bin/podkop-sub"
    return f"{shlex.quote(exe)} --config {shlex.quote(str(config_path))} --sync"


def render(expr: str, config_path: Path | str, home: str) -> str:
    line = (f"{expr} root {command(config_path)} >> {shlex.quote(str(log_path()))} 2>&1")
    line = line.replace("%", "\\%")  # в cron % означает перевод строки
    return ("# " + _("Managed by podkop-sub (Schedule menu). Do not edit by hand:") + "\n"
            "# " + _("changes will be lost the next time the schedule is enabled or changed.") + "\n"
            "SHELL=/bin/sh\n"
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            f"HOME={home}\n"
            f"{line}\n")


def get_expr() -> str | None:
    try:
        text = cron_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "--sync" not in line:
            continue
        fields = line.split(None, 5)
        if len(fields) >= 6:
            return " ".join(fields[:5])
    return None


def enable(expr: str, config_path: Path | str, home: str | None = None) -> Path:
    expr = validate(expr)
    path = cron_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(render(expr, config_path, home or str(Path.home())), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        rot = logrotate_path()
        if rot.parent.is_dir():
            rot.write_text(f"{log_path()} {{\n    weekly\n    rotate 4\n    compress\n"
                           "    missingok\n    notifempty\n    copytruncate\n}\n", encoding="utf-8")
    except PermissionError as exc:
        raise ScheduleError(_("no permission to write /etc/cron.d — run podkop-sub as root")) from exc
    except OSError as exc:
        raise ScheduleError(_("could not write the schedule: {e}").format(e=exc)) from exc
    return path


def disable() -> bool:
    removed = False
    for path in (cron_path(), logrotate_path()):
        try:
            path.unlink()
            removed = removed or path == cron_path()
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise ScheduleError(_("no permission to delete the schedule file — run as root")) from exc
    return removed


def cron_installed() -> bool:
    return bool(shutil.which("cron") or shutil.which("crond")
                or Path("/usr/sbin/cron").exists() or os.environ.get("PODKOP_SUB_CRON_FILE"))


def read_log(lines: int = 40) -> str:
    try:
        data = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])
