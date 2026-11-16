# omini AI — Qurilma boshqaruv agenti (companion) dizayni

Sana: 2026-11-13 · Holat: 1-2-bosqichlar yozildi (`companion/ops.py`, `protocol.py`,
`agent.py`), 3-4-bosqichlar navbatda

## 1. Maqsad

Telegram bot foydalanuvchining o'z qurilmasiga (Windows kompyuter yoki Android/Termux)
ulanib, fayllarni ko'rish, yaratish, ko'chirish va o'qish imkonini beradi. Xato yuz bersa
bot sababini o'zbek tilida aniq yozadi — javobni "kechirasiz" bilan almashtirmaydi.

## 2. Qamrov va chegaralar

**Bor:** `/ls /cd /cat /find /tree /disk /stat /mk /write /mv /cp /rm /get /put`,
`/pair /devices /unpair /device`.

**Yo'q (kodda ham bo'lmaydi):** shell buyrug'ini ijro etish, jarayonni o'ldirish,
o'chirib qo'yish/yuklash, katalog yoki glob o'chirish, screenshot, klaviatura
simulyatsiyasi, bir nechta faylni bir buyruqda o'chirish.

**Nima uchun yo'q:** foydalanuvchi "xavfli amallar umuman bo'lmasin" deb tanladi.
Telegram akkaunti o'g'irlansa ham zarar fayl yo'qotish bilan cheklanadi.

## 3. Arxitektura

Ulanishni **har doim qurilma** boshlaydi. Bot Render'da `web` servis
([bot.py:1871-1878](../../../bot.py) — `start_web_server`), qurilma esa NAT ostida,
shuning uchun bot qurilmaga kira olmaydi. Qurilma botga WebSocket ochadi.

```
Telegram ──▶ aiogram handler (bot.py)
                 │ run_command(user_id, op, args)
                 ▼
            devicehub.py ── WS kadr ──▶ companion/agent.py
                 ▲                          │ ops.fn(**args)
                 └──── natija kadri ────────┘   companion/ops.py
```

Tanlangan yo'lning alternaturalari: HTTP long-poll (reconnect oddiyroq, lekin holat
saqlash qiyin) va lokal bot instansiyasi (Telegramda bitta token = bitta polling,
Render bilan 409 konflikt — rad etildi).

### Komponentlar

| Fayl | Vazifasi | Tarmoq |
|---|---|---|
| `companion/protocol.py` | Xabr konvertsiyasi, `PROTOCOL_VERSION`, timeout konstantalari | yo'q (ikkala tomon import qiladi) |
| `companion/ops.py` | Fayl amallari + yo'l qorovuli. Faqat `pathlib`/`shutil`/`os` | yo'q |
| `companion/agent.py` | `agent.json`, pairing, WS ulanish, uzilsa qayta ulanish, `ops`ni chaqirish | bor |
| `devicehub.py` | `agent_id → WebSocket` ro'yxati, bindinglar, `run_command()` + `Future` + timeout | bor |
| `bot.py` | Buyruq handlerlari, `/device/ws` va `/device/register` marshrutlari | bor |

`ops.py` va `protocol.py` tarmoqsiz — xavfsizlik yadrosi shu fayllarda va ular
yolg'iz sinovdan o'tadi.

## 4. Protokol

Bitta JSON kadr, `id` — agentga kelgan har bir buyruq uchun noyob (botgeneratsiyali
`secrets.token_hex(6)`).

```jsonc
// bot ──▶ qurilma
{"v": 1, "type": "cmd", "id": "9f2c1a", "op": "ls", "args": {"path": "Documents"}}
// qurilma ──▶ bot
{"v": 1, "type": "res", "id": "9f2c1a", "ok": true,  "data": {"entries": [...]}}
{"v": 1, "type": "res", "id": "9f2c1a", "ok": false, "error": "FileNotFoundError: ..."}
```

Boshqa kadrlar: `hello` (qurilma `agent_id`, `bind`, `owner_user_id`ni yuboradi),
`bound` (bot yangi `bind`ni uzatadi), `err` (kanal darajasidagi rad etish),
`ping`/`pong` (25 s interval — Render bo'sh kanalni o'ldirmasin).

Kanal ochilganda qurilma birinchi kadr sifatida `hello` yuboradi:
`{"type":"hello","agent_id":…,"bind":…,"owner_user_id":…,"name":…}`. `bind` bo'lmasa
kanal `pending` holatida qoladi (buyruq olmaydi), noto'g'ri `bind` bo'lsa bot kanalni
`4401` bilan yopadi.

`v` mos kelmasa ikkala tomon ham kadarni tushirib yuborib, log yozadi.

`/get` va `/put` fayl mazmunini `b64` maydonida (base64, `encoding: "base64"`)
oladi — shunda bitta JSON kadr yetadi va alohida oqim mexanizmi kerak bo'lmaydi.

## 5. Pairing va xavfsizlik modeli

**Kalit printsip: botda doimiy holat saqlanmaydi.** Render Free har restart'da
SQLite'ni tozalaydi, shuning uchun egalik huquqi bazada emas, imzoda yashaydi:

```
bind = HMAC_SHA256(DEVICE_SECRET, f"{agent_id}:{owner_user_id}").hexdigest()
```

`DEVICE_SECRET` — Render dashboard'dagi env kaliti (repositoriyaga tushmaydi).
Qurilma `agent.json`da `agent_id`, `owner_user_id`, `bind` saqlaydi va ulanishda
uchalasini yuboradi; bot `bind`ni qayta hisoblab solishtiradi. Shuning uchun
deploy/restart'dan keyin hech kim qayta `/pair` yozmaydi.

### Pairing oqimi

1. Agentda `bind` yo'q → `agent_id = secrets.token_hex(16)` yaratiladi,
   WS `?agent_id=…` bilan ochiladi (bind'siz). **Kutilayotgan** kanal buyruq ola
   olmaydi, faqat `bound` kadrasini kutadi va 10 daqiqadan keyin yopiladi.
2. `GET /device/register?agent_id=…&name=…&root=…` javobi `{"code": "483920"}`.
   Bot `code → agent_id` ni 10 daqiqali xotirada (in-memory) ushlaydi.
3. Agent konsolga yozadi: `Telegram'da /pair 483920 yozing`.
4. Foydalanuvchi `/pair 483920` → bot `owner_user_id = from_user.id` bilan `bind`
   hisoblaydi, kutilayotkan kanalga `{"type":"bound","bind":…}` yuboradi, kodni
   o'chiradi. Agent `agent.json`ga yozadi va javoban `✅ "desk-pc" ulandi`.
   Agar shu paytda kutilayotgan kanal bo'lmasa (agent ulanishni yo'qotgan),
   bot baribir `✅` yuboradi, lekin tagiga `📴 … onlayn emas` ogohlantirishini
   qo'shadi — soxta muvaffaqiyat ko'rsatmaydi.

### Binding bekor qilinishi (`/unpair`)

`/unpair` qurilma yozuvini o'chiradi va kanalni `4403` bilan yopadi; eski `bind`
endi `4401` («ro'yxatda yo'q») bilan rad etiladi. Agent kanal yopilish kodini
`on_closed()`da ko'radi (`4401`/`4403`) — `agent.json`dan `bind` va
`owner_user_id`ni o'chirib, keyingi ulanishda yangi kod so'raydi. Shuning uchun
foydalanuvchi agentni o'chirib-yoqmasdan `/unpair` → `/pair` qilishi mumkin;
e2e tekshiruvda shu zanjir to'liq ko'rildi.

### Buyruq paytidagi tekshiruv

`HUB.run(user_id, op, timeout=None, **args)` faqat `owner_user_id == user_id`
bo'lgan qurilmalarni ko'radi. `agent_id`ni bilish yetarli emas: uni yuborish uchun
`DEVICE_SECRET` kerak, u esa faqat Render'da.

### Yo'l qorovuli (agentning o'zida, eng oxirgi to'siq)

Ruxsat etilgan ildizlar `agent.json:roots`da (Windows: `%USERPROFILE%`,
Termux: `/sdcard`). Har bir yo'l uchun ketma-ket tekshiruv:

1. `~` va `%USERPROFILE%` kengaytiriladi, nisbiy yo'l joriy katalog (`/cd` holati) bilan yig'iladi.
2. `Path.resolve(strict=False)` — nuqtalar, `..` va qo'sh bo'linishlar yechiladi.
3. `resolved.is_relative_to(root)` — ildizdan tashqariga chiqsa `⛔ ruxsat yo'q`.
4. Har bir yo'l bo'lagi `os.path.islink()` — `symlink`/reparse orqali chiqish rad etiladi.
5. `deny` glob ro'yxati (standart: `**/.ssh/**`, `**/.aws/**`, `**/User Data/**`, `**/NTUSER.DAT`) — mos kelsa `⛔`.
6. `/rm` uchun qo'shimcha: obyekt fayl bo'lishi shart (`is_file()`), katalog bo'lsa rad.

### Limitlar

Har kanalga bir vaqtda bitta buyruq; tez amallar (`ls /cat /stat /find /tree /disk`)
10 s, sekin amallar (`cp /mv /get /put /write`) 60 s ichida tugashi kerak; `/ls` 60 joy;
`/cat` 3800 belgi (offset bilan davom ettiriladi); `/find` 200 natija; `/get` 8 MB
(fayl base64 ko'rinishida bitta JSON kadrda uzatiladi va kadr ~1.37 barobar
kengayadi — Telegramning 50 MB chegarasidan ancha past); foydalanuvchiga 5 qurilma;
`/pair` da 10 daqiqada 5 marta xato kod urinishi.

## 6. Buyruqlar

| Buyruq | Agentdagi op | Izoh |
|---|---|---|
| `/ls [yo'l]` | `ls` | nom, hajm, sana; `/`da tugaydigan yo'llar katalog |
| `/cd [yo'l]` | — | foydalanuvchi+qurilma uchun joriy katalog (bot xotirasida) |
| `/cat <yo'l> [offset]` | `cat` | matn; binar bo'lsa "binar fayl, `/get` biling" |
| `/qidir <nom> [yo'l]` | `find` | glob bo'yicha, 200 natijagacha (`/find` aloqa qidiruviga band) |
| `/tree [yo'l]` | `tree` | 3 chuqurlik, 200 joy |
| `/disk` | `disk` | ildiz hajmi/bo'sh joyi |
| `/stat <yo'l>` | `stat` | hajm, sana,/rejim, havola |
| `/mk <yo'l>` | `mkdir` | katalog(lar) yaratish |
| `/write <yo'l>` + matn | `write` | xabar matnini faylga yozadi (ustidan yozadi) |
| `/mv <manba> <maqsad>` | `mv` | ildizlar ichida |
| `/cp <manba> <maqsad>` | `cp` | ildizlar ichida |
| `/rm <fayl>` | `rm` | **faqat bitta fayl** |
| `/get <yo'l>` | `get` | faylni Telegramga yuboradi |
| `/put <yo'l>` (joylangan hujjat bilan) | `put` | Telegram hujjatini saqlaydi |
| `/pair <kod>` | — | qurilmani o'ziga bog'laydi |
| `/device` | — | qurilmalar ro'yxati + qaysi biri tanlangan (`/devices` alohira buyruq qilinmadi) |
| `/device <nom\|id>` | — | boshqa qurilmani tanlash (1 ta bo'lsa avtomatik tanlanadi) |
| `/unpair <nom\|id>` | — | bot ro'yxatidan olib tashlaydi, agentga `reset` buyuradi |

Barcha qurilma buyruqlari **faqat shaxsiy chat'da** ishlaydi (`device_gate`):
guruhda `/ls` yozilsa bot qurilmaga biror kadr yubormaydi.

`/put` filtri `F.document` + `F.caption.regexp("^/put")` — Telegram captionni
xabarning o'ziga (`message.caption`) qo'yadi, `message.document.caption`da emas.

### Kadr o'lchami

`/get` va `/put` faylni base64 bilan bitta kadrga joylaydi (8 MB fayl ≈ 11 MB kadr),
shuning uchun `protocol.MAX_FRAME_BYTES = 16 MiB` ikkala tomonda ham aiohttpga
beriladi: agentda `ws_connect(max_msg_size=…)`, botda `WebSocketResponse(max_msg_size=…)`.
Standart 4 MB qiymat qolganda katta fayl sessiyani jimgina yo'q qilardi.

## 7. Xatolar

Agent har bir amalni `except Exception` bilan tutadi va `{"ok": false, "error":
"<Tur>: <xabar>"}` qaytaradi — matn o'zgartirilmaydi. Bot tomonida:

- `error`dagi `PermissionError` → `⛔ Ruxsat yo'q (Windows sizga yozishni taqiqladi): <matn>`
- `FileNotFoundError` → `❌ Bunday fayl yo'q: <yo'l>`
- `⛔ ruxsat yo'q` (sandbox) → aynan shu so'z bilan, qaysi ildiz ruxsat etilgani ko'rsatiladi
- kanal ochiq emas → `📴 "desk-pc" hozir onlayn emas. Agent ishlayaptimi tekshiring.`
- 10 s o'tdi → `⏱ "desk-pc" javob bermadi (10s). Katta katalog bo'lishi ehtimoli bor.`

`/stop` bilan bekor qilinsa yoki kanal uzilsa, kutilayotgan `Future` tozalanadi —
sizib qolgan buyruq keyinroq o'z-o'zidan javob bermaydi.

### Qayta-ulinish tartibi (agent)

Bot ro'yxatda yo'q yoki imzosi noto'g'ri qurilma kanalini birinchi sekuntda yopadi.
Agar agent sabrsiz qayta urilsa, bitta Render Free instansiyani zarba qilardi —
e2e tekshiruvda 4 sekundda 83 ulanish paydo bo'ldi. Shuning uchun `serve()`
sessiya davomiyligini o'lchaydi: `STABLE_SESSION_SECONDS` (60 s)dan qisqa sessiyalar
`backoff` 1→2→4→…→30 s ko'payadi, uzun (ya'ni ishlagan) sessiyadan keyin hisob
nolga qaytadi va 1 s oraliq bilan davom etiladi. Agent kanal yopilish sababini
ham yashirmaydi: `err` kadri konsolga `!! Bot: …` bo'lib chiqadi.

## 8. Sozlash

`agent.json` (Windows: `%APPDATA%\omini-agent\agent.json`, Termux: `~/.omini/agent.json`):

```json
{"bot_url": "https://omini-ai-bot.onrender.com", "agent_id": "…hex…",
 "owner_user_id": 8692018182, "bind": "…hex…", "name": "desk-pc",
 "roots": ["C:/Users/User"], "deny": ["**/.ssh/**"]}
```

`DEVICE_SECRET` Render env'iga qo'shiladi (`render.yaml`ga qiymati yozilmaydi,
`sync: false`). Birinchi ishga tushirishda `bot_url` so'raladi yoki
`OMINI_BOT_URL` muhit o'zgaruvchisidan olinadi.

## 9. Testlar

- `companion/test_ops.py` — `tmp_path`da ildiz yasab: `/etc` yo'li, `..` qochish,
  `symlink` qochish, `.ssh` o'qish, `/rm` katalogga urinish → hammasi `⛔`;
  `/ls /cat /mk /mv /cp /get /put` → kutilgan natija. `ops.py` sof funksiya bo'lgani
  uchun TDD (RED → GREEN) shu yerda boshlanadi.
- `test_devicehub.py` (42 test) — kod bir martaligi va muddati, `bind` HMAC tekshiruvi,
  begona `user_id`ning begona qurilmaga kira olmasligi, timeout xabari,
  kanal yopilsa `Future` tozalanishi, 5 qurilma chegarasi, xato kod urinishlarini
  hisoblab foydalanuvchini vaqtincha bloklash.
- `test_commands.py` → `DeviceCommands` (9 test, `feed_update` + `FakeHub` orqali) —
  `/pair` dispatcher'dan o'tadi, kanal bo'lmasa `/pair` ogohlantiradi, `/ls` natijasi
  chiroyli chiqadi, agent xato matni
  foydalanuvchiga yetadi, guruh chat'i qurilmaga hech qachon tegmaydi, `/put`
  caption'i qurilmaga `b64` bo'lib boradi, chegaradan ortiq hujjat yuklashdan oldin
  rad etiladi, `/get` bazani dekodlangan hujjat qilib qaytaradi.
- `companion/test_agent.py` → `ServeRetry` — qayta-ulinish oraliqlari (1,2,4,8,16)
  va uzun sessiyadan keyin hisobning nolga qaytishi; `BindingRevoked` — `4401`/`4403`
  kodli yopilish `bind`ni o'chiradi, oddiy uzilish uni saqlaydi.
- Bajarish: `python -m unittest discover -s . -p "test_*.py"` — 209 test, hammasi
  tarmoqsiz va soatsiz (`skipped=1` — symlink testi Windows'da admin so'raydi).
- Bir martalik e2e tekshiruv (kommit qilinmaydi): haqiqiy `aiohttp` server + haqiqiy
  `agent.serve()` — pairing, `agent.json`ga `bind` yozilishi, `ls/cat/get/mkdir/cd`,
  ildizdan tashqariga chiqish urinishi (absolyut va nisbiy), begona foydalanuvchi,
  6 MB fayl `/get` (≈8 MB kadr), `/unpair`dan keyin agentning yangi kod so'rashi va
  qayta `/pair` bilan `/ls`. Hammasi o'tdi.

## 10. Bosqichlar (har biri alohida tekshiriladi)

1. ✅ `companion/ops.py` + `companion/test_ops.py` — sandbox va fayl amallari. Tarmoq,
   deploy va env talab qilmaydi. (`5bbfd62`)
2. ✅ `companion/protocol.py` + `companion/agent.py` — kadr formati, `handle_frame`,
   `pump`, config va `agent.json`ga `bind` saqlash. (`6fdbef0`)
3. ✅ `devicehub.py` + aiohttp marshrutlari + `bot.py` handlerlari — pairing kodlari,
   HMAC `bind`, `run()` + timeout, `/pair /device /unpair /cd /ls /cat /qidir /tree
   /disk /stat /mk /write /mv /cp /rm /get /put`.
4. ⏳ Ishga tushirish hujjati: `DEVICE_SECRET` qo'shish (Render env + `render.yaml`
   `sync: false`), `python companion/agent.py`, Windows uchun `start_agent.bat`,
   Termux uchun `pkg install python` ko'rsatmasi.

## 11. Qabul qilingan zaifliklar

- **Telegram akkaunti = kompyuter kaliti.** Akkaunt o'g'irlansa hujjatlar o'qiladi va
  bitta fayl o'chiriladi; shell bo'lgani uchun emas, zarar cheklangan.
- **Haqiqiy bekor qilish (revocation) yo'q.** `/unpair` faqat bog'lanishni uzadi;
  qurilmani abadiy bloklash uchun `DEVICE_SECRET` almashtiriladi — barcha agentlar
  `4401` bilan rad etilib `bind`ini o'zi tashlaydi, lekin ular yangi kod so'rab
  turaveradi: ega qayta `/pair` yozmagunicha hech biri ishlamaydi.
- **Sandbox — bu agent ichidagi siyosat**, OS darajasidagi jail emas. Agent sizning
  huquqingizda ishlaydi.
- **Kutilayotgan pairing kodlari va joriy katalog in-memory** — Render restart'ida
  unutiladi. Bog'lanish (`bind`) esa yuqoridagi HMAC tufayli saqlanib qoladi.
