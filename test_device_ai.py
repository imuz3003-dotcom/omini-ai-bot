"""AI suhbati endi qurilma ustida o'zi ishlaydi (Gemini function calling).

Nima uchun kerak: foydalanuvchi "menga player papka yaratib ber" deb yozganda
bot avvallari "fayl tizimingizga kirish huquqim yo'q" deb javob qilardi — AI
yarimi qurilma ulanganini bilmasdi. Bu testlar shu uzilishni yopadi: modelga
qurilma vositalari beriladi, u chaqiradi, bot HUB orqali bajaradi va natijani
modelga qaytaradi.

Ishga tushirish:  python -m unittest test_device_ai -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ["BOT_TOKEN"] = os.environ.get("BOT_TOKEN", "123456:AAtest-token")
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "test-key")
_tmpdir = tempfile.mkdtemp(prefix="omini_test_ai_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "bot_data.db")
os.environ["LOG_FILE"] = ""
os.environ["ADMIN_ID"] = "1111"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as app  # noqa: E402
import botcore as core  # noqa: E402
import devicehub  # noqa: E402
from google.genai import types  # noqa: E402

# Modelga beriladigan vositalar to'plami — qo'lda yozilgan, koddan olinmaydi.
EXPECTED_TOOLS = frozenset({"ls", "cd", "cat", "find", "tree", "stat",
                            "mkdir", "write", "mv", "cp", "rm"})


def text_chunk(text: str):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
    )])


def call_chunk(call_id: str, name: str, args: dict):
    part = types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
    )


class FakeChat:
    """Gemini chat'ining halol dublikati: har oqim bir marta, ketma-ket beriladi."""

    def __init__(self, streams):
        self.streams = [list(chunk) for chunk in streams]
        self.sent = []

    def send_message_stream(self, message, config=None):
        self.sent.append(message)
        if not self.streams:
            raise AssertionError("chatdan ortiqcha oqim so'raldi")
        return iter(self.streams.pop(0))


class Recorder:
    """dispatch_tool o'rniga: chaqiruvlarni yozadi, natijani beradi."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {} if result is None else result
        self.error = error

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error
        return self.result


class ToolDeclarations(unittest.TestCase):
    def names(self):
        return {decl.name for decl in core.device_tool_declarations()}

    def test_names_are_the_agent_op_names(self):
        self.assertEqual(self.names(), EXPECTED_TOOLS)

    def test_binary_transfer_stays_out_of_the_model(self):
        # /get va /put — odam buyrug'i: 8 MB faylni AI'ga uzatish xavfsiz emas.
        self.assertNotIn("get", self.names())
        self.assertNotIn("put", self.names())

    def test_rm_declaration_says_single_file_only(self):
        decl = next(d for d in core.device_tool_declarations() if d.name == "rm")
        text = (decl.description or "").lower()
        self.assertIn("fayl", text)
        self.assertIn("katalog", text)

    def test_mkdir_requires_path(self):
        decl = next(d for d in core.device_tool_declarations() if d.name == "mkdir")
        self.assertEqual(list(decl.parameters.required), ["path"])

    def test_cd_takes_path(self):
        decl = next(d for d in core.device_tool_declarations() if d.name == "cd")
        self.assertIn("path", decl.parameters.properties)


class ChatConfig(unittest.TestCase):
    def test_device_tools_attached_when_with_devices(self):
        cfg = core.build_config(False, "ti", with_devices=True)
        declared = {decl.name
                    for tool in cfg["tools"]
                    for decl in (tool.function_declarations or [])}
        self.assertEqual(declared, EXPECTED_TOOLS)

    def test_no_tools_at_all_by_default(self):
        self.assertNotIn("tools", core.build_config(False, "ti"))

    def test_search_and_device_tools_coexist(self):
        cfg = core.build_config(True, "ti", with_devices=True)
        tools = cfg["tools"]
        self.assertEqual(len(tools), 2)
        self.assertTrue(any(t.google_search is not None for t in tools))
        self.assertTrue(any(t.function_declarations for t in tools))


class DeviceInstruction(unittest.TestCase):
    def test_device_block_names_the_tools(self):
        text = app.instruction_for({"id": 1, "persona": "general"}, with_devices=True)
        self.assertIn("mkdir", text)
        self.assertIn("qurilma", text.lower())

    def test_plain_instruction_has_no_device_block(self):
        text = app.instruction_for({"id": 1, "persona": "general"})
        self.assertNotIn("mkdir", text)

    def test_device_block_tells_the_model_about_offline(self):
        # Model "huquqim yo'q" demasin: agar vosita 📴 qaytarsa, shuni aytsin.
        text = app.instruction_for({"id": 1, "persona": "general"}, with_devices=True)
        self.assertIn("onlayn", text)


class SessionFingerprint(unittest.TestCase):
    """Suhbat sessiyasi qurilma paydo bo'lganda/qaytib ketganda yangilanadi."""

    def setUp(self):
        self.sessions = {}
        self.created = []

        def fake_create_chat(user_id, instruction, model, use_search, replay=True,
                             with_devices=False):
            self.created.append({"instruction": instruction,
                                 "with_devices": with_devices})
            return object(), False

        patches = [
            patch.object(core, "create_chat", side_effect=fake_create_chat),
            patch.object(core, "current_session",
                         side_effect=lambda uid: self.sessions.setdefault(uid, {})),
            patch.object(core, "remember_session",
                         side_effect=lambda uid, chat, search: self.sessions.setdefault(
                             uid, {}).update({"chat": chat, "search": search})
                             or self.sessions[uid]),
            patch.object(app.core.store, "user", return_value={"id": 1, "persona": "general"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def devices(self, count):
        return [{"agent_id": f"a{i}", "name": f"d{i}", "online": True}
                for i in range(count)]

    def test_chat_recreated_when_first_device_paired(self):
        with patch.object(app.HUB, "devices_of", return_value=[]):
            app.ensure_session(1)
        with patch.object(app.HUB, "devices_of", return_value=self.devices(1)):
            app.ensure_session(1)
        self.assertEqual(len(self.created), 2)
        self.assertEqual([c["with_devices"] for c in self.created], [False, True])

    def test_chat_reused_when_device_state_unchanged(self):
        with patch.object(app.HUB, "devices_of", return_value=self.devices(1)):
            app.ensure_session(1)
            app.ensure_session(1)
        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.created[0]["with_devices"])

    def test_device_block_reaches_the_instruction(self):
        with patch.object(app.HUB, "devices_of", return_value=self.devices(1)):
            app.ensure_session(1)
        self.assertIn("mkdir", self.created[0]["instruction"])


class DispatchTool(unittest.IsolatedAsyncioTestCase):
    class Hub:
        def __init__(self, result=None, error=None):
            self.calls = []
            self.dirs = []
            self.result = {} if result is None else result
            self.error = error

        async def run(self, user_id, op, timeout=None, **args):
            self.calls.append((op, args))
            if self.error:
                raise self.error
            return self.result

        async def change_dir(self, user_id, path):
            self.dirs.append((user_id, path))
            return "C:/Users/me/Documents"

    async def test_mkdir_maps_to_hub_run(self):
        hub = self.Hub(result={"path": "player"})
        with patch.object(app, "HUB", hub):
            out = await app.dispatch_tool(1, "mkdir", {"path": "player"})
        self.assertEqual(hub.calls, [("mkdir", {"path": "player"})])
        self.assertEqual(out, {"path": "player"})

    async def test_cd_uses_change_dir_and_reports_cwd(self):
        hub = self.Hub()
        with patch.object(app, "HUB", hub):
            out = await app.dispatch_tool(1, "cd", {"path": "Documents"})
        self.assertEqual(hub.dirs, [(1, "Documents")])
        self.assertEqual(hub.calls, [])
        self.assertEqual(out, {"cwd": "C:/Users/me/Documents"})

    async def test_device_error_is_returned_not_raised(self):
        hub = self.Hub(error=devicehub.DeviceError("📴 \"desk\" hozir onlayn emas"))
        with patch.object(app, "HUB", hub):
            out = await app.dispatch_tool(1, "ls", {})
        self.assertEqual(out, {"error": "📴 \"desk\" hozir onlayn emas"})

    async def test_unknown_tool_is_refused_before_the_hub(self):
        hub = self.Hub()
        with patch.object(app, "HUB", hub):
            out = await app.dispatch_tool(1, "shutdown", {})
        self.assertIn("error", out)
        self.assertEqual(hub.calls, [])

    async def test_binary_ops_are_refused_before_the_hub(self):
        hub = self.Hub()
        with patch.object(app, "HUB", hub):
            out = await app.dispatch_tool(1, "get", {"path": "x.zip"})
        self.assertIn("error", out)
        self.assertEqual(hub.calls, [])

    async def test_only_declared_params_reach_the_agent(self):
        hub = self.Hub(result={})
        with patch.object(app, "HUB", hub):
            await app.dispatch_tool(1, "cat", {"path": "a.txt", "offset": 3800,
                                               "sudo": True, "shell": "rm -rf /"})
        self.assertEqual(hub.calls, [("cat", {"path": "a.txt", "offset": 3800})])

    async def test_empty_values_fall_back_to_agent_defaults(self):
        hub = self.Hub(result={})
        with patch.object(app, "HUB", hub):
            await app.dispatch_tool(1, "ls", {"path": ""})
        self.assertEqual(hub.calls, [("ls", {})])

    async def test_group_chat_dispatch_refuses_without_touching_the_hub(self):
        """Guruhda AI `ls` so'rasa ham qurilmaga buyruq bormaydi — javob dict
        bo'lishi shart, chunka javobsiz function_call Gemini tarixini buzadi."""
        hub = self.Hub()
        with patch.object(app, "HUB", hub):
            out = await app.tool_dispatch(1, False)("ls", {})
        self.assertIn("Guruh", out["error"])
        self.assertEqual(hub.calls, [])

    async def test_private_dispatch_reaches_the_hub(self):
        hub = self.Hub(result={"entries": []})
        with patch.object(app, "HUB", hub):
            out = await app.tool_dispatch(1, True)("ls", {})
        self.assertEqual(hub.calls, [("ls", {})])
        self.assertNotIn("error", out)


class StreamToolLoop(unittest.IsolatedAsyncioTestCase):
    async def collect(self, chat, prompt, dispatch=None):
        pieces = []

        async def drain():
            async for piece in core.stream_text(chat, prompt, dispatch):
                pieces.append(piece)

        # Worker o'lim-holatida qotib qolmasin: qotish = xato, cheksiz kutish emas.
        await asyncio.wait_for(drain(), timeout=10)
        return pieces

    async def test_plain_answer_never_touches_dispatch(self):
        chat = FakeChat([[text_chunk("Salom")]])
        recorder = Recorder()
        pieces = await self.collect(chat, "salom", recorder)
        self.assertEqual(pieces, ["Salom"])
        self.assertEqual(recorder.calls, [])

    async def test_function_call_is_executed_and_answered(self):
        chat = FakeChat([[call_chunk("c1", "mkdir", {"path": "player"})],
                         [text_chunk("✅ player papkasi yaratildi")]])
        recorder = Recorder(result={"path": "player"})
        pieces = await self.collect(chat, "menga player papka yarat", recorder)
        self.assertEqual(recorder.calls, [("mkdir", {"path": "player"})])
        self.assertEqual(pieces, ["✅ player papkasi yaratildi"])
        answer = chat.sent[1]
        self.assertEqual(len(answer), 1)
        self.assertEqual(answer[0].function_response.id, "c1")
        self.assertEqual(answer[0].function_response.name, "mkdir")
        self.assertEqual(answer[0].function_response.response, {"path": "player"})

    async def test_two_calls_in_one_turn_are_both_executed(self):
        chat = FakeChat([[call_chunk("c1", "mkdir", {"path": "a"}),
                         call_chunk("c2", "mkdir", {"path": "b"})],
                        [text_chunk("tayyor")]])
        recorder = Recorder(result={"ok": True})
        pieces = await self.collect(chat, "ikki papka", recorder)
        self.assertEqual(recorder.calls, [("mkdir", {"path": "a"}),
                                          ("mkdir", {"path": "b"})])
        self.assertEqual(len(chat.sent[1]), 2)
        self.assertEqual(pieces, ["tayyor"])

    async def test_dispatch_crash_is_answered_honestly(self):
        chat = FakeChat([[call_chunk("c1", "rm", {"path": "x.txt"})],
                         [text_chunk("o'chirib bo'lmadi")]])
        recorder = Recorder(error=RuntimeError("boom"))
        pieces = await self.collect(chat, "x.txt ni o'chir", recorder)
        response = chat.sent[1][0].function_response
        self.assertIn("error", response.response)
        self.assertIn("boom", str(response.response["error"]))
        self.assertEqual(pieces, ["o'chirib bo'lmadi"])

    async def test_limit_winds_down_into_a_summary(self):
        """8 amaldan keyin: chaqiruvlar bajarilmaydi, stub bilan yopiladi va
        modeldan xulosa so'raladi — foydalanuvchi xato emas, matn oladi."""
        streams = [[call_chunk(f"c{i}", "ls", {})] for i in range(9)] + [[text_chunk("mana xulosa")]]
        chat = FakeChat(streams)
        recorder = Recorder(result={})
        pieces = await self.collect(chat, "loyihani ko'rib chiq", recorder)
        joined = "".join(pieces)
        self.assertEqual(len(recorder.calls), 8, "chegaradan keyin birorta amal bajarilmasligi kerak")
        self.assertIn("mana xulosa", joined)
        self.assertIn("⏹️", joined, "foydalanuvchi to'xtatilganini bilishi kerak")
        stub = chat.sent[9][0].function_response.response
        self.assertIn("chegarasi", str(stub))

    async def test_model_that_ignores_the_limit_still_reports_it(self):
        streams = [[call_chunk(f"c{i}", "ls", {})] for i in range(14)]
        chat = FakeChat(streams)
        recorder = Recorder(result={})
        with self.assertRaises(core.ToolLoopLimit):
            await self.collect(chat, "ko'rib chiqaylik", recorder)
        self.assertEqual(len(recorder.calls), 8)
        self.assertEqual(len(chat.sent), 11, "8 amal + 2 stub davosi + birinchi xabar")

    async def test_dispatch_is_optional(self):
        chat = FakeChat([[text_chunk("oddiy javob")]])
        self.assertEqual(await self.collect(chat, "salom"), ["oddiy javob"])

    async def test_tool_call_without_dispatch_is_refused_not_dropped(self):
        # Javoblanmagan function_call Gemini tarixini buzadi — shuning uchun
        # bunday holatda suhbat davom etmaydi, lekin xato aniq ko'rinadi.
        chat = FakeChat([[call_chunk("c1", "ls", {})]])
        with self.assertRaises(core.ToolLoopLimit):
            await self.collect(chat, "papkani ko'rsat")
        self.assertEqual(len(chat.sent), 1)


class FakeSyncChat:
    """chat.send_message dublikati — oqimsiz zaxira yo'l tekshiriladi."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send_message(self, message, config=None):
        self.sent.append(message)
        if not self.responses:
            raise AssertionError("chatdan ortiqcha javob so'raldi")
        return self.responses.pop(0)


class SendTextFallback(unittest.IsolatedAsyncioTestCase):
    async def test_plain_answer_returned_as_text(self):
        chat = FakeSyncChat([text_chunk("Salom")])
        recorder = Recorder()
        text, response = await core.send_text(chat, "salom", recorder)
        self.assertEqual(text, "Salom")
        self.assertEqual(response.text, "Salom")
        self.assertEqual(recorder.calls, [])

    async def test_tool_call_is_answered_before_the_final_text(self):
        chat = FakeSyncChat([call_chunk("c1", "write", {"path": "a.txt", "text": "hi"}),
                             text_chunk("yozildi")])
        recorder = Recorder(result={"path": "a.txt"})
        text, _ = await core.send_text(chat, "a.txt ga hi yoz", recorder)
        self.assertEqual(recorder.calls, [("write", {"path": "a.txt", "text": "hi"})])
        self.assertEqual(text, "yozildi")
        self.assertEqual(chat.sent[1][0].function_response.response, {"path": "a.txt"})

    async def test_fallback_winds_down_instead_of_failing(self):
        responses = [call_chunk(f"c{i}", "ls", {}) for i in range(9)] + [text_chunk("mana xulosa")]
        chat = FakeSyncChat(responses)
        recorder = Recorder(result={})
        text, _ = await core.send_text(chat, "davom et", recorder)
        self.assertEqual(len(recorder.calls), 8)
        self.assertIn("mana xulosa", text)
        self.assertIn("⏹️", text)
        self.assertIn("chegarasi", str(chat.sent[9][0].function_response.response))

    async def test_fallback_shares_the_round_cap(self):
        chat = FakeSyncChat([call_chunk(f"c{i}", "ls", {}) for i in range(14)])
        recorder = Recorder(result={})
        with self.assertRaises(core.ToolLoopLimit):
            await core.send_text(chat, "davom et", recorder)
        self.assertEqual(len(recorder.calls), 8)
        self.assertEqual(len(chat.sent), 11)


class DeviceChat:
    id = 5
    type = "private"


class DeviceMessage:
    chat = DeviceChat()
    message_id = 1


class ToolLimitFallback(unittest.IsolatedAsyncioTestCase):
    """Chegarada zaxira yo'l qayta ishga tushmasligi kerak: aks holda 8 qadam
    kompyuterda ikki marta bajariladi (fayllar, o'chirishlar takrorlanadi)."""

    async def test_stream_limit_is_not_retried_through_send_text(self):
        import contextlib

        session = {"chat": "chat:X", "search": False}
        delivered = []

        async def fake_stream_reply(*args, **kwargs):
            raise core.ToolLoopLimit("8 qadamdan ortiqcha amal so'raldi")

        @contextlib.asynccontextmanager
        async def fake_keep_typing(chat_id, action=None):
            yield

        async def fake_send_long(message, text, user_id, markup=None, sources=None):
            delivered.append(text)
            return "sent"

        async def fail_send_text(*args, **kwargs):
            raise AssertionError("zaxira yo'l amallarni qayta bajarishi mumkin emas")

        with patch.object(app, "keep_typing", fake_keep_typing), \
                patch.object(app, "ensure_session", lambda user_id: session), \
                patch.object(app, "tool_dispatch", lambda user_id, private: None), \
                patch.object(app, "stream_reply", fake_stream_reply), \
                patch.object(app, "send_long", fake_send_long), \
                patch.object(app.core, "send_text", fail_send_text), \
                patch.object(app.core.store, "add_message"), \
                patch.object(app.core.store, "bump"):
            await app.ask_ai(DeviceMessage(), "davom et", 1)

        self.assertEqual(len(delivered), 1)
        self.assertIn("⏹️", delivered[0], "foydalanuvchi chegarani davom ettirish yo'lini ko'rsin")


if __name__ == "__main__":
    unittest.main()
