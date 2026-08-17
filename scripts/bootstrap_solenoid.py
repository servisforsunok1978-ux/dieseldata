#!/usr/bin/env python3
"""Одноразовий bootstrap: дамп public.bosch_solenoid_inj -> ІСНУЮЧА Google-таблиця.

Аналогічно bootstrap_vehicles.py. Сервісний акаунт без Workspace не може створити
Drive-файл (403), тож таблицю створює людина/конектор під власним акаунтом і
ділиться з СА (Редактор), а цей скрипт наповнює вкладку `bosch_solenoid_inj`.

Режими:
  без --sheet-id  -> INFO: друкує client_email сервісного акаунта (кому шарити).
  --sheet-id ID   -> наповнює вкладку (створює за потреби; очищає й перезаписує).
                     Потрібен --create.

Env:
  GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID_SOLENOID (або --sheet-id),
  SUPABASE_DB_PASSWORD (+HOST/USER/PORT/NAME).

Вигружаються всі 9 колонок (generated-колонок у таблиці немає; з 2026-08-17
code злито в oem — перший токен oem це колишній головний номер):
  oem, cri_type, fov_code, nozzle_dlla, nozzle_0433, nut, washer,
  oe_number, valve_cap
"""
import argparse
import json
import os
import sys

SHEET_TAB = 'bosch_solenoid_inj'
EXPORT_COLS = ['oem', 'cri_type', 'fov_code', 'nozzle_dlla', 'nozzle_0433',
               'nut', 'washer', 'oe_number', 'valve_cap']
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


def fetch_rows(conn_params):
    import psycopg2
    cols_sql = ', '.join(EXPORT_COLS)
    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {cols_sql} FROM public.bosch_solenoid_inj '
                        f'ORDER BY split_part(oem, \' \', 1);')
            return cur.fetchall()


def to_cell(v):
    return '' if v is None else str(v)


def ensure_tab(sheets, sid):
    meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
    titles = [s['properties']['title'] for s in meta.get('sheets', [])]
    if SHEET_TAB in titles:
        return
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={
        'requests': [{'addSheet': {'properties': {'title': SHEET_TAB}}}]}).execute()
    print(f'Додано вкладку `{SHEET_TAB}`.')


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description='Bootstrap bosch_solenoid_inj -> existing Google Sheet')
    p.add_argument('--create', action='store_true', help='підтвердити реальний запис')
    p.add_argument('--sheet-id', default=os.environ.get('SHEET_ID_SOLENOID'),
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
        print(f'  1) Створи порожню Google-таблицю у своєму Drive.')
        print(f'  2) Поділись нею з {client_email} на роль «Редактор».')
        print(f'  3) Перезапусти bootstrap із id цієї таблиці (input sheet_id / --sheet-id).')
        return

    if not args.create:
        raise SystemExit('Відмова: для запису додай --create свідомо.')

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    sheets = build('sheets', 'v4', credentials=creds)

    data = fetch_rows(db_conn_params())
    print(f'Прочитано з bosch_solenoid_inj: {len(data)} рядків.')
    values = [EXPORT_COLS] + [[to_cell(c) for c in row] for row in data]

    ensure_tab(sheets, args.sheet_id)
    sheets.spreadsheets().values().clear(
        spreadsheetId=args.sheet_id, range=SHEET_TAB).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=args.sheet_id, range=f'{SHEET_TAB}!A1',
        valueInputOption='RAW', body={'values': values}).execute()
    print(f'Записано {len(values)} рядків (з заголовком) у вкладку `{SHEET_TAB}`.')
    print(f'SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/{args.sheet_id}')
    print('Далі: постав цей id у GitHub variable SHEET_ID_SOLENOID і прожени '
          'sync-solenoid з dry_run=true для перевірки round-trip.')


if __name__ == '__main__':
    main()
