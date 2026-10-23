"""omini AI — yadro yordamchilari: sozlamalar, SQLite store, Markdown->HTML, AI va media.

Bot mantiqi (handlerlar) bot.py da, shu modulda esa faqat qayta ishlatiladigan
yadro: konfiguratsiya, baza, formatlash, Gemini bilan ishlash, TTS va rasm generatsiyasi.
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from gtts import gTTS
from google import genai
from google.genai import types as genai_types

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8692018182"))

MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-flash-lite")
MODEL_LIST = [m.strip() for m in os.getenv("MODEL_LIST", MODEL_NAME).split(",") if m.strip()]
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
TTS_LANGS = [l.strip() for l in os.getenv("TTS_LANGS", "uz,tr").split(",") if l.strip()]
RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "6"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "30"))
MAX_VOICE_CHARS = int(os.getenv("MAX_VOICE_CHARS", "700"))
HISTORY_REPLAY = int(os.getenv("HISTORY_REPLAY", "24"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.8"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "4096"))
IMAGE_TIMEOUT = int(os.getenv("IMAGE_TIMEOUT", "120"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "150"))
TG_MSG_LIMIT = 4096
CHUNK_LIMIT = 3600

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN yoki GEMINI_API_KEY topilmadi! .env faylini tekshiring.")

client = genai.Client(api_key=GEMINI_API_KEY)

CREATOR_RULE = (
    "MUHIM QOIDA: Sizning yagona yaratuvchingiz va dasturchingiz — Muslimbek. "
    "Kim yaratgan, dasturchingiz kim deb so'rashsa, doim 'Meni Muslimbek yaratgan' deb javob bering. "
    "Hech qachon 'meni Google yaratgan' yoki boshqa kompaniya nomini aytmang."
)
FORMAT_RULE = (
    "Siz Telegram botidasiz: javoblaringizda faqat oddiy markdown ishlatings — **qalin**, *kursiv*, "
    "`kod`, ```blok kod```, - ro'yxat va [matn](https://havola). HTML teglari yoki markdown jadval ishlatmang. "
    "Javobni lo'nda, aniq va bo'limlarga bo'lib bering; keraksiz takrorlanishdan qoching."
)
BASE_INSTRUCTION = (
    "Siz 'omini AI' nomli universal, aqlli va xushmuomala sun'iy intellekt yordamchisisiz. "
    "Matn bilan suhbat qilasiz, rasm tahlil qilasiz, ovozli xabarlarni tushunasiz, hujjatlarni o'qiysiz va rasm chizasiz. "
    "Foydalanuvchi qaysi tilda yozsa yoki gapirsa, shu tilda javob bering. " + FORMAT_RULE + " " + CREATOR_RULE
)

PERSONAS = {
    "general": ("💬 Umumiy yordamchi", "Umumiy savollarga aniq, ishonchli va foydali javob ber."),
    "translator": ("🌍 Tarjimon", "Foydalanuvchi matnini professional tarjimonga aylantir. Agar til ko'rsatilmagan bo'lsa, o'zbek tiliga tarjima qil. Tarjimadan keyin qisqa izoh yoki muqobil variant ber."),
    "teacher": ("🎓 O'qituvchi", "Mavzuni bosqichma-bosqich, misollar va sodda tilda tushuntir. Foydalanuvchini o'ylashga undovchi savol ber, mavzuni oxirida 3 ta mashq taklif qil."),
    "coder": ("👨‍💻 Dasturchi", "Dasturlash bo'yicha yuqori sifatli, ishlaydigan kod yoz. Kodni blokda ber, xatolik xavflarini aytib o't, eng yaxshi amaliyotlarga amal qil."),
    "writer": ("✍️ Yozuvchi", "Ijodiy matn, she'r, hikoya, post va e'lon matnlari yoz. Uslub, ohang va uzunlikni foydalanuvchidan so'ra yoki mos tanla."),
    "math": ("🧮 Matematikachi", "Masalani yechishda har bir qadamni ko'rsat, formulani `kod` blokida yoz, javobni alohida belgilab ber."),
}


def persona_instruction(key: str) -> str:
    label, extra = PERSONAS.get(key, PERSONAS["general"])
    return f"{BASE_INSTRUCTION}\n\nSizning rolingiz: {label}. {extra}"


# --------------------------------------------------------------------------- baza


class Store:
    """SQLite asosidagi oddiy saqlash: foydalanuvchilar, xotira, statistika."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at REAL,
                last_seen REAL,
                persona TEXT DEFAULT 'general',
                model TEXT,
                voice INTEGER DEFAULT 1,
                search INTEGER DEFAULT 1,
                prefs TEXT DEFAULT '{}',
                banned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, seq);
            CREATE TABLE IF NOT EXISTS usage(
                day TEXT,
                user_id INTEGER,
                kind TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY(day, user_id, kind)
            );
            CREATE TABLE IF NOT EXISTS tracked(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                msg_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked(user_id);
            """
        )
        self._db.commit()
        self._migrate_legacy_users()

    def _migrate_legacy_users(self):
        """Eski users.json faylidagi IDlarni bir marta bazaga ko'chiradi."""
        if not os.path.exists("users.json"):
            return
        try:
            with open("users.json", "r") as f:
                ids = json.load(f)
        except Exception:
            return
        with self._lock:
            for uid in ids:
                try:
                    self._db.execute(
                        "INSERT OR IGNORE INTO users(id, created_at, last_seen) VALUES(?,?,?)",
                        (int(uid), time.time(), time.time()),
                    )
                except Exception:
                    continue
            self._db.commit()

    def _write(self, sql: str, params: tuple = ()):
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def touch_user(self, user_id: int, username: str = None, first_name: str = None):
        now = time.time()
        self._write(
            """INSERT INTO users(id, username, first_name, created_at, last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
            (user_id, username, first_name, now, now),
        )

    def user(self, user_id: int) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            self.touch_user(user_id)
            return self.user(user_id)
        return dict(row)

    def set_flag(self, user_id: int, column: str, value):
        assert column in {"persona", "model", "voice", "search", "banned", "username", "first_name"}
        self._write(f"UPDATE users SET {column}=?, last_seen=? WHERE id=?", (value, time.time(), user_id))

    def set_pref(self, user_id: int, key: str, value) -> dict:
        prefs = dict(json.loads(self.user(user_id).get("prefs") or "{}"))
        prefs[key] = value
        self._write("UPDATE users SET prefs=? WHERE id=?", (json.dumps(prefs), user_id))
        return prefs

    def prefs(self, user_id: int) -> dict:
        return dict(json.loads(self.user(user_id).get("prefs") or "{}"))

    def add_note(self, user_id: int, note: str, limit: int = 12) -> list:
        prefs = self.prefs(user_id)
        notes = (prefs.get("notes") or []) + [note.strip()]
        notes = [n[:500] for n in notes][-limit:]
        self.set_pref(user_id, "notes", notes)
        return notes

    def notes(self, user_id: int) -> list:
        return self.prefs(user_id).get("notes") or []

    def add_message(self, user_id: int, role: str, content: str):
        self._write(
            "INSERT INTO messages(user_id, role, content, created_at) VALUES(?,?,?,?)",
            (user_id, role, content[:8000], time.time()),
        )

    def history(self, user_id: int, limit: int = HISTORY_REPLAY):
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content FROM messages WHERE user_id=? ORDER BY seq DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def drop_history(self, user_id: int):
        self._write("DELETE FROM messages WHERE user_id=?", (user_id,))

    def track_msg(self, user_id: int, msg_id: int):
        self._write("INSERT INTO tracked(user_id, msg_id) VALUES(?,?)", (user_id, msg_id))

    def tracked_msgs(self, user_id: int) -> list:
        with self._lock:
            rows = self._db.execute("SELECT msg_id FROM tracked WHERE user_id=?", (user_id,)).fetchall()
        return [r["msg_id"] for r in rows]

    def untrack_msgs(self, user_id: int):
        self._write("DELETE FROM tracked WHERE user_id=?", (user_id,))

    def bump(self, user_id: int, kind: str = "ai", amount: int = 1):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._write(
            """INSERT INTO usage(day, user_id, kind, count) VALUES(?,?,?,?)
               ON CONFLICT(day, user_id, kind) DO UPDATE SET count=count+?""",
            (day, user_id, kind, amount, amount),
        )

    def is_banned(self, user_id: int) -> bool:
        return bool(self.user(user_id).get("banned"))

    def all_users(self) -> list:
        with self._lock:
            return [r["id"] for r in self._db.execute("SELECT id FROM users").fetchall()]

    def today_total(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(count),0) AS c FROM usage WHERE day=?", (day,)
            ).fetchone()
        return row["c"]

    def today_count(self, user_id: int) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self._db.execute(
                "SELECT count AS c FROM usage WHERE day=? AND user_id=? AND kind='ai'",
                (day, user_id),
            ).fetchone()
        return row["c"] if row else 0

    def stats(self) -> dict:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            total_users = self._db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            active_today = self._db.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM usage WHERE day=? AND kind='ai'", (day,)
            ).fetchone()["c"]
            msg_total = self._db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            top = self._db.execute(
                "SELECT user_id, SUM(count) AS c FROM usage WHERE kind='ai' GROUP BY user_id ORDER BY c DESC LIMIT 5"
            ).fetchall()
        return {
            "users": total_users,
            "active_today": active_today,
            "requests_today": self.today_total(),
            "stored_messages": msg_total,
            "top": [(r["user_id"], r["c"]) for r in top],
        }


store = Store(DB_PATH)


# --------------------------------------------------------------------- formatlash

_PLACEHOLDER = "\x00{}\x01"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html(text: str) -> str:
    """Matnni HTML'ga o'giradi; href atributlarida ham xavfsiz ishlaydi."""
    return _escape(text or "").replace('"', "&quot;")


def md_to_html(text: str) -> str:
    """Gemini markdown'ini Telegram HTML'ga aylantiradi (xavfsiz, buzilmaydi)."""
    if not text:
        return ""
    parts = re.split(r"```", text)
    out = []
    for index, segment in enumerate(parts):
        if index % 2 == 1:
            out.append("<pre><code>" + _escape(_drop_language(segment)) + "</code></pre>")
            continue
        out.append(_inline_to_html(segment))
    return "".join(out)


_CODE_LANGS = {
    "python", "py", "js", "jsx", "ts", "tsx", "javascript", "typescript", "html", "css", "scss",
    "json", "yaml", "yml", "toml", "ini", "xml", "md", "markdown", "bash", "sh", "shell", "zsh",
    "sql", "java", "kotlin", "swift", "dart", "c", "cpp", "cs", "go", "rust", "rs", "php", "rb",
    "lua", "r", "pl", "ps1", "powershell", "dockerfile", "plaintext", "text", "vue", "graphql",
}


def _drop_language(code: str) -> str:
    """```python``` kabi til belgisini kod blokidagi matndan olib tashlaydi."""
    lines = code.strip("\n").split("\n")
    if len(lines) > 1 and lines[0].strip().lower() in _CODE_LANGS:
        lines = lines[1:]
    return "\n".join(lines)


def _inline_to_html(chunk: str) -> str:
    if not chunk:
        return ""
    protected = []

    def keep(html: str) -> str:
        protected.append(html)
        return _PLACEHOLDER.format(len(protected) - 1)

    text = _escape(chunk)
    # oldin himoyalanadigan qismlar: inline kod va havolalar
    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + m.group(1) + "</code>"), text)
    text = re.sub(
        r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)",
        lambda m: keep('<a href="' + m.group(2) + '">' + m.group(1) + "</a>"),
        text,
    )
    text = re.sub(r"^#{1,6}\s*(.+)$", lambda m: keep("<b>" + m.group(1).strip() + "</b>") + "\n", text, flags=re.M)
    text = re.sub(r"\*\*\*([^*\n]+)\*\*\*", lambda m: keep("<b><i>" + m.group(1) + "</i></b>"), text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: keep("<b>" + m.group(1) + "</b>"), text)
    text = re.sub(r"__([^_\n]+)__", lambda m: keep("<b>" + m.group(1) + "</b>"), text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", lambda m: keep("<i>" + m.group(1) + "</i>"), text)
    text = re.sub(r"~~([^~\n]+)~~", lambda m: keep("<s>" + m.group(1) + "</s>"), text)
    text = re.sub(r"^\s*[-•*]\s+", "• ", text, flags=re.M)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    for index, html in enumerate(protected):
        text = text.replace(_PLACEHOLDER.format(index), html)
    return text


def html_chunks(text: str, limit: int = CHUNK_LIMIT, hard_limit: int = 4000) -> list:
    """Matnni Telegram HTML xabarlari ro'yxatiga aylantiradi (teglar qo'shimcha o'rin oladi)."""
    result = []
    for chunk in safe_chunks(text, limit):
        html = md_to_html(chunk)
        if len(html) <= hard_limit:
            result.append(html)
            continue
        smaller = max(400, int(limit * hard_limit / max(len(html), 1)) - 50)
        if smaller >= limit:
            result.append(html[:hard_limit])
            continue
        result.extend(html_chunks(chunk, smaller, hard_limit))
    return result


def strip_markdown(text: str) -> str:
    """Markdown belilarini tushirib qoldiradi (TTS va zaxira javoblar uchun)."""
    text = re.sub(r"```", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"__([^_]*)__", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*]*)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text.strip()


def safe_chunks(text: str, limit: int = CHUNK_LIMIT) -> list:
    """Matnni Telegram chegarasiga bo'ladi: kod bloklari va paragraflar oralib yuborilmaydi."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    budget = limit - 24  # ``` qayta ochilishi/yopilishi uchun zaxira
    chunks = []
    current = ""
    for unit in _split_units(text, budget):
        candidate = current + "\n" + unit if current else unit
        if len(candidate) > budget:
            if current:
                chunks.append(current)
            current = unit
        else:
            current = candidate
    if current.strip():
        chunks.append(current)

    # bir chunkda ochiq qolgan kod blokini yopib, keyingisida qayta ochamiz
    fixed, open_fence = [], False
    for chunk in chunks:
        if open_fence:
            chunk = "```\n" + chunk
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            open_fence = True
        else:
            open_fence = False
        fixed.append(chunk.strip("\n"))
    return [c for c in fixed if c]


def _split_units(text: str, budget: int) -> list:
    lines = text.split("\n")
    units, paragraph, index = [], [], 0

    def flush():
        if paragraph:
            units.extend(_split_plain("\n".join(paragraph), budget))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            block = [line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                closed = lines[index].strip().startswith("```")
                index += 1
                if closed:
                    break
            flush()
            units.extend(_split_code("\n".join(block), budget))
            continue
        if line.strip() == "":
            flush()
            index += 1
            continue
        paragraph.append(line)
        index += 1
    flush()
    return units


def _split_plain(block: str, budget: int) -> list:
    if len(block) <= budget:
        return [block]
    pieces, current = [], ""
    for line in block.split("\n"):
        if len(line) > budget:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(line, budget))
            continue
        candidate = current + "\n" + line if current else line
        if len(candidate) > budget:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces


def _split_code(block: str, budget: int) -> list:
    if len(block) <= budget:
        return [block]
    body = block.strip("` \n")
    wrapped = []
    for piece in _split_plain(body, budget - 12):
        wrapped.append("```\n" + piece + "\n```")
    return wrapped


def _hard_split(block: str, budget: int) -> list:
    pieces, rest = [], block
    while rest:
        cut = rest.rfind(" ", 0, budget)
        if cut < budget // 2:
            cut = budget
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    return [p for p in pieces if p]


# ---------------------------------------------------------------------------- AI


def _search_tool():
    google_search = getattr(genai_types, "GoogleSearch", None)
    if google_search is None:
        return None
    try:
        return genai_types.Tool(google_search=google_search())
    except Exception:
        return None


_search_broken = set()


def build_config(use_search: bool, system_instruction: str = BASE_INSTRUCTION):
    cfg = {
        "temperature": TEMPERATURE,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "system_instruction": system_instruction,
    }
    tool = _search_tool() if use_search else None
    if tool is not None:
        cfg["tools"] = [tool]
    return cfg


def to_history(records: list) -> list:
    """SQLite yozuvlarini Gemini chat history'ga aylantiradi (to'liq bo'lmasin deb qisqartiriladi)."""
    history = []
    for rec in records:
        content = (rec.get("content") or "").strip()
        role = "model" if rec.get("role") == "model" else "user"
        if not content:
            continue
        history.append(
            genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=content[:4000])])
        )
    return history


def create_chat(user_id: int, system_instruction: str, model: str, use_search: bool, replay: bool = True):
    """Yangi Gemini sessiyasi; bazadagi oxirgi suhbatni xotiraga qayta yuklaydi."""
    history = to_history(store.history(user_id, HISTORY_REPLAY)) if replay else []
    attempts = [use_search, False] if use_search and model not in _search_broken else [False]
    last_error = None
    for want_search in attempts:
        try:
            chat = client.chats.create(
                model=model,
                config=build_config(want_search, system_instruction),
                history=history,
            )
            return chat, want_search
        except Exception as error:
            last_error = error
            if want_search:
                _search_broken.add(model)
                logging.warning("Web-search modeli qo'llab-quvvatlamadi (%s): %s", model, error)
    raise last_error


def extract_text(response) -> str:
    try:
        text = response.text
    except Exception:
        text = None
    if text:
        return text.strip()
    try:
        parts = response.candidates[0].content.parts
        return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
    except Exception:
        return ""


def extract_sources(response) -> list:
    """google_search grounding bo'lsa manba havolalarini qaytaradi."""
    seen, links = set(), []
    try:
        chunks = response.metadata.google_search_grounding_metadata.grounding_chunks
    except Exception:
        return links
    for chunk in chunks or []:
        web_info = getattr(chunk, "web", None)
        if not web_info:
            continue
        uri = getattr(web_info, "uri", None)
        title = getattr(web_info, "title", None) or (uri.split("/")[2] if uri and len(uri.split("/")) > 2 else uri)
        if uri and uri not in seen:
            seen.add(uri)
            links.append((title, uri))
    return links[:5]


def ask_once(model: str, contents, use_search: bool, system_instruction: str = BASE_INSTRUCTION) -> tuple:
    """Bir martalik so'rov (media/audit uchun chat sessiyasi kerak emas)."""
    attempts = [use_search, False] if use_search and model not in _search_broken else [False]
    last_error = None
    for want_search in attempts:
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=build_config(want_search, system_instruction),
            )
            return extract_text(response), extract_sources(response)
        except Exception as error:
            last_error = error
            if want_search:
                _search_broken.add(model)
                logging.warning("Web-search bilan so'rov yutildi: %s", error)
    raise last_error


def translate_prompt_to_english(prompt_text: str, model: str) -> str:
    result = ""
    try:
        result, _ = ask_once(
            model,
            "Translate the user's image idea into ONE concise English prompt for an image generator. "
            "Describe the subject literally and directly (e.g. 'kema' -> 'a ship sailing on the sea', "
            "'ot' -> 'a running horse', 'samoviy kema' -> 'a spaceship in outer space'). "
            "Output only the English prompt, no quotes, no commentary.\n\nInput: " + prompt_text,
            use_search=False,
        )
    except Exception as error:
        logging.warning("Prompt tarjimasi yutildi: %s", error)
    result = result.strip().strip('"').strip()
    return (result or prompt_text).replace("\n", " ")


# ------------------------------------------------------------------------ ovoz


def text_to_speech_bytes(text: str, langs: list = None) -> tuple:
    """Matnni mp3'ga aylantiradi; ishlamaydigan tillar bo'lsa keyingi tilga o'tadi."""
    clean = strip_markdown(text)[:MAX_VOICE_CHARS]
    errors = []
    for lang in langs or TTS_LANGS:
        try:
            stream = io.BytesIO()
            gTTS(text=clean, lang=lang).write_to_fp(stream)
            stream.seek(0)
            data = stream.read()
            if data:
                return data, lang
        except Exception as error:
            errors.append(f"{lang}: {error}")
    raise RuntimeError("; ".join(errors) or "TTS bo'sh javob qaytardi")


# ------------------------------------------------------------------- rasm AI


ASPECT_SIZES = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
}
IMAGE_MODELS = ("flux", "turbo", "nanobanana")


def parse_image_request(text: str) -> tuple:
    """`/image` matnidan bayroqlarni ajratadi: --ar 16:9, --seed 42, --model turbo."""
    options = {"aspect": "1:1", "seed": None, "model": "flux", "nologo": True}
    flags = re.findall(r"--(ar|seed|model)\s+(\S+)", text)
    prompt = re.sub(r"--(ar|seed|model)\s+\S+", "", text).strip()
    for name, value in flags:
        if name == "ar" and value in ASPECT_SIZES:
            options["aspect"] = value
        elif name == "seed" and value.isdigit():
            options["seed"] = int(value)
        elif name == "model" and value in IMAGE_MODELS:
            options["model"] = value
    return prompt, options


def pollinations_url(prompt: str, options: dict) -> str:
    width, height = ASPECT_SIZES.get(options.get("aspect", "1:1"), (1024, 1024))
    seed = options.get("seed") or int(time.time() * 1000) % 10**9
    enhanced = f"{prompt}, ultra-detailed, high quality, sharp focus"
    return (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(enhanced)
        + f"?width={width}&height={height}&model={options.get('model', 'flux')}"
        + f"&seed={seed}&nologo=true&referrer=omini-ai-bot"
    )


async def download_image(url: str, attempts: int = 3) -> bytes:
    """Rasmni haqiqiy fayl sifatida yuklab oladi (Telegram tashqi URL'ga bog'liq qolmaydi)."""
    timeout = aiohttp.ClientTimeout(total=IMAGE_TIMEOUT)
    last_error = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(attempts):
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.read()
                        if len(data) > 1000:
                            return data
                        raise RuntimeError("rasm javobi juda kichik")
                    last_error = RuntimeError(f"pollinations HTTP {response.status}")
            except Exception as error:
                last_error = error
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Rasmni yuklab bo'lmadi: {last_error}")


# ------------------------------------------------------- limit va holat boshqaruvi


class SlidingWindowLimiter:
    """Foydalanuvchi/suvatchi bo'yicha spam himoyasi (o'tkazib yuboriladigan so'rovlar)."""

    def __init__(self, max_events: int, window: int):
        self.max_events = max_events
        self.window = window
        self._events = defaultdict(deque)

    def check(self, key) -> tuple:
        now = time.monotonic()
        queue = self._events[key]
        while queue and now - queue[0] > self.window:
            queue.popleft()
        if len(queue) >= self.max_events:
            return False, round(self.window - (now - queue[0]), 1)
        queue.append(now)
        return True, 0.0

    def reset(self, key):
        self._events.pop(key, None)


limiter = SlidingWindowLimiter(RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW)
chat_locks: dict = {}
inflight: dict = {}
session_state: dict = {}


def lock_for(user_id: int) -> asyncio.Lock:
    lock = chat_locks.get(user_id)
    if lock is None:
        lock = chat_locks[user_id] = asyncio.Lock()
    return lock


def remember_session(user_id: int, chat, use_search: bool):
    session_state[user_id] = {"chat": chat, "search": use_search}
    return session_state[user_id]


def current_session(user_id: int) -> dict:
    return session_state.get(user_id, {})


def reset_session(user_id: int):
    session_state.pop(user_id, None)
    limiter.reset(user_id)
    chat_locks.pop(user_id, None)


def human_wait(seconds: float) -> str:
    return f"{int(seconds) + 1}" if seconds < 60 else f"{round(seconds / 60, 1)} daqiqa"


def setup_logging():
    """Konsol + bot.log fayliga yozadi (fayl yozib bo'lmasa faqat konsol)."""
    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        try:
            from logging.handlers import RotatingFileHandler

            handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
