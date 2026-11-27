"""Bot tomonidagi qurilmalar xabari: pairing, imzo tekshiruvi, buyruq yuborish.

Render Free har restart'da SQLite'ni tozalaydi, shuning uchun bu yerda hech
narsa doimiy saqlanmaydi. Egalik huquqi `bind` imzosida yashaydi — uni faqat
`DEVICE_SECRET` bilan qayta hisoblash mumkin, agent esa o'z nusxasini
`agent.json`da saqlaydi. Shuning uchun deploydan keyin hech kim qayta /pair
yozmaydi, lekin huquq ham bazadan o'g'irlanmaydi.

Test:  python -m unittest discover -s . -p "test_*.py"
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import time

from aiohttp import web

from companion import protocol

log = logging.getLogger("devicehub")

PAIR_TTL = 600.0              # kod 10 daqiqa yashaydi
WRONG_PAIR_WINDOW = 600.0     # xato urinishlar shu oynada sanaladi
MAX_WRONG_PAIRS = 5
MAX_DEVICES_PER_USER = 5
COMMAND_TIMEOUT = 10.0        # tez amallar
SLOW_TIMEOUT = 60.0           # fayl ko'chirish/yuborish
SLOW_OPS = frozenset({"cp", "mv", "get", "put", "write"})
MAX_NAME_LENGTH = 40
# `/device` ro'yxatida ko'rinadigan qisqa ID: `--pair`dan qolgan bir xil nomli
# qatorlarni shu ajratadi (`/unpair a3f9c1`).
SHORT_ID = 6
# `/put` oldindan tekshiriladi: agent kanali bitta kadrga sig'magan faylni butunlay
# yo'qotadi. ops.MAX_GET_BYTES (8 MB) bilan bir xil — ikkala yo'nalish simmetrik.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

AGENT_ID_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")


class DeviceError(Exception):
    """Matni allaqachon foydalanuvchiga ko'rsatishga tayyor xato."""


def bind_token(secret, agent_id, owner_user_id) -> str:
    message = f"{agent_id}:{int(owner_user_id)}".encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()


def binds_match(candidate, expected) -> bool:
    """`hmac.compare_digest` ASCII bo'lmagan satr bilan `TypeError` beradi;
    `bind` esa tashqi kadrdan keladi — shuning uchun baytlarni solishtiramiz."""
    return hmac.compare_digest(str(candidate).encode("utf-8"), str(expected).encode("utf-8"))


class Device:
    """Bitta qurilma: kimga tegishli, qaysi kanaldan eshitiladi, joriy katalogi."""

    def __init__(self, agent_id, name, root, owner_user_id, bind):
        self.agent_id = agent_id
        self.name = name
        self.root = root
        self.cwd = root
        self.owner_user_id = int(owner_user_id)
        self.bind = bind
        self.socket = None
        self.pump = None
        self.pending = {}

    @property
    def online(self) -> bool:
        return self.socket is not None

    def info(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "root": self.root,
            "cwd": self.cwd,
            "online": self.online,
        }


class DeviceHub:
    def __init__(self, secret, clock=None):
        self.secret = str(secret)
        self._clock = clock or time.monotonic
        self._devices = {}        # agent_id -> Device
        self._registrations = {}  # agent_id -> {code, name, root, expires}
        self._codes = {}          # code -> agent_id
        self._pending = {}        # agent_id -> (socket, pump task) — pairing kutilmoqda
        self._wrong = {}          # user_id -> (count, since)
        self._active = {}         # user_id -> agent_id
        self._revoked = set()     # /unpair qilingan — imzosi to'g'ri bo'lsa ham qaytarmaymiz

    # ------------------------------------------------------------------ pairing

    def register(self, agent_id, name, root) -> str:
        """Agent birinchi marta ulan-ganda kod so'raydi; qayta so'rasa — shu kod qaytadi."""
        existing = self._registrations.get(agent_id)
        if existing is not None and existing["expires"] > self._clock():
            existing.update(name=name, root=root)
            return existing["code"]
        code = self._fresh_code()
        self._registrations[agent_id] = {
            "code": code, "name": name, "root": root, "expires": self._clock() + PAIR_TTL,
        }
        self._codes[code] = agent_id
        return code

    def _fresh_code(self) -> str:
        while True:
            code = f"{secrets.randbelow(1000000):06d}"
            if code not in self._codes:
                return code

    def pair(self, user_id, code) -> dict:
        owner = int(user_id)
        self._assert_not_locked(owner)
        text = str(code).strip()
        agent_id = self._codes.get(text) if len(text) == 6 and text.isdigit() else None
        registration = self._registrations.get(agent_id) if agent_id else None
        if registration is None or registration["expires"] <= self._clock():
            self._count_wrong(owner)
            raise DeviceError("Kod noto'g'ri yoki muddati o'tgan. "
                              "Qurilmadagi agent kodni qayta ko'rsatadi.")
        del self._codes[text]
        del self._registrations[agent_id]
        self._wrong.pop(owner, None)

        device = Device(agent_id, registration["name"], registration["root"], owner,
                        bind_token(self.secret, agent_id, owner))
        self._devices[agent_id] = device
        self._active[owner] = agent_id

        waiting = self._pending.pop(agent_id, None)
        if waiting is not None:
            socket, pump = waiting
            device.socket = socket
            device.pump = pump
            self._adopt_delivery(device, socket)
        log.info("Pairing: %s -> user %s", device.name, owner)
        return dict(device.info(), bind=device.bind, owner_user_id=owner)

    def _adopt_delivery(self, device, socket) -> None:
        """Kutilayotkan kanalga imzoni yuborish — agent uni `agent.json`ga yozadi."""
        frame = protocol.encode(protocol.bound(device.bind, device.owner_user_id))

        async def deliver():
            try:
                await socket.send_str(frame)
            except Exception as error:
                log.warning("%s: `bound` kadri yetmadi (%s)", device.name, error)

        with contextlib.suppress(RuntimeError):
            asyncio.ensure_future(deliver())

    def _assert_not_locked(self, owner) -> None:
        entry = self._wrong.get(owner)
        if entry is None:
            return
        count, since = entry
        if self._clock() - since >= WRONG_PAIR_WINDOW:
            del self._wrong[owner]
            return
        if count >= MAX_WRONG_PAIRS:
            raise DeviceError(f"Juda ko'p xato kod ({MAX_WRONG_PAIRS} marta). "
                              f"{int(WRONG_PAIR_WINDOW / 60)} daqiqadan so'ng qayta urinib ko'ring.")

    def _count_wrong(self, owner) -> None:
        count, since = self._wrong.get(owner, (0, self._clock()))
        self._wrong[owner] = (count + 1, since)

    # ------------------------------------------------------------------ kanallar

    async def attach(self, agent_id, socket, bind=None, owner_user_id=None, name=None, root=None):
        """Kanalni ro'yxatga olish. `bind` yo'q bo'lsa — pairing kutilayotgan kanal."""
        agent_id = str(agent_id)
        if not AGENT_ID_RE.match(agent_id):
            await socket.close(code=4400)
            raise DeviceError("agent_id formati noto'g'ri.")
        if bind is None:
            return await self._watch_for_pairing(agent_id, socket)

        device = self._devices.get(agent_id)
        fresh = device is None
        if fresh:
            device = self._rebuild_from_bind(agent_id, str(bind), owner_user_id, name, root)
            if device is None:
                await socket.close(code=4401)
                raise DeviceError("Bu qurilma ro'yxatda yo'q — Telegramda /pair kodini kiriting.")
        if not binds_match(bind, device.bind):
            log.warning("Soxta imzo: agent=%s da'vo qilingan user=%s", agent_id[:8], owner_user_id)
            await socket.close(code=4401)
            raise DeviceError("⛔ Imzo tekshiruvidan o'tmadi.")

        waiting = self._pending.pop(agent_id, None)
        if waiting is not None and waiting[0] is not socket:
            await self._close(waiting[0], code=4409)
        try:
            await self._go_online(device, socket)
        except DeviceError:
            if fresh:
                self._devices.pop(agent_id, None)
            raise
        return device.pump

    def _rebuild_from_bind(self, agent_id, bind, owner_user_id, name, root):
        """Ro'yxat restart'da o'chadi, `bind` esa HMAC — imzoni qayta hisoblab
        tekshiramiz, shunda qurilma qayta `/pair` yozmasdan qayta keladi."""
        if agent_id in self._revoked:
            return None
        try:
            owner = int(owner_user_id)
        except (TypeError, ValueError):
            return None
        if not binds_match(bind, bind_token(self.secret, agent_id, owner)):
            return None
        device = Device(agent_id, str(name or "qurilma"), str(root or ""), owner, bind)
        self._devices[agent_id] = device
        log.info("Imzo bo'yicha qayta qabul: %s (user %s)", device.name, owner)
        return device

    async def _watch_for_pairing(self, agent_id, socket):
        stale = self._pending.pop(agent_id, None)
        if stale is not None and stale[0] is not socket:
            await self._close(stale[0], code=4409)
        task = asyncio.ensure_future(self._pump(agent_id, socket))
        self._pending[agent_id] = (socket, task)
        asyncio.ensure_future(self._expire_pending(agent_id, socket, PAIR_TTL))
        log.info("Kutilayotkan kanal: agent %s", agent_id[:8])
        return task

    async def _expire_pending(self, agent_id, socket, seconds) -> None:
        """Hech kim /pair yozmasa kanal abadiy ochiq qolmasin (agent qayta ulanadi)."""
        await asyncio.sleep(seconds)
        waiting = self._pending.get(agent_id)
        if waiting is not None and waiting[0] is socket:
            log.info("Pairing kutilmadi, kanal yopiladi: agent %s", agent_id[:8])
            await self._close(socket, code=4408)

    async def _go_online(self, device, socket) -> None:
        if not device.online:
            others = [d for d in self._devices.values()
                      if d.online and d.owner_user_id == device.owner_user_id]
            if len(others) >= MAX_DEVICES_PER_USER:
                await self._close(socket, code=4403)
                raise DeviceError(f"Bitta foydalanuvchiga {MAX_DEVICES_PER_USER} qurilmagacha ruxsat etilgan.")
        stale = device.socket
        device.socket = socket
        device.pump = asyncio.ensure_future(self._pump(device.agent_id, socket))
        self._active[device.owner_user_id] = device.agent_id
        log.info("Onlayn: %s (user %s)", device.name, device.owner_user_id)
        if stale is not None and stale is not socket:
            await self._close(stale, code=4409)

    async def detach(self, agent_id, socket) -> None:
        """Kanal uzildi: buyruq kutilayotgan bo'lsa, foydalanuvchi osilib qolmaydi."""
        device = self._devices.get(str(agent_id))
        if device is None or device.socket is not socket:
            return
        device.socket = None
        self._fail_pending(device, f"📴 \"{device.name}\" kanali uzildi.")

    async def _close(self, socket, code) -> None:
        with contextlib.suppress(Exception):
            await socket.close(code=code)

    async def _pump(self, agent_id, socket) -> None:
        """Bitta kanal: kelgan kadrlarni o'qiydi. Uzanishlar `res` orqali javobga aylanadi."""
        try:
            async for message in socket:
                try:
                    frame = protocol.decode(message.data)
                except protocol.ProtocolError as error:
                    await socket.send_str(protocol.encode(protocol.channel_error(str(error))))
                    continue
                if frame["type"] == "res":
                    self._resolve(agent_id, socket, frame)
                elif frame["type"] == "ping":
                    await socket.send_str(protocol.encode(protocol.pong()))
        except Exception as error:
            log.warning("Kanal %s uzildi: %s", agent_id[:8], error)
        finally:
            await self._forget(agent_id, socket)

    def _resolve(self, agent_id, socket, frame) -> None:
        device = self._devices.get(agent_id)
        if device is None or device.socket is not socket:
            return
        future = device.pending.pop(frame["id"], None)
        if future is None or future.done():
            return
        if frame["ok"]:
            future.set_result(frame.get("data"))
        else:
            future.set_exception(DeviceError(str(frame.get("error") or "agent xatosi")))

    async def _forget(self, agent_id, socket) -> None:
        waiting = self._pending.get(agent_id)
        if waiting is not None and waiting[0] is socket:
            self._pending.pop(agent_id, None)
        await self.detach(agent_id, socket)

    def _fail_pending(self, device, text) -> None:
        for future in device.pending.values():
            if not future.done():
                future.set_exception(DeviceError(text))
        device.pending.clear()

    # ------------------------------------------------------------------ buyruqlar

    def pick(self, user_id) -> Device:
        owner = int(user_id)
        owned = [d for d in self._devices.values() if d.owner_user_id == owner]
        if not owned:
            raise DeviceError("Sizga bog'langan qurilma yo'q. Qurilmada agentni ishga tushiring, "
                              "keyin /pair kodini yuboring.")
        active = self._devices.get(self._active.get(owner, ""))
        if active is None or active.owner_user_id != owner:
            active = owned[0] if len(owned) == 1 else next(
                (d for d in owned if d.online), owned[-1])
            self._active[owner] = active.agent_id
        return active

    def _match(self, user_id, ref) -> Device:
        owner = int(user_id)
        owned = [d for d in self._devices.values() if d.owner_user_id == owner]
        needle = str(ref).strip().lower()
        matches = [d for d in owned if d.name.lower() == needle
                   or d.agent_id == needle or d.agent_id.startswith(needle)]
        if not matches:
            names = ", ".join(f"\"{d.name}\"" for d in owned) if owned else "yo'q"
            raise DeviceError(f"Bunday qurilma topilmadi. Sizdagi qurilmalar: {names}.")
        if len(matches) > 1:
            ids = ", ".join(d.agent_id[:SHORT_ID] for d in matches)
            raise DeviceError(f"\"{matches[0].name}\" nomida {len(matches)} qurilma bor — "
                              f"qaysi biri kerak?\nNom o'rniga ID bilan yozing: {ids}. "
                              "Ro'yxat: /device")
        return matches[0]

    def select(self, user_id, ref) -> dict:
        device = self._match(user_id, ref)
        self._active[device.owner_user_id] = device.agent_id
        return device.info()

    def devices_of(self, user_id) -> list:
        owner = int(user_id)
        active = self._active.get(owner, "")
        return [dict(d.info(), active=(d.agent_id == active))
                for d in self._devices.values() if d.owner_user_id == owner]

    def cwd_of(self, user_id) -> str:
        return self.pick(user_id).cwd

    async def run(self, user_id, op, timeout=None, **args):
        device = self.pick(user_id)
        if not device.online:
            raise DeviceError(f"📴 \"{device.name}\" hozir onlayn emas. Agent ishlayaptimi — tekshiring.")
        if device.pending:
            raise DeviceError(f"⏳ \"{device.name}\" band — oldingi buyruq tugaguncha kuting.")
        args.setdefault("cwd", device.cwd)
        cmd_id = protocol.new_id()
        future = asyncio.get_running_loop().create_future()
        device.pending[cmd_id] = future
        limit = float(timeout) if timeout is not None else (
            SLOW_TIMEOUT if op in SLOW_OPS else COMMAND_TIMEOUT)
        try:
            await device.socket.send_str(protocol.encode(protocol.cmd(cmd_id, op, **args)))
        except Exception as error:
            device.pending.pop(cmd_id, None)
            raise DeviceError(f"📴 \"{device.name}\" ga buyruq yetmadi: {error}") from error
        try:
            return await asyncio.wait_for(future, limit)
        except asyncio.TimeoutError:
            device.pending.pop(cmd_id, None)
            raise DeviceError(f"⏱ \"{device.name}\" {limit:g} soniyada javob bermadi. "
                              "Katta katalog yoki sekin ulanish bo'lishi mumkin.") from None

    async def change_dir(self, user_id, path) -> str:
        """Agent yo'lni ma'qullaganndagina joriy katalogni almashtiramiz."""
        device = self.pick(user_id)
        result = await self.run(user_id, "stat", path=path)
        if isinstance(result, dict) and result.get("path"):
            device.cwd = str(result["path"])
        return device.cwd

    def unpair(self, user_id, ref) -> dict:
        device = self._match(user_id, ref)
        owner = device.owner_user_id
        self._devices.pop(device.agent_id, None)
        self._revoked.add(device.agent_id)
        if self._active.get(owner) == device.agent_id:
            self._active.pop(owner, None)
        self._fail_pending(device, f"\"{device.name}\" ro'yxatdan olib tashlandi.")
        socket = device.socket
        device.socket = None
        if socket is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.ensure_future(self._close(socket, code=4403))
        waiting = self._pending.pop(device.agent_id, None)
        if waiting is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.ensure_future(self._close(waiting[0], code=4403))
        log.info("Unpair: %s (user %s)", device.name, owner)
        return device.info()


# --------------------------------------------------------------- aiohttp marshrutlari

async def handle_register(request):
    """Agent shu yerdan 6 xonali pairing kodini oladi; imzo bu yerda uzilmaydi."""
    device_hub = request.app["hub"]
    agent_id = str(request.query.get("agent_id", "")).strip()
    root = str(request.query.get("root", "")).strip()
    name = (str(request.query.get("name", "")).strip()[:MAX_NAME_LENGTH] or "qurilma")
    if not AGENT_ID_RE.match(agent_id) or not root:
        return web.json_response({"error": "agent_id (hex) va root kerak"}, status=400)
    code = device_hub.register(agent_id, name, root)
    return web.json_response({"code": code, "agent_id": agent_id, "expires_in": int(PAIR_TTL)})


async def handle_ws(request):
    """Qurilma keladigan kanal. Birinchi kadr — `hello`, keyin `DeviceHub._pump` egallaydi."""
    device_hub = request.app["hub"]
    socket = web.WebSocketResponse(heartbeat=60, max_msg_size=protocol.MAX_FRAME_BYTES)
    await socket.prepare(request)
    agent_id = "?"
    try:
        frame = protocol.decode(await socket.receive_str())
        if frame["type"] != "hello":
            raise DeviceError("Birinchi kadr `hello` bo'lishi kerak.")
        agent_id = frame["agent_id"]
        task = await device_hub.attach(agent_id, socket, bind=frame.get("bind"),
                                       owner_user_id=frame.get("owner_user_id"),
                                       name=frame.get("name"), root=frame.get("root"))
    except (protocol.ProtocolError, DeviceError) as error:
        log.warning("Qurilma ulanishi rad etildi (%s): %s", agent_id, error)
        with contextlib.suppress(Exception):
            await socket.send_str(protocol.encode(protocol.channel_error(str(error))))
            await socket.close(code=4401)
        return socket
    if task is not None:
        await task  # handler tugasa aiohttp kanalni yopadi — shu sababli shu yerda turamiz
    return socket
