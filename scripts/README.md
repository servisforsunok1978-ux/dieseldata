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
| `SUPABASE_DB_URL` | рядок підключення Postgres (pooler, з паролем) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | вміст JSON-ключа сервісного акаунта |
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
