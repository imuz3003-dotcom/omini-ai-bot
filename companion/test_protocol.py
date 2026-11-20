"""companion/protocol.py — bot va agent o'rtasidagi kadr shakli tekshiriladi.

Nima uchun kerak: protokoldagi bitta bo'sh joy yoki noto'g'ri `v` butun
ulanganni buzuq xabar bilan portlatishi mumkin. Bu testlar kadarni ikkala
tomon ham qabul qila oladigan yagona shaklga mahkamlaydi va yomon kadr
assotsiatsiya bo'lib ketmasligini kafolatlaydi.

Ishga tushirish:  python -m unittest discover -s . -p "test_*.py"
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion import protocol  # noqa: E402


class Builders(unittest.TestCase):
    def test_cmd_frame_shape(self):
        self.assertEqual(
            protocol.cmd("9f2c1a", "ls", path="Documents"),
            {"v": 1, "type": "cmd", "id": "9f2c1a", "op": "ls", "args": {"path": "Documents"}},
        )

    def test_cmd_frame_without_args(self):
        self.assertEqual(
            protocol.cmd("ab", "disk"),
            {"v": 1, "type": "cmd", "id": "ab", "op": "disk", "args": {}},
        )

    def test_result_ok_frame_shape(self):
        self.assertEqual(
            protocol.result_ok("9f2c1a", {"cwd": "/x"}),
            {"v": 1, "type": "res", "id": "9f2c1a", "ok": True, "data": {"cwd": "/x"}},
        )

    def test_result_error_frame_shape(self):
        self.assertEqual(
            protocol.result_error("9f2c1a", "Denied: ruxsat yo'q"),
            {"v": 1, "type": "res", "id": "9f2c1a", "ok": False, "error": "Denied: ruxsat yo'q"},
        )

    def test_hello_frame_carries_identity(self):
        self.assertEqual(
            protocol.hello("a1b2", "desk-pc", bind="beef", owner_user_id=42),
            {"v": 1, "type": "hello", "agent_id": "a1b2", "name": "desk-pc",
             "bind": "beef", "owner_user_id": 42, "root": None},
        )

    def test_hello_carries_the_root_for_readmission(self):
        """Bot ro'yxati restart'da o'chadi — qayta qabulda papka nomini hello
        o'zi olib keladi, aks holda `/device` bo'sh root ko'rsatardi."""
        frame = protocol.hello("a1b2", "desk-pc", bind="beef", owner_user_id=42,
                               root="C:/Users/me")
        self.assertEqual(frame["root"], "C:/Users/me")

    def test_bound_frame_shape(self):
        self.assertEqual(
            protocol.bound("beef", 42),
            {"v": 1, "type": "bound", "bind": "beef", "owner_user_id": 42},
        )

    def test_new_id_is_hex_and_unique(self):
        first, second = protocol.new_id(), protocol.new_id()
        self.assertEqual(len(first), 12)
        self.assertTrue(all(char in "0123456789abcdef" for char in first))
        self.assertNotEqual(first, second)


class Decode(unittest.TestCase):
    def test_roundtrip(self):
        frame = protocol.cmd("1", "cat", path="a.txt", offset=0)
        self.assertEqual(protocol.decode(protocol.encode(frame)), frame)

    def test_rejects_not_json(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode("salom")

    def test_rejects_json_that_is_not_object(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode("[1, 2]")

    def test_rejects_other_version(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 2, "type": "pong"}))

    def test_rejects_unknown_type(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "teleport"}))

    def test_rejects_cmd_without_op(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "cmd", "id": "1"}))

    def test_rejects_res_without_id(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "res", "ok": True, "data": {}}))

    def test_rejects_res_without_ok_flag(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "res", "id": "1", "data": {}}))

    def test_rejects_cmd_with_non_string_op(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "cmd", "id": "1", "op": 7, "args": {}}))

    def test_rejects_cmd_with_non_dict_args(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(json.dumps({"v": 1, "type": "cmd", "id": "1", "op": "ls", "args": "x"}))

    def test_accepts_ping_and_pong(self):
        self.assertEqual(protocol.decode('{"v": 1, "type": "ping"}'), {"v": 1, "type": "ping"})
        self.assertEqual(protocol.decode('{"v": 1, "type": "pong"}'), {"v": 1, "type": "pong"})


class ErrorText(unittest.TestCase):
    def test_describe_error_names_the_exception_type(self):
        text = protocol.describe_error(FileNotFoundError("bunday yo'l yo'q: /x"))
        self.assertEqual(text, "FileNotFoundError: bunday yo'l yo'q: /x")

    def test_describe_error_keeps_denied_prefix(self):
        from companion import ops

        text = protocol.describe_error(ops.Denied("ruxsat yo'q: ildizdan tashqarida"))
        self.assertTrue(text.startswith("Denied: "))
        self.assertIn("ruxsat yo'q", text)

    def test_encode_is_utf8_json(self):
        frame = protocol.result_ok("1", {"text": "salom дунё"})
        self.assertEqual(json.loads(protocol.encode(frame))["data"]["text"], "salom дунё")


if __name__ == "__main__":
    unittest.main()
