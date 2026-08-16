#!/usr/bin/env python3
"""Одноразовий bootstrap: дамп public.vehicles -> ІСНУЮЧА Google-таблиця.

Чому не «скрипт створює таблицю сам»: сервісний акаунт без Google Workspace не
має власного сховища Drive і не може створити файл (Sheets API повертає 403
"The caller does not have permission"). Тож потрібен один ручний крок:

  1) Андрій створює ПОРОЖНЮ Google-таблицю у своєму Drive.
  2) Ділиться нею з email сервісного акаунта (роль *Редактор*).
     Цей email друкує info-режим цього скрипта (нижче) — це не секрет.
  3) Дає id таблиці (SHEET_ID_VEHICLES) — і скрипт наповнює вкладку `vehicles`.

Після цього Google-таблиця стає джерелом істини, а sync_vehicles.py дзеркалить
її назад у БД.

Режими:
  без --sheet-id  -> INFO: лише друкує client_email сервісного акаунта та
                     інструкцію (нічого не пише). Зручно дізнатись, кому шарити.
  --sheet-id ID   -> наповнює вкладку `vehicles` цієї таблиці (створює вкладку,
                     якщо її нема; очищає й перезаписує). Потрібен --create.
  --create        -> підтвердження реального запису (щоб не зачепити випадково).

Env:
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  SHEET_ID_VEHICLES            — id цільової таблиці (або прапорець --sheet-id)
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Вигружаються всі 16 колонок, крім generated search_vector (БД рахує її сама):
  id, brand, model, generation, volume, years, year_start, year_end, year_open,
  engine, body, manufacturer, injector, pump, vin, spec_code
year_start/end/open дзеркалимо як є (round-trip точний); sync перераховує роки
зі `years` лише для нового рядка, де вони порожні.
"""
import argparse
import json
import os
import sys

SHEET_TAB = 'vehicles'
EXPORT_COLS = ['id', 'brand', 'model', 'generation', 'volume', 'years',
               'year_start', 'year_end', 'year_open',
               'engine', 'body', 'manufacturer', 'injector', 'pump', 'vin',
               'spec_code']
# Запис у наявну таблицю — досить вузького scope.
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def db_conn_params():
    raw_pwd = os.environ.get('SUPABASE_DB_PASSWORD')
    if raw_pwd:
        pwd = raw_pwd.strip()
        host = (os.environ.get('SUPABASE_DB_HOST') or '').strip()
        user = (os.environ.get('SUPABASE_DB_USER') or '').strip()
        port = (os.environ.get('SUPABASE_DB_PORT') or '6543').strip()
        dbname = (os.environ.get('SUPABASE_DB_NAME') or 'postgres').strip()
        if not host or not user:
            raise SystemExit('ABORT: бракує SUPABASE_DB_HOST або SUPABASE_DB_USER.')
        print(f'DB: host={host}, port={port}, user={user}, dbname={dbname}, '
              f'довжина пароля={len(pwd)}.')
        return dict(host=host, port=port, user=user, password=pwd, dbname=dbname)
    url = os.environ.get('SUPABASE_DB_URL')
    if url:
        return dict(dsn=url)
    raise SystemExit('ABORT: не задано SUPABASE_DB_PASSWORD (+HOST/USER) чи SUPABASE_DB_URL.')


def fetch_vehicles(conn_params):
    import psycopg2
    cols_sql = ', '.join(EXPORT_COLS)
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {cols_sql} FROM public.vehicles ORDER BY id;')
            return cur.fetchall()


def to_cell(v):
    """None -> '' ; усе інше -> текст (щоб Google з RAW нічого не перетворював)."""
    return '' if v is None else str(v)


def ensure_tab(sheets, sid):
    """Повертає, коли вкладка SHEET_TAB існує (додає її за потреби)."""
    meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
    titles = [s['properties']['title'] for s in meta.get('sheets', [])]
    if SHEET_TAB in titles:
        return
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={
        'requests': [{'addSheet': {'properties': {'title': SHEET_TAB}}}]}).execute()
    print(f'Додано вкладку `{SHEET_TAB}`.')


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description='Bootstrap vehicles -> existing Google Sheet')
    p.add_argument('--create', action='store_true',
                   help='підтвердити реальний запис у таблицю')
    p.add_argument('--sheet-id', default=os.environ.get('SHEET_ID_VEHICLES'),
                   help='id цільової таблиці; без нього — INFO-режим')
    args = p.parse_args(argv)

    sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not sa_raw:
        raise SystemExit('ABORT: не задано GOOGLE_SERVICE_ACCOUNT_JSON.')
    sa_info = json.loads(sa_raw)
    client_email = sa_info.get('client_email', '(невідомо)')

    print('=' * 60)
    print(f'SERVICE_ACCOUNT_EMAIL={client_email}')
    print('=' * 60)

    if not args.sheet_id:
        print('INFO-режим (без --sheet-id): нічого не записую.')
        print('Кроки:')
        print(f'  1) Створи порожню Google-таблицю у своєму Drive.')
        print(f'  2) Поділись нею з {client_email} на роль «Редактор».')
        print(f'  3) Перезапусти bootstrap із id цієї таблиці '
              f'(input sheet_id / --sheet-id).')
        return

    if not args.create:
        raise SystemExit('Відмова: для запису додай --create свідомо.')

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    sheets = build('sheets', 'v4', credentials=creds)

    data = fetch_vehicles(db_conn_params())
    print(f'Прочитано з vehicles: {len(data)} рядків.')
    values = [EXPORT_COLS] + [[to_cell(c) for c in row] for row in data]

    ensure_tab(sheets, args.sheet_id)
    # очистити стару вкладку, щоб не лишились хвостові рядки
    sheets.spreadsheets().values().clear(
        spreadsheetId=args.sheet_id, range=SHEET_TAB).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=args.sheet_id, range=f'{SHEET_TAB}!A1',
        valueInputOption='RAW', body={'values': values}).execute()
    print(f'Записано {len(values)} рядків (з заголовком) у вкладку `{SHEET_TAB}`.')
    print(f'SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/{args.sheet_id}')
    print('Далі: постав цей id у GitHub variable SHEET_ID_VEHICLES і прожени '
          'sync-vehicles з dry_run=true для перевірки round-trip.')


if __name__ == '__main__':
    main()
