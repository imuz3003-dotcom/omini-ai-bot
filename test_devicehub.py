"""devicehub.py — bot tomonidagi qurilma ulanishi, pairing va buyruq yuborish.

Nima uchun kerak: bu qatlam "kim qaysi kompyuterga buyruq bera oladi" degan
savolga javob beradi. Bu yerda xato = begona odamning fayllarga kirishi.
Shuning uchun testlar HMAC imzoni, kodning bir martaligini, urinish limitini,
foydalanuvchilararo izolatsiyani va uzilgan kanaldagi buyruqning "osilib"
qolmasligini qoplaydi. Socket — `FakeSocket` orqali: u aiohttp WS'ning
haqiqiy shakli (async iteratsiya + send_str + close) kabi yuradi.

Ishga tushirish:  python -m unittest discover -s . -p "test_*.py"
"""

import asyncio
import json
import time
import unittest
from unittest import mock

import devicehub
from companion import protocol

SECRET = "test-device-secret"
AGENT = "a" * 32
FAST = 0.2  # buyruq javobini kutish uchun qisqa timeout (test sekinlamasin)


class FakeMessage:
    """aiohttp `async for` WSMessage qaytaradi (matn `.data`da) — shu shakl."""

    def __init__(self, data):
        self.type = 1  # WSMsgType.TEXT
        self.data = data


class FakeSocket:
    """aiohttp WebSocket'ning halol dublikati: qatorlarni navbatdan oladi."""

    def __init__(self):
        self.sent = []
        self.closed = None
        self._inbox = asyncio.Queue()

    def __aiter__(self):
        return self._rows()

    async def _rows(self):
        while True:
            row = await self._inbox.get()
            if row is None:
                return
            yield FakeMessage(row)

    async def send_str(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=None, **_kwargs):
        self.closed = code
        await self._inbox.put(None)

    async def feed(self, frame):
        await self._inbox.put(protocol.encode(frame))

    def sent_of(self, kind):
        return [frame for frame in self.sent if frame.get("type") == kind]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def hub(clock=None):
    return devicehub.DeviceHub(SECRET, clock=clock or Clock())


def bind_of(agent_id=AGENT, user_id=42, secret=SECRET):
    return devicehub.bind_token(secret, agent_id, user_id)


async def settle(times: int = 6):
    for _ in range(times):
        await asyncio.sleep(0)


class BindToken(unittest.TestCase):
    def test_same_inputs_give_same_token(self):
        self.assertEqual(bind_of(), bind_of())

    def test_token_is_64_hex(self):
        token = bind_of()
        self.assertEqual(len(token), 64)
        self.assertTrue(all(char in "0123456789abcdef" for char in token))

    def test_other_user_changes_token(self):
        self.assertNotEqual(bind_of(user_id=42), bind_of(user_id=43))

    def test_other_agent_changes_token(self):
        self.assertNotEqual(bind_of(agent_id=AGENT), bind_of(agent_id="b" * 32))

    def test_other_secret_changes_token(self):
        self.assertNotEqual(bind_of(), devicehub.bind_token("boshqa-maxfiy", AGENT, 42))


class PairingCodes(unittest.TestCase):
    def test_register_returns_six_digits(self):
        code = hub().register(AGENT, "desk-pc", "C:/Users/me")
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_register_is_idempotent_per_agent(self):
        instance = hub()
        self.assertEqual(instance.register(AGENT, "desk-pc", "C:/Users/me"),
                         instance.register(AGENT, "desk-pc", "C:/Users/me"))

    def test_pair_returns_bind_for_the_owner(self):
        instance = hub()
        code = instance.register(AGENT, "desk-pc", "C:/Users/me")
        record = instance.pair(42, code)
        self.assertEqual(record["agent_id"], AGENT)
        self.assertEqual(record["bind"], bind_of(user_id=42))

    def test_code_works_only_once(self):
        instance = hub()
        code = instance.register(AGENT, "desk-pc", "C:/Users/me")
        instance.pair(42, code)
        with self.assertRaises(devicehub.DeviceError):
            instance.pair(43, code)

    def test_unknown_code_is_refused(self):
        with self.assertRaises(devicehub.DeviceError):
            hub().pair(42, "000000")

    def test_non_digit_code_is_refused(self):
        with self.assertRaises(devicehub.DeviceError):
            hub().pair(42, "salom")

    def test_expired_code_is_refused(self):
        clock = Clock()
        instance = hub(clock)
        code = instance.register(AGENT, "desk-pc", "C:/Users/me")
        clock.advance(11 * 60)
        with self.assertRaises(devicehub.DeviceError):
            instance.pair(42, code)

    def test_too_many_wrong_codes_locks_the_user(self):
        instance = hub()
        code = instance.register(AGENT, "desk-pc", "C:/Users/me")
        for _ in range(5):
            with self.assertRaises(devicehub.DeviceError):
                instance.pair(42, "111111")
        with self.assertRaises(devicehub.DeviceError):
            instance.pair(42, code)

    def test_wrong_codes_of_one_user_do_not_lock_another(self):
        instance = hub()
        code = instance.register(AGENT, "desk-pc", "C:/Users/me")
        for _ in range(5):
            try:
                instance.pair(42, "111111")
            except devicehub.DeviceError:
                pass
        self.assertEqual(instance.pair(43, code)["agent_id"], AGENT)


class Attach(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instance = hub()
        self.code = self.instance.register(AGENT, "desk-pc", "C:/Users/me")
        self.record = self.instance.pair(42, self.code)

    async def test_valid_bind_marks_device_online_for_its_owner(self):
        socket = FakeSocket()
        await self.instance.attach(AGENT, socket, bind=bind_of(user_id=42), owner_user_id=42)
        devices = self.instance.devices_of(42)
        self.assertEqual(len(devices), 1)
        self.assertTrue(devices[0]["online"])

    async def test_forged_bind_is_refused_and_socket_closed(self):
        socket = FakeSocket()
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.attach(AGENT, socket, bind="0" * 64, owner_user_id=42)
        await settle()
        self.assertEqual(socket.closed, 4401)

    async def test_bind_of_another_owner_is_refused(self):
        socket = FakeSocket()
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.attach(AGENT, socket, bind=bind_of(user_id=99), owner_user_id=99)

    async def test_pending_socket_receives_bound_frame(self):
        instance = hub()
        instance.register(AGENT, "desk-pc", "C:/Users/me")
        socket = FakeSocket()
        await instance.attach(AGENT, socket)                      # bindsiz — pairing kutilmoqda
        instance.pair(42, instance.register(AGENT, "desk-pc", "C:/Users/me"))
        await settle()
        frames = socket.sent_of("bound")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["bind"], bind_of(user_id=42))
        self.assertEqual(frames[0]["owner_user_id"], 42)

    async def test_pending_socket_closes_if_nobody_pairs(self):
        instance = hub()
        instance.register(AGENT, "desk-pc", "C:/Users/me")
        socket = FakeSocket()
        with mock.patch.object(devicehub, "PAIR_TTL", 0.02):
            await instance.attach(AGENT, socket)
            await asyncio.sleep(0.15)
        self.assertEqual(socket.closed, 4408)

    async def test_second_attach_replaces_the_stale_socket(self):
        first, second = FakeSocket(), FakeSocket()
        await self.instance.attach(AGENT, first, bind=bind_of(user_id=42), owner_user_id=42)
        await self.instance.attach(AGENT, second, bind=bind_of(user_id=42), owner_user_id=42)
        await settle()
        self.assertIsNotNone(first.closed)
        self.assertIsNone(second.closed)
        self.assertEqual(len(self.instance.devices_of(42)), 1)

    async def test_sixth_device_for_one_user_is_refused(self):
        for index in range(5):
            agent = f"{index:032x}"
            self.instance.pair(42, self.instance.register(agent, f"pc-{index}", "C:/x"))
            await self.instance.attach(agent, FakeSocket(), bind=bind_of(agent_id=agent), owner_user_id=42)
        sixth = "f" * 32
        self.instance.pair(42, self.instance.register(sixth, "pc-6", "C:/x"))
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.attach(sixth, FakeSocket(), bind=bind_of(agent_id=sixth), owner_user_id=42)


class RunningCommands(unittest.IsolatedAsyncioTestCase):
    FAST = 0.2

    async def asyncSetUp(self):
        self.instance = hub()
        code = self.instance.register(AGENT, "desk-pc", "C:/Users/me")
        self.instance.pair(42, code)
        self.socket = FakeSocket()
        await self.instance.attach(AGENT, self.socket, bind=bind_of(user_id=42), owner_user_id=42)
        await settle()

    async def answer_next(self, frame_builder):
        """Agent javobini simulyatsiya qiladi: yuborilgan cmd id'sini o'qib olamiz."""
        await settle()
        command = self.socket.sent_of("cmd")[-1]
        await self.socket.feed(frame_builder(command["id"]))
        await settle()

    async def test_agent_error_text_reaches_the_caller(self):
        task = asyncio.create_task(self.instance.run(42, "cat", path="../x", timeout=self.FAST))
        await settle()
        command = self.socket.sent_of("cmd")[-1]
        await self.socket.feed(protocol.result_error(command["id"], "Denied: ruxsat yo'q: ildizdan tashqarida"))
        with self.assertRaises(devicehub.DeviceError) as caught:
            await task
        self.assertIn("ruxsat yo'q", str(caught.exception))

    async def test_run_sends_op_and_args_with_cwd(self):
        task = asyncio.create_task(self.instance.run(42, "ls", path="Documents", timeout=self.FAST))
        await settle()
        command = self.socket.sent_of("cmd")[-1]
        await self.socket.feed(protocol.result_ok(command["id"], {"entries": []}))
        await task
        self.assertEqual(command["op"], "ls")
        self.assertEqual(command["args"]["path"], "Documents")
        self.assertEqual(command["args"]["cwd"], "C:/Users/me")

    async def test_run_returns_agent_data(self):
        task = asyncio.create_task(self.instance.run(42, "disk", timeout=self.FAST))
        await self.answer_next(lambda cid: protocol.result_ok(cid, {"volumes": [{"free": 1}]}))
        result = await task
        self.assertEqual(result["volumes"][0]["free"], 1)

    async def test_timeout_raises_device_error(self):
        instance = self.instance
        task = asyncio.create_task(instance.run(42, "tree", timeout=0.05))
        await settle()
        with self.assertRaises(devicehub.DeviceError):
            await task

    async def test_other_user_cannot_reach_the_device(self):
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.run(777, "ls", timeout=FAST)
        self.assertEqual(self.socket.sent_of("cmd"), [])

    async def test_offline_user_without_device_gets_error(self):
        instance = hub()
        with self.assertRaises(devicehub.DeviceError):
            await instance.run(5, "ls", timeout=FAST)

    async def test_detach_fails_the_inflight_command_at_once(self):
        task = asyncio.create_task(self.instance.run(42, "tree", timeout=5.0))
        await settle()
        started = time.monotonic()
        await self.instance.detach(AGENT, self.socket)
        with self.assertRaises(devicehub.DeviceError):
            await task
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_second_command_while_channel_is_busy_is_refused(self):
        task = asyncio.create_task(self.instance.run(42, "tree", timeout=self.FAST))
        await settle()
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.run(42, "ls", timeout=self.FAST)
        command = self.socket.sent_of("cmd")[-1]
        await self.socket.feed(protocol.result_ok(command["id"], {"lines": []}))
        await task

    async def test_timed_out_command_frees_the_channel(self):
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.run(42, "tree", timeout=0.02)
        task = asyncio.create_task(self.instance.run(42, "ls", timeout=self.FAST))
        await settle()
        await self.socket.feed(protocol.result_ok(self.socket.sent_of("cmd")[-1]["id"], {"entries": []}))
        self.assertEqual(await task, {"entries": []})

    async def test_change_dir_updates_cwd_only_when_agent_agrees(self):
        task = asyncio.create_task(self.instance.change_dir(42, "Documents"))
        await settle()
        command = self.socket.sent_of("cmd")[-1]
        self.assertEqual(command["op"], "stat")
        await self.socket.feed(protocol.result_ok(command["id"], {"path": "C:/Users/me/Documents", "dir": 1}))
        await task
        next_task = asyncio.create_task(self.instance.run(42, "ls", timeout=self.FAST))
        await settle()
        self.assertEqual(self.socket.sent_of("cmd")[-1]["args"]["cwd"], "C:/Users/me/Documents")
        await self.socket.feed(protocol.result_ok(self.socket.sent_of("cmd")[-1]["id"], {"entries": []}))
        await next_task

    async def test_failed_change_dir_keeps_previous_cwd(self):
        task = asyncio.create_task(self.instance.change_dir(42, "Yo'q"))
        await settle()
        command = self.socket.sent_of("cmd")[-1]
        await self.socket.feed(protocol.result_error(command["id"], "FileNotFoundError: yo'q"))
        with self.assertRaises(devicehub.DeviceError):
            await task
        next_task = asyncio.create_task(self.instance.run(42, "ls", timeout=self.FAST))
        await settle()
        self.assertEqual(self.socket.sent_of("cmd")[-1]["args"]["cwd"], "C:/Users/me")
        await self.socket.feed(protocol.result_ok(self.socket.sent_of("cmd")[-1]["id"], {"entries": []}))
        await next_task


class Selection(unittest.IsolatedAsyncioTestCase):
    """Bir necha qurilmada buyruq qaysi biriga ketishi — `/device <nom>` boshqaradi."""

    async def asyncSetUp(self):
        self.instance = hub()
        self.sockets = {}
        for index, name in ((0xA, "desk-pc"), (0xB, "note-pc")):
            agent = f"{index:032x}"
            self.instance.pair(42, self.instance.register(agent, name, "C:/Users/me"))
            socket = FakeSocket()
            self.sockets[name] = socket
            await self.instance.attach(agent, socket,
                                       bind=bind_of(agent_id=agent), owner_user_id=42)

    async def answer(self, socket):
        await settle()
        command = socket.sent_of("cmd")[-1]
        await socket.feed(protocol.result_ok(command["id"], {"volumes": []}))

    async def test_last_connected_device_is_used(self):
        task = asyncio.create_task(self.instance.run(42, "disk", timeout=FAST))
        await settle()
        self.assertEqual(len(self.sockets["note-pc"].sent_of("cmd")), 1)
        self.assertEqual(self.sockets["desk-pc"].sent_of("cmd"), [])
        await self.answer(self.sockets["note-pc"])
        await task

    async def test_select_by_name_switches_the_target(self):
        self.instance.select(42, "desk-pc")
        task = asyncio.create_task(self.instance.run(42, "disk", timeout=FAST))
        await settle()
        self.assertEqual(len(self.sockets["desk-pc"].sent_of("cmd")), 1)
        await self.answer(self.sockets["desk-pc"])
        await task

    async def test_select_of_an_unknown_name_lists_the_devices(self):
        with self.assertRaises(devicehub.DeviceError) as caught:
            self.instance.select(42, "telefon")
        self.assertIn("desk-pc", str(caught.exception))

    async def test_select_of_another_users_device_is_refused(self):
        with self.assertRaises(devicehub.DeviceError):
            self.instance.select(777, "desk-pc")


class Revocation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instance = hub()
        code = self.instance.register(AGENT, "desk-pc", "C:/Users/me")
        self.instance.pair(42, code)
        self.socket = FakeSocket()
        await self.instance.attach(AGENT, self.socket, bind=bind_of(user_id=42), owner_user_id=42)

    async def test_unpair_drops_the_device_and_closes_the_socket(self):
        self.instance.unpair(42, "desk-pc")
        await settle()
        self.assertEqual(self.instance.devices_of(42), [])
        self.assertIsNotNone(self.socket.closed)

    async def test_unpaired_agent_cannot_reconnect(self):
        self.instance.unpair(42, "desk-pc")
        await settle()
        with self.assertRaises(devicehub.DeviceError):
            await self.instance.attach(AGENT, FakeSocket(), bind=bind_of(user_id=42), owner_user_id=42)

    async def test_unpair_of_another_users_device_is_refused(self):
        with self.assertRaises(devicehub.DeviceError):
            self.instance.unpair(777, "desk-pc")


class Routes(unittest.IsolatedAsyncioTestCase):
    """/device/register — faqat ildizdan kod qaytadi, boshqa narsa uzilmaydi."""

    async def test_register_handler_returns_code(self):
        instance = hub()
        response = await devicehub.handle_register(fake_request(instance, {"agent_id": AGENT, "name": "pc", "root": "C:/x"}))
        self.assertEqual(json.loads(response.text)["agent_id"], AGENT)
        self.assertTrue(json.loads(response.text)["code"].isdigit())

    async def test_register_without_agent_id_is_400(self):
        instance = hub()
        response = await devicehub.handle_register(fake_request(instance, {"name": "pc"}))
        self.assertEqual(response.status, 400)

    async def test_register_rejects_odd_agent_id(self):
        instance = hub()
        response = await devicehub.handle_register(fake_request(instance, {"agent_id": "../../etc", "root": "C:/x"}))
        self.assertEqual(response.status, 400)


def fake_request(instance, query):
    class Request:
        def __init__(self):
            self.app = {"hub": instance}
            self.query = query

    return Request()


if __name__ == "__main__":
    unittest.main()
