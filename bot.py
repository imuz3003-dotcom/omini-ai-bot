"""omini AI — aqlli Telegram boti (aiogram 3 + Gemini).

Yadro yordamchilari botcore.py da: konfiguratsiya, SQLite, HTML formatlash, AI, TTS, rasm.
"""

import asyncio
import io
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

import botcore as core

bot = Bot(token=core.BOT_TOKEN)
dp = Dispatcher()
ADMIN_ID = core.ADMIN_ID
BOT_ID = 0
BOT_URL = "https://t.me/"

pending_action: dict = {}
last_image: dict = {}
pending_dm: dict = {}
panel_search: dict = {}

ACTIONS = {
    "summarize": (
        "📗 Xulosa",
        "Ushbu {typ}ni o'qib chiqing va bo'limlarga ajratib bering: 1) bir jumlali kirish, 2) 5 ta asosiy g'oya, "
        "3) muhim raqamlar/sanalar/ismlar, 4) yakuniy xulosa. O'zbek tilida.",
    ),
    "translate": (
        "🌍 Tarjima",
        "Ushbu {typ}dagi butun matnni to'liq va tabiiy qilib tarjima qiling. Til ko'rsatilmasa, o'zbek tiliga tarjima qiling.",
    ),
}


# ------------------------------------------------------------------ yordamchilar


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def on_off(value) -> str:
    return "yoqilgan" if value else "o'chirilgan"


def toggle_done(value) -> str:
    return "yoqildi ✅" if value else "o'chirildi ❌"


TOGGLE_ARGS = {"on": 1, "1": 1, "yok": 1, "yoqilsin": 1, "off": 0, "0": 0, "ochir": 0, "o'chir": 0, "uchir": 0}


def btn(text: str, data: str = None, url: str = None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url)


def command_link(command: str) -> str:
    return f"{BOT_URL}?text={urllib.parse.quote(command)}"


async def send_long(message, text: str, user_id: int, markup=None, sources=None):
    """AI javobini HTML ko'rinishida bo'lib yuboradi; manbalar alohida HTML blok qo'shiladi."""
    chunks = core.html_chunks(text)
    if sources and chunks:
        links = "\n".join(
            f"• <a href=\"{core.escape_html(url)}\">{core.escape_html(title or url)}</a>" for title, url in sources
        )
        block = f"\n\n🔗 <b>Manbalar:</b>\n{links}"
        if len(chunks[-1]) + len(block) <= 4000:
            chunks[-1] += block
        else:
            chunks.append(block.strip())

    # guruhda javobni foydalanuvchiga mahkamlab (reply) yuboramiz
    send = message.reply if message.chat.type != "private" else message.answer
    last = None
    for index, chunk in enumerate(chunks):
        keyboard = markup if index == len(chunks) - 1 else None
        try:
            sent = await send(chunk, reply_markup=keyboard, parse_mode="HTML")
        except TelegramAPIError:
            sent = await send(core.strip_markdown(chunk)[:4000], reply_markup=keyboard)
        core.store.track_msg(user_id, sent.message_id)
        last = sent
    return last


async def notify(message, text: str, markup=None):
    try:
        return await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        return await message.answer(core.strip_markdown(text), reply_markup=markup)


@asynccontextmanager
async def keep_typing(chat_id: int, action: str = ChatAction.TYPING):
    """AI ishlayotgan davomda 'yozmoqda...' indikatorini ushlab turadi."""

    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except TelegramAPIError:
                return
            await asyncio.sleep(5)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


def model_of(user: dict) -> str:
    return user.get("model") or core.MODEL_NAME


def instruction_for(user: dict) -> str:
    notes = core.store.notes(user["id"]) if user.get("id") else []
    text = core.persona_instruction(user.get("persona") or "general")
    if notes:
        text += "\n\nFoydalanuvchi haqidagi doimiy eslatmalar (ularni hisobga oling):\n- " + "\n- ".join(notes)
    return text


def ensure_session(user_id: int, fresh: bool = False) -> dict:
    """Foydalanuvchi Gemini sessiyasi; sozlamalar o'zgarsa yoki yo'q bo'lsa qayta yaratadi."""
    user = core.store.user(user_id)
    session = core.current_session(user_id)
    wanted = {
        "model": model_of(user),
        "persona": user.get("persona") or "general",
        "search": bool(user.get("search")),
    }
    if not fresh and session.get("chat") and all(session.get(k) == v for k, v in wanted.items()):
        return session
    chat, search_on = core.create_chat(
        user_id, instruction_for(user), wanted["model"], wanted["search"], replay=not fresh
    )
    session = core.remember_session(user_id, chat, search_on)
    session.update(wanted)
    return session


def drop_search(user_id: int) -> dict:
    """Model web-search'ni ko'tara olmasa, qidiruvni o'chirib sessiyani qayta ochadi."""
    user = core.store.user(user_id)
    chat, _ = core.create_chat(user_id, instruction_for(user), model_of(user), False, replay=True)
    session = core.remember_session(user_id, chat, False)
    session.update({"model": model_of(user), "persona": user.get("persona") or "general", "search": False})
    return session


async def ask_ai(message, prompt: str, user_id: int, markup=None):
    """Suhbat sessiyasi orqali javob oladi, xotiraga yozadi va yuboradi."""
    async with keep_typing(message.chat.id):
        session = ensure_session(user_id)
        core.store.add_message(user_id, "user", prompt)
        try:
            response = await asyncio.to_thread(session["chat"].send_message, prompt)
        except Exception as error:
            if not session.get("search"):
                raise
            logging.warning("Sessiyada web-search xatosi, o'chirilib qayta uriladi: %s", error)
            session = drop_search(user_id)
            response = await asyncio.to_thread(session["chat"].send_message, prompt)

        answer = core.extract_text(response) or "Kechirasiz, javob shakllanmadi."
        core.store.add_message(user_id, "model", answer)
        core.store.bump(user_id)
        sources = core.extract_sources(response) if session.get("search") else []
        sent = await send_long(message, answer, user_id, markup=markup, sources=sources)
    return sent


async def send_voice(message, text: str):
    """Javobni ovozli xabar sifatida yuboradi (uzun bo'lsa qisqartiriladi)."""
    wait = await message.reply(f"🔊 Ovoz tayyorlanmoqda ({core.MAX_VOICE_CHARS} belgigacha)...")
    try:
        data, _ = await asyncio.to_thread(core.text_to_speech_bytes, text)
        voice = await message.answer_voice(
            voice=BufferedInputFile(data, filename="omini_ai.ogg"),
            caption="🔊 Ovozli javob",
        )
        core.store.track_msg(message.from_user.id, voice.message_id)
    except Exception as error:
        logging.error("TTS xatosi: %s", error)
        await wait.edit_text(f"⚠️ Ovoz yuborib bo'lmadi: {str(error)[:120]}")
        return
    await wait.delete()


async def download_tg_file(file) -> bytes:
    stream = io.BytesIO()
    await bot.download(file, destination=stream)
    return stream.getvalue()


def take_pending(user_id: int):
    entry = pending_action.pop(user_id, None)
    if entry and entry[1] > asyncio.get_running_loop().time():
        return entry[0]
    return None


async def wipe_messages(chat_id: int, user_id: int, around_message_id: int = None):
    """Faqat bot/eslatilgan xabarlarni o'chiradi; eski versiyadan qolganlarini bir marta tozalaydi."""
    ids = core.store.tracked_msgs(user_id)
    for msg_id in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramAPIError:
            pass
    core.store.untrack_msgs(user_id)

    if around_message_id and core.store.prefs(user_id).get("legacy_swept") is None:
        for offset in range(1, 21):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=around_message_id - offset)
            except TelegramAPIError:
                pass
            await asyncio.sleep(0.02)
        core.store.set_pref(user_id, "legacy_swept", True)


async def preflight(message) -> bool:
    """Umumiy tekshiruv: guruh filtiri, ro'yxatdan o'tkazish, ban va spam limiti."""
    user = message.from_user
    if user is None:
        return False
    if message.chat.type != "private" and not group_allowed(message):
        return False
    core.store.touch_user(user.id, username=user.username, first_name=user.first_name)
    if not is_admin(user.id):
        if core.store.is_banned(user.id):
            await notify(message, "⛔ Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return False
        allowed, wait = core.limiter.check(user.id)
        if not allowed:
            await notify(message, f"⏳ Juda ko'p so'rov. {core.human_wait(wait)} soniyadan so'ng qayta urinib ko'ring.")
            return False
        if core.DAILY_LIMIT and core.store.today_count(user.id) >= core.DAILY_LIMIT:
            await notify(
                message,
                f"📉 Bugungi limit ({core.DAILY_LIMIT} so'rov) tugadi. Ertaga qayta urinib ko'ring yoki admin bilan bog'laning.",
            )
            return False
    return True


def group_allowed(message) -> bool:
    """Guruhda bot faqat unga yo'naltirilgan xabarlarga javob beradi."""
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == BOT_ID:
        return True
    return f"@{BOT_URL.split('/')[-1]}" in text or "@bot" in text.lower()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("💬 Yangi suhbat", data="btn_new"), btn("🎨 Rasm chizish", url=command_link("/image"))],
            [btn("🔎 Web-qidiruv", url=command_link("/search on")), btn("📗 Xulosa / Tarjima", url=command_link("/summarize"))],
            [btn("🧠 Rejim va model", data="btn_modes"), btn("⚙️ Sozlamalar", data="btn_settings")],
            [btn("ℹ️ Yordam", data="btn_help"), btn("🌐 Loyihalar", data="btn_sites"), btn("👨‍💻 Dasturchi", data="btn_creator")],
        ]
    )


def settings_keyboard(user: dict) -> InlineKeyboardMarkup:
    persona_label = core.PERSONAS.get(user.get("persona") or "general", core.PERSONAS["general"])[0]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(f"{persona_label}", data="btn_modes")],
            [btn(f"🧠 Model: {model_of(user).replace('gemini-', '')}", data="btn_models")],
            [
                btn("🔊 Ovoz: " + ("✅" if user.get("voice") else "❌"), data="tg_voice"),
                btn("🔎 Qidiruv: " + ("✅" if user.get("search") else "❌"), data="tg_search"),
            ],
            [btn("🧹 Suhbatni tozalash", data="btn_new"), btn("📝 Eslatmalarim", data="btn_notes")],
            [btn("⬅️ Asosiy menyu", data="btn_back_main")],
        ]
    )


def modes_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    keys = list(core.PERSONAS)
    for index in range(0, len(keys), 2):
        row = []
        for key in keys[index : index + 2]:
            mark = "✅ " if key == current else ""
            row.append(btn(mark + core.PERSONAS[key][0], data=f"mode_{key}"))
        rows.append(row)
    rows.append([btn("⬅️ Orqaga", data="btn_back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = [[btn(("✅ " if model == current else "") + model, data=f"model_{i}") for i, model in enumerate(core.MODEL_LIST)]]
    rows.append([btn("⬅️ Orqaga", data="btn_back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(user: dict) -> str:
    persona_label = core.PERSONAS.get(user.get("persona") or "general", core.PERSONAS["general"])[0]
    return (
        "⚙️ <b>Sozlamalaringiz</b>\n\n"
        f"👤 Rejim: <b>{core.escape_html(persona_label)}</b>\n"
        f"🧠 Model: <code>{core.escape_html(model_of(user))}</code>\n"
        f"🔊 Ovozli javob: <b>{on_off(user.get('voice'))}</b>\n"
        f"🔎 Web-qidiruv: <b>{on_off(user.get('search'))}</b>\n\n"
        "Tugmani bosing — o'zgarish darhol ishlaydi.\n"
        "💡 <code>/remember</code> bilan botga doimiy ma'lumot eslatib qo'yishingiz mumkin."
    )


HELP_TEXT = (
    "ℹ️ <b>omini AI imkoniyatlari</b>\n\n"
    "💬 <b>Suhbat + xotira</b> — savolingizni yozing; bot suhbatni eslaydi va restart'dan keyin ham tiklaydi.\n"
    "🔎 <b>Web-qidiruv</b> — <code>/search on</code>: yangiliklar, narxlar, oxirgi ma'lumotlar manbalar bilan.\n"
    "🎤 <b>Ovoz</b> — voice yuboring, matn + ovozli javob olasiz.\n"
    "🖼️ <b>Vision</b> — rasm yuboring, tahlil qilib beraman.\n"
    "📄 <b>Hujjat</b> — PDF, TXT, kod fayllarini o'qiyman.\n"
    "🎨 <b>Rasm chizish</b> — <code>/image mashina --ar 16:9</code> (variantlar: 1:1, 16:9, 9:16, 4:3, 3:2).\n"
    "📗 <b>Xulosa</b> — <code>/summarize</code> keyin fayl yoki matn yuboring.\n"
    "🌍 <b>Tarjima</b> — <code>/translate</code> bilan ishlaydi.\n"
    "🧠 <b>Rejimlar</b> — <code>/mode</code>: tarjimon, o'qituvchi, dasturchi, yozuvchi, matematikachi.\n"
    "📝 <b>Eslatmalar</b> — <code>/remember Ismim Muslimbek</code>, <code>/notes</code>, <code>/forget</code>.\n"
    "👥 <b>Odam qo'shish</b> — <code>/add</code> deb yozing, keyin kontakt yuboring (yoki <code>/add 123456789</code>).\n"
    "🛑 <b>Bekor qilish</b> — <code>/stop</code> joriy javobni to'xtatadi.\n\n"
    "Boshqa buyruqlar: <code>/new</code> · <code>/settings</code> · <code>/model</code> · <code>/voice</code> · <code>/sites</code>"
)


# ------------------------------------------------------------------------ start


@dp.message(CommandStart())
async def send_welcome(message: types.Message, command: CommandObject = None):
    user_id = message.from_user.id
    core.store.touch_user(user_id, username=message.from_user.username, first_name=message.from_user.first_name)
    core.reset_session(user_id)
    ensure_session(user_id, fresh=True)
    invited_by = ""
    args = (command.args or "").strip() if command else ""
    if args.startswith("ref") and args[3:].isdigit():
        referrer_id = int(args[3:])
        core.store.touch_user(referrer_id)
        core.store.set_added_by(user_id, referrer_id)
        invited_by = f"👥 Sizni <b>{core.escape_html(display_name(core.store.user(referrer_id)))}</b> taklif qildi.\n\n"
    name = core.escape_html(message.from_user.first_name or "foydalanuvchi")
    welcome = (
        f"Assalomu alaykum, <b>{name}</b> 👋\n\n"
        + invited_by
        + "Men <b>omini AI</b> — universal aqlli yordamchiman. Muslimbek tomonidan yaratilgan. 🚀\n\n"
        "<b>Nima qila olaman:</b>\n"
        "• 💬 Xotirali suhbat va 🔎 real vaqt web-qidiruv\n"
        "• 🖼️ Rasm tahlili, 📄 PDF/TXT hujjat o'qish\n"
        "• 🎤 Ovozli xabarni tushunib, 🔊 ovoz bilan javob berish\n"
        "• 🎨 AI rasm chizish: <code>/image kosmodagi kema --ar 16:9</code>\n\n"
        "Xohlagan savolingizni yozing yoki quyidagi menyudan foydalaning 👇"
    )
    await notify(message, welcome, markup=main_keyboard())


@dp.callback_query(F.data == "btn_help")
async def cb_help(callback: types.CallbackQuery):
    await notify(callback.message, HELP_TEXT)
    await callback.answer()


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not await preflight(message):
        return
    await notify(message, HELP_TEXT, markup=main_keyboard())


@dp.callback_query(F.data == "btn_back_main")
async def cb_back_main(callback: types.CallbackQuery):
    await notify(callback.message, "🏠 <b>Asosiy menyu</b>", markup=main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "btn_creator")
async def cb_creator(callback: types.CallbackQuery):
    await notify(
        callback.message,
        "👨‍💻 <b>Yaratuvchi</b>\n\nBu bot <b>Muslimbek</b> tomonidan Gemini AI, "
        "aiogram 3 va Flux rasm modeli asosida yaratilgan. 🚀\n\n"
        f"🌐 Loyihalar: <a href=\"https://github.com/imuz3003-dotcom\">github.com/imuz3003-dotcom</a>",
    )
    await callback.answer()


# ------------------------------------------------------------------- suhbat boshqaruvi


async def restart_chat(target_message, user_id: int, chat_id: int, note: str = "🔄 Yangi suhbat boshlandi!"):
    await wipe_messages(chat_id, user_id, around_message_id=target_message.message_id)
    core.store.drop_history(user_id)
    core.reset_session(user_id)
    ensure_session(user_id, fresh=True)
    sent = await notify(target_message, note, markup=main_keyboard())
    if sent:
        core.store.track_msg(user_id, sent.message_id)


@dp.message(Command("new"))
async def reset_chat(message: types.Message):
    core.store.touch_user(message.from_user.id, username=message.from_user.username, first_name=message.from_user.first_name)
    core.store.track_msg(message.from_user.id, message.message_id)
    await restart_chat(message, message.from_user.id, message.chat.id)


@dp.callback_query(F.data == "btn_new")
async def cb_new(callback: types.CallbackQuery):
    await restart_chat(callback.message, callback.from_user.id, callback.message.chat.id)
    await callback.answer("Suhbat tozalandi")


@dp.message(Command("stop"))
async def stop_generation(message: types.Message):
    task = core.inflight.pop(message.from_user.id, None)
    if task and not task.done():
        task.cancel()
        await notify(message, "🛑 Joriy javob bekor qilindi.")
    else:
        await notify(message, "ℹ️ Hozir ishlayotgan so'rov yo'q.")


# ------------------------------------------------------------------ sozlamalar


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    if not await preflight(message):
        return
    await notify(message, settings_text(core.store.user(message.from_user.id)), markup=settings_keyboard(core.store.user(message.from_user.id)))


@dp.callback_query(F.data == "btn_settings")
async def cb_settings(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user), parse_mode="HTML")
    except TelegramAPIError:
        await notify(callback.message, settings_text(user), markup=settings_keyboard(user))
    await callback.answer()


@dp.callback_query(F.data == "btn_back_settings")
async def cb_back_settings(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user), parse_mode="HTML")
    except TelegramAPIError:
        await notify(callback.message, settings_text(user), markup=settings_keyboard(user))
    await callback.answer()


@dp.callback_query(F.data == "tg_voice")
async def tg_voice(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    new_value = 0 if user.get("voice") else 1
    core.store.set_flag(callback.from_user.id, "voice", new_value)
    await refresh_settings(callback, f"🔊 Ovozli javob: {on_off(new_value)}")


@dp.callback_query(F.data == "tg_search")
async def tg_search(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    new_value = 0 if user.get("search") else 1
    core.store.set_flag(callback.from_user.id, "search", new_value)
    core.reset_session(callback.from_user.id)
    await refresh_settings(callback, f"🔎 Web-qidiruv: {on_off(new_value)}")


async def refresh_settings(callback: types.CallbackQuery, toast: str):
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user), parse_mode="HTML")
    except TelegramAPIError:
        await notify(callback.message, settings_text(user), markup=settings_keyboard(user))
    await callback.answer(toast[:180] if toast else None)


@dp.message(Command("mode"))
async def cmd_mode(message: types.Message):
    if not await preflight(message):
        return
    user = core.store.user(message.from_user.id)
    await notify(message, "🧠 <b>Javob rejimi</b> — o'zingizga mosini tanlang:", markup=modes_keyboard(user.get("persona") or "general"))


@dp.callback_query(F.data == "btn_modes")
async def cb_modes(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(
            "🧠 <b>Javob rejimi</b> — o'zingizga mosini tanlang:",
            reply_markup=modes_keyboard(user.get("persona") or "general"),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        await notify(callback.message, "🧠 <b>Javob rejimi</b>:", markup=modes_keyboard(user.get("persona") or "general"))
    await callback.answer()


@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(callback: types.CallbackQuery):
    key = callback.data.split("_", 1)[1]
    if key not in core.PERSONAS:
        await callback.answer("Noma'lum rejim")
        return
    core.store.set_flag(callback.from_user.id, "persona", key)
    core.reset_session(callback.from_user.id)
    ensure_session(callback.from_user.id, fresh=True)
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user), parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer(f"Rejim: {core.PERSONAS[key][0]}")


@dp.message(Command("model"))
async def cmd_model(message: types.Message):
    if not await preflight(message):
        return
    user = core.store.user(message.from_user.id)
    await notify(
        message,
        "🧠 <b>Model</b> — tanlang. Ro'yxatni o'zgartirish uchun <code>MODEL_LIST</code> env ishlating.\n"
        "Agar model kalitingizda bo'lmasa, xato chiqadi.",
        markup=models_keyboard(model_of(user)),
    )


@dp.callback_query(F.data == "btn_models")
async def cb_models(callback: types.CallbackQuery):
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text("🧠 <b>Model</b> — tanlang:", reply_markup=models_keyboard(model_of(user)), parse_mode="HTML")
    except TelegramAPIError:
        await notify(callback.message, "🧠 <b>Model</b>:", markup=models_keyboard(model_of(user)))
    await callback.answer()


@dp.callback_query(F.data.startswith("model_"))
async def cb_model(callback: types.CallbackQuery):
    index = callback.data.split("_", 1)[1]
    if not index.isdigit() or int(index) >= len(core.MODEL_LIST):
        await callback.answer("Noma'lum model")
        return
    model = core.MODEL_LIST[int(index)]
    core.store.set_flag(callback.from_user.id, "model", model)
    core.reset_session(callback.from_user.id)
    try:
        ensure_session(callback.from_user.id, fresh=True)
    except Exception as error:
        logging.error("Model tanlashda xato: %s", error)
        await callback.answer(f"❌ {model} ishlamaydi: {str(error)[:60]}", show_alert=True)
        return
    user = core.store.user(callback.from_user.id)
    try:
        await callback.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user), parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer(f"Model: {model}")


@dp.message(Command("search"))
async def cmd_search(message: types.Message, command: CommandObject):
    if not await preflight(message):
        return
    user = core.store.user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    value = TOGGLE_ARGS.get(arg)
    if value is None:
        await notify(
            message,
            f"🔎 Web-qidiruv hozir: <b>{on_off(user.get('search'))}</b>\n"
            "O'zgartirish: <code>/search on</code> yoki <code>/search off</code>",
        )
        return
    core.store.set_flag(message.from_user.id, "search", value)
    core.reset_session(message.from_user.id)
    await notify(message, f"🔎 Web-qidiruv {toggle_done(value)}")


@dp.message(Command("voice"))
async def cmd_voice(message: types.Message, command: CommandObject):
    if not await preflight(message):
        return
    user = core.store.user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    value = TOGGLE_ARGS.get(arg)
    if value is None:
        await notify(
            message,
            f"🔊 Ovozli javob hozir: <b>{on_off(user.get('voice'))}</b>\n"
            "O'zgartirish: <code>/voice on</code> yoki <code>/voice off</code>",
        )
        return
    core.store.set_flag(message.from_user.id, "voice", value)
    await notify(message, f"🔊 Ovozli javob {toggle_done(value)}")


# ---------------------------------------------------------------------- eslatmalar


@dp.message(Command("remember"))
async def cmd_remember(message: types.Message, command: CommandObject):
    if not await preflight(message):
        return
    if not command.args:
        await notify(message, "📝 Namuna: <code>/remember Mening ismim Muslimbek, talabaman</code>")
        return
    notes = core.store.add_note(message.from_user.id, command.args)
    core.reset_session(message.from_user.id)
    await notify(message, f"📌 Saqlandi ({len(notes)} ta eslatma). <code>/notes</code> bilan ko'ring.")


@dp.message(Command("notes"))
async def cmd_notes(message: types.Message):
    if not await preflight(message):
        return
    notes = core.store.notes(message.from_user.id)
    if not notes:
        await notify(message, "📝 Hozircha eslatmalar yo'q. Qo'shish: <code>/remember Matn</code>")
        return
    lines = "\n".join(f"{i}. {core.escape_html(n)}" for i, n in enumerate(notes, 1))
    await notify(message, f"📝 <b>Eslatmalaringiz:</b>\n{lines}\n\nTozalash: <code>/forget</code>")


@dp.message(Command("forget"))
async def cmd_forget(message: types.Message):
    if not await preflight(message):
        return
    core.store.set_pref(message.from_user.id, "notes", [])
    core.reset_session(message.from_user.id)
    await notify(message, "🗑️ Barcha eslatmalar o'chirildi.")


@dp.callback_query(F.data == "btn_notes")
async def cb_notes(callback: types.CallbackQuery):
    notes = core.store.notes(callback.from_user.id)
    text = (
        "📝 <b>Eslatmalaringiz:</b>\n" + "\n".join(f"{i}. {core.escape_html(n)}" for i, n in enumerate(notes, 1))
        if notes
        else "📝 Eslatmalar yo'q. <code>/remember Matn</code> bilan qo'shing."
    )
    await notify(callback.message, text)
    await callback.answer()


# ------------------------------------------------------------------- rasm generatsiyasi


@dp.message(Command("image"))
async def generate_ai_image(message: types.Message, command: CommandObject):
    if not await preflight(message):
        return
    prompt_text = (command.args or "").strip()
    if not prompt_text:
        await notify(
            message,
            "🎨 Rasm tavsifini yozing!\n\n"
            "Misollar:\n"
            "<code>/image kosmosda uchayotgan sport avtomobili</code>\n"
            "<code>/image tog'lar bag'ridagi uy --ar 16:9</code>\n"
            "<code>/image neon robot --model turbo --seed 7</code>\n\n"
            "Bayroqlar: <code>--ar 1:1|16:9|9:16|4:3|3:4|3:2|2:3</code>, <code>--model flux|turbo</code>, <code>--seed &lt;son&gt;</code>",
        )
        return
    core.inflight[message.from_user.id] = asyncio.current_task()
    prompt, options = core.parse_image_request(prompt_text)
    last_image[message.from_user.id] = {"prompt": prompt_text, "options": options}
    async with keep_typing(message.chat.id, ChatAction.UPLOAD_PHOTO):
        wait = await message.reply("🎨 Rasm chizilmoqda (20-60 soniya)...")
        try:
            await draw_image(message, wait, prompt, options)
        except Exception as error:
            logging.error("Rasm yaratishda xato: %s", error)
            await safe_edit(wait, f"⚠️ Rasm bo'lmadi: {str(error)[:150]}")
        finally:
            core.inflight.pop(message.from_user.id, None)


async def draw_image(message, wait, prompt, options):
    user = core.store.user(message.from_user.id)
    english = await asyncio.to_thread(core.translate_prompt_to_english, prompt, model_of(user))
    url = core.pollinations_url(english, options)
    data = await core.download_image(url)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[btn("🔁 Boshqa variant", data="btn_regen"), btn("⬅️ Menyu", data="btn_back_main")]]
    )
    caption = (
        f"✨ {core.escape_html(prompt)}\n"
        f"🇬🇧 {core.escape_html(english)}\n"
        f"📐 {options['aspect']} · {options['model']}"
        + (f" · seed {options['seed']}" if options.get("seed") else "")
        + "\n👤 Muslimbek"
    )
    if message.chat.type == "private":
        await message.answer_photo(photo=BufferedInputFile(data, filename="omini_ai.png"), caption=caption, reply_markup=keyboard, parse_mode="HTML")
        await wait.delete()
    else:
        sent = await message.reply_photo(
            photo=BufferedInputFile(data, filename="omini_ai.png"), caption=caption, reply_markup=keyboard, parse_mode="HTML"
        )
        core.store.track_msg(message.from_user.id, sent.message_id)
        await safe_edit(wait, "✅ Rasm tayyor!")
    core.store.bump(message.from_user.id, "img")


@dp.callback_query(F.data == "btn_regen")
async def cb_regen(callback: types.CallbackQuery):
    entry = last_image.get(callback.from_user.id)
    if not entry:
        await callback.answer("Avval /image buyrug'i bilan rasm so'rang", show_alert=True)
        return
    prompt, options = core.parse_image_request(entry["prompt"])
    options["seed"] = None
    await callback.answer("🎨 Yangi variant chizilmoqda...")
    core.inflight[callback.from_user.id] = asyncio.current_task()
    async with keep_typing(callback.message.chat.id, ChatAction.UPLOAD_PHOTO):
        wait = await callback.message.reply("🎨 Qayta chizilmoqda...")
        try:
            await draw_image(callback.message, wait, prompt, options)
        except Exception as error:
            logging.error("Rasm qayta yaratishda xato: %s", error)
            await safe_edit(wait, f"⚠️ {str(error)[:150]}")
        finally:
            core.inflight.pop(callback.from_user.id, None)


async def safe_edit(message_to_edit, text: str):
    try:
        await message_to_edit.edit_text(text)
    except TelegramAPIError:
        pass


# ------------------------------------------------------------ xulosa / tarjima


@dp.message(Command("summarize"))
async def cmd_summarize(message: types.Message, command: CommandObject):
    await run_action(message, "summarize", command)


@dp.message(Command("translate"))
async def cmd_translate(message: types.Message, command: CommandObject):
    await run_action(message, "translate", command)


async def run_action(message, kind: str, command: CommandObject):
    if not await preflight(message):
        return
    label, template = ACTIONS[kind]
    args = (command.args or "").strip()
    if args:
        prompt = f"{template.format(typ='matn')}\n\nMatn:\n{args[:12000]}"
        core.inflight[message.from_user.id] = asyncio.current_task()
        try:
            await ask_ai(message, prompt, message.from_user.id)
        except Exception as error:
            logging.error("%s amaliyotida xato: %s", kind, error)
            await notify(message, f"⚠️ Xatolik: {str(error)[:150]}")
        finally:
            core.inflight.pop(message.from_user.id, None)
        return
    pending_action[message.from_user.id] = (kind, asyncio.get_running_loop().time() + 600)
    await notify(
        message,
        f"{label} rejimi yoqildi (10 daqiqa).\nEndi <b>fayl, rasm yoki ovoz</b> yuboring — shu amalni bajaraman.",
    )


# ------------------------------------------------------------------------ hujjatlar


@dp.message(F.document)
async def handle_document(message: types.Message):
    if not await preflight(message):
        return
    uid = message.from_user.id
    core.store.track_msg(uid, message.message_id)
    doc = message.document
    file_name = doc.file_name or "hujjat"
    mime_type = doc.mime_type or "application/octet-stream"

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await notify(message, "⚠️ Fayl hajmi juda katta (maksimal 20 MB).")
        return

    kind = take_pending(uid)
    label, template = ACTIONS.get(kind, ("📄 Hujjat tahlili", "Faylni o'qiing va asosiy mazmunini xulosa qilib bering."))
    instruction = message.caption or template.format(typ=f"hujjat ({file_name})")

    async with keep_typing(message.chat.id):
        wait = await message.reply(f"{label}: <b>{core.escape_html(file_name)}</b> o'rganilmoqda, kuting...")
        core.inflight[uid] = asyncio.current_task()
        try:
            data = await download_tg_file(doc)
            user = core.store.user(uid)
            if mime_type.startswith("text/") or file_name.lower().endswith((".txt", ".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".csv")):
                try:
                    text_content = data.decode("utf-8")
                except UnicodeDecodeError:
                    text_content = data.decode("latin-1", errors="ignore")
                contents = f"Topshiriq: {instruction}\n\nFayl: {file_name}\n\n{text_content[:30000]}"
            else:
                api_mime = "application/pdf" if file_name.lower().endswith(".pdf") else mime_type
                contents = [
                    core.genai_types.Part.from_bytes(data=data, mime_type=api_mime),
                    f"Topshiriq: {instruction}\n\nFayl: {file_name}",
                ]
            answer, sources = await asyncio.to_thread(
                core.ask_once, model_of(user), contents, bool(user.get("search")), instruction_for(user)
            )
            answer = answer or "Hujjatdan ma'lumot topilmadi."
            core.store.add_message(uid, "user", f"[hujjat: {file_name}] {instruction}")
            core.store.add_message(uid, "model", answer)
            core.store.bump(uid, "doc")
            await wait.delete()
            await send_long(message, answer, uid, sources=sources)
        except Exception as error:
            logging.error("Hujjat tahlilida xatolik: %s", error)
            await safe_edit(wait, f"⚠️ Hujjatni o'qib bo'lmadi: {str(error)[:150]}")
        finally:
            core.inflight.pop(uid, None)


# --------------------------------------------------------------------------- ovoz


@dp.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    if not await preflight(message):
        return
    uid = message.from_user.id
    core.store.track_msg(uid, message.message_id)
    kind = take_pending(uid)
    _, template = ACTIONS.get(kind, ("🎤 Ovoz", "Foydalanuvchi ovozli xabar yubordi. Matnni tushuning va aniq, lo'nda javob bering."))
    instruction = message.caption or template.format(typ="ovozli xabar")

    async with keep_typing(message.chat.id, ChatAction.RECORD_VOICE):
        try:
            data = await download_tg_file(message.voice or message.audio)
        except Exception as error:
            logging.error("Ovoz yuklashda xato: %s", error)
            await notify(message, f"⚠️ Ovozni yuklab bo'lmadi: {str(error)[:120]}")
            return
        mime_type = "audio/ogg" if message.voice else (message.audio.mime_type or "audio/mpeg")
        user = core.store.user(uid)
        contents = [
            core.genai_types.Part.from_bytes(data=data, mime_type=mime_type),
            f"Topshiriq: {instruction}",
        ]
        core.inflight[uid] = asyncio.current_task()
        answer = None
        try:
            answer, sources = await asyncio.to_thread(
                core.ask_once, model_of(user), contents, bool(user.get("search")), instruction_for(user)
            )
            answer = answer or "Ovozli xabaringizni tushuna olmadim."
            core.store.add_message(uid, "user", f"[ovozli xabar] {instruction}")
            core.store.add_message(uid, "model", answer)
            core.store.bump(uid, "voice")
            await send_long(message, answer, uid, sources=sources)
        except Exception as error:
            logging.error("Ovozli xabarda xatolik: %s", error)
            await notify(message, f"⚠️ Ovozni tahlil qilib bo'lmadi: {str(error)[:150]}")
        finally:
            core.inflight.pop(uid, None)

    if answer and user.get("voice"):
        await send_voice(message, answer)


# --------------------------------------------------------------------------- rasm


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    if not await preflight(message):
        return
    uid = message.from_user.id
    core.store.track_msg(uid, message.message_id)
    kind = take_pending(uid)
    label, template = ACTIONS.get(kind, ("🖼️ Rasm tahlili", "Ushbu rasmda nima tasvirlangan? Batafsil va aniq tushuntiring."))
    instruction = message.caption or template.format(typ="rasm")

    async with keep_typing(message.chat.id):
        wait = await message.reply(f"{label} — kuting...")
        try:
            data = await download_tg_file(message.photo[-1])
        except Exception as error:
            logging.error("Rasm yuklashda xato: %s", error)
            await safe_edit(wait, f"⚠️ Rasmni yuklab bo'lmadi: {str(error)[:120]}")
            return
        user = core.store.user(uid)
        contents = [
            core.genai_types.Part.from_bytes(data=data, mime_type="image/jpeg"),
            instruction,
        ]
        core.inflight[uid] = asyncio.current_task()
        try:
            answer, sources = await asyncio.to_thread(
                core.ask_once, model_of(user), contents, bool(user.get("search")), instruction_for(user)
            )
            answer = answer or "Rasm bo'yicha ma'lumot topilmadi."
            core.store.add_message(uid, "user", f"[rasm] {instruction}")
            core.store.add_message(uid, "model", answer)
            core.store.bump(uid, "photo")
            await wait.delete()
            await send_long(message, answer, uid, sources=sources)
        except Exception as error:
            logging.error("Rasm tahlilida xatolik: %s", error)
            await safe_edit(wait, f"⚠️ Rasmni tahlil qilib bo'lmadi: {str(error)[:150]}")
        finally:
            core.inflight.pop(uid, None)


# ----------------------------------------------------- kontakt va foydalanuvchilar paneli


def ago(timestamp) -> str:
    if not timestamp:
        return "—"
    delta = max(0, time.time() - timestamp)
    if delta < 90:
        return "hozir"
    if delta < 3600:
        return f"{int(delta // 60)} daqiqa oldin"
    if delta < 86400:
        return f"{int(delta // 3600)} soat oldin"
    return f"{int(delta // 86400)} kun oldin"


def display_name(user: dict) -> str:
    return user.get("first_name") or (f"@{user['username']}" if user.get("username") else f"foydalanuvchi {user['id']}")


def invite_link(referrer_id: int) -> str:
    return f"{BOT_URL}?start=ref{referrer_id}"


async def deliver(target_id: int, text: str) -> bool:
    """Matnni (markdown→HTML) foydalanuvchining shaxsiy chatiga yuboradi."""
    chunks = core.html_chunks(text)
    try:
        for chunk in chunks:
            await bot.send_message(chat_id=target_id, text=chunk, parse_mode="HTML")
        return True
    except TelegramAPIError:
        pass
    try:
        for chunk in chunks:
            await bot.send_message(chat_id=target_id, text=core.strip_markdown(chunk)[:4000])
        return True
    except TelegramAPIError:
        return False


async def add_person(origin, target_id: int, referrer_id: int):
    """Kontakt orqali qo'shilgan odamni bazaga yozadi va unga xush kelibsiz yuboradi."""
    core.store.touch_user(target_id)
    core.store.set_added_by(target_id, referrer_id)
    referrer = core.store.user(referrer_id)
    ref_name = display_name(referrer)
    hello = (
        f"Salom! 👋 Sizni **{ref_name}** ushbu botga taklif qildi.\n\n"
        "Men **omini AI** — aqlli yordamchi. Savolingizni yozing, rasm/PDF/ovoz yuboring yoki "
        "`/image` bilan rasm chizdiring.\n\n**/start** deb yozib, menyuni oching 👉"
    )
    delivered = await deliver(target_id, hello)
    if delivered:
        await notify(
            origin,
            f"✅ <code>{target_id}</code> ro'yxatga qo'shildi va unga xush kelibsiz xabari yuborildi.",
        )
    else:
        await notify(
            origin,
            f"👤 <code>{target_id}</code> ro'yxatga qo'shildi, lekin u hali bot bilan suhbatlashmagan — "
            f"xabar yetkazib bo'lmadi.\nUnga shu havolani yuboring: <a href=\"{invite_link(referrer_id)}\">Botni ochish</a>",
        )


@dp.message(Command("add"))
async def cmd_add(message: types.Message, command: CommandObject):
    if not await preflight(message):
        return
    arg = (command.args or "").strip().lstrip("@")
    if arg.isdigit():
        await add_person(message, int(arg), message.from_user.id)
        return
    pending_action[message.from_user.id] = ("add_contact", asyncio.get_running_loop().time() + 600)
    await notify(
        message,
        "👤 Endi <b>kontakt</b> yuboring (xabar yozish maydonida 📎 → \"Kontakt\") — uni ro'yxatga qo'shaman.\n\n"
        "Yoki to'g'ridan-to'g'ri ID bilan: <code>/add 123456789</code>",
    )


@dp.message(F.contact)
async def handle_contact(message: types.Message):
    if not await preflight(message):
        return
    uid = message.from_user.id
    core.store.track_msg(uid, message.message_id)
    contact = message.contact
    name = " ".join(part for part in [contact.first_name, contact.last_name] if part) or "Noma'lum"
    lines = [
        "📇 <b>Yuborilgan kontakt</b>\n",
        f"👤 Ism: <b>{core.escape_html(name)}</b>",
        f"📞 Telefon: <code>{core.escape_html(contact.phone_number or '—')}</code>",
    ]
    if contact.user_id:
        lines.append(f"🆔 Telegram ID: <code>{contact.user_id}</code>")
        known = core.store.user(contact.user_id)
        if known.get("username"):
            lines.append(f"💬 Username: @{core.escape_html(known['username'])}")
    else:
        lines.append("🆔 Bu raqam Telegram'da ro'yxatdan o'tmagan (ID yo'q).")

    kind = take_pending(uid)
    if kind == "add_contact" and contact.user_id:
        await notify(message, "\n".join(lines))
        await add_person(message, contact.user_id, uid)
        return
    if kind == "add_contact":
        await notify(message, "\n".join(lines) + "\n\n⚠️ ID yo'qligi uchun qo'shib bo'lmadi.")
        return
    await notify(message, "\n".join(lines) + "\n\nQo'shish uchun avval <code>/add</code> deb yozing, keyin kontakt yuboring.")


def users_keyboard(rows: list, total: int, page: int) -> InlineKeyboardMarkup:
    rows_kb = [[btn(f"✉️ {display_name(row)[:28]}", data=f"dm:{row['id']}")] for row in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️ Oldingi", data=f"users:{page - 1}"))
    if (page + 1) * 8 < total:
        nav.append(btn("Keyingi ▶️", data=f"users:{page + 1}"))
    if nav:
        rows_kb.append(nav)
    rows_kb.append([btn("🔎 Qidiruv", url=command_link("/find ")), btn("⬅️ Menyu", data="btn_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows_kb)


def users_panel_text(rows: list, total: int, page: int, search: str) -> str:
    header = f"👥 <b>Foydalanuvchilar: {total} ta</b>" + (f" · qidiruv: <code>{core.escape_html(search)}</code>" if search else "")
    header += f" · sahifa {page + 1}\n\n"
    if not rows:
        return header + "Hech kim topilmadi."
    lines = []
    for index, row in enumerate(rows, start=page * 8 + 1):
        flags = []
        if row.get("banned"):
            flags.append("⛔")
        if row.get("added_by"):
            flags.append("👥 taklif")
        lines.append(
            f"{index}. 👤 <b>{core.escape_html(display_name(row))}</b>"
            + (f" @{core.escape_html(row['username'])}" if row.get("username") else "")
            + f" · <code>{row['id']}</code>\n"
            f"    🧠 {row.get('requests', 0)} so'rov · 🕒 {ago(row.get('last_seen'))}"
            + (f" · {' '.join(flags)}" if flags else "")
        )
    return header + "\n".join(lines) + "\n\n✉️ tugmasi — o'sha odamga xabar yozish."


async def show_users_panel(target, page: int = 0, search: str = None):
    total, rows = core.store.users_page(offset=page * 8, limit=8, search=search)
    uid = target.from_user.id
    if search is not None:
        panel_search[uid] = search
    text = users_panel_text(rows, total, page, panel_search.get(uid))
    keyboard = users_keyboard(rows, total, page)
    try:
        return await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramAPIError:
        return await target.answer(core.strip_markdown(text)[:4000], reply_markup=keyboard)


@dp.message(Command("users"))
@dp.message(Command("contacts"))
async def cmd_users(message: types.Message):
    if not is_admin(message.from_user.id):
        await notify(message, "🔒 Bu panel faqat admin uchun.")
        return
    await show_users_panel(message)


@dp.message(Command("find"))
async def cmd_find(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await notify(message, "🔒 Qidiruv faqat admin uchun.")
        return
    term = (command.args or "").strip()
    if not term:
        await notify(message, "🔎 Namuna: <code>/find Ali</code> yoki <code>/find 869201818</code>")
        return
    await show_users_panel(message, 0, term)


@dp.callback_query(F.data.startswith("users:"))
async def cb_users_page(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Faqat admin", show_alert=True)
        return
    page = callback.data.split(":", 1)[1]
    await show_users_panel(callback.message, int(page or 0))
    await callback.answer()


@dp.callback_query(F.data.startswith("dm:"))
async def cb_dm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Faqat admin", show_alert=True)
        return
    target = callback.data.split(":", 1)[1]
    if not target.lstrip("-").isdigit():
        await callback.answer("Noto'g'ri ID")
        return
    target_id = int(target)
    pending_dm[callback.from_user.id] = target_id
    core.store.touch_user(target_id)
    name = display_name(core.store.user(target_id))
    await callback.answer(f"✉️ {name}ga yozish: endi matn yuboring")
    await notify(
        callback.message,
        f"✉️ <b>{core.escape_html(name)}</b> (<code>{target_id}</code>) ga xabar yozish rejimi.\n"
        "Oddiy matn yuboring — jo'nataman. Bekor qilish: <code>/cancel</code>",
    )


@dp.message(Command("dm"))
async def cmd_dm_direct(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await notify(message, "🔒 Faqat admin.")
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await notify(message, "Namuna: <code>/dm 123456789 Salom, yangi funksiya chiqdi!</code>")
        return
    target_id, text = int(parts[0]), parts[1]
    ok = await deliver(target_id, text)
    await notify(message, f"✅ Yetkazildi (<code>{target_id}</code>)" if ok else f"❌ <code>{target_id}</code> ga yuborib bo'lmadi — botni boshlamagan.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    uid = message.from_user.id
    cleared = pending_dm.pop(uid, None) is not None
    pending_action.pop(uid, None)
    core.store.track_msg(uid, message.message_id)
    await notify(message, "🚫 Bekor qilindi. Oddiy suhbat rejimi." if cleared else "ℹ️ Bekor qilinadigan holat yo'q edi.")


# ---------------------------------------------------------------------- matn suhbati


@dp.message(F.sticker | F.video | F.video_note | F.animation | F.location)
async def handle_unsupported_media(message: types.Message):
    if not await preflight(message):
        return
    await notify(
        message,
        "🤖 Bu turdagi xabar hozircha qo'llab-quvvatlanmaydi.\n\n"
        "Yuborish mumkin: 🖼️ rasm, 📄 PDF/TXT/kod fayli, 🎤 ovozli xabar yoki oddiy 💬 matn.",
    )


@dp.message(F.text)
async def ai_chat(message: types.Message):
    admin_target = pending_dm.get(message.from_user.id)
    if admin_target and is_admin(message.from_user.id):
        pending_dm.pop(message.from_user.id, None)
        delivered = await deliver(admin_target, message.text)
        target = core.store.user(admin_target)
        status = "✅ Yetkazildi" if delivered else "❌ Yetkazib bo'lmadi (odam botni boshlamagan)"
        await notify(
            message,
            f"{status}: <b>{core.escape_html(display_name(target))}</b> (<code>{admin_target}</code>)\n\n"
            f"Yuborilgan xabar:\n<i>{core.escape_html(message.text[:300])}</i>",
        )
        return
    if message.text.startswith("/"):
        await notify(message, "🤔 Bunday buyruq yo'q. <code>/help</code> bilan ro'yxatni ko'ring.")
        return
    if not await preflight(message):
        return
    uid = message.from_user.id
    core.store.track_msg(uid, message.message_id)

    kind = take_pending(uid)
    prompt = message.text
    if kind:
        _, template = ACTIONS[kind]
        prompt = f"{template.format(typ='matn')}\n\nMatn:\n{message.text[:12000]}"

    if core.lock_for(uid).locked():
        await notify(message, "⏳ Oldingi so'rov hali ishlayapti. Kuting yoki <code>/stop</code> deb yuboring.")
        return

    async with core.lock_for(uid):
        core.inflight[uid] = asyncio.current_task()
        try:
            await ask_ai(message, prompt, uid)
        except asyncio.CancelledError:
            logging.info("Foydalanuvchi javobni bekor qildi: %s", uid)
            return
        except Exception as error:
            logging.error("AI javobida xatolik: %s", error)
            await notify(message, f"⚠️ Xatolik: {str(error)[:180]}")
        finally:
            core.inflight.pop(uid, None)


# ------------------------------------------------------------------------ loyihalar


@dp.message(Command("sites"))
async def cmd_sites(message: types.Message):
    if not await preflight(message):
        return
    await send_sites(message.answer)


@dp.callback_query(F.data == "btn_sites")
async def cb_sites(callback: types.CallbackQuery):
    await send_sites(callback.message.answer)
    await callback.answer()


async def send_sites(sender):
    text = (
        "🌐 <b>Muslimbekning loyihalari</b>\n\n"
        "• 🤖 <b>omini AI Bot</b> — aynan shu Telegram AI yordamchisi\n"
        "• 🏦 <b>Digital Bank</b> — zamonaviy landing page\n"
        "• 🎮 <b>Cyberdrift</b> — veb o'yin\n"
        "• 🏠 <b>3D House</b> — interaktiv 3D arxitektura\n"
        "• ⚡ <b>Chrome & Web Apps</b> — foydali veb-ilovalar\n\n"
        "Barchasi GitHub'da 👇"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🔗 GitHub profil", url="https://github.com/imuz3003-dotcom")],
            [btn("🏦 Digital Bank", url="https://github.com/imuz3003-dotcom/digitalbank-landing-page-master"), btn("🎮 Cyberdrift", url="https://github.com/imuz3003-dotcom")],
            [btn("⬅️ Menyu", data="btn_back_main")],
        ]
    )
    await sender(text, reply_markup=keyboard, parse_mode="HTML", link_preview_options=types.LinkPreviewOptions(is_disabled=True))


# ------------------------------------------------------------------------ admin


@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await notify(message, "🔒 Bu buyruq faqat admin uchun.")
        return
    data = core.store.stats()
    uptime = round((datetime.now(timezone.utc) - BOOT_TIME).total_seconds() / 3600, 1)
    top = "\n".join(f"{i}. <code>{uid}</code> — {count}" for i, (uid, count) in enumerate(data["top"], 1)) or "—"
    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Jami foydalanuvchi: <b>{data['users']}</b>\n"
        f"🟢 Bugungi faol: <b>{data['active_today']}</b>\n"
        f"🧠 Bugungi so'rovlar: <b>{data['requests_today']}</b>\n"
        f"💾 Saqlangan xabar: <b>{data['stored_messages']}</b>\n"
        f"⏱️ Up-time: <b>{uptime} soat</b>\n"
        f"⚙️ Model: <code>{core.escape_html(model_of(core.store.user(ADMIN_ID)))}</code>\n\n"
        f"🏆 Eng ko'p so'rov yuborganlar:\n{top}"
    )
    await notify(message, text)


@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await notify(message, "🔒 Bu buyruq faqat admin uchun.")
        return
    text = (command.args or message.text.partition(" ")[2] or "").strip()
    if not text:
        await notify(message, "⚠️ Matn kerak: <code>/broadcast Yangi funksiya qo'shildi!</code>")
        return
    targets = core.store.all_users()
    info = await message.reply(f"📢 {len(targets)} ta foydalanuvchiga yuborilmoqda...")
    sent, failed = 0, 0
    html = core.md_to_html(text)
    for uid in targets:
        try:
            try:
                await bot.send_message(chat_id=uid, text=html, parse_mode="HTML")
            except TelegramAPIError:
                await bot.send_message(chat_id=uid, text=core.strip_markdown(text))
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await safe_edit(info, f"✅ Yuborildi: <b>{sent}</b> · ❌ Yetmadi: <b>{failed}</b>")


@dp.message(Command("ban"))
async def admin_ban(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    uid = (command.args or "").strip()
    if not uid.lstrip("-").isdigit():
        await notify(message, "Namuna: <code>/ban 123456789</code>")
        return
    core.store.touch_user(int(uid))
    core.store.set_flag(int(uid), "banned", 1)
    core.reset_session(int(uid))
    await notify(message, f"⛔ <code>{uid}</code> bloklandi.")


@dp.message(Command("unban"))
async def admin_unban(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    uid = (command.args or "").strip()
    if not uid.lstrip("-").isdigit():
        await notify(message, "Namuna: <code>/unban 123456789</code>")
        return
    core.store.set_flag(int(uid), "banned", 0)
    await notify(message, f"✅ <code>{uid}</code> blokdan chiqarildi.")


@dp.message(Command("user"))
async def admin_user(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    uid = (command.args or "").strip()
    if not uid.lstrip("-").isdigit():
        await notify(message, "Namuna: <code>/user 123456789</code>")
        return
    uid = int(uid)
    user = core.store.user(uid)
    text = (
        f"👤 <code>{uid}</code>\n"
        f"Username: @{core.escape_html(user.get('username') or '—')}\n"
        f"Ism: {core.escape_html(user.get('first_name') or '—')}\n"
        f"Rejim: {user.get('persona')} · Model: {model_of(user)}\n"
        f"Ovoz: {user.get('voice')} · Qidiruv: {user.get('search')} · Ban: {user.get('banned')}\n"
        f"Keldi: {ago(user.get('created_at'))} · Oxirgi: {ago(user.get('last_seen'))}\n"
        + (f"Taklif qildi: <code>{user.get('added_by')}</code>\n" if user.get("added_by") else "")
        + f"Bugungi so'rov: {core.store.today_count(uid)} · Jami: {core.store.total_requests(uid)}\n"
        f"Suhbatda saqlangan xabar: {len(core.store.history(uid, 200))}"
    )
    await notify(message, text)


@dp.error()
async def on_error(event):
    logging.exception("Noma'lum xato: %s", event.exception)
    update = event.update
    if update is None:
        return True
    query = update.callback_query
    message = (query.message if query else None) or update.message or update.edited_message
    user = (query.from_user if query else None) or (message.from_user if message else None)
    if message and user and is_admin(user.id):
        try:
            await notify(message, f"⚠️ Ichki xato: {str(event.exception)[:150]}")
        except TelegramAPIError:
            pass
    return True


# --------------------------------------------------------------------- ishga tushirish


BOOT_TIME = datetime.now(timezone.utc)
USER_COMMANDS = [
    ("start", "Botni boshlash va menyuni ko'rsatish"),
    ("help", "Barcha imkoniyatlar va buyruqlar"),
    ("new", "Suhbatni va xabarlarni tozalash"),
    ("image", "AI rasm chizish: /image matn --ar 16:9"),
    ("summarize", "Fayl yoki matnning xulossasi"),
    ("translate", "Fayl, rasm yoki matnni tarjima qilish"),
    ("mode", "Javob rejimini tanlash"),
    ("model", "AI modelni tanlash"),
    ("search", "Web-qidiruvni yoqish/o'chirish"),
    ("voice", "Ovozli javobni yoqish/o'chirish"),
    ("settings", "Sozlamalar paneli"),
    ("remember", "Botga doimiy ma'lumot eslatish"),
    ("notes", "Eslatmalarimni ko'rish"),
    ("add", "Kontakt yoki ID bilan odam qo'shish"),
    ("cancel", "Joriy rejimni bekor qilish"),
    ("stop", "Joriy javobni bekor qilish"),
    ("sites", "Dasturchi loyihalari"),
]


async def setup_menus():
    global BOT_ID, BOT_URL
    me = await bot.get_me()
    BOT_ID = me.id
    BOT_URL = f"https://t.me/{me.username}" if me.username else "https://t.me"
    await bot.set_my_commands([types.BotCommand(command=c, description=d) for c, d in USER_COMMANDS])
    await bot.set_my_commands(
        [
            types.BotCommand(command="stats", description="Bot statistikasi"),
            types.BotCommand(command="broadcast", description="Barchaga xabar yuborish"),
            types.BotCommand(command="ban", description="Foydalanuvchini bloklash"),
            types.BotCommand(command="unban", description="Blokdan chiqarish"),
            types.BotCommand(command="user", description="Bitta foydalanuvchini ko'rish"),
            types.BotCommand(command="users", description="Foydalanuvchilar paneli (ism bilan)"),
            types.BotCommand(command="find", description="Ism/username/ID bo'yicha qidirish"),
            types.BotCommand(command="dm", description="Muayyan odamga xabar yuborish"),
        ],
        scope=types.BotCommandScopeChat(chat_id=ADMIN_ID),
    )


async def handle_ping(request):
    return web.Response(text="omini AI bot is running 24/7")


async def handle_health(request):
    data = core.store.stats()
    data.update(model=core.MODEL_NAME, uptime_s=round((datetime.now(timezone.utc) - BOOT_TIME).total_seconds()), status="ok")
    return web.json_response(data)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_health)
    port = int(os.environ.get("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f">> Web server {port} da ishga tushdi (/health mavjud).")


async def main():
    core.setup_logging()
    try:
        await setup_menus()
    except TelegramAPIError as error:
        logging.warning("Menyu buyruqlari o'rnatilmadi: %s", error)
    print(">> omini AI bot ishga tushdi!")
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
