# Doim online: pinger + Telegram webhook (2026-11-23)

## Muammo

Render Free instansiya ~20 daqiqa kiruvchi HTTP bo'lmasa o'ldiriladi. Bot
hozircha `getUpdates` long-polling bilan ishlaydi — u **chiquvchi** so'rov,
shuning uchun instance uxlaganda hech bir foydalanuvchiga javob ketmaydi.
Xabarlar Telegram serverida navbatda qoladi va uyg'ilganda bir dasta bo'lib
keladi: «birdan ishlamay qoldi, keyin o'zi qayta ketdi» degan shikoyatning
sababi shu.

## O'lchangan dalillar

| Vaqt | Sinov | Natija |
|---|---|---|
| 13:38 → 13:55 | 17 daqiqa kiruvchi so'rovsiz | tirik: `/health` 1.2 s, `uptime_s` 1543 uzluksiz |
| 13:56 → 14:16 | ~20 daqiqa jimlik | o'lgan: birinchi `/health` 20 s timeout, keyin `uptime_s: 7` |
| 14:17:59 → 14:19:09 | qayta tiklanish | `uptime_s` 30 → 100 monoton |
| 14:26:05 → 14:26:50 | push keyin deploy | `uptime` 516 → 43, `users` 9 → 0 (baza bo'shadi) |

Xulosa: chegara 17 va 20 daqiqa oralig'ida; deploy/restart SQLite'ni tozaydi.

## Qaror

1. **GitHub Actions pingeri** (`6cbea29`) — 5 daqiqada bir `/health`. Repo
   ochiq, shuning uchun bepul va kompyuterga bog'liq emas.
2. **Telegram webhook** (`webhook.py`) — har xabar kiruvchi HTTP bo'ladi,
   shuning uchun instance foydalanuvchining o'z xabari bilan uyg'onadi.
   Ikkalasining yig'indisi: pinger asosiy, webhook esa «biri kechiksa»
   sug'urtasi.

## Dizayn

- `webhook.Receiver(secret, feed, dedup_limit=512)` — bitta kirish nuqtasi:
  `X-Telegram-Bot-Api-Secret-Token` sarlavhasi `devicehub.binds_match` bilan
  solishtiriladi (mos kelmasa 403, `feed` chaqirilmaydi), JSON buzuq yoki
  `update_id` yo'q bo'lsa 400.
- **Nima uchun darhol 200:** Telegram 60 sekunddan keyin webhook'ni
  «ishlamayapti» deb o'chirib qo'yadi, AI javobi esa undan uzoq vaqt olishi
  mumkin. Shuning uchun update `asyncio.create_task` bilan orqaga tashlanadi,
  javob oldindan chiqadi; `feed` ichidagi istisno logga yoziladi, o'sib
  chiqmaydi.
- **Nima uchun dedup:** sovuq startda Telegram javobni olmagan hisoblab bir
  xabarni qayta yuboradi. Oxirgi 512 `update_id` halqada saqlanadi
  (xotira o'smaydi), takrorlar tashlanadi.
- **Transport tanlovi** `bot.start_updates()`da: `WEBHOOK_SECRET` bo'lsa
  `set_webhook(url, secret_token, drop_pending_updates=False)` va polling
  **boshlanmaydi**; bo'lmasa `delete_webhook()` + polling. Har boot'da
  `set_webhook` qayta chaqiriladi, shuning uchun Telegram uni o'chirib
  qo'ysa ham keyingi restart tiklaydi.
- **Webhook rejimida protsessni kimdir tirik ushlab turishi shart.** Birinchi
  jonli deploy (`9c7c5cd`) bu tufayli o'ldi: `start_polling` chaqirilmagani
  uchun `main()` tugadi, protsess chiqib ketdi, Render `Application exited
  early` + `No open ports detected` bilan uni qayta-qayta ko'tardi (bot ~13
  daqiqa hech kimga javob bermadi). Endi webhook shoxida `asyncio.Event().wait()`
  turadi (`47616bd`).
- **Orqaga qaytish:** dashboard'dan `WEBHOOK_SECRET`'ni o'chirish — kod
  push qilmasdan polling rejimiga qaytadi.

## Cheklovlar (qabul qilingan narx)

- Instance uxlab qolsa birinchi javob 30–90 s kechikadi (sovuq start +
  Telegram qayta urinishi), lekin yo'qolmaydi.
- Qurilma agenti (exe) WS'i uxlash paytida uziladi; agent 30 s ichida o'zi
  qayta ulanadi.
- Har deploy/restart SQLite bo'shaydi (banlar, kontaktlar, suhbat tarixi).

## Sinovlar

`test_webhook.py` (9 test) — `aiohttp.test_utils` bilan **haqiqiy HTTP**
so'rovi orqali (soxta `request` obyekt header/JSON parse xatolarini
yashirardi): to'g'ri/noto'g'ri/yo'q secret, takroriy `update_id`, javob ish
tugamagacha chiqishi, buzuq JSON, `update_id` yo'qligi, `feed` xatosi,
dedup halqasi eskisini unutishi.

`test_commands.Transport` (3 test) — secret bilan webhook va polling
chaqirilmaydi; secretsiz `delete_webhook` + polling; webhook orqali kelgan
`/help` polling orqali kelgani bilan **bir xil** javob beradi.

Mutatsiyalar (uchchalari qayta tiklandi): secret tekshiruvi `if False` →
2 test yiqildi; dedup `if True` → 1 test; `if secret:` → `if True:` → 1 test.

Bajarish: `python -m unittest discover -s . -p "test_*.py"` — **279 test**.

## Jonli tekshiruv tartibi

1. `WEBHOOK_SECRET` generatsiya qilinadi, foydalanuvchi clipboard orqali
   Render dashboard → Environment'ga o'zi yapishtiradi (agent hech qachon
   maxfiy qiymat kiritmaydi).
2. Render qayta tushganda `getWebhookInfo` so'raladi: `url` to'liq va
   `pending_update_count` 0 bo'lishi kerak. `getUpdates` qo'lda so'rolmaydi
   — u tirik foydalanuvchi xabarini o'g'irlaydi.
3. Ikkinchi akkauntdan xabar yoziladi, Logs'da `Webhook rejimi:` qatori va
   javobning kelishi ko'riladi.
