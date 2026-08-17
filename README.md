# TibDaftari Backend

Ko‘p ijarachili (multi-tenant) klinika platformasining server qismi: qabul (cheklar, to‘lovlar),
laboratoriya (natija kiritish → vrach tasdig‘i → PDF), bemor portali, Telegram bot, SMS (Xabarchi),
hisobotlar va andoza (blanka) muharriri. `../Clinic-Web` React ilovasiga API beradi — har bir
repository metodi bitta endpointga mos keladi.

## Stek

| Qatlam | Texnologiya |
|---|---|
| HTTP | FastAPI, Pydantic v2 (camelCase DTO), uvicorn |
| Ma’lumotlar bazasi | PostgreSQL 16+, SQLAlchemy 2 (async, asyncpg), Alembic |
| Kesh / navbat / sessiya | Redis (JWT allow-list, rate limit, kesh, lease) |
| PDF | fpdf2 (andoza renderer, `docs/RENDERER_SPEC.md`) |
| Integratsiyalar | Xabarchi SMS, python-telegram-bot |
| Xavfsizlik | argon2id, JWT, Fernet (maxfiy kalitlar shifrlangan) |

Batafsil arxitektura va endpoint xaritasi: [`ARCHITECTURE.md`](ARCHITECTURE.md);
biznes qoidalari, xato kodlari: [`docs/DOMAIN_RULES.md`](docs/DOMAIN_RULES.md).

## Tez boshlash (lokal)

Talablar: Python 3.12, PostgreSQL 16+, Redis 7+.

```bash
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt   # faqat prod: requirements.txt
cp .env.example .env              # DATABASE_URL, REDIS_URL, JWT_SECRET ni to‘g‘rilang
python -m app                     # alembic upgrade head + uvicorn 0.0.0.0:$PORT
```

Swagger: <http://localhost:8000/docs> · Liveness: `GET /health` · Readiness (DB+Redis): `GET /health/ready`.

Faqat API (migratsiyasiz, auto-reload bilan):

```bash
uvicorn app.main:app --reload --port 8000
```

`Makefile` da qisqa buyruqlar bor: `make dev`, `make migrate`, `make seed`, `make seed-full`,
`make test`, `make lint`.

## Migratsiyalar

Sxema Alembic bilan boshqariladi (`migrations/`). Qo‘llangan migratsiya hech qachon tahrirlanmaydi —
yangisi qo‘shiladi.

```bash
alembic revision --autogenerate -m "short_description"   # modeldan farqni aniqlaydi
alembic upgrade head                                     # qo‘llash (python -m app buni o‘zi bajaradi)
alembic downgrade -1                                     # bir qadam orqaga (faqat dev)
```

`audit_log` jadvali oy bo‘yicha bo‘limlangan (partition). Kelgusi oylar uchun bo‘limlar
`python -m app.cli ensure-partitions` bilan yaratiladi (cron/maintenance ishchisi ham chaqiradi).

## Seed va operatsion buyruqlar (`python -m app.cli`)

| Buyruq | Vazifa |
|---|---|
| `seed-reference` | O‘zbekiston viloyat/tumanlari (`seed/reference/uz-regions.json`), idempotent upsert |
| `seed-demo` | Demo dataset: 2 kompaniya, filiallar, rollar, 9 xodim, kategoriya/xizmat turlari, sxemalar, andozalar va rasmlar (`seed/demo/core.json`). Barcha id’lar yangi UUID bilan qayta bog‘lanadi. Slug band bo‘lsa — rad etadi |
| `seed-demo --with-transactions` | Yuqoridagiga qo‘shimcha ~9 000 qator: bemorlar, cheklar, xizmatlar, to‘lovlar, hujjatlar, SMS outbox, bildirishnomalar (`seed/demo/transactions.json`). Demo kompaniya allaqachon bo‘lsa, tranzaksiyalar unga qo‘shiladi (ikki marta qo‘shishga yo‘l qo‘ymaydi) |
| `create-superadmin --login L --password P --company-slug S [--company-name N] --name "F.I.O."` | Platforma superadmin roli (companyId = null, barcha ruxsatlar) + `is_super_admin` xodim; kompaniya bo‘lmasa yaratadi |
| `set-password --login L --password P` | Xodim parolini almashtiradi, blokirovkani tozalaydi |
| `ensure-partitions [--months 3]` | `SELECT ensure_audit_partitions(n)` |

```bash
python -m app.cli seed-reference
python -m app.cli seed-demo --with-transactions
python -m app.cli create-superadmin --login owner --password 'S3cret!' --company-slug shifomed --name "Platforma egasi"
```

Seed tranzaksion: yoki butun dataset yoziladi, yoki hech narsa. Kompaniya `settings.seed` maydonida
id-xarita saqlanadi — keyinroq `--with-transactions` bilan davom ettirish uchun.

### Demo loginlar

Barcha demo xodimlar paroli: **`123456`** (`seed/demo/core.json` → `credentials`).

| Login | Rol | Filiallar |
|---|---|---|
| `super` | superadmin (platforma) | UR, XV |
| `admin` | admin (Shifo Med) | UR, XV |
| `umida`, `sevara` | registrator | UR / XV |
| `muhammad`, `dilnoza` | laborant | UR / UR+XV |
| `ahmed`, `nodir` | vrach | UR+XV / UR |
| `bahrom` | rahbar (nofaol) | UR, XV |

Bemor portali: telefon raqam bo‘yicha OTP. `OTP_DEV_MODE=true` bo‘lsa kod SMS o‘rniga javobda
(`devCode`) va logda qaytadi — real Xabarchi kaliti shart emas.

## API haqida

* Prefiks `/api/v1`, Swagger `/docs`, OpenAPI `/openapi.json`.
* Barcha DTO’lar camelCase, vaqtlar ISO-8601 UTC (`…Z`), pul — butun so‘m, id — UUID (string).
* Xatolar: `{ "code": "...", "message": "..." }` (kodlar `docs/DOMAIN_RULES.md` §1).
* Autentifikatsiya: `Authorization: Bearer <jwt>`; xodim sessiyasi 12 soat (sirg‘aluvchi), bemor — 30 kun.
* Endpoint xaritasi moduli bo‘yicha: [`ARCHITECTURE.md`](ARCHITECTURE.md#endpoint-map-prefix-apiv1).

## Deploy (Dokploy + Railpack)

Repo ildizida `railpack.json` bor — Dokploy uni avtomatik ishlatadi (Python `3.12` —
eng yangi 3.12.x, start buyrug‘i `python -m app`). `mise.toml` faqat `[settings]` saqlaydi
(`python.github_attestations = false`) — mise 2026.x attestation’siz eski CPython build’larida
`No GitHub artifact attestations found` xatosi bilan to‘xtamasligi uchun. `Dockerfile` ham
mavjud (Docker provider tanlansa).

1. Dokploy’da PostgreSQL 16 va Redis servislarini yarating (yoki tashqi manzil bering).
2. Application → Git repo → branch. Build: Railpack (default) yoki Dockerfile.
3. Environment: `.env.example` dagi barcha o‘zgaruvchilar. Majburiylari:
   `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` (≥32 belgi), `ENCRYPTION_KEY` (Fernet),
   `APP_ENV=production`, `CORS_ORIGINS`, `PUBLIC_API_URL`, `FRONTEND_URL`, `LOG_JSON=true`.
4. `PORT` ni Dokploy o‘zi beradi — ilova aynan shu portda tinglaydi (`0.0.0.0:$PORT`).
5. Health check: `/health/ready`.

Ishga tushishda `python -m app` avval `alembic upgrade head` ni bajaradi, so‘ng uvicorn’ni ko‘taradi.

**Bitta instans qoidasi.** `WORKERS_ENABLED=true` (outbox dispatcher, maintenance) va
`TELEGRAM_ENABLED=true` (kompaniya botlari, long polling) faqat **bitta** jarayonda yoqilishi kerak —
Redis lease dublikatni to‘xtatadi, lekin Telegram polling ikkita nusxada ishlamaydi. Shuning uchun:

* `WEB_CONCURRENCY=1` (default) — asosiy instans: API + ishchilar + botlar;
* gorizontal kengaytirish kerak bo‘lsa, qo‘shimcha instanslarni `WORKERS_ENABLED=false TELEGRAM_ENABLED=false`
  bilan ko‘taring (faqat API), yoki botlar uchun webhook rejimini yoqing.

Birinchi deploydan keyin:

```bash
python -m app.cli seed-reference
python -m app.cli create-superadmin --login owner --password '...' --company-slug <slug> --company-name "<Klinika>" --name "<F.I.O.>"
```

## Xavfsizlik

* Parollar — argon2id; login bo‘yicha urinishlar cheklangan (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCK_MINUTES`).
* JWT + Redis allow-list: `logout`/bekor qilish darhol kuchga kiradi; rol o‘zgarishi keyingi so‘rovdan amal qiladi.
* Har bir IP uchun rate limit (`RATE_LIMIT_PER_MINUTE`, auth uchun alohida), so‘rov hajmi cheklangan.
* Maxfiy kalitlar (Xabarchi API key, Telegram bot token) bazada Fernet bilan shifrlangan, API’da faqat
  niqoblangan ko‘rinishda (`xab_live_••••••••7f2a`), loglarga tushmaydi. `ENCRYPTION_KEY` ni yo‘qotmang —
  usiz saqlangan kalitlarni o‘qib bo‘lmaydi.
* Tenant izolyatsiyasi: har bir so‘rov `company_id` bo‘yicha filtrlanadi; ruxsatlar rol ∪ allow − deny.
* Kiruvchi ma’lumot qat’iy Pydantic validatsiyasi; xom SQL string interpolatsiyasi yo‘q.
* Prod’da `APP_ENV=production`, `OTP_DEV_MODE=false`, HTTPS faqat reverse-proxy orqali (`TRUST_PROXY_HEADERS`).

## Soft-delete va zaxira (backup) siyosati

* Hech bir biznes qator fizik o‘chirilmaydi: `deleted_at/deleted_by` belgilanadi, o‘qishlar
  `deleted_at IS NULL` bilan filtrlanadi. "O‘chirish" endpointlari audit yozadi.
* Barcha muhim mutatsiyalar `audit_log` ga (oy bo‘yicha partition) yoziladi: kim, qachon, nima
  (before/after). Yozuvlar o‘zgartirilmaydi.
* PDF va rasmlar (`stored_files`) bazada saqlanadi — volume shart emas, `pg_dump` hammasini qamrab oladi.
* Tavsiya: kunlik `pg_dump -Fc` + WAL arxiv (PITR), Redis uchun zaxira shart emas (sessiya/kesh qayta tiklanadi).
  Eski `audit_log` bo‘limlarini o‘chirish o‘rniga arxiv bazaga ko‘chiring (`DETACH PARTITION`).

## Telegram bot va Xabarchi (har bir kompaniya uchun)

Har klinika o‘z SMS akkaunti va o‘z Telegram botiga ega — sozlamalar kompaniya darajasida.

**Xabarchi SMS**
1. Xabarchi kabinetidan API kalit oling.
2. Sozlamalar → SMS: provayder `xabarchi`, API kalit, ustuvorlik (`urgent`/`transactional`/`bulk`), imzo.
   API: `PUT /api/v1/companies/{id}` (`sms.apiKey` faqat yozish uchun; javobda niqob).
3. `POST /api/v1/companies/{id}/sms/test` — haqiqiy test SMS.
4. Yuborish navbati (`outbox_messages`) va qayta urinishlar — bizda; dispatcher ishchi
   `OUTBOX_POLL_SECONDS` oraliqda navbatni ishlaydi. Matn shablonlari — kompaniya `settings.smsTemplates`.

**Telegram bot**
1. `@BotFather` → yangi bot → token.
2. Sozlamalar → Telegram: `PUT /api/v1/companies/{id}/telegram` `{ botToken }` — token shifrlanadi,
   bot `TELEGRAM_ENABLED=true` instansda ishga tushadi (long polling; webhook — `settings.telegramWebhookSecret`
   bo‘lsa, `POST /api/v1/telegram/webhook/{companyId}/{secret}`).
3. Bemor `/start` → til → telefon → OTP (SMS) → chat bog‘lanadi. Menyu: cheklarim, to‘lovlarim,
   natijalarim (PDF), til, chiqish. To‘lov cheki va tasdiqlangan natija PDF’i avtomatik push qilinadi.
4. Uzish: `PUT .../telegram` `{ botToken: null }` — bot to‘xtaydi, bog‘lanish tarixi saqlanadi.

## Testlar va sifat

```bash
make test        # pytest (real dev DB, fixture qatorlar T-<module>- prefiks bilan)
make lint        # ruff check app tests
```

Testlar `.env` dagi bazaga ulanadi; jadval tozalanmaydi, faqat o‘z fixture qatorlarini yaratadi.
