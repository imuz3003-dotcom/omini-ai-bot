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
from unittest.mock import AsyncMock, patch

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


class GroupSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.roles = {}
        self.lookup_error = False

    async def __call__(self, bot, method, timeout=None):
        result = await super().__call__(bot, method, timeout)
        if type(method).__name__ == "GetMe":
            return types.User(id=bot.id, is_bot=True, first_name="omini", username="omini_test_bot")
        if type(method).__name__ == "GetChatMember":
            if self.lookup_error:
                raise app.TelegramAPIError(method=method, message="Membership lookup failed")
            user = types.User(id=method.user_id, is_bot=False, first_name="Test")
            role = self.roles.get((method.chat_id, method.user_id), "member")
            if role == "creator":
                return types.ChatMemberOwner(user=user, is_anonymous=False)
            if role == "administrator":
                permissions = {
                    name: False for name, field in types.ChatMemberAdministrator.model_fields.items()
                    if field.is_required() and name != "user"
                }
                return types.ChatMemberAdministrator(user=user, **permissions)
            return types.ChatMemberMember(user=user)
        return result


class GroupWelcomeRules(unittest.IsolatedAsyncioTestCase):
    CHAT = -1002345
    OTHER_CHAT = -1006789
    MODERATOR = 2222

    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="omini_group_"))
        self.db_path = os.path.join(directory, "groups.db")
        self.store = app.core.Store(":memory:")
        self.addCleanup(lambda: self.store._db.close())
        self.enterContext(patch.object(app.core, "store", self.store))
        self.session = GroupSession()
        self.session.roles[(self.CHAT, self.MODERATOR)] = "administrator"
        self.enterContext(patch.object(app.bot, "session", self.session))
        self.ai = self.enterContext(patch.object(app, "ask_ai", new_callable=AsyncMock))

    async def asyncTearDown(self):
        self.ai.assert_not_called()

    async def feed(self, text=None, user_id=USER, chat_id=None, chat_type="supergroup", **fields):
        payload = update_of(text, user_id=user_id, chat_id=chat_id or self.CHAT, chat_type=chat_type)
        payload["message"]["chat"]["title"] = "Sinf <A> & B"
        payload["message"].update(fields)
        update = types.Update.model_validate(payload, context={"bot": app.bot})
        await app.dp.feed_update(app.bot, update)

    def sent(self, chat_id=None):
        return "\n".join(self.session.texts(chat_id or self.CHAT))

    def use_persistent_store(self):
        """Davomiylik testlari uchun xotira store'ini fayl store'iga almashtiradi."""
        self.store._db.close()
        self.store = app.core.Store(self.db_path)
        self.addCleanup(self.store._db.close)
        app.core.store = self.store

    def newcomers(self):
        return [
            {"id": 6001, "is_bot": False, "first_name": "Ali <b> & Vali"},
            {"id": 6002, "is_bot": False, "first_name": "Diyor"},
            {"id": 6003, "is_bot": True, "first_name": "HelperRobot"},
        ]

    async def test_new_human_members_receive_welcome_with_rules_command(self):
        for chat_type in ("group", "supergroup"):
            with self.subTest(chat_type=chat_type):
                self.session.calls.clear()
                await self.feed(chat_type=chat_type, new_chat_members=self.newcomers())
                self.assertIn("Xush kelibsiz", self.sent())
                self.assertIn("Ali <b> & Vali", self.sent())
                self.assertIn("Diyor", self.sent())
                self.assertIn("Sinf <A> & B", self.sent())
                self.assertIn("/rules", self.sent())
                self.assertNotIn("HelperRobot", self.sent())
                messages = [p for name, p in self.session.calls if name == "SendMessage"]
                self.assertTrue(all(p.get("parse_mode") is None for p in messages))

    async def test_bots_and_banned_newcomers_are_not_welcomed(self):
        self.store.ban_user(6001)
        await self.feed(new_chat_members=[self.newcomers()[0], self.newcomers()[2]])
        self.assertEqual(self.sent(), "")

    async def test_large_join_event_fits_in_one_telegram_message(self):
        members = [
            {"id": 7000 + i, "is_bot": False, "first_name": "A" * 64, "last_name": "B" * 64}
            for i in range(100)
        ]
        await self.feed(new_chat_members=members)
        messages = self.session.texts(self.CHAT)
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0].encode("utf-16-le")) // 2, 4096)
        self.assertIn("90", messages[0])

    async def test_rules_not_set_explains_how_to_set_them(self):
        await self.feed("/rules")
        self.assertIn("hali belgilanmagan", self.sent())
        self.assertIn("/setrules", self.sent())

    async def test_admin_and_owner_can_set_rules_that_members_can_read(self):
        for role in ("administrator", "creator"):
            with self.subTest(role=role):
                self.session.roles[(self.CHAT, self.MODERATOR)] = role
                await self.feed("/setrules Bir-biringizni hurmat qiling.\nReklama yo'q.", user_id=self.MODERATOR)
                self.assertIn("saqlandi", self.sent())
                self.session.calls.clear()
                await self.feed("/rules")
                self.assertIn("Bir-biringizni hurmat qiling.\nReklama yo'q.", self.sent())

    async def test_rules_are_plain_text_not_html_or_mentions(self):
        rules = '<a href="tg://user?id=123">Admin</a> & <b>salom</b>'
        await self.feed("/setrules " + rules, user_id=self.MODERATOR)
        self.session.calls.clear()
        await self.feed("/rules")
        self.assertIn(rules, self.sent())
        message = next(p for name, p in self.session.calls if name == "SendMessage")
        self.assertIsNone(message.get("parse_mode"))

    async def test_members_including_bot_owner_cannot_change_group_settings(self):
        await self.feed("/setrules Asl qoidalar", user_id=self.MODERATOR)
        for user_id in (USER, ADMIN):
            for command in ("/setrules O'zgartirish", "/welcome off"):
                with self.subTest(user_id=user_id, command=command):
                    self.session.calls.clear()
                    await self.feed(command, user_id=user_id)
                    self.assertIn("guruh admin", self.sent())
                    self.assertNotIn("saqlandi", self.sent())
        self.session.calls.clear()
        await self.feed("/rules")
        self.assertIn("Asl qoidalar", self.sent())
        await self.feed(new_chat_members=self.newcomers())
        self.assertIn("Xush kelibsiz", self.sent())

    async def test_anonymous_sender_cannot_change_settings(self):
        await self.feed("/setrules Asl qoidalar", user_id=self.MODERATOR)
        self.session.calls.clear()
        await self.feed(
            "/setrules Yangi qoidalar", user_id=self.MODERATOR,
            sender_chat={"id": self.CHAT, "type": "supergroup", "title": "Sinf"},
        )
        self.assertIn("shaxsiy akkaunt", self.sent())
        self.assertNotIn("GetChatMember", self.session.names())
        self.session.calls.clear()
        await self.feed("/rules")
        self.assertIn("Asl qoidalar", self.sent())

    async def test_failed_permission_lookup_does_not_change_settings(self):
        await self.feed("/setrules Asl qoidalar", user_id=self.MODERATOR)
        self.session.lookup_error = True
        self.session.calls.clear()
        with self.assertLogs(level="WARNING") as logs:
            await self.feed("/setrules Yangi qoidalar", user_id=self.MODERATOR)
        self.assertIn("tekshirib bo'lmadi", self.sent())
        self.assertNotIn("saqlandi", self.sent())
        self.session.calls.clear()
        await self.feed("/rules")
        self.assertIn("Asl qoidalar", self.sent())

    async def test_admin_permission_is_checked_again_after_demotion(self):
        await self.feed("/setrules Asl qoidalar", user_id=self.MODERATOR)
        self.session.roles[(self.CHAT, self.MODERATOR)] = "member"
        self.session.calls.clear()
        await self.feed("/setrules Yangi qoidalar", user_id=self.MODERATOR)
        self.assertIn("guruh admin", self.sent())
        self.session.calls.clear()
        await self.feed("/rules")
        self.assertIn("Asl qoidalar", self.sent())

    async def test_settings_and_admin_permission_are_scoped_to_one_group(self):
        await self.feed("/setrules Birinchi guruh", user_id=self.MODERATOR)
        await self.feed("/welcome off", user_id=self.MODERATOR)
        await self.feed("/setrules Ikkinchi guruh", user_id=self.MODERATOR, chat_id=self.OTHER_CHAT)
        self.assertIn("guruh admin", self.sent(self.OTHER_CHAT))
        self.session.calls.clear()
        await self.feed("/rules", chat_id=self.OTHER_CHAT)
        self.assertIn("hali belgilanmagan", self.sent(self.OTHER_CHAT))
        await self.feed(new_chat_members=self.newcomers(), chat_id=self.OTHER_CHAT)
        self.assertIn("Xush kelibsiz", self.sent(self.OTHER_CHAT))
        await self.feed("/rules")
        self.assertIn("Birinchi guruh", self.sent())
        self.session.calls.clear()
        await self.feed(new_chat_members=self.newcomers())
        self.assertEqual(self.sent(), "")

    async def test_welcome_toggle_and_rules_survive_reopening_database(self):
        self.use_persistent_store()
        await self.feed("/welcome off", user_id=self.MODERATOR)
        await self.feed("/setrules Saqlangan qoidalar", user_id=self.MODERATOR)
        self.assertIn("saqlandi", self.sent())
        self.use_persistent_store()
        self.session.calls.clear()
        await self.feed(new_chat_members=self.newcomers())
        self.assertEqual(self.sent(), "")
        await self.feed("/rules")
        self.assertIn("Saqlangan qoidalar", self.sent())
        await self.feed("/welcome on", user_id=self.MODERATOR)
        self.session.calls.clear()
        await self.feed(new_chat_members=self.newcomers())
        self.assertIn("Xush kelibsiz", self.sent())
        await self.feed("/rules")
        self.assertIn("Saqlangan qoidalar", self.sent())

    async def test_invalid_commands_leave_existing_settings_unchanged(self):
        await self.feed("/setrules Asl qoidalar", user_id=self.MODERATOR)
        for command, expected in (
            ("/setrules", "/setrules"),
            ("/setrules " + "x" * 3501, "3500"),
            ("/setrules " + "\U0001f600" * 2000, "3500"),
            ("/welcome wrong", "/welcome on"),
            ("/welcome on extra", "/welcome on"),
        ):
            with self.subTest(command=command[:50]):
                self.session.calls.clear()
                await self.feed(command, user_id=self.MODERATOR)
                self.assertIn(expected, self.sent())
                self.assertNotIn("saqlandi", self.sent())
                self.session.calls.clear()
                await self.feed("/rules")
                self.assertIn("Asl qoidalar", self.sent())

    async def test_welcome_without_arguments_shows_status_without_changing_it(self):
        await self.feed("/welcome off", user_id=self.MODERATOR)
        self.session.calls.clear()
        await self.feed("/welcome", user_id=self.MODERATOR)
        self.assertIn("o'chirilgan", self.sent())
        self.session.calls.clear()
        await self.feed(new_chat_members=self.newcomers())
        self.assertEqual(self.sent(), "")

    async def test_private_chat_and_channel_commands_do_not_change_group(self):
        for chat_type in ("private", "channel"):
            for command in ("/rules", "/setrules Yangi", "/welcome off"):
                with self.subTest(chat_type=chat_type, command=command):
                    self.session.calls.clear()
                    await self.feed(command, chat_id=USER, chat_type=chat_type)
                    self.assertIn("faqat guruh", self.sent(USER))
                    self.assertNotIn("GetChatMember", self.session.names())

    async def test_telegram_command_suffix_reaches_group_handler(self):
        await self.feed("/setrules@omini_test_bot Guruh matni", user_id=self.MODERATOR)
        self.assertIn("saqlandi", self.sent())
        self.session.calls.clear()
        await self.feed("/rules@omini_test_bot")
        self.assertIn("Guruh matni", self.sent())

    async def test_banned_user_still_cannot_use_rules_command(self):
        self.store.ban_user(USER)
        await self.feed("/rules")
        self.assertEqual(self.sent(), "")
        self.assertIn("BanChatMember", self.session.names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
