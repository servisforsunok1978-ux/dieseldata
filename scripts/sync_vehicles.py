#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.vehicles.

Модель: повна транзакційна заміна (Google = джерело істини).
Логіка — за брифом brief_sync_vehicles.md.

Читає з env:
  SHEET_ID_VEHICLES            — id Google-таблиці (вкладка `vehicles`)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres
                                 (той самий формат, що в sync_piezo.py)

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API
                                 (перший рядок — header). Зручно для тесту.
  --skip-fk-check              — не валідувати spec_code проти bosch_solenoid_inj
                                 (лише для офлайн-тесту без БД; у CI не вживати)

Особливості vehicles (не як у piezo):
  * search_vector — GENERATED ALWAYS: НІКОЛИ не вставляємо, БД рахує сама.
  * id — PK без sequence: беремо з аркуша як є; дубль id -> ABORT.
  * year_start/year_end/year_open — похідні від `years` (перераховуємо тут).
  * spec_code — FK на bosch_solenoid_inj.code: невідомий код -> NULL + звіт.
  * brand, model — NOT NULL: рядок без них відкидаємо.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

SHEET_NAME = 'vehicles'
# Абсолютний нижній поріг (запобіжник від збою експорту аркуша). Поточно ~260.
MIN_ROWS = int(os.environ.get('MIN_ROWS', '220'))
# Відносний поріг: макс. допустиме падіння к-сті рядків проти того, що ЗАРАЗ у БД.
# Зростання не обмежене (аркуш поповнюється); різке падіння через збій — блокуємо.
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC', '0.2'))

# Заголовки аркуша == імена колонок БД (bootstrap так їх і створює).
SHEET_COLS = ['id', 'brand', 'model', 'generation', 'volume', 'years',
              'engine', 'body', 'manufacturer', 'injector', 'pump', 'vin',
              'spec_code']
# Що реально пишемо в INSERT: + похідні роки, БЕЗ search_vector (generated).
INSERT_COLS = ['id', 'brand', 'model', 'generation', 'volume', 'years',
               'year_start', 'year_end', 'year_open',
               'engine', 'body', 'manufacturer', 'injector', 'pump', 'vin',
               'spec_code']


def clean(v):
    v = re.sub(r'\s+', ' ', (v or '')).strip()
    if v.lower() == 'none':
        return None
    return v or None


def parse_years(years):
    """'2011-2015'->(2011,2015,False); '2011-'->(2011,None,True);
       '2014'->(2014,2014,False); порожньо/'—'->(None,None,False).
    Невідомий формат — НЕ вгадуємо: роки NULL, year_open=False (див. звіт)."""
    s = (years or '').strip().replace('—', '').strip()
    if not s:
        return None, None, False
    m = re.match(r'^(\d{4})\s*-\s*(\d{4})$', s)
    if m:
        return int(m.group(1)), int(m.group(2)), False
    m = re.match(r'^(\d{4})\s*-\s*$', s)
    if m:
        return int(m.group(1)), None, True
    m = re.match(r'^(\d{4})$', s)
    if m:
        return int(m.group(1)), int(m.group(1)), False
    return None, None, False


def transform(header, data_rows, valid_spec_codes):
    """header: назви колонок; data_rows: списки клітинок.
    valid_spec_codes: set існуючих bosch_solenoid_inj.code, або None (пропустити FK).
    Повертає (rows, dropped_specs, bad_years)."""
    idx = {name: header.index(name) for name in SHEET_COLS if name in header}
    missing = [n for n in SHEET_COLS if n not in idx]
    if missing:
        raise SystemExit(f'У джерелі бракує колонок: {missing}')

    need = max(idx.values())
    out, seen_ids, dropped_specs, bad_years = [], set(), [], []
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))

        brand = clean(cells[idx['brand']])
        model = clean(cells[idx['model']])
        if not brand or not model:
            continue  # службові/порожні рядки; brand/model — NOT NULL

        rid = clean(cells[idx['id']])
        if rid is None:
            raise SystemExit(
                f'ABORT: рядок ({brand} {model}) без id. Постав наступний '
                f'вільний id (див. next_free_id у логу).')
        if not rid.isdigit():
            raise SystemExit(f'ABORT: нечисловий id: {rid!r} ({brand} {model}).')
        rid = int(rid)
        if rid in seen_ids:
            raise SystemExit(f'ABORT: дубль id {rid} ({brand} {model}).')
        seen_ids.add(rid)

        rec = {c: clean(cells[idx[c]]) for c in SHEET_COLS}
        rec['id'] = rid
        rec['brand'] = brand
        rec['model'] = model

        ys, ye, yo = parse_years(rec['years'])
        if rec['years'] and ys is None and not yo:
            bad_years.append((rid, rec['years']))
        rec['year_start'], rec['year_end'], rec['year_open'] = ys, ye, yo

        sc = rec['spec_code']
        if sc is not None and valid_spec_codes is not None and sc not in valid_spec_codes:
            dropped_specs.append((rid, sc))
            rec['spec_code'] = None

        out.append(rec)
    return out, dropped_specs, bad_years


def fetch_sheet(sheet_id, sa_info):
    """Приватний Google Sheets API через сервісний акаунт (readonly)."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(
        sa_info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    svc = build('sheets', 'v4', credentials=creds)
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=SHEET_NAME).execute()
    rows = resp.get('values', [])
    if not rows:
        raise SystemExit('ABORT: аркуш порожній або недоступний.')
    return rows[0], rows[1:]


def fetch_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit('ABORT: CSV порожній.')
    return rows[0], rows[1:]


def db_conn_params():
    """Параметри psycopg2.connect. Пароль передається СИРИМ (без URL-екранування).
    Пріоритет — компоненти SUPABASE_DB_*, fallback — SUPABASE_DB_URL."""
    raw_pwd = os.environ.get('SUPABASE_DB_PASSWORD')
    if raw_pwd:
        pwd = raw_pwd.strip()
        if pwd != raw_pwd:
            print('УВАГА: у SUPABASE_DB_PASSWORD були пробіли/переноси на краях '
                  '— обрізав їх.')
        host = (os.environ.get('SUPABASE_DB_HOST') or '').strip()
        user = (os.environ.get('SUPABASE_DB_USER') or '').strip()
        port = (os.environ.get('SUPABASE_DB_PORT') or '6543').strip()
        dbname = (os.environ.get('SUPABASE_DB_NAME') or 'postgres').strip()
        if not host or not user:
            raise SystemExit('ABORT: задано SUPABASE_DB_PASSWORD, але бракує '
                             'SUPABASE_DB_HOST або SUPABASE_DB_USER.')
        print(f'DB: host={host}, port={port}, user={user}, dbname={dbname}, '
              f'довжина пароля={len(pwd)}.')
        return dict(host=host, port=port, user=user, password=pwd, dbname=dbname)
    url = os.environ.get('SUPABASE_DB_URL')
    if url:
        print('DB: використано SUPABASE_DB_URL (fallback).')
        return dict(dsn=url)
    raise SystemExit('ABORT: не задано ні SUPABASE_DB_PASSWORD (+HOST/USER), '
                     'ні SUPABASE_DB_URL.')


def fetch_valid_spec_codes(conn_params):
    import psycopg2
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT code FROM public.bosch_solenoid_inj;')
            return {r[0] for r in cur.fetchall()}


def replace_all(conn_params, rows):
    """Атомарна повна заміна в одній транзакції.
    Перед TRUNCATE — відносний запобіжник (див. MAX_SHRINK_FRAC)."""
    import psycopg2

    cols = INSERT_COLS
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM public.vehicles;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.vehicles;')
            args = [[r.get(c) for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.vehicles ({",".join(cols)}) '
                f'VALUES {values_sql};')
        # commit — автоматично при виході з with conn


def parse_args(argv):
    p = argparse.ArgumentParser(description='Sync vehicles: Sheets -> Supabase')
    p.add_argument('--dry-run', action='store_true',
                   help='розпарсувати й показати статистику без запису')
    p.add_argument('--csv', metavar='PATH', default=os.environ.get('CSV_PATH'),
                   help='читати з локального CSV-семпла замість Google API')
    p.add_argument('--skip-fk-check', action='store_true',
                   help='не валідувати spec_code проти bosch_solenoid_inj (офлайн-тест)')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    dry_run = args.dry_run or os.environ.get('DRY_RUN') == '1'

    if args.csv:
        print(f'Джерело: локальний CSV {args.csv}')
        header, data = fetch_csv(args.csv)
    else:
        sheet_id = os.environ.get('SHEET_ID_VEHICLES')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID_VEHICLES.')
        if not sa_raw:
            raise SystemExit('ABORT: не задано GOOGLE_SERVICE_ACCOUNT_JSON.')
        sa_info = json.loads(sa_raw)
        print(f'Джерело: Google Sheet {sheet_id} / {SHEET_NAME}')
        header, data = fetch_sheet(sheet_id, sa_info)

    # Валідні spec_code для FK — читаємо з БД (крім --skip-fk-check).
    valid = None
    conn_params = None
    if not args.skip_fk_check:
        conn_params = db_conn_params()
        valid = fetch_valid_spec_codes(conn_params)
        print(f'Валідних spec_code у bosch_solenoid_inj: {len(valid)}.')

    rows, dropped, bad_years = transform(header, data, valid)
    print(f'Розпарсовано рядків: {len(rows)}')
    if rows:
        print(f'next_free_id = {max(r["id"] for r in rows) + 1}')  # підказка для нового авто
    print('По брендах:', dict(sorted(Counter(r['brand'] for r in rows).items())))
    if dropped:
        print(f'spec_code -> NULL (немає в bosch_solenoid_inj), {len(dropped)}: {dropped}')
    if bad_years:
        print(f'Незрозумілий формат `years` (роки лишив NULL), {len(bad_years)}: {bad_years}')

    # Санітарні перевірки
    for r in rows:
        if not r['brand'] or not r['model']:
            raise SystemExit(f'ABORT: рядок id={r["id"]} без brand/model.')

    if len(rows) < MIN_ROWS:
        raise SystemExit(f'ABORT: рядків {len(rows)} < {MIN_ROWS}. Таблицю не чіпаю.')

    if dry_run:
        print('DRY RUN — запис пропущено.')
        return

    if conn_params is None:
        conn_params = db_conn_params()
    replace_all(conn_params, rows)
    print('OK: vehicles оновлено.')


if __name__ == '__main__':
    main()
