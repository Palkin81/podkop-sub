import base64
import http.server
import json
import os
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from podkop_sub import cli
from podkop_sub.config import Config, dumps
from podkop_sub.links import (apply_filters, compute, countries, diff, normalize,
                              parse_link, parse_many)
from podkop_sub.router import Router, parse_status
from podkop_sub.subscription import SubscriptionError, decode_body

FAKEBIN = str(Path(__file__).parent / "fakebin")
UUID = "00000000-0000-0000-0000-000000000000"


def flag(cc):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)


def mk(cc, city, country, host="192.0.2.1", sni="a.example.ru", sid="aa", sep="?", port=443):
    name = quote(f"{flag(cc)} {city}, {country}, Extra")
    return (f"vless://{UUID}@{host}:{port}{sep}security=reality&encryption=none&fp=firefox"
            f"&type=tcp&flow=xtls-rprx-vision&sni={sni}&pbk=KEY&sid={sid}#{name}")


SAMPLE = [
    mk("NL", "Амстердам", "Нидерланды", "192.0.2.1"),
    mk("DE", "Берлин", "Германия", "192.0.2.2"),
    mk("US", "Хьюстон", "США", "192.0.2.3"),
    mk("US", "Майами", "США", "192.0.2.3"),
    mk("JP", "Токио", "Япония", "192.0.2.4"),
]


class LinkTests(unittest.TestCase):
    def test_parse_fields(self):
        l = parse_link(SAMPLE[0])
        self.assertEqual((l.scheme, l.host, l.port, l.cc, l.city, l.country),
                         ("vless", "192.0.2.1", "443", "NL", "Амстердам", "Нидерланды"))
        self.assertEqual(l.userinfo, UUID)
        self.assertIn("/?security=reality", l.raw)
        self.assertEqual(l.label(True).split()[0], flag("NL"))
        self.assertEqual(l.label(False), "[NL] Амстердам, Нидерланды")

    def test_normalize_idempotent(self):
        once = normalize(SAMPLE[0])
        self.assertEqual(normalize(once), once)
        self.assertIn(":443/?", once)
        self.assertEqual(normalize("vless://u@h:1/?a=b#x"), "vless://u@h:1/?a=b#x")

    def test_rotating_sni_same_key(self):
        a = parse_link(mk("NL", "Амстердам", "Нидерланды", sni="x.ru", sid="01"))
        b = parse_link(mk("NL", "Амстердам", "Нидерланды", sni="y.ru", sid="02"))
        self.assertEqual(a.key, b.key)
        self.assertEqual(len(parse_many([a.raw, b.raw])), 1)

    def test_same_host_different_city_not_duplicate(self):
        self.assertEqual(len(parse_many(SAMPLE[2:4])), 2)

    def test_filters_and_countries(self):
        links = parse_many(SAMPLE)
        out = apply_filters(links, ["vless"], ["US"], ["JP"])
        self.assertEqual([l.cc for l in out], ["JP", "DE", "NL"])
        self.assertEqual([(c, n) for c, _, _, n in countries(links)],
                         [("DE", 1), ("JP", 1), ("NL", 1), ("US", 2)])

    def test_compute_actions(self):
        links = parse_many(SAMPLE)
        cur = [mk("NL", "Амстердам", "Нидерланды", sni="old.ru"),
               "socks5://log:pass@10.0.0.1:1080"]
        sel = [links[0], links[1]]
        self.assertEqual(compute(cur, sel, "replace"), [links[0].raw, links[1].raw])
        app = compute(cur, sel, "append")
        self.assertEqual(app[0], cur[0])
        self.assertEqual(app[-1], links[1].raw)
        mer = compute(cur, sel, "merge")
        self.assertEqual(mer, [links[0].raw, cur[1], links[1].raw])
        d = diff(cur, mer)
        self.assertEqual((len(d["added"]), len(d["updated"]), len(d["removed"])), (1, 1, 0))

    def test_vmess(self):
        payload = base64.b64encode(json.dumps(
            {"ps": f"{flag('FR')} Париж, Франция", "add": "h.example", "port": 8443, "id": "u"}).encode()).decode()
        l = parse_link("vmess://" + payload)
        self.assertEqual((l.cc, l.host, l.port, l.city), ("FR", "h.example", "8443", "Париж"))


class DecodeTests(unittest.TestCase):
    def test_base64_variants(self):
        text = "\n".join(SAMPLE)
        raw = base64.b64encode(text.encode())
        self.assertEqual(len(decode_body(raw)), 5)
        self.assertEqual(len(decode_body(raw.rstrip(b"="))), 5)
        self.assertEqual(len(decode_body(base64.urlsafe_b64encode(text.replace("\n", "\r\n").encode()))), 5)
        self.assertEqual(len(decode_body(text.encode())), 5)

    def test_garbage(self):
        with self.assertRaises(SubscriptionError):
            decode_body(b"<html>blocked</html>")
        with self.assertRaises(SubscriptionError):
            decode_body(b"")


def init_fake(tmp: Path, links):
    state = tmp / "uci"
    state.mkdir()
    (state / "committed.json").write_text(json.dumps({
        "main": {"selector_proxy_links": ["socks5://log:pass@10.0.0.9:1080"], "mixed_proxy_port": "0"},
        "Proxy": {"selector_proxy_links": links, "mixed_proxy_port": "2080"},
    }, ensure_ascii=False), encoding="utf-8")
    conf = tmp / "podkop"
    conf.write_text("config section 'Proxy'\n\tlist selector_proxy_links 'x'\n", encoding="utf-8")
    os.environ.update(FAKE_UCI_DIR=str(state), PODKOP_SUB_CONFIG_FILE=str(conf),
                      PODKOP_SUB_SSH=f"{FAKEBIN}/ssh", PODKOP_SUB_BIN=f"{FAKEBIN}/podkop",
                      PATH=FAKEBIN + os.pathsep + os.environ["PATH"])
    return state, conf


def committed(state: Path, sec="Proxy"):
    return json.loads((state / "committed.json").read_text(encoding="utf-8"))[sec]["selector_proxy_links"]


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in ("uci", "ssh"):
            os.chmod(f"{FAKEBIN}/{f}", 0o755)
        self.state, self.conf = init_fake(self.tmp, [mk("NL", "Амстердам", "Нидерланды")])
        self.router = Router("fake", section="Proxy")

    def test_roundtrip_special_chars(self):
        tricky = [normalize(mk("DE", "Берлин", "Германия", sid="a&b%20c")),
                  normalize(mk("JP", "Токио", "Япония")) + "'quote"]
        self.router.set_links(tricky)
        self.assertEqual(self.router.get_links(), tricky)
        self.assertEqual(committed(self.state), tricky)

    def test_other_sections_untouched(self):
        self.router.set_links([SAMPLE[1]])
        self.assertEqual(committed(self.state, "main"), ["socks5://log:pass@10.0.0.9:1080"])
        self.assertEqual(self.router.sections(), ["main", "Proxy"])

    def test_empty_get(self):
        self.router.set_links([])
        self.assertEqual(self.router.get_links(), [])

    def test_backup_restore_and_rotation(self):
        paths = [self.router.backup(self.tmp / "bk", keep=2) for _ in range(1)]
        self.assertTrue(paths[0].exists())
        self.assertEqual(oct(paths[0].stat().st_mode & 0o777), "0o600")
        self.conf.write_text("config section 'Proxy'\n\tlist selector_proxy_links 'changed'\n")
        self.router.restore(paths[0])
        self.assertIn("'x'", self.conf.read_text())

    def test_status_from_podkop_not_initd(self):
        st = self.router.sing_box_status()
        self.assertEqual(st["running"], 1)
        text = cli.describe_status(self.router)
        self.assertIn("sing-box: запущен, автозапуск включён", text)
        self.assertIn("DNS настроен под podkop: да", text)
        self.assertIn("1.12.4", text)

    def test_parse_status_variants(self):
        self.assertEqual(parse_status('noise {"running":0,"enabled":"1"} tail')["enabled"], "1")
        self.assertIsNone(parse_status("not json"))
        self.assertIsNone(parse_status("{broken"))

    def test_status_fallback_when_podkop_cli_missing(self):
        os.environ["PODKOP_SUB_BIN"] = "/nonexistent/podkop"
        text = cli.describe_status(Router("fake", section="Proxy"))
        self.assertIn("Не удалось получить статус", text)

    def test_logs_command_runs(self):
        self.assertIsInstance(self.router.logs(5), str)

    def test_bad_section(self):
        with self.assertRaises(Exception):
            Router("fake", section="a; rm -rf /")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in ("uci", "ssh"):
            os.chmod(f"{FAKEBIN}/{f}", 0o755)
        self.state, self.conf = init_fake(self.tmp, [mk("NL", "Амстердам", "Нидерланды", sni="old.ru")])
        counter = {"n": 0}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                counter["n"] += 1
                body = base64.b64encode("\n".join(
                    mk(cc, city, ctry, host, sni=f"s{counter['n']}.ru", sid=f"{counter['n']:02x}")
                    for cc, city, ctry, host in
                    [("NL", "Амстердам", "Нидерланды", "192.0.2.1"), ("DE", "Берлин", "Германия", "192.0.2.2"),
                     ("US", "Хьюстон", "США", "192.0.2.3"), ("JP", "Токио", "Япония", "192.0.2.4")]).encode())
                self.send_response(200)
                self.send_header("subscription-userinfo", "upload=1; download=2; total=1073741824; expire=1893456000")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        cfg = Config(self.tmp / "config.toml")
        cfg.subscriptions.append({"name": "main", "url": f"http://127.0.0.1:{self.srv.server_port}/s/x",
                                  "enabled": True})
        cfg["router"]["host"] = "fake"
        cfg["filters"]["exclude_countries"] = ["US"]
        cfg["filters"]["prefer_countries"] = ["JP"]
        cfg["backup"]["dir"] = str(self.tmp / "bk")
        cfg.save()
        self.cfg_path = str(self.tmp / "config.toml")

    def tearDown(self):
        self.srv.shutdown()

    def test_config_roundtrip_and_perms(self):
        cfg = Config(self.cfg_path)
        self.assertEqual(cfg["filters"]["exclude_countries"], ["US"])
        self.assertEqual(oct(Path(self.cfg_path).stat().st_mode & 0o777), "0o600")
        self.assertTrue(cfg.configured)

    def test_dry_run_changes_nothing(self):
        before = committed(self.state)
        self.assertEqual(cli.main(["--config", self.cfg_path, "--update", "--dry-run"]), 0)
        self.assertEqual(committed(self.state), before)

    def test_update_replace_excludes_us_and_sorts_preferred_first(self):
        self.assertEqual(cli.main(["--config", self.cfg_path, "--update", "--action", "replace"]), 0)
        got = [parse_link(r).cc for r in committed(self.state)]
        self.assertEqual(got, ["JP", "DE", "NL"])
        self.assertEqual(len(list((self.tmp / "bk").glob("podkop-*.conf"))), 1)

    def test_update_merge_refreshes_rotated_sni_without_duplicates(self):
        self.assertEqual(cli.main(["--config", self.cfg_path, "--update", "--action", "merge"]), 0)
        links = committed(self.state)
        self.assertEqual(len(links), 3)  # NL обновлён, DE и JP добавлены
        self.assertNotIn("old.ru", links[0])
        cli.main(["--config", self.cfg_path, "--update", "--action", "merge"])
        self.assertEqual(len(committed(self.state)), 3)

    def test_only_preferred_and_rollback(self):
        self.assertEqual(cli.main(["--config", self.cfg_path, "--update", "--only-preferred"]), 0)
        self.assertEqual([parse_link(r).cc for r in committed(self.state)], ["JP"])
        self.assertEqual(cli.main(["--config", self.cfg_path, "--rollback"]), 0)
        self.assertIn("'x'", self.conf.read_text())

    def test_list_command(self):
        self.assertEqual(cli.main(["--config", self.cfg_path, "--list"]), 0)


if __name__ == "__main__":
    unittest.main()
