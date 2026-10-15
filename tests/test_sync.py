"""Синхронизация (--sync), расписание, уведомления — с имитацией роутера."""
import http.server
import json
import os
import subprocess
import tempfile
import threading
import sys
import unittest
from datetime import datetime
from pathlib import Path

from podkop_sub import cli, schedule
from podkop_sub.config import Config
from podkop_sub.links import normalize, parse_link, parse_many, raw_id
from podkop_sub.notify import Notifier
from podkop_sub.router import Router
from podkop_sub.state import State
from podkop_sub.subscription import SubscriptionError
from podkop_sub.sync import run_sync

sys.path.insert(0, str(Path(__file__).parent))
import test_all  # noqa: E402
from test_all import FAKEBIN, committed, init_fake, mk

NL = lambda **kw: mk("NL", "Амстердам", "Нидерланды", "192.0.2.1", **kw)
DE = lambda **kw: mk("DE", "Берлин", "Германия", "192.0.2.2", **kw)
JP = lambda **kw: mk("JP", "Токио", "Япония", "192.0.2.4", **kw)
MANUAL = mk("FR", "Париж", "Франция", "192.0.2.9", sni="m.ru")


class Sink:
    def __init__(self):
        self.msgs = []

    def send(self, text, kind="change"):
        self.msgs.append((kind, text))
        return True


class SyncBase(unittest.TestCase):
    initial = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in ("uci", "ssh", "podkop", "initd"):
            os.chmod(f"{FAKEBIN}/{f}", 0o755)
        self.state_dir, _ = init_fake(
            self.tmp, [normalize(x) for x in (self.initial or [NL(sni="a.ru"), DE(sni="a.ru"), MANUAL])])
        os.environ["PODKOP_SUB_INITD"] = f"{FAKEBIN}/initd"
        os.environ["PODKOP_SUB_SINGBOX_CONFIG"] = str(self.tmp / "sb.json")
        subprocess.run([f"{FAKEBIN}/initd", "restart"], check=True)
        self.cfg = Config(self.tmp / "c.toml")
        self.cfg["router"]["host"] = "fake"
        self.cfg["backup"]["dir"] = str(self.tmp / "bk")
        self.cfg["protect"]["reviewed"] = True
        self.cfg["protect"]["links"] = [f"{raw_id(MANUAL)} FR"]
        self.cfg["apply"]["wait_sec"] = 0
        self.cfg["sync"]["probe"] = "off"  # сценарии синхронизации; проверка кандидатов — в ProbeTests
        self.router = Router("fake", section="Proxy")
        self.st = State(self.tmp / "state.json")
        self.sink = Sink()
        self.sleeps = []

    def run_sync(self, sub, **kw):
        links = parse_many(sub)
        return run_sync(self.cfg, self.router, self.st, self.sink, lambda: links, sleep=self.sleeps.append,
                        now=kw.pop("now", 1_000_000.0), **kw)

    def dead(self, *hosts):
        (self.state_dir / "dead").write_text(" ".join(hosts))

    def stored_hosts(self):
        return [parse_link(r).host for r in committed(self.state_dir)]


class SyncTests(SyncBase):
    def test_no_changes_exits_quietly(self):
        before = committed(self.state_dir)
        self.assertEqual(self.run_sync([NL(sni="z.ru"), DE(sni="z.ru")]), 0)
        self.assertEqual(committed(self.state_dir), before)
        self.assertEqual(self.sink.msgs, [])
        self.assertEqual(self.st["last_status"], "ok")

    def test_added_and_removed_applied_via_reload(self):
        self.assertEqual(self.run_sync([NL(sni="a.ru"), JP()]), 0)
        self.assertEqual(self.stored_hosts(), ["192.0.2.1", "192.0.2.9", "192.0.2.4"])  # DE удалён, ручная осталась
        self.assertEqual(self.st["last_status"], "changed")
        self.assertEqual(len(self.sink.msgs), 1)
        self.assertIn("➕", self.sink.msgs[0][1])
        self.assertIn("➖", self.sink.msgs[0][1])

    def test_dead_link_replaced_with_fresh_params(self):
        self.dead("192.0.2.1")
        self.assertEqual(self.run_sync([NL(sni="fresh.ru", sid="ff"), DE(sni="a.ru")]), 0)
        self.assertIn("fresh.ru", committed(self.state_dir)[0])

    def test_dead_link_cooldown(self):
        self.dead("192.0.2.1")
        self.run_sync([NL(sni="fresh.ru"), DE(sni="a.ru")], now=1_000_000.0)
        # sing-box снова «мёртв» для нового узла, но 10 минут — меньше cooldown
        self.dead("192.0.2.1")
        before = committed(self.state_dir)
        self.run_sync([NL(sni="fresher.ru"), DE(sni="a.ru")], now=1_000_600.0)
        self.assertEqual(committed(self.state_dir), before)
        self.run_sync([NL(sni="fresher.ru"), DE(sni="a.ru")], now=1_000_000.0 + 91 * 60)
        self.assertIn("fresher.ru", committed(self.state_dir)[0])

    def test_protected_dead_and_missing_untouched(self):
        self.dead("192.0.2.9")
        self.assertEqual(self.run_sync([NL(sni="a.ru"), DE(sni="a.ru")]), 0)
        self.assertIn("192.0.2.9", self.stored_hosts())
        self.assertEqual(self.sink.msgs, [])

    def test_active_dead_switches_without_reload(self):
        self.dead("192.0.2.1")
        # свежие параметры совпадают -> менять нечего, только переключить активный узел
        self.assertEqual(self.run_sync([NL(sni="a.ru"), DE(sni="a.ru")]), 0)
        self.assertEqual((self.state_dir / "active").read_text(), "Proxy-2-out")
        self.assertIn("🔀", self.sink.msgs[0][1])

    def test_reload_noop_falls_back_to_restart(self):
        (self.state_dir / "reload_noop").write_text("1")
        self.assertEqual(self.run_sync([NL(sni="a.ru"), JP()]), 0)
        self.assertIn("restart", self.st["last_message"])

    def test_apply_failure_exit_2(self):
        (self.state_dir / "reload_noop").write_text("1")
        self.cfg["apply"]["fallback_restart"] = False
        self.assertEqual(self.run_sync([NL(sni="a.ru"), JP()]), 2)
        self.assertEqual(self.sink.msgs[0][0], "error")

    def test_not_reviewed_exit_3(self):
        self.cfg["protect"]["reviewed"] = False
        before = committed(self.state_dir)
        self.assertEqual(self.run_sync([JP()]), 3)
        self.assertEqual(committed(self.state_dir), before)

    def test_min_links_guard(self):
        self.cfg["sync"]["min_links"] = 3
        before = committed(self.state_dir)
        self.assertEqual(self.run_sync([JP()]), 1)
        self.assertEqual(committed(self.state_dir), before)

    def test_empty_subscription_never_wipes(self):
        before = committed(self.state_dir)
        self.assertEqual(self.run_sync([]), 1)
        self.assertEqual(committed(self.state_dir), before)

    def test_dry_run(self):
        before = committed(self.state_dir)
        self.assertEqual(self.run_sync([JP()], dry_run=True), 0)
        self.assertEqual(committed(self.state_dir), before)
        self.assertEqual(self.sink.msgs, [])

    def test_new_links_only_preferred_like_manual(self):
        self.cfg["filters"]["prefer_countries"] = ["JP"]
        self.run_sync([NL(sni="a.ru"), DE(sni="a.ru"), JP(), mk("FI", "Хельсинки", "Финляндия", "192.0.2.7")])
        self.assertIn("192.0.2.4", self.stored_hosts())
        self.assertNotIn("192.0.2.7", self.stored_hosts())  # FI не предпочитаемая — не добавлена

    def test_no_preferences_adds_everything(self):
        self.run_sync([NL(sni="a.ru"), DE(sni="a.ru"), JP(), mk("FI", "Хельсинки", "Финляндия", "192.0.2.7")])
        self.assertIn("192.0.2.7", self.stored_hosts())

    def test_remove_missing_off(self):
        self.cfg["sync"]["remove_missing"] = False
        self.run_sync([NL(sni="a.ru"), JP()])
        self.assertIn("192.0.2.2", self.stored_hosts())

    def test_liveness_unavailable_still_syncs_composition(self):
        os.remove(self.tmp / "sb.json")  # sing-box config недоступен
        self.assertEqual(self.run_sync([NL(sni="a.ru"), DE(sni="a.ru"), JP()]), 0)
        self.assertIn("192.0.2.4", self.stored_hosts())

    def test_subscription_failure_counts_and_notifies_at_threshold(self):
        def boom():
            raise SubscriptionError("HTTP 500")
        self.cfg["notify"]["error_threshold"] = 2
        for i in range(2):
            rc = run_sync(self.cfg, self.router, self.st, self.sink, boom,
                          sleep=lambda s: None, now=1e6 + i)
            self.assertEqual(rc, 1)
        self.assertEqual(self.st["fetch_failures"], 2)
        self.assertEqual(len(self.sink.msgs), 1)
        self.assertEqual(self.run_sync([NL(sni="a.ru"), DE(sni="a.ru")]), 0)
        self.assertEqual(self.st["fetch_failures"], 0)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.chmod(f"{FAKEBIN}/singbox", 0o755)
        self.bin = f"{FAKEBIN}/singbox"
        os.environ.pop("FAKE_SINGBOX_BAD", None)

    def test_outbound_from_reality_link(self):
        from podkop_sub.probe import outbound_from_link
        ob = outbound_from_link(parse_link(NL(sni="a.ru", sid="aa")))
        self.assertEqual(ob["server"], "192.0.2.1")
        self.assertEqual(ob["tls"]["server_name"], "a.ru")
        self.assertEqual(ob["tls"]["reality"]["short_id"], "aa")
        self.assertEqual(ob["tls"]["reality"]["public_key"], "KEY")
        self.assertEqual(ob["flow"], "xtls-rprx-vision")

    def test_unsupported_link_not_judged(self):
        from podkop_sub.probe import outbound_from_link, reality_ok
        link = parse_link("socks5://u:p@10.0.0.1:1080")
        self.assertIsNone(outbound_from_link(link))
        self.assertIsNone(reality_ok(link, self.bin))

    def test_reality_through_fake_singbox(self):
        from podkop_sub.probe import reality_ok
        link = parse_link(NL())
        self.assertTrue(reality_ok(link, self.bin, timeout=5))
        os.environ["FAKE_SINGBOX_BAD"] = "192.0.2.1"
        try:
            self.assertFalse(reality_ok(link, self.bin, timeout=5))
        finally:
            os.environ.pop("FAKE_SINGBOX_BAD", None)

    def test_missing_binary_falls_back_to_tcp(self):
        from unittest import mock
        from podkop_sub import probe
        logs = []
        with mock.patch.object(probe, "tcp_ok", return_value=True):
            p = probe.Prober("full", "/nonexistent/sing-box", 3, 10, logs.append)
            self.assertTrue(p(parse_link(NL())))
        self.assertEqual(p.notes[parse_link(NL()).raw], "TCP ок (Reality не проверялась)")
        self.assertTrue(logs)

    def test_prober_cache_and_limit(self):
        from unittest import mock
        from podkop_sub import probe
        with mock.patch.object(probe, "tcp_ok", return_value=False) as t:
            p = probe.Prober("tcp", "", 3, 1)
            a, b = parse_link(NL()), parse_link(DE())
            self.assertFalse(p(a))
            self.assertFalse(p(a))          # из кэша
            self.assertFalse(p(b))          # лимит 1 исчерпан
            self.assertTrue(p.deferred)
            self.assertEqual(t.call_count, 1)

    def test_plan_rejects_dead_candidates_and_keeps_good(self):
        from podkop_sub.sync import Entry, build_plan
        good, bad = parse_link(JP()), parse_link(mk("FI", "Хельсинки", "Финляндия", "192.0.2.7"))
        entries = [Entry(parse_link(NL()).raw, parse_link(NL()), raw_id(NL()), False)]
        plan = build_plan(entries, [good, bad], prefer=[], replaced_at={}, now=1e6,
                          cooldown_s=0, remove_missing=True, active_tag=None,
                          alive=lambda l: l.host != "192.0.2.7")
        self.assertEqual([l.host for l in plan.added], ["192.0.2.4"])
        self.assertEqual(plan.rejected, [bad.raw])

    def test_dead_link_in_cooldown_is_still_probed(self):
        from podkop_sub.sync import Entry, build_plan
        cur = parse_link(NL(sni="old.ru"))
        fresh = parse_link(NL(sni="new.ru"))
        e = Entry(cur.raw, cur, cur.id, False, ms=0)
        seen = []
        plan = build_plan([e], [fresh], prefer=[], replaced_at={cur.id: 1e6 - 60}, now=1e6,
                          cooldown_s=90 * 60, remove_missing=True, active_tag=None,
                          alive=lambda l: seen.append(l.raw) or False)
        self.assertEqual(seen, [fresh.raw])          # проверка выполнена, несмотря на cooldown
        self.assertEqual(plan.rejected, [fresh.raw])

    def test_plan_keeps_dead_link_when_fresh_copy_is_dead(self):
        from podkop_sub.sync import Entry, build_plan
        cur = parse_link(NL(sni="old.ru"))
        fresh = parse_link(NL(sni="new.ru"))
        e = Entry(cur.raw, cur, cur.id, False, ms=0)
        plan = build_plan([e], [fresh], prefer=[], replaced_at={}, now=1e6, cooldown_s=0,
                          remove_missing=True, active_tag=None, alive=lambda l: False)
        self.assertEqual(plan.new, [cur.raw])
        self.assertEqual(plan.rejected, [fresh.raw])
        self.assertEqual(plan.refreshed, [])


class CliSyncTests(test_all.CliTests):
    def test_sync_flag_end_to_end(self):
        os.chmod(f"{FAKEBIN}/podkop", 0o755)
        os.chmod(f"{FAKEBIN}/initd", 0o755)
        os.environ["PODKOP_SUB_INITD"] = f"{FAKEBIN}/initd"
        os.environ["PODKOP_SUB_SINGBOX_CONFIG"] = str(self.tmp / "sb.json")
        subprocess.run([f"{FAKEBIN}/initd", "restart"], check=True)
        cfg = Config(self.cfg_path)
        cfg["apply"]["wait_sec"] = 0
        cfg["protect"]["reviewed"] = True
        cfg["sync"]["probe"] = "off"
        cfg.save()
        self.assertEqual(cli.main(["--config", self.cfg_path, "--sync"]), 0)
        self.assertEqual([parse_link(r).cc for r in committed(self.state)].count("US"), 0)
        self.assertEqual(cli.main(["--config", self.cfg_path, "--sync"]), 0)  # второй раз — без изменений
        self.assertEqual(Config(self.cfg_path)["protect"]["reviewed"], True)

    def test_sync_without_review_exit_3(self):
        self.assertEqual(cli.main(["--config", self.cfg_path, "--sync"]), 3)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        os.environ.update(PODKOP_SUB_CRON_FILE=str(tmp / "cron" / "podkop-sub"),
                          PODKOP_SUB_LOG=str(tmp / "log"),
                          PODKOP_SUB_LOGROTATE_FILE=str(tmp / "lr" / "podkop-sub"),
                          PODKOP_SUB_EXE="/usr/local/bin/podkop-sub")
        self.tmp = tmp

    def test_validate(self):
        self.assertEqual(schedule.validate("  7  */2 * * * "), "7 */2 * * *")
        for bad in ("* * * *", "61 * * * *", "a b c d e", "*/0 * * * *", "* 24 * * *"):
            with self.assertRaises(schedule.CronError):
                schedule.validate(bad)

    def test_next_run(self):
        now = datetime(2026, 10, 14, 10, 30)
        self.assertEqual(schedule.next_run("15 * * * *", now), datetime(2026, 10, 14, 11, 15))
        self.assertEqual(schedule.next_run("0 */2 * * *", now), datetime(2026, 10, 14, 12, 0))
        self.assertEqual(schedule.next_run("0 3 * * *", now), datetime(2026, 10, 15, 3, 0))
        # 2026-10-14 — среда; понедельник = 1
        self.assertEqual(schedule.next_run("0 9 * * 1", now), datetime(2026, 10, 19, 9, 0))
        self.assertIsNone(schedule.next_run("junk", now))

    def test_describe_and_builders(self):
        self.assertEqual(schedule.describe(schedule.every_n_hours(2, 5)), "каждые 2 ч. в :05")
        self.assertEqual(schedule.describe(schedule.every_hour(0)), "каждый час в :00")
        self.assertEqual(schedule.describe(schedule.daily(3, 30)), "каждый день в 03:30")
        self.assertEqual(schedule.describe(schedule.weekly([1, 5], 9, 0)), "по дням (пн, пт) в 09:00")
        self.assertTrue(schedule.describe("1 2 3 4 5").startswith("cron"))

    def test_enable_disable_roundtrip(self):
        self.assertIsNone(schedule.get_expr())
        schedule.enable("10 */1 * * *", "/root/.config/podkop-sub/config.toml", home="/root")
        self.assertEqual(schedule.get_expr(), "10 */1 * * *")
        text = schedule.cron_path().read_text()
        self.assertIn("root /usr/local/bin/podkop-sub --config /root/.config/podkop-sub/config.toml --sync", text)
        self.assertIn("2>&1", text)
        schedule.enable("0 4 * * *", "/c.toml", home="/root")
        self.assertEqual(schedule.get_expr(), "0 4 * * *")
        self.assertTrue(schedule.disable())
        self.assertIsNone(schedule.get_expr())
        self.assertFalse(schedule.disable())

    def test_percent_escaped_and_log(self):
        schedule.enable("0 4 * * *", "/c%d.toml", home="/root")
        self.assertIn("c\\%d", schedule.cron_path().read_text())
        schedule.log_path().write_text("\n".join(str(i) for i in range(100)))
        self.assertEqual(schedule.read_log(3), "97\n98\n99")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        got = self.got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.path, json.loads(body)))
                ok = "BAD" not in self.path
                data = json.dumps({"ok": True} if ok else {"ok": False, "description": "Unauthorized"})
                self.send_response(200 if ok else 401)
                self.end_headers()
                self.wfile.write(data.encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        os.environ["PODKOP_SUB_TG_API"] = f"http://127.0.0.1:{self.srv.server_port}"
        self.cfg = Config(Path(tempfile.mkdtemp()) / "c.toml")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("PODKOP_SUB_TG_API", None)

    def test_disabled_without_credentials(self):
        n = Notifier(self.cfg)
        self.assertFalse(n.enabled)
        self.assertFalse(n.send("x"))
        self.assertEqual(self.got, [])

    def test_send_and_switches(self):
        self.cfg["notify"].update(telegram_token="123:ABC", telegram_chat_id="42")
        n = Notifier(self.cfg)
        self.assertTrue(n.send("привет", "change"))
        self.assertEqual(self.got[0][0], "/bot123:ABC/sendMessage")
        self.assertEqual(self.got[0][1]["chat_id"], "42")
        self.cfg["notify"]["on_error"] = False
        self.assertFalse(n.send("err", "error"))
        self.assertEqual(len(self.got), 1)

    def test_bad_token_masked(self):
        self.cfg["notify"].update(telegram_token="BAD", telegram_chat_id="1")
        n = Notifier(self.cfg)
        ok, err = n.test()
        self.assertFalse(ok)
        self.assertNotIn("BAD", err)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()


class CheckSubTests(test_all.CliTests):
    def test_check_sub_writes_nothing_and_reports(self):
        from unittest import mock
        from podkop_sub import probe
        out = []
        before = committed(self.state)
        with mock.patch.object(probe, "tcp_ok", side_effect=lambda h, p, t=5.0: h != "192.0.2.2"):
            cfg = Config(self.cfg_path)
            cfg["sync"]["probe"] = "tcp"
            cfg.save()
            self.assertEqual(cli.check_subscription(cfg, out.append), 0)
        self.assertEqual(committed(self.state), before)
        text = "\n".join(out)
        self.assertIn("На роутер ничего не записано", text)
        self.assertIn("✓", text)
        self.assertIn("✗", text)  # DE не отвечает по TCP
