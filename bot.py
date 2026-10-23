import os
import io
import json
import asyncio
import logging
import urllib.parse
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatAction
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiohttp import web
from gtts import gTTS
from google import genai
from google.genai import types as genai_types

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = 8692018182  # Muslimbek

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN yoki GEMINI_API_KEY topilmadi!")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MODEL_NAME = "gemini-3.5-flash-lite"

SYSTEM_INSTRUCTION = (
    "Siz aqlli, xushmuomala, aniq va ixcham yordam beradigan 'omini AI' nomli universal sun'iy intellektsiz. "
    "Siz matnlarga javob berasiz, rasmlarni tahlil qilasiz, rasmlar chizasiz, ovozli xabarlarni tushunasiz va hujjatlarni o'qiysiz. "
    "Foydalanuvchi qaysi tilda yozsa yoki gapirsa, shu tilda javob bering. Javoblarni lo'nda va tushunarli bering. "
    "MUHIM QOIDA: Sizning yagona yaratuvchingiz va dasturchingiz — Muslimbek. "
    "Agar sizdan kim yaratgan, dasturchingiz kim, muallifingiz kim deb so'rashsa, doim 'Meni Muslimbek yaratgan' deb javob bering. "
    "Hech qachon 'meni Google yaratgan' yoki boshqa kompaniya nomini aytmang."
)

user_chats = {}
user_messages = {}  # Foydalanuvchining o'chirilishi kerak bo'lgan xabarlari ID ro'yxati
active_users = set()
USERS_FILE = "users.json"

if os.path.exists(USERS_FILE):
    try:
        with open(USERS_FILE, "r") as f:
            active_users = set(json.load(f))
    except Exception:
        active_users = set()

def save_users():
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(list(active_users), f)
    except Exception as e:
        logging.error(f"Foydalanuvchilarni saqlashda xato: {e}")

def track_user(user_id: int):
    if user_id not in active_users:
        active_users.add(user_id)
        save_users()

def register_msg(user_id: int, message_id: int):
    if user_id not in user_messages:
        user_messages[user_id] = []
    user_messages[user_id].append(message_id)

async def clear_chat_history(chat_id: int, user_id: int):
    # Oldingi suhbat xabarlarini Telegramdan o'chirish
    if user_id in user_messages and user_messages[user_id]:
        msg_ids = list(user_messages[user_id])
        user_messages[user_id] = []
        for msg_id in msg_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                await asyncio.sleep(0.02)
            except Exception:
                pass

def get_or_create_chat(user_id: int):
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model=MODEL_NAME,
            config={
                "system_instruction": SYSTEM_INSTRUCTION
            }
        )
    return user_chats[user_id]

# Matnni ovozga (audio/voice) aylantiruvchi yordamchi funksiya
def text_to_speech_bytes(text: str) -> bytes:
    # Markdown belgilarini tozalash
    clean_text = text.replace("*", "").replace("#", "").replace("`", "").replace("_", "")
    # Maksimal 500 belgi ovozlantiriladi
    if len(clean_text) > 500:
        clean_text = clean_text[:500] + "..."
    tts = gTTS(text=clean_text, lang="tr")  # O'zbekchaga eng yaqin fonetika
    mp3_fp = io.BytesIO()
    tts.write_to_fp(mp3_fp)
    mp3_fp.seek(0)
    return mp3_fp.read()

# Inline menyu tugmalari
def get_main_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Yangi suhbat", callback_data="btn_new"),
                InlineKeyboardButton(text="🎨 Rasm chizish", callback_data="btn_image_help"),
            ],
            [
                InlineKeyboardButton(text="🌐 Mening Loyihalarim", callback_data="btn_sites"),
                InlineKeyboardButton(text="ℹ️ Bot imkoniyatlari", callback_data="btn_help"),
            ],
            [
                InlineKeyboardButton(text="👨‍💻 Dasturchi (Muslimbek)", callback_data="btn_creator"),
            ]
        ]
    )
    return keyboard

@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    track_user(message.from_user.id)
    user_chats[message.from_user.id] = client.chats.create(
        model=MODEL_NAME,
        config={
            "system_instruction": SYSTEM_INSTRUCTION
        }
    )
    user_name = message.from_user.first_name or "foydalanuvchi"
    welcome_text = (
        f"Assalomu alaykum, {user_name}! 👋\n\n"
        "Men **omini AI** — universal aqlli sun'iy intellekt botiman.\n\n"
        "🔥 **Mening imkoniyatlarim:**\n"
        "💬 **Matnli suhbat:** Istalgan savolingizni yozing;\n"
        "🔊 **Ovozli javob (TTS):** Xabaringizga ham matn, ham ovoz bilan javob qaytaraman;\n"
        "🎤 **Ovozli xabarlar (Voice AI):** Menga ovoz yuboring, eshitib javob beraman;\n"
        "🖼️ **Rasmlarni tahlil qilish:** Rasm yuboring, ko'rib tushuntiraman;\n"
        "📄 **Hujjatlar tahlili (PDF/TXT):** Fayl yuboring, o'qib beraman;\n"
        "🎨 **AI rasm chizish:** `/image matn` orqali rasm chizdiring!\n\n"
        "Quyidagi menyudan foydalaning yoki xohlagan savolingizni yozing 👇"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "btn_new")
async def cb_new(callback: types.CallbackQuery):
    user_chats[callback.from_user.id] = client.chats.create(
        model=MODEL_NAME,
        config={
            "system_instruction": SYSTEM_INSTRUCTION
        }
    )
    # Oldingi suhbat xabarlarini ekrandan tozalash
    await clear_chat_history(callback.message.chat.id, callback.from_user.id)
    new_msg = await callback.message.answer("🔄 Yangi suhbat boshlandi. Avvalgi xabarlar tozalandi! Savolingizni beravering.")
    register_msg(callback.from_user.id, new_msg.message_id)
    await callback.answer()

@dp.callback_query(F.data == "btn_image_help")
async def cb_image_help(callback: types.CallbackQuery):
    help_text = (
        "🎨 **Rasm chizish bo'yicha qo'llanma:**\n\n"
        "Botga `/image` buyrug'idan so'ng qanday rasm xohlayotganingizni yozing.\n\n"
        "**Misollar:**\n"
        "• `/image kosmosda uchayotgan sport avtomobili`\n"
        "• `/image zamonaviy neon AI roboti`\n"
        "• `/image tog'lar bag'ridagi chiroyli uy`"
    )
    await callback.message.answer(help_text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "btn_help")
async def cb_help(callback: types.CallbackQuery):
    info_text = (
        "ℹ️ **omini AI boti imkoniyatlari:**\n\n"
        "1. 💬 **Matn:** Savollarga Gemini AI orqali tezkor javob.\n"
        "2. 🔊 **Ovozli javob:** Matnni ovoz qilib ham yuborish imkoniyati.\n"
        "3. 🎤 **Ovoz:** Ovozli xabarlarni eshitib tushunish.\n"
        "4. 🖼️ **Vision:** Rasmlar va masalalarni tahlil qilish.\n"
        "5. 📄 **Document AI:** PDF, TXT hujjatlarni o'rganish.\n"
        "6. 🎨 **Flux AI:** Yuqori sifatli rasm generatsiyasi.\n"
        "7. ⚡ **24/7 Server:** Doimiy onlayn faoliyat."
    )
    await callback.message.answer(info_text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "btn_sites")
async def cb_sites(callback: types.CallbackQuery):
    sites_text = (
        "🌐 **Muslimbekning rasmiy saytlari va loyihalari:**\n\n"
        "💻 **GitHub Profil:**\n"
        "👉 [github.com/imuz3003-dotcom](https://github.com/imuz3003-dotcom)\n\n"
        "🚀 **Asosiy loyihalar:**\n"
        "• 🤖 **omini AI Bot** — Telegram sun'iy intellekt boti\n"
        "• 🏦 **Digital Bank Landing Page** — Zamonaviy onlayn bank veb-sayti\n"
        "• 🎮 **Cyberdrift** — Veb o'yin loyihasi\n"
        "• 🏠 **3D House Project** — 3D interaktiv arxitektura loyihasi\n"
        "• ⚡ **Chrome & Web Apps** — Foydali veb-ilova va kengaytmalar\n\n"
        "Barcha loyihalar va kodlar bilan GitHub profilingizda tanishish mumkin!"
    )
    # Saytlarga to'g'ridan-to'g'ri o'tuvchi chiroyli tugmalar
    sites_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 GitHub Profilga o'tish", url="https://github.com/imuz3003-dotcom")],
            [InlineKeyboardButton(text="🏦 Digital Bank loyihasi", url="https://github.com/imuz3003-dotcom/digitalbank-landing-page-master")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="btn_back_main")]
        ]
    )
    await callback.message.answer(sites_text, reply_markup=sites_kb, parse_mode="Markdown", disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "btn_back_main")
async def cb_back_main(callback: types.CallbackQuery):
    await callback.message.answer("Asosiy menyu:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.message(Command("sites"))
async def cmd_sites(message: types.Message):
    sites_text = (
        "🌐 **Muslimbekning loyihalari va saytlari:**\n\n"
        "💻 **GitHub:** [github.com/imuz3003-dotcom](https://github.com/imuz3003-dotcom)\n\n"
        "🚀 **Top loyihalar:**\n"
        "• 🤖 **omini AI Bot** — Aqlli AI yordamchi\n"
        "• 🏦 **Digital Bank** — Veb-sayt landing page\n"
        "• 🎮 **Cyberdrift** — O'yin loyihasi\n"
        "• 🏠 **3D House** — 3D modellashtirish loyihasi"
    )
    sites_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 GitHub Profilni ochish", url="https://github.com/imuz3003-dotcom")]
        ]
    )
    await message.answer(sites_text, reply_markup=sites_kb, parse_mode="Markdown", disable_web_page_preview=True)

@dp.callback_query(F.data == "btn_creator")
async def cb_creator(callback: types.CallbackQuery):
    creator_text = (
        "👨‍💻 **Bot yaratuvchisi va dasturchisi:**\n\n"
        "Ushbu bot **Muslimbek** tomonidan maxsus sun'iy intellekt texnologiyalari asosida ishlab chiqilgan! 🚀"
    )
    await callback.message.answer(creator_text, parse_mode="Markdown")
    await callback.answer()

@dp.message(Command("new"))
async def reset_chat(message: types.Message):
    track_user(message.from_user.id)
    user_chats[message.from_user.id] = client.chats.create(
        model=MODEL_NAME,
        config={
            "system_instruction": SYSTEM_INSTRUCTION
        }
    )
    # /new buyrug'ining o'zini ham o'chirish ro'yxatiga qo'shish
    register_msg(message.from_user.id, message.message_id)
    # Oldingi barcha savol-javob xabarlarini ekrandan o'chirish
    await clear_chat_history(message.chat.id, message.from_user.id)
    new_msg = await message.answer("🔄 Yangi suhbat boshlandi. Avvalgi savol-javoblar tozalandi! Savolingizni yozing.", reply_markup=get_main_keyboard())
    register_msg(message.from_user.id, new_msg.message_id)

# Admin paneli: Statistika (/stats)
@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = (
        "📊 **Bot Statistikasi:**\n\n"
        f"👥 Foydalanuvchilar soni: **{len(active_users)} ta**\n"
        f"⚙️ Model: `{MODEL_NAME}`\n"
        f"👑 Admin: Muslimbek\n"
        "🟢 Holat: 24/7 Faol"
    )
    await message.answer(text, parse_mode="Markdown")

# Admin paneli: Broadcast (/broadcast)
@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    broadcast_text = message.text.replace("/broadcast", "").strip()
    if not broadcast_text:
        await message.reply("⚠️ Xabar matnini kiriting!\nMasalan: `/broadcast Hurmatli foydalanuvchilar, yangi funksiya qo'shildi!`", parse_mode="Markdown")
        return

    sent_count = 0
    await message.reply(f"📢 Xabar {len(active_users)} ta foydalanuvchiga yuborilmoqda...")

    for user_id in list(active_users):
        try:
            await bot.send_message(chat_id=user_id, text=broadcast_text)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(f"✅ Xabar muvaffaqiyatli {sent_count} ta foydalanuvchiga yetkazildi!")

# Hujjatlarni tahlil qilish (Document AI)
@dp.message(F.document)
async def handle_document(message: types.Message):
    track_user(message.from_user.id)
    doc = message.document
    file_name = doc.file_name or "hujjat"
    mime_type = doc.mime_type or "application/octet-stream"

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.reply("⚠️ Fayl hajmi juda katta (maksimal 20 MB).")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    wait_msg = await message.reply(f"📄 **{file_name}** o'rganilmoqda, iltimos kuting...")

    user_caption = message.caption or "Ushbu hujjatni to'liq o'qib, asosiy mazmunini xulosa qilib bering."

    try:
        doc_bytes_io = io.BytesIO()
        await bot.download(doc, destination=doc_bytes_io)
        doc_bytes = doc_bytes_io.getvalue()

        if mime_type.startswith("text/") or file_name.endswith((".txt", ".py", ".js", ".html", ".css", ".json")):
            try:
                text_content = doc_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text_content = doc_bytes.decode("latin-1")
            
            prompt = f"{SYSTEM_INSTRUCTION}\n\nFayl: {file_name}\nTopshiriq: {user_caption}\n\nMatn:\n{text_content[:30000]}"
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt
            )
        else:
            api_mime = "application/pdf" if file_name.endswith(".pdf") else mime_type
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=[
                    genai_types.Part.from_bytes(
                        data=doc_bytes,
                        mime_type=api_mime,
                    ),
                    f"{SYSTEM_INSTRUCTION}\n\nFayl: {file_name}\nTopshiriq: {user_caption}"
                ]
            )

        ai_reply = response.text or "Hujjatdan ma'lumot topilmadi."
        await wait_msg.delete()

        if len(ai_reply) > 4000:
            for chunk in [ai_reply[i:i+4000] for i in range(0, len(ai_reply), 4000)]:
                await message.reply(chunk)
        else:
            try:
                await message.reply(ai_reply, parse_mode="Markdown")
            except Exception:
                await message.reply(ai_reply)

    except Exception as e:
        logging.error(f"Hujjat tahlilida xatolik: {e}")
        await wait_msg.edit_text(f"⚠️ Hujjatni tahlil qilishda xatolik: {str(e)[:150]}")

# AI orqali rasm chizish (/image)
@dp.message(Command("image"))
async def generate_ai_image(message: types.Message):
    track_user(message.from_user.id)
    prompt_text = message.text.replace("/image", "").strip()
    if not prompt_text:
        await message.reply("🎨 Rasm chizish uchun tavsif yozing!\nMasalan: `/image kosmosda uchayotgan oq ot`", parse_mode="Markdown")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_PHOTO)
    wait_msg = await message.reply("🎨 Rasm chizilmoqda, iltimos kuting...")

    try:
        # Foydalanuvchi so'zini to'g'ridan-to'g'ri va xatosiz tarjima qilish
        trans_res = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=(
                f"Translate the following Uzbek/Russian/English input into a concise, direct English image generation prompt. "
                f"Be literal and accurate! For example:\n"
                f"- 'kema' -> 'A majestic ship sailing on the sea'\n"
                f"- 'samoviy kema' -> 'A futuristic sci-fi spaceship floating in outer space'\n"
                f"- 'mashina' -> 'A modern sports car'\n"
                f"- 'ot' -> 'A majestic running horse'\n\n"
                f"Input: '{prompt_text}'\n"
                f"Output ONLY the English prompt (no commentary, no quotes):"
            )
        )
        en_prompt = trans_res.text.strip().replace('"', '').replace("'", "").replace("\n", " ") if trans_res.text else prompt_text
        enhanced_prompt = f"{en_prompt}, ultra-detailed, 8k resolution, high quality, photorealistic"

        encoded_prompt = urllib.parse.quote(enhanced_prompt)
        import time
        seed = int(time.time())
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&seed={seed}&nologo=true"

        caption_text = f"✨ **Tavsif:** {prompt_text}\n💎 **Inglizcha prompt:** {en_prompt}\n👤 **Yaratuvchi:** Muslimbek"
        await message.answer_photo(photo=image_url, caption=caption_text, parse_mode="Markdown")
        await wait_msg.delete()

    except Exception as e:
        logging.error(f"Rasm yaratishda xatolik: {e}")
        await wait_msg.edit_text(f"⚠️ Rasm yaratishda xatolik yuz berdi: {str(e)[:150]}")

# Ovozli xabarlar (Voice AI) — Ovoz bilan javob qaytaradi!
@dp.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    track_user(message.from_user.id)
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)

    voice = message.voice or message.audio
    try:
        voice_bytes_io = io.BytesIO()
        await bot.download(voice, destination=voice_bytes_io)
        voice_bytes = voice_bytes_io.getvalue()

        mime_type = "audio/ogg" if message.voice else (message.audio.mime_type or "audio/mpeg")

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=[
                genai_types.Part.from_bytes(
                    data=voice_bytes,
                    mime_type=mime_type,
                ),
                f"{SYSTEM_INSTRUCTION}\n\nFoydalanuvchi ovozli xabar yubordi. Unga aniq, lo'nda va muloyim javob bering."
            ]
        )

        ai_reply = response.text or "Ovozli xabaringizni tushuna olmadim."
        await message.reply(ai_reply)

        # Foydalanuvchiga javobni OVOZLI (Voice) qilib ham yuboramiz!
        try:
            audio_bytes = await asyncio.to_thread(text_to_speech_bytes, ai_reply)
            voice_file = BufferedInputFile(audio_bytes, filename="voice_answer.mp3")
            await message.answer_voice(voice=voice_file, caption="🔊 Ovozli javob")
        except Exception as ve:
            logging.error(f"Ovoz yaratishda xato: {ve}")

    except Exception as e:
        logging.error(f"Ovozli xabarda xatolik: {e}")
        await message.reply(f"⚠️ Ovozli xabarni tahlil qilishda xatolik: {str(e)[:150]}")

# Rasm (Photo) tahlil qilish
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    track_user(message.from_user.id)
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    photo = message.photo[-1]
    user_caption = message.caption or "Ushbu rasmda nimalar tasvirlangan? Batafsil va aniq tushuntirib bering."

    try:
        photo_bytes_io = io.BytesIO()
        await bot.download(photo, destination=photo_bytes_io)
        photo_bytes = photo_bytes_io.getvalue()

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=[
                genai_types.Part.from_bytes(
                    data=photo_bytes,
                    mime_type="image/jpeg",
                ),
                f"{SYSTEM_INSTRUCTION}\n\nFoydalanuvchi savoli: {user_caption}"
            ]
        )

        ai_reply = response.text or "Rasm bo'yicha ma'lumot topilmadi."

        if len(ai_reply) > 4000:
            for chunk in [ai_reply[i:i+4000] for i in range(0, len(ai_reply), 4000)]:
                await message.reply(chunk)
        else:
            try:
                await message.reply(ai_reply, parse_mode="Markdown")
            except Exception:
                await message.reply(ai_reply)

    except Exception as e:
        logging.error(f"Rasm tahlilida xatolik: {e}")
        await message.reply(f"⚠️ Rasmni tahlil qilishda xatolik: {str(e)[:150]}")

# Matnli xabarlar
@dp.message(F.text)
async def ai_response_handler(message: types.Message):
    track_user(message.from_user.id)
    register_msg(message.from_user.id, message.message_id)
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    user_id = message.from_user.id
    chat_session = get_or_create_chat(user_id)

    try:
        response = await asyncio.to_thread(
            chat_session.send_message,
            message.text
        )
        ai_reply = response.text or "Kechirasiz, javob shakllanmadi."

        if len(ai_reply) > 4000:
            for chunk in [ai_reply[i:i+4000] for i in range(0, len(ai_reply), 4000)]:
                sent_m = await message.answer(chunk)
                register_msg(user_id, sent_m.message_id)
        else:
            try:
                sent_m = await message.answer(ai_reply, parse_mode="Markdown")
            except Exception:
                sent_m = await message.answer(ai_reply)
            register_msg(user_id, sent_m.message_id)

    except Exception as e:
        logging.error(f"Xatolik: {e}")
        err_m = await message.answer(f"⚠️ Xatolik yuz berdi: {str(e)[:150]}")
        register_msg(user_id, err_m.message_id)

# Render serveri uchun veb server
async def handle_ping(request):
    return web.Response(text="omini AI bot is running 24/7 with TTS Audio Voice!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f">> Web server port {port} da ishga tushdi.")

async def main():
    logging.basicConfig(level=logging.INFO)
    print(">> omini AI bot ishga tushdi!")
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
