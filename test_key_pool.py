"""Gemini kalitlari havuzi: bittasi kvotaga urilsa, ikkinchisiga o'tamiz.

Nima uchun kerak: Render'da bitta GEMINI_API_KEY turgani uchun 429
(RESOURCE_EXHAUSTED) butun botni vaqtincha o'ldirardi. Endi bitta env
qiymatiga vergul bilan bir necha kalit yoziladi (`K1,K2`) va kvotaga urilgan
kalit qisqa vaqtga chetga chiqadi — foydalanuvchi uzilishni sezmaydi.

Ishga tushirish:  python -m unittest test_key_pool -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ["BOT_TOKEN"] = os.environ.get("BOT_TOKEN", "123456:AAtest-token")
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "test-key")
_tmpdir = tempfile.mkdtemp(prefix="omini_test_pool_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "bot_data.db")
os.environ["LOG_FILE"] = ""
os.environ["ADMIN_ID"] = "1111"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botcore as core  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimited(Exception):
    """genai.errors.APIError'ning 429 ko'rinishi — faqat `code` maydoni muhim."""

    def __init__(self, code=429):
        super().__init__("Resource has been exhausted (quota exceeded)")
        self.code = code


class FakeResponse:
    """Javob dubligati — `extract_text` faqat `.text` maydonini oladi."""

    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append(model)
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse("navbatdagi javob")
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, str):
            return FakeResponse(outcome)
        return outcome


class FakeChats:
    def __init__(self, name):
        self.name = name
        self.created = []

    def create(self, model=None, config=None, history=None):
        self.created.append(model)
        return f"chat:{self.name}"


class FakeClient:
    def __init__(self, name, outcomes=()):
        self.name = name
        self.models = FakeModels(outcomes)
        self.chats = FakeChats(name)


class BrokenKey(unittest.TestCase):
    def test_broken_key_detected_from_code(self):
        self.assertTrue(core.is_broken_key(RateLimited(401)))
        self.assertTrue(core.is_broken_key(RateLimited(403)))
        self.assertFalse(core.is_broken_key(RateLimited(429)))
        self.assertFalse(core.is_broken_key(RateLimited(400)))

    def test_broken_key_detected_from_text(self):
        self.assertTrue(core.is_broken_key(
            Exception("401 UNAUTHENTICATED. {'error': {'message': 'API key not valid.'}}")))
        self.assertFalse(core.is_broken_key(Exception("boshqa xato")))

    def test_switch_covers_quota_and_dead_key(self):
        self.assertTrue(core.should_switch_key(RateLimited(429)))
        self.assertTrue(core.should_switch_key(RateLimited(401)))
        self.assertFalse(core.should_switch_key(Exception("model yo'q")))

    def test_dead_key_is_cooled_longer_and_next_key_answers(self):
        first = FakeClient("A", [RateLimited(401)])
        second = FakeClient("B", ["javob B"])
        pool = core.KeyPool([first, second])
        with patch.object(core, "pool", pool):
            text, _ = core.ask_once("gemini-test", "salom", False)
        self.assertEqual(text, "javob B")
        self.assertGreater(pool.cooled[0], pool.clock() + pool.cooldown,
                           "o'lik kalit oddiy band kalitdan uzoqroqqa chetlanishi kerak")

    def test_user_key_cooling_scales_with_the_error(self):
        pool = core.KeyPool([FakeClient("A"), FakeClient("B")])
        with patch.object(core, "pool", pool):
            core._user_key[4242] = 0
            core.cool_user_key(4242, RateLimited(401))
            dead_until = pool.cooled[0]
            core._user_key[7777] = 1
            core.cool_user_key(7777, RateLimited(429))
        self.assertGreater(dead_until, pool.clock() + pool.cooldown)
        self.assertLessEqual(pool.cooled[1], pool.clock() + pool.cooldown)


class Pool(unittest.TestCase):
    def test_comma_separated_env_yields_multiple_keys(self):
        self.assertEqual(core.parse_keys(" a , b ,,c "), ["a", "b", "c"])
        self.assertEqual(core.parse_keys("  "), [])

    def test_primary_key_is_used_while_healthy(self):
        pool = core.KeyPool([FakeClient("A"), FakeClient("B")])
        index, used = pool.pick()
        self.assertEqual((index, used.name), (0, "A"))

    def test_cooled_key_is_skipped(self):
        pool = core.KeyPool([FakeClient("A"), FakeClient("B")])
        pool.cool(0)
        index, used = pool.pick()
        self.assertEqual((index, used.name), (1, "B"))

    def test_cooling_expires_and_the_key_returns(self):
        clock = FakeClock()
        pool = core.KeyPool([FakeClient("A"), FakeClient("B")], clock=clock)
        pool.cool(0)
        clock.advance(pool.cooldown + 1)
        self.assertEqual(pool.pick()[0], 0)

    def test_all_keys_cooled_still_returns_one(self):
        pool = core.KeyPool([FakeClient("A"), FakeClient("B")])
        pool.cool(0, seconds=30)
        pool.cool(1, seconds=10)
        index, _ = pool.pick()
        self.assertEqual(index, 1, "eng tez ochiladigan kalit tanlanishi kerak")

    def test_rate_limit_detected_from_code(self):
        self.assertTrue(core.is_rate_limited(RateLimited(429)))
        self.assertTrue(core.is_rate_limited(RateLimited(503)))
        self.assertFalse(core.is_rate_limited(RateLimited(400)))

    def test_rate_limit_detected_from_text_when_no_code(self):
        self.assertTrue(core.is_rate_limited(Exception("429 RESOURCE_EXHAUSTED")))
        self.assertFalse(core.is_rate_limited(Exception("bunday model yo'q")))


class AskOnceFailover(unittest.TestCase):
    def test_next_key_answers_when_first_is_exhausted(self):
        first = FakeClient("A", [RateLimited(429)])
        second = FakeClient("B", ["javob B"])
        pool = core.KeyPool([first, second])
        with patch.object(core, "pool", pool):
            text, _ = core.ask_once("gemini-test", "salom", False)
        self.assertEqual(text, "javob B")
        self.assertEqual(first.models.calls, ["gemini-test"])
        self.assertEqual(second.models.calls, ["gemini-test"])
        self.assertEqual(pool.pick()[0], 1, "A kaliti sovigan bo'lishi kerak")

    def test_non_quota_error_does_not_switch_keys(self):
        first = FakeClient("A", [Exception("model yo'q")])
        second = FakeClient("B", ["keraksiz"])
        pool = core.KeyPool([first, second])
        with patch.object(core, "pool", pool):
            with self.assertRaises(Exception):
                core.ask_once("gemini-test", "salom", False)
        self.assertEqual(second.models.calls, [])


class ImageFailover(unittest.TestCase):
    def test_image_retry_uses_the_other_key(self):
        first = FakeClient("A", [RateLimited(429)])
        second = FakeClient("B", ["rasmvor"])
        pool = core.KeyPool([first, second])
        with patch.object(core, "pool", pool), \
                patch.object(core, "extract_image_part", lambda r: (b"px", "image/png")):
            data, mime, name = core.generate_image_gemini("chiz", refs=None, aspect=None)
        self.assertEqual((data, mime), (b"px", "image/png"))
        self.assertEqual(first.models.calls, second.models.calls)

    def test_image_gives_quota_error_after_every_key_failed(self):
        clients = [FakeClient("A", [RateLimited(429)]), FakeClient("B", [RateLimited(429)])]
        pool = core.KeyPool(clients)
        with patch.object(core, "pool", pool), patch.object(core, "IMAGE_MODEL_FALLBACK", ""):
            with self.assertRaises(core.ImageQuotaError):
                core.generate_image_gemini("chiz", refs=None, aspect=None)


class SessionKey(unittest.TestCase):
    def test_create_chat_remembers_the_key_of_the_user(self):
        clients = [FakeClient("A"), FakeClient("B")]
        pool = core.KeyPool(clients)
        with patch.object(core, "pool", pool):
            chat, _ = core.create_chat(4242, "instruktsiya", "gemini-test", False)
        self.assertEqual(chat, "chat:A")
        self.assertEqual(core.user_key_index(4242), 0)

    def test_cool_user_key_moves_the_next_session_to_the_other_key(self):
        clients = [FakeClient("A"), FakeClient("B")]
        pool = core.KeyPool(clients)
        with patch.object(core, "pool", pool):
            core.create_chat(4242, "instruktsiya", "gemini-test", False)
            core.cool_user_key(4242)
            chat, _ = core.create_chat(4242, "instruktsiya", "gemini-test", False)
        self.assertEqual(chat, "chat:B")

    def test_cool_user_key_without_a_session_is_harmless(self):
        pool = core.KeyPool([FakeClient("A")])
        with patch.object(core, "pool", pool):
            core.cool_user_key(999999)
        self.assertEqual(pool.pick()[0], 0)


class PrivateChat:
    id = 5
    type = "private"


class FakeMessage:
    chat = PrivateChat()
    message_id = 1


class AskAiFailover(unittest.IsolatedAsyncioTestCase):
    """Suhbat o'rtasida kalit tugasa — sessiya boshqa kalitda qayta quriladi."""

    Message = FakeMessage

    async def _drive(self, code):
        """Berilgan xato kodi bilan ask_ai ni yuritadi: (rebuilt, asked, cooled, error)."""
        import contextlib

        import bot as app

        session = {"chat": "chat:A", "search": False}
        asked = []
        error = RateLimited(code)

        async def fake_send_text(chat, prompt, dispatch=None):
            asked.append(chat)
            if len(asked) == 1:
                raise error
            return "javob B", None

        async def fake_stream_reply(*args, **kwargs):
            raise error

        @contextlib.asynccontextmanager
        async def fake_keep_typing(chat_id, action=None):
            yield

        rebuilt = []

        def fake_rebuild(user_id, use_search=None):
            rebuilt.append(user_id)
            session["chat"] = "chat:B"
            return session

        with patch.object(app, "keep_typing", fake_keep_typing), \
                patch.object(app, "ensure_session", lambda user_id: session), \
                patch.object(app, "rebuild_session", fake_rebuild), \
                patch.object(app, "tool_dispatch", lambda user_id, private: None), \
                patch.object(app, "stream_reply", fake_stream_reply), \
                patch.object(app, "send_long", new_callable=AsyncMock), \
                patch.object(app.core.store, "add_message"), \
                patch.object(app.core.store, "bump"), \
                patch.object(app.core, "send_text", fake_send_text), \
                patch.object(app.core, "cool_user_key", new_callable=Mock) as cooled:
            await app.ask_ai(self.Message(), "salom", 4242)
        return rebuilt, asked, cooled, error

    async def test_rate_limit_switches_the_key_and_answers(self):
        rebuilt, asked, cooled, error = await self._drive(429)
        self.assertEqual(rebuilt, [4242], "sessiya qayta qurilishi kerak")
        self.assertEqual(asked, ["chat:A", "chat:B"])
        cooled.assert_called_once_with(4242, error)

    async def test_dead_key_switches_the_key_too(self):
        rebuilt, asked, cooled, error = await self._drive(401)
        self.assertEqual(rebuilt, [4242])
        self.assertEqual(asked, ["chat:A", "chat:B"])
        cooled.assert_called_once_with(4242, error)

    async def test_plain_error_is_not_retried_on_another_key(self):
        import contextlib

        import bot as app

        session = {"chat": "chat:A", "search": False}
        asked = []

        async def fake_send_text(chat, prompt, dispatch=None):
            asked.append(chat)
            raise RuntimeError("boshqa xato")

        async def fake_stream_reply(*args, **kwargs):
            raise RuntimeError("boshqa xato")

        @contextlib.asynccontextmanager
        async def fake_keep_typing(chat_id, action=None):
            yield

        with patch.object(app, "keep_typing", fake_keep_typing), \
                patch.object(app, "ensure_session", lambda user_id: session), \
                patch.object(app, "rebuild_session", new_callable=Mock) as rebuilt, \
                patch.object(app, "drop_search", new_callable=Mock) as dropped, \
                patch.object(app, "tool_dispatch", lambda user_id, private: None), \
                patch.object(app, "stream_reply", fake_stream_reply), \
                patch.object(app.core.store, "add_message"), \
                patch.object(app.core, "send_text", fake_send_text):
            with self.assertRaises(RuntimeError):
                await app.ask_ai(self.Message(), "salom", 4242)

        self.assertEqual(asked, ["chat:A"])
        rebuilt.assert_not_called()
        dropped.assert_not_called()


if __name__ == "__main__":
    unittest.main()
