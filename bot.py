import os
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatAction
from aiohttp import web
from google import genai

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN yoki GEMINI_API_KEY topilmadi!")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MODEL_NAME = "gemini-3.5-flash-lite"

SYSTEM_INSTRUCTION = (
    "Siz aqlli, xushmuomala, aniq va ixcham javob beradigan 'omini AI' nomli sun'iy intellektsiz. "
    "Foydalanuvchi qaysi tilda yozsa, shu tilda javob bering. Javoblarni keraksiz cho'zmasdan, lo'nda va tushunarli bering. "
    "MUHIM QOIDA: Sizning yagona yaratuvchingiz va dasturchingiz — Muslimbek. "
    "Agar sizdan kim yaratgan, dasturchingiz kim, muallifingiz kim deb so'rashsa, doim 'Meni Muslimbek yaratgan' deb javob bering. "
    "Hech qachon 'meni Google yaratgan' yoki boshqa kompaniya nomini aytmang."
)

user_chats = {}

def get_or_create_chat(user_id: int):
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model=MODEL_NAME,
            config={
                "system_instruction": SYSTEM_INSTRUCTION
            }
        )
    return user_chats[user_id]

@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    user_chats[message.from_user.id] = client.chats.create(
        model=MODEL_NAME,
        config={
            "system_instruction": SYSTEM_INSTRUCTION
        }
    )
    user_name = message.from_user.first_name or "foydalanuvchi"
    welcome_text = (
        f"Assalomu alaykum, {user_name}! 👋\n\n"
        "Men **omini AI** — aqlli va o'ta tezkor sun'iy intellekt botiman.\n"
        "Menga istalgan savolingizni yozing, darhol javob beraman! ⚡\n\n"
        "Yangi mavzuda suhbat boshlash uchun: /new"
    )
    await message.answer(welcome_text, parse_mode="Markdown")

@dp.message(Command("new"))
async def reset_chat(message: types.Message):
    user_chats[message.from_user.id] = client.chats.create(
        model=MODEL_NAME,
        config={
            "system_instruction": SYSTEM_INSTRUCTION
        }
    )
    await message.answer("🔄 Yangi suhbat boshlandi. Xotira tozalandi. Savolingizni beravering!")

@dp.message()
async def ai_response_handler(message: types.Message):
    if not message.text:
        return

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
                await message.answer(chunk)
        else:
            try:
                await message.answer(ai_reply, parse_mode="Markdown")
            except Exception:
                await message.answer(ai_reply)

    except Exception as e:
        logging.error(f"Xatolik: {e}")
        await message.answer(f"⚠️ Xatolik yuz berdi: {str(e)[:150]}")

# Render bepul Web Service uchun doimiy ochiq turuvchi port
async def handle_ping(request):
    return web.Response(text="omini AI bot is running 24/7!")

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
    print(">> omini AI ultra-fast bot ishga tushdi!")
    # Render Web Service uchun web serverni ishga tushiramiz
    await start_web_server()
    # Telegram pollingni ishga tushiramiz
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
