"""So'kinish detektorini tekshiradi: oddiy gaplar bloklanmasligi, haqiqiy haqoratlar
topilishi kerak. Xatolar xavfsizlik uchun emas, noto'g'ri bloklar uchun xavflidir —
shuning uchun OK ro'yxati BAD ro'yxatidan uzunroq va e'tiborli yozilgan.

Ishga tushirish:  python -m unittest test_profanity -v
"""

import os
import sys
import tempfile
import unittest

os.environ["BOT_TOKEN"] = os.environ.get("BOT_TOKEN", "test:token")
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "test-key")
_tmpdir = tempfile.mkdtemp(prefix="omini_test_prof_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "bot_data.db")
os.environ["LOG_FILE"] = ""
# .env'dan mustaqil bo'lishi uchun moderasiya sozlamalari qat'iy qo'yiladi:
os.environ["PROFANITY_MODE"] = "block"
os.environ["PROFANITY_BLOCK_AFTER"] = "2"
os.environ["PROFANITY_BAN_HOURS"] = "0"
os.environ["PROFANITY_WORDS"] = ""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import botcore as core  # noqa: E402


class TestNoFalsePositives(unittest.TestCase):
    """Bu gaplarning HEC QAYSISI bloklanmasligi kerak."""

    OK = [
        # foydalanuvchi shikoyat qilgan real xabar (akkaunt noto'g'ri bloklangan edi)
        "men bir qiz sinfdoshimga maktabda bila otreli dedim u yoq dedi",
        "U yo'q, kelmaydi.",
        "u yoq dedi, keyin u yo'q dedi",
        "u o'q idi, endi kitob",
        "u o'qiydi, men bormayman",
        "U olma yedi",
        # "uy" (uy) so'zi bilan mos tushmasligi kerak
        "Uyaga chiqma, u buzilgan",
        "uyaga kir",
        "men uyerda emasman",
        "uya tushdim",
        # "yaqqol", "yaqin", "yaqinda" — oddiy so'zlar
        "u yaqqol yozilgan",
        "u yaqin atroda",
        "u yaqinda keladi",
        # "u" + tinish belgisi / bo'shjoy variantlari
        "menda u qaerda?",
        "u, albatta",
        "ular u yerga bordi",
        "u yuqoridagi fayl",
        "u allaqachon ketgan",
        "u albumni ko'rdingmi",
        # ismlar va so'z ichidagi moslar
        "Erlan klassda o'qiydi",
        "Sizga qanday yordam bera olaman?",
        "class and classes sinf",
        "assalomu alaykum, qalaysiz",
        "bugun havo yaxshi",
        "the weather is great, you?",
        "can you assist me? assemble the class",
        "I don't know, maybe",
        # kirillcha oddiy matn ("план/класс" lookbehind bilan himoyalangan)
        "У менда планета деб ойкилди",
        "у классда ўқийди",
        "мен мактабга бордим",
        "у китоб окувди",
    ]

    def test_ok_phrases(self):
        for text in self.OK:
            with self.subTest(text=text):
                hit, term = core.contains_profanity(text)
                self.assertFalse(hit, f"'{text}' noto'g'ri blokindi -> {term!r}")


class TestProfanityStillCaught(unittest.TestCase):
    """Haqiqiy so'kinishlar topilishi kerak."""

    BAD = [
        # o'zbekcha lotin
        "uyaqqi", "uyaqqini", "xuyaqqi", "uwaq", "uvaq", "uya'q",
        "blyat", "blyaaaa", "bljad",
        "lan", "sening lan", "lakki", "lakk", "buzur", "yelat", "pishi",
        "sukka",
        # o'zbekcha kirill
        "уяқ", "уяқни", "увақ", "увояк", "пезд", "ланга ўхшайди", "лакк", "сука", "сучка",
        # ruscha
        "блять", "пиздец", "мудак", "говно", "ублюдок", "шлюха", "мразь",
        # inglizcha
        "fuck you", "motherfucker", "shit bro", "bitch", "wtf", "you asshole", "cunt",
        # leet / aralashtirilgan yozuv
        "FUCK", "SHIT", "sh1t", "0uyaqqi", "b1tch",
    ]

    def test_bad_phrases(self):
        for text in self.BAD:
            with self.subTest(text=text):
                hit, term = core.contains_profanity(text)
                self.assertTrue(hit, f"'{text}' topilmadi")
                self.assertTrue(term, "topilgan so'z bo'sh")

    def test_empty_and_none(self):
        self.assertEqual(core.contains_profanity(""), (False, None))
        self.assertEqual(core.contains_profanity(None), (False, None))

    def test_names_are_not_stems(self):
        # comment'dagi asosiy kafolat: nomlar va so'z ichidagi moslar bloklanmaydi
        for name in ["Erlan", "Erlanni", "Lankast", "classes", "assassin", "superlan"]:
            with self.subTest(name=name):
                self.assertFalse(core.contains_profanity(name)[0], name)

    def test_admin_config_defaults(self):
        self.assertEqual(core.PROFANITY_MODE, "block")
        self.assertEqual(core.PROFANITY_BLOCK_AFTER, 2)
        self.assertEqual(core.PROFANITY_BAN_HOURS, 0)  # 0 = doimiy blok


if __name__ == "__main__":
    unittest.main(verbosity=2)
