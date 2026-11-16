"""companion/agent.py — agentning mafhum qismi (kadr → amal → javob) tekshiriladi.

Nima uchun kerak: agent foydalanuvchining kompyuterida ishlaydi, lekin uni
tarmoqsiz sinab bo'lmaydigan qilib bo'laklarga ajratish mumkin: `handle_frame`
haqiqiy `ops.py`ni chaqiradi va haqiqiy fayllarga tegadi — ulanish (aiohttp)
butunlay tashqarida qoladi. Shunda rad etish matnlari, xato nomlari va bind
saqlanishi fayl yozuvlari orqali tekshiriladi.

Ishga tushirish:  python -m unittest discover -s . -p "test_*.py"
"""

import asyncio
import base64
import contextlib
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion import agent, ops, protocol  # noqa: E402


class ConfigFile(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="omini_agent_"))
        self.root = self.base / "root"
        self.root.mkdir()
        self.path = self.base / "nested" / "agent.json"

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(agent.load_config(self.path))

    def test_new_config_generates_stable_agent_id(self):
        config = agent.new_config("https://bot.example", [self.root], "desk-pc")
        first = config["agent_id"]
        self.assertEqual(len(first), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in first))
        self.assertIsNone(config["bind"])
        self.assertEqual(config["roots"], [str(self.root)])

    def test_save_then_load_roundtrip(self):
        config = agent.new_config("https://bot.example", [self.root], "desk-pc")
        agent.save_config(self.path, config)
        self.assertEqual(agent.load_config(self.path), config)

    def test_save_creates_parent_directory(self):
        config = agent.new_config("https://bot.example", [self.root], "desk-pc")
        agent.save_config(self.path, config)
        self.assertTrue(self.path.is_file())

    def test_saved_file_is_readable_json(self):
        config = agent.new_config("https://bot.example", [self.root], "desk-pc")
        agent.save_config(self.path, config)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["name"], "desk-pc")

    def test_default_config_path_is_named_and_grouped(self):
        path = agent.default_config_path()
        self.assertEqual(path.name, "agent.json")
        expected = "omini-agent" if os.name == "nt" else ".omini"
        self.assertEqual(path.parent.name, expected)


class UrlAndBackoff(unittest.TestCase):
    def test_https_becomes_wss_with_device_path(self):
        self.assertEqual(agent.ws_url("https://omini.example.onrender.com"),
                         "wss://omini.example.onrender.com/device/ws")

    def test_trailing_slash_is_not_doubled(self):
        self.assertEqual(agent.ws_url("http://localhost:8080/"),
                         "ws://localhost:8080/device/ws")

    def test_backoff_grows_then_caps(self):
        steps = [agent.backoff(attempt) for attempt in range(12)]
        self.assertLess(steps[0], steps[1])
        self.assertLess(steps[1], steps[2])
        self.assertTrue(all(step <= 60 for step in steps))
        self.assertEqual(steps[-1], steps[-2])

    def test_backoff_never_negative(self):
        self.assertGreaterEqual(agent.backoff(0), 0.5)


def bound_state(test, root):
    config = agent.new_config("https://bot.example", [root], "desk-pc")
    config["bind"] = "deadbeef"
    config["owner_user_id"] = 42
    path = test.base / "agent.json"
    agent.save_config(path, config)
    return agent.AgentState(config, path)


class HandleFrame(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="omini_agent_"))
        self.root = self.base / "root"
        (self.root / "Documents").mkdir(parents=True)
        (self.root / "Documents" / "a.txt").write_text("salom", encoding="utf-8")
        self.state = bound_state(self, self.root)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def run_op(self, op, **args):
        frame = protocol.cmd("c1", op, **args)
        result = agent.handle_frame(self.state, frame)
        self.assertEqual(result["type"], "res")
        self.assertEqual(result["id"], "c1")
        return result

    def test_ls_lists_real_entries(self):
        result = self.run_op("ls", cwd=str(self.root), path="Documents")
        self.assertTrue(result["ok"])
        self.assertEqual([e["name"] for e in result["data"]["entries"]], ["a.txt"])

    def test_cat_returns_text(self):
        result = self.run_op("cat", cwd=str(self.root), path="Documents/a.txt")
        self.assertEqual(result["data"]["text"], "salom")

    def test_unknown_op_is_reported_by_name(self):
        result = self.run_op("teleport", cwd=str(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("teleport", result["error"])

    def test_escape_attempt_returns_denied_text(self):
        result = self.run_op("cat", cwd=str(self.root), path="../../Windows/win.ini")
        self.assertFalse(result["ok"])
        self.assertIn("ruxsat yo'q", result["error"])

    def test_missing_file_names_the_exception(self):
        result = self.run_op("cat", cwd=str(self.root), path="nope.txt")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("FileNotFoundError"))

    def test_get_sends_base64_payload(self):
        result = self.run_op("get", cwd=str(self.root), path="Documents/a.txt")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["encoding"], "base64")
        self.assertEqual(base64.b64decode(result["data"]["b64"]), b"salom")

    def test_put_writes_base64_payload(self):
        result = self.run_op("put", cwd=str(self.root), path="in.bin",
                             b64=base64.b64encode(b"\x00\x01").decode())
        self.assertTrue(result["ok"])
        self.assertEqual((self.root / "in.bin").read_bytes(), b"\x00\x01")

    def test_put_with_bad_base64_is_error_not_crash(self):
        result = self.run_op("put", cwd=str(self.root), path="in.bin", b64="!!!")
        self.assertFalse(result["ok"])

    def test_get_refuses_file_over_transfer_limit(self):
        big = self.root / "big.bin"
        with big.open("wb") as handle:
            handle.truncate(ops.MAX_GET_BYTES + 1)
        result = self.run_op("get", cwd=str(self.root), path="big.bin")
        self.assertFalse(result["ok"])
        self.assertIn("juda katta", result["error"])

    def test_cwd_outside_roots_is_denied(self):
        result = self.run_op("ls", cwd=str(self.base))
        self.assertFalse(result["ok"])

    def test_rm_refuses_directory_through_the_frame(self):
        result = self.run_op("rm", cwd=str(self.root), path="Documents")
        self.assertFalse(result["ok"])
        self.assertTrue((self.root / "Documents").exists())


class BindState(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="omini_agent_"))
        self.root = self.base / "root"
        self.root.mkdir()
        self.config = agent.new_config("https://bot.example", [self.root], "desk-pc")
        self.path = self.base / "agent.json"
        agent.save_config(self.path, self.config)
        self.state = agent.AgentState(self.config, self.path)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_command_before_binding_is_refused(self):
        result = agent.handle_frame(self.state, protocol.cmd("c1", "ls", cwd=str(self.root)))
        self.assertFalse(result["ok"])
        self.assertIn("ulanmagan", result["error"])

    def test_bound_frame_is_persisted_to_disk(self):
        agent.handle_frame(self.state, protocol.bound("beefcafe", 42))
        stored = agent.load_config(self.path)
        self.assertEqual(stored["bind"], "beefcafe")
        self.assertEqual(stored["owner_user_id"], 42)

    def test_bound_frame_returns_nothing(self):
        self.assertIsNone(agent.handle_frame(self.state, protocol.bound("beefcafe", 42)))

    def test_ping_answers_pong(self):
        self.assertEqual(agent.handle_frame(self.state, {"v": 1, "type": "ping"}),
                         {"v": 1, "type": "pong"})

    def test_hello_and_result_frames_are_ignored(self):
        self.assertIsNone(agent.handle_frame(self.state, protocol.hello("a1", "pc")))
        self.assertIsNone(agent.handle_frame(self.state, protocol.result_ok("x", {})))


class BindingRevoked(unittest.TestCase):
    """`/unpair` yoki o'zgargan `DEVICE_SECRET`dan keyin agent o'z-o'zidan tuzaladi.

    Nima uchun kerak: bot bindingni bekor qilsa, agent eski `bind`ni saqlab
    qolgani uchun abadiy «ro'yxatda yo'q» deb rad etilardi — foydalanuvchi esa
    agentni o'chirib-qayta yoqishga majbur bo'lardi.
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="omini_agent_"))
        self.root = self.base / "root"
        self.root.mkdir()
        self.state = bound_state(self, self.root)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_reset_binding_clears_and_persists(self):
        agent.reset_binding(self.state)
        self.assertFalse(self.state.bound)
        self.assertIsNone(agent.load_config(self.state.path)["bind"])
        self.assertIsNone(agent.load_config(self.state.path)["owner_user_id"])

    def test_revoked_close_code_drops_the_binding(self):
        # 4401 = «ro'yxatda yo'q/imzo yaroqsiz», 4403 = «/unpair» — devicehub'dagi
        # tarmoq kontrakti, shuning uchun bu yerda qo'lda yozilgan.
        for code in (4401, 4403):
            state = bound_state(self, self.root)
            agent.on_closed(state, SimpleNamespace(close_code=code))
            self.assertFalse(state.bound, f"close_code={code}")

    def test_ordinary_close_keeps_the_binding(self):
        for code in (None, 1000, 4408, 4409):
            state = bound_state(self, self.root)
            agent.on_closed(state, SimpleNamespace(close_code=code))
            self.assertTrue(state.bound, f"close_code={code}")

    def test_socket_without_close_code_is_not_a_crash(self):
        agent.on_closed(self.state, object())
        self.assertTrue(self.state.bound)


class FakeMessage:
    """aiohttp `async for` WSMessage qaytaradi (matn `.data`da) — shu shakl."""

    def __init__(self, data):
        self.type = 1  # WSMsgType.TEXT
        self.data = data


class FakeSocket:
    """WebSocket o'rniga: kelgan qatorlarni beradi, yuborilganlarni yozib oladi."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    def __aiter__(self):
        return self._rows()

    async def _rows(self):
        for row in self.incoming:
            yield FakeMessage(row)

    async def send_str(self, text):
        self.sent.append(json.loads(text))


class FramePump(unittest.TestCase):
    """`_pump` — tarmoq chegarasi: bu yerda yolg'on kadr ham sinovdan o'tadi."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="omini_agent_"))
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_text("salom", encoding="utf-8")
        self.state = bound_state(self, self.root)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def pump(self, incoming):
        socket = FakeSocket(incoming)
        asyncio.run(agent.pump(socket, self.state))
        return socket

    def test_command_gets_a_result(self):
        frame = protocol.cmd("c1", "ls", cwd=str(self.root))
        socket = self.pump([protocol.encode(frame)])
        self.assertEqual(len(socket.sent), 1)
        self.assertEqual(socket.sent[0]["id"], "c1")
        self.assertEqual([e["name"] for e in socket.sent[0]["data"]["entries"]], ["a.txt"])

    def test_garbage_gets_channel_error_and_next_frame_still_works(self):
        good = protocol.cmd("c2", "ls", cwd=str(self.root))
        socket = self.pump(["salom", protocol.encode(good)])
        self.assertEqual(socket.sent[0]["type"], "err")
        self.assertEqual(socket.sent[1]["id"], "c2")

    def test_ping_is_answered_with_pong(self):
        socket = self.pump([protocol.encode(protocol.ping())])
        self.assertEqual(socket.sent, [{"v": 1, "type": "pong"}])

    def test_bound_frame_sends_nothing(self):
        socket = self.pump([protocol.encode(protocol.bound("beef", 42))])
        self.assertEqual(socket.sent, [])
        self.assertEqual(agent.load_config(self.state.path)["bind"], "beef")

    def test_bot_refusal_is_echoed_to_the_operator(self):
        """Bot kanalni yopib «/pair kodini kiriting» desa, odam buni ko'rishi shart."""
        err = protocol.channel_error("Bu qurilma ro'yxatda yo'q — Telegramda /pair kodini kiriting.")
        with mock.patch("builtins.print") as echoed:
            self.pump([protocol.encode(err)])
        text = " ".join(str(call) for call in echoed.call_args_list)
        self.assertIn("/pair kodini kiriting", text)


class ServeRetry(unittest.IsolatedAsyncioTestCase):
    """`serve` — botga qayta-ulinish tartibi.

    Nima uchun kerak: ro'yxatdan o'tmagan agent kanalni birinchi sekuntda
    yo'qotadi va oraliq bo'lmasa Render'dagi bitta free instansiyani zarba
    qiladi (e2e tekshiruvda 4 sekundda 83 ulanish).
    """

    async def drive(self, durations):
        clock = {"t": 0.0}
        delays = []
        enough = asyncio.Event()
        lengths = itertools.cycle(durations)

        async def connect(_state):
            clock["t"] += next(lengths)

        async def sleep(delay):
            delays.append(delay)
            clock["t"] += delay
            if len(delays) == len(durations):
                enough.set()
            await asyncio.sleep(0)

        task = asyncio.create_task(agent.serve(
            None, connect=connect, sleep=sleep, now=lambda: clock["t"]))
        try:
            await asyncio.wait_for(enough.wait(), timeout=2.0)
        finally:
            # Boshqa holatda `serve` cheksiz aylanadi: to'xtatish shart.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return delays[:len(durations)]

    async def test_short_sessions_back_off_exponentially(self):
        delays = await self.drive([0.0] * 5)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0])

    async def test_a_long_session_resets_the_wait(self):
        stable = agent.STABLE_SESSION_SECONDS + 5
        delays = await self.drive([0.0, 0.0, stable, 0.0])
        self.assertEqual(delays, [1.0, 2.0, 1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
