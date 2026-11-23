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
from types import SimpleNamespace
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
import devicehub  # noqa: E402
from aiogram import types  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402

ADMIN = app.ADMIN_ID
USER = 7896336878  # foydalanuvchi sinovdan o'tkazgan hisob (noto'g'ri bloklangan edi)


class FakeSession:
    """Telegram'ga chiqadigan har bir so'rovni yozib qo'yadi, hech qayoqqa bormaydi."""

    def __init__(self):
        self.calls = []
        self._mid = 1000
        self._texts = {}

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
            self._texts[self._mid] = payload.get("text", "")
            return types.Message.model_validate({
                "message_id": self._mid,
                "date": int(time.time()),
                "chat": {"id": payload.get("chat_id", 1), "type": payload.get("chat_type", "private")},
                "from": {"id": 123456, "is_bot": True, "first_name": "omini"},
            }, context={"bot": bot})
        if name == "EditMessageText":
            if self._texts.get(method.message_id) == method.text:
                # Telegram o'zgarmagan matnni edit qilishni rad etadi
                raise TelegramBadRequest(method=method, message="message is not modified")
            self._texts[method.message_id] = method.text
            return True
        if name == "SendDocument":
            self._mid += 1
            return types.Message.model_validate({
                "message_id": self._mid,
                "date": int(time.time()),
                "chat": {"id": payload.get("chat_id", 1), "type": "private"},
                "from": {"id": 123456, "is_bot": True, "first_name": "omini"},
                "document": {"file_id": "sent-1", "file_unique_id": "sent-u",
                             "file_name": getattr(payload.get("document"), "filename", "f")},
            }, context={"bot": bot})
        if name in ("DeleteMessage", "BanChatMember", "AnswerCallbackQuery", "SendChatAction",
                    "DeleteChatMessage"):
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

    def bot_msg_ids(self):
        """Bot yuborgan xabarlarning ID'lari — Telegram haqiqatda shularni qaytaradi."""
        return sorted(self._texts)

    def deleted_msg_ids(self):
        return [p["message_id"] for name, p in self.calls if name == "DeleteMessage"]


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


class FakeChat:
    """Gemini chat sessiyasi o'rniga: faqat bitta-yuborish yo'lini ishlatadi."""

    def __init__(self, text="Zaxira javob"):
        self.text = text
        self.prompts = []

    def send_message(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.text)


def chunks_stream(pieces=None, error=None, fail_after=None):
    """core.stream_text o'rniga: berilgan bo'laklarni oqim qilib qaytaradi."""

    async def gen(chat, prompt, dispatch=None):
        for index, piece in enumerate(pieces or []):
            if error is not None and index == fail_after:
                raise error
            yield piece
        if error is not None and fail_after is None:
            raise error

    return gen


class StreamingReply(unittest.IsolatedAsyncioTestCase):
    """Javob qism-qism ko'rsatilsa, foydalanuvchi birinchi belgilarni kutmaydi."""

    async def asyncSetUp(self):
        self.store = app.core.Store(":memory:")
        self.addCleanup(lambda: self.store._db.close())
        self.enterContext(patch.object(app.core, "store", self.store))
        self.session = FakeSession()
        self.enterContext(patch.object(app.bot, "session", self.session))
        self.chat = FakeChat()
        self.enterContext(patch.object(app, "ensure_session", lambda uid, fresh=False: {"chat": self.chat, "search": False}))
        self.enterContext(patch.object(app, "STREAM_EDIT_INTERVAL", 0))

    async def feed(self, text="salom"):
        update = types.Update.model_validate(update_of(text, user_id=USER, chat_id=USER), context={"bot": app.bot})
        await app.dp.feed_update(app.bot, update)

    def sends(self):
        return [p.get("text", "") for p in (payload for name, payload in self.session.calls if name == "SendMessage")]

    def edits(self):
        return [p for name, p in self.session.calls if name == "EditMessageText"]

    async def test_first_message_shown_before_answer_is_complete(self):
        pieces = [f"qism{i} " + "a" * 200 for i in range(3)]
        pieces[-1] = "**qism2** " + "a" * 200
        self.enterContext(patch.object(app.core, "stream_text", chunks_stream(pieces)))
        await self.feed()
        self.assertEqual(len(self.sends()), 1, "javob bitta xabarda oqishi kerak")
        self.assertLess(len(self.sends()[0]), sum(len(p) for p in pieces), "birinchi xabar to'liq javabni kutib yubordi")
        self.assertGreaterEqual(len(self.edits()), 2, "oqim davomida xabar edit qilinmadi")
        self.assertIn("qism2", self.edits()[-1]["text"])
        self.assertEqual(self.edits()[-1]["parse_mode"], "HTML")
        self.assertIn("<b>qism2</b>", self.edits()[-1]["text"])

    async def test_long_answer_finishes_in_second_message(self):
        pieces = [f"{i:03d}" + "b" * 100 for i in range(60)]
        self.enterContext(patch.object(app.core, "stream_text", chunks_stream(pieces)))
        await self.feed()
        self.assertEqual(len(self.sends()), 2, "juda uzun javob ikkinchi xabarga o'tmadi")
        self.assertIn("000", self.edits()[-1]["text"])
        self.assertIn("059", self.sends()[1])

    async def test_short_plain_answer_is_not_sent_twice(self):
        # yakuniy HTML edit ko'rsatilgan matn bilan bir xil — Telegram "not modified" deydi
        pieces = ["Qisqa javob. " + "e" * 200]
        self.enterContext(patch.object(app.core, "stream_text", chunks_stream(pieces)))
        await self.feed()
        self.assertEqual(len(self.sends()), 1, "qisqa javob ikki marta yuborildi")
        self.assertEqual(self.chat.prompts, [], "zaxira yo'li bekor ishga tushdi")

    async def test_stop_cancels_stream_and_deletes_half_answer(self):
        hold = asyncio.Event()

        async def stalled(chat, prompt, dispatch=None):
            yield "yarim javob " + "g" * 200
            await hold.wait()

        self.enterContext(patch.object(app.core, "stream_text", stalled))
        task = asyncio.create_task(self.feed())
        for _ in range(300):
            # Yarim xabar ko'ringan paytni kuzatib olamiz: qattiq 50 ms kutish
            # mashina band bo'lganda sinadi (birinchi xabar ulgurmay qoladi).
            if "SendMessage" in self.session.names():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIn("DeleteMessage", self.session.names(), "to'xtatilganda yarim xabar qoldi")
        self.assertEqual(self.chat.prompts, [], "to'xtatilgan so'rov zaxira yo'liga tushdi")

    async def test_stream_failure_falls_back_to_single_send(self):
        self.enterContext(patch.object(app.core, "stream_text", chunks_stream(error=RuntimeError("oqim uzildi"))))
        await self.feed()
        self.assertEqual(self.chat.prompts, ["salom"], "zaxira yo'li Gemini'ga so'rov yubormadi")
        self.assertIn("Zaxira javob", "\n".join(self.sends()))
        self.assertNotIn("", self.sends(), "bo'sh oqim xabari foydalanuvchida qoldi")

    async def test_half_streamed_answer_is_deleted_before_fallback(self):
        pieces = ["yarim qolgan javob " + "c" * 200, "yana bir bo'lak " + "d" * 200]
        self.enterContext(
            patch.object(app.core, "stream_text", chunks_stream(pieces, error=RuntimeError("oqim uzildi"), fail_after=1))
        )
        await self.feed()
        self.assertIn("DeleteMessage", self.session.names(), "yarim ko'rinib turgan xabar o'chirilmadi")
        self.assertIn("Zaxira javob", "\n".join(self.sends()))


class FakeHub:
    """DeviceHub'ning handlerlarga kerak bo'lgan qismi — chaqiruvlarni yozib boradi."""

    def __init__(self):
        self.calls = []
        self.results = {}
        self.error = None
        self.offline = False

    def _fail(self):
        if self.error:
            raise devicehub.DeviceError(self.error)

    def pair(self, user_id, code):
        self._fail()
        self.calls.append(("pair", user_id, code))
        return {"agent_id": "a" * 32, "name": "desk-pc", "root": "C:/Users/me",
                "cwd": "C:/Users/me", "online": not self.offline}

    async def run(self, user_id, op, timeout=None, **args):
        self.calls.append((op, args))
        self._fail()
        return self.results.get(op, {})

    def devices_of(self, user_id):
        return []

    def cwd_of(self, user_id):
        self._fail()
        return "C:/Users/me"

    def select(self, user_id, ref):
        self._fail()
        return {"name": ref, "cwd": "C:/Users/me"}

    def unpair(self, user_id, ref):
        self._fail()
        return {"name": ref}


def document_update(caption, user_id=USER, file_size=5):
    message = {
        "message_id": 1,
        "date": int(time.time()),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test", "username": "tester"},
        "document": {
            "file_id": "doc-1", "file_unique_id": "doc-u",
            "file_name": "salom.txt", "mime_type": "text/plain", "file_size": file_size,
        },
        "caption": caption,
    }
    return {"update_id": int(time.time() * 1000) % 10_000_000, "message": message}


class DeviceCommands(unittest.IsolatedAsyncioTestCase):
    """Qurilma buyruqlari dispatcher DARAJASIDA.

    Nima uchun aynan bu yerda: `/put` hujjat caption'i bilan keladi — agar u
    `F.document` (Gemini tahlili) handleridan oldin turmasa, fayl qurilmaga
    yozilmay AI'ga ketardi. Shuningdek guruh chatida hech qanday qurilma
    buyrug'i bajarilmasligi kerak.
    """

    def setUp(self):
        self.session = FakeSession()
        app.bot.session = self.session
        self.hub = FakeHub()
        self.enterContext(patch.object(app, "HUB", self.hub))

        async def fake_download(file):
            return b"salom"

        self.enterContext(patch.object(app, "download_tg_file", fake_download))
        app.core.store.unban(USER)

    async def typed(self, text, chat_id=None, chat_type="private"):
        payload = update_of(text, user_id=USER, chat_id=chat_id or USER, chat_type=chat_type)
        await app.dp.feed_update(app.bot, types.Update.model_validate(payload, context={"bot": app.bot}))

    def replies(self, chat_id=USER):
        return " ".join(self.session.texts(chat_id))

    async def test_pair_reaches_the_hub(self):
        await self.typed("/pair 123456")
        self.assertEqual(self.hub.calls, [("pair", USER, "123456")])
        self.assertIn("desk-pc", self.replies())
        self.assertNotIn("Bunday buyruq yo'q", self.replies())

    async def test_pair_says_nothing_about_being_offline_when_it_worked(self):
        await self.typed("/pair 123456")
        self.assertNotIn("onlayn emas", self.replies())

    async def test_pair_warns_when_the_channel_is_missing(self):
        """Kod yaroqli, lekin agent kanali topilmadi: ogohlantirish ko'rsatiladi,
        soxta «hammasi joyida» javobi emas."""
        self.hub.offline = True
        await self.typed("/pair 123456")
        self.assertIn("onlayn emas", self.replies())

    async def test_ls_renders_entries_from_the_device(self):
        self.hub.results["ls"] = {
            "cwd": "C:/Users/me/Documents", "truncated": False,
            "entries": [
                {"name": "Arxiv", "dir": True, "size": 0, "mtime": 0},
                {"name": "hisobot.txt", "dir": False, "size": 2048, "mtime": 0},
            ],
        }
        await self.typed("/ls Documents")
        self.assertEqual(self.hub.calls, [("ls", {"path": "Documents"})])
        text = self.replies()
        self.assertIn("C:/Users/me/Documents", text)
        self.assertIn("Arxiv/", text)
        self.assertIn("hisobot.txt — 2.0 KB", text)

    async def test_agent_error_text_reaches_the_user(self):
        self.hub.error = "Denied: ruxsat yo'q: C:/Windows ildizdan tashqarida"
        await self.typed("/rm C:/Windows/x.txt")
        self.assertIn("ildizdan tashqarida", self.replies())
        self.assertNotIn("Bunday buyruq yo'q", self.replies())

    async def test_group_chat_never_reaches_a_device(self):
        await self.typed("/ls", chat_id=-100123, chat_type="group")
        await self.typed("/pair 123456", chat_id=-100123, chat_type="group")
        self.assertEqual(self.hub.calls, [])

    async def test_put_caption_goes_to_the_device_not_the_ai(self):
        self.hub.results["put"] = {"path": "C:/Users/me/notes.txt", "bytes": 5}
        await app.dp.feed_update(app.bot, types.Update.model_validate(
            document_update("/put notes.txt"), context={"bot": app.bot}))
        self.assertEqual(self.hub.calls, [("put", {"path": "notes.txt", "b64": "c2Fsb20="})])
        self.assertIn("notes.txt", self.replies())

    async def test_put_over_the_transfer_limit_is_refused_before_upload(self):
        self.session.calls.clear()
        await app.dp.feed_update(app.bot, types.Update.model_validate(
            document_update("/put katta.bin", file_size=9 * 1024 * 1024), context={"bot": app.bot}))
        self.assertEqual(self.hub.calls, [])
        self.assertIn("juda katta", self.replies())

    async def test_get_sends_the_decoded_file_back_as_document(self):
        self.hub.results["get"] = {"path": "C:/Users/me/hisobot.txt", "size": 5,
                                   "encoding": "base64", "b64": "c2Fsb20="}
        await self.typed("/get hisobot.txt")
        sent = [payload for name, payload in self.session.calls if name == "SendDocument"]
        self.assertEqual(len(sent), 1, "qurilma fayli hujjat bo'lib qaytmadi")
        self.assertEqual(sent[0]["document"].filename, "hisobot.txt")
        self.assertEqual(sent[0]["document"].data, b"salom", "base64 qayta ochilmadi")
        self.assertIn("hisobot.txt", sent[0]["caption"])


class ClearChat(unittest.IsolatedAsyncioTestCase):
    """«🧹 Suhbatni tozalash» / `/new` botning xabarlarini ham o'chiradi.

    Nima uchun aynan bu yerda: `wipe_messages` faqat `tracked` jadvalidagi
    msg_id'larni yuboradi. AI javoblari yozib qo'yilardi, ammo `notify()`
    orqali chiquvchi xabarlar (menyu, sozlamalar, qurilma javoblari,
    ogohlantirishlar) ro'yxatga tushmasdi — natijada tozalashdan keyin faqat
    foydalanuvchining xabari yo'qolib, botnikisi chiqib qolardi.
    """

    CLEARED = 7896336812  # alohida id: `legacy_swept` pref'i boshqa testlarga aralashmasligi uchun
    MENU_AUTHOR = 7896336821
    CLICKER = 7896336822

    def setUp(self):
        self.session = FakeSession()
        app.bot.session = self.session

        async def fake_ask_ai(message, prompt, user_id, markup=None):
            pass

        self.enterContext(patch.object(app, "ask_ai", fake_ask_ai))
        app.core.store.touch_user(self.CLEARED, username="tester", first_name="Test")
        app.core.store.set_pref(self.CLEARED, "legacy_swept", True)

    def tearDown(self):
        for uid in (self.CLEARED, self.MENU_AUTHOR, self.CLICKER):
            app.core.store.untrack_msgs(uid)

    async def typed(self, text, message_id):
        payload = update_of(text, user_id=self.CLEARED, chat_id=self.CLEARED)
        payload["message"]["message_id"] = message_id
        await app.dp.feed_update(app.bot, types.Update.model_validate(payload, context={"bot": app.bot}))

    async def test_clear_deletes_the_bots_notice_messages(self):
        await self.typed("/stop", 500)  # javob — `notify()` orqali, AI'ga umuman kirmaydi
        notice = self.session.bot_msg_ids()[-1]
        self.session.calls.clear()

        await self.typed("/new", 501)

        deleted = self.session.deleted_msg_ids()
        self.assertIn(notice, deleted, "botning notify() xabari tozalashdan keyin qoldi")
        self.assertIn(501, deleted, "foydalanuvchining xabari o'chirilmadi")

    async def test_every_notice_command_reply_is_deleted(self):
        """`notify()` bilan javob beradigan har bir buyruq tozalashga tushadi."""
        for number, command in enumerate(("/help", "/settings", "/notes"), start=600):
            with self.subTest(command=command):
                await self.typed(command, number)
                reply = self.session.bot_msg_ids()[-1]
                self.session.calls.clear()

                await self.typed("/new", number + 100)

                self.assertIn(reply, self.session.deleted_msg_ids(),
                              f"{command} javobi tozalashdan keyin qoldi")

    async def test_notice_is_tracked_for_the_given_user_not_the_author(self):
        """Guruhda menyu A'ning xabari, tugmani B bosdi — yangi belgi B'ning
        ro'yxatiga tushishi kerak, aks holda B uni keyin o'chira olmaydi."""
        menu = update_of("menyu", user_id=self.MENU_AUTHOR)["message"]
        message = types.Message.model_validate(menu, context={"bot": app.bot})

        sent = await app.notify(message, "🔄 Yangi suhbat boshlandi!", user_id=self.CLICKER)

        self.assertIn(sent.message_id, app.core.store.tracked_msgs(self.CLICKER))
        self.assertNotIn(sent.message_id, app.core.store.tracked_msgs(self.MENU_AUTHOR))

    async def test_clear_keeps_the_fresh_chat_marker(self):
        """«Yangi suhbat boshlandi» — yangi suhbatning boshi, u o'chirilmaydi."""
        await self.typed("/stop", 500)
        self.session.calls.clear()

        await self.typed("/new", 501)

        marker = self.session.bot_msg_ids()[-1]
        self.assertIn("Yangi suhbat", self.session.texts()[-1])
        self.assertNotIn(marker, self.session.deleted_msg_ids(), "yangi belgi ham o'chirib yuborildi")


class Transport(unittest.IsolatedAsyncioTestCase):
    """Kirish yo'lini bitta narsa hal qiladi: `WEBHOOK_SECRET` bor-yo'qligi.

    Nima uchun alohida funksiya: webhook bilan polling bir vaqtda ishl olmaydi
    (Telegram `getUpdates`ni 409 bilan rad etadi), shuning uchun «qaysi biri»
    qarori testlanadigan bitta joyda turishi kerak.
    """

    class Bot:
        def __init__(self):
            self.calls = []

        async def set_webhook(self, **kwargs):
            self.calls.append(("set_webhook", kwargs))

        async def delete_webhook(self, **kwargs):
            self.calls.append(("delete_webhook", kwargs))

    class Dp:
        def __init__(self):
            self.calls = []

        async def start_polling(self, bot):
            self.calls.append(("start_polling", bot))

    async def test_secret_present_switches_to_webhook_and_never_polls(self):
        bot, dp = self.Bot(), self.Dp()

        await app.start_updates(bot_obj=bot, dispatcher=dp, secret="kalit",
                                base_url="https://omini-ai-bot.onrender.com")

        self.assertEqual([name for name, _ in bot.calls], ["set_webhook"])
        sent = bot.calls[0][1]
        self.assertEqual(sent["url"], "https://omini-ai-bot.onrender.com/telegram/webhook")
        self.assertEqual(sent["secret_token"], "kalit")
        self.assertIs(sent["drop_pending_updates"], False, "uyqudagi xabarlar o'chib ketmasligi kerak")
        self.assertEqual(dp.calls, [], "webhook bilan polling birga ishlay olmaydi")

    async def test_without_secret_polling_starts_and_webhook_is_cleared(self):
        bot, dp = self.Bot(), self.Dp()

        await app.start_updates(bot_obj=bot, dispatcher=dp, secret="",
                                base_url="https://omini-ai-bot.onrender.com")

        self.assertEqual([name for name, _ in bot.calls], ["delete_webhook"])
        self.assertEqual([name for name, _ in dp.calls], ["start_polling"])

    async def test_webhook_update_answers_exactly_like_polling(self):
        """Kirish yo'li o'zgarsa ham handlerlar bir xil javob berishi shart."""
        session = FakeSession()
        app.bot.session = session
        payload = update_of("/help", user_id=USER)

        await app.feed_webhook_update(payload)
        through_webhook = session.texts()
        session.calls.clear()

        await app.dp.feed_update(app.bot, types.Update.model_validate(dict(payload), context={"bot": app.bot}))
        through_polling = session.texts()

        self.assertTrue(through_webhook, "webhook orqali kelgan /help javobsiz qoldi")
        self.assertEqual(through_webhook, through_polling)


if __name__ == "__main__":
    unittest.main(verbosity=2)
