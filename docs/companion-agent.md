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

## 1. Windows — `omini-agent.exe` (Python talab qilmaydi)

Exe — bitta fayl, o'rnatish va terminal shart emas. Uni ikki marta bosing:

1. `dist\omini-agent.exe` ni istalgan joyga ko'chiring (masalan Ish stoli) va ikki
   marta bosing.
2. Ochilgan konsol shuni ko'rsatadi:

   ```
   >> Telegram'da /pair 483920 yozing (10 daqiqa ichida).
   >> Yoki shu havolani bosing: https://t.me/omniai_app_bot?text=%2Fpair%20483920
   >> LAPTOP-XXXX botga ulandi.
   ```

3. Havolani bosing → Telegram'da `/pair 483920` yozilgan xabar tayyor turadi →
   faqat **Send** bosing → `✅ … qurilmangiz bo'lib ulandi`.

Birinchi ishga tushirishda hech qanday parametr kerak emas: bot manzili
`https://omini-ai-bot.onrender.com`, ildiz esa `%USERPROFILE%` (Desktop,
Documents, Downloads hammasi ko'rinadi) o'zi tanlanadi. Sozlama
`%APPDATA%\omini-agent\agent.json`da saqlanadi — shu fayl o'chirilsa hammasi
qaytadan so'raladi.

Exe git repositoriyasida yo'q (33 MB). Yangilash kerak bo'lsa (kod o'zgarsa) —
Python'li kompyuterda bir marta:

```
powershell -ExecutionPolicy Bypass -File companion\build_exe.ps1
```

- Brauzer avtomatik ochilmasin desangiz: `omini-agent.exe --no-open` (havola
  baribir konsolda ko'rinadi). Boshqa botga ulanmoqchi bo'lsangiz:
  `--url https://… --root D:\ish`.
- Antivirus PyInstaller fayllariga shubha bilan qarashi mumkin: ishga tushmasa,
  o'sha papkani istisnolarga qo'shing (kod ochiq — `companion/agent.py`).
- Deraza yopilsa agent to'xtaydi. Orqaga qaytarish uchun konsolni yopish yetadi.

## 1b. Windows — Python orqali (`start_agent.bat`)

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
keyin `agent.json`da imzo saqlangan bo'ladi: deraza yopilib-qayta ochilsa ham,
bot Render'da qayta ishga tushsa (deploy) ham `/pair` shart emas — agent o'z
imzosi bilan jimgina qayta qabul qilinadi. Konsol derazasi yopilsa agent to'xtaydi.

- Config: `%APPDATA%\omini-agent\agent.json`
- Butun sozlamani yangilash (yangi `agent_id` va yangi kod):
  `python companion\agent.py --pair --url … --root …`. Eski qurilma bot ro'yxatida
  qoladi va nomi yangisi bilan bir xil bo'ladi — `/device` ro'yxatidagi qisqa ID
  (`/unpair a3f9c1`) bilan o'chirib tashlang. Nomi takrorlangan qurilmaga oddiy
  `/unpair <nom>` buyruq bermaydi: bot xato qaysi biri ekanligini biladi.

## 1c. Windows — kompyuter yoqilganda o'zi boshlansin

Agent ishlamasa AI qurilma vositalarini umuman ko'rmaydi va "men fayllar bilan
ishlay olmayman" deb javob beradi (bu kod xatosi emas). Shuning uchun `.exe`ni
Startup'ga qisqa yo'lama bilan qo'ying — admin huquqi talab qilinmaydi:

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'omini-agent.lnk'))
$lnk.TargetPath = 'C:\Users\User\Desktop\omini-agent.exe'
$lnk.WorkingDirectory = 'C:\Users\User\Desktop'
$lnk.WindowStyle = 7   # konsol derazasi minimizatsiyada ochilsin
$lnk.Save()
```

- Faqat **shu foydalanuvchi sessiyasi** boshlanganda ishlaydi (qulflangan ekranda
  emas), ya'ni hisobga kirgach agent avtomatik ulanadi.
- O'chirish: `Win+R` → `shell:startup` → `omini-agent.lnk` ni o'chirish.
- `.exe` boshqa papkaga ko'chsa qisqa yo'lanani yangilang (`TargetPath`).

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
- AI 8 qadamdan ko'p amal so'rasa, qolgan chaqiruvlar **bajarilmaydi** (ularga
  «chegara» javobi yoziladi) va modeldan xulosa so'raladi: foydalanuvchi `⏹️ …
  «davom et» deb yozing` xabarini oladi, javobsiz `function_call` tarixni buzmaydi.

## 5. Xatolar nimani anglatadi

| Ko'rinadi | Sabab | Nima qilish |
|---|---|---|
| `!! Bot: Bu qurilma ro'yxatda yo'q` (agent konsolida) | qurilma hali `/pair` yozmagan yoki `/unpair` qilingan | agent yangi kod ko'rsatadi — o'shani yozing |
| `⛔ Imzo tekshiruvidan o'tmadi` | `DEVICE_SECRET` almashtirilgan | qurilmalarni qayta `/pair` yozing |
| Konsolda hech narsa chiqmaydi, lekin `/ls` `📴 onlayn emas` deb javob beradi | Render proksi rad etish kodini (~21 s kechikib) yetkazmaydi — agent jimgina qayta-qayta ulanib uziladi | 3 qisqa ulanishdan keyin agent imzosini o'zi tashlab, `>> Telegram'da /pair …` kodini ko'rsatadi; shuni yozing |
| `📴 "…" hozir onlayn emas` | agent o'chgan yoki interneti yo'q | agentni ishga tushiring |
| `⏱ "…" 10 soniyada javob bermadi` | juda katta katalog yoki sekin tarmoq | `/cd` bilan torroq joyga kiring (`/cp`, `/get` kabi sekin amallar uchun chegara 60 s) |
| `⏹️ 8 qadam bajarildi. «davom et» deb yozing` | so'rov juda ko'p qadamli ish (masalan butun o'yin loyihasi) | `davom et` deb yozing — qolgan qismini yozadi |
| `Kod noto'g'ri yoki muddati o'tgan` | 10 daqiqa o'tdi / xato raqam | agent kodni qayta ko'rsatadi; 5 marta xato qilsangiz 10 daqiqa kuting |
| `⛔ ruxsat yo'q: … ildizdan tashqarida` | `ROOT` tashqarisiga chiqdingiz | `/cd` bilan ildiz ichida qoling yoki `--root` ni kengaytiring |
| `«nom» nomida 2 qurilma bor` | agent `--pair` bilan qayta ishga tushganda eski yozuv qoladi | `/device` dagi qisqa ID bilan yozing: `/unpair a3f9c1` (eski, ⚪ oflayn qatorni o'chiradi) |
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

`/unpair` haqidagi ochig'i: imzo holat bilan emas, `DEVICE_SECRET` bilan
tekshiriladi (chunki Render Free har restart'da ro'yxatni o'chiradi). Shuning
uchun `/unpair` bloki faqat **ishlayotgan protsessda** yashaydi — bot qayta ishga
tushsa, o'sha qurilma o'z imzosi bilan yana qabul qilinadi. Agar biror qurilmani
butunlay yaroqsiz qilish kerak bo'lsa, `DEVICE_SECRET` ni almashtiring (bu barcha
imzolarni bir yoqdan o'chiradi) va qurilmadagi `agent.json` ni o'chiring.
