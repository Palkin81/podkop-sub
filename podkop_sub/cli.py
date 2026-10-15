"""Menu, setup wizard and command line for podkop-sub."""
from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import schedule as sched
from .config import Config
from .help import help_text
from .i18n import LANGUAGES, _, set_language
from .links import Link, apply_filters, compute, countries, diff, parse_link, raw_id
from .notify import Notifier
from .router import Router, RouterError
from .state import state_for
from .subscription import Subscription, SubscriptionError, describe_meta, fetch
from .sync import (RunLock, apply_and_verify, format_entry, inspect, label_of,
                   protect_record, protected_ids, run_sync)

HANDLED = (SubscriptionError, RouterError, sched.ScheduleError, sched.CronError)


def actions() -> dict[str, str]:
    return {
        "replace": _("Replace everything with the selection (old links are removed)"),
        "append": _("Add new links (keep old ones untouched)"),
        "merge": _("Merge (update matching, add new, keep the rest)"),
    }


def probe_labels() -> dict[str, str]:
    return {"full": _("TCP + Reality"), "tcp": _("TCP only"), "off": _("disabled")}


def yes_no(value: bool) -> str:
    return _("yes") if value else _("no")


def on_off(value: bool) -> str:
    return _("on") if value else _("off")


# --- common helpers --------------------------------------------------------
def make_router(cfg: Config) -> Router:
    r = cfg["router"]
    if not r["host"]:
        raise RouterError(_("Router address is not set (Settings → Router)"))
    return Router(r["host"], r["port"], r["user"], r["key_path"], r["section"])


def get_subscription(cfg: Config) -> Subscription:
    s = cfg["subscription"]
    return fetch(s["url"], s["user_agent"], s["timeout"])


def visible_links(cfg: Config, sub: Subscription) -> list[Link]:
    f = cfg["filters"]
    return apply_filters(sub.links, f["types"], f["exclude_countries"], f["prefer_countries"])


def print_diff(old: list[str], new: list[str], emoji: bool = True) -> None:
    d = diff(old, new)
    for sign, key in (("+", "added"), ("~", "updated"), ("-", "removed")):
        for raw in d[key]:
            print(f"  {sign} {label_of(raw, emoji)}")
    print(_("Total: added {a}, updated {u}, removed {r}, unchanged {s}").format(
        a=len(d["added"]), u=len(d["updated"]), r=len(d["removed"]), s=len(d["same"])))


def compute_protected(cfg: Config, current: list[str], selected: list[Link], action: str) -> list[str]:
    """compute(), but protected links stay as they are for any action."""
    new = compute(current, selected, action)
    prot = protected_ids(cfg)
    kept = {raw_id(r): r for r in current if raw_id(r) in prot}
    new = [kept.get(raw_id(r), r) for r in new]
    present = {raw_id(r) for r in new}
    return [r for pid, r in kept.items() if pid not in present] + new


def write_links(cfg: Config, router: Router, new: list[str], dry_run: bool = False) -> bool:
    if dry_run:
        print(_("Dry run: nothing was written to the router."))
        return False
    backup = router.backup(cfg["backup"]["dir"], cfg["backup"]["keep"])
    print(_("Backup: {path}").format(path=backup))
    router.set_links(new)
    if router.get_links() != new:
        raise RouterError(_("After writing, the list on the router differs from the expected one"))
    print(_("Done: section {section} now has {n} links. Podkop has not applied the changes yet.")
          .format(section=router.section, n=len(new)))
    return True


def apply_now(cfg: Config, router: Router, new: list[str]) -> bool:
    method, ok = apply_and_verify(router, cfg, new, print)
    print(_("Applied via {method}.").format(method=method) if ok else
          _("Could not apply: check Podkop → Status and the log."))
    return ok


# --- non-interactive commands ----------------------------------------------
def cmd_list(cfg: Config) -> int:
    sub = get_subscription(cfg)
    meta = describe_meta(sub.meta)
    print(_("Links in subscription: {n}").format(n=len(sub.links)) + (f" ({meta})" if meta else ""))
    for l in visible_links(cfg, sub):
        print(f"  {l.label(cfg['ui']['show_emoji'])}  {l.host}:{l.port}")
    return 0


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    sub = get_subscription(cfg)
    links = visible_links(cfg, sub)
    prefer = {c.upper() for c in cfg["filters"]["prefer_countries"]}
    if args.only_preferred and prefer:
        links = [l for l in links if (l.cc or "??") in prefer]
    if not links:
        print(_("No links left after filters, nothing written."), file=sys.stderr)
        return 1
    router = make_router(cfg)
    current = router.get_links()
    action = args.action or cfg["update"]["action"]
    new = compute_protected(cfg, current, links, action)
    if set(new) == set(current):
        print(_("No changes."))
        return 0
    print_diff(current, new, cfg["ui"]["show_emoji"])
    if write_links(cfg, router, new, dry_run=args.dry_run) and args.restart:
        return 0 if apply_now(cfg, router, new) else 2
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    router = make_router(cfg)
    state = state_for(cfg)
    with RunLock(state.path.with_name("sync.lock")) as lock:
        if not lock.acquired:
            print(_("Another run is already in progress, exiting."))
            return 0
        return run_sync(cfg, router, state, Notifier(cfg),
                        lambda: visible_links(cfg, get_subscription(cfg)),
                        dry_run=args.dry_run)


def print_check(cfg: Config, entries, active) -> None:
    emoji = cfg["ui"]["show_emoji"]
    for e in entries:
        print(format_entry(e, active, emoji))
    available = sum(1 for e in entries if e.alive)
    dead = sum(1 for e in entries if e.dead)
    rest = len(entries) - available - dead
    summary = _("Available {a}, not responding {d}, not checked {r}.").format(a=available, d=dead, r=rest)
    if emoji:
        summary += " " + _("▶ — active node, 🔒 — protected from changes.")
    print("\n" + summary)


def cmd_check(cfg: Config) -> int:
    router = make_router(cfg)
    print(_("Checking delays via podkop…"))
    entries, active = inspect(cfg, router, print)
    print_check(cfg, entries, active)
    return 0


def check_subscription(cfg: Config, printer=print) -> int:
    """Check new links from the subscription (not yet on the router). Writes nothing."""
    from .probe import Prober
    router = make_router(cfg)
    emoji = cfg["ui"]["show_emoji"]
    sub_links = visible_links(cfg, get_subscription(cfg))
    current = {parse_link(r).key for r in router.get_links() if parse_link(r)}
    cands = [l for l in sub_links if l.key not in current]
    if not cands:
        printer(_("All links from the subscription (after filters) are already on the router."))
        return 0
    mode = cfg["sync"].get("probe", "full")
    if mode == "off":
        mode = "tcp"
        printer(_("Probing is disabled in settings, using TCP only."))
    prober = Prober(mode, cfg["sync"].get("singbox_bin", ""),
                    float(cfg["sync"].get("probe_timeout_sec", 15)), len(cands), printer)
    ok = 0
    for l in cands:
        alive = prober(l)
        ok += alive
        mark = "✓" if alive else "✗"
        printer(f"{mark} {l.label(emoji)}  — {prober.notes.get(l.raw, '')}")
    printer("\n" + _("New links from subscription: {n}, passed: {ok}. Nothing was written to the router.")
            .format(n=len(cands), ok=ok))
    return 0


def cmd_check_sub(cfg: Config) -> int:
    return check_subscription(cfg)


def flow_check_sub(cfg: Config) -> None:
    print(_("Checking new links from the subscription (read-only)…"))
    check_subscription(cfg)


def cmd_rollback(cfg: Config) -> int:
    router = make_router(cfg)
    backups = Router.list_backups(cfg["backup"]["dir"])
    if not backups:
        print(_("No backups."), file=sys.stderr)
        return 1
    router.restore(backups[0])
    print(_("Restored {name}. To apply: podkop reload.").format(name=backups[0]))
    return 0


# --- setup wizard ----------------------------------------------------------
def ensure_sub(cfg: Config, state: dict) -> Subscription | None:
    if state.get("sub") is None:
        try:
            state["sub"] = get_subscription(cfg)
        except SubscriptionError as exc:
            print(_("Error: {e}").format(e=exc))
            return None
    return state["sub"]


def step_language(cfg: Config, state: dict) -> str:
    from . import ui
    choice = ui.select("Language / Язык", [(LANGUAGES["en"], "en"), (LANGUAGES["ru"], "ru")])
    if choice == ui.BACK:
        return "back"
    cfg["ui"]["language"] = choice
    set_language(choice)
    return "next"


def step_subscription(cfg: Config, state: dict) -> str:
    from . import ui
    while True:
        url = ui.text(_("Subscription link:"), cfg["subscription"]["url"])
        if url is None:
            return "back"
        cfg["subscription"]["url"] = url.strip()
        state["sub"] = None
        sub = ensure_sub(cfg, state)
        if sub:
            meta = describe_meta(sub.meta)
            types = sorted({l.scheme for l in sub.links})
            print(_("Links received: {n} (types: {types})").format(n=len(sub.links), types=", ".join(types))
                  + (f", {meta}" if meta else ""))
            return "next"


def _keygen(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "podkop-sub",
                    "-f", str(path)], check=True)
    return Path(str(path) + ".pub").read_text().strip()


def step_router(cfg: Config, state: dict) -> str:
    from . import ui
    r = cfg["router"]
    host = ui.text(_("Router address (IP or hostname):"), r["host"])
    if host is None:
        return "back"
    port = ui.text(_("SSH port:"), str(r["port"]))
    if port is None:
        return "back"
    user = ui.text(_("SSH user (on OpenWrt only root can edit /etc/config):"), r["user"])
    if user is None:
        return "back"
    r.update(host=host.strip(), port=int(port) if port.strip().isdigit() else 22,
             user=user.strip() or "root")
    key = Path(r["key_path"]).expanduser()
    pub = Path(str(key) + ".pub")
    if not key.exists():
        if not ui.confirm(_("Create a separate SSH key for this script ({path})?").format(path=key), True):
            path = ui.text(_("Path to an existing private key:"), str(key))
            if path is None:
                return "back"
            r["key_path"] = path.strip()
            key = Path(r["key_path"]).expanduser()
            pub = Path(str(key) + ".pub")
        else:
            try:
                _keygen(key)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(_("Could not create the key: {e}").format(e=exc))
                return "back"
    if pub.exists():
        print(_("\nAdd the public key on the router (run this there):\n"))
        print(f"  echo '{pub.read_text().strip()}' >> /etc/dropbear/authorized_keys "
              "&& chmod 600 /etc/dropbear/authorized_keys\n")
    while True:
        try:
            make_router(cfg).test()
            print(_("Connected to the router, podkop found."))
            return "next"
        except RouterError as exc:
            print(_("Could not connect: {e}").format(e=exc))
            choice = ui.select(_("What next?"), [(_("Retry check"), "retry"),
                                                 (_("Skip, I'll check later"), "skip")])
            if choice == "retry":
                continue
            return "next" if choice == "skip" else "back"


def step_section(cfg: Config, state: dict) -> str:
    from . import ui
    current = cfg["router"]["section"]
    try:
        names = make_router(cfg).sections()
    except RouterError:
        names = []
    if names:
        choice = ui.select(_("Podkop section to write links to:"),
                           [(n + (_("  (current)") if n == current else ""), n) for n in names])
        if choice == ui.BACK:
            return "back"
        cfg["router"]["section"] = choice
    else:
        name = ui.text(_("Podkop section name (e.g. Proxy):"), current)
        if name is None:
            return "back"
        cfg["router"]["section"] = name.strip() or current
    return "next"


def step_types(cfg: Config, state: dict) -> str:
    from . import ui
    sub = ensure_sub(cfg, state)
    if sub is None:
        return "back"
    counts: dict[str, int] = {}
    for l in sub.links:
        counts[l.scheme] = counts.get(l.scheme, 0) + 1
    chosen = set(cfg["filters"]["types"])
    while True:
        res = ui.checkbox(_("Which link types to take from the subscription:"),
                          [(f"{t} ({n})", t, t in chosen) for t, n in sorted(counts.items())])
        if res is None:
            return "back"
        if res:
            cfg["filters"]["types"] = res
            return "next"
        print(_("Choose at least one type."))


def _country_items(links: list[Link], selected: set[str], emoji: bool):
    items = []
    for cc, flag, name, n in countries(links):
        flag_s = f"{flag} " if emoji and flag else ""
        items.append((f"{flag_s}[{cc}] {name or _('no name')} ({n})", cc, cc in selected))
    return items


def step_exclude(cfg: Config, state: dict) -> str:
    from . import ui
    sub = ensure_sub(cfg, state)
    if sub is None:
        return "back"
    links = apply_filters(sub.links, cfg["filters"]["types"], [], [])
    res = ui.checkbox(_("Excluded countries (their links will not be shown):"),
                      _country_items(links, set(cfg["filters"]["exclude_countries"]),
                                     cfg["ui"]["show_emoji"]))
    if res is None:
        return "back"
    cfg["filters"]["exclude_countries"] = res
    return "next"


def step_prefer(cfg: Config, state: dict) -> str:
    from . import ui
    sub = ensure_sub(cfg, state)
    if sub is None:
        return "back"
    links = apply_filters(sub.links, cfg["filters"]["types"],
                          cfg["filters"]["exclude_countries"], [])
    res = ui.checkbox(_("Preferred countries (listed first and pre-selected):"),
                      _country_items(links, set(cfg["filters"]["prefer_countries"]),
                                     cfg["ui"]["show_emoji"]))
    if res is None:
        return "back"
    cfg["filters"]["prefer_countries"] = res
    return "next"


def step_protect(cfg: Config, state: dict) -> str:
    """Which links on the router the script must never touch (delete, replace, update)."""
    from . import ui
    emoji = cfg["ui"]["show_emoji"]
    try:
        current = make_router(cfg).get_links()
    except RouterError as exc:
        print(_("Could not read links from the router: {e}").format(e=exc) + "\n" +
              _("You can set up the protected list later: Settings → Protected links.") + "\n" +
              _("Until it is set up, scheduled runs will not change anything."))
        return "next"
    sub = ensure_sub(cfg, state)
    sub_keys = {l.key for l in sub.links} if sub else set()
    stored = {s.split()[0]: s for s in cfg["protect"]["links"] if s.strip()}
    reviewed = cfg["protect"]["reviewed"]
    items, seen = [], set()
    for raw in current:
        rid = raw_id(raw)
        seen.add(rid)
        link = parse_link(raw)
        if reviewed:
            checked = rid in stored
        else:  # first time: pre-select what is not in the subscription (probably added by hand)
            checked = rid in stored or (sub is not None and (link is None or link.key not in sub_keys))
        where = f"  {link.host}:{link.port}" if link else ""
        items.append((f"{label_of(raw, emoji)}{where}", rid, checked))
    for rid, rec in stored.items():
        if rid not in seen:
            items.append((_("(not on router) {d}").format(d=rec.split(' ', 1)[-1]), rid, True))
    if not items:
        print(_("There are no links on the router, nothing to protect yet."))
        cfg["protect"]["reviewed"] = True
        return "next"
    res = ui.checkbox(_("Protected links (the script never deletes, replaces or updates them, "
                        "even if they don't respond):"), items)
    if res is None:
        return "back"
    records = {}
    for raw in current:
        rid = raw_id(raw)
        if rid in res:
            records[rid] = protect_record(raw)
    for rid in res:
        records.setdefault(rid, stored.get(rid, rid))
    cfg["protect"]["links"] = list(records.values())
    cfg["protect"]["reviewed"] = True
    return "next"


WIZARD_STEPS = [step_language, step_subscription, step_router, step_section,
                step_types, step_exclude, step_prefer, step_protect]


def wizard(cfg: Config) -> bool:
    snapshot = copy.deepcopy(cfg.data)
    state: dict = {"sub": None}
    i = 0
    print(_("\n=== Initial setup ===  (Ctrl+C at a prompt — go back one step)\n"))
    while 0 <= i < len(WIZARD_STEPS):
        i += 1 if WIZARD_STEPS[i](cfg, state) == "next" else -1
    if i < 0:
        cfg.data = snapshot
        set_language(cfg["ui"]["language"])
        print(_("Setup cancelled, nothing saved."))
        return False
    cfg.save()
    print(_("Settings saved: {path}").format(path=cfg.path))
    return True


# --- menu items ------------------------------------------------------------
def pick_links(cfg: Config, links: list[Link]):
    from . import ui
    pref = {c.upper() for c in cfg["filters"]["prefer_countries"]}
    emoji = cfg["ui"]["show_emoji"]
    return ui.checkbox(
        _("Which links to write ({n} available):").format(n=len(links)),
        [(l.label(emoji), l, (l.cc or "??") in pref if pref else True) for l in links])


def flow_update(cfg: Config) -> None:
    from . import ui
    emoji = cfg["ui"]["show_emoji"]
    sub = get_subscription(cfg)
    meta = describe_meta(sub.meta)
    if meta:
        print(_("Subscription: {meta}").format(meta=meta))
    links = visible_links(cfg, sub)
    if not links:
        print(_("No links left after filters (check Settings → Filters)."))
        return
    router = make_router(cfg)
    current = router.get_links()
    step, selected, action, new = 0, [], cfg["update"]["action"], []
    while True:
        if step == 0:
            res = pick_links(cfg, links)
            if res is None:
                return
            if not res:
                print(_("Nothing selected."))
                continue
            selected, step = res, 1
        elif step == 1:
            res = ui.select(_("What to do with the list on the router ({n} links now)?").format(n=len(current)),
                            [(t, k) for k, t in actions().items()])
            if res == ui.BACK:
                step = 0
                continue
            action, step = res, 2
        else:
            new = compute_protected(cfg, current, selected, action)
            print()
            if set(new) == set(current):
                print(_("No changes."))
                return
            print_diff(current, new, emoji)
            res = ui.select(_("Write changes to the config?"),
                            [(_("Write"), "ok")], back_label=_("← Back to action"))
            if res == "ok":
                write_links(cfg, router, new)
                if ui.confirm(_("Apply now (podkop {method} with verification)?").format(
                        method=cfg["apply"]["method"]), True):
                    apply_now(cfg, router, new)
                return
            step = 1


def show_current(cfg: Config) -> list[str]:
    router = make_router(cfg)
    current = router.get_links()
    prot = protected_ids(cfg)
    emoji = cfg["ui"]["show_emoji"]
    print(_("\nIn section {section}: {n} links").format(section=router.section, n=len(current)))
    for i, raw in enumerate(current, 1):
        link = parse_link(raw)
        extra = f"  {link.host}:{link.port}" if link else ""
        lock = (" 🔒" if emoji else " [P]") if raw_id(raw) in prot else ""
        print(f"{i:>3}. {label_of(raw, emoji)}{extra}{lock}")
    return current


def flow_check(cfg: Config) -> None:
    router = make_router(cfg)
    print(_("Checking delays via podkop (a few seconds)…"))
    entries, active = inspect(cfg, router, print)
    print()
    print_check(cfg, entries, active)


def flow_delete(cfg: Config) -> None:
    from . import ui
    router = make_router(cfg)
    current = router.get_links()
    if not current:
        print(_("No links in the section."))
        return
    emoji = cfg["ui"]["show_emoji"]
    prot = protected_ids(cfg)
    res = ui.checkbox(_("Which links to delete:"),
                      [(("🔒 " if raw_id(raw) in prot else "") + label_of(raw, emoji), raw, False)
                       for raw in current])
    if not res:
        return
    chosen = set(res)
    new = [raw for raw in current if raw not in chosen]
    print_diff(current, new, emoji)
    hit = [raw for raw in chosen if raw_id(raw) in prot]
    if hit and not ui.confirm(_("Among the selected protected links: {n}. Delete anyway?").format(n=len(hit)), False):
        return
    if ui.confirm(_("Delete the selected ones?"), False):
        write_links(cfg, router, new)
        if hit:
            gone = {raw_id(r) for r in hit}
            cfg["protect"]["links"] = [s for s in cfg["protect"]["links"]
                                       if s.split()[0] not in gone]
            cfg.save()


def flow_add(cfg: Config) -> None:
    from . import ui
    raw = ui.text(_("Paste the link (vless://…):"))
    if not raw:
        return
    link = parse_link(raw)
    if link is None:
        print(_("This does not look like a link (expected scheme://…)."))
        return
    router = make_router(cfg)
    current = router.get_links()
    new = compute(current, [link], "merge")
    print_diff(current, new, cfg["ui"]["show_emoji"])
    if ui.confirm(_("Write?"), False):
        write_links(cfg, router, new)
        if ui.confirm(_("Protect this link (the script will not change or delete it)?"), True):
            rid = raw_id(link.raw)
            cfg["protect"]["links"] = [s for s in cfg["protect"]["links"] if s.split()[0] != rid]
            cfg["protect"]["links"].append(protect_record(link.raw))
            cfg.save()


def flow_backups(cfg: Config) -> None:
    from . import ui
    backups = Router.list_backups(cfg["backup"]["dir"])
    if not backups:
        print(_("No backups yet."))
        return
    choice = ui.select(_("Choose a backup to roll back to:"), [(b.name, b) for b in backups[:30]])
    if choice == ui.BACK:
        return
    if ui.confirm(_("Restore {name}? The current config will be saved as a separate backup.").format(
            name=choice.name), False):
        router = make_router(cfg)
        print(_("Current backup: {path}").format(path=router.backup(cfg['backup']['dir'], cfg['backup']['keep'])))
        router.restore(choice)
        print(_("Rollback done. To apply: Podkop → Reload config (reload)."))


def flow_subscription_view(cfg: Config) -> None:
    sub = get_subscription(cfg)
    meta = describe_meta(sub.meta)
    print(_("\nLinks in subscription: {n}").format(n=len(sub.links)) + (f" ({meta})" if meta else ""))
    for l in sub.links:
        print(f"  {l.label(cfg['ui']['show_emoji'])}  {l.host}:{l.port}")


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def describe_status(router: Router) -> str:
    st = router.sing_box_status()
    if st is None:
        running = router.sing_box_process_running()
        return (_("Could not get status via podkop get_sing_box_status.\n") +
                _("sing-box process: {state}").format(
                    state=_("running") if running else _("not found")))
    lines = [_("sing-box: {state}, autostart {auto}").format(
        state=_("running") if _truthy(st.get("running")) else _("stopped"),
        auto=_("enabled") if _truthy(st.get("enabled")) else _("disabled"))]
    if "dns_configured" in st:
        lines.append(_("DNS set up for podkop: {v}").format(
            v=yes_no(_truthy(st["dns_configured"]))))
    if st.get("version"):
        lines.append(_("sing-box version: {v}").format(v=st["version"]))
    if st.get("status"):
        lines.append(_("Podkop status: {v}").format(v=st["status"]))
    return "\n".join(lines)


def flow_service(cfg: Config, action: str) -> None:
    router = make_router(cfg)
    if action == "status":
        out = describe_status(router)
    elif action == "logs":
        out = router.logs(40)
    else:
        out = router.service(action)
    print(out.strip() or _("(empty output)"))


# --- notifications, sync settings, schedule --------------------------------
def notify_menu(cfg: Config) -> None:
    from . import ui
    while True:
        n = cfg["notify"]
        choice = ui.select(_("Telegram notifications"), [
            (_("Bot: {state}").format(state=_("configured") if n["telegram_token"] else _("not configured")), "bot"),
            (_("Report changes: {v}").format(v=yes_no(n["on_change"])), "on_change"),
            (_("Report errors: {v}").format(v=yes_no(n["on_error"])), "on_error"),
            (_("Send test message"), "test"),
            (_("Delete bot settings"), "clear"),
        ])
        if choice == ui.BACK:
            return
        if choice == "bot":
            token = ui.text(_("Bot token (issued by @BotFather):"))
            if not token:
                continue
            chat = ui.text(_("chat_id (number; write to the bot, then open "
                             "https://api.telegram.org/bot<token>/getUpdates):"), n["telegram_chat_id"])
            if not chat:
                continue
            n["telegram_token"], n["telegram_chat_id"] = token.strip(), chat.strip()
        elif choice in ("on_change", "on_error"):
            n[choice] = not n[choice]
        elif choice == "clear":
            n["telegram_token"] = n["telegram_chat_id"] = ""
        elif choice == "test":
            notifier = Notifier(cfg)
            if not notifier.enabled:
                print(_("First set the token and chat_id."))
            else:
                ok, err = notifier.test()
                print(_("Message sent.") if ok else _("Could not send: {e}").format(e=err))
            ui.pause()
            continue
        cfg.save()


def ask_int(message: str, default: int, lo: int, hi: int):
    from . import ui
    while True:
        raw = ui.text(message, str(default))
        if raw is None:
            return None
        raw = raw.strip()
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        print(_("Enter a number from {lo} to {hi}.").format(lo=lo, hi=hi))


def ask_time(message: str, default: str):
    from . import ui
    while True:
        raw = ui.text(message, default)
        if raw is None:
            return None
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", raw)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return int(m.group(1)), int(m.group(2))
        print(_("Format HH:MM, for example 04:30."))


def sync_settings_menu(cfg: Config) -> None:
    from . import ui
    while True:
        s, a = cfg["sync"], cfg["apply"]
        labels = probe_labels()
        choice = ui.select(_("Sync and apply settings"), [
            (_("Remove links missing from the subscription: {v}").format(v=yes_no(s["remove_missing"])), "remove"),
            (_("Apply changes via: podkop {m}").format(m=a["method"]), "method"),
            (_("If reload fails, restart: {v}").format(v=yes_no(a["fallback_restart"])), "fallback"),
            (_("Replace one dead link at most every {n} min").format(n=s["refresh_cooldown_min"]), "cooldown"),
            (_("Minimum links in subscription: {n}").format(
                n=s["min_links"] or _("not limited")), "minlinks"),
            (_("Check new links before writing: {v}").format(v=labels.get(s.get("probe", "full"), "?")), "probe"),
            (_("Path to sing-box (for full check): {p}").format(p=s.get("singbox_bin") or "—"), "singbox"),
        ])
        if choice == ui.BACK:
            return
        if choice == "remove":
            s["remove_missing"] = not s["remove_missing"]
        elif choice == "method":
            a["method"] = "restart" if a["method"] == "reload" else "reload"
        elif choice == "fallback":
            a["fallback_restart"] = not a["fallback_restart"]
        elif choice == "cooldown":
            v = ask_int(_("Minutes between replacements of one link (0 — no limit):"),
                        s["refresh_cooldown_min"], 0, 10080)
            if v is None:
                continue
            s["refresh_cooldown_min"] = v
        elif choice == "probe":
            order = ["full", "tcp", "off"]
            s["probe"] = order[(order.index(s.get("probe", "full")) + 1) % len(order)]
        elif choice == "singbox":
            v = ui.text(_("Path to sing-box (empty — TCP check only):"), s.get("singbox_bin", ""))
            if v is None:
                continue
            s["singbox_bin"] = v.strip()
        elif choice == "minlinks":
            v = ask_int(_("Minimum links in the subscription after filters (0 — no limit):"),
                        s["min_links"], 0, 1000)
            if v is None:
                continue
            s["min_links"] = v
        cfg.save()


def schedule_status(cfg: Config) -> None:
    expr = sched.get_expr()
    st = state_for(cfg)
    print()
    if expr:
        print(_("Schedule: enabled — {desc}\n  cron: {expr}").format(desc=sched.describe(expr), expr=expr))
        nxt = sched.next_run(expr)
        if nxt:
            print(_("  next run: {t}").format(t=f"{nxt:%d.%m.%Y %H:%M}"))
    else:
        print(_("Schedule: disabled"))
    if not sched.cron_installed():
        print(_("Warning: cron not found (apt install cron), the schedule will not work."))
    if st["last_run"]:
        print(_("Last run: {t} — {status}: {msg}").format(
            t=f"{datetime.fromtimestamp(st['last_run']):%d.%m.%Y %H:%M}",
            status=st["last_status"], msg=st["last_message"]))
    else:
        print(_("No scheduled runs yet."))
    if not cfg["protect"]["reviewed"]:
        print(_("Warning: protected list is not set up, a run will change nothing "
                "(Settings → Protected links)."))
    print(_("Apply: podkop {m}; Telegram: {t}; log: {path}").format(
        m=cfg["apply"]["method"], t=on_off(Notifier(cfg).enabled), path=sched.log_path()))


def schedule_edit(cfg: Config) -> None:
    from . import ui
    kind = ui.select(_("How often to run?"), [
        (_("Every hour"), "hour"), (_("Every N hours"), "nhours"),
        (_("Every day at a set time"), "daily"), (_("On selected weekdays"), "weekly"),
        (_("Custom cron expression"), "custom")])
    if kind == ui.BACK:
        return
    if kind == "custom":
        txt = ui.text(_("cron expression (minute hour day month weekday):"),
                      sched.get_expr() or "7 * * * *")
        if txt is None:
            return
        expr = sched.validate(txt)
    elif kind in ("hour", "nhours"):
        n = 1
        if kind == "nhours":
            n = ask_int(_("Every how many hours (1-23):"), 2, 1, 23)
            if n is None:
                return
        minute = ask_int(_("Which minute of the hour to run (0-59):"), 7, 0, 59)
        if minute is None:
            return
        expr = sched.every_n_hours(n, minute)
    else:
        when = ask_time(_("Run time (HH:MM):"), "04:00")
        if when is None:
            return
        if kind == "daily":
            expr = sched.daily(*when)
        else:
            names = {1: _("Mon"), 2: _("Tue"), 3: _("Wed"), 4: _("Thu"), 5: _("Fri"), 6: _("Sat"), 0: _("Sun")}
            days = ui.checkbox(_("Which days:"), [(names[i], i, i in (1, 2, 3, 4, 5)) for i in (1, 2, 3, 4, 5, 6, 0)])
            if not days:
                print(_("Choose at least one day."))
                return
            expr = sched.weekly(days, *when)
    if not ui.confirm(_("Enable schedule: {desc} (cron: {expr})?").format(
            desc=sched.describe(expr), expr=expr), True):
        return
    sched.enable(expr, cfg.path)
    print(_("Schedule enabled: {desc}.").format(desc=sched.describe(expr)))
    nxt = sched.next_run(expr)
    if nxt:
        print(_("Next run: {t}").format(t=f"{nxt:%d.%m.%Y %H:%M}"))
    if not sched.cron_installed():
        print(_("Warning: cron not found, install it: apt install cron"))
    if not cfg["protect"]["reviewed"] and ui.confirm(
            _("The protected list is not set up yet, without it a run does nothing. Set it up now?"), True):
        if step_protect(cfg, {"sub": None}) == "next":
            cfg.save()
    if not Notifier(cfg).enabled and ui.confirm(_("Set up Telegram notifications now?"), False):
        notify_menu(cfg)


def schedule_disable(cfg: Config) -> None:
    from . import ui
    if ui.confirm(_("Disable scheduled runs?"), False):
        print(_("Schedule disabled.") if sched.disable() else _("Schedule was already disabled."))


def schedule_run_now(cfg: Config) -> None:
    from . import ui
    mode = ui.select(_("How to run?"), [(_("Dry run (change nothing, only show)"), "dry"),
                                        (_("Full run (as on schedule)"), "full")])
    if mode == ui.BACK:
        return
    code = cmd_sync(cfg, argparse.Namespace(dry_run=mode == "dry"))
    print(_("\nExit code: {code}").format(code=code))


def schedule_log(cfg: Config) -> None:
    text = sched.read_log(40)
    print(text if text else _("Log is empty or unavailable ({path}).").format(path=sched.log_path()))


def schedule_menu(cfg: Config) -> None:
    from . import ui
    while True:
        expr = sched.get_expr()
        title = (_("Schedule: enabled — {desc}").format(desc=sched.describe(expr)) if expr
                 else _("Schedule: disabled"))
        items = [(_("Status"), "status"), (_("Edit…") if expr else _("Enable…"), "edit")]
        if expr:
            items.append((_("Disable"), "disable"))
        items += [(_("Run now"), "run"), (_("Show log"), "log")]
        choice = ui.select(title, items)
        if choice == ui.BACK:
            return
        fn = {"status": schedule_status, "edit": schedule_edit, "disable": schedule_disable,
              "run": schedule_run_now, "log": schedule_log}[choice]
        guarded(fn, cfg)


# --- settings --------------------------------------------------------------
def settings_menu(cfg: Config) -> None:
    from . import ui
    state: dict = {"sub": None}
    while True:
        f = cfg["filters"]
        choice = ui.select(_("Settings"), [
            (_("Subscription link"), "sub"),
            (_("Router: {user}@{host}:{port}").format(user=cfg['router']['user'], host=cfg['router']['host'],
                                                       port=cfg['router']['port']), "router"),
            (_("Podkop section: {section}").format(section=cfg['router']['section']), "section"),
            (_("Link types: {types}").format(types=', '.join(f['types'])), "types"),
            (_("Excluded countries: {v}").format(v=', '.join(f['exclude_countries']) or '—'), "exclude"),
            (_("Preferred countries: {v}").format(v=', '.join(f['prefer_countries']) or '—'), "prefer"),
            (_("Protected links: {n}").format(n=len(cfg['protect']['links'])) +
             ("" if cfg['protect']['reviewed'] else _(" (not set up!)")), "protect"),
            (_("Sync and apply settings"), "syncset"),
            (_("Telegram notifications: {v}").format(v=on_off(Notifier(cfg).enabled)), "notify"),
            (_("Default action (manual update): {a}").format(a=cfg['update']['action']), "action"),
            (_("Flag emoji: {v}").format(v=on_off(cfg['ui']['show_emoji'])), "emoji"),
            (_("Language: {lang}").format(lang=LANGUAGES.get(cfg['ui']['language'], cfg['ui']['language'])), "language"),
            (_("Run the setup wizard again"), "wizard"),
        ])
        if choice == ui.BACK:
            return
        if choice == "wizard":
            wizard(cfg)
        elif choice == "language":
            cfg["ui"]["language"] = "en" if cfg["ui"]["language"] == "ru" else "ru"
            set_language(cfg["ui"]["language"])
            cfg.save()
        elif choice == "syncset":
            sync_settings_menu(cfg)
        elif choice == "notify":
            notify_menu(cfg)
        elif choice == "action":
            res = ui.select(_("Choose default action:"), [(t, k) for k, t in actions().items()])
            if res != ui.BACK:
                cfg["update"]["action"] = res
                cfg.save()
        elif choice == "emoji":
            cfg["ui"]["show_emoji"] = not cfg["ui"]["show_emoji"]
            cfg.save()
        else:
            step = {"sub": step_subscription, "router": step_router, "section": step_section,
                    "types": step_types, "exclude": step_exclude, "prefer": step_prefer,
                    "protect": step_protect}[choice]
            try:
                if step(cfg, state) == "next":
                    cfg.save()
            except HANDLED as exc:
                print(_("Error: {e}").format(e=exc))
        state["sub"] = None


def guarded(fn, *a) -> None:
    from . import ui
    try:
        fn(*a)
    except HANDLED as exc:
        print(_("\nError: {e}").format(e=exc))
    ui.pause()


def show_help(cfg: Config) -> None:
    print(help_text())
    from . import ui
    ui.pause()


def main_menu(cfg: Config) -> None:
    from . import ui
    while True:
        choice = ui.select("podkop-sub", [
            (_("Update links from subscription"), "update"),
            (_("Links on the router"), "links"),
            (_("Podkop (status, restart, logs)"), "podkop"),
            (_("Schedule (automatic updates)"), "schedule"),
            (_("Settings"), "settings"),
            (_("Help"), "help"),
            (_("Exit"), "exit"),
        ], back_label="")
        if choice in ("exit", ui.BACK):
            return
        if choice == "update":
            guarded(flow_update, cfg)
        elif choice == "settings":
            settings_menu(cfg)
        elif choice == "schedule":
            schedule_menu(cfg)
        elif choice == "help":
            show_help(cfg)
        elif choice == "links":
            while True:
                sub = ui.select(_("Links on the router"), [
                    (_("Show"), "show"), (_("Availability check (delay test)"), "check"),
                    (_("Delete…"), "delete"), (_("Add my own link…"), "add"),
                    (_("Check new links from subscription (no changes)"), "checksub"),
                    (_("Backups and rollback…"), "backups"),
                    (_("View subscription without writing"), "view")])
                if sub == ui.BACK:
                    break
                fn = {"show": show_current, "check": flow_check, "checksub": flow_check_sub,
                      "delete": flow_delete, "add": flow_add, "backups": flow_backups,
                      "view": flow_subscription_view}[sub]
                guarded(fn, cfg)
        elif choice == "podkop":
            while True:
                sub = ui.select(_("Podkop"), [(_("Status"), "status"), (_("Restart"), "restart"),
                                              (_("Reload config (reload)"), "reload"),
                                              (_("Last log lines"), "logs")])
                if sub == ui.BACK:
                    break
                if sub in ("restart", "reload") and not ui.confirm(_("Run {action}?").format(action=sub), False):
                    continue
                guarded(flow_service, cfg, sub)


# --- entry point -----------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="podkop-sub",
        description="Import links from a subscription into a podkop section on OpenWrt. "
                    "Without arguments, opens the menu.")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--wizard", action="store_true", help="run the setup wizard")
    p.add_argument("--list", action="store_true", help="show the subscription (after filters) and exit")
    p.add_argument("--check", action="store_true",
                   help="check availability of links on the router (delay test via podkop)")
    p.add_argument("--check-sub", action="store_true",
                   help="check new links from the subscription (TCP and Reality), write nothing")
    p.add_argument("--sync", action="store_true",
                   help="for cron: refresh the subscription; if something changed, write and apply, "
                        "otherwise just exit")
    p.add_argument("--update", action="store_true", help="update the section once, without questions")
    p.add_argument("--action", choices=list(actions()), help="for --update: replace / append / merge")
    p.add_argument("--only-preferred", action="store_true", help="for --update: preferred countries only")
    p.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    p.add_argument("--restart", action="store_true",
                   help="for --update: apply after writing (reload with verification)")
    p.add_argument("--rollback", action="store_true", help="restore the last backup")
    args = p.parse_args(argv)
    cfg = Config(args.config)
    set_language(cfg["ui"]["language"])
    try:
        if args.list:
            return cmd_list(cfg)
        if args.check:
            return cmd_check(cfg)
        if args.check_sub:
            return cmd_check_sub(cfg)
        if args.sync:
            return cmd_sync(cfg, args)
        if args.update:
            return cmd_update(cfg, args)
        if args.rollback:
            return cmd_rollback(cfg)
        if args.wizard or not cfg.configured:
            if not wizard(cfg) and not cfg.configured:
                return 1
        main_menu(cfg)
        return 0
    except HANDLED as exc:
        print(_("Error: {e}").format(e=exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
