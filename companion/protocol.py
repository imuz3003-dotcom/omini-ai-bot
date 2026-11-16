"""Bot va agent o'rtasidagi kadr shakli — ikkala tomon shu fayldan foydalanadi.

Kadr matn ko'rinishida yuboriladi (`encode`/`decode`), shuning uchun yomon
kadr `ProtocolError` bilan darhol uziladi: xato keyinroq "nima bo'ldi?" deb
qidirishdan ko'ra shu yerda arzon.
"""

import json
import secrets

PROTOCOL_VERSION = 1
FRAME_TYPES = ("cmd", "res", "hello", "bound", "err", "ping", "pong")
# /get va /put faylni base64 bilan bitta kadrga joylaydi (8 MB fayl ≈ 11 MB kadr).
# aiohttpning standart 4 MB chegarasi ikkala tomonda ham ko'tarilishi shart.
MAX_FRAME_BYTES = 16 * 1024 * 1024


class ProtocolError(Exception):
    """Kadr shakli buzuq yoki bizning versiyaga tegishli emas."""


def cmd(cmd_id, op, **args) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "cmd", "id": str(cmd_id), "op": str(op), "args": args}


def result_ok(cmd_id, data) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "res", "id": str(cmd_id), "ok": True, "data": data}


def result_error(cmd_id, error) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "res", "id": str(cmd_id), "ok": False, "error": str(error)}


def hello(agent_id, name, bind=None, owner_user_id=None) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "hello", "agent_id": str(agent_id), "name": str(name),
            "bind": bind, "owner_user_id": owner_user_id}


def bound(bind, owner_user_id) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "bound", "bind": str(bind),
            "owner_user_id": int(owner_user_id)}


def channel_error(error) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "err", "error": str(error)}


def ping() -> dict:
    return {"v": PROTOCOL_VERSION, "type": "ping"}


def pong() -> dict:
    return {"v": PROTOCOL_VERSION, "type": "pong"}


def new_id() -> str:
    return secrets.token_hex(6)


def describe_error(error) -> str:
    return f"{type(error).__name__}: {error}"


def encode(frame) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def decode(text) -> dict:
    try:
        frame = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"JSON emas: {exc}")
    if not isinstance(frame, dict):
        raise ProtocolError("kadr obyekt emas")
    if frame.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"protokol versiyasi mos emas: {frame.get('v')!r}")
    kind = frame.get("type")
    if kind not in FRAME_TYPES:
        raise ProtocolError(f"noma'lum kadr turi: {kind!r}")
    if kind == "cmd":
        if not isinstance(frame.get("id"), str) or not isinstance(frame.get("op"), str):
            raise ProtocolError("cmd kadrida id va op satr bo'lishi kerak")
        if not isinstance(frame.get("args", {}), dict):
            raise ProtocolError("cmd.args obyekt bo'lishi kerak")
    if kind == "res":
        if not isinstance(frame.get("id"), str):
            raise ProtocolError("res kadrida id satr bo'lishi kerak")
        if not isinstance(frame.get("ok"), bool):
            raise ProtocolError("res kadrida ok boolean bo'lishi kerak")
    return frame
