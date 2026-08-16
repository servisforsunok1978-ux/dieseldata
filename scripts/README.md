# sync_piezo — Google Sheets → Supabase

Автосинхронізація таблиці `public.bosch_piezo_inj` у Supabase з Google-таблицею.

**Модель:** повна транзакційна заміна. Google-таблиця — джерело істини; ручні
правки прямо в БД між синхронізаціями будуть затерті (це очікувано). Тому
**усі правки роби лише в аркуші**.

## Що робить скрипт

1. Тягне значення аркуша `bosch_piezo_inj` (приватний Google Sheets API, сервісний акаунт).
2. Мапить 9 колонок джерела на колонки БД **за назвами заголовків**.
3. Чистить дані: стискає пробіли, `None`-літерал → `NULL`, лишає тільки рядки з
   `oem_number`, що починається з `0445`, дедуплікує за `oem_number`.
4. Нормалізує **тільки** `washer`: кирилична `С` (U+0421) → латинська `C`.
5. Запобіжники (аркуш поповнюється, тож зростання дозволене, а провали — ні):
   - **Абсолютний:** якщо розпарсовано `< MIN_ROWS` (за замовч. `70`) — не чіпає таблицю.
   - **Відносний:** перед записом читає поточний `count(*)` у БД; якщо нова
     кількість впала проти поточної більш ніж на `MAX_SHRINK_FRAC` (за замовч.
     20%) — не чіпає таблицю. Зростання не обмежене; поріг масштабується разом з
     аркушем, тож фіксовані `70` не старіють.
6. Атомарно замінює вміст таблиці: `TRUNCATE` → батч-`INSERT` в одній транзакції.

## Env-змінні

| Змінна | Призначення |
|--------|-------------|
| `SHEET_ID` | id Google-таблиці |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | вміст JSON-ключа сервісного акаунта |
| `SUPABASE_DB_PASSWORD` | пароль БД (сирий, без URL-екранування) — **секрет** |
| `SUPABASE_DB_HOST` | хост pooler, напр. `aws-0-eu-west-3.pooler.supabase.com` |
| `SUPABASE_DB_USER` | користувач, напр. `postgres.fxqboawaiwgltvxxykan` |
| `SUPABASE_DB_PORT` | порт (за замовч. `6543`) |
| `SUPABASE_DB_NAME` | база (за замовч. `postgres`) |
| `SUPABASE_DB_URL` | *(альтернатива)* цілісний рядок підключення; використовується, лише якщо не задано `SUPABASE_DB_PASSWORD` |
| `DRY_RUN=1` | те саме, що `--dry-run` |
| `CSV_PATH` | те саме, що `--csv PATH` |
| `MIN_ROWS` | абсолютний нижній поріг запобіжника (за замовч. `70`) |
| `MAX_SHRINK_FRAC` | макс. допустиме відносне падіння к-сті рядків (за замовч. `0.2`) |

**Ніяких секретів у коді** — усе через env / GitHub Secrets.

## Локальне тестування

Створи venv і встанови залежності:

```bash
python -m venv .venv
. .venv/Scripts/activate    # Windows;  на Linux/mac: . .venv/bin/activate
pip install -r scripts/requirements.txt
```

### Dry-run через Google API

```bash
export SHEET_ID=1KqejpCZangs4tXghDAGtpYKpI6rk0RY8vdkDil6sMLM
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat sa.json)"
python scripts/sync_piezo.py --dry-run
```

### Dry-run через локальний CSV (без Google-креденшелів)

Перший рядок CSV — заголовки аркуша (усі 14 колонок у тому ж порядку).

```bash
python scripts/sync_piezo.py --csv sample.csv --dry-run
```

Очікуваний вивід зараз: `Розпарсовано рядків: 90` (або `91` до узгодження
рядка-дубля в аркуші) і розподіл
`{'0445115': 19, '0445116': 27, '0445117': 28, '0445118': 16}`.

## Розклад (GitHub Actions)

`.github/workflows/sync-piezo.yml` — cron + ручний `workflow_dispatch`.
Секрети/змінні задаються в GitHub → Settings → Secrets and variables → Actions:
- secret `SUPABASE_DB_URL`
- secret `GOOGLE_SERVICE_ACCOUNT_JSON`
- variable `SHEET_ID`

---

# sync_vehicles — Google Sheets → Supabase (`public.vehicles`)

Те саме дзеркало, але для таблиці авто. Google-таблиця — джерело істини;
`public.vehicles` щодня дзеркалить її (повна транзакційна заміна).

**Google-таблиця:** `dieseldata · vehicles (source of truth)`, вкладка `vehicles`.
Створена у Drive власника, поділена з сервісним акаунтом
`sheets-sync@dieseldata-sync.iam.gserviceaccount.com` (роль Редактор).
Id — у GitHub variable `SHEET_ID_VEHICLES`.

## Колонки аркуша (16 — усе, крім generated `search_vector`)

```
id | brand | model | generation | volume | years |
year_start | year_end | year_open |
engine | body | manufacturer | injector | pump | vin | spec_code
```

Дзеркалимо всі 16 як є (точний round-trip). `search_vector` не чіпаємо — БД
рахує її сама (GENERATED ALWAYS).

## Правила (специфіка vehicles)

1. **`id`** — PK без sequence: береться з аркуша як є. Дубль id → ABORT. Рядок
   без id → ABORT (у логу друкується `next_free_id` для нового авто).
2. **`brand`, `model`** — NOT NULL: рядок без них відкидається (службові/порожні).
3. **`spec_code`** — FK на `bosch_solenoid_inj.code`. Невідомий код → `NULL` +
   рядок у звіт (не ABORT, не вигадуємо). Валідні коди читаються з БД перед синком.
4. **`year_start/year_end/year_open`** — дзеркаляться як є. Перераховуються зі
   `years` **лише коли всі три порожні** (нове авто). `parse_years` розуміє
   `YYYY`, `YYYY-YYYY`, `YYYY-`, `MM/YYYY-MM/YYYY`, `MM/YYYY-`.
5. **Чистка:** стискання пробілів + trim, `none`-літерал → `NULL`. (На bootstrap
   нормалізувалось 6 «брудних» клітинок — подвійні пробіли в `pump`, службовий
   перенос у `vin`; символи номерів не зачіпались.)
6. **Запобіжники:** абсолютний `MIN_ROWS` (за замовч. `220`) + відносний
   `MAX_SHRINK_FRAC` (20%), як у piezo.

## Скрипти

| Файл | Призначення |
|------|-------------|
| `scripts/bootstrap_vehicles.py` | одноразовий дамп `vehicles` → існуюча Google-таблиця (пише у вкладку `vehicles`). Info-режим без `--sheet-id` друкує email сервісного акаунта. |
| `scripts/sync_vehicles.py` | щоденний синк Sheet → `vehicles`. Прапорці: `--dry-run`, `--diff` (сирий-аркуш vs БД, без запису), `--csv PATH`, `--skip-fk-check`. |

## Env-змінні (vehicles)

Ті самі, що в piezo, окрім id таблиці:

| Змінна | Призначення |
|--------|-------------|
| `SHEET_ID_VEHICLES` | id Google-таблиці vehicles |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `SUPABASE_DB_*` | як у piezo |
| `OWNER_EMAIL` | *(лише bootstrap, info-режим не потребує)* кому шарити |
| `DRY_RUN=1` / `DIFF=1` | те саме, що `--dry-run` / `--diff` |
| `MIN_ROWS` | за замовч. `220` |

## Розклад (GitHub Actions)

- `.github/workflows/sync-vehicles.yml` — cron `30 6 * * *` (щодня 06:30 UTC) +
  ручний `workflow_dispatch` з інпутами `dry_run` і `diff`.
- `.github/workflows/bootstrap-vehicles.yml` — лише ручний; наповнює наявну
  таблицю (input `sheet_id`) або друкує email СА (без `sheet_id`).

Змінні/секрети: variable `SHEET_ID_VEHICLES`; секрети `GOOGLE_SERVICE_ACCOUNT_JSON`,
`SUPABASE_DB_PASSWORD` (спільні з piezo).
