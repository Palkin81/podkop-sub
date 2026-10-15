"""Языки интерфейса: полнота перевода и отсутствие русского в английском режиме."""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_all  # noqa: E402

from podkop_sub import help as help_mod, schedule  # noqa: E402
from podkop_sub.i18n import get_language, set_language, tr  # noqa: E402
from podkop_sub.i18n_ru import RU  # noqa: E402

PKG = Path(__file__).resolve().parent.parent / "podkop_sub"
CYR = re.compile(r"[А-Яа-яЁё]")


def translatable_keys():
    keys = []
    for f in sorted(PKG.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.append(node.args[0].value)
    return list(dict.fromkeys(keys))


class I18nTests(unittest.TestCase):
    def tearDown(self):
        set_language("ru")

    def test_every_key_has_russian_translation(self):
        missing = [k for k in translatable_keys() if k not in RU]
        self.assertEqual(missing, [])

    def test_no_orphan_translations(self):
        keys = set(translatable_keys())
        self.assertEqual([k for k in RU if k not in keys], [])

    def test_english_is_default_key_and_russian_is_translation(self):
        set_language("ru")
        self.assertEqual(tr("Status"), "Статус")
        set_language("en")
        self.assertEqual(tr("Status"), "Status")
        self.assertEqual(get_language(), "en")

    def test_unknown_language_falls_back_to_russian(self):
        set_language("xx")
        self.assertEqual(get_language(), "ru")

    def test_format_placeholders_work_in_both_languages(self):
        set_language("ru")
        self.assertEqual(tr("Backup: {path}", path="/x"), "Бэкап: /x")
        set_language("en")
        self.assertEqual(tr("Backup: {path}", path="/x"), "Backup: /x")

    def test_english_schedule_descriptions_have_no_cyrillic(self):
        set_language("en")
        for expr in ("7 * * * *", "7 */2 * * *", "0 4 * * *", "0 9 * * 1,5", "1 2 3 4 5"):
            self.assertFalse(CYR.search(schedule.describe(expr)), expr)
        self.assertIn("every day at 04:00", schedule.describe("0 4 * * *"))
        self.assertIn("on Mon, Fri", schedule.describe("0 9 * * 1,5"))

    def test_help_in_both_languages(self):
        set_language("en")
        en = help_mod.help_text()
        self.assertFalse(CYR.search(en))
        self.assertIn("AVAILABILITY", en)
        set_language("ru")
        self.assertIn("ДОСТУПНОСТЬ", help_mod.help_text())



if __name__ == "__main__":
    unittest.main()
