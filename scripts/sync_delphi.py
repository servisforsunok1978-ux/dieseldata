#!/usr/bin/env python3
"""Автосинхронізація Google Sheets -> Supabase для public.delphi_inj.

Модель: повна транзакційна заміна (Google = джерело істини), аналогічно
sync_continental.py / sync_denso.py / sync_piezo.py.

Джерело істини — майстер користувача «delphi»
(`1BPqgq-MPFOVhY1T3BhyK1oVEIXlbNeJLdh51n9DYhuo`, власник remontforsunok.com,
поділений із СА), вкладка «delphi». Допоміжні деталі (TIR delphi, Термошайбы,
КНР валера, РЕДУКЦІЙНІ) — в ІНШИХ вкладках і в синк не йдуть.

Читає з env:
  SHEET_ID_DELPHI              — id Google-таблиці
  SHEET_TAB_DELPHI             — назва вкладки (дефолт «delphi»)
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Прапорці / режими:
  --dry-run  або  DRY_RUN=1    — розпарсувати й показати статистику без запису
  --diff     або  DIFF=1       — розбіжності сирий-аркуш vs БД (без запису)
  --csv PATH або  CSV_PATH=... — читати з локального CSV-семпла замість Google API

Особливості delphi_inj:
  * усі 9 колонок text; generated-колонок немає; PK немає.
  * заголовки аркуша здебільшого = імена колонок БД, АЛЕ два відрізняються
    (`adaptor plate`->adaptor_plate, `nozzle-kit`->nozzle_kit), тож мапінг
    SOURCE_TO_TARGET. Зайві колонки аркуша (ведуча «1», Виробник ТС, Модель
    двигуна, Injector Spring, Термошайба, Сальник… тощо) ігноруються.
  * `oe_number` — унікальний і 100% заповнений -> логічний ключ; дубль -> ABORT.
  * фільтр рядків: від верху до першого порожнього `oe_number` (take_main).
"""
import argparse
import csv
import json
import os
import re
import sys

SHEET_NAME = os.environ.get('SHEET_TAB_DELPHI') or 'delphi'
MIN_ROWS = int(os.environ.get('MIN_ROWS') or '70')        # поточно 103
MAX_SHRINK_FRAC = float(os.environ.get('MAX_SHRINK_FRAC') or '0.2')

# Заголовок аркуша -> колонка БД. Беремо лише ці 9; решту колонок ігноруємо.
SOURCE_TO_TARGET = {
    'oe_number': 'oe_number',
    'oem_delphi': 'oem_delphi',
    'type_code': 'type_code',
    'valve': 'valve',
    'adaptor plate': 'adaptor_plate',
    'nozzle': 'nozzle',
    'nozzle-kit': 'nozzle_kit',
    'nut': 'nut',
    'washer': 'washer',
}
# Порядок колонок БД (ordinal_position).
TARGET_COLS = ['oe_number', 'oem_delphi', 'type_code', 'valve', 'adaptor_plate',
               'nozzle', 'nozzle_kit', 'nut', 'washer']
INSERT_COLS = TARGET_COLS


def key_of(oe_number):
    """Логічний ключ рядка = увесь `oe_number` (унікальний, завжди заповнений)."""
    return (oe_number or '').strip()


def clean(v):
    v = re.sub(r'\s+', ' ', (v or '')).strip()
    if v.lower() == 'none':
        return None
    return v or None


def take_main(data_rows, oe_idx, need):
    """Рядки основної таблиці: від верху до ПЕРШОГО порожнього `oe_number`
    (відсікає можливі службові/порожні рядки внизу)."""
    for cells in data_rows:
        if len(cells) <= need:
            cells = cells + [''] * (need + 1 - len(cells))
        if not (cells[oe_idx] or '').strip():
            break
        yield cells


def transform(header, data_rows):
    """header: назви колонок аркуша; data_rows: списки клітинок. Повертає rows."""
    idx = {src: header.index(src) for src in SOURCE_TO_TARGET if src in header}
    missing = [s for s in SOURCE_TO_TARGET if s not in idx]
    if missing:
        raise SystemExit(f'У джерелі бракує колонок: {missing}')

    need = max(idx.values())
    out, seen = [], set()
    for cells in take_main(data_rows, idx['oe_number'], need):
        oe = clean(cells[idx['oe_number']])
        key = key_of(oe)
        if not key:
            continue
        if key in seen:
            raise SystemExit(f'ABORT: дубль oe_number {key!r}.')
        seen.add(key)
        rec = {tgt: clean(cells[idx[src]]) for src, tgt in SOURCE_TO_TARGET.items()}
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
            cur.execute('SELECT count(*) FROM public.delphi_inj;')
            current = cur.fetchone()[0]
            floor = int(current * (1 - MAX_SHRINK_FRAC))
            if current > 0 and len(rows) < floor:
                raise SystemExit(
                    f'ABORT: нових рядків {len(rows)} < {floor} '
                    f'({int(MAX_SHRINK_FRAC * 100)}% падіння проти поточних '
                    f'{current}). Таблицю не чіпаю.')
            print(f'Поточних у БД: {current}; нових: {len(rows)}; поріг падіння: {floor}.')

            cur.execute('TRUNCATE public.delphi_inj;')
            args = [[r.get(c) for c in cols] for r in rows]
            placeholders = '(' + ','.join(['%s'] * len(cols)) + ')'
            values_sql = ','.join(cur.mogrify(placeholders, a).decode() for a in args)
            cur.execute(
                f'INSERT INTO public.delphi_inj ({",".join(cols)}) '
                f'VALUES {values_sql};')
        # commit — автоматично при виході з with conn


def diff_against_db(conn_params, header, data_rows):
    """Діагностика (нічого не пише). Ключ — `oe_number` (унікальний)."""
    import psycopg2

    idx = {src: header.index(src) for src in SOURCE_TO_TARGET if src in header}
    need = max(idx.values())
    dbrows = {}
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {",".join(TARGET_COLS)} FROM public.delphi_inj;')
            for rec in cur.fetchall():
                d = {c: ('' if v is None else str(v)) for c, v in zip(TARGET_COLS, rec)}
                dbrows[key_of(d['oe_number'])] = d

    diffs, only_sheet, seen = [], [], set()
    for cells in take_main(data_rows, idx['oe_number'], need):
        key = key_of((cells[idx['oe_number']] or '').strip())
        if not key:
            continue
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
    p = argparse.ArgumentParser(description='Sync delphi_inj: Sheets -> Supabase')
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
        sheet_id = os.environ.get('SHEET_ID_DELPHI')
        sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        if not sheet_id:
            raise SystemExit('ABORT: не задано SHEET_ID_DELPHI.')
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
    print('OK: delphi_inj оновлено.')


if __name__ == '__main__':
    main()
