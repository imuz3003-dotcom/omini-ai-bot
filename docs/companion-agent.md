# Qurilma agentini ishga tushirish (companion)

Bot Render'da ishlaydi, sizning kompyuter/telefoningiz esa NAT ostida — shuning
uchun ulanishni doim **agent** boshlaydi. Agent ishlaguniga qadar Telegram'dagi
qurilma buyruqlari (`/ls`, `/cat`, …) javobsiz qoladi.

Dizayn va xavfsizlik modeli: `docs/superpowers/specs/2026-11-13-companion-agent-design.md`.

## 0. Bir marta: Render'da `DEVICE_SECRET`

Bu kalit qurilmaning sizga tegishli ekanini imzolaydi. Bo'sh qolsa bot baribir
ishlaydi, lekin **har restart'dan keyin barcha qurilmalar qayta `/pair` yozishi
kerak bo'ladi** (bot logida `DEVICE_SECRET o'rnatilmagan` ogohlantirishi chiqadi).

1. Mahalliyda bitta tasodifiy kalit yasang va uni hech qaerga yozmang:

   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Render dashboard → `omini-ai-bot` → **Settings → Environment → Edit** →
   yangi qator: `DEVICE_SECRET` = yuqoridagi qiymat → **Save, rebuild, and deploy**.

`render.yaml`da bu kalit `sync: false` bilan turadi — ya'ni blueprint uni
dashboard'dagi qiymat bilan almashtirmaydi, repositoriyda ham saqlanmaydi.

## 1. Windows

Python 3.11+ kerak (python.org, o'rnatishda **Add python.exe to PATH**).

```
pip install aiohttp
git clone https://github.com/imuz3003-dotcom/omini-ai-bot.git
```

Repozitoriydagi `start_agent.bat` faylini ochib, birinchi ishga tushirishda ikki
qatorni o'zingizga moslang:

```bat
set "BOT_URL=https://omini-ai-bot.onrender.com"
set "ROOT=%USERPROFILE%"
```

`ROOT` — agentga ruxsat etilgan katalog (ichida istalgan papkaga kirish mumkin,
tashqarisiga yo'q). `.bat` faylni ikki marta bosing; konsol shuni ko'rsatadi:

```
>> Telegram'da /pair 483920 yozing (10 daqiqa ichida).
>> LAPTOP-XXXX botga ulandi.
```

Telegram'da `/pair 483920` yozing → `✅ … qurilmangiz bo'lib ulandi`. Shundan
keyin `agent.json`da imzo saqlangan bo'ladi: deraza yopilib-qayta ochilsa ham
`/pair` shart emas. Konsol derazasi yopilsa agent to'xtaydi.

- Config: `%APPDATA%\omini-agent\agent.json`
- Butun sozlamani yangilash (yangi `agent_id` va yangi kod):
  `python companion\agent.py --pair --url … --root …`. Eski qurilma bot ro'yxatida
  qoladi — `/unpair <nom>` bilan o'chirib tashlang.

## 2. Android (Termux)

Quyidagi qadamlar Termux'da sinab ko'rilmagan — birinchi telefon ulanishida
xato bo'lsa, konsoldagi matnni to'liq ko'chirib yuboring.

1. Termux'ni **F-Droid**'dan oling (Play Market'dagi versiya endi yangilanmaydi).
2. `pkg update && pkg install python git`
3. `pip install aiohttp` — qurilish (build) xatosi chiqsa `pkg install clang`
   qo'shib qayta urinib ko'ring.
4. ```
   git clone https://github.com/imuz3003-dotcom/omini-ai-bot.git
   cd omini-ai-bot
   termux-setup-storage        # /sdcard'ga ruxsat (bir marta)
   python companion/agent.py --url https://omini-ai-bot.onrender.com --root /sdcard
   ```
5. Ko'rsatilgan kod bilan Telegram'da `/pair yozing`.

Telefon ekrani o'chsa yoki Doze rejimga o'tsa ulanish uziladi — agent 1→30 s
backoff bilan qayta uriladi, lekin uzilgan paytda `/ls` `📴 onlayn emas` deb
javob beradi. Uzoqroq turish uchun `termux-wake-lock` va batareya
optimizatsiyasini o'chirish foyda beradi.

## 3. Tekshirib ko'rish

Telegram'da: `/device` (qaysi qurilma tanlangan), `/ls`, `/cd Documents`,
`/cat hisobot.txt`, `/tree`, `/stat fayl.txt`, `/qidir *.log`, `/get fayl.txt`,
`/mk yangi_papka`, `/write eslatma.txt` (ikkinchi qatordan yozgan matn faylga
tushadi), `/cp` / `/mv`, `/rm fayl.txt`, `/put` — hujjatni xabar bilan birga
joylab, caption'iga `/put manzil/fayl.ext` yozing.

Uzun faylning davomi botning o'zi ko'rsatgan buyruqda: `/cat fayl.txt 3798`.

## 4. Buyruq yozmasdan, oddiy gap bilan

Qurilma ulangan foydalanuvchida bot oddiy suhbatda ham fayllar bilan ishlaydi —
`/` buyruqlarini yodash shart emas:

```
menga Desktop'da player papka yaratib ber
→ Bot: ✅ Desktop/player yaratildi
```

Bot bu yerda Gemini'ning function calling'idan foydalanadi va `ls cd cat find tree
stat mkdir write mv cp rm` vositalarining birini (yoki bir necha tasini ketma-ket)
chaqiradi. Xavfli bo'lmaganlar tasdiq so'ramaydi. Chegaralar:

- `/get` va `/put` faylni Telegram'ga uzatadi — bu alohida ish, shuning uchun AI
  ularni o'zi chaqirmaydi. Katta fayl kerak bo'lsa bot ` /get` ni maslahat qiladi.
- `rm` orqali ham **faqat bitta fayl** o'chadi — katalog yoki `*.tmp` kabi pattern
  AI tomonidan so'ralsa ham agent rad etadi.
- Guruh chat'ida AI qurilmaga tegmaydi (faqat shaxsiy suhbat).
- Agent onlayn bo'lmasa bot qisqacha `📴` bilan javob beradi, javobni uydurmaydi.
- AI 4 qadamdan ortiq amal so'rasa to'xtaydi — cheksiz sikl bo'lmaydi.

## 5. Xatolar nimani anglatadi

| Ko'rinadi | Sabab | Nima qilish |
|---|---|---|
| `!! Bot: Bu qurilma ro'yxatda yo'q` (agent konsolida) | qurilma hali `/pair` yozmagan yoki `/unpair` qilingan | agent yangi kod ko'rsatadi — o'shani yozing |
| `⛔ Imzo tekshiruvidan o'tmadi` | `DEVICE_SECRET` almashtirilgan | qurilmalarni qayta `/pair` yozing |
| `📴 "…" hozir onlayn emas` | agent o'chgan yoki interneti yo'q | agentni ishga tushiring |
| `⏱ "…" 10 soniyada javob bermadi` | juda katta katalog yoki sekin tarmoq | `/cd` bilan torroq joyga kiring (`/cp`, `/get` kabi sekin amallar uchun chegara 60 s) |
| `Kod noto'g'ri yoki muddati o'tgan` | 10 daqiqa o'tdi / xato raqam | agent kodni qayta ko'rsatadi; 5 marta xato qilsangiz 10 daqiqa kuting |
| `⛔ ruxsat yo'q: … ildizdan tashqarida` | `ROOT` tashqarisiga chiqdingiz | `/cd` bilan ildiz ichida qoling yoki `--root` ni kengaytiring |
| `Python topilmadi` (`.bat` derazasida) | PATH'da yo'q | Python'ni qayta o'rnatib, "Add python.exe to PATH" belgisini qo'ying |

## 6. Nima ataylab qilinmaydi

- Shell yo'q: agent bitta ham tizim buyrug'ini ishga tushirmaydi (`/run`,
  `/kill`, `/shutdown` kabi buyruqlar umuman mavjud emas).
- Katalog o'chirilmaydi: `/rm` faqat **bitta fayl**ni oladi, glob/katalog rad etiladi.
- `~/.ssh`, `~/.aws`, brauzer profili va `NTUSER.DAT` deny ro'yxatida — o'qilmaydi ham.
- 8 MB'dan katta fayl uzatilmaydi (`/get` va `/put`), `/cat` bir xabarda 3800
  belgi beradi (davomi offset bilan).
- Guruh chat'ida qurilma buyruqlari ishlamaydi: faqat shaxsiy suhbat.

## 7. Xavfsizlik eslatmasi

Telegram akkauntingiz = kompyuteringizning kaliti. Akkaunt o'g'irlansa hujjatlaringiz
o'qiladi va bitta fayl o'chirilishi mumkin. Shuning uchun: `DEVICE_SECRET`ni
hech qaerga (`.env`, chat, screenshot) yozmang. Biror qurilmani ishonchsiz deb
bilsangiz — `/unpair` bilan o'chiring. Barcha qurilmani birdaniga yaroqsiz qilish
uchun dashboard'da `DEVICE_SECRET` ni almashtiring: agentlar rad etilib yangi kod
so'raydi, siz ularni qayta `/pair` yozmaguningizcha hech biri ishlamaydi.
