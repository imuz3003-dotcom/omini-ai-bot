#!/usr/bin/env python3
"""render_check.py uchun avtotest (tarmoqsiz, kalitsiz — hammasi mock qilinadi).

Ishga tushirish:
    python -m unittest -v test_render_check
yoki
    python test_render_check.py
"""

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_check  # noqa: E402

SHA = "43123b0" + "0" * 33
SERVICE = [{"service": {"id": "svc-1", "name": "omini-ai-bot",
                        "dashboardUrl": "https://dashboard.render.com/web/svc-1"}}]


class FakeClock:
    """time.sleep haqiqiy kutmaydi — soatni oldinga suradi, shunda --wait tez sinaladi."""

    def __init__(self, start=1_000.0):
        self.now = start
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


def deploy(status, sha=SHA, message="Blok barcha xabar turlariga tarqaladi"):
    return {"deploy": {"id": f"dv-{status}", "status": status, "startedAt": "2026-11-04T08:07:00Z",
                        "commit": {"id": sha, "message": message}}}


class Harness:
    """render_check.api_get o'rniga stsenariy bo'yicha javob beradigan qopqon."""

    def __init__(self, statuses, services=None, error=None, pages=None):
        self.statuses = list(statuses)      # har bir poll uchun keyingi javob
        self.services = SERVICE if services is None else services
        self.error = error                  # (json_xato, matn) ko'rinishida
        self.pages = pages                  # /services uchun sahifalar (cursor pagination)
        self.deploy_calls = 0
        self.service_calls = 0
        self.requests = []
        self.clock = FakeClock()

    def api_get(self, path, key, params=None):
        self.requests.append((path, key, params))
        if self.error:
            return None, self.error
        if path == "/services":
            self.service_calls += 1
            pages = self.pages if self.pages is not None else [self.services]
            cursor = (params or {}).get("cursor")
            idx = int(cursor.split("-")[-1]) if cursor and cursor.startswith("cursor-") else 0
            idx = min(idx, len(pages) - 1)
            page = pages[idx]
            if idx + 1 < len(pages):
                return [{**item, "cursor": f"cursor-{idx + 1}"} for item in page], None
            return page, None
        self.deploy_calls += 1
        status = self.statuses[min(self.deploy_calls - 1, len(self.statuses) - 1)]
        return [deploy(status)], None


class RenderCheckTest(unittest.TestCase):
    def setUp(self):
        self.real_api_get = render_check.api_get
        self.real_time = render_check.time
        self.real_head = render_check.local_head
        os.environ.pop("RENDER_API_KEY", None)

    def tearDown(self):
        render_check.api_get = self.real_api_get
        render_check.time = self.real_time
        render_check.local_head = self.real_head
        os.environ.pop("RENDER_API_KEY", None)

    def run_cli(self, argv, harness, key="rnd_test", head=None):
        """main()ni chaqiradi; (chiqish_kodi, stdout, stderr) qaytaradi."""
        render_check.api_get = harness.api_get
        render_check.time = harness.clock
        render_check.local_head = head if head is not None else (lambda: None)
        if key is not None:
            os.environ["RENDER_API_KEY"] = key
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render_check.main(argv)
        return code, out.getvalue(), err.getvalue()

    # ------------------------------------------------------------ asosiy holatlar

    def test_live_ok_teskari_kutilgan_commit_bilan_mos(self):
        harness = Harness(["live"])
        code, out, _ = self.run_cli(["--expect", SHA[:7]], harness)
        self.assertEqual(code, 0, out)
        self.assertIn("Deploy tugagan", out)
        self.assertIn("MOS", out)
        self.assertEqual(harness.deploy_calls, 1)

    def test_live_lekin_commit_boshqa(self):
        harness = Harness(["live"])
        code, out, _ = self.run_cli(["--expect", "deadbeef123"], harness)
        self.assertEqual(code, 1, out)
        self.assertIn("MOS EMAS", out)

    def test_build_failed_kod_2(self):
        harness = Harness(["build_failed"])
        code, out, _ = self.run_cli(["--expect", SHA[:7]], harness)
        self.assertEqual(code, 2, out)
        self.assertIn("muvaffaqiyatsiz", out)

    # ------------------------------------------------------------ --wait xulqi

    def test_wait_pendingdan_keyin_live(self):
        harness = Harness(["build_in_progress", "update_in_progress", "live"])
        code, out, _ = self.run_cli(["--wait", "--interval", "20", "--timeout", "600",
                                     "--expect", SHA[:7]], harness)
        self.assertEqual(code, 0, out)
        self.assertEqual(harness.deploy_calls, 3)
        self.assertEqual(harness.clock.slept, 40)  # 2 marta uxlagan
        self.assertIn("qayta tekshiriladi", out)

    def test_wait_deadlinidan_keyin_toxtaydi(self):
        harness = Harness(["build_in_progress"])
        code, out, _ = self.run_cli(["--wait", "--interval", "30", "--timeout", "120",
                                     "--expect", SHA[:7]], harness)
        self.assertEqual(code, 3, out)
        self.assertLess(harness.deploy_calls, 12, "wait cheksizpolling qilmaydi")
        self.assertGreaterEqual(harness.deploy_calls, 2, "kamida bir marta qayta so'raladi")
        self.assertIn("hali yakunlanmagan", out)

    def test_wait_xatoda_kutmaydi(self):
        harness = Harness(["canceled"])
        code, _, _ = self.run_cli(["--wait", "--timeout", "600", "--expect", SHA[:7]], harness)
        self.assertEqual(code, 2)
        self.assertEqual(harness.deploy_calls, 1)
        self.assertEqual(harness.clock.slept, 0)

    def test_wait_api_xatosi_davom_etmaydi(self):
        harness = Harness(["live"], error="HTTP 401: kalit noto'g'ri yoki eskirgan")
        code, _, err = self.run_cli(["--wait", "--expect", SHA[:7]], harness)
        self.assertEqual(code, 3)
        self.assertIn("401", err)

    # ------------------------------------------------------------ sozlamalar/xatolar

    def test_kalit_bo_lmasa_so_rov_qilmaydi(self):
        harness = Harness(["live"])
        code, _, err = self.run_cli([], harness, key=None)
        self.assertEqual(code, 3)
        self.assertIn("RENDER_API_KEY", err)
        self.assertEqual(harness.requests, [])

    def test_servis_topilmadi(self):
        harness = Harness(["live"], services=[])
        code, _, err = self.run_cli(["--service", "omini-ai-bot"], harness)
        self.assertEqual(code, 3)
        self.assertIn("topilmadi", err)

    def test_cursor_pagination(self):
        pages = [
            [{"service": {"id": "svc-a", "name": "boshqa-servis"}}],
            [{"service": {"id": "svc-1", "name": "omini-ai-bot", "dashboardUrl": "https://x"}}],
        ]
        harness = Harness(["live"], pages=pages)
        code, out, _ = self.run_cli(["--expect", SHA[:7]], harness)
        self.assertEqual(code, 0, out)
        self.assertEqual(harness.service_calls, 2)
        cursors = [p[2].get("cursor") for p in harness.requests if p[0] == "/services"]
        self.assertEqual(cursors, [None, "cursor-1"])

    # ------------------------------------------------------------ chiqarish formati

    def test_json_chiqishi(self):
        harness = Harness(["live"])
        code, out, _ = self.run_cli(["--expect", SHA[:7], "--json"], harness)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(sorted(data), ["deploys", "service"])
        self.assertEqual(data["service"]["name"], "omini-ai-bot")
        self.assertEqual(data["deploys"][0]["commit"]["id"], SHA)

    def test_lokal_head_kutilgan_commit_sifatsida(self):
        harness = Harness(["live"])
        code, out, _ = self.run_cli([], harness, head=lambda: SHA)
        self.assertEqual(code, 0, out)
        self.assertIn("MOS", out)

    def test_help_chiqadi(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as stop:
            render_check.main(["--help"])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn("--wait", buf.getvalue())

    def test_tarmoqqa_chiqmaydi(self):
        """Butun suite mobaynida haqiqiy HTTP bo'lmasligi kerak: api_get doim qopqonda."""
        self.assertEqual(self.real_api_get, render_check.api_get)
        harness = Harness(["live"])
        self.run_cli(["--expect", SHA[:7]], harness)
        self.assertTrue(all(call[1] == "rnd_test" for call in harness.requests))


if __name__ == "__main__":
    unittest.main(verbosity=2)
