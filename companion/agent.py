"""Qurilma (Windows / Android-Termux) tomonidagi yordamchi dastur.

Bot Render'da, qurilma esa NAT ostida — shuning uchun ulanishni doim shu
dastur boshlaydi. Foydali ishning katta qismi (`handle_frame`, `pump`,
config) tarmoqdan mustaqil va sinovdan o'tgan; `serve`/`session`/`main` esa
faqat ulanishni ushlab turadi.

Ishga tushirish:  python companion/agent.py --url https://bot.example --root C:/Users/me
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

try:
    from . import ops, protocol
except ImportError:  # `python companion/agent.py`da paket konteksti bo'lmaydi
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ops
    import protocol

WS_PATH = "/device/ws"
REGISTER_PATH = "/device/register"
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
STABLE_SESSION_SECONDS = 60.0
# Bot bu kod bilan yopadi: «bindinging endi yaroqsiz, qayta /pair yoz».
REVOKED_CLOSE_CODES = frozenset({4401, 4403})


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "omini-agent"
    else:
        base = Path.home() / ".omini"
    return base / "agent.json"


def new_config(bot_url, roots, name) -> dict:
    return {
        "bot_url": str(bot_url).rstrip("/"),
        "agent_id": secrets.token_hex(16),
        "name": str(name),
        "owner_user_id": None,
        "bind": None,
        "roots": [str(Path(root)) for root in roots],
        "deny": list(ops.DEFAULT_DENY),
    }


def load_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(text)


def save_config(path, config) -> None:
    """Vaqtinchalik faylga yozib almashtiramiz: yarim yozilgan config = yo'qolgan pairing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def ws_url(bot_url) -> str:
    text = str(bot_url).rstrip("/")
    if text.startswith("https://"):
        text = "wss://" + text[len("https://"):]
    elif text.startswith("http://"):
        text = "ws://" + text[len("http://"):]
    return text + WS_PATH


def backoff(attempt, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP) -> float:
    return min(cap, base * (2 ** max(0, int(attempt))))


class AgentState:
    def __init__(self, config: dict, path):
        self.config = config
        self.path = Path(path)

    @property
    def bound(self) -> bool:
        return bool(self.config.get("bind"))

    def sandbox(self, cwd=None) -> ops.Sandbox:
        roots = [Path(root) for root in self.config["roots"]]
        deny = tuple(self.config.get("deny") or ops.DEFAULT_DENY)
        return ops.Sandbox(roots=roots, deny=deny, cwd=Path(cwd) if cwd else None)


def reset_binding(state: AgentState) -> None:
    """Bot bindingni bekor qildi (`/unpair` yoki imzo yaroqsiz) — jimgina
    qayta-urilaverish o'rniga yangi `/pair` kodini so'raymiz."""
    state.config["bind"] = None
    state.config["owner_user_id"] = None
    save_config(state.path, state.config)


def on_closed(state: AgentState, socket) -> None:
    """Kanal yopilish sababidan kelib chiqadigan siyosat (tarmoqqa bog'liq emas)."""
    if getattr(socket, "close_code", None) in REVOKED_CLOSE_CODES:
        reset_binding(state)


def _op_get(sandbox, **args) -> dict:
    result = ops.get(sandbox, **args)
    return {
        "path": result["path"],
        "size": result["size"],
        "encoding": "base64",
        "b64": base64.b64encode(result["data"]).decode("ascii"),
    }


def _op_put(sandbox, path, b64, **args) -> dict:
    return ops.put(sandbox, path, base64.b64decode(b64, validate=True))


DISPATCH = {
    "ls": ops.ls,
    "cat": ops.cat,
    "find": ops.find,
    "tree": ops.tree,
    "disk": ops.disk,
    "stat": ops.stat,
    "mkdir": ops.mkdir,
    "write": ops.write,
    "mv": ops.mv,
    "cp": ops.cp,
    "rm": ops.rm,
    "get": _op_get,
    "put": _op_put,
}


def handle_frame(state: AgentState, frame: dict):
    """Bitta kadarni bajarib, javob kadrrini (yoki None) qaytaradi."""
    kind = frame["type"]
    if kind == "ping":
        return protocol.pong()
    if kind == "bound":
        state.config["bind"] = frame["bind"]
        state.config["owner_user_id"] = frame["owner_user_id"]
        save_config(state.path, state.config)
        return None
    if kind != "cmd":
        return None

    cmd_id = frame["id"]
    if not state.bound:
        return protocol.result_error(cmd_id, "Denied: agent hali ulanmagan (binding yo'q)")
    args = dict(frame.get("args") or {})
    cwd = args.pop("cwd", None)
    function = DISPATCH.get(frame["op"])
    if function is None:
        return protocol.result_error(cmd_id, f"noma'lum amal: {frame['op']}")
    try:
        data = function(state.sandbox(cwd), **args)
    except Exception as error:
        # Har qanday xato matn bo'lib qaytadi: foydalanuvchi sababni ko'radi.
        return protocol.result_error(cmd_id, protocol.describe_error(error))
    return protocol.result_ok(cmd_id, data)


async def pump(socket, state: AgentState) -> None:
    async for message in socket:
        try:
            frame = protocol.decode(message.data)
        except protocol.ProtocolError as error:
            await socket.send_str(protocol.encode(protocol.channel_error(protocol.describe_error(error))))
            continue
        reply = handle_frame(state, frame)
        if reply is not None:
            await socket.send_str(protocol.encode(reply))
        elif frame["type"] == "err":
            # Bot kanalni yopishdan oldin sababni yuboradi: uni yashirmaymiz.
            print(f"!! Bot: {frame['error']}")


# --- Ulanish qatlami (tarmoq): sinovda emas, jonli bot bilan tekshiriladi. ---


async def request_code(state: AgentState) -> str:
    import aiohttp

    url = state.config["bot_url"] + REGISTER_PATH
    params = {
        "agent_id": state.config["agent_id"],
        "name": state.config["name"],
        "root": state.config["roots"][0],
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            payload = await response.json(content_type=None)
            if response.status != 200 or "code" not in payload:
                raise RuntimeError(f"bot javob bermadi ({response.status}): {payload}")
            return payload["code"]


async def session(state: AgentState) -> None:
    import aiohttp

    url = ws_url(state.config["bot_url"]) + "?agent_id=" + state.config["agent_id"]
    async with aiohttp.ClientSession() as client:
        async with client.ws_connect(url, heartbeat=30,
                                     max_msg_size=protocol.MAX_FRAME_BYTES) as socket:
            await socket.send_str(protocol.encode(protocol.hello(
                state.config["agent_id"], state.config["name"],
                bind=state.config.get("bind"), owner_user_id=state.config.get("owner_user_id"))))
            if not state.bound:
                code = await request_code(state)
                print(f">> Telegram'da /pair {code} yozing (10 daqiqa ichida).")
            print(f">> {state.config['name']} botga ulandi.")
            await pump(socket, state)
            on_closed(state, socket)


async def serve(state: AgentState, connect=session, sleep=asyncio.sleep,
                now=time.monotonic) -> None:
    """Qayta-ulinish tartibi: uzilishlar 1s→30s'ga qadar ko'payib boradi.

    Javobsiz qolish xavfi shundaki, bot ro'yxatdagi agent kanalini birinchi
    sekuntda yopadi — oraliq bo'lmasa bitta Render Free instansiyani zarba
    qilamiz. Shuning uchun muvaffaqiyatli (uzun) sessiyadan keyingina hisob
    nolga qaytadi.
    """
    attempt = 0
    while True:
        started = now()
        try:
            await connect(state)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning("Ulanish uzildi: %s", error)
        if now() - started >= STABLE_SESSION_SECONDS:
            attempt = 0
            delay = backoff(0)
        else:
            delay = backoff(attempt)
            attempt += 1
        await sleep(delay)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="omini AI qurilma agenti")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--url", help="botning https manzili")
    parser.add_argument("--root", action="append", help="ruxsat etilgan katalog (takrorlanadi)")
    parser.add_argument("--name", default=os.environ.get("COMPUTERNAME") or "qurilma")
    parser.add_argument("--pair", action="store_true", help="bindingni o'chirib, qayta /pair so'rash")
    arguments = parser.parse_args(argv)

    path = Path(arguments.config)
    config = load_config(path)
    if config is None or arguments.pair:
        if not arguments.url or not arguments.root:
            parser.error("birinchi ishga tushirishda --url va --root kerak")
        config = new_config(arguments.url, arguments.root, arguments.name)
    else:
        if arguments.url:
            config["bot_url"] = arguments.url.rstrip("/")
        if arguments.root:
            config["roots"] = [str(Path(root)) for root in arguments.root]
    save_config(path, config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f">> Config: {path} | ildizlar: {', '.join(config['roots'])}")
    try:
        asyncio.run(serve(AgentState(config, path)))
    except KeyboardInterrupt:
        print("\n>> To'xtatildi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
