"""Несколько подписок: миграция конфига, объединение, владельцы ссылок."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_all  # noqa: E402

from podkop_sub import sources  # noqa: E402
from podkop_sub.config import Config, dumps  # noqa: E402
from podkop_sub.links import parse_link, parse_many  # noqa: E402
from podkop_sub.subscription import Subscription, SubscriptionError  # noqa: E402
from test_all import mk  # noqa: E402

NL = mk("NL", "Амстердам", "Нидерланды", "192.0.2.1", sni="a.ru")
DE = mk("DE", "Берлин", "Германия", "192.0.2.2", sni="a.ru")
US = mk("US", "Хьюстон", "США", "192.0.2.3", sni="a.ru")
JP = mk("JP", "Токио", "Япония", "192.0.2.4", sni="a.ru")


def sub(*lines):
    return Subscription(parse_many(list(lines)), {})


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_legacy_single_subscription_becomes_main(self):
        path = self.tmp / "old.toml"
        path.write_text('[subscription]\nurl = "https://x/s"\nuser_agent = "ua/1"\ntimeout = 7\n',
                        encoding="utf-8")
        cfg = Config(path)
        self.assertTrue(cfg.migrated)
        self.assertEqual(cfg.subscriptions, [{"name": "main", "url": "https://x/s", "enabled": True}])
        self.assertEqual(cfg["fetch"]["user_agent"], "ua/1")
        self.assertEqual(cfg["fetch"]["timeout"], 7)
        self.assertNotIn("subscription", cfg.data)

    def test_new_config_roundtrip_with_several_subscriptions(self):
        path = self.tmp / "new.toml"
        cfg = Config(path)
        cfg.subscriptions.extend([
            {"name": "main", "url": "https://a/s", "enabled": True},
            {"name": "backup", "url": 'https://b/"quoted"', "enabled": False},
        ])
        cfg.save()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[[subscriptions]]"), 2)
        again = Config(path)
        self.assertFalse(again.migrated)
        self.assertEqual([s["name"] for s in again.subscriptions], ["main", "backup"])
        self.assertFalse(again.subscriptions[1]["enabled"])
        self.assertEqual(again.subscriptions[1]["url"], 'https://b/"quoted"')

    def test_no_legacy_keys_when_empty(self):
        self.assertNotIn("[[subscriptions]]", dumps(Config(self.tmp / "none.toml").data))

    def test_configured_needs_some_url(self):
        cfg = Config(self.tmp / "c.toml")
        cfg["router"]["host"] = "fake"
        cfg.save()
        self.assertFalse(Config(self.tmp / "c.toml").configured)
        cfg = Config(self.tmp / "c.toml")
        cfg.subscriptions.append({"name": "main", "url": "https://a/s", "enabled": True})
        cfg.save()
        self.assertTrue(Config(self.tmp / "c.toml").configured)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(Path(tempfile.mkdtemp()) / "c.toml")
        self.cfg.subscriptions.extend([{"name": "main", "url": "u1", "enabled": True},
                                       {"name": "backup", "url": "u2", "enabled": True},
                                       {"name": "off", "url": "u3", "enabled": False}])

    def test_first_subscription_wins_and_owner_recorded(self):
        downloaded = {"main": sub(NL), "backup": sub(NL, JP)}
        res = sources.merge(self.cfg, downloaded)
        self.assertEqual(res.ok, ["main", "backup"])
        self.assertEqual([l.host for l in res.raw_links], ["192.0.2.1", "192.0.2.4"])
        self.assertEqual(res.owner_of[parse_link(NL).key], "main")
        self.assertEqual(res.owner_of[parse_link(JP).key], "backup")

    def test_filters_are_shared_across_subscriptions(self):
        self.cfg["filters"]["exclude_countries"] = ["US"]
        self.cfg["filters"]["prefer_countries"] = ["JP"]
        res = sources.merge(self.cfg, {"main": sub(NL, US), "backup": sub(JP, DE)})
        self.assertEqual([l.cc for l in res.links], ["JP", "DE", "NL"])  # предпочтительные первыми, остальные по коду

    def test_failed_subscription_is_reported_not_fatal(self):
        res = sources.merge(self.cfg, {"main": SubscriptionError("HTTP 500"), "backup": sub(JP)})
        self.assertEqual(res.ok, ["backup"])
        self.assertIn("main", res.errors)
        res.require()  # одна работает — не ошибка

    def test_all_failed_raises(self):
        res = sources.merge(self.cfg, {"main": SubscriptionError("x")})
        with self.assertRaises(SubscriptionError):
            res.require()

    def test_disabled_subscriptions_are_not_downloaded(self):
        calls = []

        def fake_fetch(url, ua, timeout):
            calls.append(url)
            return sub(NL)

        with mock.patch.object(sources, "fetch", side_effect=fake_fetch):
            res = sources.fetch_all(self.cfg)
        self.assertEqual(calls, ["u1", "u2"])
        self.assertNotIn("off", res.sources)


class OwnershipTests(unittest.TestCase):
    def test_reconcile_assigns_and_keeps_owners(self):
        owner_of = {parse_link(NL).key: "backup"}
        owners = {parse_link(DE).id: "main"}
        raws = [NL, DE, JP]
        out = sources.reconcile_owners(raws, owners, owner_of, {"main", "backup"})
        self.assertEqual(out[parse_link(NL).id], "backup")   # владелец — тот, кто отдаёт сейчас
        self.assertEqual(out[parse_link(DE).id], "main")     # подписка не скачалась — владелец прежний
        self.assertNotIn(parse_link(JP).id, out)             # ручная ссылка остаётся ручной

    def test_owner_removed_from_config_makes_link_manual(self):
        owners = {parse_link(DE).id: "gone"}
        out = sources.reconcile_owners([DE], owners, {}, {"main"})
        self.assertEqual(out, {})

    def test_links_no_longer_on_router_drop_out(self):
        owners = {parse_link(DE).id: "main"}
        self.assertEqual(sources.reconcile_owners([NL], owners, {}, {"main"}), {})


if __name__ == "__main__":
    unittest.main()
