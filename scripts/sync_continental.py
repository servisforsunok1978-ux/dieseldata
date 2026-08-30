#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.continental_injector.

Модель: повна транзакційна заміна (Google = джерело істини), аналогічно
sync_solenoid.py / sync_piezo.py / sync_vehicles.py.

Читає з env:
  SHEET_ID_CONTINENTAL         — id Google-таблиці (вкладка `continental_injector`)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --diff     або  DIFF=1       — розбіжності сирий-аркуш vs БД (без запису)
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API

Особливості continental_injector:
  * усі 5 колонок text; generated-колонок немає; PK немає (як bosch_piezo_inj).
  * логічний ключ рядка = `oe_number` (у БД він 100% заповнений і унікальний;
    `oem_continental` має NULL-и, тож ключем бути не може). Дубль ключа -> ABORT.
  * фільтр рядків: лишаємо тільки ті, де `oe_number` непорожній
    (відсікає заголовок/порожні/службові рядки аркуша).
  * вхідних FK на таблицю немає, тож TRUNCATE безпечний.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

SHEET_NAME = 'continental_injector'
MIN_ROWS = int(os.environ.get('MIN_ROWS') or '20')        # поточно 31
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC') or '0.2')

# Заголовки аркуша == імена колонок БД (bootstrap так їх і створює). Усі 5,
# у порядку ordinal_position таблиці.
SHEET_COLS = ['oe_number', 'oem_continental', 'nozzle', 'washer', 'nut']
INSERT_COLS = SHEET_COLS  # усе, що читаємо, те й пишемо (generated-колонок нема)


def key_of(oe_number):
    """Логічний ключ рядка = увесь `oe_number` (унікальний, завжди заповнений)."""
    return (oe_number or '').strip()


def clean(v):
    v = re.sub(r'\s+', ' ', (v or '')).strip()
    if v.lower() == 'none':
        return None
    return v or None


def transform(header, data_rows):
    """header: назви колонок; data_rows: списки клітинок. Повертає rows."""
    idx = {name: header.index(name) for name in SHEET_COLS if name in header}
    missing = [n for n in SHEET_COLS if n not in idx]
    if missing:
        raise SystemExit(f'У джерелі бракує колонок: {missing}')

    need = max(idx.values())
    out, seen = [], set()
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        oe = clean(cells[idx['oe_number']])
        key = key_of(oe)
        if not key:
            continue  # заголовок/порожні/службові рядки
        if key in seen:
            raise SystemExit(f'ABORT: дубль oe_number {key!r}.')
        seen.add(key)
        rec = {c: clean(cells[idx[c]]) for c in SHEET_COLS}
        out.append(rec)
    return out


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


def replace_all(conn_params, rows):
    """Атомарна повна заміна в одній транзакції.
    Перед TRUNCATE — відносний запобіжник (див. MAX_SHRINK_FRAC)."""
    import psycopg2

    cols = INSERT_COLS
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM public.continental_injector;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.continental_injector;')
            args = [[r.get(c) for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.continental_injector ({",".join(cols)}) '
                f'VALUES {values_sql};')
        # commit — автоматично при виході з with conn


def diff_against_db(conn_params, header, data_rows):
    """Діагностика: кожна клітинка, де СИРЕ значення аркуша != значення в БД
    (усе як текст, NULL -> ''). Нічого не пише."""
    import psycopg2

    idx = {n: header.index(n) for n in SHEET_COLS if n in header}
    need = max(idx.values())
    dbrows = {}
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {",".join(SHEET_COLS)} FROM public.continental_injector;')
            for rec in cur.fetchall():
                d = {c: ('' if v is None else str(v)) for c, v in zip(SHEET_COLS, rec)}
                dbrows[key_of(d['oe_number'])] = d

    diffs, only_sheet, seen = [], [], set()
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        key = key_of((cells[idx['oe_number']] or '').strip())
        if not key:
            continue
        seen.add(key)
        d = dbrows.get(key)
        if d is None:
            only_sheet.append(key)
            continue
        for c in SHEET_COLS:
            raw = '' if cells[idx[c]] is None else str(cells[idx[c]])
            if raw != d[c]:
                diffs.append((key, c, d[c], raw))
    only_db = sorted(set(dbrows) - seen)
    print(f'Рядків тільки в аркуші (нема в БД): {only_sheet}')
    print(f'Рядків тільки в БД (нема в аркуші): {only_db}')
    print(f'Клітинок-розбіжностей (raw-аркуш != БД): {len(diffs)}')
    for key, c, dv, raw in diffs[:300]:
        print(f'  key={key} {c}: DB={dv!r}  SHEET_raw={raw!r}')


def parse_args(argv):
    p = argparse.ArgumentParser(description='Sync continental_injector: Sheets -> Supabase')
    p.add_argument('--dry-run', action='store_true',
                   help='розпарсувати й показати статистику без запису')
    p.add_argument('--diff', action='store_true',
                   help='показати розбіжності сирий-аркуш vs БД (без запису)')
    p.add_argument('--csv', metavar='PATH', default=os.environ.get('CSV_PATH'),
                   help='читати з локального CSV-семпла замість Google API')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    dry_run = args.dry_run or os.environ.get('DRY_RUN') == '1'
    do_diff = args.diff or os.environ.get('DIFF') == '1'

    if args.csv:
        print(f'Джерело: локальний CSV {args.csv}')
        header, data = fetch_csv(args.csv)
    else:
        sheet_id = os.environ.get('SHEET_ID_CONTINENTAL')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID_CONTINENTAL.')
        if not sa_raw:
            raise SystemExit('ABORT: не задано GOOGLE_SERVICE_ACCOUNT_JSON.')
        sa_info = json.loads(sa_raw)
        print(f'Джерело: Google Sheet {sheet_id} / {SHEET_NAME}')
        header, data = fetch_sheet(sheet_id, sa_info)

    if do_diff:
        diff_against_db(db_conn_params(), header, data)
        return

    rows = transform(header, data)
    print(f'Розпарсовано рядків: {len(rows)}')

    if len(rows) < MIN_ROWS:
        raise SystemExit(f'ABORT: рядків {len(rows)} < {MIN_ROWS}. Таблицю не чіпаю.')

    if dry_run:
        print('DRY RUN — запис пропущено.')
        return

    replace_all(db_conn_params(), rows)
    print('OK: continental_injector оновлено.')


if __name__ == '__main__':
    main()
