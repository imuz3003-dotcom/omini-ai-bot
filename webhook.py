"""Telegram webhook'ini qabul qilish — `getUpdates` o'rniga kiruvchi HTTP.

Xavfsizlik: Telegram har bir so'rovni biz bergan kalit bilan
`X-Telegram-Bot-Api-Secret-Token` sarlavhasida qaytaradi. Kalit mos
kelmasa update dispatcher'ga yetmaydi — aks holda istalgan kishi soxta
xabar yuborib admin buyruqlarini taqlid qilardi (bot id hujjatda ochiq).

Tayming: Telegram 60 sekunddan keyin "webhook ishlamayapti" deb uni
o'chirib qo'yadi, AI javobi esa shundan uzoq vaqt olishi mumkin. Shuning
uchun javob darhol qaytariladi, ish orqa fonda ketadi.
"""

import asyncio
import json
import logging
from collections import deque

from aiohttp import web

import devicehub

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
DEDUP_LIMIT = 512


class Receiver:
    """Bitta kirish nuqtasi: kalitni tekshiradi, takrorlari tashlaydi, ishini
    orqaga tashlaydi. `feed` — update dict'ini oladigan koroutin."""

    def __init__(self, secret, feed, dedup_limit=DEDUP_LIMIT):
        self.secret = str(secret)
        self.feed = feed
        self.tasks = set()
        self._seen = deque(maxlen=max(1, int(dedup_limit)))
        self._ids = set()

    def _is_new(self, update_id) -> bool:
        if update_id in self._ids:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._ids.discard(self._seen[0])
        self._seen.append(update_id)
        self._ids.add(update_id)
        return True

    def _spawn(self, payload):
        task = asyncio.create_task(self._run(payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, payload):
        try:
            await self.feed(payload)
        except Exception as error:
            # Javob allaqachon yuborilgan, shuning uchun xato faqat logga.
            logging.warning("Webhook update ishlanmadi: %s", error)

    async def handle(self, request):
        if not devicehub.binds_match(request.headers.get(SECRET_HEADER, ""), self.secret):
            return web.Response(status=403)
        try:
            payload = json.loads(await request.text())
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(payload, dict) or "update_id" not in payload:
            return web.Response(status=400)
        if self._is_new(payload["update_id"]):
            self._spawn(payload)
        return web.Response(status=200)
