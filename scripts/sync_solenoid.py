#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.bosch_solenoid_inj.

Модель: повна транзакційна заміна (Google = джерело істини), аналогічно
sync_vehicles.py / sync_piezo.py.

Читає з env:
  SHEET_ID_SOLENOID            — id Google-таблиці (вкладка `bosch_solenoid_inj`)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --diff     або  DIFF=1       — розбіжності сирий-аркуш vs БД (без запису)
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API

Особливості bosch_solenoid_inj:
  * усі колонки text; generated-колонок немає.
  * `code` — PRIMARY KEY (повний номер 0445110xxx). Дубль code -> ABORT.
  * фільтр рядків: лишаємо тільки ті, де code починається з `0445` (відсікає
    заголовок/порожні/службові рядки аркуша).
  * вхідних FK на таблицю немає (vehicles.spec_code видалено 2026-08-16), тож
    TRUNCATE безпечний. Зіставлення авто↔спека йде за 0445-токеном injector у
    RPC search_vehicles — на нього ця заміна не впливає (той самий набір code).
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

SHEET_NAME = 'bosch_solenoid_inj'
MIN_ROWS = int(os.environ.get('MIN_ROWS', '700'))          # поточно 776
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC', '0.2'))

# Заголовки аркуша == імена колонок БД (bootstrap так їх і створює). Усі 10.
SHEET_COLS = ['code', 'cri_type', 'fov_code', 'nozzle_dlla', 'nozzle_0433',
              'nut', 'washer', 'oem', 'oe_number', 'valve_cap']
INSERT_COLS = SHEET_COLS  # усе, що читаємо, те й пишемо (generated-колонок нема)


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
        code = clean(cells[idx['code']])
        if not code or not code.startswith('0445'):
            continue  # заголовок/порожні/службові рядки
        if code in seen:
            raise SystemExit(f'ABORT: дубль code {code} (code — PK).')
        seen.add(code)
        rec = {c: clean(cells[idx[c]]) for c in SHEET_COLS}
        rec['code'] = code
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
            cur.execute('SELECT count(*) FROM public.bosch_solenoid_inj;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.bosch_solenoid_inj;')
            args = [[r.get(c) for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.bosch_solenoid_inj ({",".join(cols)}) '
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
            cur.execute(f'SELECT {",".join(SHEET_COLS)} FROM public.bosch_solenoid_inj;')
            for rec in cur.fetchall():
                d = {c: ('' if v is None else str(v)) for c, v in zip(SHEET_COLS, rec)}
                dbrows[d['code']] = d

    diffs, only_sheet, seen = [], [], set()
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        code = (cells[idx['code']] or '').strip()
        if not code:
            continue
        seen.add(code)
        d = dbrows.get(code)
        if d is None:
            only_sheet.append(code)
            continue
        for c in SHEET_COLS:
            raw = '' if cells[idx[c]] is None else str(cells[idx[c]])
            if raw != d[c]:
                diffs.append((code, c, d[c], raw))
    only_db = sorted(set(dbrows) - seen)
    print(f'Рядків тільки в аркуші (нема в БД): {only_sheet}')
    print(f'Рядків тільки в БД (нема в аркуші): {only_db}')
    print(f'Клітинок-розбіжностей (raw-аркуш != БД): {len(diffs)}')
    for code, c, dv, raw in diffs[:300]:
        print(f'  code={code} {c}: DB={dv!r}  SHEET_raw={raw!r}')


def parse_args(argv):
    p = argparse.ArgumentParser(description='Sync bosch_solenoid_inj: Sheets -> Supabase')
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
        sheet_id = os.environ.get('SHEET_ID_SOLENOID')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID_SOLENOID.')
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
    print('По серіях (code[:7]):', dict(sorted(Counter(r['code'][:7] for r in rows).items())))

    # Санітарна перевірка: усі code починаються з 0445 (гарантовано transform)
    bad = [r['code'] for r in rows if not r['code'].startswith('0445')]
    if bad:
        raise SystemExit(f'ABORT: є code не з 0445: {bad[:5]}')

    if len(rows) < MIN_ROWS:
        raise SystemExit(f'ABORT: рядків {len(rows)} < {MIN_ROWS}. Таблицю не чіпаю.')

    if dry_run:
        print('DRY RUN — запис пропущено.')
        return

    replace_all(db_conn_params(), rows)
    print('OK: bosch_solenoid_inj оновлено.')


if __name__ == '__main__':
    main()
