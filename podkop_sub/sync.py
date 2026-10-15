"""Синхронизация секции podkop с подпиской.

Что считается «неактуальным»:
  * сервера нет в подписке, из которой он взят, -> удаляем (если включено remove_missing);
    ссылки без владельца (добавлены вручную) и ссылки подписок, которые не скачались, не трогаем;
  * сервер новый            -> добавляем;
  * ссылка не прошла тест задержки -> заменяем свежей версией из подписки
    (новые sni/sid), не чаще раза в refresh_cooldown_min минут;
  * живые ссылки и защищённые ссылки не трогаем.
Различия только в sni/sid у доступных ссылок изменением не считаются.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .links import Link, parse_link, raw_id, same_server
from .i18n import _
from .probe import Prober
from .router import Router, RouterError
from .sources import Fetched, reconcile_owners


# --- защищённый список ----------------------------------------------------
def protected_ids(cfg) -> set[str]:
    return {s.split()[0] for s in cfg["protect"]["links"] if s.strip()}


def label_of(raw: str, emoji: bool = True) -> str:
    link = parse_link(raw)
    return link.label(emoji) if link else raw[:70]


def protect_record(raw: str) -> str:
    link = parse_link(raw)
    where = f" {link.host}:{link.port}" if link else ""
    return f"{raw_id(raw)} {label_of(raw, False)}{where}"


# --- модель ---------------------------------------------------------------
@dataclass
class Entry:
    raw: str
    link: Link | None
    id: str
    protected: bool
    tag: str | None = None     # узел в запущенном sing-box (Proxy-N-out)
    ms: int | None = None      # >0 жив, 0 не ответил, None не проверялся

    @property
    def alive(self) -> bool:
        return bool(self.ms)

    @property
    def dead(self) -> bool:
        return self.ms == 0


@dataclass
class Plan:
    new: list[str]
    added: list[Link] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refreshed: list[tuple[str, str]] = field(default_factory=list)
    cooldown: list[str] = field(default_factory=list)
    switch_to: str | None = None
    liveness: bool = False
    rejected: list[str] = field(default_factory=list)   # кандидаты из подписки, не прошли проверку

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.refreshed)


def make_entries(current: list[str], protected: set[str], running: list[dict]) -> list[Entry]:
    entries, used = [], set()
    for raw in current:
        link = parse_link(raw)
        rid = raw_id(raw)
        entry = Entry(raw, link, rid, rid in protected)
        if link:
            for ob in running:
                if ob["tag"] not in used and same_server(link, ob["server"], ob["port"], ob["sni"]):
                    entry.tag = ob["tag"]
                    used.add(ob["tag"])
                    break
        entries.append(entry)
    return entries


def inspect(cfg, router: Router, log: Callable[[str], None] | None = None):
    """Читает роутер: ссылки, узлы sing-box, задержки. -> (entries, активный тег)."""
    sync = cfg["sync"]
    current = router.get_links()
    try:
        running = router.running_outbounds()
    except RouterError as exc:
        running = []
        if log:
            log(_("Warning: availability check is unavailable ({e})").format(e=exc))
    entries = make_entries(current, protected_ids(cfg), running)
    tags = [e.tag for e in entries if e.tag]
    active = None
    if tags:
        lat = router.latencies(tags, sync["latency_timeout_ms"], sync["latency_attempts"])
        for e in entries:
            if e.tag:
                e.ms = lat.get(e.tag)
        active = router.active_tag()
    elif current and log:
        log(_("Warning: links could not be matched with the running sing-box, "
            "availability check skipped (changes may not be applied yet)."))
    return entries, active


def format_entry(e: Entry, active_tag: str | None, emoji: bool = True) -> str:
    mark = "▶" if e.tag and e.tag == active_tag else " "
    lock = ("🔒" if emoji else "[P]") if e.protected else ("  " if emoji else "   ")
    if e.ms:
        state = _("{ms} ms").format(ms=e.ms)
    elif e.ms == 0:
        state = "N/A"
    elif e.tag:
        state = "?"
    else:
        state = _("not running (waiting to be applied)")
    return f"{mark} {lock} {label_of(e.raw, emoji)}  {state}"


def build_plan(entries: list[Entry], sub_links: list[Link], *, prefer: list[str],
               replaced_at: dict, now: float, cooldown_s: float, remove_missing: bool,
               active_tag: str | None, alive: Callable[[Link], bool] | None = None,
               owners: dict | None = None, ok_sources: set[str] | None = None) -> Plan:
    """owners: id ссылки -> подписка; ok_sources: подписки, которые скачались в этот запуск.
    Удаляем только ссылку, чей владелец скачался и больше её не отдаёт."""
    owners = owners or {}
    ok_sources = set(ok_sources or ())
    sub_by_key = {l.key: l for l in sub_links}
    cur_keys = {e.link.key for e in entries if e.link}
    plan = Plan(new=[])
    plan.liveness = any(e.ms is not None for e in entries)
    for e in entries:
        if e.protected:
            plan.new.append(e.raw)
            continue
        fresh = sub_by_key.get(e.link.key) if e.link else None
        if fresh is None:
            owner = owners.get(e.link.id) if e.link else None
            if remove_missing and owner in ok_sources:
                plan.removed.append(e.raw)
            else:
                plan.new.append(e.raw)
            continue
        if e.dead and fresh.raw != e.raw:
            # сначала проверка кандидата, потом cooldown: иначе мёртвые ссылки в cooldown
            # вообще не проверялись бы
            if alive is not None and not alive(fresh):
                plan.rejected.append(fresh.raw)  # свежая версия тоже не отвечает: оставляем старую
                plan.new.append(e.raw)
            elif now - float(replaced_at.get(e.id, 0)) < cooldown_s:
                plan.cooldown.append(e.raw)
                plan.new.append(e.raw)
            else:
                plan.refreshed.append((e.raw, fresh.raw))
                plan.new.append(fresh.raw)
        else:
            plan.new.append(e.raw)
    # новые ссылки добавляем так же, как ручное меню по умолчанию: только страны-предпочтения
    # (если предпочтения не заданы — все, что прошло фильтры)
    pref_set = {c.upper() for c in prefer}
    candidates = [l for l in sub_links if l.key not in cur_keys
                  and (not pref_set or (l.cc or "??") in pref_set)]
    plan.added = []
    for l in candidates:
        if alive is None or alive(l):
            plan.added.append(l)
        else:
            plan.rejected.append(l.raw)
    plan.new += [l.raw for l in plan.added]

    active = next((e for e in entries if e.tag and e.tag == active_tag), None)
    if active is not None and active.dead:
        others = [e for e in entries if e.alive and e.tag != active_tag]
        if others:
            pref = {c.upper(): i for i, c in enumerate(prefer)}
            others.sort(key=lambda e: (pref.get(e.link.cc if e.link else "", len(pref)), e.ms))
            plan.switch_to = others[0].tag
    return plan


# --- применение -----------------------------------------------------------
def verify_applied(router: Router, new_raws: list[str]) -> bool:
    """Запущенный sing-box работает именно с тем списком, который мы записали."""
    running = router.running_outbounds()
    links = [parse_link(r) for r in new_raws]
    if any(l is None for l in links):
        return False
    for l in links:
        if not any(same_server(l, o["server"], o["port"], o["sni"]) for o in running):
            return False
    for o in running:
        if not any(same_server(l, o["server"], o["port"], o["sni"]) for l in links):
            return False
    return True


def wait_running(router: Router, sleep: Callable[[float], None] = time.sleep,
                 tries: int = 15, pause: float = 2.0) -> bool:
    for _ in range(tries):
        try:
            if router.sing_box_running():
                return True
        except RouterError:
            pass
        sleep(pause)
    return False


def apply_and_verify(router: Router, cfg, new_raws: list[str], log: Callable[[str], None],
                     sleep: Callable[[float], None] = time.sleep) -> tuple[str | None, bool]:
    """reload (быстрее), проверка, при неудаче restart. -> (чем применено, всё ли хорошо)."""
    method = cfg["apply"]["method"]
    order = [method]
    if method == "reload" and cfg["apply"]["fallback_restart"]:
        order.append("restart")
    used = None
    for m in order:
        log(_("Applying changes: podkop {m}…").format(m=m))
        router.service(m)
        sleep(cfg["apply"]["wait_sec"])
        ok = False
        for attempt in range(3):  # restart у procd асинхронный: даём время дозапуститься
            try:
                ok = verify_applied(router, new_raws)
            except RouterError as exc:
                log(_("Could not verify the result: {e}").format(e=exc))
                ok = False
            if ok:
                break
            if attempt < 2:
                sleep(2)
        if ok:
            used = m
            break
        log(_("After {m}, the running config does not match the written list.").format(m=m))
    if used is None:
        return None, False
    if not wait_running(router, sleep):
        log(_("sing-box did not start after applying."))
        return used, False
    return used, True


# --- блокировка и запуск --------------------------------------------------
class RunLock:
    """Защита от одновременного запуска (cron + ручной запуск)."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # не Linux: блокировки нет
            self.acquired = True
            return self
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            self._fh.close()
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            self._fh.close()


def _stamp(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run_sync(cfg, router: Router, state, notifier, fetch_fn: Callable[[], Fetched], *,
             dry_run: bool = False, sleep: Callable[[float], None] = time.sleep,
             now: float | None = None) -> int:
    """Код возврата: 0 — ок (в том числе «изменений нет»), 1 — ошибка подписки/роутера,
    2 — записано, но применить не удалось, 3 — не настроен защищённый список."""
    log = _stamp
    ts = time.time() if now is None else now
    sync = cfg["sync"]
    emoji = cfg["ui"]["show_emoji"]

    def finish(status: str, message: str, code: int) -> int:
        if not dry_run:
            state["last_run"], state["last_status"], state["last_message"] = ts, status, message
            state.save()
        return code

    def fail(message: str, code: int = 1, notify: bool = True) -> int:
        log(_("ERROR: {m}").format(m=message))
        if notify:
            notifier.send(f"⚠️ podkop-sub: {message}", "error")
        return finish("error", message, code)

    if not cfg["protect"]["reviewed"]:
        log(_("The protected list is not set up: the script does not know which links must not be touched. "
            "Open podkop-sub → Settings → Protected links and save your choice."))
        return 3

    # 1. подписки: повторяем только если не скачалась ни одна
    fetched = Fetched()
    attempts = max(1, int(sync["fetch_attempts"]))
    for attempt in range(1, attempts + 1):
        fetched = fetch_fn()
        if fetched.ok:
            break
        if attempt < attempts:
            sleep(10)
    if not fetched.ok:
        state["fetch_failures"] = int(state["fetch_failures"]) + 1
        threshold = int(cfg["notify"]["error_threshold"])
        detail = "; ".join(f"{n}: {e}" for n, e in fetched.errors.items()) or _("no enabled subscriptions")
        return fail(_("subscription unavailable ({e}); consecutive failures: {n}").format(
            e=detail, n=state['fetch_failures']),
                    notify=state["fetch_failures"] == threshold)
    state["fetch_failures"] = 0
    for name, err in fetched.errors.items():
        log(_("Subscription «{name}» unavailable ({e}): its links are left as they are.").format(
            name=name, e=err))
    links = fetched.links
    configured = {s["name"] for s in cfg["subscriptions"]}
    owners_before = dict(state["owners"])
    if not links:
        return fail(_("no links left after filters, nothing changed"))
    if sync["min_links"] and len(links) < int(sync["min_links"]):
        return fail(_("the subscription has {n} links, below the minimum {m}; nothing changed").format(
                    n=len(links), m=sync['min_links']))

    # 2. роутер
    try:
        entries, active = inspect(cfg, router, log)
    except RouterError as exc:
        return fail(_("router: {e}").format(e=exc))
    alive = sum(1 for e in entries if e.alive)
    dead = sum(1 for e in entries if e.dead)
    log(_("Router: links {n} (protected {p}), available {a}, not responding {d}, not checked {r}. "
          "In subscription: {s}.").format(n=len(entries), p=sum(e.protected for e in entries),
                                          a=alive, d=dead, r=len(entries) - alive - dead, s=len(links)))

    prober = Prober(sync.get("probe", "full"), sync.get("singbox_bin", ""),
                    float(sync.get("probe_timeout_sec", 15)), int(sync.get("probe_max", 20)), log)
    # при включённой проверке cooldown не нужен: заменяем только на проверенную ссылку
    cooldown_s = 0.0 if prober.mode != "off" else float(sync["refresh_cooldown_min"]) * 60
    plan = build_plan(entries, links, prefer=cfg["filters"]["prefer_countries"],
                      replaced_at=state["replaced_at"], now=ts,
                      cooldown_s=cooldown_s,
                      remove_missing=bool(sync["remove_missing"]), active_tag=active,
                      alive=prober, owners=owners_before, ok_sources=set(fetched.ok))
    for raw in plan.rejected:
        log(_("  ✗ {label} — {why}, not added").format(label=label_of(raw, emoji),
                                             why=prober.notes.get(raw, _("failed the check"))))
    if prober.deferred:
        log(_("The per-run check limit is reached: unchecked links are skipped until next time."))
    for l in plan.added:
        log(f"  + {l.label(emoji)}")
    for old, _new in plan.refreshed:
        log(_("  ~ {label} (not responding, taking fresh parameters)").format(label=label_of(old, emoji)))
    for raw in plan.removed:
        log(_("  - {label} (not in subscription)").format(label=label_of(raw, emoji)))
    for raw in plan.cooldown:
        log(_("  · {label} not responding, but replaced recently — waiting").format(label=label_of(raw, emoji)))

    if not plan.changed and not plan.switch_to:
        state["owners"] = reconcile_owners(plan.new, owners_before, fetched.owner_of, configured)
        log(_("No changes."))
        return finish("ok", _("no changes"), 0)
    if dry_run:
        log(_("Dry run: nothing changed on the router."))
        return 0

    # 3. переключение активного узла без перезапуска
    switched = ""
    if plan.switch_to and not plan.changed:
        state["owners"] = reconcile_owners(plan.new, owners_before, fetched.owner_of, configured)
        target = next((e for e in entries if e.tag == plan.switch_to), None)
        name = label_of(target.raw, emoji) if target else plan.switch_to
        if router.switch_active(plan.switch_to):
            switched = _("the active node did not respond, switched to {name}").format(name=name)
            log(switched)
            notifier.send(f"🔀 podkop-sub: {switched}", "change")
        else:
            return fail(_("could not switch the active node to {name}").format(name=name))
        return finish("changed", switched, 0)

    # 4. запись и применение
    try:
        backup = router.backup(cfg["backup"]["dir"], cfg["backup"]["keep"])
        log(_("Backup: {path}").format(path=backup))
        router.set_links(plan.new)
        if router.get_links() != plan.new:
            return fail(_("after writing, the list on the router differs from the expected one"), 2)
    except RouterError as exc:
        return fail(_("writing to the router: {e}").format(e=exc))
    state["owners"] = reconcile_owners(plan.new, owners_before, fetched.owner_of, configured)
    for old, _new in plan.refreshed:
        state["replaced_at"][raw_id(old)] = ts
    keep = {raw_id(r) for r in plan.new}
    state["replaced_at"] = {k: v for k, v in state["replaced_at"].items() if k in keep}

    try:
        method, ok = apply_and_verify(router, cfg, plan.new, log, sleep)
    except RouterError as exc:
        return fail(_("applying: {e}").format(e=exc), 2)
    if not ok:
        return fail(_("the list was written, but could not be applied: check podkop manually "
                          "(Podkop → Status / Log)"), 2)
    log(_("Applied via {method}.").format(method=method))

    # 5. что получилось
    result = ""
    try:
        entries2, active2 = inspect(cfg, router)
        a2 = sum(1 for e in entries2 if e.alive)
        t2 = sum(1 for e in entries2 if e.ms is not None)
        result = _("available {a} of {t}").format(a=a2, t=t2)
        current = next((e for e in entries2 if e.tag and e.tag == active2), None)
        if current is not None and current.dead:
            plan2 = build_plan(entries2, [], prefer=cfg["filters"]["prefer_countries"],
                               replaced_at={}, now=ts, cooldown_s=0, remove_missing=False,
                               active_tag=active2)
            if plan2.switch_to and router.switch_active(plan2.switch_to):
                log(_("The active node did not respond, switched to an available one."))
    except RouterError:
        pass
    if result:
        log(_("After applying: {r}.").format(r=result))

    lines = [_("🔄 podkop-sub: list updated (applied via {method})").format(method=method)]
    lines += [f"➕ {l.label(emoji)}" for l in plan.added]
    lines += [f"♻️ {label_of(o, emoji)}" for o, _ in plan.refreshed]
    lines += [f"➖ {label_of(r, emoji)}" for r in plan.removed]
    if result:
        lines.append(result.capitalize())
    notifier.send("\n".join(lines), "change")
    return finish("changed", f"+{len(plan.added)} ~{len(plan.refreshed)} -{len(plan.removed)}, "
                  f"{method}" + (f", {result}" if result else ""), 0)
