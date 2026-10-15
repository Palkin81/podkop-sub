"""Проверка логики меню и мастера с заглушкой questionary (ответы заготовлены)."""
import os
import sys
import types
import unittest
from pathlib import Path

# --- заглушка questionary -------------------------------------------------
ANSWERS: list = []
ASKED: list = []


class Choice:
    def __init__(self, title, value=None, checked=False):
        self.title, self.value, self.checked = title, value, checked


class _Prompt:
    def __init__(self, kind, message, choices=None, default=None):
        self.kind, self.message, self.choices, self.default = kind, message, choices, default

    def ask(self):
        ASKED.append((self.kind, self.message))
        ans = ANSWERS.pop(0)
        return ans(self) if callable(ans) else ans


stub = types.ModuleType("questionary")
stub.Choice = Choice
stub.select = lambda message, choices=None, **kw: _Prompt("select", message, choices)
stub.checkbox = lambda message, choices=None, **kw: _Prompt("checkbox", message, choices)
stub.text = lambda message, default="", **kw: _Prompt("text", message, None, default)
stub.confirm = lambda message, default=False, **kw: _Prompt("confirm", message, None, default)
sys.modules["questionary"] = stub

from podkop_sub import cli, ui  # noqa: E402
from podkop_sub.config import Config  # noqa: E402
from podkop_sub.links import parse_link  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import test_all  # noqa: E402


def pick(value):
    return lambda p: next(c.value for c in p.choices if c.value == value)


def default_checked(p):
    return [c.value for c in p.choices if c.checked]


def codes(p_or_values):
    return lambda p: [c.value for c in p.choices if c.value in p_or_values]


class UiFlowTests(test_all.CliTests):
    def setUp(self):
        super().setUp()
        ANSWERS.clear()
        ASKED.clear()
        self.cfg = Config(self.cfg_path)

    def test_flow_update_merge(self):
        ANSWERS[:] = [default_checked, "merge", "ok", False]
        cli.flow_update(self.cfg)
        self.assertEqual(ANSWERS, [])
        # US исключена, JP предпочтителен: JP отмечен заранее, остальные нет
        got = [parse_link(r).cc for r in test_all.committed(self.state)]
        self.assertEqual(got, ["NL", "JP"])  # NL был, JP добавлен; DE не отмечен

    def test_flow_update_back_navigation(self):
        # выбрали → «назад» на шаге действия → снова выбор → действие → «назад» с подтверждения
        ANSWERS[:] = [default_checked, ui.BACK, default_checked, "replace", ui.BACK,
                      "append", "ok", False]
        cli.flow_update(self.cfg)
        self.assertEqual(ANSWERS, [])
        self.assertEqual([parse_link(r).cc for r in test_all.committed(self.state)], ["NL", "JP"])

    def test_flow_update_cancel_writes_nothing(self):
        before = test_all.committed(self.state)
        ANSWERS[:] = [None]
        cli.flow_update(self.cfg)
        self.assertEqual(test_all.committed(self.state), before)

    def test_delete_and_add(self):
        link = test_all.mk("FR", "Париж", "Франция", "192.0.2.9")
        ANSWERS[:] = [link, True, False]
        cli.flow_add(self.cfg)
        self.assertEqual(len(test_all.committed(self.state)), 2)
        ANSWERS[:] = [lambda p: [p.choices[0].value], True]
        cli.flow_delete(self.cfg)
        self.assertEqual([parse_link(r).cc for r in test_all.committed(self.state)], ["FR"])

    def test_wizard_full_and_back(self):
        key = self.tmp / "key"
        key.write_text("x")
        cfg = Config(self.tmp / "fresh.toml")
        cfg["router"]["key_path"] = str(key)
        url = f"http://127.0.0.1:{self.srv.server_port}/s/x"
        ANSWERS[:] = [
            url,                         # подписка
            "fake", "22", "root",        # роутер
            None,                        # Ctrl+C на шаге «секция»?? нет — ниже
        ]
        # шаги: subscription, router(host, port, user), section(select), types, exclude, prefer
        ANSWERS[:] = [
            "ru",                        # язык интерфейса
            url,
            "fake", "22", "root",
            "Proxy",                     # секция
            ["vless"],                   # типы
            None,                        # исключения: Ctrl+C → назад на типы
            ["vless"],                   # типы ещё раз
            codes({"US"}),               # исключения
            codes({"JP"}),               # предпочтения
            default_checked,             # защищённые ссылки
        ]
        self.assertTrue(cli.wizard(cfg))
        self.assertEqual(ANSWERS, [])
        saved = Config(self.tmp / "fresh.toml")
        self.assertEqual(saved["filters"]["exclude_countries"], ["US"])
        self.assertEqual(saved["filters"]["prefer_countries"], ["JP"])
        self.assertEqual(saved["router"]["section"], "Proxy")
        self.assertEqual(saved["subscription"]["url"], url)
        self.assertTrue(saved.configured)
        self.assertTrue(saved["protect"]["reviewed"])

    def test_wizard_cancel_restores_config(self):
        cfg = Config(self.tmp / "none.toml")
        ANSWERS[:] = [None]
        self.assertFalse(cli.wizard(cfg))
        self.assertFalse((self.tmp / "none.toml").exists())

    def test_main_menu_navigation(self):
        ANSWERS[:] = [
            "podkop", "status", ui.BACK,   # Podkop → статус → назад
            "links", "show", ui.BACK,      # Ссылки → показать → назад
            "exit",
        ]
        os.environ["PODKOP_SUB_INPUT"] = "1"
        import builtins
        orig = builtins.input
        builtins.input = lambda *a: ""
        try:
            cli.main_menu(self.cfg)
        finally:
            builtins.input = orig
        self.assertEqual(ANSWERS, [])

    def test_settings_toggle(self):
        ANSWERS[:] = ["emoji", ui.BACK]
        cli.settings_menu(self.cfg)
        self.assertFalse(Config(self.cfg_path)["ui"]["show_emoji"])


if __name__ == "__main__":
    unittest.main()
