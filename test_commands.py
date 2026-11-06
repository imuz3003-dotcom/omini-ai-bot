"""Buyruqlar dispatcher DARAJASIDA tekshiriladi (dp.feed_update).

Nima uchun kerak: aiogram handlerlarni ro'yxatga olingan tartibda tekshiradi.
Avval oddiy matn handleri (F.text -> ai_chat) admin buyruqlaridan oldin turgani
uchun /unban, /user, /ban, /stats, /broadcast, /sites "Bunday buyruq yo'q" deb
javob olardi — admin bloklangan odamni chiQola olmay qolgandi. Bu test shu
xatoning qayta paydo bo'lmasligini kafolatlaydi.

Ishga tushirish:  python -m unittest test_commands -v
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

os.environ["BOT_TOKEN"] = os.environ.get("BOT_TOKEN", "123456:AAtest-token")
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "test-key")
_tmpdir = tempfile.mkdtemp(prefix="omini_test_cmd_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "bot_data.db")
os.environ["LOG_FILE"] = ""
os.environ["ADMIN_ID"] = "1111"
os.environ["PROFANITY_MODE"] = "block"
os.environ["PROFANITY_BLOCK_AFTER"] = "2"
os.environ["PROFANITY_BAN_HOURS"] = "0"
os.environ["PROFANITY_WORDS"] = ""
os.environ["DAILY_LIMIT"] = "150"
os.environ["RATE_LIMIT_COUNT"] = "100"  # testda spam limiti aralashmasligi uchun

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as app  # noqa: E402
from aiogram import types  # noqa: E402

ADMIN = app.ADMIN_ID
USER = 7896336878  # foydalanuvchi sinovdan o'tkazgan hisob (noto'g'ri bloklangan edi)


class FakeSession:
    """Telegram'ga chiqadigan har bir so'rovni yozib qo'yadi, hech qayoqqa bormaydi."""

    def __init__(self):
        self.calls = []
        self._mid = 1000

    async def __call__(self, bot, method, timeout=None):
        name = type(method).__name__
        payload = {
            field: getattr(method, field)
            for field in type(method).model_fields
            if getattr(method, field) is not None
        }
        self.calls.append((name, payload))
        if name == "SendMessage":
            self._mid += 1
            return {
                "message_id": self._mid,
                "date": int(time.time()),
                "chat": {"id": payload.get("chat_id", 1), "type": payload.get("chat_type", "private")},
                "from": {"id": 123456, "is_bot": True, "first_name": "omini"},
            }
        if name in ("DeleteMessage", "BanChatMember", "AnswerCallbackQuery", "SendChatAction",
                    "EditMessageText", "DeleteChatMessage"):
            return True
        return {"ok": True}

    def texts(self, chat_id=None):
        return [
            p.get("text", "")
            for name, p in self.calls
            if name == "SendMessage" and (chat_id is None or p.get("chat_id") == chat_id)
        ]

    def names(self):
        return [name for name, _ in self.calls]


def update_of(text=None, user_id=USER, chat_id=None, chat_type="private", caption=None):
    chat_id = chat_id if chat_id is not None else user_id
    message = {
        "message_id": 1,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test", "username": "tester"},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
        message["photo"] = [
            {"file_id": "f", "file_unique_id": "u", "width": 10, "height": 10},
        ]
    return {"update_id": int(time.time() * 1000) % 10_000_000, "message": message}


class CommandRouting(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        app.bot.session = self.session
        self.ai_calls = []

        async def fake_ask_ai(message, prompt, user_id, markup=None):
            self.ai_calls.append(prompt)

        self._real_ask_ai = app.ask_ai
        app.ask_ai = fake_ask_ai
        self._real_lock = app.core.lock_for
        app.core.lock_for = lambda uid: asyncio.Lock()

    def tearDown(self):
        app.ask_ai = self._real_ask_ai
        app.core.lock_for = self._real_lock
        app.core.store.unban(USER)
        app.core.store.reset_strikes(USER)

    def feed(self, payload):
        update = types.Update.model_validate(payload, context={"bot": app.bot})
        asyncio.run(app.dp.feed_update(app.bot, update))

    def to_admin(self, text):
        self.feed(update_of(text, user_id=ADMIN, chat_id=ADMIN))

    def sent_to(self, chat_id):
        return self.session.texts(chat_id)

    # ---------------------------------------------------------------- admin buyruqlari

    def test_unban_works_and_clears_ban_and_strikes(self):
        app.core.store.touch_user(USER, username="tester", first_name="Test")
        app.core.store.ban_user(USER, hours=None)
        app.core.store.record_strike(USER)
        self.assertTrue(app.core.store.is_banned(USER))
        self.session.calls.clear()

        self.to_admin(f"/unban {USER}")

        self.assertFalse(app.core.store.is_banned(USER), "/unban ishlamadi — blok qolib ketdi")
        self.assertEqual(app.core.store.strikes(USER), 0, "/unban zarbalarni tozalamadi")
        joined = " ".join(self.sent_to(ADMIN))
        self.assertIn("blokdan chiqarildi", joined, f"/unban javobi yo'q: {joined!r}")
        self.assertNotIn("Bunday buyruq yo'q", joined)

    def test_user_panel_command_replies(self):
        app.core.store.touch_user(USER, username="tester", first_name="Test")
        self.session.calls.clear()
        self.to_admin(f"/user {USER}")
        joined = " ".join(self.sent_to(ADMIN))
        self.assertIn(str(USER), joined)
        self.assertIn("Haqorat zarbalari", joined)
        self.assertNotIn("Bunday buyruq yo'q", joined)

    def test_ban_command_replies(self):
        self.session.calls.clear()
        self.to_admin(f"/ban {USER}")
        self.assertTrue(app.core.store.is_banned(USER))
        self.assertIn("bloklandi", " ".join(self.sent_to(ADMIN)))
        app.core.store.unban(USER)

    def test_other_admin_commands_reach_handlers(self):
        for command, needle in (
            ("/stats", "Bot statistikasi"),
            ("/sites", "Muslimbekning loyihalari"),
            ("/users", "Foydalanuvchilar"),
            ("/help", "Intizom"),
        ):
            with self.subTest(command=command):
                self.session.calls.clear()
                self.to_admin(command)
                self.assertIn(needle, " ".join(self.sent_to(ADMIN)), command)
                self.assertNotIn("Bunday buyruq yo'q", " ".join(self.sent_to(ADMIN)), command)

    def test_unknown_command_still_answered(self):
        self.session.calls.clear()
        self.to_admin("/ foobar")  # noto'g'ri buyruq
        self.to_admin("/notacommand")
        self.assertIn("Bunday buyruq yo'q", " ".join(self.sent_to(ADMIN)))

    def test_admin_only_for_others(self):
        self.session.calls.clear()
        self.feed(update_of("/stats", user_id=USER, chat_id=USER))
        self.assertIn("faqat admin uchun", " ".join(self.sent_to(USER)))

    # ---------------------------------------------------------------- AI yo'li

    def test_plain_text_goes_to_ai(self):
        self.session.calls.clear()
        self.feed(update_of("salom, qalaysiz?", user_id=USER, chat_id=USER))
        self.assertEqual(self.ai_calls, ["salom, qalaysiz?"])

    def test_user_cannot_trigger_ai_with_a_command(self):
        self.session.calls.clear()
        self.feed(update_of("/ping", user_id=USER, chat_id=USER))
        self.assertEqual(self.ai_calls, [])

    # ---------------------------------------------------------------- bloklash

    def test_permanent_block_after_two_swears_and_message_types_gated(self):
        app.core.store.unban(USER)
        app.core.store.reset_strikes(USER)
        app.core.store.touch_user(USER, username="tester", first_name="Test")
        self.session.calls.clear()

        self.feed(update_of("uyaqqi", user_id=USER, chat_id=USER))  # 1-zarba
        self.assertFalse(app.core.store.is_banned(USER), "1-zarbadan keyin bloklanmasligi kerak")
        self.assertIn("Iltimos, e'tiborli bo'ling", " ".join(self.sent_to(USER)))

        self.feed(update_of("uyaqqi", user_id=USER, chat_id=USER))  # 2-zarba -> doimiy blok
        self.assertTrue(app.core.store.is_banned(USER), "2-zarbada doimiy blok qo'yilmadi")
        self.assertIsNone(app.core.store.ban_expire_at(USER), "blok vaqtinchalik bo'lmasligi kerak")
        self.session.calls.clear()

        # bloklangan foydalanuvchi hech qanday xabar turida javob olmasligi kerak
        for payload in (
            update_of("hello", user_id=USER, chat_id=USER),
            update_of("/start", user_id=USER, chat_id=USER),
            update_of("/help", user_id=USER, chat_id=USER),
            update_of(None, user_id=USER, chat_id=USER, caption="rasm bilan"),
        ):
            self.session.calls.clear()
            self.feed(payload)
            texts = " ".join(self.sent_to(USER))
            self.assertIn("doimiy bloklangansiz", texts, payload)
            self.assertNotIn("Bunday buyruq yo'q", texts)
        self.assertEqual(self.ai_calls, [], "bloklangan foydalanuvchi AI ga yozolmasligi kerak")
        app.core.store.unban(USER)

    def test_banned_user_in_group_is_purged(self):
        app.core.store.touch_user(USER, username="tester", first_name="Test")
        app.core.store.ban_user(USER, hours=None)
        self.session.calls.clear()
        self.feed(update_of("oddiy gap", user_id=USER, chat_id=-1001234, chat_type="supergroup"))
        self.assertIn("DeleteMessage", self.session.names())
        self.assertIn("BanChatMember", self.session.names())
        self.assertEqual(self.ai_calls, [])
        app.core.store.unban(USER)

    # ---------------------------------------------------------------- xato bo'lmagan FP

    def test_ordinary_sentence_from_report_is_not_punished(self):
        # foydalanuvchi shikoyat qilgan real gap: akkaunt noto'g'ri bloklangan edi
        app.core.store.touch_user(USER, username="tester", first_name="Test")
        self.session.calls.clear()
        text = "men bir qiz sinfdoshimga maktabda bila otreli dedim u yoq dedi"
        self.feed(update_of(text, user_id=USER, chat_id=USER))
        self.assertFalse(app.core.store.is_banned(USER), "oddiy gap uchun bloklandi!")
        self.assertEqual(app.core.store.strikes(USER), 0, "oddiy gap uchun zarba yozildi")
        self.assertEqual(self.ai_calls, [text], "oddiy gap AI ga bormadi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
