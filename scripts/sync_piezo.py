#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.bosch_piezo_inj.

Модель: повна транзакційна заміна (Google = джерело істини).
Логіка парсингу — за брифом brief_sync_piezo.md (§5, §8).

Читає з env:
  SHEET_ID                     — id Google-таблиці
  SUPABASE_DB_URL              — рядок підключення до Postgres (pooler)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API
                                 (перший рядок — header). Зручно для тесту.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

SHEET_NAME = 'bosch_piezo_inj'
# Абсолютний нижній поріг (запобіжник від збою експорту аркуша).
MIN_ROWS = int(os.environ.get('MIN_ROWS', '70'))
# Відносний поріг: максимально допустиме падіння кількості рядків проти того,
# що ЗАРАЗ у БД. Зростання не обмежене — аркуш поповнюється. Дрібне легітимне
# зменшення (напр. видалення рядка-дубля §6) проходить; різке падіння через
# збій експорту — блокується. Масштабується разом зі зростанням аркуша.
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC', '0.2'))

SOURCE_TO_TARGET = {
    'Type': 'type', 'oem_number': 'oem_number', 'oe_number': 'oe_number',
    'nozzle': 'nozzle', 'Piezo Control Valve': 'piezo_control_valve',
    'nut': 'nut', 'Spacer Plate': 'spacer_plate', 'washer': 'washer',
    'O-Ring': 'o_ring',
}
TARGET_COLS = ['type', 'oem_number', 'oe_number', 'nozzle',
               'piezo_control_valve', 'nut', 'spacer_plate', 'washer', 'o_ring']


def clean(v):
    v = (v or '').replace('&#9;', '')
    v = re.sub(r'\s+', ' ', v).strip()
    if v.lower() == 'none':
        return None
    return v or None


def normalize_washer(v):
    # Кирилична С (U+0421) -> латинська C (U+0043); тільки для washer
    return v.replace('С', 'C') if v else v


def transform(header, data_rows):
    """header: список назв колонок; data_rows: список списків клітинок."""
    idx = {name: header.index(name) for name in SOURCE_TO_TARGET
           if name in header}
    missing = [n for n in SOURCE_TO_TARGET if n not in idx]
    if missing:
        raise SystemExit(f'У джерелі бракує колонок: {missing}')

    out, seen = [], set()
    for cells in data_rows:
        if len(cells) <= max(idx.values()):
            cells = cells + [''] * (max(idx.values()) + 1 - len(cells))
        oem = clean(cells[idx['oem_number']])
        if not oem or not oem.startswith('0445'):
            continue
        if oem in seen:
            continue
        seen.add(oem)
        rec = {tgt: clean(cells[idx[src]]) for src, tgt in SOURCE_TO_TARGET.items()}
        rec['washer'] = normalize_washer(rec['washer'])
        out.append(rec)
    return out


def fetch_sheet(sheet_id, sa_info):
    """Приватний Google Sheets API через сервісний акаунт."""
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
    """Локальний CSV-семпл: перший рядок — header, решта — дані."""
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit('ABORT: CSV порожній.')
    return rows[0], rows[1:]


def db_conn_params():
    """Параметри підключення для psycopg2.connect.

    Пріоритет — окремі змінні: пароль передається СИРИМ, без складання URL і
    без percent-encoding, тож будь-які спецсимволи в паролі працюють:
      SUPABASE_DB_HOST, SUPABASE_DB_PORT (6543), SUPABASE_DB_USER,
      SUPABASE_DB_NAME (postgres), SUPABASE_DB_PASSWORD
    Fallback — цілісний рядок SUPABASE_DB_URL (застарілий шлях).
    """
    pwd = os.environ.get('SUPABASE_DB_PASSWORD')
    if pwd:
        host = os.environ.get('SUPABASE_DB_HOST')
        user = os.environ.get('SUPABASE_DB_USER')
        if not host or not user:
            raise SystemExit('ABORT: задано SUPABASE_DB_PASSWORD, але бракує '
                             'SUPABASE_DB_HOST або SUPABASE_DB_USER.')
        return dict(
            host=host,
            port=os.environ.get('SUPABASE_DB_PORT', '6543'),
            user=user,
            password=pwd,
            dbname=os.environ.get('SUPABASE_DB_NAME', 'postgres'),
        )
    url = os.environ.get('SUPABASE_DB_URL')
    if url:
        return dict(dsn=url)
    raise SystemExit('ABORT: не задано ні SUPABASE_DB_PASSWORD (+HOST/USER), '
                     'ні SUPABASE_DB_URL.')


def replace_all(conn_params, rows):
    """Атомарна повна заміна в одній транзакції.

    Перед TRUNCATE — відносний запобіжник: якщо нова кількість рядків впала
    проти поточної в БД більш ніж на MAX_SHRINK_FRAC, викидаємо виняток. Він
    відкочує транзакцію (TRUNCATE ще не виконано), тож таблиця лишається цілою.
    Зростання аркуша (нова кількість >= поточної) проходить завжди.
    """
    import psycopg2

    cols = TARGET_COLS
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM public.bosch_piezo_inj;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.bosch_piezo_inj;')
            args = [[r[c] for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.bosch_piezo_inj ({",".join(cols)}) '
                f'VALUES {values_sql};')
        # commit — автоматично при виході з with conn


def parse_args(argv):
    p = argparse.ArgumentParser(description='Sync bosch_piezo_inj: Sheets -> Supabase')
    p.add_argument('--dry-run', action='store_true',
                   help='розпарсувати й показати статистику без запису')
    p.add_argument('--csv', metavar='PATH', default=os.environ.get('CSV_PATH'),
                   help='читати з локального CSV-семпла замість Google API')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    dry_run = args.dry_run or os.environ.get('DRY_RUN') == '1'

    if args.csv:
        print(f'Джерело: локальний CSV {args.csv}')
        header, data = fetch_csv(args.csv)
    else:
        sheet_id = os.environ.get('SHEET_ID')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID.')
        if not sa_raw:
            raise SystemExit('ABORT: не задано GOOGLE_SERVICE_ACCOUNT_JSON.')
        sa_info = json.loads(sa_raw)
        print(f'Джерело: Google Sheet {sheet_id} / {SHEET_NAME}')
        header, data = fetch_sheet(sheet_id, sa_info)

    rows = transform(header, data)
    print(f'Розпарсовано рядків: {len(rows)}')
    print('По серіях:', dict(sorted(Counter(r["oem_number"][:7] for r in rows).items())))

    # Санітарна перевірка: усі oem_number починаються з 0445 (гарантовано transform)
    bad = [r['oem_number'] for r in rows if not r['oem_number'].startswith('0445')]
    if bad:
        raise SystemExit(f'ABORT: є oem_number не з 0445: {bad[:5]}')

    # Запобіжник
    if len(rows) < MIN_ROWS:
        raise SystemExit(f'ABORT: рядків {len(rows)} < {MIN_ROWS}. Таблицю не чіпаю.')

    if dry_run:
        print('DRY RUN — запис пропущено.')
        return

    replace_all(db_conn_params(), rows)
    print('OK: таблицю оновлено.')


if __name__ == '__main__':
    main()
