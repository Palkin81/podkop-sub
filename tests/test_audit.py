"""Исправления по результатам аудита: мастер, типы, названия шагов, known_hosts, resolve_url."""
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# общий модуль с заглушкой questionary: иначе очередь ANSWERS окажется в другой копии модуля
from tests import test_all, test_ui  # noqa: E402

from podkop_sub import cli  # noqa: E402
from podkop_sub.config import Config  # noqa: E402
from podkop_sub.i18n import set_language  # noqa: E402
from podkop_sub.router import Router  # noqa: E402
from podkop_sub.subscription import resolve_url  # noqa: E402

ANSWERS, ASKED, pick = test_ui.ANSWERS, test_ui.ASKED, test_ui.pick


class ResolveUrlTests(unittest.TestCase):
    def test_plain_https_is_unchanged(self):
        self.assertEqual(resolve_url("https://example.com/sub?token=abc"), "https://example.com/sub?token=abc")

    def test_wrapped_in_query_is_unwrapped(self):
        self.assertEqual(resolve_url("https://short.example/?url=https%3A%2F%2Freal.example%2Fsub"),
                         "https://real.example/sub")

    def test_deep_link_is_unwrapped(self):
        self.assertEqual(resolve_url("incy://add/https://real.example/sub"), "https://real.example/sub")

    def test_non_url_param_is_not_touched(self):
        self.assertEqual(resolve_url("https://x.example/sub?sub=1"), "https://x.example/sub?sub=1")


class RouterKnownHostsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.args = self.tmp / "args.txt"
        self.fake = self.tmp / "ssh"
        self.fake.write_text(f'#!/bin/sh\necho "$@" > {self.args}\n', encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def _ssh_args(self, known):
        Router("h", ssh_bin=str(self.fake), known_hosts=known).test()
        return self.args.read_text()

    def test_confirmed_key_is_strict(self):
        kh = self.tmp / "known_hosts"
        kh.write_text("h ssh-ed25519 AAAA\n")
        args = self._ssh_args(str(kh))
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn(f"UserKnownHostsFile={kh}", args)

    def test_without_saved_key_keeps_accept_new(self):
        args = self._ssh_args(str(self.tmp / "missing"))
        self.assertIn("StrictHostKeyChecking=accept-new", args)


class AuditWizardTests(test_all.CliTests):
    def setUp(self):
        super().setUp()
        ANSWERS.clear()
        ASKED.clear()
        set_language("ru")

    def tearDown(self):
        set_language("ru")

    def test_failed_url_is_not_kept_after_skip(self):
        cfg = Config(self.cfg_path)
        cfg.data["subscriptions"] = []
        ANSWERS[:] = ["http://127.0.0.1:1/unreachable", pick("skip")]
        self.assertEqual(cli.step_subscription(cfg, {"fetched": None}), "next")
        self.assertEqual(cfg.subscriptions, [])
        self.assertFalse(cfg.configured)

    def test_failed_url_restores_previous_on_skip(self):
        cfg = Config(self.cfg_path)
        previous = cfg.subscriptions[0]["url"]
        ANSWERS[:] = ["http://127.0.0.1:1/unreachable", pick("skip")]
        self.assertEqual(cli.step_subscription(cfg, {"fetched": None}), "next")
        self.assertEqual(cfg.subscriptions[0]["url"], previous)

    def test_types_need_at_least_one(self):
        cfg = Config(self.cfg_path)
        ANSWERS[:] = [[], ["vless"]]
        self.assertEqual(cli.step_types(cfg, {"fetched": None}), "next")
        self.assertEqual(cfg["filters"]["types"], ["vless"])
        self.assertEqual(ANSWERS, [])

    def test_step_names_follow_the_chosen_language(self):
        set_language("en")
        self.assertEqual(cli.wizard_step_names()["step_language"], "Language")
        set_language("ru")
        self.assertEqual(cli.wizard_step_names()["step_language"], "Язык")


if __name__ == "__main__":
    unittest.main()
