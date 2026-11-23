"""Telegram webhook'ini qabul qiluvchi qatlam.

Nima uchun kerak: Render Free instansiya ~20 daqiqa kiruvchi so'rovsiz
o'ldiriladi va uxlaganda long-polling (chiquvchi `getUpdates`) ham to'xtaydi —
hech kimga javob ketmaydi. Webhook'da har xabar Telegram'dan KIRUVCHI HTTP
bo'ladi, shuning uchun foydalanuvchining o'zi instansiyanı uyg'otadi.

Bu fayl `aiohttp.test_utils` orqali HAQIQIY so'rov yuboradi: soxta `request`
obyekti header/JSON parse xatolarini yashirib qo'yardi.

Ishga tushirish:  python -m unittest test_webhook -v
"""

import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webhook

SECRET = "topsecret-value"


class Receiver(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.updates = []

    def build(self, feed=None, **kwargs):
        async def capture(update):
            self.updates.append(update)

        return webhook.Receiver(SECRET, feed or capture, **kwargs)

    async def post(self, receiver, *, body=None, secret=SECRET, raw=None):
        app = web.Application()
        app.router.add_post("/telegram/webhook", receiver.handle)
        headers = {webhook.SECRET_HEADER: secret} if secret is not None else {}
        async with TestClient(TestServer(app)) as client:
            if raw is not None:
                response = await client.post("/telegram/webhook", headers=headers, data=raw)
            else:
                response = await client.post("/telegram/webhook", headers=headers, json=body)
            return response.status

    async def drain(self, receiver):
        while receiver.tasks:
            await asyncio.gather(*list(receiver.tasks))

    async def test_valid_secret_passes_the_update_through(self):
        receiver = self.build()
        payload = {"update_id": 11, "message": {"text": "salom"}}

        status = await self.post(receiver, body=payload)
        await self.drain(receiver)

        self.assertEqual(status, 200)
        self.assertEqual(self.updates, [payload])

    async def test_wrong_secret_is_refused_and_nothing_is_fed(self):
        receiver = self.build()

        status = await self.post(receiver, body={"update_id": 12}, secret="boshqa-kalit")
        await self.drain(receiver)

        self.assertEqual(status, 403)
        self.assertEqual(self.updates, [])

    async def test_missing_secret_header_is_refused(self):
        receiver = self.build()

        status = await self.post(receiver, body={"update_id": 13}, secret=None)
        await self.drain(receiver)

        self.assertEqual(status, 403)
        self.assertEqual(self.updates, [])

    async def test_retried_update_id_is_processed_only_once(self):
        # Sovuq startda Telegram javobni kutib tura olmaydi va bir xabarini
        # qayta yuboradi — takrorlanmasa foydalanuvchiga ikkita javob ketardi.
        receiver = self.build()
        payload = {"update_id": 14, "message": {"text": "bir marta"}}

        first = await self.post(receiver, body=payload)
        second = await self.post(receiver, body=payload)
        await self.drain(receiver)

        self.assertEqual((first, second), (200, 200))
        self.assertEqual(len(self.updates), 1)

    async def test_reply_returns_before_the_update_is_processed(self):
        gate = asyncio.Event()

        async def slow_feed(update):
            await gate.wait()
            self.updates.append(update)

        receiver = self.build(feed=slow_feed)
        status = await self.post(receiver, body={"update_id": 15})

        self.assertEqual(status, 200)
        self.assertEqual(self.updates, [])

        gate.set()
        await self.drain(receiver)
        self.assertEqual(len(self.updates), 1)

    async def test_malformed_body_is_refused_without_a_crash(self):
        receiver = self.build()

        status = await self.post(receiver, raw="bu json emas", )

        self.assertEqual(status, 400)
        self.assertEqual(self.updates, [])

    async def test_body_without_update_id_is_refused(self):
        # Dedup `update_id`ga tayanadi: yo'q bo'lsa uni hisoblab bo'lmaydi.
        receiver = self.build()

        status = await self.post(receiver, body={"message": {"text": "id yo'q"}})
        await self.drain(receiver)

        self.assertEqual(status, 400)
        self.assertEqual(self.updates, [])

    async def test_feed_failure_is_logged_and_does_not_escape(self):
        async def broken_feed(update):
            raise RuntimeError("handler port qildi")

        receiver = self.build(feed=broken_feed)

        with self.assertLogs(level="WARNING") as logged:
            status = await self.post(receiver, body={"update_id": 16})
            await self.drain(receiver)

        self.assertEqual(status, 200)
        self.assertIn("handler port qildi", "\n".join(logged.output))

    async def test_dedup_ring_forgets_old_ids(self):
        # Xotira cheksiz o'smasligi kerak: chegara tashqarisidagi id qayta
        # ruxsat etiladi (Telegram eskirgan id'ni qayta yubormaydi).
        receiver = self.build(dedup_limit=2)

        for update_id in (21, 22, 23):
            await self.post(receiver, body={"update_id": update_id})
            await self.drain(receiver)
        await self.post(receiver, body={"update_id": 21})
        await self.drain(receiver)

        self.assertEqual([u["update_id"] for u in self.updates], [21, 22, 23, 21])


if __name__ == "__main__":
    unittest.main()
