# TibDaftari Backend

Davlat tibbiyot tarmog‘i uchun platformaning server qismi: tashkiliy daraxt (Davlat → Viloyat → Tuman/Shahar →
Muassasa(lar)), qabul (cheklar, to‘lovlar), laboratoriya (natija kiritish → vrach tasdig‘i → PDF), bemor portali,
Telegram bot, SMS (Xabarchi), hisobotlar (muassasa / tuman / viloyat / davlat kesimida) va andoza (blanka)
muharriri. `../Clinic-Web` React ilovasiga API beradi — har bir repository metodi bitta endpointga mos keladi.

## Stek

| Qatlam | Texnologiya |
|---|---|
| HTTP | FastAPI, Pydantic v2 (camelCase DTO), uvicorn |
| Ma’lumotlar bazasi | PostgreSQL 16+, SQLAlchemy 2 (async, asyncpg); sxema `db/v2/*.sql`, Alembic baseline `v2_0001` |
| Kesh / navbat / sessiya | Redis (JWT allow-list, rate limit, kesh, lease); testlarda `REDIS_URL=fake://` |
| PDF | fpdf2 (andoza renderer, `docs/RENDERER_SPEC.md`) |
| Integratsiyalar | Xabarchi SMS, Eskiz.uz SMS, python-telegram-bot |
| Xavfsizlik | argon2id, JWT, Fernet (maxfiy kalitlar shifrlangan) |

Batafsil arxitektura va endpoint xaritasi: [`ARCHITECTURE.md`](ARCHITECTURE.md);
biznes qoidalari, xato kodlari: [`docs/DOMAIN_RULES.md`](docs/DOMAIN_RULES.md);
eski kompaniya/filial modelidan daraxtga o‘tish shartnomasi: [`docs/V2_PORT.md`](docs/V2_PORT.md).

## Tashkiliy daraxt (qisqacha)

Kompaniya va filial tushunchasi yo‘q. Bitta `org_units` jadvali butun ierarxiyani saqlaydi:

```
country  (O‘zbekiston Respublikasi)            level = 'country'
└─ region   (Xorazm viloyati)                  level = 'region'
   └─ district (Bog‘ot tumani / Urganch shahri) level = 'district'
      └─ facility (Bog‘ot tumani SES, …)        level = 'facility'   ← har bir tumanda istalgancha
```

Chek, to‘lov, hujjat, SMS va bemor ro‘yxati **muassasaga** (`unitId`) bog‘lanadi; katalog (kategoriya, xizmat,
sxema, andoza) istalgan darajadagi birlikka tegishli (`ownerUnitId`) va uning butun daraxtida ko‘rinadi;
sozlamalar, SMS akkaunt va SMS matnlari pastga meros bo‘ladi (yaqinroq daraja ustun). Xodim o‘z birligi
(va qo‘shimcha birliklari) ostidagi hamma narsani ko‘radi; superadmin istalgan birlikni tanlaydi. Barcha
«tenant» endpointlar `/api/v1/units/{unit_id}/…` ko‘rinishida.

## Tez boshlash (lokal)

Talablar: Python 3.12, PostgreSQL 16+, Redis 7+ (yoki `REDIS_URL=fake://`).

```bash
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt   # faqat prod: requirements.txt
cp .env.example .env              # DATABASE_URL, REDIS_URL, JWT_SECRET ni to‘g‘rilang
python -m app                     # alembic upgrade head (bo‘sh bazaga db/v2/*.sql) + uvicorn 0.0.0.0:$PORT
python -m app.cli seed-reference  # davlatlar, viloyat/tumanlar
python -m app.cli create-country-root --country UZ --name "O‘zbekiston Respublikasi" --short-name "O‘zbekiston"
python -m app.cli create-superadmin --login owner --password 'S3cret!' --name "Platforma egasi"
```

Swagger: <http://localhost:8000/docs> · Liveness: `GET /health` · Readiness (DB+Redis): `GET /health/ready`.
Superadmin bilan kirib, `POST /units` orqali viloyat → tuman → muassasa tugunlarini yarating (yoki UI’dagi
«Hududlar» bo‘limi), so‘ng muassasaga xodimlar biriktiring.

Faqat API (migratsiyasiz, auto-reload bilan):

```bash
uvicorn app.main:app --reload --port 8000
```

`Makefile` da qisqa buyruqlar bor: `make dev`, `make migrate`, `make partitions`, `make test`, `make lint`.

## Muhit o‘zgaruvchilari (`.env.example`)

| O‘zgaruvchi | Vazifa |
|---|---|
| `APP_ENV` | `development` \| `test` \| `production` (prod’da `OTP_DEV_MODE` majburan o‘chadi) |
| `PORT`, `WEB_CONCURRENCY` | HTTP port (Dokploy beradi), uvicorn worker soni (default 1) |
| `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | PostgreSQL |
| `REDIS_URL` | Redis; **`fake://`** — jarayon ichidagi fakeredis (testlar / Redis’siz lokal ish; prod’da hech qachon) |
| `JWT_SECRET` (≥32), `STAFF_TOKEN_TTL_HOURS`, `PATIENT_TOKEN_TTL_HOURS` | sessiyalar |
| `ENCRYPTION_KEY` | Fernet kaliti — Xabarchi API kalitlari va bot tokenlari shu bilan shifrlanadi |
| `CORS_ORIGINS`, `PUBLIC_API_URL`, `FRONTEND_URL` | brauzer manzillari, SMS/Telegram’dagi havolalar |
| `DEFAULT_COUNTRY_CODE` | `GET /regions` `countryId`siz qaysi davlatni qaytaradi (default `UZ`) |
| `XABARCHI_BASE_URL`, `XABARCHI_TIMEOUT_SECONDS` | SMS server (kalitlar bazada, birlik bo‘yicha) |
| `OTP_DEV_MODE`, `OTP_TTL_SECONDS` | dev’da kod javobda (`devCode`) qaytadi |
| **`TELEGRAM_BOT_TOKEN`** (yangi) | **platforma boti** tokeni — tokeni saqlanmagan (`api_key_enc NULL`) `telegram` akkaunt shu tokenda ishlaydi; muassasa o‘z botini ulasa, uning tokeni bazada |
| `WORKERS_ENABLED`, `TELEGRAM_ENABLED`, `OUTBOX_POLL_SECONDS` | fon ishchilari va botlar (faqat bitta instansda) |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCK_MINUTES`, `RATE_LIMIT_PER_MINUTE`, `TRUST_PROXY_HEADERS`, `LOG_JSON` | xavfsizlik / log |

## Ma’lumotlar bazasi va migratsiyalar

Sxemaning yagona manbai — **`db/v2/*.sql`** (DDL, triggerlar, funksiyalar, view’lar, seed’lar). Alembic’da bitta
baseline revision bor — `v2_0001` — u bo‘sh bazaga aynan shu skriptlarni (`00_`…`10_`, `20_`, `21_`, `31_`) qo‘llaydi
va `schema_migrations` ga yozadi. Keyingi o‘zgarishlar yangi Alembic revision **va** yangi `db/v2/NN_*.sql`
sifatida qo‘shiladi; qo‘llangan migratsiya hech qachon tahrirlanmaydi.

```bash
alembic upgrade head        # bo‘sh bazani v2 ko‘rinishiga keltiradi (python -m app buni o‘zi bajaradi)
alembic stamp v2_0001       # baza allaqachon db/v2/run.sh bilan yaratilgan bo‘lsa — faqat belgilash
alembic revision -m "..."   # yangi o‘zgarish (baseline’ni downgrade qilib bo‘lmaydi — bazani o‘chirish kerak)
```

`audit_log` jadvali oy bo‘yicha bo‘limlangan (partition). Kelgusi oylar uchun bo‘limlar
`python -m app.cli ensure-partitions` bilan yaratiladi (maintenance ishchisi ham chaqiradi).

### Eski (kompaniya/filial) bazadan ko‘chirish — bir martalik

`db/v2/migrate.sh` (serverda, root sifatida) production bazasini **faqat o‘qib** `pg_dump` qiladi, nusxani yangi
Postgres instansida `legacy_tibdaftari` nomi bilan tiklaydi va `run.sh` orqali `10_` (DDL), `29_legacy_fdw.sql`
(postgres_fdw: `legacy` sxemasi) va `30_migrate_legacy.sql` (kompaniya → birlik, filial → muassasa, `branch_prices`
→ `service_unit_settings`, SMS ustunlari → `messaging_accounts`, …; hisobot `legacy.migration_report` ga) ni
qo‘llaydi. Production’ga hech narsa yozilmaydi; qayta ishga tushirish xavfsiz (qo‘llangan skriptlar o‘tkazib
yuboriladi). `22_seed_org.sql` — hozirgi real tashkiliy daraxt (muassasa id’lari = eski filial id’lari, chek
raqamlari davom etadi). `29_`/`30_` Alembic baseline’ga kirmaydi — ular faqat shu ko‘chirish uchun.

## Operatsion buyruqlar (`python -m app.cli`)

| Buyruq | Vazifa |
|---|---|
| `seed-reference` | Davlatlar (`seed/reference/countries.json`), O‘zbekiston viloyat/tumanlari (`seed/reference/uz-regions.json`), idempotent upsert |
| `create-country-root --country UZ --name "…" [--short-name …] [--code UZ]` | Daraxtning davlat tuguni (bo‘lmasa yaratadi) va davlatni yoqadi |
| `create-superadmin --login L --password P --name "F.I.O."` | Platforma superadmini: tizim `superadmin` roli, birliksiz (`is_super_admin`), hamma narsani ko‘radi |
| *(UI: Xodimlar → rol «O‘rnatuvchi»)* | Hudud o‘rnatuvchisi: superadmin kabi, lekin faqat o‘z viloyati (yoki butun davlati) va superadmin bergan qo‘shimcha viloyatlar ichida; boshqa davlatga chiqolmaydi. Faqat superadmin beradi |
| *(UI: Xodimlar → rol «Davlat / Viloyat / Tuman / Muassasa rahbari»)* | Faqat ko‘ruvchi rahbar: o‘z darajasidagi tugunga (davlat, viloyat, tuman yoki muassasa) biriktiriladi, hududidagi hamma hisobot, statistika, ro‘yxat va hujjatlarni ko‘radi, hech narsani o‘zgartirmaydi |
| `set-password --login L --password P` | Xodim parolini almashtiradi, blokirovkani tozalaydi |
| `ensure-partitions [--months 3]` | `SELECT ensure_audit_partitions(n)` |

`seed-demo` buyrug‘i **olib tashlangan** (demo dataset kompaniya modeliga qurilgan edi). Real tuzilma
`db/v2/22_seed_org.sql` yoki `POST /units` orqali kiritiladi.

Bemor portali: telefon raqam bo‘yicha OTP yoki qabulxona bergan login + parol. `OTP_DEV_MODE=true` bo‘lsa kod SMS
o‘rniga javobda (`devCode`) va logda qaytadi — real Xabarchi kaliti shart emas.

## API haqida

* Prefiks `/api/v1`, Swagger `/docs`, OpenAPI `/openapi.json`.
* Barcha DTO’lar camelCase, vaqtlar ISO-8601 UTC (`…Z`), pul — butun so‘m, id — UUID (string).
* Xatolar: `{ "error": { "code": "...", "message": "..." } }` (kodlar `docs/DOMAIN_RULES.md` §1).
* Autentifikatsiya: `Authorization: Bearer <jwt>`; xodim sessiyasi 12 soat, bemor — 30 kun.
* Tenant endpointlar `/units/{unit_id}/…`: `unit_id` — xodim ishlayotgan birlik (muassasa yoki yuqoriroq tugun);
  ro‘yxatlar shu birlik va ostidagilarni qaytaradi, `unitId` so‘rov parametri ichki birlikka toraytiradi. Chek
  yozish, bemor ro‘yxatga olish, SMS yuborish — faqat muassasa darajasida (`422 facility_required`).
* **Tezkor ishga tushirish** — `GET/POST /units/{unit_id}/quick-start`: bitta so‘rov bilan ishga tayyor muassasa
  (kerak bo‘lsa viloyat / tuman tugunlari, muassasa, standart xodimlar — kassir, vrach, laborant, rahbar — bitta
  parol bilan, namuna muassasaning katalogi va andozalari yangi muassasaga bog‘lab ko‘chiriladi, faol chek).
  Qoidalar: `docs/DOMAIN_RULES.md` §3.
* **Yordamchi (AI)** — `/assistant/*`: xodim o‘z roli doirasida model bilan suhbatlashadi; model faqat xodimga
  ruxsat etilgan vositalarni ko‘radi (hammasi oddiy servis funksiyalari orqali, xodimning o‘z principal’i bilan),
  o‘zgartirishlar xodim tasdiqlagandan keyingina bajariladi, kalit/parol/tokenlar modelga yetib bormaydi, har bir amal
  auditga yoziladi. Provayder superadmin tomonidan bir marta platforma uchun kiritiladi (`PUT /assistant/platform-account`)
  va hamma uchun ishlaydi; birlik xohlasa o‘z hisobini qo‘yadi yoki o‘chiradi, pastga meros bo‘ladi (`PUT /units/{id}/assistant`):
  Claude (rasmiy Anthropic SDK), OpenAI, DeepSeek, Deploy yoki istalgan OpenAI-compatible manzil. «Excel qilib ber»
  deyilsa model ma’lumotni serverda filtrlab/saralab fayl (xlsx / csv / json / pdf / docx / md / txt) yasaydi; «hisobot
  yoz» → Word / PDF hujjat, «diagramma» → PNG / SVG rasm — xodim chatdan yuklab oladi. Qoidalar:
  `docs/DOMAIN_RULES.md` §3.
* Endpoint xaritasi moduli bo‘yicha: [`ARCHITECTURE.md`](ARCHITECTURE.md#endpoint-map-prefix-apiv1).

## Deploy (Dokploy + Railpack)

Repo ildizida `railpack.json` bor — Dokploy uni avtomatik ishlatadi (Python `3.12` —
eng yangi 3.12.x, start buyrug‘i `python -m app`). `mise.toml` faqat `[settings]` saqlaydi
(`python.github_attestations = false`) — mise 2026.x attestation’siz eski CPython build’larida
`No GitHub artifact attestations found` xatosi bilan to‘xtamasligi uchun. `Dockerfile` ham
mavjud (Docker provider tanlansa).

**v2 kod v2 bazani talab qiladi.** Eski (kompaniya/filial) baza ustida ishlamaydi. Ikki yo‘l:
* **bo‘sh baza** → `python -m app` (yoki `alembic upgrade head`) `db/v2/*.sql` ni o‘zi qo‘llaydi;
* **`db/v2/run.sh` bilan tayyorlangan baza** (masalan, `migrate.sh` orqali eski ma’lumotlar ko‘chirilgan
  `TemoDB`) → `alembic stamp v2_0001`, so‘ng `python -m app`.

1. Dokploy’da PostgreSQL 16 va Redis servislarini yarating (yoki tashqi manzil bering).
2. Application → Git repo → branch. Build: Railpack (default) yoki Dockerfile.
3. Environment: `.env.example` dagi barcha o‘zgaruvchilar. Majburiylari:
   `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` (≥32 belgi), `ENCRYPTION_KEY` (Fernet),
   `APP_ENV=production`, `CORS_ORIGINS`, `PUBLIC_API_URL`, `FRONTEND_URL`, `LOG_JSON=true`;
   platforma boti uchun `TELEGRAM_BOT_TOKEN`.
4. `PORT` ni Dokploy o‘zi beradi — ilova aynan shu portda tinglaydi (`0.0.0.0:$PORT`).
5. Health check: `/health/ready`.

Ishga tushishda `python -m app` avval `alembic upgrade head` ni bajaradi, so‘ng uvicorn’ni ko‘taradi.

**Bitta instans qoidasi.** `WORKERS_ENABLED=true` (outbox dispatcher, maintenance) va
`TELEGRAM_ENABLED=true` (botlar, long polling) faqat **bitta** jarayonda yoqilishi kerak —
Redis lease dublikatni to‘xtatadi, lekin Telegram polling ikkita nusxada ishlamaydi. Shuning uchun:

* `WEB_CONCURRENCY=1` (default) — asosiy instans: API + ishchilar + botlar;
* gorizontal kengaytirish kerak bo‘lsa, qo‘shimcha instanslarni `WORKERS_ENABLED=false TELEGRAM_ENABLED=false`
  bilan ko‘taring (faqat API), yoki botlar uchun webhook rejimini yoqing.

Birinchi deploydan keyin (bo‘sh baza bo‘lsa):

```bash
python -m app.cli seed-reference
python -m app.cli create-country-root --country UZ --name "O‘zbekiston Respublikasi" --short-name "O‘zbekiston"
python -m app.cli create-superadmin --login owner --password '...' --name "<F.I.O.>"
```

## Xavfsizlik

* Parollar — argon2id; login bo‘yicha urinishlar cheklangan (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCK_MINUTES`).
* JWT + Redis allow-list: `logout`/bekor qilish darhol kuchga kiradi; rol o‘zgarishi keyingi so‘rovdan amal qiladi.
* Har bir IP uchun rate limit (`RATE_LIMIT_PER_MINUTE`, auth uchun alohida), so‘rov hajmi cheklangan.
* Maxfiy kalitlar (Xabarchi API key, Telegram bot token) bazada Fernet bilan shifrlangan, API’da faqat
  niqoblangan ko‘rinishda (`xab_live_••••••••7f2a`), loglarga tushmaydi. `ENCRYPTION_KEY` ni yo‘qotmang —
  usiz saqlangan kalitlarni o‘qib bo‘lmaydi.
* Ko‘lam izolyatsiyasi: har bir so‘rov URL’dagi birlik (`UnitScope`) va xodimning ruxsat etilgan birliklari
  bo‘yicha filtrlanadi; ruxsatlar rol ∪ allow − deny; ruxsat kaliti NIMA, xodimning birligi QAYERDA ekanini aytadi.
* Kiruvchi ma’lumot qat’iy Pydantic validatsiyasi; xom SQL string interpolatsiyasi yo‘q.
* Prod’da `APP_ENV=production`, `OTP_DEV_MODE=false`, HTTPS faqat reverse-proxy orqali (`TRUST_PROXY_HEADERS`).

## Soft-delete va zaxira (backup) siyosati

* Hech bir biznes qator fizik o‘chirilmaydi: `deleted_at/deleted_by` belgilanadi, o‘qishlar
  `deleted_at IS NULL` bilan filtrlanadi. "O‘chirish" endpointlari audit yozadi. Yagona istisno — superadminning
  birlikni «yangi tug‘ilgandek» qilib tozalashi (`GET/POST /units/{id}/reset`): tanlangan bo‘limlar (cheklar,
  bemorlar, andozalar, xodimlar, narxlar / katalog / ichki birliklar) bitta tranzaksiyada o‘chiriladi, birlikning
  o‘zi, sozlamalari va audit qoladi.
* Barcha muhim mutatsiyalar `audit_log` ga (oy bo‘yicha partition; `unit_id` + `country_id` bilan) yoziladi: kim,
  qachon, nima (before/after). Yozuvlar o‘zgartirilmaydi.
* PDF va rasmlar (`stored_files`) bazada saqlanadi — volume shart emas, `pg_dump` hammasini qamrab oladi.
* Tavsiya: kunlik `pg_dump -Fc` + WAL arxiv (PITR), Redis uchun zaxira shart emas (sessiya/kesh qayta tiklanadi).
  Eski `audit_log` bo‘limlarini o‘chirish o‘rniga arxiv bazaga ko‘chiring (`DETACH PARTITION`).

## Telegram bot va SMS provayderlar — Xabarchi, Eskiz (birlik bo‘yicha, meros bilan)

SMS akkaunt, SMS matnlari va Telegram bot istalgan birlikka (davlat, viloyat, tuman, muassasa) biriktiriladi va
pastga meros bo‘ladi — eng yaqin sozlangan daraja ustun. Masalan, viloyat tuguniga kiritilgan Xabarchi kaliti
uning barcha muassasalari uchun ishlaydi; bitta muassasa o‘z kalitini kiritsa — o‘ziniki; `off` rejimi shu
birlik va ostidagilar uchun SMS’ni o‘chiradi.

**Xabarchi SMS** (server manzili `XABARCHI_BASE_URL`, standart: `https://manager-xabarchi-backend-bula2s-f6aaa1-13-140-185-49.sslip.io`)
1. Xabarchi kabinetidan API kalit oling (`xab_live_…`, `sms.send` huquqi).
2. Birlik → Sozlamalar → SMS: `PUT /api/v1/units/{id}/sms` `{ mode: own, apiKey, defaultPriority, senderNote }`
   (`mode: inherited` — yuqoridagi birlik akkauntini ishlatadi, `mode: off` — o‘chiradi). `apiKey` faqat yozish
   uchun; javobda niqob va qaysi birlik akkaunti amalda ekani (`effectiveUnitId`).
3. `POST /api/v1/units/{id}/sms/test` — amaldagi akkaunt bilan haqiqiy test SMS.
4. Yuborish navbati (`outbox_messages`) va qayta urinishlar — bizda; dispatcher ishchi `OUTBOX_POLL_SECONDS`
   oraliqda navbatni ishlaydi. Matn shablonlari — `GET/PUT /api/v1/units/{id}/sms-templates` (o‘z matnini saqlamagan
   birlik yuqoridagi birlikning matnini, u ham bo‘lmasa standart matnni ishlatadi; `applyToAll` — butun ichki
   daraxtga yozadi). Matnda `{org}` — tashkilot (muassasadan yuqoridagi eng yaqin tugunning qisqa nomi), `{unit}` —
   muassasa; eski `{company}` / `{branch}` shularning sinonimi.

**Telegram bot**
1. Platforma boti: `TELEGRAM_BOT_TOKEN` muhit o‘zgaruvchisi — davlat tugunidagi tokensiz `telegram` akkaunt shu
   token bilan ishlaydi, hamma muassasalar uchun bitta bot.
2. Birlikning o‘z boti: `@BotFather` → token → `PUT /api/v1/units/{id}/telegram` `{ botToken }` — token `getMe` bilan
   tekshiriladi, shifrlanadi, bot `TELEGRAM_ENABLED=true` instansda ishga tushadi (long polling; webhook — birlik
   sozlamalarida `telegramWebhookSecret` bo‘lsa, `POST /api/v1/telegram/webhook/{accountId}/{secret}`).
   `GET /api/v1/units/{id}/telegram/status` — qaysi bot amalda (o‘ziniki / meros) va ishlayaptimi.
3. Bemor `/start` → til → telefon → bot davlatining bemorlar reyestridan qidiriladi → OTP (SMS) → chat bog‘lanadi.
   Menyu: cheklarim, to‘lovlarim, natijalarim (PDF), til, chiqish. To‘lov cheki va tasdiqlangan natija PDF’i
   avtomatik push qilinadi.
4. Uzish: `PUT .../telegram` `{ botToken: null }` — birlik meros botiga (yoki platforma botiga) qaytadi,
   bog‘lanish tarixi saqlanadi.

**Eskiz.uz SMS** (`ESKIZ_BASE_URL`, standart `https://notify.eskiz.uz/api`) — Xabarchi bilan bir qatorda ikkinchi provayder.
1. Eskiz kabinetida (my.eskiz.uz) hisob oching, e-mail + parol va yuboruvchi nomi (nick; dastlab `4546`) — shular kiritiladi.
2. Xodimlar ilovasi → Boshqaruv → Birlik (yoki SMS sozlamalari) → «SMS hisobi» → «O‘z hisobi» → provayder **Eskiz**:
   e-mail, parol, yuboruvchi nomi. Saqlashda tizim Eskiz'ga bir marta kiradi — noto‘g‘ri parol darhol ko‘rinadi.
   Davlat tuguniga kiritilsa — butun davlat muassasalari shu hisob orqali yuboradi; istalgan viloyat / tuman /
   muassasa o‘ziga Xabarchi yoki Eskiz hisobini kiritib ustun qilishi yoki SMS'ni o‘chirishi mumkin (eng yaqin hisob ustun).
3. «Ulanishni tekshirish» Eskiz'ning ruxsat etilgan test matnini («Bu Eskiz dan test») yuboradi va balansni ko‘rsatadi.
4. Eskiz jonli hisobda faqat kabinetda tasdiqlangan (moderatsiyadan o‘tgan) shablon matnlarini yuboradi —
   chek / natija / eslatma matnlarini Eskiz kabinetida shablon sifatida tasdiqlatib qo‘ying; mos kelmagan matn
   outbox'da Eskiz xabari bilan «failed» bo‘ladi.
5. Yetkazilganlik hisoboti: `ESKIZ_CALLBACK_URL` (yoki `PUBLIC_API_URL` asosida) → `POST /api/v1/messaging/eskiz/callback` —
   outbox qatori `delivered` / `failed` bo‘ladi.
6. Callback kelmasa ham holat yangilanadi: har 5 daqiqada (maintenance) oxirgi 3 kunda yuborilgan xabarlar Eskiz’dan
   so‘raladi (`status_by_id` / paket ro‘yxati) — qismlar soni va narxi ham yoziladi (Xabarlar sahifasida ko‘rinadi).
7. Ikki va undan ko‘p xabar bitta Eskiz hisobidan **bitta paket** (`send-batch`) bo‘lib ketadi; xorijiy raqamlar
   (`countryCode` ≠ UZ) `send-global` orqali. SMS sozlamalarida Eskiz paneli: balans, narxlar, yuboruvchi nomlari,
   shablonlar va moderatsiyaga yuborish; matn tahririda narx va qismlar soni. Hisobotlar → **SMS**: hudud / kun / tur /
   provayder / operator kesimida soni, yetkazilgani, xatosi, xarajati, Excel.

## Testlar va sifat

Testlar **faqat alohida, tashlab yuboriladigan bazaga** qarshi ishlaydi — hech qachon production yoki umumiy dev
bazaga emas (`tests/conftest.py` sessiya oxirida `T-…` nomli / `T%` kodli fixture birliklarini tozalaydi; fixture
daraxti davlat tuguni → viloyat → tuman → muassasa ko‘rinishida quriladi).

```bash
createdb temodb_test
DATABASE_URL=postgresql://postgres:toor@localhost:5432/temodb_test alembic upgrade head
DATABASE_URL=postgresql://postgres:toor@localhost:5432/temodb_test REDIS_URL=fake:// APP_ENV=test pytest -q
make lint        # ruff check app tests
```

`REDIS_URL=fake://` — jarayon ichidagi fakeredis (`requirements-dev.txt`), haqiqiy Redis kerak emas. `.env` dagi
`DATABASE_URL` production’ga qarasa, uni o‘zgartirmasdan `pytest`/`alembic`/`app.cli` ishga tushirmang.
