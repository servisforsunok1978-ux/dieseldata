#!/usr/bin/env python3
"""Одноразовий bootstrap: дамп public.vehicles -> НОВА Google-таблиця.

Створює таблицю сервісним акаунтом (варіант «а» з брифу), пише в неї поточні
рядки vehicles і ділиться нею з поштою власника на *Редагування*. Після цього
Google-таблиця стає джерелом істини, а sync_vehicles.py дзеркалить її назад у БД.

Читає з env:
  GOOGLE_SERVICE_ACCOUNT_JSON  — вміст JSON-ключа сервісного акаунта
  OWNER_EMAIL                  — пошта, якій дати доступ *Редагування*
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME) — підключення до Postgres

Прапорці:
  --create   — ОБОВʼЯЗКОВО для реального створення (захист від випадкового
               повторного прогону, що наплодив би дублі таблиць).
  --title T  — назва файлу (за замовч. "dieseldata · vehicles (source of truth)").

Вимоги в Google Cloud (перевірити ДО запуску):
  * увімкнено Google Sheets API І Google Drive API;
  * scope нижче доступні сервісному акаунту.

Колонки, що вигружаються (13; БЕЗ search_vector та year_start/end/open —
їх sync перераховує/БД генерує сама):
  id, brand, model, generation, volume, years, engine, body,
  manufacturer, injector, pump, vin, spec_code
"""
import argparse
import json
import os
import sys

SHEET_TAB = 'vehicles'
EXPORT_COLS = ['id', 'brand', 'model', 'generation', 'volume', 'years',
               'engine', 'body', 'manufacturer', 'injector', 'pump', 'vin',
               'spec_code']
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive.file']


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
    if v is None:
        return ''
    return str(v)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description='Bootstrap vehicles -> new Google Sheet')
    p.add_argument('--create', action='store_true',
                   help='реально створити таблицю (обовʼязково)')
    p.add_argument('--title', default='dieseldata · vehicles (source of truth)')
    args = p.parse_args(argv)

    if not args.create:
        raise SystemExit(
            'Відмова: bootstrap створює НОВУ таблицю. Запусти з --create свідомо. '
            'Якщо таблиця вже є — bootstrap не потрібен, користуйся sync_vehicles.py.')

    sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    owner = (os.environ.get('OWNER_EMAIL') or '').strip()
    if not sa_raw:
        raise SystemExit('ABORT: не задано GOOGLE_SERVICE_ACCOUNT_JSON.')
    if not owner:
        raise SystemExit('ABORT: не задано OWNER_EMAIL (кому дати доступ).')
    sa_info = json.loads(sa_raw)

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    sheets = build('sheets', 'v4', credentials=creds)
    drive = build('drive', 'v3', credentials=creds)

    # 1) дані з БД
    data = fetch_vehicles(db_conn_params())
    print(f'Прочитано з vehicles: {len(data)} рядків.')
    values = [EXPORT_COLS] + [[to_cell(c) for c in row] for row in data]

    # 2) створити таблицю з єдиною вкладкою `vehicles`
    created = sheets.spreadsheets().create(body={
        'properties': {'title': args.title},
        'sheets': [{'properties': {'title': SHEET_TAB}}],
    }).execute()
    sid = created['spreadsheetId']
    url = created.get('spreadsheetUrl', f'https://docs.google.com/spreadsheets/d/{sid}')
    print(f'Створено таблицю: {sid}')

    # 3) записати header + рядки (RAW — без автоперетворень чисел/дат)
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range=f'{SHEET_TAB}!A1',
        valueInputOption='RAW', body={'values': values}).execute()
    print(f'Записано {len(values)} рядків (з заголовком).')

    # 4) поділитися з власником на Редагування
    drive.permissions().create(
        fileId=sid, sendNotificationEmail=True,
        body={'type': 'user', 'role': 'writer', 'emailAddress': owner}).execute()
    print(f'Надано доступ (writer) для {owner}.')

    print('=' * 60)
    print(f'SPREADSHEET_ID={sid}')
    print(f'SPREADSHEET_URL={url}')
    print('=' * 60)
    print('Далі: постав цей id у GitHub variable SHEET_ID_VEHICLES і прожени '
          'sync-vehicles з DRY_RUN=1 для перевірки round-trip.')


if __name__ == '__main__':
    main()
