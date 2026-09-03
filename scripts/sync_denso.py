#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.denso_inj.

Модель: повна транзакційна заміна (Google = джерело істини), аналогічно
sync_piezo.py / sync_solenoid.py / sync_continental.py.

Джерело істини — власний майстер користувача «denso_inj»
(`17qpxENEUsgT9Qe-bRcpKNcV_mdDcmHYXMUjgn_zaA2E`, власник servisforsunok1978,
поділений із СА), вкладка «Denso таблица». Допоміжні деталі (регулятори,
сальники, термошайби, скоби обратки) — в ІНШИХ вкладках і в синк не йдуть.

Читає з env:
  SHEET_ID_DENSO               — id Google-таблиці
  SHEET_TAB_DENSO              — назва вкладки (дефолт «Denso таблица»)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --diff     або  DIFF=1       — розбіжності сирий-аркуш vs БД (без запису)
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API

Особливості denso_inj:
  * усі 12 колонок text; generated-колонок немає; PK немає.
  * заголовки аркуша НЕ збігаються з іменами колонок БД — мапінг SOURCE_TO_TARGET
    (як bosch_piezo_inj). Зайві колонки аркуша (L, R, «Сальник…») ігноруються.
  * НЕМАЄ унікального ключа: `oe_number` має легітимні дублі (напр. 31336878 —
    дві форсунки Denso з одним OE) і порожні значення; `oem_denso` має порожні.
    Тож НЕ ABORT на дублі — лише пропускаємо ТОЧНІ дублі-рядки (усі 12 полів рівні).
  * фільтр рядків: лишаємо рядок, якщо непорожній `oe_number` АБО `oem_denso`
    (рядок-роздільник з обома порожніми — пропускаємо).
"""
import argparse
import csv
import json
import os
import re
import sys

SHEET_NAME = os.environ.get('SHEET_TAB_DENSO') or 'Denso таблица'
MIN_ROWS = int(os.environ.get('MIN_ROWS') or '100')        # поточно ~156
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC') or '0.2')

# Заголовок аркуша -> колонка БД. Беремо лише ці 12; решту колонок аркуша
# (порожня, L, R, дві «Сальник…») ігноруємо.
SOURCE_TO_TARGET = {
    'oe_number': 'oe_number',
    'oem_denso': 'oem_denso',
    'QR code': 'qr_code',
    'nozzle': 'nozzle',
    'plate': 'plate',
    'valve rod': 'valve_rod',
    'nut': 'nut',
    'washer': 'washer',
    'return line': 'return_line',
    'Injector O-Ring': 'injector_o_ring',
    'Pressure Limiter Valve': 'pressure_limiter',
    'comments': 'comments',
}
# Порядок колонок БД (ordinal_position).
TARGET_COLS = ['oe_number', 'oem_denso', 'qr_code', 'nozzle', 'plate',
               'valve_rod', 'nut', 'washer', 'return_line', 'injector_o_ring',
               'pressure_limiter', 'comments']
INSERT_COLS = TARGET_COLS


def clean(v):
    v = re.sub(r'\s+', ' ', (v or '')).strip()
    if v.lower() == 'none':
        return None
    return v or None


def transform(header, data_rows):
    """header: назви колонок аркуша; data_rows: списки клітинок. Повертає rows."""
    idx = {src: header.index(src) for src in SOURCE_TO_TARGET if src in header}
    missing = [s for s in SOURCE_TO_TARGET if s not in idx]
    if missing:
        raise SystemExit(f'У джерелі бракує колонок: {missing}')

    need = max(idx.values())
    out, seen = [], set()
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        oe = clean(cells[idx['oe_number']])
        oem = clean(cells[idx['oem_denso']])
        if not oe and not oem:
            continue  # рядок-роздільник (обидва ідентифікатори порожні)
        rec = {tgt: clean(cells[idx[src]]) for src, tgt in SOURCE_TO_TARGET.items()}
        tup = tuple(rec[c] for c in TARGET_COLS)
        if tup in seen:
            continue  # точний дубль рядка (усі 12 полів рівні) — пропускаємо
        seen.add(tup)
        out.append(rec)
    return out


def fetch_sheet(sheet_id, sa_info):
    """Приватний Google Sheets API через сервісний акаунт (readonly)."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(
        sa_info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    svc = build('sheets', 'v4', credentials=creds)
    # Назва вкладки містить пробіл/кирилицю — беремо в лапки.
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{SHEET_NAME}'").execute()
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
    """Параметри psycopg2.connect. Пароль СИРИЙ (без URL-екранування)."""
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
    """Атомарна повна заміна в одній транзакції з відносним запобіжником."""
    import psycopg2

    cols = INSERT_COLS
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM public.denso_inj;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.denso_inj;')
            args = [[r.get(c) for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.denso_inj ({",".join(cols)}) '
                f'VALUES {values_sql};')
        # commit — автоматично при виході з with conn


def diff_against_db(conn_params, header, data_rows):
    """Діагностика (нічого не пише). Ключ — (oe_number ¦ oem_denso), бо жодна
    колонка окремо не унікальна."""
    import psycopg2

    idx = {src: header.index(src) for src in SOURCE_TO_TARGET if src in header}
    need = max(idx.values())

    def dkey(oe, oem):
        return f'{(oe or "").strip()}¦{(oem or "").strip()}'

    dbrows = {}
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {",".join(TARGET_COLS)} FROM public.denso_inj;')
            for rec in cur.fetchall():
                d = {c: ('' if v is None else str(v)) for c, v in zip(TARGET_COLS, rec)}
                dbrows[dkey(d['oe_number'], d['oem_denso'])] = d

    diffs, only_sheet, seen = [], [], set()
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        oe = (cells[idx['oe_number']] or '').strip()
        oem = (cells[idx['oem_denso']] or '').strip()
        if not oe and not oem:
            continue
        key = dkey(oe, oem)
        seen.add(key)
        d = dbrows.get(key)
        if d is None:
            only_sheet.append(key)
            continue
        for src, tgt in SOURCE_TO_TARGET.items():
            raw = '' if cells[idx[src]] is None else str(cells[idx[src]])
            if raw != d[tgt]:
                diffs.append((key, tgt, d[tgt], raw))
    only_db = sorted(set(dbrows) - seen)
    print(f'Рядків тільки в аркуші (нема в БД): {only_sheet}')
    print(f'Рядків тільки в БД (нема в аркуші): {only_db}')
    print(f'Клітинок-розбіжностей (raw-аркуш != БД): {len(diffs)}')
    for key, c, dv, raw in diffs[:300]:
        print(f'  key={key} {c}: DB={dv!r}  SHEET_raw={raw!r}')


def parse_args(argv):
    p = argparse.ArgumentParser(description='Sync denso_inj: Sheets -> Supabase')
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
        sheet_id = os.environ.get('SHEET_ID_DENSO')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID_DENSO.')
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
    print('OK: denso_inj оновлено.')


if __name__ == '__main__':
    main()
